//! Integration tests against a **real** Postgres container -- `crate::backend`'s
//! unit tests already exercise every validation/predicate-shape path in
//! isolation; this file exists because those are hand-written assertions
//! about SQL strings, and only a real server proves the actual
//! `waddles_bundle_runtime` role, RLS policy, and advisory lock agree with
//! them. Mirrors `bundle_host_kv/tests/valkey_integration.rs`'s pattern
//! (own container per scenario, `testcontainers::runners::AsyncRunner`).
//!
//! **DDL provenance.** `alembic/versions/0030_bundle_app_schemas.py`
//! (merged) creates the `app_core`/`app_community` schemas and the
//! `waddles_bundle_migrator`/`waddles_bundle_runtime` roles, but is a
//! Python/Alembic migration -- invoking it from this Rust integration test
//! would require a Python/hub-api environment as a test dependency, which
//! this crate does not otherwise need. `hub_api/services/bundle_data_ddl.py`
//! (PR #430, the generator that would emit a bundle's own `CREATE TABLE`)
//! is not merged yet. So `apply_bundle_schema_ddl` below re-issues the same
//! SQL 0030 runs (roles, schemas, grants, `REVOKE ALL ... FROM PUBLIC` on
//! `public`) plus a hand-written `CREATE TABLE`/RLS policy matching the
//! design doc's fixed per-app-table template (§3.3/§3.4/§7:
//! `row_id`/`tenant_id`/`community_id`/`version`/`created_at`/`updated_at`
//! platform columns, `FORCE ROW LEVEL SECURITY`, a policy keyed on the same
//! `waddles.tenant_id`/`waddles.community_id` GUCs `crate::backend::
//! set_local_scope` sets) -- faithfully reproducing what those two
//! migrations do, not a simplified stand-in for them.
//!
//! Requires Docker (via `testcontainers`) -- CI (`rust-bundle-host-db.yml`,
//! alongside `rust-bundle-host-kv.yml`) runs on `ubuntu-latest`, which ships
//! Docker; local runs need it too. The crate's self dev-dependency enables
//! the `test-util` feature for this test automatically, so plain
//! `cargo test --test postgres_integration` is enough.

use std::sync::Arc;

use bundle_host_db::{
    AppSchema, CapabilitySnapshot, ColumnDef, ColumnType, DbBackend, DbError, DbHost, DbScope,
    DbValue, OrderBy, PostgresBackend, Row, SchemaCache, TableSchema,
};
use sea_orm::{ConnectionTrait, Database, DatabaseConnection, Statement, TransactionTrait};
use testcontainers::core::logs::LogSource;
use testcontainers::core::wait::LogWaitStrategy;
use testcontainers::core::{ContainerPort, WaitFor};
use testcontainers::runners::AsyncRunner;
use testcontainers::{ContainerAsync, GenericImage, ImageExt};

/// Exact upstream version tag -- confirmed at the time this was pinned to
/// resolve to `sha256:f3bd19c606e442c3d7bdfa8002e03fe260a1023351e0ea4598032022b68dd6e3`
/// (`docker inspect --format='{{index .RepoDigests 0}}' postgres:17.6-bookworm`).
/// `testcontainers`' `GenericImage::new(name, tag)` API pulls by `name:tag`,
/// not `name@sha256:digest` (see `bundle_host_kv`'s own `VALKEY_IMAGE` doc
/// for the same limitation) -- this is the most specific pin it supports:
/// an exact version+distro tag, never a floating `17`/`latest`.
const POSTGRES_IMAGE: &str = "postgres";
const POSTGRES_TAG: &str = "17.6-bookworm";

const SUPERUSER_PASSWORD: &str = "postgres_test_superuser_pw";
const RUNTIME_PASSWORD: &str = "waddles_bundle_runtime_test_pw";
const RUNTIME_ROLE: &str = "waddles_bundle_runtime";
/// The all-column-types table `typed_columns_round_trip_against_real_postgres`
/// runs against -- see `apply_bundle_schema_ddl`.
const TYPED_TABLE: &str = "typed_core";

/// Starts one Postgres container and returns it alongside a superuser
/// connection URL -- callers open additional connections (as the
/// superuser, for setup/assertions, or as `waddles_bundle_runtime`, for the
/// backend under test and the DDL-denial check) against the same server.
async fn start() -> (ContainerAsync<GenericImage>, String) {
    let container = GenericImage::new(POSTGRES_IMAGE, POSTGRES_TAG)
        .with_exposed_port(ContainerPort::Tcp(5432))
        .with_wait_for(WaitFor::log(
            // Postgres logs this line twice: once on stdout for the
            // initdb-time throwaway server, once on stderr for the real
            // one -- `BothStd` + `with_times(2)` waits for both, so this
            // never races a connection against the restart in between
            // (confirmed empirically: `docker logs --tags` shows the first
            // occurrence on stdout, the second on stderr).
            LogWaitStrategy::new(
                LogSource::BothStd,
                "database system is ready to accept connections",
            )
            .with_times(2),
        ))
        .with_env_var("POSTGRES_PASSWORD", SUPERUSER_PASSWORD)
        .with_env_var("POSTGRES_DB", "waddles_test")
        .start()
        .await
        .expect("postgres test container starts");
    let host = container.get_host().await.expect("container host");
    let port = container
        .get_host_port_ipv4(5432)
        .await
        .expect("container port");
    (
        container,
        format!("postgres://postgres:{SUPERUSER_PASSWORD}@{host}:{port}/waddles_test"),
    )
}

async fn connect_superuser(url: &str) -> DatabaseConnection {
    Database::connect(url)
        .await
        .expect("superuser connection opens")
}

