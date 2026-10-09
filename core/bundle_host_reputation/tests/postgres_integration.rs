//! Integration tests against a **real** Postgres container: the store's SQL
//! (row lock, rolling-24h cap, ledger atomicity, live membership predicate)
//! and the `waddles_bundle_reputation` role's privilege boundary can only be
//! proven by a real server. The schema under test is the EXACT shipped DDL --
//! `scripts/db/bundle_reputation_store.sql`, which alembic 0043 also executes
//! -- applied on top of hand-written minimal prerequisites (`tenants`,
//! `communities`, `community_members`, `app_catalog` and a copy of 0041's
//! `bundle_reputation_adjustments`; the full legacy baseline needs the whole
//! docker stack, see `alembic/tests/pg_docker.py`).
//!
//! Requires Docker via `testcontainers`. Run with
//! `cargo test -p bundle-host-reputation --test postgres_integration`.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;

use bundle_capability_gate::{
    HostInvokeScopeBuilder, MembershipCheck, ScopeKind, SnapshotMembership, TenantTier,
};
use bundle_host_reputation::{
    connect, load_membership, ConnectConfig, PostgresReputationStore, ReputationCaps,
    ReputationError, ReputationScope, ReputationStore,
};
use sea_orm::{ConnectionTrait, Database, DatabaseConnection, Statement};
use testcontainers::core::logs::LogSource;
use testcontainers::core::wait::LogWaitStrategy;
use testcontainers::core::{ContainerPort, WaitFor};
use testcontainers::runners::AsyncRunner;
use testcontainers::{ContainerAsync, GenericImage, ImageExt};
use uuid::Uuid;

const POSTGRES_IMAGE: &str = "postgres";
const POSTGRES_TAG: &str = "17.6-bookworm";
const SUPERUSER_PASSWORD: &str = "postgres_test_superuser_pw";
const REP_PASSWORD: &str = "waddles_bundle_reputation_test_pw";
const APP_ID: &str = "waddles.core.test-reputation";
const OTHER_APP_ID: &str = "waddles.core.other-reputation";
const SHIPPED_DDL: &str = include_str!("../../../scripts/db/bundle_reputation_store.sql");

const TENANT: i32 = 1;
const COMMUNITY: i32 = 10;
const OTHER_TENANT: i32 = 2;
const OTHER_COMMUNITY: i32 = 20;

struct Fixture {
    _container: ContainerAsync<GenericImage>,
    su: DatabaseConnection,
    store: PostgresReputationStore,
    rep_conn: DatabaseConnection,
    /// How to open ANOTHER pool as the reputation role -- a second replica
    /// (or the same one after a restart) against the same database.
    cfg: ConnectConfig,
    member: Uuid,
}

/// Per-user cap `per_user`, per-scope cap effectively unlimited -- the shape
/// every pre-existing test wants (it exercises the per-user cap only).
fn caps(per_user: i64) -> ReputationCaps {
    ReputationCaps {
        per_user_daily_abs_max: per_user,
        per_scope_daily_abs_max: i64::MAX,
    }
}

/// Both caps explicit.
fn caps2(per_user: i64, per_scope: i64) -> ReputationCaps {
    ReputationCaps {
        per_user_daily_abs_max: per_user,
        per_scope_daily_abs_max: per_scope,
    }
}

/// A brand-new pool + store as the reputation role: no state shared with the
/// fixture's store except the database itself (a second replica / a restart).
async fn fresh_replica(f: &Fixture) -> PostgresReputationStore {
    PostgresReputationStore::new(
        connect(&f.cfg, REP_PASSWORD)
            .await
            .expect("second replica connects"),
    )
}

fn scope() -> ReputationScope {
    ReputationScope {
        tenant_id: TENANT,
        community_id: COMMUNITY,
        app_id: APP_ID.to_string(),
    }
}

async fn exec(conn: &DatabaseConnection, sql: &str) {
    conn.execute_unprepared(sql)
        .await
        .unwrap_or_else(|e| panic!("sql failed: {e}\n{sql}"));
}

