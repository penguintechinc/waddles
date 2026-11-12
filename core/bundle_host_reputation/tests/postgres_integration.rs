//! Integration tests against a **real** Postgres container: the store's SQL
//! (row lock, rolling-24h cap, ledger atomicity, live membership predicate)
//! and the `waddles_bundle_reputation` role's privilege boundary can only be
//! proven by a real server. The schema under test is the EXACT shipped DDL --
//! `scripts/db/bundle_reputation_store.sql`, which alembic 0050 also executes
//! -- applied on top of hand-written minimal prerequisites (`tenants`,
//! `communities`, `community_members`, `app_catalog` and a copy of 0041's
//! `bundle_reputation_adjustments`; the full legacy baseline needs the whole
//! docker stack, see `alembic/tests/pg_docker.py`).
//!
//! Requires Docker (the `docker` CLI, `core/test_support`). Run with
//! `cargo test -p bundle-host-reputation --test postgres_integration`.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;
use std::time::Duration;

use bundle_capability_gate::{
    HostInvokeScopeBuilder, MembershipCheck, ScopeKind, SnapshotMembership, TenantTier,
};
use bundle_host_reputation::{
    connect, load_membership, ApplyOutcome, ConnectConfig, IdempotencyKey, PostgresReputationStore,
    ReasonClass, ReputationCaps, ReputationError, ReputationScope, ReputationStore, ScoreEntry,
    SoftBanLimits, TenantPolicy, TenantScore, MAX_BATCH, MAX_LEADERBOARD_LIMIT, MAX_SCORE,
    MIN_SCORE,
};
use sea_orm::{ConnectionTrait, Database, DatabaseConnection, Statement};
use uuid::Uuid;
#[allow(dead_code)]
#[path = "../../test_support/tests/docker_container.rs"]
mod docker_container;
use docker_container::{start_container_blocking, DockerContainer};

const POSTGRES_IMAGE: &str = "postgres:17.6-bookworm";
const SUPERUSER_PASSWORD: &str = "postgres_test_superuser_pw";
const REP_PASSWORD: &str = "waddles_bundle_reputation_test_pw";
const APP_ID: &str = "waddles.core.test-reputation";
const OTHER_APP_ID: &str = "waddles.core.other-reputation";
const SHIPPED_DDL: &str = include_str!("../../../scripts/db/bundle_reputation_store.sql");
const HOST_OPS_DDL: &str = include_str!("../../../scripts/db/bundle_reputation_host_ops.sql");

const TENANT: i32 = 1;
const COMMUNITY: i32 = 10;
const OTHER_TENANT: i32 = 2;
const OTHER_COMMUNITY: i32 = 20;