fn runtime_url(superuser_url: &str) -> String {
    // Same host/port/db, different role/password -- proves the DDL-denial
    // and backend-under-test paths both go through the real
    // `waddles_bundle_runtime` grant set, not the superuser's.
    superuser_url.replacen(
        &format!("postgres:{SUPERUSER_PASSWORD}"),
        &format!("{RUNTIME_ROLE}:{RUNTIME_PASSWORD}"),
        1,
    )
}

/// Re-issues the schema/role half of `alembic/versions/0030_bundle_app_schemas.py`
/// plus a hand-written per-app `CREATE TABLE` matching the design doc's
/// fixed template (row_id/tenant_id/community_id/version/created_at/
/// updated_at, `FORCE ROW LEVEL SECURITY`, a policy on the same
/// `waddles.tenant_id`/`waddles.community_id` GUCs `crate::backend` sets)
/// -- see module doc "DDL provenance" for why this isn't invoked through
/// Alembic itself. `community_id` uses `IS NOT DISTINCT FROM` against
/// `NULLIF(current_setting(...), '')` so a tenant-only (no community)
/// scope's GUC still matches a `NULL` `community_id` column, exactly like
/// `crate::backend::tenant_predicate`'s own `IS NULL` branch --
/// **discovered empirically while writing this test**: `crate::backend::
/// set_local_scope` binds `scope.community`'s `None` as a SQL `NULL`
/// parameter to `set_config(..., $2, true)`, and Postgres's `set_config`
/// silently turns a `NULL` new-value into an **empty string** GUC, not an
/// unset/NULL one (`current_setting(..., true)` then returns `''`, not
/// `NULL`) -- a naive `community_id IS NOT DISTINCT FROM current_setting(...)`
/// policy (without the `NULLIF`) therefore never matches a real `NULL`
/// `community_id` column and silently denies every tenant-only bundle's
/// own rows under RLS. This is a real landmine for whatever DDL generator
/// (`hub_api/services/bundle_data_ddl.py`, PR #430) eventually emits the
/// production RLS policy -- flagged in this PR's "remaining work".
async fn apply_bundle_schema_ddl(conn: &DatabaseConnection) {
    let statements = [
        "CREATE SCHEMA IF NOT EXISTS app_core",
        "CREATE SCHEMA IF NOT EXISTS app_community",
        &format!(
            "DO $$ BEGIN \
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{RUNTIME_ROLE}') THEN \
                    EXECUTE format('CREATE ROLE {RUNTIME_ROLE} LOGIN NOSUPERUSER NOCREATEDB \
                        NOCREATEROLE NOREPLICATION PASSWORD %L', '{RUNTIME_PASSWORD}'); \
                END IF; \
            END $$;"
        ),
        &format!("GRANT USAGE ON SCHEMA app_core, app_community TO {RUNTIME_ROLE}"),
        &format!("ALTER ROLE {RUNTIME_ROLE} SET search_path = app_core, app_community, pg_catalog"),
        &format!("REVOKE ALL ON SCHEMA public FROM {RUNTIME_ROLE}"),
        &format!("REVOKE TEMPORARY ON DATABASE waddles_test FROM {RUNTIME_ROLE}"),
        "REVOKE TEMPORARY ON DATABASE waddles_test FROM PUBLIC",
    ];
    for sql in statements {
        conn.execute_unprepared(sql)
            .await
            .unwrap_or_else(|e| panic!("setup statement failed ({sql:?}): {e}"));
    }

    // `fishing_core`/`other_app_core` keep the original uuid/int/text shape
    // the isolation scenarios were written against; `typed_core` carries
    // every declared column type (uuid incl. `user_ref`, int4, timestamptz,
    // jsonb, and a length-limited varchar like hub-api's `text(max_len)`
    // DDL) -- what the type round-trip test needs.
    const PLAIN_COLUMNS: &str = "user_ref uuid, score bigint, note text";
    const TYPED_COLUMNS: &str = "user_ref uuid, other_ref uuid, small int4, score int8, \
                                 flag boolean, note varchar(8), seen_at timestamptz, doc jsonb";
    for (schema, table, columns) in [
        ("app_core", "fishing_core", PLAIN_COLUMNS),
        ("app_core", "other_app_core", PLAIN_COLUMNS),
        ("app_core", TYPED_TABLE, TYPED_COLUMNS),
    ] {
        let ddl = format!(
            "CREATE TABLE {schema}.{table} (
                row_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                tenant_id text NOT NULL,
                community_id text,
                version bigint NOT NULL DEFAULT 1,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now(),
                {columns}
            )"
        );
        conn.execute_unprepared(&ddl).await.expect("create table");
        conn.execute_unprepared(&format!(
            "ALTER TABLE {schema}.{table} ENABLE ROW LEVEL SECURITY"
        ))
        .await
        .expect("enable rls");
        conn.execute_unprepared(&format!(
            "ALTER TABLE {schema}.{table} FORCE ROW LEVEL SECURITY"
        ))
        .await
        .expect("force rls");
        conn.execute_unprepared(&format!(
            "CREATE POLICY {table}_tenant_isolation ON {schema}.{table} USING ( \
                tenant_id = current_setting('waddles.tenant_id', true) \
                AND community_id IS NOT DISTINCT FROM NULLIF(current_setting('waddles.community_id', true), '') \
            )"
        ))
        .await
        .expect("create rls policy");
        conn.execute_unprepared(&format!(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON {schema}.{table} TO {RUNTIME_ROLE}"
        ))
        .await
        .expect("grant dml");
    }
}

