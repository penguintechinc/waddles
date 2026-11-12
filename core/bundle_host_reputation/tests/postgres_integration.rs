//! Integration tests against a **real** Postgres container: the host's calls
//! into the reputation SQL contract (`reputation_apply`, the floor soft-ban
//! wrappers, `reputation_is_soft_banned`, `reputation_active_logic_app`), the
//! unscored-default reads, and the `waddles_bundle_reputation` role's privilege
//! boundary (no direct writes, no manual soft-ban EXECUTE) can only be proven
//! by a real server. The schema is the shipped 0050/0072 DDL plus the contract
//! SQL: PR #1236's `scripts/db/bundle_reputation_unify.sql` when present in the
//! checkout, else the test-only stand-in `tests/support/reputation_contract_standin.sql`
//! (see `contract_sql()`); the migration itself is never duplicated here.
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
    SoftBanLimits, TenantPolicy, TenantScore, FLOOR_SOFT_BAN_REASON, MAX_LEADERBOARD_LIMIT,
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
    member: Uuid,
}

fn scope() -> ReputationScope {
    ReputationScope {
        tenant_id: TENANT,
        community_id: COMMUNITY,
        app_id: APP_ID.to_string(),
    }
}

/// The contract SQL under test: PR #1236's `scripts/db/bundle_reputation_unify.sql`
/// (a stacked dependency of this PR). Fails loudly when it is absent -- a gate that
/// silently falls back to a copy is not a gate.
fn contract_sql() -> String {
    let real = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../scripts/db/bundle_reputation_unify.sql");
    std::fs::read_to_string(&real).unwrap_or_else(|e| {
        panic!(
            "contract SQL {} missing ({e}); this suite needs PR #1236's \
             bundle_reputation_unify.sql in the checkout",
            real.display()
        )
    })
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
         CREATE TABLE bundle_tenant_availability (tenant_id INTEGER, app_id TEXT, available BOOLEAN);
         CREATE TABLE app_global_installs (app_id TEXT, version TEXT, superseded_by BIGINT, revoked_at TIMESTAMPTZ);
         CREATE TABLE app_version_uploads (app_id TEXT, version TEXT, manifest_json JSONB);
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
    // The reputation SQL contract (functions + policy/soft-ban tables + the
    // role's contract grants).
    exec(&su, &contract_sql()).await;

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
        member,
    }
}

async fn ledger_count(f: &Fixture) -> i64 {
    scalar_i64(&f.su, "SELECT COUNT(*) FROM bundle_reputation_adjustments").await
}

async fn scalar_text(conn: &DatabaseConnection, sql: &str) -> String {
    conn.query_one_raw(Statement::from_string(sea_orm::DbBackend::Postgres, sql))
        .await
        .unwrap()
        .unwrap()
        .try_get_by_index::<String>(0)
        .unwrap()
}

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

fn key(k: &str) -> IdempotencyKey {
    IdempotencyKey::new(k).unwrap()
}

/// Gate-quota caps the host passes; the SQL ignores them (it is authoritative).
fn caps() -> ReputationCaps {
    ReputationCaps {
        per_user_daily_abs_max: i64::MAX,
        per_scope_daily_abs_max: i64::MAX,
    }
}

async fn apply(
    f: &Fixture,
    user: Uuid,
    delta: i32,
    class: ReasonClass,
    k: &str,
) -> Result<ApplyOutcome, ReputationError> {
    f.store
        .apply(
            &scope(),
            user,
            delta,
            class,
            "activity.follow",
            caps(),
            &key(k),
        )
        .await
}

#[tokio::test]
async fn adjust_goes_through_reputation_apply_with_app_actor_and_the_unscored_default() {
    let f = fixture().await;
    // Unscored member reads as the policy/legacy default (600), not 0.
    assert_eq!(f.store.get(&scope(), f.member).await, Ok(600));
    assert_eq!(
        f.store
            .adjust(&scope(), f.member, 5, "game.win", caps())
            .await,
        Ok(605)
    );
    assert_eq!(f.store.get(&scope(), f.member).await, Ok(605));
    assert_eq!(ledger_count(&f).await, 1);
    assert_eq!(
        scalar_text(
            &f.su,
            "SELECT kind || '|' || app_id || '|' || actor || '|' || source_event \
             FROM bundle_reputation_adjustments"
        )
        .await,
        format!("adjust|{APP_ID}|system:bundle|game.win")
    );
}

