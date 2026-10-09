//! Issue #726: the `reputation` capability's real path joined end to end on
//! the stage side -- `CapabilityHandler::handle` (host-call decode) -> the REAL
//! `CapabilityGate` (grants, declared bounds, quotas, production
//! `SnapshotMembership` populated from the DB by the REAL
//! `load_membership`) -> the REAL `PostgresReputationStore` -> a real Postgres
//! container running the exact shipped DDL
//! (`scripts/db/bundle_reputation_store.sql`). Nothing in the chain is a test
//! double. (The executor half -- real wasm calling the import and emitting
//! this exact wire shape -- is `core/bundle_executor/tests/
//! stage_next_reputation.rs`.)
//!
//! Requires Docker via `testcontainers`.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;

use bundle_capability_gate::{
    CapabilityGate, GrantScopeKey, GrantSet, GrantedPermission, InMemoryGrantSnapshot,
    InMemoryInstancePolicySnapshot, InMemoryQuotaLedger, SnapshotMembership,
};
use bundle_host_http::egress::{boxed, EgressGuard, EgressLimits, ReqwestTransport, StaticFlag};
use bundle_host_reputation::{
    connect, load_membership, ConnectConfig, PostgresReputationStore, ReputationStore,
};
use penguin_bundle_host::wire::{CapabilityKind, HostCallBody};
use sea_orm::{ConnectionTrait, Database, DatabaseConnection, Statement};
use svc_process::capabilities::{
    CapabilityHandler, HttpEgressCatalog, ReputationWiring, StageCapabilities,
};
use svc_process::license::StaticGate;
use testcontainers::core::logs::LogSource;
use testcontainers::core::wait::LogWaitStrategy;
use testcontainers::core::{ContainerPort, WaitFor};
use testcontainers::runners::AsyncRunner;
use testcontainers::{ContainerAsync, GenericImage, ImageExt};
use uuid::Uuid;

const SU_PW: &str = "postgres_test_superuser_pw";
const REP_PW: &str = "waddles_bundle_reputation_test_pw";
const APP_ID: &str = "waddles.core.test-reputation";
const TENANT: i32 = 1;
const COMMUNITY: i32 = 10;
const SHIPPED_DDL: &str = include_str!("../../../scripts/db/bundle_reputation_store.sql");

struct World {
    _container: ContainerAsync<GenericImage>,
    su: DatabaseConnection,
    /// Reputation-role connection settings, to build a restarted/second stack.
    cfg: ConnectConfig,
    caps: StageCapabilities,
    /// `members[0]`.
    member: Uuid,
    /// Every active identified member of community 10 (`member` first).
    members: Vec<Uuid>,
}

async fn exec(c: &DatabaseConnection, sql: &str) {
    c.execute_unprepared(sql)
        .await
        .unwrap_or_else(|e| panic!("sql failed: {e}\n{sql}"));
}

async fn scalar(c: &DatabaseConnection, sql: &str) -> i64 {
    c.query_one_raw(Statement::from_string(sea_orm::DbBackend::Postgres, sql))
        .await
        .unwrap()
        .unwrap()
        .try_get_by_index::<i64>(0)
        .unwrap()
}

fn grant(id: &str, params: serde_json::Value) -> (String, GrantedPermission) {
    (
        id.to_string(),
        GrantedPermission {
            permission_id: id.to_string(),
            params,
        },
    )
}

async fn world() -> World {
    world_with_members(0).await
}