fn fishing_core_schema() -> TableSchema {
    TableSchema::validated(
        AppSchema::Core,
        "fishing_core",
        vec![
            ColumnDef {
                name: "user_ref".to_string(),
                sql_type: ColumnType::Uuid,
                nullable: true,
                is_user_ref: true,
            },
            ColumnDef {
                name: "score".to_string(),
                sql_type: ColumnType::Int8,
                nullable: true,
                is_user_ref: false,
            },
            ColumnDef {
                name: "note".to_string(),
                sql_type: ColumnType::Text,
                nullable: true,
                is_user_ref: false,
            },
        ],
    )
    .unwrap()
}

fn other_app_core_schema() -> TableSchema {
    TableSchema::validated(AppSchema::Core, "other_app_core", vec![]).unwrap()
}

async fn row_count(conn: &DatabaseConnection, schema: &str, table: &str) -> i64 {
    let row = conn
        .query_one_raw(Statement::from_string(
            conn.get_database_backend(),
            format!("SELECT COUNT(*) AS n FROM {schema}.{table}"),
        ))
        .await
        .expect("count query")
        .expect("count row");
    row.try_get::<i64>("", "n").expect("count value")
}

/// Sequential scenario covering the bulk of the "remaining work" isolation
/// list in one shared container/table set (container startup dominates
/// per-test wall time; splitting each assertion into its own `#[tokio::test]`
/// would multiply that cost eight-fold for no isolation benefit, since every
/// assertion below is read-only or uses a distinct tenant/app to avoid
/// cross-contamination). Each numbered step is independently meaningful --
/// a failure names exactly which guarantee broke.
#[tokio::test(flavor = "multi_thread")]
async fn bundle_db_capability_isolation_and_safety_against_real_postgres() {
    let (_container, superuser_url) = start().await;
    let superuser_conn = connect_superuser(&superuser_url).await;
    apply_bundle_schema_ddl(&superuser_conn).await;

    let runtime_conn = Database::connect(runtime_url(&superuser_url))
        .await
        .expect("runtime role connects");
    let backend = PostgresBackend::new(runtime_conn);
    let schema = fishing_core_schema();

    // --- 1. Insert round-trips, SQL-injection-shaped values are inert ---
    let injection_note = "acme'); DROP TABLE app_core.fishing_core; --".to_string();
    let tenant_a_scope = DbScope::new("tenant-a", Some("main".to_string()), "waddles.bot.a");
    let inserted = backend
        .insert(
            &schema,
            &tenant_a_scope,
            vec![
                ("score".to_string(), DbValue::Int(42)),
                ("note".to_string(), DbValue::Text(injection_note.clone())),
            ],
        )
        .await
        .expect("insert succeeds despite injection-shaped value");
    assert_eq!(
        row_count(&superuser_conn, "app_core", "fishing_core").await,
        1,
        "the injection-shaped value must be bound as data, never executed as SQL \
         (a successful DROP would make this table not exist at all)"
    );
    let note_value = inserted
        .columns
        .iter()
        .find(|(k, _)| k == "note")
        .map(|(_, v)| v.clone());
    assert_eq!(note_value, Some(DbValue::Text(injection_note)));

    // --- 2. non-UUID user_ref is rejected before it ever reaches Postgres ---
    let bad_user_ref = backend
        .insert(
            &schema,
            &tenant_a_scope,
            vec![(
                "user_ref".to_string(),
                DbValue::Text("not-a-uuid".to_string()),
            )],
        )
        .await
        .unwrap_err();
    assert_eq!(bad_user_ref.code(), "invalid_value");
    assert_eq!(
        row_count(&superuser_conn, "app_core", "fishing_core").await,
        1,
        "a rejected user_ref must never reach the table"
    );

    // --- 3. Tenant isolation, BOTH layers active: tenant B cannot read tenant A's row ---
    let tenant_b_scope = DbScope::new("tenant-b", Some("main".to_string()), "waddles.bot.a");
    let cross_tenant_get = backend
        .get(&schema, &tenant_b_scope, &inserted.row_id)
        .await;
    assert_eq!(cross_tenant_get.unwrap_err(), DbError::NotFound);
    let cross_tenant_update = backend
        .update(
            &schema,
            &tenant_b_scope,
            &inserted.row_id,
            inserted.version,
            vec![("score".to_string(), DbValue::Int(999))],
        )
        .await;
    assert!(
        matches!(
            cross_tenant_update,
            Err(DbError::NotFound | DbError::Conflict)
        ),
        "tenant B must not be able to update tenant A's row"
    );

    // --- 3b. Successful update + query for the owning tenant (the "happy path"
    // every isolation/denial check above is a variation of) ---
    let updated = backend
        .update(
            &schema,
            &tenant_a_scope,
            &inserted.row_id,
            inserted.version,
            vec![("score".to_string(), DbValue::Int(100))],
        )
        .await
        .expect("tenant A updates its own row");
    assert_eq!(updated.version, inserted.version + 1);
    let updated_score = updated
        .columns
        .iter()
        .find(|(k, _)| k == "score")
        .map(|(_, v)| v.clone());
    assert_eq!(updated_score, Some(DbValue::Int(100)));

    let queried = backend
        .query(&schema, &tenant_a_scope, 10, 0, None)
        .await
        .expect("tenant A queries its own table");
    assert_eq!(queried.len(), 1);
    assert_eq!(queried[0].row_id, inserted.row_id);
    assert_eq!(queried[0].version, updated.version);

    let queried_for_tenant_b = backend
        .query(&schema, &tenant_b_scope, 10, 0, None)
        .await
        .expect("query itself succeeds, just returns nothing");
    assert!(
        queried_for_tenant_b.is_empty(),
        "tenant B's query must never see tenant A's row"
    );

    // --- 4. RLS defends alone: disable the explicit predicate manually, RLS must still block ---
    // Runs the exact same SELECT `crate::backend::get_impl` would, minus its
    // `tenant_id = $n AND community_id = $m` predicate -- proves the RLS
    // policy alone rejects tenant B's read even with the second layer gone.
    let runtime_txn = Database::connect(runtime_url(&superuser_url))
        .await
        .expect("second runtime connection");
    runtime_txn
        .execute_unprepared(&format!(
            "SET waddles.tenant_id = '{}'; SET waddles.community_id = 'main';",
            tenant_b_scope.tenant
        ))
        .await
        .expect("set rls gucs for tenant b");
    let rls_only_row = runtime_txn
        .query_one_raw(Statement::from_string(
            runtime_txn.get_database_backend(),
            format!(
                "SELECT 1 AS present FROM app_core.fishing_core WHERE row_id = '{}'",
                inserted.row_id
            ),
        ))
        .await
        .expect("query without explicit predicate");
    assert!(
        rls_only_row.is_none(),
        "RLS alone (no explicit tenant predicate in this raw query) must still hide tenant A's row from tenant B"
    );

    // --- 5. Explicit predicate defends alone: disable RLS, predicate must still block ---
    superuser_conn
        .execute_unprepared("ALTER TABLE app_core.fishing_core DISABLE ROW LEVEL SECURITY")
        .await
        .expect("disable rls for this check");
    let predicate_only_cross_tenant = backend
        .get(&schema, &tenant_b_scope, &inserted.row_id)
        .await;
    assert_eq!(
        predicate_only_cross_tenant.unwrap_err(),
        DbError::NotFound,
        "the explicit tenant_id/community_id predicate alone (RLS disabled) must still block tenant B"
    );
    let same_tenant_still_works = backend
        .get(&schema, &tenant_a_scope, &inserted.row_id)
        .await;
    assert!(
        same_tenant_still_works.is_ok(),
        "tenant A must still read its own row"
    );
    superuser_conn
        .execute_unprepared("ALTER TABLE app_core.fishing_core ENABLE ROW LEVEL SECURITY")
        .await
        .expect("re-enable rls");

    // --- 6. Cross-bundle isolation: bundle Y's table never exposes bundle X's row ---
    let other_schema = other_app_core_schema();
    let bundle_y_scope = DbScope::new("tenant-a", Some("main".to_string()), "waddles.bot.other");
    let cross_bundle = backend
        .get(&other_schema, &bundle_y_scope, &inserted.row_id)
        .await;
    assert_eq!(
        cross_bundle.unwrap_err(),
        DbError::NotFound,
        "app_id b's table is a physically different table -- app_id a's row_id can never resolve there"
    );

    // --- 7. Data-plane role cannot run DDL ---
    for ddl in [
        "CREATE TABLE app_core.hacked (id int)",
        "ALTER TABLE app_core.fishing_core ADD COLUMN hacked text",
        "DROP TABLE app_core.fishing_core",
    ] {
        let err = runtime_txn
            .execute_unprepared(ddl)
            .await
            .expect_err(&format!("{ddl:?} must be denied for {RUNTIME_ROLE}"));
        let msg = err.to_string().to_lowercase();
        assert!(
            msg.contains("permission denied") || msg.contains("must be owner"),
            "expected a permission error for {ddl:?}, got: {err}"
        );
    }
    assert_eq!(
        row_count(&superuser_conn, "app_core", "fishing_core").await,
        1,
        "the DROP TABLE attempt above must not have succeeded"
    );

    // --- 8. Quota enforcement (with a small test-only cap) ---
    let quota_backend = PostgresBackend::new(
        Database::connect(runtime_url(&superuser_url))
            .await
            .expect("quota-test connection"),
    )
    .with_row_cap_for_test(2);
    let quota_scope = DbScope::new("tenant-quota", None, "waddles.bot.a");
    quota_backend
        .insert(&schema, &quota_scope, vec![])
        .await
        .expect("first row under cap");
    quota_backend
        .insert(&schema, &quota_scope, vec![])
        .await
        .expect("second row hits but does not exceed cap");
    let over_quota = quota_backend.insert(&schema, &quota_scope, vec![]).await;
    assert_eq!(
        over_quota.unwrap_err().code(),
        "quota_exceeded",
        "a third insert must be rejected once the (test) row cap is reached"
    );

    // --- 9. Statement timeout actually fires (same SET LOCAL mechanism `set_local_scope` uses) ---
    let timeout_conn = Database::connect(runtime_url(&superuser_url))
        .await
        .expect("timeout-test connection");
    let timeout_txn = timeout_conn.begin().await.expect("begin txn");
    timeout_txn
        .execute_unprepared("SET LOCAL statement_timeout = 500")
        .await
        .expect("set local statement_timeout");
    let slow_query = timeout_txn.execute_unprepared("SELECT pg_sleep(2)").await;
    let err = slow_query.expect_err("a 2s sleep under a 500ms statement_timeout must be canceled");
    assert!(
        err.to_string().to_lowercase().contains("statement timeout")
            || err
                .to_string()
                .to_lowercase()
                .contains("canceling statement"),
        "expected a statement_timeout cancellation, got: {err}"
    );

    // --- 10. Delete: wrong version conflicts, wrong tenant 404s, then the real delete ---
    let delete_wrong_version = backend
        .delete(&schema, &tenant_a_scope, &inserted.row_id, inserted.version)
        .await;
    assert_eq!(
        delete_wrong_version.unwrap_err(),
        DbError::Conflict,
        "deleting with a stale expected_version (the row is now at `updated.version`) must conflict"
    );
    let delete_wrong_tenant = backend
        .delete(&schema, &tenant_b_scope, &inserted.row_id, updated.version)
        .await;
    assert_eq!(delete_wrong_tenant.unwrap_err(), DbError::NotFound);
    backend
        .delete(&schema, &tenant_a_scope, &inserted.row_id, updated.version)
        .await
        .expect("tenant A deletes its own row with the correct version");
    let after_delete = backend
        .get(&schema, &tenant_a_scope, &inserted.row_id)
        .await;
    assert_eq!(after_delete.unwrap_err(), DbError::NotFound);
    let delete_already_gone = backend
        .delete(&schema, &tenant_a_scope, &inserted.row_id, updated.version)
        .await;
    assert_eq!(
        delete_already_gone.unwrap_err(),
        DbError::NotFound,
        "deleting an already-deleted row must 404, not conflict"
    );

    // --- 11. Host-side rejections that never reach Postgres (row_id shape, version range) ---
    let bad_row_id = backend.get(&schema, &tenant_a_scope, "not-a-uuid").await;
    assert_eq!(bad_row_id.unwrap_err().code(), "invalid_value");
    let bad_delete_row_id = backend
        .delete(&schema, &tenant_a_scope, "not-a-uuid", 1)
        .await;
    assert_eq!(bad_delete_row_id.unwrap_err().code(), "invalid_value");
}

