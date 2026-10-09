//! Issue #714: the `economy` capability's real path joined end to end on the
//! stage side -- `CapabilityHandler::handle` (host-call decode) -> the REAL
//! `CapabilityGate` (grants, declared `max_bet`/`max_amount`, the economy's own
//! quotas, production `SnapshotMembership` populated from the DB by the REAL
//! `load_membership`) -> the REAL `PostgresEconomyStore` -> a real Postgres
//! container running the exact shipped DDL
//! (`scripts/db/bundle_economy_store.sql`). Nothing in the chain is a test
//! double. (The executor half -- real wasm calling the import and emitting
//! this exact wire shape -- is `core/bundle_executor/tests/
//! stage_next_economy.rs`.)
//!
//! Requires Docker via `testcontainers`.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;

use bundle_capability_gate::{
    CapabilityGate, GrantScopeKey, GrantSet, GrantedPermission, InMemoryGrantSnapshot,
    InMemoryInstancePolicySnapshot, InMemoryQuotaLedger, SnapshotMembership,
};
use bundle_host_economy::{
    connect, load_membership, ConnectConfig, EconomyStore, PostgresEconomyStore,
};
use bundle_host_http::egress::{boxed, EgressGuard, EgressLimits, ReqwestTransport, StaticFlag};
use penguin_bundle_host::wire::{CapabilityKind, HostCallBody};
use sea_orm::{ConnectionTrait, Database, DatabaseConnection, Statement};
use svc_process::capabilities::{
    CapabilityHandler, EconomyWiring, HttpEgressCatalog, StageCapabilities,
};
use svc_process::license::StaticGate;
use testcontainers::core::logs::LogSource;
use testcontainers::core::wait::LogWaitStrategy;
use testcontainers::core::{ContainerPort, WaitFor};
use testcontainers::runners::AsyncRunner;
use testcontainers::{ContainerAsync, GenericImage, ImageExt};
use uuid::Uuid;

const SU_PW: &str = "postgres_test_superuser_pw";
const ECO_PW: &str = "waddles_economy_runtime_test_pw";
const APP_ID: &str = "waddles.core.test-economy";
const TENANT: i32 = 1;
const COMMUNITY: i32 = 10;
const SHIPPED_DDL: &str = include_str!("../../../scripts/db/bundle_economy_store.sql");

struct World {
    _container: ContainerAsync<GenericImage>,
    su: DatabaseConnection,
    caps: Arc<StageCapabilities>,
    alice: Uuid,
    bob: Uuid,
    stranger: Uuid,
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
         INSERT INTO tenants (id, slug) VALUES (1, 't1');
         INSERT INTO communities (id, tenant_id) VALUES (10, 1);",
    )
    .await;
    exec(
        &su,
        &format!(
            "CREATE ROLE waddles_economy_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE \
             NOREPLICATION PASSWORD '{ECO_PW}'"
        ),
    )
    .await;
    exec(&su, SHIPPED_DDL).await;
    let (alice, bob, stranger) = (Uuid::new_v4(), Uuid::new_v4(), Uuid::new_v4());
    exec(
        &su,
        &format!(
            "INSERT INTO community_members (community_id, user_uuid) VALUES (10, '{alice}'), (10, '{bob}')"
        ),
    )
    .await;
    // Privileged hub-side funding (the runtime role cannot mint).
    exec(
        &su,
        &format!(
            "INSERT INTO economy_balances (tenant_id, community_id, user_uuid, balance) \
             VALUES (1, 10, '{alice}', 500)"
        ),
    )
    .await;

    let eco_conn = connect(
        &ConnectConfig {
            host,
            port,
            name: "waddles_test".to_string(),
            user: "waddles_economy_runtime".to_string(),
        },
        ECO_PW,
    )
    .await
    .unwrap();

    // Production membership path: snapshot populated by the real loader.
    let membership = Arc::new(SnapshotMembership::new());
    assert!(membership.replace_all(load_membership(&eco_conn, None).await.unwrap()));

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
                grant("economy.read", serde_json::json!({})),
                grant("economy.wager", serde_json::json!({"max_bet": 50})),
                grant("economy.transfer", serde_json::json!({"max_amount": 200})),
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
            prometheus::Opts::new("e2e_eco_egress_denied_total", "test"),
            &["app_id", "reason"],
        )
        .unwrap(),
        boxed(StaticFlag(true)),
    ));
    let caps = StageCapabilities::new(
        "acme".to_string(),
        Some("main".to_string()),
        APP_ID.to_string(),
        TENANT,
        COMMUNITY,
        1,
        egress,
        gate,
    )
    .with_economy(EconomyWiring {
        store: Arc::new(PostgresEconomyStore::new(eco_conn)) as Arc<dyn EconomyStore>,
        flag: Arc::new(StaticGate(true)),
    });
    World {
        _container: container,
        su,
        caps: Arc::new(caps),
        alice,
        bob,
        stranger,
    }
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
async fn wager_balance_max_bet_and_leaderboard_through_gate_and_real_store() {
    let w = world().await;
    let a = w.alice.to_string();
    // win +15, then loss -40
    let out = w
        .caps
        .handle(call(
            "economy.wager",
            serde_json::json!({"user": a, "stake": 10, "payout": 25}),
        ))
        .await
        .unwrap();
    assert_eq!(out, serde_json::json!({"balance": 515}));
    let out = w
        .caps
        .handle(call(
            "economy.wager",
            serde_json::json!({"user": a, "stake": 40, "payout": 0}),
        ))
        .await
        .unwrap();
    assert_eq!(out, serde_json::json!({"balance": 475}));
    let out = w
        .caps
        .handle(call("economy.balance", serde_json::json!({"user": a})))
        .await
        .unwrap();
    assert_eq!(out, serde_json::json!({"balance": 475}));
    // min(declared max_bet 50, balance 475)
    let out = w
        .caps
        .handle(call("economy.max_bet", serde_json::json!({"user": a})))
        .await
        .unwrap();
    assert_eq!(out, serde_json::json!({"max_bet": 50}));
    let out = w
        .caps
        .handle(call("economy.leaderboard", serde_json::json!({"limit": 5})))
        .await
        .unwrap();
    assert_eq!(
        out,
        serde_json::json!({"entries": [{"user": a, "balance": 475}]})
    );
    assert_eq!(
        scalar(&w.su, "SELECT COUNT(*) FROM economy_ledger").await,
        2
    );
}