/// `1 + extra_members` active members, all loaded into the gate's snapshot.
async fn world_with_members(extra_members: usize) -> World {
    let container = GenericImage::new("postgres", "17.6-bookworm")
        .with_exposed_port(ContainerPort::Tcp(5432))
        .with_wait_for(WaitFor::log(
            LogWaitStrategy::new(
                LogSource::BothStd,
                "database system is ready to accept connections",
            )
            .with_times(2),
        ))
        .with_env_var("POSTGRES_PASSWORD", SU_PW)
        .with_env_var("POSTGRES_DB", "waddles_test")
        .start()
        .await
        .expect("postgres container starts");
    let host = container.get_host().await.unwrap().to_string();
    let port = container.get_host_port_ipv4(5432).await.unwrap();
    let su = Database::connect(format!(
        "postgres://postgres:{SU_PW}@{host}:{port}/waddles_test"
    ))
    .await
    .unwrap();
    exec(
        &su,
        "CREATE TABLE tenants (id SERIAL PRIMARY KEY, slug TEXT);
         CREATE TABLE communities (id SERIAL PRIMARY KEY, tenant_id INTEGER REFERENCES tenants(id));
         CREATE TABLE community_members (
             id SERIAL PRIMARY KEY,
             community_id INTEGER REFERENCES communities(id) ON DELETE CASCADE,
             is_active BOOLEAN DEFAULT true, left_at TIMESTAMP, removed_at TIMESTAMP);
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
         INSERT INTO tenants (id, slug) VALUES (1, 't1');
         INSERT INTO communities (id, tenant_id) VALUES (10, 1);
         INSERT INTO app_catalog (app_id) VALUES ('waddles.core.test-reputation');",
    )
    .await;
    exec(
        &su,
        &format!(
            "CREATE ROLE waddles_bundle_reputation LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE \
             NOREPLICATION PASSWORD '{REP_PW}'"
        ),
    )
    .await;
    exec(&su, SHIPPED_DDL).await;
    let mut members = Vec::new();
    for _ in 0..=extra_members {
        let m = Uuid::new_v4();
        exec(
            &su,
            &format!("INSERT INTO community_members (community_id, user_uuid) VALUES (10, '{m}')"),
        )
        .await;
        members.push(m);
    }
    let member = members[0];

    let cfg = ConnectConfig {
        host,
        port,
        name: "waddles_test".to_string(),
        user: "waddles_bundle_reputation".to_string(),
    };
    let caps = build_caps(&cfg).await;
    World {
        _container: container,
        su,
        cfg,
        caps,
        member,
        members,
    }
}

/// A complete stage-side stack over the database `cfg` points at, sharing
/// NOTHING in memory with any other: a NEW connection pool, a NEW store, a NEW
/// membership snapshot (loaded by the real loader), a NEW gate and a NEW
/// in-memory quota ledger. Calling it again against the same database is
/// exactly a process restart or a second `svc_process` replica.
async fn build_caps(cfg: &ConnectConfig) -> StageCapabilities {
    let rep_conn = connect(cfg, REP_PW).await.unwrap();

    // Production membership path: snapshot populated by the real loader.
    let membership = Arc::new(SnapshotMembership::new());
    assert!(membership.replace_all(load_membership(&rep_conn, None).await.unwrap()));

    let snapshot = InMemoryGrantSnapshot::new();
    snapshot.set(
        GrantScopeKey {
            tenant_id: TENANT,
            community_id: COMMUNITY,
            app_id: APP_ID.to_string(),
            app_version: 1,
        },
        GrantSet {
            permission_snapshot_hash: "test".to_string(),
            grants: [
                grant("reputation.read", serde_json::json!({})),
                grant(
                    "reputation.community.write",
                    serde_json::json!({"delta_min": -5, "delta_max": 5}),
                ),
            ]
            .into_iter()
            .collect(),
        },
    );
    let gate = Arc::new(CapabilityGate::new(
        Arc::new(snapshot),
        membership,
        Arc::new(InMemoryQuotaLedger::new()),
        Arc::new(InMemoryInstancePolicySnapshot::new()),
    ));
    let egress = Arc::new(EgressGuard::new(
        Arc::new(ReqwestTransport::new()),
        EgressLimits {
            allow_private_hosts: false,
            rate_limit_rps: 10,
            rate_limit_burst: 20,
            timeout: std::time::Duration::from_secs(5),
            max_redirects: 3,
            max_response_bytes: 1_048_576,
            allowed_ports: vec![443],
            proxy_url: None,
        },
        HttpEgressCatalog::new(),
        prometheus::IntCounterVec::new(
            prometheus::Opts::new("e2e_egress_denied_total", "test"),
            &["app_id", "reason"],
        )
        .unwrap(),
        boxed(StaticFlag(true)),
    ));
    StageCapabilities::new(
        "acme".to_string(),
        Some("main".to_string()),
        APP_ID.to_string(),
        TENANT,
        COMMUNITY,
        1,
        egress,
        gate,
    )
    .with_reputation(ReputationWiring {
        store: Arc::new(PostgresReputationStore::new(rep_conn)) as Arc<dyn ReputationStore>,
        flag: Arc::new(StaticGate(true)),
    })
}