/// Race-safety: `MAX_ROWS_PER_APP` concurrent inserts against a cap of 5
/// (via `with_row_cap_for_test`) must admit **exactly** 5 rows, never more
/// -- proving `lock_quota_scope`'s `pg_advisory_xact_lock` actually
/// serializes the check-then-insert critical section under real
/// concurrency, not just in the single-threaded sequential test above.
#[tokio::test(flavor = "multi_thread")]
async fn concurrent_inserts_never_exceed_the_row_cap() {
    const CAP: i64 = 5;
    const CONCURRENT_ATTEMPTS: usize = 20;

    let (_container, superuser_url) = start().await;
    let superuser_conn = connect_superuser(&superuser_url).await;
    apply_bundle_schema_ddl(&superuser_conn).await;

    let schema = Arc::new(fishing_core_schema());
    let scope = Arc::new(DbScope::new("tenant-race", None, "waddles.bot.a"));

    let mut handles = Vec::with_capacity(CONCURRENT_ATTEMPTS);
    for _ in 0..CONCURRENT_ATTEMPTS {
        let url = runtime_url(&superuser_url);
        let schema = Arc::clone(&schema);
        let scope = Arc::clone(&scope);
        handles.push(tokio::spawn(async move {
            let backend = PostgresBackend::new(Database::connect(url).await.expect("connect"))
                .with_row_cap_for_test(CAP);
            backend.insert(&schema, &scope, vec![]).await
        }));
    }

    let mut succeeded = 0usize;
    let mut quota_denied = 0usize;
    for handle in handles {
        match handle.await.expect("task join") {
            Ok(_) => succeeded += 1,
            Err(DbError::QuotaExceeded(_)) => quota_denied += 1,
            Err(other) => panic!("unexpected error under concurrency: {other:?}"),
        }
    }

    assert_eq!(
        succeeded, CAP as usize,
        "exactly the cap must succeed, no more, no fewer"
    );
    assert_eq!(quota_denied, CONCURRENT_ATTEMPTS - CAP as usize);
    let actual_rows = row_count(&superuser_conn, "app_core", "fishing_core").await;
    assert_eq!(
        actual_rows, CAP,
        "the physical row count must match the cap exactly -- a lost-update race would leave \
         either fewer rows (undercounting failures) or, without the fix, more than the cap"
    );
}