#[tokio::test]
async fn non_members_are_rejected_by_the_sql_for_every_flavor_of_non_membership() {
    let f = fixture().await;
    let stranger = Uuid::new_v4();
    let left = Uuid::new_v4();
    let removed = Uuid::new_v4();
    let inactive = Uuid::new_v4();
    let null_active = Uuid::new_v4();
    let other_community = Uuid::new_v4();
    exec(
        &f.su,
        &format!(
            "INSERT INTO community_members (community_id, user_uuid, left_at) VALUES (10, '{left}', NOW());
             INSERT INTO community_members (community_id, user_uuid, removed_at) VALUES (10, '{removed}', NOW());
             INSERT INTO community_members (community_id, user_uuid, is_active) VALUES (10, '{inactive}', false);
             INSERT INTO community_members (community_id, user_uuid, is_active) VALUES (10, '{null_active}', NULL);
             INSERT INTO community_members (community_id, user_uuid) VALUES (20, '{other_community}');"
        ),
    )
    .await;
    for who in [
        stranger,
        left,
        removed,
        inactive,
        null_active,
        other_community,
    ] {
        assert_eq!(
            f.store.adjust(&scope(), who, 1, "r", caps()).await,
            Err(ReputationError::NotAMember),
            "adjust {who}"
        );
        assert_eq!(
            apply(&f, who, 1, ReasonClass::Activity, "k1").await,
            Err(ReputationError::NotAMember),
            "apply {who}"
        );
        assert_eq!(
            f.store.get(&scope(), who).await,
            Err(ReputationError::NotAMember),
            "get {who}"
        );
    }
    // Tenant mismatch: a real member, but the scope names the wrong tenant.
    let wrong_tenant = ReputationScope {
        tenant_id: OTHER_TENANT,
        ..scope()
    };
    assert_eq!(
        f.store
            .adjust(&wrong_tenant, f.member, 1, "r", caps())
            .await,
        Err(ReputationError::NotAMember)
    );
    assert_eq!(ledger_count(&f).await, 0, "nothing was written");
}

#[tokio::test]
async fn adjust_many_is_atomic_and_a_replayed_batch_is_a_no_op() {
    let f = fixture().await;
    let b = add_member(&f, COMMUNITY).await;
    let k = key("evt-1:reputation_adjust_many:0");
    f.store
        .adjust_many(
            &scope(),
            &[(f.member, 3), (b, -2)],
            "game.round",
            caps(),
            &k,
        )
        .await
        .unwrap();
    assert_eq!(f.store.get(&scope(), f.member).await, Ok(603));
    assert_eq!(f.store.get(&scope(), b).await, Ok(598));
    f.store
        .adjust_many(
            &scope(),
            &[(f.member, 3), (b, -2)],
            "game.round",
            caps(),
            &k,
        )
        .await
        .unwrap();
    assert_eq!(
        f.store.get(&scope(), f.member).await,
        Ok(603),
        "replay moved nothing"
    );
    assert_eq!(ledger_count(&f).await, 2);
    // A non-member in the batch aborts everything.
    let stranger = Uuid::new_v4();
    assert_eq!(
        f.store
            .adjust_many(
                &scope(),
                &[(f.member, 1), (stranger, 1)],
                "game.round",
                caps(),
                &key("k2")
            )
            .await,
        Err(ReputationError::NotAMember)
    );
    assert_eq!(f.store.get(&scope(), f.member).await, Ok(603));
    assert_eq!(ledger_count(&f).await, 2);
    // Shape errors never reach the SQL.
    assert!(f
        .store
        .adjust_many(&scope(), &[], "r", caps(), &key("k3"))
        .await
        .is_err());
    assert!(f
        .store
        .adjust_many(
            &scope(),
            &[(f.member, 1), (f.member, 1)],
            "r",
            caps(),
            &key("k4")
        )
        .await
        .is_err());
}