fn call(op: &str, args: serde_json::Value) -> HostCallBody {
    HostCallBody {
        app_id: APP_ID.to_string(),
        capability: CapabilityKind::Db,
        op: op.to_string(),
        args,
        call_id: 1,
    }
}

#[tokio::test]
async fn adjust_then_get_through_gate_and_real_store_with_ledger_row() {
    let w = world().await;
    let m = w.member.to_string();
    let out = w
        .caps
        .handle(call(
            "reputation.adjust",
            serde_json::json!({"user": m, "delta": 3, "reason": "game.win"}),
        ))
        .await
        .unwrap();
    assert_eq!(out, serde_json::json!({"balance": 3}));
    let out = w
        .caps
        .handle(call("reputation.get", serde_json::json!({"user": m})))
        .await
        .unwrap();
    assert_eq!(out, serde_json::json!({"balance": 3}));
    assert_eq!(
        scalar(&w.su, "SELECT COUNT(*) FROM bundle_reputation_adjustments").await,
        1
    );
    assert_eq!(
        scalar(
            &w.su,
            "SELECT balance FROM bundle_reputation_scores WHERE community_id = 10"
        )
        .await,
        3
    );
}

#[tokio::test]
async fn gate_denials_fail_loud_and_write_nothing() {
    let w = world().await;
    let m = w.member.to_string();
    // Out of the declared [-5, 5] bounds.
    let err = w
        .caps
        .handle(call(
            "reputation.adjust",
            serde_json::json!({"user": m, "delta": 6, "reason": "r"}),
        ))
        .await
        .unwrap_err();
    assert_eq!(err.code, "delta_out_of_bounds");
    // Not a member (never loaded into the snapshot).
    let err = w
        .caps
        .handle(call(
            "reputation.adjust",
            serde_json::json!({"user": Uuid::new_v4().to_string(), "delta": 1, "reason": "r"}),
        ))
        .await
        .unwrap_err();
    assert_eq!(err.code, "user_not_in_scope");
    assert_eq!(
        scalar(&w.su, "SELECT COUNT(*) FROM bundle_reputation_adjustments").await,
        0
    );
    assert_eq!(
        scalar(&w.su, "SELECT COUNT(*) FROM bundle_reputation_scores").await,
        0
    );
}

/// The membership snapshot is stale (member loaded at startup, removed since);
/// the gate's pre-filter still passes, but the store's in-transaction live
/// re-check rejects -- a departed member can never be written.
#[tokio::test]
async fn a_stale_snapshot_cannot_authorize_a_write_for_a_departed_member() {
    let w = world().await;
    exec(
        &w.su,
        "UPDATE community_members SET removed_at = NOW() WHERE community_id = 10",
    )
    .await;
    let err = w
        .caps
        .handle(call(
            "reputation.adjust",
            serde_json::json!({"user": w.member.to_string(), "delta": 1, "reason": "r"}),
        ))
        .await
        .unwrap_err();
    assert_eq!(err.code, "not_a_member");
    assert_eq!(
        scalar(&w.su, "SELECT COUNT(*) FROM bundle_reputation_adjustments").await,
        0
    );
}

/// Catalog cap is 5/user/day: 3 + 3 > 5. The in-memory gate quota trips
/// first here (`quota_exceeded`); the durable store cap is the backstop
/// proven in `core/bundle_host_reputation`'s integration tests.
#[tokio::test]
async fn the_daily_cap_is_enforced_across_calls() {
    let w = world().await;
    let m = w.member.to_string();
    w.caps
        .handle(call(
            "reputation.adjust",
            serde_json::json!({"user": m, "delta": 3, "reason": "a"}),
        ))
        .await
        .unwrap();
    let err = w
        .caps
        .handle(call(
            "reputation.adjust",
            serde_json::json!({"user": m, "delta": 3, "reason": "b"}),
        ))
        .await
        .unwrap_err();
    assert!(
        err.code == "quota_exceeded" || err.code == "daily_cap_exceeded",
        "{}",
        err.code
    );
    assert_eq!(
        scalar(
            &w.su,
            "SELECT COALESCE(SUM(delta), 0)::BIGINT FROM bundle_reputation_adjustments"
        )
        .await,
        3
    );
}