struct Fixture {
    _container: DockerContainer,
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

/// Attempts the pull-owning (first) container start gets before the fixture
/// gives up -- a registry stream can be cut mid-pull on a cold CI runner.
const PULL_ATTEMPTS: u32 = 3;

/// Process-wide guard for the image pull; `true` once the image is local.
///
/// Every test owns its own container, so on a cold runner all 13 tests would
/// otherwise pull `postgres:17.6-bookworm` at the same instant -- concurrent
/// identical pulls intermittently die with `PullImage ... bytes remaining on
/// stream` (seen in CI on #773). The first test to take the lock owns the
/// pull; the rest queue behind it, then start in parallel off the cached image.
static IMAGE_PULL: tokio::sync::Mutex<bool> = tokio::sync::Mutex::const_new(false);

/// Starts one Postgres container with the readiness probe the suite relies
/// on (`pg_isready` over TCP, which only the real server answers -- never the
/// initdb-time throwaway server).
async fn start_once() -> Result<DockerContainer, String> {
    tokio::task::spawn_blocking(|| {
        start_container_blocking(
            POSTGRES_IMAGE,
            5432,
            &[
                ("POSTGRES_PASSWORD", SUPERUSER_PASSWORD),
                ("POSTGRES_DB", "waddles_test"),
            ],
            &["pg_isready", "-h", "127.0.0.1", "-U", "postgres"],
            Duration::from_secs(240),
        )
    })
    .await
    .expect("container start task joins")
}

/// Starts a Postgres container, serializing the first start (the one that
/// pulls the image if it is not cached) so concurrent tests never race the
/// same pull. Panics with the last error once the pull-owning start has
/// exhausted its attempts, or immediately for any later start.
async fn start_postgres() -> DockerContainer {
    let mut pulled = IMAGE_PULL.lock().await;
    if *pulled {
        drop(pulled);
        return start_once()
            .await
            .unwrap_or_else(|e| panic!("postgres test container starts: {e}"));
    }
    let mut last_err = None;
    for _ in 0..PULL_ATTEMPTS {
        match start_once().await {
            Ok(container) => {
                *pulled = true;
                drop(pulled);
                return container;
            }
            Err(e) => last_err = Some(e),
        }
    }
    panic!("postgres test container starts after {PULL_ATTEMPTS} attempts: {last_err:?}");
}

async fn fixture() -> Fixture {
    let container = start_postgres().await;
    let host = container.host().to_string();
    let port = container.port();
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
    // (same ordering alembic 0050 guarantees).
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
    // Host-ops DDL (alembic 0071) runs after it, also idempotent.
    exec(&su, HOST_OPS_DDL).await;
    exec(&su, HOST_OPS_DDL).await;

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

// ---------------------------------------------------------------------------
// P5 Task 1: score clamp + admin override
// ---------------------------------------------------------------------------

fn key(k: &str) -> IdempotencyKey {
    IdempotencyKey::new(k).unwrap()
}

async fn set_balance(f: &Fixture, user: Uuid, balance: i64) {
    exec(
        &f.su,
        &format!(
            "INSERT INTO bundle_reputation_scores (tenant_id, community_id, user_uuid, balance) \
             VALUES ({TENANT}, {COMMUNITY}, '{user}', {balance}) \
             ON CONFLICT (community_id, user_uuid) DO UPDATE SET balance = {balance}"
        ),
    )
    .await;
}

async fn scalar_text(conn: &DatabaseConnection, sql: &str) -> String {
    conn.query_one_raw(Statement::from_string(sea_orm::DbBackend::Postgres, sql))
        .await
        .unwrap()
        .unwrap()
        .try_get_by_index::<String>(0)
        .unwrap()
}

/// The DB CHECK is the backstop under the store's clamp: no writer, however
/// privileged, can persist an out-of-range score.
#[tokio::test]
async fn the_score_range_check_rejects_an_out_of_range_balance() {
    let f = fixture().await;
    for bad in [MAX_SCORE + 1, MIN_SCORE - 1] {
        let err = f
            .su
            .execute_unprepared(&format!(
                "INSERT INTO bundle_reputation_scores (tenant_id, community_id, user_uuid, balance) \
                 VALUES ({TENANT}, {COMMUNITY}, '{}', {bad})",
                f.member
            ))
            .await
            .unwrap_err()
            .to_string();
        assert!(
            err.contains("ck_bundle_reputation_scores_balance_range"),
            "{err}"
        );
    }
}

/// An adjust that would cross the bound is clamped, ledgered with the APPLIED
/// delta (reconciles with the balance) and the REQUESTED one (which is what
/// the caps meter), and a call already pinned at the bound still consumes quota.
#[tokio::test]
async fn adjust_clamps_at_the_bounds_and_the_ledger_reconciles() {
    let f = fixture().await;
    set_balance(&f, f.member, MAX_SCORE - 2).await;
    assert_eq!(
        f.store
            .adjust(&scope(), f.member, 5, "win", caps(100))
            .await,
        Ok(MAX_SCORE)
    );
    assert_eq!(
        scalar_i64(
            &f.su,
            "SELECT delta::BIGINT FROM bundle_reputation_adjustments ORDER BY id DESC LIMIT 1"
        )
        .await,
        2
    );
    assert_eq!(
        scalar_i64(
            &f.su,
            "SELECT requested_value FROM bundle_reputation_adjustments ORDER BY id DESC LIMIT 1"
        )
        .await,
        5
    );
    // Pinned at the bound: no movement, but the call is ledgered and metered.
    assert_eq!(
        f.store
            .adjust(&scope(), f.member, 5, "win", caps(100))
            .await,
        Ok(MAX_SCORE)
    );
    assert_eq!(ledger_count(&f).await, 2);
    // ...so a cap of 10 is now exhausted (5 + 5), not unbounded.
    assert_eq!(
        f.store.adjust(&scope(), f.member, 1, "win", caps(10)).await,
        Err(ReputationError::DailyCapExceeded { cap: 10 })
    );
    set_balance(&f, f.member, MIN_SCORE + 1).await;
    assert_eq!(
        f.store
            .adjust(&scope(), f.member, -5, "loss", caps(100))
            .await,
        Ok(MIN_SCORE)
    );
}

#[tokio::test]
async fn override_sets_an_absolute_score_and_ledgers_admin_override() {
    let f = fixture().await;
    f.store
        .adjust(&scope(), f.member, 3, "win", caps(100))
        .await
        .unwrap();
    assert_eq!(
        f.store
            .override_score(&scope(), f.member, 250, "admin.fix", &key("e1:override:0"))
            .await,
        Ok(250)
    );
    assert_eq!(f.store.get(&scope(), f.member).await, Ok(250));
    let sql = "SELECT kind FROM bundle_reputation_adjustments ORDER BY id DESC LIMIT 1";
    assert_eq!(scalar_text(&f.su, sql).await, "admin_override");
    assert_eq!(
        scalar_i64(
            &f.su,
            "SELECT delta::BIGINT FROM bundle_reputation_adjustments ORDER BY id DESC LIMIT 1"
        )
        .await,
        247
    );
    assert_eq!(
        scalar_i64(
            &f.su,
            "SELECT balance_after FROM bundle_reputation_adjustments ORDER BY id DESC LIMIT 1"
        )
        .await,
        250
    );
    // Negative absolute scores are legal.
    assert_eq!(
        f.store
            .override_score(&scope(), f.member, -40, "admin.fix", &key("e1:override:1"))
            .await,
        Ok(-40)
    );
}

#[tokio::test]
async fn override_clamps_an_absurd_absolute_score() {
    let f = fixture().await;
    assert_eq!(
        f.store
            .override_score(&scope(), f.member, i64::MAX, "admin.fix", &key("k1"))
            .await,
        Ok(MAX_SCORE)
    );
    assert_eq!(
        f.store
            .override_score(&scope(), f.member, i64::MIN, "admin.fix", &key("k2"))
            .await,
        Ok(MIN_SCORE)
    );
    assert_eq!(f.store.get(&scope(), f.member).await, Ok(MIN_SCORE));
}

/// An override is an admin action, not a capped delta: it neither trips nor
/// consumes the rolling-24h adjust caps.
#[tokio::test]
async fn override_is_outside_the_daily_caps_and_does_not_consume_them() {
    let f = fixture().await;
    f.store
        .override_score(&scope(), f.member, 900, "admin.fix", &key("k1"))
        .await
        .unwrap();
    // A 900-point override left the user's whole adjust budget intact.
    assert_eq!(
        f.store
            .adjust(&scope(), f.member, 5, "win", caps2(5, 5))
            .await,
        Ok(905)
    );
    // ...and an override still works with the budget fully spent.
    assert_eq!(
        f.store
            .override_score(&scope(), f.member, 1, "admin.fix", &key("k2"))
            .await,
        Ok(1)
    );
}

#[tokio::test]
async fn override_rejects_non_members_and_bad_reasons_writing_nothing() {
    let f = fixture().await;
    let stranger = Uuid::new_v4();
    assert_eq!(
        f.store
            .override_score(&scope(), stranger, 5, "admin.fix", &key("k1"))
            .await,
        Err(ReputationError::NotAMember)
    );
    let other_comm_user = add_member(&f, OTHER_COMMUNITY).await;
    assert_eq!(
        f.store
            .override_score(&scope(), other_comm_user, 5, "admin.fix", &key("k2"))
            .await,
        Err(ReputationError::NotAMember)
    );
    assert!(matches!(
        f.store
            .override_score(&scope(), f.member, 5, "Bad Reason", &key("k3"))
            .await,
        Err(ReputationError::Invalid(_))
    ));
    assert_eq!(ledger_count(&f).await, 0);
}

#[tokio::test]
async fn override_is_idempotent_on_its_key_and_conflicts_on_changed_parameters() {
    let f = fixture().await;
    let k = key("evt-9:override:0");
    assert_eq!(
        f.store
            .override_score(&scope(), f.member, 77, "admin.fix", &k)
            .await,
        Ok(77)
    );
    // Something else moves the score; the replay must NOT re-apply.
    f.store
        .adjust(&scope(), f.member, 2, "win", caps(100))
        .await
        .unwrap();
    assert_eq!(
        f.store
            .override_score(&scope(), f.member, 77, "admin.fix", &k)
            .await,
        Ok(77),
        "a replay answers with the original result"
    );
    assert_eq!(
        f.store.get(&scope(), f.member).await,
        Ok(79),
        "and moves nothing"
    );
    assert_eq!(ledger_count(&f).await, 2);
    assert_eq!(
        f.store
            .override_score(&scope(), f.member, 78, "admin.fix", &k)
            .await,
        Err(ReputationError::IdempotencyConflict)
    );
    // Same key, different user -> conflict as well.
    let other = add_member(&f, COMMUNITY).await;
    assert_eq!(
        f.store
            .override_score(&scope(), other, 77, "admin.fix", &k)
            .await,
        Err(ReputationError::IdempotencyConflict)
    );
    // Another app's identical key is a different scope: independent.
    let other_app = ReputationScope {
        app_id: OTHER_APP_ID.to_string(),
        ..scope()
    };
    assert_eq!(
        f.store
            .override_score(&other_app, f.member, 5, "admin.fix", &k)
            .await,
        Ok(5)
    );
}

#[tokio::test]
async fn override_is_atomic_a_failing_ledger_insert_rolls_back_the_score() {
    let f = fixture().await;
    let bad = ReputationScope {
        app_id: "waddles.unknown.not-in-catalog".to_string(),
        ..scope()
    };
    let err = f
        .store
        .override_score(&bad, f.member, 9, "admin.fix", &key("k"))
        .await
        .unwrap_err();
    assert!(matches!(err, ReputationError::Backend(_)), "{err:?}");
    assert_eq!(f.store.get(&scope(), f.member).await, Ok(0));
    assert_eq!(
        scalar_i64(&f.su, "SELECT COUNT(*) FROM bundle_reputation_scores").await,
        0
    );
}

// ---------------------------------------------------------------------------
// P5 Task 1: adjust_many
// ---------------------------------------------------------------------------

async fn score_of(f: &Fixture, user: Uuid) -> i64 {
    f.store.get(&scope(), user).await.unwrap()
}

#[tokio::test]
async fn adjust_many_applies_every_item_in_one_transaction_with_a_ledger_row_each() {
    let f = fixture().await;
    let b = add_member(&f, COMMUNITY).await;
    let c = add_member(&f, COMMUNITY).await;
    let items = [(f.member, 3), (b, -2), (c, 1)];
    f.store
        .adjust_many(
            &scope(),
            &items,
            "game.round",
            caps(10),
            &key("e1:adjust_many:0"),
        )
        .await
        .unwrap();
    assert_eq!(score_of(&f, f.member).await, 3);
    assert_eq!(score_of(&f, b).await, -2);
    assert_eq!(score_of(&f, c).await, 1);
    assert_eq!(ledger_count(&f).await, 3);
    assert_eq!(
        scalar_i64(&f.su, "SELECT COUNT(*) FROM bundle_reputation_adjustments WHERE kind = 'adjust' AND idempotency_key = 'e1:adjust_many:0'").await,
        3
    );
}

/// All-or-nothing: the second item breaches ITS per-user cap, so the first
/// (which fit) must not land either.
#[tokio::test]
async fn adjust_many_is_atomic_when_any_item_breaches_the_per_user_cap() {
    let f = fixture().await;
    let b = add_member(&f, COMMUNITY).await;
    f.store
        .adjust(&scope(), b, 4, "win", caps(100))
        .await
        .unwrap(); // b has 4/5 used
    let before = ledger_count(&f).await;
    let err = f
        .store
        .adjust_many(&scope(), &[(f.member, 1), (b, 3)], "r", caps(5), &key("k1"))
        .await
        .unwrap_err();
    assert_eq!(err, ReputationError::DailyCapExceeded { cap: 5 });
    assert_eq!(
        score_of(&f, f.member).await,
        0,
        "the in-cap item rolled back too"
    );
    assert_eq!(score_of(&f, b).await, 4);
    assert_eq!(ledger_count(&f).await, before);
}

/// The batch's TOTAL counts against the per-scope cap (not each item alone).
#[tokio::test]
async fn adjust_many_counts_the_batch_total_against_the_scope_cap() {
    let f = fixture().await;
    let b = add_member(&f, COMMUNITY).await;
    let c = add_member(&f, COMMUNITY).await;
    let items = [(f.member, 4), (b, 4), (c, 4)]; // 12 total, each under the user cap
    assert_eq!(
        f.store
            .adjust_many(&scope(), &items, "r", caps2(10, 11), &key("k1"))
            .await,
        Err(ReputationError::ScopeQuotaExceeded { cap: 11 })
    );
    assert_eq!(ledger_count(&f).await, 0);
    f.store
        .adjust_many(&scope(), &items, "r", caps2(10, 12), &key("k2"))
        .await
        .unwrap();
    // The scope window now holds 12: even one more point is refused.
    assert_eq!(
        f.store
            .adjust(&scope(), f.member, 1, "r", caps2(100, 12))
            .await,
        Err(ReputationError::ScopeQuotaExceeded { cap: 12 })
    );
}

/// The 24h per-user cap still binds across the batch AND single adjusts, and a
/// prior admin override neither trips nor feeds it.
#[tokio::test]
async fn adjust_many_shares_the_rolling_24h_window_with_adjust_and_ignores_overrides() {
    let f = fixture().await;
    f.store
        .override_score(&scope(), f.member, 500, "admin.fix", &key("o"))
        .await
        .unwrap();
    f.store
        .adjust_many(&scope(), &[(f.member, 5)], "r", caps(5), &key("k1"))
        .await
        .unwrap();
    assert_eq!(
        f.store.adjust(&scope(), f.member, 1, "r", caps(5)).await,
        Err(ReputationError::DailyCapExceeded { cap: 5 })
    );
    assert_eq!(
        f.store
            .adjust_many(&scope(), &[(f.member, 1)], "r", caps(5), &key("k2"))
            .await,
        Err(ReputationError::DailyCapExceeded { cap: 5 })
    );
}

#[tokio::test]
async fn adjust_many_rejects_non_members_dupes_zero_empty_and_oversize_writing_nothing() {
    let f = fixture().await;
    let b = add_member(&f, COMMUNITY).await;
    let foreign = add_member(&f, OTHER_COMMUNITY).await;
    assert_eq!(
        f.store
            .adjust_many(
                &scope(),
                &[(f.member, 1), (foreign, 1)],
                "r",
                caps(9),
                &key("k1")
            )
            .await,
        Err(ReputationError::NotAMember),
        "one cross-community user fails the whole batch"
    );
    assert_eq!(
        f.store
            .adjust_many(
                &scope(),
                &[(f.member, 1), (Uuid::new_v4(), 1)],
                "r",
                caps(9),
                &key("k2")
            )
            .await,
        Err(ReputationError::NotAMember)
    );
    for bad in [
        vec![(f.member, 1), (f.member, 2)],
        vec![(f.member, 0)],
        vec![],
        (0..=MAX_BATCH).map(|_| (b, 1)).collect(),
    ] {
        assert!(
            matches!(
                f.store
                    .adjust_many(&scope(), &bad, "r", caps(9), &key("k3"))
                    .await,
                Err(ReputationError::Invalid(_))
            ),
            "{bad:?}"
        );
    }
    assert!(matches!(
        f.store
            .adjust_many(&scope(), &[(b, 1)], "Bad Reason", caps(9), &key("k4"))
            .await,
        Err(ReputationError::Invalid(_))
    ));
    assert_eq!(ledger_count(&f).await, 0);
    assert_eq!(
        scalar_i64(&f.su, "SELECT COUNT(*) FROM bundle_reputation_scores").await,
        0
    );
}

#[tokio::test]
async fn adjust_many_is_idempotent_on_its_key_and_conflicts_on_a_changed_batch() {
    let f = fixture().await;
    let b = add_member(&f, COMMUNITY).await;
    let items = [(f.member, 2), (b, 3)];
    let k = key("evt-3:adjust_many:0");
    f.store
        .adjust_many(&scope(), &items, "r", caps(10), &k)
        .await
        .unwrap();
    // Replay (any order of the same items): a no-op success.
    f.store
        .adjust_many(&scope(), &[(b, 3), (f.member, 2)], "r", caps(10), &k)
        .await
        .unwrap();
    assert_eq!(score_of(&f, f.member).await, 2);
    assert_eq!(score_of(&f, b).await, 3);
    assert_eq!(ledger_count(&f).await, 2);
    for changed in [
        vec![(f.member, 2), (b, 4)],
        vec![(f.member, 2)],
        vec![(f.member, 2), (b, 3), (add_member(&f, COMMUNITY).await, 1)],
    ] {
        assert_eq!(
            f.store
                .adjust_many(&scope(), &changed, "r", caps(10), &k)
                .await,
            Err(ReputationError::IdempotencyConflict),
            "{changed:?}"
        );
    }
    assert_eq!(ledger_count(&f).await, 2);
}

#[tokio::test]
async fn adjust_many_clamps_per_item_at_the_bounds() {
    let f = fixture().await;
    let b = add_member(&f, COMMUNITY).await;
    set_balance(&f, f.member, MAX_SCORE - 1).await;
    set_balance(&f, b, MIN_SCORE + 1).await;
    f.store
        .adjust_many(
            &scope(),
            &[(f.member, 5), (b, -5)],
            "r",
            caps(10),
            &key("k"),
        )
        .await
        .unwrap();
    assert_eq!(score_of(&f, f.member).await, MAX_SCORE);
    assert_eq!(score_of(&f, b).await, MIN_SCORE);
}

#[tokio::test]
async fn adjust_many_is_atomic_a_failing_ledger_insert_rolls_back_every_score() {
    let f = fixture().await;
    let b = add_member(&f, COMMUNITY).await;
    let bad = ReputationScope {
        app_id: "waddles.unknown.not-in-catalog".to_string(),
        ..scope()
    };
    let err = f
        .store
        .adjust_many(&bad, &[(f.member, 1), (b, 1)], "r", caps(9), &key("k"))
        .await
        .unwrap_err();
    assert!(matches!(err, ReputationError::Backend(_)), "{err:?}");
    assert_eq!(
        scalar_i64(&f.su, "SELECT COUNT(*) FROM bundle_reputation_scores").await,
        0
    );
}

/// Overlapping concurrent batches across two replicas: no deadlock (uniform
/// lock order), and the per-user cap is never exceeded.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn concurrent_overlapping_adjust_many_never_deadlock_or_exceed_the_cap() {
    let f = fixture().await;
    let b = add_member(&f, COMMUNITY).await;
    let s1 = Arc::new(fresh_replica(&f).await);
    let s2 = Arc::new(fresh_replica(&f).await);
    let mut tasks = Vec::new();
    for i in 0..16 {
        let store = if i % 2 == 0 {
            Arc::clone(&s1)
        } else {
            Arc::clone(&s2)
        };
        let (m, other) = (f.member, b);
        tasks.push(tokio::spawn(async move {
            // Opposite item order per task: a naive lock order would deadlock.
            let items = if i % 3 == 0 {
                [(m, 1), (other, 1)]
            } else {
                [(other, 1), (m, 1)]
            };
            let k = IdempotencyKey::new(format!("race:{i}")).unwrap();
            store
                .adjust_many(&scope(), &items, "race", caps(6), &k)
                .await
        }));
    }
    let (mut ok, mut capped) = (0, 0);
    for t in tasks {
        match t.await.unwrap() {
            Ok(()) => ok += 1,
            Err(ReputationError::DailyCapExceeded { .. }) => capped += 1,
            Err(other) => panic!("unexpected: {other:?}"),
        }
    }
    assert_eq!(ok, 6);
    assert_eq!(capped, 10);
    assert_eq!(score_of(&f, f.member).await, 6);
    assert_eq!(score_of(&f, b).await, 6);
}

// ---------------------------------------------------------------------------
// P5 Task 1: get_many + leaderboard
// ---------------------------------------------------------------------------

#[tokio::test]
async fn get_many_returns_request_order_dedupes_and_reports_zero_for_unscored_members() {
    let f = fixture().await;
    let b = add_member(&f, COMMUNITY).await;
    let c = add_member(&f, COMMUNITY).await;
    f.store
        .adjust(&scope(), f.member, 4, "r", caps(10))
        .await
        .unwrap();
    f.store
        .adjust(&scope(), b, -3, "r", caps(10))
        .await
        .unwrap();
    let got = f
        .store
        .get_many(&scope(), &[b, c, f.member, b])
        .await
        .unwrap();
    assert_eq!(
        got,
        vec![
            ScoreEntry { user: b, score: -3 },
            ScoreEntry { user: c, score: 0 },
            ScoreEntry {
                user: f.member,
                score: 4
            },
        ]
    );
}

#[tokio::test]
async fn get_many_is_bounded_and_fail_closed_on_non_members_and_other_communities() {
    let f = fixture().await;
    let foreign = add_member(&f, OTHER_COMMUNITY).await;
    assert_eq!(
        f.store.get_many(&scope(), &[f.member, foreign]).await,
        Err(ReputationError::NotAMember),
        "community isolation: a user of another community is not readable here"
    );
    assert_eq!(
        f.store
            .get_many(&scope(), &[f.member, Uuid::new_v4()])
            .await,
        Err(ReputationError::NotAMember)
    );
    assert!(matches!(
        f.store.get_many(&scope(), &[]).await,
        Err(ReputationError::Invalid(_))
    ));
    let oversize: Vec<Uuid> = (0..=MAX_BATCH).map(|_| Uuid::new_v4()).collect();
    assert!(matches!(
        f.store.get_many(&scope(), &oversize).await,
        Err(ReputationError::Invalid(_))
    ));
    // Exactly MAX_BATCH distinct members is accepted (one round-trip).
    let mut many = Vec::new();
    for _ in 0..MAX_BATCH {
        many.push(add_member(&f, COMMUNITY).await);
    }
    assert_eq!(
        f.store.get_many(&scope(), &many).await.unwrap().len(),
        MAX_BATCH
    );
}

#[tokio::test]
async fn get_many_treats_a_departed_member_as_a_non_member() {
    let f = fixture().await;
    let b = add_member(&f, COMMUNITY).await;
    exec(
        &f.su,
        &format!("UPDATE community_members SET left_at = NOW() WHERE user_uuid = '{b}'"),
    )
    .await;
    assert_eq!(
        f.store.get_many(&scope(), &[f.member, b]).await,
        Err(ReputationError::NotAMember)
    );
}

#[tokio::test]
async fn leaderboard_is_ordered_limited_community_isolated_and_active_only() {
    let f = fixture().await;
    let mut users = vec![f.member];
    for _ in 0..4 {
        users.push(add_member(&f, COMMUNITY).await);
    }
    for (i, u) in users.iter().enumerate() {
        // scores 10, 20, 30, 40, 50
        set_balance(&f, *u, 10 * (i as i64 + 1)).await;
    }
    // A foreign community's higher scorer must never appear.
    let foreign = add_member(&f, OTHER_COMMUNITY).await;
    exec(&f.su, &format!(
        "INSERT INTO bundle_reputation_scores (tenant_id, community_id, user_uuid, balance) VALUES ({OTHER_TENANT}, {OTHER_COMMUNITY}, '{foreign}', 999)")).await;
    // A departed member must not appear either.
    exec(
        &f.su,
        &format!(
            "UPDATE community_members SET left_at = NOW() WHERE user_uuid = '{}'",
            users[4]
        ),
    )
    .await;

    let top = f.store.leaderboard(&scope(), 100).await.unwrap();
    let scores: Vec<i64> = top.iter().map(|e| e.score).collect();
    assert_eq!(scores, vec![40, 30, 20, 10]);
    assert!(top.iter().all(|e| e.user != foreign && e.user != users[4]));

    let two = f.store.leaderboard(&scope(), 2).await.unwrap();
    assert_eq!(
        two.iter().map(|e| e.score).collect::<Vec<_>>(),
        vec![40, 30]
    );
}

#[tokio::test]
async fn leaderboard_breaks_ties_by_user_uuid_and_rejects_bad_limits() {
    let f = fixture().await;
    let b = add_member(&f, COMMUNITY).await;
    set_balance(&f, f.member, 7).await;
    set_balance(&f, b, 7).await;
    let top = f.store.leaderboard(&scope(), 10).await.unwrap();
    let mut expect = [f.member, b];
    expect.sort();
    assert_eq!(top.iter().map(|e| e.user).collect::<Vec<_>>(), expect);
    for bad in [0, MAX_LEADERBOARD_LIMIT + 1] {
        assert!(matches!(
            f.store.leaderboard(&scope(), bad).await,
            Err(ReputationError::Invalid(_))
        ));
    }
}

// ---------------------------------------------------------------------------
// Review fixes: lock order, legacy rows, reason in idempotency
// ---------------------------------------------------------------------------

/// Two apps batching the SAME absent users in OPPOSITE order must both
/// complete (rows are inserted/locked in one sorted order).
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn opposite_order_batches_over_absent_users_do_not_deadlock() {
    let f = fixture().await;
    let mut users = Vec::new();
    for _ in 0..8 {
        users.push(add_member(&f, COMMUNITY).await);
    }
    let s1 = Arc::new(fresh_replica(&f).await);
    let s2 = Arc::new(fresh_replica(&f).await);
    for round in 0..5 {
        // Fresh absent users each round.
        let mut round_users = Vec::new();
        for _ in 0..8 {
            round_users.push(add_member(&f, COMMUNITY).await);
        }
        let fwd: Vec<(Uuid, i32)> = round_users.iter().map(|u| (*u, 1)).collect();
        let mut rev = fwd.clone();
        rev.reverse();
        let other_app = ReputationScope {
            app_id: OTHER_APP_ID.to_string(),
            ..scope()
        };
        let (a, b) = (Arc::clone(&s1), Arc::clone(&s2));
        let t1 = tokio::spawn(async move {
            a.adjust_many(&scope(), &fwd, "r", caps(5), &key(&format!("f{round}")))
                .await
        });
        let t2 = tokio::spawn(async move {
            b.adjust_many(&other_app, &rev, "r", caps(5), &key(&format!("r{round}")))
                .await
        });
        let done = tokio::time::timeout(std::time::Duration::from_secs(20), async {
            (t1.await.unwrap(), t2.await.unwrap())
        })
        .await
        .expect("no deadlock");
        assert_eq!(done.0, Ok(()));
        assert_eq!(done.1, Ok(()));
    }
    let _ = users;
}

/// A legacy out-of-range row is CLAMPED by the migration and the constraint
/// validated -- never silently dropped by a later adjust.
#[tokio::test]
async fn migration_clamps_legacy_out_of_range_rows_and_validates_the_check() {
    let f = fixture().await;
    exec(
        &f.su,
        "ALTER TABLE bundle_reputation_scores DROP CONSTRAINT ck_bundle_reputation_scores_balance_range",
    )
    .await;
    set_balance(&f, f.member, 1_500_000).await;
    let low = add_member(&f, COMMUNITY).await;
    set_balance(&f, low, -2_000_000).await;
    exec(&f.su, HOST_OPS_DDL).await;
    assert_eq!(f.store.get(&scope(), f.member).await, Ok(MAX_SCORE));
    assert_eq!(f.store.get(&scope(), low).await, Ok(MIN_SCORE));
    assert_eq!(
        scalar_text(
            &f.su,
            "SELECT convalidated::text FROM pg_constraint WHERE conname = 'ck_bundle_reputation_scores_balance_range'"
        )
        .await,
        "true"
    );
    // The adjust now moves from the corrected value, not from 1.5M.
    assert_eq!(
        f.store.adjust(&scope(), f.member, -5, "r", caps(10)).await,
        Ok(MAX_SCORE - 5)
    );
}

/// The reason is part of the replayed operation's identity.
#[tokio::test]
async fn a_reused_key_with_a_changed_reason_is_an_idempotency_conflict() {
    let f = fixture().await;
    let k = key("evt:override:0");
    f.store
        .override_score(&scope(), f.member, 5, "admin.a", &k)
        .await
        .unwrap();
    assert_eq!(
        f.store
            .override_score(&scope(), f.member, 5, "admin.a", &k)
            .await,
        Ok(5)
    );
    assert_eq!(
        f.store
            .override_score(&scope(), f.member, 5, "admin.b", &k)
            .await,
        Err(ReputationError::IdempotencyConflict)
    );
    let k2 = key("evt:adjust_many:0");
    f.store
        .adjust_many(&scope(), &[(f.member, 1)], "r.a", caps(10), &k2)
        .await
        .unwrap();
    f.store
        .adjust_many(&scope(), &[(f.member, 1)], "r.a", caps(10), &k2)
        .await
        .unwrap();
    assert_eq!(
        f.store
            .adjust_many(&scope(), &[(f.member, 1)], "r.b", caps(10), &k2)
            .await,
        Err(ReputationError::IdempotencyConflict)
    );
}

/// regression: #983 review -- the leaderboard filter + sort must be served by an
/// index, not a sequential scan of `bundle_reputation_scores`. Seeds thousands
/// of rows across several communities (so the planner makes a real choice),
/// ANALYZEs, then EXPLAINs the store's exact leaderboard query.
#[tokio::test]
async fn leaderboard_query_uses_an_index_not_a_seq_scan_on_the_scores_table() {
    let f = fixture().await;
    exec(
        &f.su,
        "INSERT INTO communities (id, tenant_id) SELECT g, 1 FROM generate_series(30, 39) g;
         INSERT INTO community_members (community_id, user_uuid)
             SELECT c, gen_random_uuid() FROM unnest(ARRAY[10, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39]) c,
             generate_series(1, 1000) n;
         INSERT INTO bundle_reputation_scores (tenant_id, community_id, user_uuid, balance)
             SELECT 1, cm.community_id, cm.user_uuid, (random() * 2000 - 1000)::BIGINT
             FROM community_members cm WHERE cm.user_uuid IS NOT NULL
             ON CONFLICT DO NOTHING;
         ANALYZE bundle_reputation_scores; ANALYZE community_members; ANALYZE communities;",
    )
    .await;
    let rows =
        f.su.query_all_raw(Statement::from_string(
            sea_orm::DbBackend::Postgres,
            "EXPLAIN SELECT s.user_uuid AS user_uuid, s.balance AS balance \
             FROM bundle_reputation_scores s \
             JOIN community_members cm \
               ON cm.community_id = s.community_id AND cm.user_uuid = s.user_uuid \
             JOIN communities c ON c.id = cm.community_id AND c.tenant_id = s.tenant_id \
             WHERE s.tenant_id = 1 AND s.community_id = 10 \
               AND cm.is_active IS TRUE AND cm.removed_at IS NULL AND cm.left_at IS NULL \
             ORDER BY s.balance DESC, s.user_uuid ASC \
             LIMIT 10"
                .to_string(),
        ))
        .await
        .unwrap();
    let plan: Vec<String> = rows
        .iter()
        .map(|r| r.try_get_by_index::<String>(0).unwrap())
        .collect();
    let plan = plan.join("\n");
    eprintln!("{plan}");
    assert!(
        !plan.contains("Seq Scan on bundle_reputation_scores"),
        "leaderboard seq-scans the scores table:\n{plan}"
    );
}

/// Stand-in for the hub-api-owned `reputation_apply()` (created by the
/// hub-api alembic migration, `feature/reputation-hub-api`): same signature
/// and result shape, minimal body (upsert row at 600, clamp to 300..=850,
/// ledger row, idempotent on the key). The REAL function replaces it in
/// deployment; this only lets the store's call contract be exercised.
const REPUTATION_APPLY_STUB: &str = r#"
CREATE TABLE rep_test_sleep (seconds float NOT NULL);
ALTER TABLE bundle_reputation_adjustments DROP CONSTRAINT ck_bundle_reputation_adjustments_kind;
ALTER TABLE bundle_reputation_adjustments ADD CONSTRAINT ck_bundle_reputation_adjustments_kind
  CHECK (kind IN ('adjust', 'admin_override', 'apply'));
CREATE OR REPLACE FUNCTION reputation_apply(
    p_tenant bigint, p_community bigint, p_user_uuid uuid, p_delta int,
    p_reason_code smallint, p_actor text, p_source_event text, p_idempotency_key text)
RETURNS TABLE (new_score int, applied bool) LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE v_old int; v_new int;
BEGIN
  PERFORM 1 FROM community_members cm JOIN communities c ON c.id = cm.community_id
    WHERE cm.community_id = p_community AND c.tenant_id = p_tenant AND cm.user_uuid = p_user_uuid
      AND cm.is_active IS TRUE AND cm.removed_at IS NULL AND cm.left_at IS NULL FOR SHARE OF cm;
  IF NOT FOUND THEN RAISE EXCEPTION 'not_a_member'; END IF;
  PERFORM pg_sleep(COALESCE((SELECT seconds FROM rep_test_sleep LIMIT 1), 0));
  IF EXISTS (SELECT 1 FROM bundle_reputation_adjustments
             WHERE idempotency_key = p_idempotency_key AND community_id = p_community) THEN
    RETURN QUERY SELECT s.balance::int, false FROM bundle_reputation_scores s
      WHERE s.community_id = p_community AND s.user_uuid = p_user_uuid;
    RETURN;
  END IF;
  INSERT INTO bundle_reputation_scores (tenant_id, community_id, user_uuid, balance)
    VALUES (p_tenant, p_community, p_user_uuid, 600) ON CONFLICT DO NOTHING;
  SELECT balance INTO v_old FROM bundle_reputation_scores
    WHERE community_id = p_community AND user_uuid = p_user_uuid FOR UPDATE;
  v_new := LEAST(850, GREATEST(300, v_old + p_delta));
  UPDATE bundle_reputation_scores SET balance = v_new
    WHERE community_id = p_community AND user_uuid = p_user_uuid;
  INSERT INTO bundle_reputation_adjustments
    (app_id, tenant_id, community_id, target_user_uuid, scope, delta, reason_code, kind,
     requested_value, balance_after, idempotency_key)
    VALUES (p_actor, p_tenant, p_community, p_user_uuid::text, 'community', v_new - v_old,
            (CASE p_reason_code WHEN 1 THEN 'activity' ELSE 'moderation' END) || ':' || p_source_event, 'apply', p_delta, v_new, p_idempotency_key);
  RETURN QUERY SELECT v_new, true;
END $$;
GRANT EXECUTE ON FUNCTION reputation_apply(bigint,bigint,uuid,int,smallint,text,text,text)
  TO waddles_bundle_reputation;
CREATE TABLE reputation_tenant_policy (
  tenant_id integer PRIMARY KEY, tenant_auto_ban_enabled boolean NOT NULL DEFAULT false,
  tenant_auto_ban_floor integer NOT NULL, tenant_at_risk_threshold integer NOT NULL);
GRANT SELECT ON reputation_tenant_policy TO waddles_bundle_reputation;
CREATE TABLE reputation_soft_bans (
  id BIGSERIAL PRIMARY KEY, tenant_id integer NOT NULL, community_id integer NOT NULL,
  user_uuid uuid NOT NULL, reason_code text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT NOW(), lifted_at timestamptz);
GRANT SELECT, INSERT, UPDATE ON reputation_soft_bans TO waddles_bundle_reputation;
GRANT USAGE ON SEQUENCE reputation_soft_bans_id_seq TO waddles_bundle_reputation;
"#;

fn ekey(n: &str) -> IdempotencyKey {
    IdempotencyKey::for_event(n, "reputation_apply", 0).unwrap()
}

/// `apply` with the event id as both source event and key seed.
async fn ap(
    f: &Fixture,
    scope: &ReputationScope,
    user: Uuid,
    delta: i32,
    class: ReasonClass,
    ev: &str,
    caps: ReputationCaps,
) -> Result<ApplyOutcome, ReputationError> {
    f.store
        .apply(scope, user, delta, class, ev, caps, &ekey(ev))
        .await
}

#[tokio::test]
async fn apply_goes_through_reputation_apply_is_idempotent_clamped_and_class_bounded() {
    let f = fixture().await;
    exec(&f.su, REPUTATION_APPLY_STUB).await;
    let first = ap(
        &f,
        &scope(),
        f.member,
        5,
        ReasonClass::Activity,
        "evt-1",
        acaps(),
    )
    .await
    .unwrap();
    assert_eq!(
        first,
        ApplyOutcome {
            new_score: 605,
            applied: true
        }
    );
    // Same event id replayed: nothing moves.
    let replay = ap(
        &f,
        &scope(),
        f.member,
        5,
        ReasonClass::Activity,
        "evt-1",
        acaps(),
    )
    .await
    .unwrap();
    assert_eq!(
        replay,
        ApplyOutcome {
            new_score: 605,
            applied: false
        }
    );
    // Moderation bound (-200) is allowed, the activity bound (5) is not.
    assert!(matches!(
        ap(
            &f,
            &scope(),
            f.member,
            -200,
            ReasonClass::Activity,
            "evt-2",
            acaps()
        )
        .await,
        Err(ReputationError::Invalid(_))
    ));
    let out = ap(
        &f,
        &scope(),
        f.member,
        -200,
        ReasonClass::Moderation,
        "evt-2",
        acaps(),
    )
    .await
    .unwrap();
    assert_eq!(out.new_score, 405);
    // Floor clamp at 300.
    let low = ap(
        &f,
        &scope(),
        f.member,
        -200,
        ReasonClass::Moderation,
        "evt-3",
        acaps(),
    )
    .await
    .unwrap();
    assert_eq!(low.new_score, 300);
    assert_eq!(ledger_count(&f).await, 3);
    assert_eq!(
        ap(
            &f,
            &scope(),
            Uuid::new_v4(),
            1,
            ReasonClass::Activity,
            "evt-4",
            acaps()
        )
        .await,
        Err(ReputationError::NotAMember)
    );
}

#[tokio::test]
async fn apply_enforces_per_user_and_per_scope_daily_caps_per_class() {
    let f = fixture().await;
    exec(&f.su, REPUTATION_APPLY_STUB).await;
    // Per-user cap 7: 5 + 2 fits, the next +1 does not.
    let c = caps2(7, 1_000);
    ap(&f, &scope(), f.member, 5, ReasonClass::Activity, "c1", c)
        .await
        .unwrap();
    ap(&f, &scope(), f.member, 2, ReasonClass::Activity, "c2", c)
        .await
        .unwrap();
    assert_eq!(
        ap(&f, &scope(), f.member, 1, ReasonClass::Activity, "c3", c).await,
        Err(ReputationError::DailyCapExceeded { cap: 7 })
    );
    // The activity window does not eat the moderation window.
    ap(&f, &scope(), f.member, -6, ReasonClass::Moderation, "c4", c)
        .await
        .unwrap();
    // A replay of an applied key is not re-counted against the cap.
    let replay = ap(&f, &scope(), f.member, 5, ReasonClass::Activity, "c1", c)
        .await
        .unwrap();
    assert!(!replay.applied);
    // Per-scope cap across users: 7 already used + 5 = 12, next +1 refused.
    let other = add_member(&f, COMMUNITY).await;
    let sc = caps2(100, 12);
    ap(&f, &scope(), other, 5, ReasonClass::Activity, "s1", sc)
        .await
        .unwrap();
    // c1+c2 (7) used by `member` + 5 = 12: the scope window is now full.
    assert_eq!(
        ap(&f, &scope(), other, 1, ReasonClass::Activity, "s2", sc).await,
        Err(ReputationError::ScopeQuotaExceeded { cap: 12 })
    );
    // Negative caps are rejected loudly.
    assert!(matches!(
        ap(
            &f,
            &scope(),
            other,
            1,
            ReasonClass::Activity,
            "s3",
            caps2(-1, 5)
        )
        .await,
        Err(ReputationError::Invalid(_))
    ));
}

#[tokio::test]
async fn apply_rejects_a_bad_source_event_and_writes_nothing() {
    let f = fixture().await;
    exec(&f.su, REPUTATION_APPLY_STUB).await;
    for bad in ["", "Has Upper", "has space", "semi;colon", &"a".repeat(101)] {
        let key = IdempotencyKey::new("kk-1").unwrap();
        assert!(
            matches!(
                f.store
                    .apply(
                        &scope(),
                        f.member,
                        1,
                        ReasonClass::Activity,
                        bad,
                        acaps(),
                        &key
                    )
                    .await,
                Err(ReputationError::Invalid(_))
            ),
            "{bad:?}"
        );
    }
    assert_eq!(ledger_count(&f).await, 0);
}

#[tokio::test]
async fn apply_holds_the_member_row_so_a_concurrent_leave_cannot_slip_between_check_and_apply() {
    let f = fixture().await;
    exec(&f.su, REPUTATION_APPLY_STUB).await;
    // Slow the function so the apply transaction stays open, then try to make
    // the member leave concurrently: the UPDATE must block on the FOR SHARE
    // row lock until the apply commits (so the apply saw a live member).
    exec(&f.su, "INSERT INTO rep_test_sleep VALUES (1.5)").await;
    let su = f.su.clone();
    let member = f.member;
    let leaver = tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(500)).await;
        let started = std::time::Instant::now();
        exec(
            &su,
            &format!("UPDATE community_members SET left_at = NOW() WHERE user_uuid = '{member}'"),
        )
        .await;
        started.elapsed()
    });
    let out = ap(
        &f,
        &scope(),
        f.member,
        1,
        ReasonClass::Activity,
        "race-1",
        acaps(),
    )
    .await;
    assert!(out.unwrap().applied, "the apply saw a live member");
    let waited = leaver.await.unwrap();
    assert!(
        waited >= Duration::from_millis(700),
        "the leave must have blocked on the FOR SHARE row lock (waited {waited:?})"
    );
    // After the leave, a fresh apply is refused.
    assert_eq!(
        ap(
            &f,
            &scope(),
            f.member,
            1,
            ReasonClass::Activity,
            "race-2",
            acaps()
        )
        .await,
        Err(ReputationError::NotAMember)
    );
}

