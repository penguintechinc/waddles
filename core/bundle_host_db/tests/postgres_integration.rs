//! Integration tests against a **real** Postgres container -- `crate::backend`'s
//! unit tests already exercise every validation/predicate-shape path in
//! isolation; this file exists because those are hand-written assertions
//! about SQL strings, and only a real server proves the actual
//! `waddles_bundle_runtime` role, RLS policy, and advisory lock agree with
//! them. Mirrors `bundle_host_kv/tests/valkey_integration.rs`'s pattern
//! (own container per scenario, `testcontainers::runners::AsyncRunner`).
//!
//! **DDL provenance -- the test DDL mirrors production, type for type.**
//! `alembic/versions/0030_bundle_app_schemas.py` (merged) creates the
//! `app_core`/`app_community` schemas and the `waddles_bundle_migrator`/
//! `waddles_bundle_runtime` roles; `hub_api/services/bundle_data_ddl.py`
//! emits each bundle's own `CREATE TABLE` + RLS policy + grant. Invoking
//! either from this Rust test would need a Python/hub-api environment as a
//! test dependency, so `apply_bundle_schema_ddl` re-issues the schema/role
//! SQL 0030 runs and `production_table_ddl` re-states the generator's fixed
//! template **verbatim** -- the exact text hub-api's golden snapshot pins
//! (`hub_api/tests/test_bundle_data_ddl.py::TestGoldenDdlSnapshots`):
//! `tenant_id integer NOT NULL`, `community_id integer NOT NULL`,
//! `version integer NOT NULL DEFAULT 1`, `FORCE ROW LEVEL SECURITY`, and a
//! policy comparing both scope columns to
//! `NULLIF(current_setting('waddles.*_id', true), '')::integer`. This test
//! once used its own hand-written shape (`tenant_id text`, `community_id
//! text NULL`, a `text`-comparing policy) and so passed while every scoped
//! statement failed against the real hub-api-created schema -- keep this
//! helper byte-identical to the generator, never "simplified".
//!
//! Requires Docker (via `testcontainers`) -- CI (`rust-bundle-host-db.yml`,
//! alongside `rust-bundle-host-kv.yml`) runs on `ubuntu-latest`, which ships
//! Docker; local runs need it too. The crate's self dev-dependency enables
//! the `test-util` feature for this test automatically, so plain
//! `cargo test --test postgres_integration` is enough.

use std::sync::Arc;

use bundle_host_db::{
    AppSchema, CapabilitySnapshot, ColumnDef, ColumnType, DbBackend, DbError, DbHost, DbScope,
    DbValue, OrderBy, PostgresBackend, Row, SchemaCache, TableSchema, TENANT_WIDE_COMMUNITY_ID,
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
/// The tenant the typed-column scenario scopes its rows to.
const TYPED_TENANT: i32 = 110;
/// The `numeric(p,s)` table `numeric_columns_round_trip_against_real_postgres`
/// runs against.
const MONEY_TABLE: &str = "money_core";

/// Tenant/community ids the scenarios scope rows to. Distinct values per
/// scenario keep the shared-container assertions independent; `COMMUNITY_*`
/// are real community ids, `TENANT_WIDE` the `0` sentinel.
const TENANT_A: i32 = 101;
const TENANT_B: i32 = 102;
const COMMUNITY_MAIN: i32 = 7;
const TENANT_WIDE: i32 = TENANT_WIDE_COMMUNITY_ID;

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

/// Re-issues the schema/role half of `alembic/versions/0030_bundle_app_schemas.py`,
/// then creates every test table from [`production_table_ddl`] -- see module
/// doc "DDL provenance" for why this isn't invoked through Alembic/hub-api
/// itself.
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
    // DDL); `money_core` carries the `numeric(p,s)` shapes.
    const PLAIN_COLUMNS: &[&str] = &[
        r#""user_ref" uuid"#,
        r#""score" bigint"#,
        r#""note" varchar(240)"#,
    ];
    const TYPED_COLUMNS: &[&str] = &[
        r#""user_ref" uuid"#,
        r#""other_ref" uuid"#,
        r#""small" integer"#,
        r#""score" bigint"#,
        r#""flag" boolean"#,
        r#""note" varchar(8)"#,
        r#""seen_at" timestamptz"#,
        r#""doc" jsonb"#,
    ];
    const MONEY_COLUMNS: &[&str] = &[
        r#""amount" numeric(10,2)"#,
        r#""units" numeric(3,0)"#,
        r#""ratio" numeric(5,5)"#,
    ];
    for (schema, table, columns) in [
        ("app_core", "fishing_core", PLAIN_COLUMNS),
        ("app_core", "other_app_core", PLAIN_COLUMNS),
        ("app_core", TYPED_TABLE, TYPED_COLUMNS),
        ("app_core", MONEY_TABLE, MONEY_COLUMNS),
    ] {
        for ddl in production_table_ddl(schema, table, columns) {
            conn.execute_unprepared(&ddl)
                .await
                .unwrap_or_else(|e| panic!("table setup failed ({ddl:?}): {e}"));
        }
    }
}