/// regression: pr-741 review -- a zero delta passed the gate's bounds and
/// quotas (a 0-amount consumption) and reached the store, which appended a
/// ledger row per call. It is now an `invalid_args` rejection before the gate,
/// so it neither writes nor consumes any quota.
#[tokio::test]
async fn a_zero_delta_is_invalid_args_writes_nothing_and_consumes_no_quota() {
    let w = world().await;
    let m = w.member.to_string();
    for _ in 0..50 {
        let err = w
            .caps
            .handle(call(
                "reputation.adjust",
                serde_json::json!({"user": m, "delta": 0, "reason": "noop"}),
            ))
            .await
            .unwrap_err();
        assert_eq!(err.code, "invalid_args");
    }
    assert_eq!(
        scalar(&w.su, "SELECT COUNT(*) FROM bundle_reputation_adjustments").await,
        0
    );
    assert_eq!(
        scalar(&w.su, "SELECT COUNT(*) FROM bundle_reputation_scores").await,
        0
    );
    // The full per-user budget (5/day) is still intact after 50 zero calls.
    let out = w
        .caps
        .handle(call(
            "reputation.adjust",
            serde_json::json!({"user": m, "delta": 5, "reason": "real"}),
        ))
        .await
        .unwrap();
    assert_eq!(out, serde_json::json!({"balance": 5}));
}

/// regression: pr-741 review -- the per-scope daily cap (catalog: 50 for
/// `reputation.community.write`, the Dangerous-grant blast-radius bound) used
/// to live only in the gate's in-memory ledger, so a restart or another replica
/// reset it. Spend the whole budget through one stack, then build a FRESH stack
/// (new gate + empty in-memory ledger + new pool, i.e. a restart / second
/// replica): it must still refuse, and only the durable store can be why.
#[tokio::test]
async fn the_per_scope_cap_survives_a_gate_restart_and_a_second_replica() {
    let w = world_with_members(10).await;
    assert_eq!(w.members.len(), 11);

    // 10 distinct users x 5 (the per-call and per-user ceiling) = 50, the
    // whole per-scope budget. Distinct users so only the SCOPE cap can bind.
    for m in &w.members[..10] {
        let out = w
            .caps
            .handle(call(
                "reputation.adjust",
                serde_json::json!({"user": m.to_string(), "delta": 5, "reason": "burn"}),
            ))
            .await
            .unwrap();
        assert_eq!(out, serde_json::json!({"balance": 5}));
    }
    let sum_sql = "SELECT COALESCE(SUM(ABS(delta)), 0)::BIGINT FROM bundle_reputation_adjustments";
    assert_eq!(scalar(&w.su, sum_sql).await, 50);

    // An 11th user with a pristine per-user budget: the original stack's
    // in-memory scope ledger refuses (control) ...
    let fresh_user = w.members[10].to_string();
    let adjust_one = serde_json::json!({"user": fresh_user, "delta": 1, "reason": "over"});
    let err = w
        .caps
        .handle(call("reputation.adjust", adjust_one.clone()))
        .await
        .unwrap_err();
    assert_eq!(err.code, "quota_exceeded");

    // ... and so do a restarted stack and a second replica, whose gates hold NO
    // in-memory state at all -- the refusal can only come from the store.
    for label in ["restarted", "second replica"] {
        let stack = build_caps(&w.cfg).await;
        let err = stack
            .handle(call("reputation.adjust", adjust_one.clone()))
            .await
            .unwrap_err();
        assert_eq!(err.code, "quota_exceeded", "{label}");
    }
    // Nothing was written by any refused call.
    assert_eq!(scalar(&w.su, sum_sql).await, 50);
    assert_eq!(
        scalar(&w.su, "SELECT COUNT(*) FROM bundle_reputation_adjustments").await,
        10
    );

    // Reads are unaffected by the write budget.
    let stack = build_caps(&w.cfg).await;
    let out = stack
        .handle(call(
            "reputation.get",
            serde_json::json!({"user": fresh_user}),
        ))
        .await
        .unwrap();
    assert_eq!(out, serde_json::json!({"balance": 0}));
}