#[tokio::test]
async fn history_pages_newest_first_and_is_scoped_to_the_community() {
    let f = fixture().await;
    exec(&f.su, REPUTATION_APPLY_STUB).await;
    for i in 0..4 {
        ap(
            &f,
            &scope(),
            f.member,
            1,
            ReasonClass::Activity,
            &format!("h{i}"),
            acaps(),
        )
        .await
        .unwrap();
    }
    let page1 = f.store.history(&scope(), f.member, 3, None).await.unwrap();
    assert_eq!(page1.len(), 3);
    assert!(page1[0].id > page1[1].id && page1[1].id > page1[2].id);
    assert_eq!(page1[0].balance_after, Some(604));
    let page2 = f
        .store
        .history(&scope(), f.member, 3, Some(page1[2].id))
        .await
        .unwrap();
    assert_eq!(page2.len(), 1, "4 rows total: 3 + 1");
    assert!(page2[0].occurred_at.ends_with('Z'));
    let other = ReputationScope {
        tenant_id: OTHER_TENANT,
        community_id: OTHER_COMMUNITY,
        app_id: APP_ID.to_string(),
    };
    assert!(f
        .store
        .history(&other, f.member, 10, None)
        .await
        .unwrap()
        .is_empty());
    assert!(matches!(
        f.store.history(&scope(), f.member, 0, None).await,
        Err(ReputationError::Invalid(_))
    ));
    assert!(matches!(
        f.store.history(&scope(), f.member, 101, None).await,
        Err(ReputationError::Invalid(_))
    ));
}