#[tokio::test]
async fn reads_report_unscored_members_at_the_policy_default_everywhere() {
    let f = fixture().await;
    let b = add_member(&f, COMMUNITY).await;
    let c = add_member(&f, COMMUNITY).await;
    f.store
        .adjust(&scope(), b, 4, "game.win", caps())
        .await
        .unwrap(); // 604
                   // get_many / leaderboard / below_threshold all agree on the default (600).
    let many = f.store.get_many(&scope(), &[f.member, b, c]).await.unwrap();
    assert_eq!(
        many.iter().map(|e| e.score).collect::<Vec<_>>(),
        vec![600, 604, 600]
    );
    let board = f.store.leaderboard(&scope(), 10).await.unwrap();
    assert_eq!(board.len(), 3, "unscored members are listed");
    assert_eq!(
        board[0],
        ScoreEntry {
            user: b,
            score: 604
        }
    );
    assert!(board[1..].iter().all(|e| e.score == 600));
    let below = f.store.below_threshold(&scope(), 601, 10).await.unwrap();
    assert_eq!(below.len(), 2, "the two unscored members are < 601");
    assert!(f
        .store
        .below_threshold(&scope(), 600, 10)
        .await
        .unwrap()
        .is_empty());
    // A per-community policy default changes the unscored reading.
    exec(
        &f.su,
        "INSERT INTO bundle_reputation_policy (tenant_id, community_id, score_default) VALUES (1, 10, 700)",
    )
    .await;
    assert_eq!(f.store.get(&scope(), c).await, Ok(700));
    assert_eq!(
        f.store.get(&scope(), b).await,
        Ok(604),
        "scored member unaffected"
    );
    // Other community isolation + limits.
    assert!(f.store.leaderboard(&scope(), 0).await.is_err());
    assert!(f
        .store
        .leaderboard(&scope(), MAX_LEADERBOARD_LIMIT + 1)
        .await
        .is_err());
}

#[tokio::test]
async fn history_is_newest_first_scoped_and_reports_the_contract_kinds() {
    let f = fixture().await;
    f.store
        .adjust(&scope(), f.member, 2, "game.win", caps())
        .await
        .unwrap();
    apply(&f, f.member, -10, ReasonClass::Moderation, "m1")
        .await
        .unwrap();
    let h = f.store.history(&scope(), f.member, 10, None).await.unwrap();
    assert_eq!(
        h.iter().map(|e| e.kind.as_str()).collect::<Vec<_>>(),
        vec!["moderation", "adjust"]
    );
    assert_eq!(h[0].balance_after, Some(592));
    let page = f
        .store
        .history(&scope(), f.member, 10, Some(h[0].id))
        .await
        .unwrap();
    assert_eq!(page.len(), 1);
    let other = ReputationScope {
        community_id: OTHER_COMMUNITY,
        tenant_id: OTHER_TENANT,
        ..scope()
    };
    assert!(f
        .store
        .history(&other, f.member, 10, None)
        .await
        .unwrap()
        .is_empty());
}