#[tokio::test]
async fn transfer_through_gate_and_real_store_conserves_currency() {
    let w = world().await;
    let out = w
        .caps
        .handle(call(
            "economy.transfer",
            serde_json::json!({"from": w.alice.to_string(), "to": w.bob.to_string(), "amount": 120}),
        ))
        .await
        .unwrap();
    assert_eq!(out, serde_json::json!({}));
    let out = w
        .caps
        .handle(call(
            "economy.balance",
            serde_json::json!({"user": w.bob.to_string()}),
        ))
        .await
        .unwrap();
    assert_eq!(out, serde_json::json!({"balance": 120}));
    assert_eq!(
        scalar(&w.su, "SELECT SUM(balance)::BIGINT FROM economy_balances").await,
        500
    );
    assert_eq!(
        scalar(&w.su, "SELECT COUNT(*) FROM economy_ledger").await,
        2
    );
}

#[tokio::test]
async fn gate_and_store_refusals_fail_loud_with_their_codes_and_write_nothing() {
    let w = world().await;
    let a = w.alice.to_string();
    let b = w.bob.to_string();
    let code = |r: Result<serde_json::Value, _>| -> (String, String) {
        let e: penguin_bundle_host::wire::HostResultError = r.unwrap_err();
        (e.code, e.message)
    };
    // Gate: stake above the DECLARED max_bet of 50.
    assert_eq!(
        code(
            w.caps
                .handle(call(
                    "economy.wager",
                    serde_json::json!({"user": a, "stake": 51, "payout": 0})
                ))
                .await
        )
        .0,
        "amount_out_of_bounds"
    );
    // Gate: a non-member (never loaded into the snapshot).
    assert_eq!(
        code(
            w.caps
                .handle(call(
                    "economy.wager",
                    serde_json::json!({"user": w.stranger.to_string(), "stake": 1, "payout": 0})
                ))
                .await
        )
        .0,
        "user_not_in_scope"
    );
    // Store: bob holds nothing -> insufficient funds, balance in the message.
    assert_eq!(
        code(
            w.caps
                .handle(call(
                    "economy.wager",
                    serde_json::json!({"user": b, "stake": 5, "payout": 0})
                ))
                .await
        ),
        ("insufficient_funds".to_string(), "0".to_string())
    );
    // Store: a payout above stake * 100.
    assert_eq!(
        code(
            w.caps
                .handle(call(
                    "economy.wager",
                    serde_json::json!({"user": a, "stake": 10, "payout": 1_001})
                ))
                .await
        ),
        ("over_cap".to_string(), "1000".to_string())
    );
    assert_eq!(
        scalar(&w.su, "SELECT COUNT(*) FROM economy_ledger").await,
        0
    );
    assert_eq!(
        scalar(&w.su, "SELECT SUM(balance)::BIGINT FROM economy_balances").await,
        500
    );
}