#[tokio::test]
async fn tenant_aggregate_averages_own_communities_never_another_tenants() {
    let f = fixture().await;
    exec(&f.su, REPUTATION_APPLY_STUB).await;
    exec(
        &f.su,
        "INSERT INTO communities (id, tenant_id) VALUES (11, 1)",
    )
    .await;
    exec(
        &f.su,
        &format!(
            "INSERT INTO community_members (community_id, user_uuid) VALUES (11, '{m}'), (20, '{m}')",
            m = f.member
        ),
    )
    .await;
    let s10 = scope();
    let s11 = ReputationScope {
        community_id: 11,
        ..scope()
    };
    let s20 = ReputationScope {
        tenant_id: OTHER_TENANT,
        community_id: OTHER_COMMUNITY,
        app_id: APP_ID.to_string(),
    };
    assert_eq!(
        f.store.get_tenant(&s10, f.member).await.unwrap(),
        TenantScore {
            score: 600,
            communities: 0
        }
    );
    ap(
        &f,
        &s10,
        f.member,
        -200,
        ReasonClass::Moderation,
        "a",
        acaps(),
    )
    .await
    .unwrap(); // 400
    ap(&f, &s11, f.member, 5, ReasonClass::Activity, "b", acaps())
        .await
        .unwrap(); // 605
    ap(&f, &s20, f.member, 5, ReasonClass::Activity, "c", acaps())
        .await
        .unwrap(); // other tenant
    let t = f.store.get_tenant(&s10, f.member).await.unwrap();
    assert_eq!(
        t,
        TenantScore {
            score: 503,
            communities: 2
        },
        "mean(400,605)=502.5 rounds to 503; tenant 2's row excluded"
    );
    assert_eq!(
        f.store.get_tenant(&s10, Uuid::new_v4()).await,
        Err(ReputationError::NotAMember)
    );
}