async fn scalar_i64(conn: &DatabaseConnection, sql: &str) -> i64 {
    conn.query_one_raw(Statement::from_string(sea_orm::DbBackend::Postgres, sql))
        .await
        .unwrap()
        .unwrap()
        .try_get_by_index::<i64>(0)
        .unwrap()
}

async fn fixture() -> Fixture {
    let container = GenericImage::new(POSTGRES_IMAGE, POSTGRES_TAG)
        .with_exposed_port(ContainerPort::Tcp(5432))
        .with_wait_for(WaitFor::log(
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
    let host = container.get_host().await.unwrap().to_string();
    let port = container.get_host_port_ipv4(5432).await.unwrap();
    let su_url = format!("postgres://postgres:{SUPERUSER_PASSWORD}@{host}:{port}/waddles_test");
    let su = Database::connect(&su_url)
        .await
        .expect("superuser connects");

    // Prerequisites (minimal shapes of the legacy tables + 0041's ledger).
    exec(
        &su,
        "CREATE TABLE tenants (id SERIAL PRIMARY KEY, slug TEXT);
         CREATE TABLE communities (id SERIAL PRIMARY KEY, tenant_id INTEGER REFERENCES tenants(id));
         CREATE TABLE community_members (
             id SERIAL PRIMARY KEY,
             community_id INTEGER REFERENCES communities(id) ON DELETE CASCADE,
             user_id VARCHAR(255),
             is_active BOOLEAN DEFAULT true,
             left_at TIMESTAMP,
             removed_at TIMESTAMP);
         CREATE TABLE hub_users (id SERIAL PRIMARY KEY);
         CREATE TABLE app_catalog (app_id VARCHAR(255) PRIMARY KEY);
         CREATE TABLE bundle_reputation_adjustments (
             id BIGSERIAL PRIMARY KEY,
             app_id VARCHAR(255) NOT NULL REFERENCES app_catalog(app_id),
             tenant_id INTEGER NOT NULL REFERENCES tenants(id),
             community_id INTEGER REFERENCES communities(id) ON DELETE CASCADE,
             target_user_uuid VARCHAR(36) NOT NULL,
             scope VARCHAR(20) NOT NULL CHECK (scope IN ('community', 'tenant')),
             delta INTEGER NOT NULL,
             reason_code VARCHAR(100) NOT NULL,
             occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
             reversal_of BIGINT REFERENCES bundle_reputation_adjustments(id));
         INSERT INTO tenants (id, slug) VALUES (1, 't1'), (2, 't2');
         INSERT INTO communities (id, tenant_id) VALUES (10, 1), (20, 2);
         INSERT INTO app_catalog (app_id) VALUES
             ('waddles.core.test-reputation'), ('waddles.core.other-reputation');",
    )
    .await;
    // The role must exist BEFORE the shipped DDL so its grants block fires
    // (same ordering alembic 0043 guarantees).
    exec(
        &su,
        &format!(
            "CREATE ROLE waddles_bundle_reputation LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE \
             NOREPLICATION PASSWORD '{REP_PASSWORD}'"
        ),
    )
    .await;
    exec(&su, SHIPPED_DDL).await;
    // Shipped DDL is idempotent: a second application must succeed.
    exec(&su, SHIPPED_DDL).await;

    let member = Uuid::new_v4();
    exec(
        &su,
        &format!("INSERT INTO community_members (community_id, user_uuid) VALUES (10, '{member}')"),
    )
    .await;

    let cfg = ConnectConfig {
        host,
        port,
        name: "waddles_test".to_string(),
        user: "waddles_bundle_reputation".to_string(),
    };
    let rep_conn = connect(&cfg, REP_PASSWORD)
        .await
        .expect("reputation role connects");
    let store = PostgresReputationStore::new(rep_conn.clone());
    Fixture {
        _container: container,
        su,
        store,
        rep_conn,
        cfg,
        member,
    }
}

async fn ledger_count(f: &Fixture) -> i64 {
    scalar_i64(&f.su, "SELECT COUNT(*) FROM bundle_reputation_adjustments").await
}

#[tokio::test]
async fn adjust_and_get_round_trip_with_one_ledger_row_per_applied_adjustment() {
    let f = fixture().await;
    assert_eq!(f.store.get(&scope(), f.member).await, Ok(0));
    assert_eq!(
        f.store
            .adjust(&scope(), f.member, 5, "game.win", caps(100))
            .await,
        Ok(5)
    );
    assert_eq!(
        f.store
            .adjust(&scope(), f.member, -2, "game.loss", caps(100))
            .await,
        Ok(3)
    );
    assert_eq!(f.store.get(&scope(), f.member).await, Ok(3));
    assert_eq!(ledger_count(&f).await, 2);
    assert_eq!(
        scalar_i64(
            &f.su,
            "SELECT SUM(delta)::BIGINT FROM bundle_reputation_adjustments WHERE scope='community'"
        )
        .await,
        3
    );
}

#[tokio::test]
async fn non_members_are_rejected_for_every_flavor_of_non_membership() {
    let f = fixture().await;
    let stranger = Uuid::new_v4();
    let left = Uuid::new_v4();
    let removed = Uuid::new_v4();
    let inactive = Uuid::new_v4();
    let other_community = Uuid::new_v4();
    exec(
        &f.su,
        &format!(
            "INSERT INTO community_members (community_id, user_uuid, left_at) VALUES (10, '{left}', NOW());
             INSERT INTO community_members (community_id, user_uuid, removed_at) VALUES (10, '{removed}', NOW());
             INSERT INTO community_members (community_id, user_uuid, is_active) VALUES (10, '{inactive}', false);
             INSERT INTO community_members (community_id, user_uuid) VALUES (20, '{other_community}');"
        ),
    )
    .await;
    for who in [stranger, left, removed, inactive, other_community] {
        assert_eq!(
            f.store.adjust(&scope(), who, 1, "r", caps(100)).await,
            Err(ReputationError::NotAMember),
            "adjust {who}"
        );
        assert_eq!(
            f.store.get(&scope(), who).await,
            Err(ReputationError::NotAMember),
            "get {who}"
        );
    }
    // Tenant mismatch: the member is real, but the scope names the wrong tenant.
    let wrong_tenant = ReputationScope {
        tenant_id: OTHER_TENANT,
        community_id: COMMUNITY,
        app_id: APP_ID.to_string(),
    };
    assert_eq!(
        f.store
            .adjust(&wrong_tenant, f.member, 1, "r", caps(100))
            .await,
        Err(ReputationError::NotAMember)
    );
    // And a member of community 20 is not reachable through community 10's scope
    // (covered by `other_community` above) nor through tenant 1 + community 20.
    let cross = ReputationScope {
        tenant_id: TENANT,
        community_id: OTHER_COMMUNITY,
        app_id: APP_ID.to_string(),
    };
    assert_eq!(
        f.store
            .adjust(&cross, other_community, 1, "r", caps(100))
            .await,
        Err(ReputationError::NotAMember)
    );
    // Nothing was written by any rejected call.
    assert_eq!(ledger_count(&f).await, 0);
    assert_eq!(
        scalar_i64(&f.su, "SELECT COUNT(*) FROM bundle_reputation_scores").await,
        0
    );
}

#[tokio::test]
async fn a_member_with_a_null_user_uuid_is_unreachable_fail_closed() {
    let f = fixture().await;
    exec(
        &f.su,
        "INSERT INTO community_members (community_id) VALUES (10), (10)",
    )
    .await;
    // No UUID can ever name a NULL-uuid row.
    assert_eq!(
        f.store.get(&scope(), Uuid::nil()).await,
        Err(ReputationError::NotAMember)
    );
}

#[tokio::test]
async fn rolling_24h_cap_counts_absolute_applied_deltas_and_expires_old_rows() {
    let f = fixture().await;
    assert_eq!(
        f.store.adjust(&scope(), f.member, 6, "a", caps(10)).await,
        Ok(6)
    );
    // |-5| + 6 = 11 > 10
    assert_eq!(
        f.store.adjust(&scope(), f.member, -5, "b", caps(10)).await,
        Err(ReputationError::DailyCapExceeded { cap: 10 })
    );
    // Exactly at the cap is allowed.
    assert_eq!(
        f.store.adjust(&scope(), f.member, -4, "c", caps(10)).await,
        Ok(2)
    );
    // Rejected attempt wrote nothing: still two ledger rows.
    assert_eq!(ledger_count(&f).await, 2);
    // Age every ledger row out of the window: the cap resets.
    exec(
        &f.su,
        "UPDATE bundle_reputation_adjustments SET occurred_at = NOW() - INTERVAL '25 hours'",
    )
    .await;
    assert_eq!(
        f.store.adjust(&scope(), f.member, 10, "d", caps(10)).await,
        Ok(12)
    );
    // The cap is per user: another member has a fresh budget.
    let other = Uuid::new_v4();
    exec(
        &f.su,
        &format!("INSERT INTO community_members (community_id, user_uuid) VALUES (10, '{other}')"),
    )
    .await;
    assert_eq!(
        f.store.adjust(&scope(), other, 10, "e", caps(10)).await,
        Ok(10)
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn concurrent_adjusts_serialize_on_the_row_lock_and_never_exceed_the_cap() {
    let f = fixture().await;
    let store = Arc::new(PostgresReputationStore::new(f.rep_conn.clone()));
    let mut tasks = Vec::new();
    for _ in 0..24 {
        let store = Arc::clone(&store);
        let member = f.member;
        tasks.push(tokio::spawn(async move {
            store.adjust(&scope(), member, 1, "race", caps(10)).await
        }));
    }
    let mut ok = 0;
    let mut capped = 0;
    for t in tasks {
        match t.await.unwrap() {
            Ok(_) => ok += 1,
            Err(ReputationError::DailyCapExceeded { .. }) => capped += 1,
            Err(other) => panic!("unexpected error: {other:?}"),
        }
    }
    assert_eq!(ok, 10, "exactly the cap's worth of adjusts apply");
    assert_eq!(capped, 14);
    assert_eq!(f.store.get(&scope(), f.member).await, Ok(10));
    assert_eq!(ledger_count(&f).await, 10);
}

/// regression: pr-741 review -- `COALESCE(cm.is_active, TRUE)` treated a NULL
/// `is_active` (the column is nullable) as an ACTIVE member. NULL must be a
/// non-member on every path: read, write, the in-transaction live re-check and
/// the snapshot loader that feeds the gate's pre-filter.
#[tokio::test]
async fn a_null_is_active_member_is_a_non_member_on_every_path_fail_closed() {
    let f = fixture().await;
    let null_active = Uuid::new_v4();
    exec(
        &f.su,
        &format!(
            "INSERT INTO community_members (community_id, user_uuid, is_active) \
             VALUES (10, '{null_active}', NULL)"
        ),
    )
    .await;
    assert_eq!(
        scalar_i64(
            &f.su,
            &format!(
                "SELECT COUNT(*) FROM community_members \
                 WHERE user_uuid = '{null_active}' AND is_active IS NULL"
            )
        )
        .await,
        1,
        "precondition: the row really has a NULL is_active"
    );

    assert_eq!(
        f.store.get(&scope(), null_active).await,
        Err(ReputationError::NotAMember)
    );
    assert_eq!(
        f.store
            .adjust(&scope(), null_active, 1, "r", caps(100))
            .await,
        Err(ReputationError::NotAMember)
    );
    assert_eq!(ledger_count(&f).await, 0);
    assert_eq!(
        scalar_i64(&f.su, "SELECT COUNT(*) FROM bundle_reputation_scores").await,
        0
    );

    // Snapshot loader: only the real (TRUE) member, never the NULL one.
    let rows = load_membership(&f.rep_conn, Some(TENANT)).await.unwrap();
    assert_eq!(rows.len(), 1, "{rows:?}");
    assert_eq!(rows[0].user, f.member);

    // In-transaction live re-check: a member that WAS active (so a stale
    // snapshot would still pass the gate) goes NULL -> the write is refused.
    assert_eq!(
        f.store.adjust(&scope(), f.member, 1, "ok", caps(100)).await,
        Ok(1)
    );
    exec(
        &f.su,
        &format!(
            "UPDATE community_members SET is_active = NULL WHERE user_uuid = '{}'",
            f.member
        ),
    )
    .await;
    assert_eq!(
        f.store
            .adjust(&scope(), f.member, 1, "again", caps(100))
            .await,
        Err(ReputationError::NotAMember)
    );
    assert_eq!(
        f.store.get(&scope(), f.member).await,
        Err(ReputationError::NotAMember)
    );
    assert_eq!(ledger_count(&f).await, 1, "only the pre-NULL adjust landed");
}

/// regression: pr-741 review -- a zero delta consumed no quota yet opened a
/// transaction, appended a ledger row and bumped `adjustment_count` on every
/// call (unbounded ledger growth for any write-granted bundle). It is now
/// rejected loudly before any write.
#[tokio::test]
async fn a_zero_delta_is_rejected_loudly_and_writes_nothing() {
    let f = fixture().await;
    for _ in 0..3 {
        assert!(matches!(
            f.store
                .adjust(&scope(), f.member, 0, "noop", caps(100))
                .await,
            Err(ReputationError::Invalid(m)) if m.contains("non-zero")
        ));
    }
    assert_eq!(ledger_count(&f).await, 0);
    assert_eq!(
        scalar_i64(&f.su, "SELECT COUNT(*) FROM bundle_reputation_scores").await,
        0,
        "not even the score row is created"
    );

    // On a member that already has a row: counters must not move either.
    assert_eq!(
        f.store
            .adjust(&scope(), f.member, 2, "real", caps(100))
            .await,
        Ok(2)
    );
    assert!(f
        .store
        .adjust(&scope(), f.member, 0, "noop", caps(100))
        .await
        .is_err());
    assert_eq!(ledger_count(&f).await, 1);
    assert_eq!(
        scalar_i64(
            &f.su,
            "SELECT adjustment_count FROM bundle_reputation_scores WHERE community_id = 10"
        )
        .await,
        1
    );
}

/// Inserts one more active, identified member into `community`.
async fn add_member(f: &Fixture, community: i32) -> Uuid {
    let u = Uuid::new_v4();
    exec(
        &f.su,
        &format!(
            "INSERT INTO community_members (community_id, user_uuid) VALUES ({community}, '{u}')"
        ),
    )
    .await;
    u
}

/// regression: pr-741 review -- the per-scope daily cap lived only in the
/// gate's in-memory ledger (resets on restart, multiplies per replica). The
/// store now enforces it durably from the audit ledger: a brand-new pool (a
/// second replica, or this one after a restart) over the same database sees
/// the budget already spent.
#[tokio::test]
async fn the_per_scope_cap_is_durable_across_a_fresh_replica_and_binds_across_users() {
    let f = fixture().await;
    let a = f.member;
    let b = add_member(&f, COMMUNITY).await;
    let c = add_member(&f, COMMUNITY).await;

    // Replica 1 spends 6 of the 10-unit scope budget on user `a` (the per-user
    // cap is generous: only the scope cap can trip below).
    assert_eq!(
        f.store.adjust(&scope(), a, 6, "r", caps2(100, 10)).await,
        Ok(6)
    );

    // "Restart": replica 2 has no in-memory state, only the database.
    let replica2 = fresh_replica(&f).await;
    // A DIFFERENT user: the per-user cap cannot be what stops this. The cap is
    // over absolute deltas, so -5 spends 5 just like +5 (6 + 5 > 10).
    for delta in [5, -5] {
        assert_eq!(
            replica2
                .adjust(&scope(), b, delta, "r", caps2(100, 10))
                .await,
            Err(ReputationError::ScopeQuotaExceeded { cap: 10 }),
            "delta {delta}"
        );
    }
    // Exactly at the cap is allowed (6 + 4 = 10).
    assert_eq!(
        replica2.adjust(&scope(), b, 4, "r", caps2(100, 10)).await,
        Ok(4)
    );
    // Budget now exhausted for everyone -- on BOTH replicas.
    for store in [&f.store, &replica2] {
        assert_eq!(
            store.adjust(&scope(), c, 1, "r", caps2(100, 10)).await,
            Err(ReputationError::ScopeQuotaExceeded { cap: 10 })
        );
    }
    // Rejected attempts wrote nothing: exactly the two applied rows.
    assert_eq!(ledger_count(&f).await, 2);
    assert_eq!(
        scalar_i64(
            &f.su,
            "SELECT SUM(ABS(delta))::BIGINT FROM bundle_reputation_adjustments"
        )
        .await,
        10
    );

    // Another APP in the same community has its own budget ...
    let other_app = ReputationScope {
        app_id: OTHER_APP_ID.to_string(),
        ..scope()
    };
    assert_eq!(
        replica2.adjust(&other_app, c, 7, "r", caps2(100, 10)).await,
        Ok(7)
    );
    // ... and so does another tenant/community.
    let t2_member = add_member(&f, OTHER_COMMUNITY).await;
    let t2_scope = ReputationScope {
        tenant_id: OTHER_TENANT,
        community_id: OTHER_COMMUNITY,
        app_id: APP_ID.to_string(),
    };
    assert_eq!(
        replica2
            .adjust(&t2_scope, t2_member, 9, "r", caps2(100, 10))
            .await,
        Ok(9)
    );

    // The window rolls: age every row out and the budget resets.
    exec(
        &f.su,
        "UPDATE bundle_reputation_adjustments SET occurred_at = NOW() - INTERVAL '25 hours'",
    )
    .await;
    assert_eq!(
        f.store.adjust(&scope(), c, 10, "r", caps2(100, 10)).await,
        Ok(17)
    );
}

/// The scope cap is checked under a per-scope advisory lock: adjusts for
/// DIFFERENT users (no shared score row) from TWO replicas still serialize, so
/// the SUM-then-insert can never overshoot the cap.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn concurrent_adjusts_for_different_users_across_replicas_never_exceed_the_scope_cap() {
    let f = fixture().await;
    let users = [
        f.member,
        add_member(&f, COMMUNITY).await,
        add_member(&f, COMMUNITY).await,
    ];
    let stores = [
        Arc::new(PostgresReputationStore::new(f.rep_conn.clone())),
        Arc::new(fresh_replica(&f).await),
    ];
    let mut tasks = Vec::new();
    for i in 0..30 {
        let store = Arc::clone(&stores[i % 2]);
        let user = users[i % 3];
        tasks.push(tokio::spawn(async move {
            store
                .adjust(&scope(), user, 1, "race", caps2(100, 10))
                .await
        }));
    }
    let (mut ok, mut capped) = (0, 0);
    for t in tasks {
        match t.await.unwrap() {
            Ok(_) => ok += 1,
            Err(ReputationError::ScopeQuotaExceeded { cap: 10 }) => capped += 1,
            Err(other) => panic!("unexpected error: {other:?}"),
        }
    }
    assert_eq!(ok, 10, "exactly the scope cap's worth of adjusts apply");
    assert_eq!(capped, 20);
    assert_eq!(ledger_count(&f).await, 10);
    assert_eq!(
        scalar_i64(
            &f.su,
            "SELECT SUM(ABS(delta))::BIGINT FROM bundle_reputation_adjustments"
        )
        .await,
        10
    );
}

#[tokio::test]
async fn adjust_is_atomic_a_failing_ledger_insert_rolls_back_the_score() {
    let f = fixture().await;
    // app_id not in app_catalog -> the ledger INSERT violates its FK AFTER the
    // score UPDATE ran; the whole transaction (incl. the score row creation)
    // must roll back.
    let bad = ReputationScope {
        tenant_id: TENANT,
        community_id: COMMUNITY,
        app_id: "waddles.unknown.not-in-catalog".to_string(),
    };
    let err = f
        .store
        .adjust(&bad, f.member, 7, "x", caps(100))
        .await
        .unwrap_err();
    assert!(matches!(err, ReputationError::Backend(_)), "{err:?}");
    assert_eq!(f.store.get(&scope(), f.member).await, Ok(0));
    assert_eq!(
        scalar_i64(&f.su, "SELECT COUNT(*) FROM bundle_reputation_scores").await,
        0
    );
    assert_eq!(ledger_count(&f).await, 0);
}

#[tokio::test]
async fn invalid_reason_is_rejected_before_any_write() {
    let f = fixture().await;
    for bad in ["", "Has Upper", "pii@example.com"] {
        assert!(matches!(
            f.store.adjust(&scope(), f.member, 1, bad, caps(100)).await,
            Err(ReputationError::Invalid(_))
        ));
    }
    assert!(matches!(
        f.store.adjust(&scope(), f.member, 1, "ok", caps(-1)).await,
        Err(ReputationError::Invalid(_))
    ));
    assert_eq!(ledger_count(&f).await, 0);
}

#[tokio::test]
async fn role_is_least_privilege_dml_only_append_only_ledger_no_foreign_tables() {
    let f = fixture().await;
    f.store
        .adjust(&scope(), f.member, 1, "seed", caps(100))
        .await
        .unwrap();
    for (sql, why) in [
        (
            "DELETE FROM bundle_reputation_scores",
            "no DELETE on scores",
        ),
        (
            "UPDATE bundle_reputation_adjustments SET delta = 99",
            "ledger is append-only (no UPDATE)",
        ),
        (
            "DELETE FROM bundle_reputation_adjustments",
            "ledger is append-only (no DELETE)",
        ),
        ("SELECT * FROM hub_users", "no access to unrelated tables"),
        (
            "SELECT * FROM community_members",
            "membership SELECT is column-scoped, not whole-table",
        ),
        (
            "INSERT INTO community_members (community_id) VALUES (10)",
            "no DML on membership",
        ),
        (
            "UPDATE communities SET tenant_id = 2",
            "no DML on communities",
        ),
        ("CREATE TABLE evil (x int)", "no DDL"),
    ] {
        assert!(
            f.rep_conn.execute_unprepared(sql).await.is_err(),
            "should be denied ({why}): {sql}"
        );
    }
    // The grants it DOES have.
    assert!(f
        .rep_conn
        .execute_unprepared("SELECT community_id, user_uuid, is_active FROM community_members")
        .await
        .is_ok());
}

#[tokio::test]
async fn membership_loader_returns_only_this_tenants_active_identified_members() {
    let f = fixture().await;
    let departed = Uuid::new_v4();
    let t2_member = Uuid::new_v4();
    exec(
        &f.su,
        &format!(
            "INSERT INTO community_members (community_id, user_uuid, removed_at) VALUES (10, '{departed}', NOW());
             INSERT INTO community_members (community_id, user_uuid) VALUES (20, '{t2_member}');
             INSERT INTO community_members (community_id) VALUES (10);"
        ),
    )
    .await;
    let rows = load_membership(&f.rep_conn, Some(TENANT)).await.unwrap();
    assert_eq!(rows.len(), 1, "{rows:?}");
    assert_eq!(rows[0].user, f.member);
    assert_eq!(
        (rows[0].tenant_id, rows[0].community_id),
        (TENANT, COMMUNITY)
    );

    // `None` = every tenant: includes the other tenant's identified member.
    let all = load_membership(&f.rep_conn, None).await.unwrap();
    assert_eq!(all.len(), 2, "{all:?}");

    // Feed the gate's production MembershipCheck end to end.
    let snapshot = SnapshotMembership::new();
    assert!(snapshot.replace_all(rows));
    let scope = HostInvokeScopeBuilder::new()
        .tenant_id(TENANT)
        .community_id(COMMUNITY)
        .app_id(APP_ID)
        .app_version(1)
        .tenant_tier(TenantTier::Free)
        .build()
        .unwrap();
    assert!(snapshot.is_member(&scope, f.member, ScopeKind::Community));
    assert!(!snapshot.is_member(&scope, departed, ScopeKind::Community));
    assert!(!snapshot.is_member(&scope, t2_member, ScopeKind::Community));
}