/// The fixed per-table DDL `hub_api/services/bundle_data_ddl.py`
/// (`generate_create_table_ddl`) emits, statement for statement and in the
/// same order: `CREATE TABLE` with the six platform columns, `ENABLE` +
/// `FORCE ROW LEVEL SECURITY`, the tenant/community RLS policy, and the
/// runtime-role grant. `declared_columns` is the already-rendered
/// bundle-declared column list. See the module doc -- this must stay
/// identical to the generator's output, including every platform column
/// type.
fn production_table_ddl(schema: &str, table: &str, declared_columns: &[&str]) -> Vec<String> {
    let declared_columns = declared_columns.join(",\n    ");
    let qualified = format!("\"{schema}\".\"{table}\"");
    vec![
        format!(
            "CREATE TABLE {qualified} (\n    \
                 row_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),\n    \
                 tenant_id integer NOT NULL,\n    \
                 community_id integer NOT NULL,\n    \
                 version integer NOT NULL DEFAULT 1,\n    \
                 created_at timestamptz NOT NULL DEFAULT now(),\n    \
                 updated_at timestamptz NOT NULL DEFAULT now(),\n    \
                 {declared_columns}\n)"
        ),
        format!("ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY"),
        format!("ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY"),
        format!(
            "CREATE POLICY \"{table}_tenant_isolation\" ON {qualified} USING \
             (tenant_id = NULLIF(current_setting('waddles.tenant_id', true), '')::integer \
             AND community_id = NULLIF(current_setting('waddles.community_id', true), '')::integer)"
        ),
        format!("GRANT SELECT, INSERT, UPDATE, DELETE ON {qualified} TO \"{RUNTIME_ROLE}\""),
    ]
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
    let tenant_a_scope = DbScope::new(TENANT_A, COMMUNITY_MAIN, "waddles.bot.a");
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
    let tenant_b_scope = DbScope::new(TENANT_B, COMMUNITY_MAIN, "waddles.bot.a");
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
            "SET waddles.tenant_id = '{}'; SET waddles.community_id = '{}';",
            tenant_b_scope.tenant_id, tenant_b_scope.community_id
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
    let bundle_y_scope = DbScope::new(TENANT_A, COMMUNITY_MAIN, "waddles.bot.other");
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
    let quota_scope = DbScope::new(103, TENANT_WIDE, "waddles.bot.a");
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
    let scope = Arc::new(DbScope::new(104, TENANT_WIDE, "waddles.bot.a"));

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
    let scope = DbScope::new(105, COMMUNITY_MAIN, "waddles.bot.a");

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
    let scope = DbScope::new(TYPED_TENANT, COMMUNITY_MAIN, "waddles.bot.typed");

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
    let q_scope = DbScope::new(TYPED_TENANT + 1, TENANT_WIDE, "waddles.bot.typed");
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
                 VALUES ({TYPED_TENANT}, {COMMUNITY_MAIN}, 'infinity') RETURNING row_id::text AS id"
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

/// `information_schema` data type of `app_core.<table>.<column>`, read as
/// the superuser -- what the DDL *actually* created, not what the test
/// helper claims to have asked for.
async fn column_data_type(conn: &DatabaseConnection, table: &str, column: &str) -> String {
    conn.query_one_raw(Statement::from_string(
        conn.get_database_backend(),
        format!(
            "SELECT data_type::text AS t FROM information_schema.columns \
             WHERE table_schema = 'app_core' AND table_name = '{table}' \
             AND column_name = '{column}'"
        ),
    ))
    .await
    .expect("information_schema query")
    .unwrap_or_else(|| panic!("no column {table}.{column}"))
    .try_get::<String>("", "t")
    .expect("data_type text")
}

/// Runs `body` inside a runtime-role transaction whose RLS scope GUCs are
/// `SET LOCAL` to `(tenant_id, community_id)` -- exactly what
/// `crate::backend::set_local_scope` sets, but with **no explicit predicate
/// anywhere**, so only the production RLS policy decides what is visible.
async fn rls_only_count(
    runtime: &DatabaseConnection,
    gucs: Option<(&str, &str)>,
    table: &str,
) -> Result<i64, String> {
    let txn = runtime.begin().await.expect("begin");
    if let Some((tenant, community)) = gucs {
        txn.execute_unprepared(&format!(
            "SET LOCAL waddles.tenant_id = '{tenant}'; SET LOCAL waddles.community_id = '{community}'"
        ))
        .await
        .expect("set local gucs");
    }
    let result = txn
        .query_one_raw(Statement::from_string(
            txn.get_database_backend(),
            format!("SELECT COUNT(*) AS n FROM app_core.{table}"),
        ))
        .await
        .map_err(|e| e.to_string())
        .map(|row| {
            row.expect("count row")
                .try_get::<i64>("", "n")
                .expect("count value")
        });
    txn.rollback().await.expect("rollback");
    result
}

/// Regression for the scope-column type mismatch: `hub_api/services/
/// bundle_data_ddl.py` creates `tenant_id`/`community_id` as `integer NOT
/// NULL` (and `version` as `integer`), but the crate bound/compared them as
/// `text` -- so every insert/update/get/delete/query on a real hub-api table
/// failed ("column \"tenant_id\" is of type integer but expression is of
/// type text", "operator does not exist: integer = text") while the old
/// hand-written test DDL (`text` columns) passed. This drives every op on
/// tenant+community scoped rows against the production-shaped DDL
/// ([`production_table_ddl`]) and its production RLS policy.
#[tokio::test(flavor = "multi_thread")]
async fn scope_columns_are_integers_end_to_end_against_production_ddl() {
    let (_container, superuser_url) = start().await;
    let superuser = connect_superuser(&superuser_url).await;
    apply_bundle_schema_ddl(&superuser).await;
    let runtime = Database::connect(runtime_url(&superuser_url))
        .await
        .expect("runtime role connects");
    let backend = PostgresBackend::new(runtime.clone());
    let schema = fishing_core_schema();

    // --- 1. The test DDL really is production-shaped (guards the guard) ---
    for column in ["tenant_id", "community_id", "version"] {
        assert_eq!(
            column_data_type(&superuser, "fishing_core", column).await,
            "integer",
            "{column} must be `integer`, exactly as bundle_data_ddl.py provisions it"
        );
    }

    let community = DbScope::new(TENANT_A, COMMUNITY_MAIN, "waddles.bot.a");
    let tenant_wide = DbScope::new(TENANT_A, TENANT_WIDE, "waddles.bot.a");
    let other_community = DbScope::new(TENANT_A, COMMUNITY_MAIN + 1, "waddles.bot.a");
    let other_tenant = DbScope::new(TENANT_B, COMMUNITY_MAIN, "waddles.bot.a");
    // Same two numbers, swapped: a tenant/community binding mix-up cannot
    // pass as a match.
    let swapped = DbScope::new(COMMUNITY_MAIN, TENANT_A, "waddles.bot.a");

    // --- 2. insert: stored as real integers, tenant-wide is 0 (never NULL) ---
    let row = backend
        .insert(
            &schema,
            &community,
            vec![("score".to_string(), DbValue::Int(1))],
        )
        .await
        .expect("insert under a community scope against the real DDL");
    let wide_row = backend
        .insert(
            &schema,
            &tenant_wide,
            vec![("score".to_string(), DbValue::Int(2))],
        )
        .await
        .expect("insert under a tenant-wide scope against the real DDL");
    assert_eq!((row.version, wide_row.version), (1, 1));
    assert!(
        pg_bool(
            &superuser,
            format!(
                "SELECT (tenant_id = {TENANT_A} AND community_id = {COMMUNITY_MAIN} \
                    AND pg_typeof(tenant_id) = 'integer'::regtype \
                    AND pg_typeof(community_id) = 'integer'::regtype) AS ok \
                 FROM app_core.fishing_core WHERE row_id = '{}'",
                row.row_id
            )
        )
        .await,
        "community row stored under its integer ids"
    );
    assert!(
        pg_bool(
            &superuser,
            format!(
                "SELECT (tenant_id = {TENANT_A} AND community_id = 0) AS ok \
                 FROM app_core.fishing_core WHERE row_id = '{}'",
                wide_row.row_id
            )
        )
        .await,
        "a tenant-wide row is stored with community_id = 0 (the column is NOT NULL)"
    );

    // --- 3. get: the WHERE predicate matches the owner and nobody else ---
    let got = backend
        .get(&schema, &community, &row.row_id)
        .await
        .expect("owner reads its community row");
    assert_eq!(cell(&got, "score"), &DbValue::Int(1));
    assert_eq!(got.version, 1, "int4 version decodes");
    let got_wide = backend
        .get(&schema, &tenant_wide, &wide_row.row_id)
        .await
        .expect("owner reads its tenant-wide row");
    assert_eq!(cell(&got_wide, "score"), &DbValue::Int(2));
    for (label, scope, target) in [
        ("tenant-wide scope", &tenant_wide, &row.row_id),
        ("community scope", &community, &wide_row.row_id),
        ("other community", &other_community, &row.row_id),
        ("other tenant", &other_tenant, &row.row_id),
        ("swapped ids", &swapped, &row.row_id),
    ] {
        assert_eq!(
            backend.get(&schema, scope, target).await.unwrap_err(),
            DbError::NotFound,
            "{label} must not read a row outside its (tenant, community)"
        );
    }

    // --- 4. update: version-checked, scope-checked, int4 version bumps ---
    for (label, scope) in [
        ("other community", &other_community),
        ("other tenant", &other_tenant),
        ("swapped ids", &swapped),
        ("tenant-wide scope", &tenant_wide),
    ] {
        let denied = backend
            .update(
                &schema,
                scope,
                &row.row_id,
                row.version,
                vec![("score".to_string(), DbValue::Int(999))],
            )
            .await;
        assert!(
            matches!(denied, Err(DbError::NotFound | DbError::Conflict)),
            "{label} must not update a row outside its scope: {denied:?}"
        );
    }
    let updated = backend
        .update(
            &schema,
            &community,
            &row.row_id,
            row.version,
            vec![("score".to_string(), DbValue::Int(10))],
        )
        .await
        .expect("owner updates its row");
    assert_eq!(updated.version, 2);
    let stale = backend
        .update(
            &schema,
            &community,
            &row.row_id,
            row.version,
            vec![("score".to_string(), DbValue::Int(11))],
        )
        .await;
    assert_eq!(
        stale.unwrap_err(),
        DbError::Conflict,
        "a stale expected_version conflicts"
    );
    let wide_updated = backend
        .update(
            &schema,
            &tenant_wide,
            &wide_row.row_id,
            wide_row.version,
            vec![("score".to_string(), DbValue::Int(20))],
        )
        .await
        .expect("tenant-wide owner updates its row");
    assert_eq!(wide_updated.version, 2);
    assert_eq!(
        cell(
            &backend.get(&schema, &community, &row.row_id).await.unwrap(),
            "score"
        ),
        &DbValue::Int(10),
        "the denied updates changed nothing"
    );

    // --- 5. query: each scope lists exactly its own rows ---
    for (label, scope, expected) in [
        ("community", &community, vec![row.row_id.clone()]),
        ("tenant-wide", &tenant_wide, vec![wide_row.row_id.clone()]),
        ("other community", &other_community, vec![]),
        ("other tenant", &other_tenant, vec![]),
        ("swapped ids", &swapped, vec![]),
    ] {
        let rows = backend
            .query(&schema, scope, 10, 0, None)
            .await
            .unwrap_or_else(|e| panic!("{label} query failed: {e:?}"));
        assert_eq!(
            rows.iter().map(|r| r.row_id.clone()).collect::<Vec<_>>(),
            expected,
            "{label}"
        );
    }

    // --- 6. RLS alone (no explicit predicate), production policy ---
    let (ta, tb, cm) = (
        TENANT_A.to_string(),
        TENANT_B.to_string(),
        COMMUNITY_MAIN.to_string(),
    );
    let count = |gucs| rls_only_count(&runtime, gucs, "fishing_core");
    assert_eq!(
        count(Some((ta.as_str(), cm.as_str()))).await,
        Ok(1),
        "RLS shows the community row to its own (tenant, community)"
    );
    assert_eq!(
        count(Some((ta.as_str(), "0"))).await,
        Ok(1),
        "RLS shows the tenant-wide row to community 0"
    );
    assert_eq!(
        count(Some((tb.as_str(), cm.as_str()))).await,
        Ok(0),
        "RLS hides another tenant's rows"
    );
    assert_eq!(
        count(Some((cm.as_str(), ta.as_str()))).await,
        Ok(0),
        "RLS hides rows from swapped ids"
    );
    assert_eq!(
        count(None).await,
        Ok(0),
        "no GUCs set (or set-then-reset to '') fails closed to zero rows, never an error"
    );
    // A non-numeric GUC (what a slug would be) is rejected loudly by the
    // production policy -- the exact failure the old text binding hit.
    let slug = count(Some(("acme", "main"))).await.unwrap_err();
    assert!(
        slug.contains("invalid input syntax for type integer"),
        "a slug GUC must not be silently accepted: {slug}"
    );
    // RLS also guards writes: a row outside the GUC scope cannot be inserted.
    let txn = runtime.begin().await.expect("begin");
    txn.execute_unprepared(&format!(
        "SET LOCAL waddles.tenant_id = '{TENANT_A}'; SET LOCAL waddles.community_id = '{COMMUNITY_MAIN}'"
    ))
    .await
    .expect("set local gucs");
    let forged = txn
        .execute_unprepared(&format!(
            "INSERT INTO app_core.fishing_core (tenant_id, community_id) \
             VALUES ({TENANT_B}, {COMMUNITY_MAIN})"
        ))
        .await
        .expect_err("RLS must reject a row for another tenant");
    assert!(
        forged.to_string().contains("row-level security"),
        "unexpected error: {forged}"
    );
    txn.rollback().await.expect("rollback");

    // --- 7. delete: version-checked and scope-checked ---
    for (label, scope) in [
        ("other community", &other_community),
        ("other tenant", &other_tenant),
        ("swapped ids", &swapped),
    ] {
        assert_eq!(
            backend
                .delete(&schema, scope, &row.row_id, updated.version)
                .await
                .unwrap_err(),
            DbError::NotFound,
            "{label} must not delete outside its scope"
        );
    }
    backend
        .delete(&schema, &community, &row.row_id, updated.version)
        .await
        .expect("owner deletes its community row");
    backend
        .delete(
            &schema,
            &tenant_wide,
            &wide_row.row_id,
            wide_updated.version,
        )
        .await
        .expect("owner deletes its tenant-wide row");
    assert_eq!(row_count(&superuser, "app_core", "fishing_core").await, 0);
}

/// `numeric(p,s)` end to end: written as a decimal string or an integer,
/// read back as the column's exact decimal text, ordered by value, and every
/// shape the host validator accepts is also accepted by real Postgres.
#[tokio::test(flavor = "multi_thread")]
async fn numeric_columns_round_trip_against_real_postgres() {
    let (_container, superuser_url) = start().await;
    let superuser = connect_superuser(&superuser_url).await;
    apply_bundle_schema_ddl(&superuser).await;
    let backend = PostgresBackend::new(
        Database::connect(runtime_url(&superuser_url))
            .await
            .expect("runtime role connects"),
    );
    let schema = money_schema();
    let scope = DbScope::new(120, COMMUNITY_MAIN, "waddles.bot.money");

    // --- 1. insert: decimal text, integer and NULL; exact text back ---
    let row = backend
        .insert(
            &schema,
            &scope,
            vec![
                ("amount".to_string(), text("1234.50")),
                ("units".to_string(), DbValue::Int(42)),
                ("ratio".to_string(), DbValue::Null),
            ],
        )
        .await
        .expect("insert numeric columns");
    let got = backend.get(&schema, &scope, &row.row_id).await.unwrap();
    assert_eq!(cell(&got, "amount"), &text("1234.50"));
    assert_eq!(cell(&got, "units"), &text("42"));
    assert_eq!(cell(&got, "ratio"), &DbValue::Null);
    assert!(
        pg_bool(
            &superuser,
            format!(
                "SELECT (amount = 1234.50 AND pg_typeof(amount) = 'numeric'::regtype \
                    AND units = 42 AND ratio IS NULL) AS ok \
                 FROM app_core.{MONEY_TABLE} WHERE row_id = '{}'",
                row.row_id
            )
        )
        .await,
        "stored as real numerics, not text"
    );

    // --- 2. update: scale is applied by the column, read back exactly ---
    let updated = backend
        .update(
            &schema,
            &scope,
            &row.row_id,
            row.version,
            vec![
                ("amount".to_string(), text("9.9")),
                ("units".to_string(), DbValue::Int(-7)),
                ("ratio".to_string(), text("0.12345")),
            ],
        )
        .await
        .expect("update numeric columns");
    let got = backend.get(&schema, &scope, &row.row_id).await.unwrap();
    assert_eq!(got.version, updated.version);
    assert_eq!(cell(&got, "amount"), &text("9.90"), "scale 2 is applied");
    assert_eq!(cell(&got, "units"), &text("-7"));
    assert_eq!(cell(&got, "ratio"), &text("0.12345"));

    // --- 3. every shape the host accepts is accepted by Postgres, exactly ---
    for (input, expected) in [
        ("0", "0.00"),
        ("12", "12.00"),
        ("-12", "-12.00"),
        ("+12", "12.00"),
        ("12.5", "12.50"),
        ("12.500", "12.50"),
        (".5", "0.50"),
        ("5.", "5.00"),
        ("-0.01", "-0.01"),
        ("00012.34", "12.34"),
        ("99999999.99", "99999999.99"),
        ("-99999999.9900", "-99999999.99"),
    ] {
        let inserted = backend
            .insert(&schema, &scope, vec![("amount".to_string(), text(input))])
            .await
            .unwrap_or_else(|e| panic!("{input:?} accepted by the host but not Postgres: {e:?}"));
        let back = backend
            .get(&schema, &scope, &inserted.row_id)
            .await
            .unwrap();
        assert_eq!(cell(&back, "amount"), &text(expected), "{input:?}");
    }

    // --- 4. ORDER BY sorts by numeric value, not by the text projection ---
    let q_scope = DbScope::new(121, TENANT_WIDE, "waddles.bot.money");
    let mut ids = Vec::new();
    for amount in ["100.00", "9.50", "10.25"] {
        ids.push(
            backend
                .insert(
                    &schema,
                    &q_scope,
                    vec![("amount".to_string(), text(amount))],
                )
                .await
                .unwrap()
                .row_id,
        );
    }
    let by = |descending| {
        Some(OrderBy::Column {
            name: "amount".to_string(),
            descending,
        })
    };
    let amounts = |rows: Vec<Row>| -> Vec<DbValue> {
        rows.iter().map(|r| cell(r, "amount").clone()).collect()
    };
    assert_eq!(
        amounts(
            backend
                .query(&schema, &q_scope, 10, 0, by(false))
                .await
                .unwrap()
        ),
        vec![text("9.50"), text("10.25"), text("100.00")],
        "text ordering would put 100.00 before 9.50"
    );
    assert_eq!(
        amounts(
            backend
                .query(&schema, &q_scope, 10, 0, by(true))
                .await
                .unwrap()
        ),
        vec![text("100.00"), text("10.25"), text("9.50")]
    );

    // --- 5. values that do not fit or are not exact fail loud, write nothing ---
    let rows_before = row_count(&superuser, "app_core", MONEY_TABLE).await;
    let bad_values: Vec<(&str, DbValue)> = vec![
        ("amount", text("1.234")),
        ("amount", text("123456789")),
        ("amount", text("1e3")),
        ("amount", text("NaN")),
        ("amount", text("Infinity")),
        ("amount", text("")),
        ("amount", text("12,5")),
        ("amount", DbValue::Int(100_000_000)),
        ("amount", DbValue::Float(1.5)),
        ("amount", DbValue::Bool(true)),
        ("units", text("1000")),
        ("units", text("1.5")),
        ("ratio", text("1")),
        ("ratio", DbValue::Int(1)),
    ];
    for (column, bad) in &bad_values {
        let err = backend
            .insert(&schema, &scope, vec![(column.to_string(), bad.clone())])
            .await
            .expect_err(&format!("insert of {bad:?} into {column} must fail"));
        assert_eq!(err.code(), "invalid_value", "insert {column}={bad:?}");
        let err = backend
            .update(
                &schema,
                &scope,
                &row.row_id,
                updated.version,
                vec![(column.to_string(), bad.clone())],
            )
            .await
            .expect_err(&format!("update of {column} to {bad:?} must fail"));
        assert_eq!(err.code(), "invalid_value", "update {column}={bad:?}");
    }
    assert_eq!(
        row_count(&superuser, "app_core", MONEY_TABLE).await,
        rows_before,
        "a rejected numeric must never leave a row behind"
    );
    let untouched = backend.get(&schema, &scope, &row.row_id).await.unwrap();
    assert_eq!(
        untouched.version, updated.version,
        "no rejected update may bump the version"
    );

    // --- 6. NULL in every numeric column, insert and update ---
    let nulls = || -> Vec<(String, DbValue)> {
        ["amount", "units", "ratio"]
            .iter()
            .map(|c| (c.to_string(), DbValue::Null))
            .collect()
    };
    let null_row = backend.insert(&schema, &scope, nulls()).await.unwrap();
    let back = backend
        .get(&schema, &scope, &null_row.row_id)
        .await
        .unwrap();
    assert!(back.columns.iter().all(|(_, v)| *v == DbValue::Null));
    backend
        .update(&schema, &scope, &row.row_id, updated.version, nulls())
        .await
        .expect("update every numeric column to NULL");
    let back = backend.get(&schema, &scope, &row.row_id).await.unwrap();
    assert!(back.columns.iter().all(|(_, v)| *v == DbValue::Null));
}

/// The `numeric(p,s)` table's schema, matching `MONEY_COLUMNS`.
fn money_schema() -> TableSchema {
    let col = |name: &str, precision: u8, scale: u8| ColumnDef {
        name: name.to_string(),
        sql_type: ColumnType::numeric(precision, scale).expect("valid numeric"),
        nullable: true,
        is_user_ref: false,
    };
    TableSchema::validated(
        AppSchema::Core,
        MONEY_TABLE,
        vec![col("amount", 10, 2), col("units", 3, 0), col("ratio", 5, 5)],
    )
    .unwrap()
}