#[tokio::test]
async fn below_threshold_lists_lowest_first_active_members_only() {
    let f = fixture().await;
    exec(&f.su, REPUTATION_APPLY_STUB).await;
    let low = add_member(&f, COMMUNITY).await;
    let high = add_member(&f, COMMUNITY).await;
    let gone = add_member(&f, COMMUNITY).await;
    ap(
        &f,
        &scope(),
        low,
        -200,
        ReasonClass::Moderation,
        "l",
        acaps(),
    )
    .await
    .unwrap(); // 400
    ap(&f, &scope(), high, 5, ReasonClass::Activity, "h", acaps())
        .await
        .unwrap(); // 605
    ap(
        &f,
        &scope(),
        gone,
        -100,
        ReasonClass::Moderation,
        "g",
        acaps(),
    )
    .await
    .unwrap(); // 500
    exec(
        &f.su,
        &format!("UPDATE community_members SET left_at = NOW() WHERE user_uuid = '{gone}'"),
    )
    .await;
    let rows = f.store.below_threshold(&scope(), 600, 10).await.unwrap();
    assert_eq!(
        rows,
        vec![ScoreEntry {
            user: low,
            score: 400
        }]
    );
    assert!(f
        .store
        .below_threshold(&scope(), 300, 10)
        .await
        .unwrap()
        .is_empty());
    assert!(matches!(
        f.store.below_threshold(&scope(), 600, 0).await,
        Err(ReputationError::Invalid(_))
    ));
}