/// Drives every op through the **public [`DbHost`] wrapper** (resolve +
/// authorize + backend dispatch + metrics/logging, `src/lib.rs`), not the
/// raw [`PostgresBackend`] the scenario test above uses directly -- the
/// unit tests in `lib.rs` already exercise `DbHost`'s authorize/resolve
/// wiring against a `FakeBackend`; this proves the exact same wrapper
/// reaches a real Postgres backend end to end for all five ops
/// (insert/get/update/delete/query), including the `not_granted`
/// short-circuit before any table/DB lookup happens.
#[tokio::test(flavor = "multi_thread")]
async fn db_host_wrapper_reaches_a_real_backend_for_every_op() {
    let (_container, superuser_url) = start().await;
    let superuser_conn = connect_superuser(&superuser_url).await;
    apply_bundle_schema_ddl(&superuser_conn).await;

    let host = DbHost::new(PostgresBackend::new(
        Database::connect(runtime_url(&superuser_url))
            .await
            .expect("runtime connects"),
    ));
    let schemas = SchemaCache::new();
    schemas.update("waddles.bot.a", fishing_core_schema());
    let snapshot = CapabilitySnapshot::new();
    let scope = DbScope::new("tenant-host", Some("main".to_string()), "waddles.bot.a");

    // Denied before ever touching the schema cache or the DB.
    let denied = host
        .insert(&scope, &schemas, &snapshot, vec![])
        .await
        .unwrap_err();
    assert_eq!(denied.code(), "invalid_column");

    snapshot.update("waddles.bot.a", ["storage.tables".to_string()]);

    let row = host
        .insert(
            &scope,
            &schemas,
            &snapshot,
            vec![("score".to_string(), DbValue::Int(7))],
        )
        .await
        .expect("insert through DbHost");
    let fetched = host
        .get(&scope, &schemas, &snapshot, &row.row_id)
        .await
        .expect("get through DbHost");
    assert_eq!(fetched.row_id, row.row_id);

    let updated = host
        .update(
            &scope,
            &schemas,
            &snapshot,
            &row.row_id,
            row.version,
            vec![("score".to_string(), DbValue::Int(8))],
        )
        .await
        .expect("update through DbHost");
    assert_eq!(updated.version, row.version + 1);

    let queried = host
        .query(&scope, &schemas, &snapshot, 10, 0, None)
        .await
        .expect("query through DbHost");
    assert_eq!(queried.len(), 1);

    host.delete(&scope, &schemas, &snapshot, &row.row_id, updated.version)
        .await
        .expect("delete through DbHost");
    let after = host.get(&scope, &schemas, &snapshot, &row.row_id).await;
    assert_eq!(after.unwrap_err(), DbError::NotFound);
}