/// The membership snapshot is stale (members loaded at startup, one removed
/// since); the gate's pre-filter still passes, but the store's live check in
/// the write rejects -- a departed member can never be debited or credited.
#[tokio::test]
async fn a_stale_snapshot_cannot_authorize_a_write_for_a_departed_member() {
    let w = world().await;
    exec(
        &w.su,
        &format!(
            "UPDATE community_members SET removed_at = NOW() WHERE user_uuid = '{}'",
            w.bob
        ),
    )
    .await;
    let err = w
        .caps
        .handle(call(
            "economy.transfer",
            serde_json::json!({"from": w.alice.to_string(), "to": w.bob.to_string(), "amount": 5}),
        ))
        .await
        .unwrap_err();
    assert_eq!(err.code, "not_a_member");
    assert_eq!(
        scalar(&w.su, "SELECT COUNT(*) FROM economy_ledger").await,
        0
    );
}

/// 80 concurrent wagers through the whole stack against a balance of 500:
/// stake 50 each can succeed exactly 10 times; the balance can never go
/// below zero and the ledger and balance agree.
#[tokio::test(flavor = "multi_thread", worker_threads = 8)]
async fn concurrent_wagers_through_the_whole_stack_never_overdraw() {
    let w = world().await;
    let mut tasks = Vec::new();
    for _ in 0..80 {
        let caps = Arc::clone(&w.caps);
        let user = w.alice.to_string();
        tasks.push(tokio::spawn(async move {
            caps.handle(call(
                "economy.wager",
                serde_json::json!({"user": user, "stake": 50, "payout": 0}),
            ))
            .await
        }));
    }
    let (mut ok, mut insufficient) = (0, 0);
    for t in tasks {
        match t.await.unwrap() {
            Ok(_) => ok += 1,
            Err(e) if e.code == "insufficient_funds" => insufficient += 1,
            // The gate's in-memory per-user daily stake ceiling (10_000)
            // is not reachable here (80 x 50 = 4_000).
            Err(e) => panic!("unexpected refusal: {} {}", e.code, e.message),
        }
    }
    assert_eq!((ok, insufficient), (10, 70));
    assert_eq!(
        scalar(
            &w.su,
            &format!(
                "SELECT balance FROM economy_balances WHERE user_uuid = '{}'",
                w.alice
            )
        )
        .await,
        0
    );
    assert_eq!(
        scalar(&w.su, "SELECT COUNT(*) FROM economy_ledger").await,
        10
    );
}

/// The daily aggregates are DURABLE: the world's gate has a fresh, empty
/// in-memory quota ledger (as after a restart or on a second replica), yet the
/// store still refuses once the ledger says the budget is spent. The ledger is
/// seeded with already-applied wagers by the privileged writer.
#[tokio::test]
async fn the_durable_daily_caps_refuse_even_with_an_empty_in_memory_gate_ledger() {
    let w = world().await;
    let a = w.alice.to_string();
    // alice already staked 9_990 of her 10_000 per-user daily budget.
    exec(
        &w.su,
        &format!(
            "INSERT INTO economy_ledger (tenant_id, community_id, app_id, user_uuid, kind, \
                                         delta, stake, payout, balance_after) \
             VALUES (1, 10, '{APP_ID}', '{}', 'wager', -9990, 9990, 0, 500)",
            w.alice
        ),
    )
    .await;
    let err = w
        .caps
        .handle(call(
            "economy.wager",
            serde_json::json!({"user": a, "stake": 20, "payout": 0}),
        ))
        .await
        .unwrap_err();
    assert_eq!(err.code, "quota_exceeded", "per-user: {}", err.message);
    // Exactly the remaining budget is allowed.
    w.caps
        .handle(call(
            "economy.wager",
            serde_json::json!({"user": a, "stake": 10, "payout": 0}),
        ))
        .await
        .unwrap();

    // Per-scope: other members' applied wagers fill the community's 250_000.
    exec(
        &w.su,
        &format!(
            "INSERT INTO economy_ledger (tenant_id, community_id, app_id, user_uuid, kind, \
                                         delta, stake, payout, balance_after) \
             VALUES (1, 10, '{APP_ID}', gen_random_uuid(), 'wager', -240000, 240000, 0, 1)"
        ),
    )
    .await;
    // scope total is now 9_990 + 10 + 240_000 = 250_000: nothing more fits.
    let err = w
        .caps
        .handle(call(
            "economy.wager",
            serde_json::json!({"user": w.bob.to_string(), "stake": 1, "payout": 0}),
        ))
        .await
        .unwrap_err();
    // bob is unfunded; the quota refusal (checked first, under the scope lock)
    // is what the bundle sees.
    assert_eq!(err.code, "quota_exceeded", "per-scope: {}", err.message);
}