#[tokio::test]
async fn tenant_policy_is_read_from_the_hub_api_table_and_validated() {
    let f = fixture().await;
    exec(&f.su, REPUTATION_APPLY_STUB).await;
    assert_eq!(f.store.tenant_policy(&scope()).await, Ok(None));
    exec(
        &f.su,
        "INSERT INTO reputation_tenant_policy VALUES (1, true, 350, 450)",
    )
    .await;
    assert_eq!(
        f.store.tenant_policy(&scope()).await,
        Ok(Some(TenantPolicy {
            auto_ban_enabled: true,
            auto_ban_floor: 350,
            at_risk_threshold: 450
        }))
    );
    let other = ReputationScope {
        tenant_id: OTHER_TENANT,
        ..scope()
    };
    assert_eq!(f.store.tenant_policy(&other).await, Ok(None));
    exec(
        &f.su,
        "UPDATE reputation_tenant_policy SET tenant_at_risk_threshold = 300",
    )
    .await;
    assert!(matches!(
        f.store.tenant_policy(&scope()).await,
        Err(ReputationError::Invalid(_))
    ));
}

#[tokio::test]
async fn soft_ban_is_idempotent_community_scoped_and_liftable() {
    let f = fixture().await;
    exec(&f.su, REPUTATION_APPLY_STUB).await;
    assert_eq!(f.store.is_soft_banned(&scope(), f.member).await, Ok(false));
    assert_eq!(
        f.store
            .soft_ban(
                &scope(),
                f.member,
                "community_floor",
                SoftBanLimits::DEFAULT
            )
            .await,
        Ok(true)
    );
    assert_eq!(
        f.store
            .soft_ban(
                &scope(),
                f.member,
                "community_floor",
                SoftBanLimits::DEFAULT
            )
            .await,
        Ok(false),
        "second call is a no-op"
    );
    assert_eq!(f.store.is_soft_banned(&scope(), f.member).await, Ok(true));
    let other = ReputationScope {
        tenant_id: OTHER_TENANT,
        community_id: OTHER_COMMUNITY,
        app_id: APP_ID.to_string(),
    };
    assert_eq!(f.store.is_soft_banned(&other, f.member).await, Ok(false));
    assert_eq!(
        f.store
            .soft_ban(
                &scope(),
                Uuid::new_v4(),
                "community_floor",
                SoftBanLimits::DEFAULT
            )
            .await,
        Err(ReputationError::NotAMember)
    );
    assert!(matches!(
        f.store
            .soft_ban(&scope(), f.member, "Bad Reason", SoftBanLimits::DEFAULT)
            .await,
        Err(ReputationError::Invalid(_))
    ));
    // Only a ban with the exact reason is lifted: a manual one is never cleared.
    assert_eq!(
        f.store
            .lift_soft_ban(&scope(), f.member, "manual", SoftBanLimits::DEFAULT)
            .await,
        Ok(false)
    );
    assert_eq!(f.store.is_soft_banned(&scope(), f.member).await, Ok(true));
    assert_eq!(
        f.store
            .lift_soft_ban(
                &scope(),
                f.member,
                "community_floor",
                SoftBanLimits::DEFAULT
            )
            .await,
        Ok(true)
    );
    assert_eq!(f.store.is_soft_banned(&scope(), f.member).await, Ok(false));
    // A fresh ban can be recorded again after a lift.
    assert_eq!(
        f.store
            .soft_ban(
                &scope(),
                f.member,
                "community_floor",
                SoftBanLimits::DEFAULT
            )
            .await,
        Ok(true)
    );
}