fn typed_core_schema() -> TableSchema {
    let col = |name: &str, sql_type: ColumnType, is_user_ref: bool| ColumnDef {
        name: name.to_string(),
        sql_type,
        nullable: true,
        is_user_ref,
    };
    TableSchema::validated(
        AppSchema::Core,
        TYPED_TABLE,
        vec![
            col("user_ref", ColumnType::Uuid, true),
            col("other_ref", ColumnType::Uuid, false),
            col("small", ColumnType::Int4, false),
            col("score", ColumnType::Int8, false),
            col("flag", ColumnType::Bool, false),
            col("note", ColumnType::Text, false),
            col("seen_at", ColumnType::Timestamptz, false),
            col("doc", ColumnType::Jsonb, false),
        ],
    )
    .unwrap()
}

/// The declared-column value `name` in `row` (panics if absent -- a test bug).
fn cell<'a>(row: &'a Row, name: &str) -> &'a DbValue {
    row.columns
        .iter()
        .find(|(k, _)| k == name)
        .map(|(_, v)| v)
        .unwrap_or_else(|| panic!("column {name:?} missing from {row:?}"))
}

fn text(s: &str) -> DbValue {
    DbValue::Text(s.to_string())
}

/// `cell(row, name)` parsed as JSON -- jsonb round-trips *semantically*
/// (Postgres normalizes whitespace/key order), never byte-for-byte.
fn json_cell(row: &Row, name: &str) -> serde_json::Value {
    match cell(row, name) {
        DbValue::Text(s) => serde_json::from_str(s)
            .unwrap_or_else(|e| panic!("{name:?} is not valid JSON ({e}): {s:?}")),
        other => panic!("{name:?} should read back as text, got {other:?}"),
    }
}

/// A single boolean computed by Postgres itself over the stored row --
/// proves the column holds a real `uuid`/`timestamptz`/`jsonb`, not text.
async fn pg_bool(conn: &DatabaseConnection, sql: String) -> bool {
    conn.query_one_raw(Statement::from_string(conn.get_database_backend(), sql))
        .await
        .expect("assertion query")
        .expect("assertion row")
        .try_get::<bool>("", "ok")
        .expect("assertion bool")
}