#[tokio::test]
async fn tenant_aggregate_reads_own_communities_only() {
    let f = fixture().await;
    f.store
        .adjust(&scope(), f.member, 4, "game.win", caps())
        .await
        .unwrap();
    assert_eq!(
        f.store.get_tenant(&scope(), f.member).await,
        Ok(TenantScore {
            score: 604,
            communities: 1
        })
    );
    assert_eq!(
        f.store.get_tenant(&scope(), Uuid::new_v4()).await,
        Err(ReputationError::NotAMember)
    );
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
    let all = load_membership(&f.rep_conn, None).await.unwrap();
    assert_eq!(all.len(), 2, "{all:?}");
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

/// Raises the moderation cap / enables the floor ban for community 10 (the
/// contract's per-community policy row).
async fn policy(f: &Fixture, mod_cap: i32, soft_enabled: bool) {
    exec(
        &f.su,
        &format!(
            "INSERT INTO bundle_reputation_policy \
             (tenant_id, community_id, moderation_user_daily_cap, soft_ban_enabled) \
             VALUES (1, 10, {mod_cap}, {soft_enabled})"
        ),
    )
    .await;
}

#[tokio::test]
async fn caps_are_enforced_inside_the_sql_not_by_the_host_caps_argument() {
    let f = fixture().await;
    // The host passes effectively unlimited caps; the SQL's own per-user activity
    // cap (policy default 100 / 24h) still binds -> RateLimited.
    for _ in 0..2 {
        f.store
            .adjust(&scope(), f.member, 50, "game.win", caps())
            .await
            .unwrap();
    }
    assert!(matches!(
        f.store
            .adjust(&scope(), f.member, 1, "game.win", caps())
            .await,
        Err(ReputationError::RateLimited { .. })
    ));
    assert_eq!(ledger_count(&f).await, 2, "the refused call wrote nothing");
    // Moderation has its own budget (kind = 'moderation', default 200).
    assert!(apply(&f, f.member, -150, ReasonClass::Moderation, "m1")
        .await
        .is_ok());
    assert!(matches!(
        apply(&f, f.member, -100, ReasonClass::Moderation, "m2").await,
        Err(ReputationError::RateLimited { .. })
    ));
}

#[tokio::test]
async fn concurrent_adjusts_never_exceed_the_sql_cap() {
    let f = Arc::new(fixture().await);
    let mut tasks = Vec::new();
    for _ in 0..8 {
        let f = Arc::clone(&f);
        tasks.push(tokio::spawn(async move {
            f.store
                .adjust(&scope(), f.member, 20, "game.win", caps())
                .await
        }));
    }
    let mut ok = 0;
    for t in tasks {
        if t.await.unwrap().is_ok() {
            ok += 1;
        }
    }
    assert_eq!(ok, 5, "exactly cap/step adjusts succeed");
    assert_eq!(f.store.get(&scope(), f.member).await, Ok(700));
}

#[tokio::test]
async fn apply_is_idempotent_clamped_and_validates_inputs_in_the_sql() {
    let f = fixture().await;
    policy(&f, 1000, false).await;
    let first = apply(
        &f,
        f.member,
        5,
        ReasonClass::Activity,
        "evt-1:reputation_apply:0",
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
    // Replay: applied = false, nothing moves, no second ledger row.
    let replay = apply(
        &f,
        f.member,
        5,
        ReasonClass::Activity,
        "evt-1:reputation_apply:0",
    )
    .await
    .unwrap();
    assert!(!replay.applied);
    assert_eq!(ledger_count(&f).await, 1);
    // An UPPERCASE host key is injectively normalised for the SQL alphabet:
    // EVT-9 and evt-9 are distinct operations.
    assert!(
        apply(
            &f,
            f.member,
            1,
            ReasonClass::Activity,
            "EVT-9:reputation_apply:0"
        )
        .await
        .unwrap()
        .applied
    );
    assert!(
        apply(
            &f,
            f.member,
            1,
            ReasonClass::Activity,
            "evt-9:reputation_apply:0"
        )
        .await
        .unwrap()
        .applied
    );
    // Moderation clamps at the SQL floor (300) over two -200 hits.
    apply(&f, f.member, -200, ReasonClass::Moderation, "m1")
        .await
        .unwrap();
    let low = apply(&f, f.member, -200, ReasonClass::Moderation, "m2")
        .await
        .unwrap();
    assert_eq!(low.new_score, 300);
    // Over-long normalised key and a bad source event are Invalid, writing nothing.
    let n = ledger_count(&f).await;
    assert!(matches!(
        apply(&f, f.member, 1, ReasonClass::Activity, &"A".repeat(100)).await,
        Err(ReputationError::Invalid(_))
    ));
    assert!(matches!(
        f.store
            .apply(
                &scope(),
                f.member,
                1,
                ReasonClass::Activity,
                "Bad Event",
                caps(),
                &key("k")
            )
            .await,
        Err(ReputationError::Invalid(_))
    ));
    assert_eq!(ledger_count(&f).await, n);
}

#[tokio::test]
async fn moderation_writes_the_moderation_kind_and_a_positive_delta_never_reaches_the_sql() {
    let f = fixture().await;
    apply(&f, f.member, -50, ReasonClass::Moderation, "m1")
        .await
        .unwrap();
    assert_eq!(
        scalar_text(
            &f.su,
            "SELECT kind FROM bundle_reputation_adjustments ORDER BY id DESC LIMIT 1"
        )
        .await,
        "moderation"
    );
    // Positive moderation == admin_override (class 3): refused host-side.
    assert!(matches!(
        apply(&f, f.member, 30, ReasonClass::Moderation, "m2").await,
        Err(ReputationError::Invalid(_))
    ));
    assert_eq!(ledger_count(&f).await, 1);
    // And the SQL itself refuses a positive moderation call, class 3 and a human
    // actor for the bundle role.
    for (delta, class, actor) in [
        (5, 2, "system:bundle".to_string()),
        (5, 3, "system:bundle".to_string()),
        (-5, 1, Uuid::new_v4().to_string()),
    ] {
        let r = f
            .rep_conn
            .execute_unprepared(&format!(
                "SELECT * FROM reputation_apply(1, 10, '{}'::uuid, {delta}, {class}::smallint, \
                 {class}::smallint, '{APP_ID}', '{actor}', 'x', 'direct-{delta}-{class}')",
                f.member
            ))
            .await;
        assert!(
            r.is_err(),
            "refused: delta {delta} class {class} actor {actor}"
        );
    }
    assert_eq!(ledger_count(&f).await, 1);
}

#[tokio::test]
async fn override_is_not_a_bundle_store_operation() {
    let f = fixture().await;
    assert!(f
        .store
        .override_score(&scope(), f.member, 700, "admin.fix", &key("k"))
        .await
        .is_err());
    assert_eq!(ledger_count(&f).await, 0);
}

#[tokio::test]
async fn soft_bans_route_only_through_the_floor_wrappers_and_manual_bans_are_untouchable() {
    let f = fixture().await;
    policy(&f, 1000, true).await; // floor 550, auto-ban enabled
    let u = f.member;
    assert_eq!(f.store.is_soft_banned(&scope(), u).await, Ok(false));
    // Score 600 is above the floor: the SQL refuses -> nothing set.
    assert_eq!(
        f.store.soft_ban(&scope(), u, FLOOR_SOFT_BAN_REASON).await,
        Ok(false)
    );
    assert_eq!(f.store.is_soft_banned(&scope(), u).await, Ok(false));
    apply(&f, u, -100, ReasonClass::Moderation, "m1")
        .await
        .unwrap(); // 500 < 550
    assert_eq!(
        f.store.soft_ban(&scope(), u, FLOOR_SOFT_BAN_REASON).await,
        Ok(true)
    );
    assert_eq!(
        f.store.soft_ban(&scope(), u, FLOOR_SOFT_BAN_REASON).await,
        Ok(false),
        "already banned"
    );
    assert_eq!(f.store.is_soft_banned(&scope(), u).await, Ok(true));
    assert_eq!(
        scalar_text(
            &f.su,
            "SELECT reason_code || '|' || set_by FROM bundle_reputation_soft_bans"
        )
        .await,
        "1|system:floor"
    );
    assert_eq!(
        f.store
            .lift_soft_ban(&scope(), u, FLOOR_SOFT_BAN_REASON, SoftBanLimits::DEFAULT)
            .await,
        Ok(true)
    );
    assert_eq!(f.store.is_soft_banned(&scope(), u).await, Ok(false));
    // The bundle may never choose another reason.
    assert!(matches!(
        f.store.soft_ban(&scope(), u, "manual.admin").await,
        Err(ReputationError::Invalid(_))
    ));
    assert!(matches!(
        f.store
            .lift_soft_ban(&scope(), u, "manual.admin", SoftBanLimits::DEFAULT)
            .await,
        Err(ReputationError::Invalid(_))
    ));
    // A MANUAL ban (hub-api, reason 2, UUID admin) survives: a floor set never
    // overwrites it and a floor lift never clears it.
    let admin = Uuid::new_v4();
    exec(
        &f.su,
        &format!(
            "SELECT reputation_set_soft_ban(1, 10, '{u}'::uuid, 2::smallint, '{admin}', NULL)"
        ),
    )
    .await;
    assert_eq!(f.store.is_soft_banned(&scope(), u).await, Ok(true));
    assert_eq!(
        f.store.soft_ban(&scope(), u, FLOOR_SOFT_BAN_REASON).await,
        Ok(false)
    );
    assert_eq!(
        f.store
            .lift_soft_ban(&scope(), u, FLOOR_SOFT_BAN_REASON, SoftBanLimits::DEFAULT)
            .await,
        Ok(false)
    );
    assert_eq!(f.store.is_soft_banned(&scope(), u).await, Ok(true));
    assert_eq!(
        scalar_text(
            &f.su,
            "SELECT reason_code::text FROM bundle_reputation_soft_bans"
        )
        .await,
        "2"
    );
}

#[tokio::test]
async fn a_floor_ban_auto_clears_in_the_sql_when_the_score_recovers() {
    let f = fixture().await;
    policy(&f, 1000, true).await;
    apply(&f, f.member, -100, ReasonClass::Moderation, "m1")
        .await
        .unwrap(); // 500
    f.store
        .soft_ban(&scope(), f.member, FLOOR_SOFT_BAN_REASON)
        .await
        .unwrap();
    assert_eq!(f.store.is_soft_banned(&scope(), f.member).await, Ok(true));
    // Recovery arrives as an admin override (hub-api / superuser only).
    exec(
        &f.su,
        &format!(
            "SELECT * FROM reputation_apply(1, 10, '{}'::uuid, 100, 3::smallint, 3::smallint, \
             NULL, '{}', 'admin.restore', 'k-admin')",
            f.member,
            Uuid::new_v4()
        ),
    )
    .await;
    assert_eq!(f.store.is_soft_banned(&scope(), f.member).await, Ok(false));
}

#[tokio::test]
async fn the_bundle_role_cannot_set_or_clear_manual_soft_bans() {
    let f = fixture().await;
    let u = f.member;
    for sql in [
        format!("INSERT INTO bundle_reputation_soft_bans (tenant_id, community_id, user_uuid, reason_code, set_by) VALUES (1, 10, '{u}', 1, 'x')"),
        "DELETE FROM bundle_reputation_soft_bans".to_string(),
        format!("SELECT reputation_set_soft_ban(1, 10, '{u}'::uuid, 2::smallint, '{}', NULL)", Uuid::new_v4()),
        format!("SELECT reputation_clear_soft_ban(1, 10, '{u}'::uuid, 'x', false)"),
    ] {
        assert!(
            f.rep_conn.execute_unprepared(&sql).await.is_err(),
            "bundle role must be denied: {sql}"
        );
    }
    assert!(f
        .rep_conn
        .execute_unprepared("SELECT 1 FROM bundle_reputation_tenant_policy")
        .await
        .is_ok());
}

#[tokio::test]
async fn tenant_policy_and_active_logic_app_read_the_contract_objects() {
    let f = fixture().await;
    assert_eq!(f.store.tenant_policy(&scope()).await, Ok(None));
    exec(
        &f.su,
        "INSERT INTO bundle_reputation_tenant_policy VALUES (1, true, 350, 450)",
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
    // No active logic app -> None (host fails closed).
    assert_eq!(f.store.active_logic_app(&scope()).await, Ok(None));
    let install = |app: &str| {
        format!(
            "INSERT INTO bundle_tenant_availability VALUES (1, '{app}', true);
             INSERT INTO app_global_installs VALUES ('{app}', '1', NULL, NULL);
             INSERT INTO app_version_uploads VALUES ('{app}', '1', '{{\"provides\": [\"reputation-logic\"]}}');"
        )
    };
    exec(&f.su, &install("waddles.core.app.reputation-accrual")).await;
    assert_eq!(
        f.store.active_logic_app(&scope()).await,
        Ok(Some("waddles.core.app.reputation-accrual".to_string()))
    );
    // Two providers -> ambiguous -> NULL (host fails closed).
    exec(&f.su, &install("acme.second-logic")).await;
    assert_eq!(f.store.active_logic_app(&scope()).await, Ok(None));
}