/// Generous caps for tests that are not about the caps.
fn acaps() -> ReputationCaps {
    caps2(100_000, 1_000_000)
}

#[tokio::test]
async fn soft_ban_toggles_are_quota_limited_per_user_and_per_community_and_typed() {
    let f = fixture().await;
    exec(&f.su, REPUTATION_APPLY_STUB).await;
    let lim = SoftBanLimits {
        per_user_daily: 4,
        per_community_daily: 5,
    };
    let sc = scope();
    // 2 full cycles = 4 toggles for `member`.
    for _ in 0..2 {
        assert_eq!(
            f.store
                .soft_ban(&sc, f.member, "community_soft_floor", lim)
                .await,
            Ok(true)
        );
        assert_eq!(
            f.store
                .lift_soft_ban(&sc, f.member, "community_soft_floor", lim)
                .await,
            Ok(true)
        );
    }
    // The 5th toggle for the same user is rate limited, state unchanged.
    assert_eq!(
        f.store
            .soft_ban(&sc, f.member, "community_soft_floor", lim)
            .await,
        Err(ReputationError::RateLimited { cap: 4 })
    );
    assert_eq!(f.store.is_soft_banned(&sc, f.member).await, Ok(false));
    // A no-op (nothing to lift) is free and never rate limited.
    assert_eq!(
        f.store
            .lift_soft_ban(&sc, f.member, "community_soft_floor", lim)
            .await,
        Ok(false)
    );
    // Another user: 1 toggle left in the community budget (4 used of 5).
    let other = add_member(&f, COMMUNITY).await;
    assert_eq!(
        f.store
            .soft_ban(&sc, other, "community_soft_floor", lim)
            .await,
        Ok(true)
    );
    let third = add_member(&f, COMMUNITY).await;
    assert_eq!(
        f.store
            .soft_ban(&sc, third, "community_soft_floor", lim)
            .await,
        Err(ReputationError::RateLimited { cap: 5 })
    );
    // Old toggles roll out of the window.
    exec(
        &f.su,
        "UPDATE reputation_soft_bans SET created_at = NOW() - INTERVAL '25 hours', \
         lifted_at = CASE WHEN lifted_at IS NULL THEN NULL ELSE NOW() - INTERVAL '25 hours' END",
    )
    .await;
    assert_eq!(
        f.store
            .soft_ban(&sc, third, "community_soft_floor", lim)
            .await,
        Ok(true)
    );
}