/// Regression for the bundle-DB typed-column bug: every `uuid` (incl.
/// `user_ref`), `timestamptz`, and `jsonb` column used to fail against real
/// Postgres ("column x is of type uuid but expression is of type text" on
/// write; jsonb/timestamptz could not be decoded as `String` on read)
/// because values were bound as `text`. The other integration tests here
/// only touch bool/int/text columns, so nothing exercised the typed paths.
/// One shared container/table (startup dominates wall time); each numbered
/// step names the guarantee it proves.
#[tokio::test(flavor = "multi_thread")]
async fn typed_columns_round_trip_against_real_postgres() {
    let (_container, superuser_url) = start().await;
    let superuser_conn = connect_superuser(&superuser_url).await;
    apply_bundle_schema_ddl(&superuser_conn).await;

    let backend = PostgresBackend::new(
        Database::connect(runtime_url(&superuser_url))
            .await
            .expect("runtime role connects"),
    );
    let schema = typed_core_schema();
    let scope = DbScope::new(
        "tenant-typed",
        Some("main".to_string()),
        "waddles.bot.typed",
    );

    // --- 1. insert: every typed column, incl. user_ref and a non-UTC offset ---
    let user_ref = "3f2b8c1e-7a4d-4e0b-9c55-1d2e3f4a5b6c";
    let inserted = backend
        .insert(
            &schema,
            &scope,
            vec![
                ("user_ref".to_string(), text(user_ref)),
                (
                    "other_ref".to_string(),
                    text("9A1B2C3D-4E5F-4A6B-8C7D-0E1F2A3B4C5D"),
                ),
                ("small".to_string(), DbValue::Int(-7)),
                ("score".to_string(), DbValue::Int(9_000_000_000)),
                ("flag".to_string(), DbValue::Bool(true)),
                ("note".to_string(), text("hello")),
                (
                    "seen_at".to_string(),
                    text("2026-01-02T03:04:05.123456+02:00"),
                ),
                (
                    "doc".to_string(),
                    text(r#"{"a": [1, 2], "b": {"c": null}}"#),
                ),
            ],
        )
        .await
        .expect("insert with uuid/user_ref/timestamptz/jsonb columns");
    assert_eq!(inserted.version, 1);

    // --- 2. read: uuid canonical, timestamptz as RFC 3339 UTC, jsonb as JSON text ---
    let fetched = backend
        .get(&schema, &scope, &inserted.row_id)
        .await
        .expect("get decodes uuid/timestamptz/jsonb columns");
    assert_eq!(cell(&fetched, "user_ref"), &text(user_ref));
    assert_eq!(
        cell(&fetched, "other_ref"),
        &text("9a1b2c3d-4e5f-4a6b-8c7d-0e1f2a3b4c5d"),
        "a uuid reads back in canonical lowercase hyphenated form"
    );
    assert_eq!(cell(&fetched, "small"), &DbValue::Int(-7));
    assert_eq!(cell(&fetched, "score"), &DbValue::Int(9_000_000_000));
    assert_eq!(cell(&fetched, "flag"), &DbValue::Bool(true));
    assert_eq!(cell(&fetched, "note"), &text("hello"));
    assert_eq!(
        cell(&fetched, "seen_at"),
        &text("2026-01-02T01:04:05.123456Z"),
        "+02:00 input must read back as the same instant in UTC, independent of session TimeZone"
    );
    assert_eq!(
        json_cell(&fetched, "doc"),
        serde_json::json!({"a": [1, 2], "b": {"c": null}})
    );
    // Postgres itself agrees the stored values are real typed values.
    let row_id = &inserted.row_id;
    assert!(
        pg_bool(
            &superuser_conn,
            format!(
                "SELECT (seen_at = TIMESTAMPTZ '2026-01-02 01:04:05.123456+00' \
                    AND doc @> '{{\"b\":{{\"c\":null}}}}'::jsonb \
                    AND doc #>> '{{a,1}}' = '2' \
                    AND user_ref = '{user_ref}'::uuid) AS ok \
                 FROM app_core.{TYPED_TABLE} WHERE row_id = '{row_id}'"
            )
        )
        .await
    );

    // --- 3. every UUID spelling the host accepts is stored canonically ---
    for spelling in [
        "9a1b2c3d4e5f4a6b8c7d0e1f2a3b4c5d",
        "{9a1b2c3d-4e5f-4a6b-8c7d-0e1f2a3b4c5d}",
        "urn:uuid:9a1b2c3d-4e5f-4a6b-8c7d-0e1f2a3b4c5d",
    ] {
        let row = backend
            .insert(
                &schema,
                &scope,
                vec![("other_ref".to_string(), text(spelling))],
            )
            .await
            .unwrap_or_else(|e| panic!("uuid spelling {spelling:?} rejected: {e:?}"));
        let back = backend.get(&schema, &scope, &row.row_id).await.unwrap();
        assert_eq!(
            cell(&back, "other_ref"),
            &text("9a1b2c3d-4e5f-4a6b-8c7d-0e1f2a3b4c5d"),
            "spelling {spelling:?}"
        );
    }

    // --- 4. update: typed columns change, untouched ones are preserved ---
    let new_user_ref = "00000000-0000-4000-8000-000000000001";
    let updated = backend
        .update(
            &schema,
            &scope,
            &inserted.row_id,
            inserted.version,
            vec![
                ("user_ref".to_string(), text(new_user_ref)),
                ("seen_at".to_string(), text("2027-06-30T23:59:59Z")),
                ("doc".to_string(), text(r#"[1, "x", {"k": true}]"#)),
                ("small".to_string(), DbValue::Int(5)),
            ],
        )
        .await
        .expect("update uuid/user_ref/timestamptz/jsonb columns");
    assert_eq!(updated.version, inserted.version + 1);
    let after_update = backend
        .get(&schema, &scope, &inserted.row_id)
        .await
        .unwrap();
    assert_eq!(after_update.version, updated.version);
    assert_eq!(cell(&after_update, "user_ref"), &text(new_user_ref));
    assert_eq!(
        cell(&after_update, "seen_at"),
        &text("2027-06-30T23:59:59.000000Z")
    );
    assert_eq!(
        json_cell(&after_update, "doc"),
        serde_json::json!([1, "x", {"k": true}])
    );
    assert_eq!(cell(&after_update, "small"), &DbValue::Int(5));
    assert_eq!(
        cell(&after_update, "other_ref"),
        cell(&fetched, "other_ref"),
        "a column the update did not name must keep its value"
    );
    assert_eq!(cell(&after_update, "score"), cell(&fetched, "score"));
    assert_eq!(cell(&after_update, "flag"), cell(&fetched, "flag"));

    // A single-typed-column update (the minimal set clause) works too.
    let doc_only = backend
        .update(
            &schema,
            &scope,
            &inserted.row_id,
            updated.version,
            vec![("doc".to_string(), text("\"just a string\""))],
        )
        .await
        .expect("update of a single jsonb column");
    let after_doc = backend
        .get(&schema, &scope, &inserted.row_id)
        .await
        .unwrap();
    assert_eq!(
        json_cell(&after_doc, "doc"),
        serde_json::json!("just a string")
    );
    assert_eq!(cell(&after_doc, "user_ref"), &text(new_user_ref));

    // --- 5. NULL in every column type: insert and update, read back as Null ---
    let all_columns = [
        "user_ref",
        "other_ref",
        "small",
        "score",
        "flag",
        "note",
        "seen_at",
        "doc",
    ];
    let nulls = || -> Vec<(String, DbValue)> {
        all_columns
            .iter()
            .map(|c| (c.to_string(), DbValue::Null))
            .collect()
    };
    let null_row = backend
        .insert(&schema, &scope, nulls())
        .await
        .expect("insert of an explicit NULL into every column type");
    let back = backend
        .get(&schema, &scope, &null_row.row_id)
        .await
        .unwrap();
    assert!(
        back.columns.iter().all(|(_, v)| *v == DbValue::Null),
        "every column must read back NULL: {back:?}"
    );
    let nulled = backend
        .update(&schema, &scope, &inserted.row_id, doc_only.version, nulls())
        .await
        .expect("update of every column type to NULL");
    let back = backend
        .get(&schema, &scope, &inserted.row_id)
        .await
        .unwrap();
    assert_eq!(back.version, nulled.version);
    assert!(
        back.columns.iter().all(|(_, v)| *v == DbValue::Null),
        "every column must read back NULL after the update: {back:?}"
    );

    // --- 6. query: typed columns decode, and ORDER BY sorts the stored type ---
    let q_scope = DbScope::new("tenant-typed-q", None, "waddles.bot.typed");
    // Insert order differs from chronological order; the middle row is only
    // earliest once its -05:00 offset is applied (2026-01-01T04:00:00Z).
    let instants = [
        "2026-03-01T00:00:00Z",
        "2025-12-31T23:00:00-05:00",
        "2026-02-01T00:00:00+00:00",
    ];
    let mut q_ids = Vec::new();
    for (i, instant) in instants.iter().enumerate() {
        let row = backend
            .insert(
                &schema,
                &q_scope,
                vec![
                    ("seen_at".to_string(), text(instant)),
                    ("doc".to_string(), text(&format!("{{\"i\": {i}}}"))),
                    (
                        "user_ref".to_string(),
                        text(&format!("00000000-0000-4000-8000-00000000000{i}")),
                    ),
                ],
            )
            .await
            .unwrap_or_else(|e| panic!("insert {instant:?}: {e:?}"));
        q_ids.push(row.row_id);
    }
    let order_of =
        |rows: &[Row]| -> Vec<String> { rows.iter().map(|r| r.row_id.clone()).collect() };
    let by = |name: &str, descending: bool| {
        Some(OrderBy::Column {
            name: name.to_string(),
            descending,
        })
    };
    let asc = backend
        .query(&schema, &q_scope, 10, 0, by("seen_at", false))
        .await
        .expect("query ordered by a timestamptz column");
    assert_eq!(
        order_of(&asc),
        vec![q_ids[1].clone(), q_ids[2].clone(), q_ids[0].clone()],
        "ascending by instant, not by insertion order"
    );
    assert_eq!(
        cell(&asc[0], "seen_at"),
        &text("2026-01-01T04:00:00.000000Z")
    );
    assert_eq!(json_cell(&asc[0], "doc"), serde_json::json!({"i": 1}));
    assert_eq!(
        cell(&asc[0], "user_ref"),
        &text("00000000-0000-4000-8000-000000000001")
    );
    let desc = backend
        .query(&schema, &q_scope, 10, 0, by("seen_at", true))
        .await
        .unwrap();
    assert_eq!(
        order_of(&desc),
        vec![q_ids[0].clone(), q_ids[2].clone(), q_ids[1].clone()]
    );
    // Ordering by a uuid / jsonb column is valid SQL and keeps decoding.
    for column in ["user_ref", "doc"] {
        let rows = backend
            .query(&schema, &q_scope, 10, 0, by(column, false))
            .await
            .unwrap_or_else(|e| panic!("query ordered by {column}: {e:?}"));
        assert_eq!(rows.len(), 3, "order by {column}");
    }
    let default_order = backend.query(&schema, &q_scope, 10, 0, None).await.unwrap();
    assert_eq!(default_order.len(), 3);

    // --- 7. malformed values fail loud as invalid_value, never a partial write ---
    let rows_before = row_count(&superuser_conn, "app_core", TYPED_TABLE).await;
    let bad_values: Vec<(&str, DbValue)> = vec![
        ("user_ref", text("not-a-uuid")),
        ("other_ref", text("9a1b2c3d-4e5f-4a6b-8c7d")),
        ("seen_at", text("not a timestamp")),
        ("seen_at", text("now")),
        ("seen_at", text("infinity")),
        ("seen_at", text("2026-01-01")),
        ("seen_at", text("2026-01-01T00:00:00")),
        ("seen_at", text("2026-13-01T00:00:00Z")),
        ("seen_at", text("2026-02-30T00:00:00Z")),
        ("seen_at", text("2026-01-01T24:00:00Z")),
        ("doc", text("{not json")),
        // Valid JSON that Postgres jsonb refuses (NUL escape) -- caught by
        // the SQLSTATE class-22 mapping, not the host-side parse.
        ("doc", text(r#""a\u0000b""#)),
        // Longer than the column's varchar(8) -- also SQLSTATE class 22.
        ("note", text("far too long for the column")),
        ("small", DbValue::Int(i64::from(i32::MAX) + 1)),
        ("flag", DbValue::Int(1)),
    ];
    for (column, bad) in &bad_values {
        let err = backend
            .insert(&schema, &scope, vec![(column.to_string(), bad.clone())])
            .await
            .expect_err(&format!("insert of {bad:?} into {column} must fail"));
        assert_eq!(
            err.code(),
            "invalid_value",
            "insert {column}={bad:?} -> {err:?}"
        );
    }
    assert_eq!(
        row_count(&superuser_conn, "app_core", TYPED_TABLE).await,
        rows_before,
        "a rejected typed value must never leave a row behind"
    );
    // The same bad values on the update path leave the row untouched.
    let victim = backend
        .insert(&schema, &scope, vec![("note".to_string(), text("ok"))])
        .await
        .expect("a valid insert still works after the rejected ones");
    for (column, bad) in &bad_values {
        let err = backend
            .update(
                &schema,
                &scope,
                &victim.row_id,
                victim.version,
                vec![(column.to_string(), bad.clone())],
            )
            .await
            .expect_err(&format!("update of {column} to {bad:?} must fail"));
        assert_eq!(
            err.code(),
            "invalid_value",
            "update {column}={bad:?} -> {err:?}"
        );
    }
    let victim_back = backend.get(&schema, &scope, &victim.row_id).await.unwrap();
    assert_eq!(
        victim_back.version, victim.version,
        "no rejected update may bump the version"
    );
    assert_eq!(cell(&victim_back, "note"), &text("ok"));

    // --- 8. a non-finite timestamptz reads back loudly, never as NULL ---
    let infinite_id = superuser_conn
        .query_one_raw(Statement::from_string(
            superuser_conn.get_database_backend(),
            format!(
                "INSERT INTO app_core.{TYPED_TABLE} (tenant_id, community_id, seen_at) \
                 VALUES ('tenant-typed', 'main', 'infinity') RETURNING row_id::text AS id"
            ),
        ))
        .await
        .expect("superuser inserts an infinite timestamp")
        .expect("returning row")
        .try_get::<String>("", "id")
        .expect("row id text");
    let infinite = backend.get(&schema, &scope, &infinite_id).await.unwrap();
    assert_eq!(cell(&infinite, "seen_at"), &text("infinity"));
}
