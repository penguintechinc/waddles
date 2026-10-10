//! Issue #714: the `economy` capability's real path joined end to end on the
//! stage side -- `CapabilityHandler::handle` (host-call decode) -> the REAL
//! `CapabilityGate` (grants, declared `max_bet`/`max_amount`, the economy's own
//! quotas, production `SnapshotMembership` populated from the DB by the REAL
//! `load_membership`) -> the REAL `PostgresEconomyStore` -> a real Postgres
//! container running the exact shipped DDL
//! (`scripts/db/bundle_economy_store.sql` + `bundle_economy_idempotency.sql`).
//! The one stand-in is the actor directory (platform account -> community
//! user): the invocation's money movers are bound to that actor, and the REAL
//! directory over the REAL `community_member_identities` view is proven in
//! `tests/identity_pg_e2e.rs`, which also joins identity and economy. (The
//! executor half -- real wasm calling the import and emitting this exact wire
//! shape -- is `core/bundle_executor/tests/stage_next_economy.rs`.)
//!
//! Requires Docker via `testcontainers`.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;

use bundle_capability_gate::{
    CapabilityGate, GrantScopeKey, GrantSet, GrantedPermission, InMemoryGrantSnapshot,
    InMemoryInstancePolicySnapshot, InMemoryQuotaLedger, MembershipCheck, SnapshotMembership,
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
use svc_process::identity::{
    BoxFuture, IdentityError, IdentityScope, IdentityWiring, InvocationIdentity, MemberDirectory,
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
const IDEMPOTENCY_DDL: &str = include_str!("../../../scripts/db/bundle_economy_idempotency.sql");
/// The spine events the two actors' invocations handle (UUID v4s).
const ALICE_EVENT: &str = "3fa85f64-5717-4562-b3fc-2c963f66afa6";
const BOB_EVENT: &str = "6f1f5a2e-9d0b-4c53-8a7e-1b2c3d4e5f60";

/// Stand-in actor directory: platform account `alice`/`bob` -> the member.
struct Directory {
    alice: Uuid,
    bob: Uuid,
}

impl MemberDirectory for Directory {
    fn member_by_platform_id<'a>(
        &'a self,
        _scope: IdentityScope,
        _platform: &'a str,
        platform_user_id: &'a str,
    ) -> BoxFuture<'a, Result<Uuid, IdentityError>> {
        Box::pin(async move {
            match platform_user_id {
                "alice" => Ok(self.alice),
                "bob" => Ok(self.bob),
                _ => Err(IdentityError::NotAMember),
            }
        })
    }

    fn confirm_member<'a>(
        &'a self,
        _scope: IdentityScope,
        user: Uuid,
    ) -> BoxFuture<'a, Result<Uuid, IdentityError>> {
        Box::pin(async move { Ok(user) })
    }
}

struct World {
    _container: ContainerAsync<GenericImage>,
    su: DatabaseConnection,
    /// The stage for an invocation TRIGGERED BY alice (the default actor).
    caps: Arc<StageCapabilities>,
    /// The stage for an invocation triggered by bob.
    bob_caps: Arc<StageCapabilities>,
    eco_conn: DatabaseConnection,
    membership: Arc<SnapshotMembership>,
    directory: Arc<Directory>,
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

/// One stage per ACTOR: its own gate (in-memory quota ledger, as after a
/// restart or on another replica) and its own invocation (the event it handles
/// and the platform account that sent it). Building a second stage for the SAME
/// event is exactly a redelivery of that event.
fn stage(
    eco_conn: &DatabaseConnection,
    membership: &Arc<SnapshotMembership>,
    directory: &Arc<Directory>,
    account: &str,
    event_id: &str,
) -> Arc<StageCapabilities> {
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
        Arc::clone(membership) as Arc<dyn MembershipCheck>,
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
    Arc::new(
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
        .with_economy(EconomyWiring {
            store: Arc::new(PostgresEconomyStore::new(eco_conn.clone())) as Arc<dyn EconomyStore>,
            flag: Arc::new(StaticGate(true)),
        })
        .with_identity(
            IdentityWiring {
                directory: Arc::clone(directory) as Arc<dyn MemberDirectory>,
                handles: None,
                flag: Arc::new(StaticGate(true)),
            },
            Arc::new(
                InvocationIdentity::new("twitch", Some(account.to_string()), vec![])
                    .with_event_id(event_id),
            ),
        ),
    )
}

impl World {
    /// A fresh stage for `account`'s invocation of `event_id` (a redelivery
    /// when the event was already handled).
    fn stage(&self, account: &str, event_id: &str) -> Arc<StageCapabilities> {
        stage(
            &self.eco_conn,
            &self.membership,
            &self.directory,
            account,
            event_id,
        )
    }
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
    exec(&su, IDEMPOTENCY_DDL).await;
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

    let directory = Arc::new(Directory { alice, bob });
    let caps = stage(&eco_conn, &membership, &directory, "alice", ALICE_EVENT);
    let bob_caps = stage(&eco_conn, &membership, &directory, "bob", BOB_EVENT);
    World {
        _container: container,
        su,
        caps,
        bob_caps,
        eco_conn,
        membership,
        directory,
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
    // Store: bob (the actor of his own invocation) holds nothing ->
    // insufficient funds, balance in the message.
    assert_eq!(
        code(
            w.bob_caps
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
        .bob_caps
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

async fn fund(w: &World, user: Uuid, amount: i64) {
    exec(
        &w.su,
        &format!(
            "INSERT INTO economy_balances (tenant_id, community_id, user_uuid, balance) \
             VALUES (1, 10, '{user}', {amount}) \
             ON CONFLICT (tenant_id, community_id, user_uuid) DO UPDATE SET balance = {amount}"
        ),
    )
    .await;
}

async fn balance_of(w: &World, user: Uuid) -> i64 {
    scalar(
        &w.su,
        &format!("SELECT balance FROM economy_balances WHERE user_uuid = '{user}'"),
    )
    .await
}

/// Regression (#751 review, theft) through gate + binding + real store: the
/// invocation alice triggered names bob (a funded member) as the payer. The
/// funds do not move, nothing is written, and bob's OWN invocation still can.
#[tokio::test]
async fn a_bundle_cannot_move_a_victims_funds_through_the_whole_stack() {
    let w = world().await;
    fund(&w, w.bob, 300).await;
    let (a, b) = (w.alice.to_string(), w.bob.to_string());

    for (op, args) in [
        (
            "economy.transfer",
            serde_json::json!({"from": b, "to": a, "amount": 100}),
        ),
        (
            "economy.wager",
            serde_json::json!({"user": b, "stake": 50, "payout": 0}),
        ),
    ] {
        let err = w.caps.handle(call(op, args)).await.unwrap_err();
        assert_eq!(err.code, "actor_mismatch", "{op}: {}", err.message);
    }
    assert_eq!(
        balance_of(&w, w.bob).await,
        300,
        "the victim was not debited"
    );
    assert_eq!(
        balance_of(&w, w.alice).await,
        500,
        "the thief was not credited"
    );
    assert_eq!(
        scalar(&w.su, "SELECT COUNT(*) FROM economy_ledger").await,
        0
    );

    // Bob's own invocation moves bob's own funds.
    w.bob_caps
        .handle(call(
            "economy.transfer",
            serde_json::json!({"from": b, "to": a, "amount": 100}),
        ))
        .await
        .unwrap();
    assert_eq!(balance_of(&w, w.bob).await, 200);
    assert_eq!(balance_of(&w, w.alice).await, 600);
}

/// Regression (#751 review, double-spend) through the whole stack: the spine
/// redelivers an event (a fresh stage for the SAME event id, as after a crash)
/// and the bundle repeats its calls. The credit lands once.
#[tokio::test]
async fn a_redelivered_event_credits_once_through_the_whole_stack() {
    let w = world().await;
    let a = w.alice.to_string();
    let b = w.bob.to_string();
    let wager = serde_json::json!({"user": a, "stake": 10, "payout": 25});
    let pay = serde_json::json!({"from": a, "to": b, "amount": 40});

    let first = w.stage("alice", ALICE_EVENT);
    assert_eq!(
        first
            .handle(call("economy.wager", wager.clone()))
            .await
            .unwrap(),
        serde_json::json!({"balance": 515})
    );
    first
        .handle(call("economy.transfer", pay.clone()))
        .await
        .unwrap();
    assert_eq!(balance_of(&w, w.alice).await, 475);

    // Redelivery: same event, fresh invocation (and a fresh in-memory gate).
    for _ in 0..3 {
        let redelivered = w.stage("alice", ALICE_EVENT);
        assert_eq!(
            redelivered
                .handle(call("economy.wager", wager.clone()))
                .await
                .unwrap(),
            serde_json::json!({"balance": 515}),
            "the replay answers with the ORIGINAL result"
        );
        redelivered
            .handle(call("economy.transfer", pay.clone()))
            .await
            .unwrap();
    }
    assert_eq!(
        balance_of(&w, w.alice).await,
        475,
        "credited and debited once"
    );
    assert_eq!(balance_of(&w, w.bob).await, 40);
    assert_eq!(
        scalar(&w.su, "SELECT COUNT(*) FROM economy_ledger").await,
        3,
        "one wager row + the transfer's out/in pair"
    );
    assert_eq!(
        scalar(&w.su, "SELECT SUM(balance)::BIGINT FROM economy_balances").await,
        515,
        "currency is conserved apart from the wager's one +15"
    );

    // A replay that re-rolls its payout is refused, not applied.
    let reroll = serde_json::json!({"user": a, "stake": 10, "payout": 90});
    let err = w
        .stage("alice", ALICE_EVENT)
        .handle(call("economy.wager", reroll))
        .await
        .unwrap_err();
    assert_eq!(err.code, "idempotency_conflict");
    assert_eq!(balance_of(&w, w.alice).await, 475);

    // A different event is a different operation.
    w.stage("alice", BOB_EVENT)
        .handle(call("economy.wager", wager))
        .await
        .unwrap();
    assert_eq!(balance_of(&w, w.alice).await, 490);
}

/// Regression (#751 review, mint cap) through the whole stack: the payout is
/// what a wager mints. The gate meters it in memory; the STORE meters it
/// durably, so a fresh (empty) in-memory gate -- a restart, a second replica --
/// cannot reset the mint budget.
#[tokio::test]
async fn the_mint_cap_is_on_the_payout_and_durable_across_gate_resets() {
    let w = world().await;
    let a = w.alice.to_string();
    // Two stake-50 wagers paying 100x = 10_000 = the per-user daily mint
    // ceiling, on a combined stake of 100.
    for event in [ALICE_EVENT, "9d3c0f55-2b1e-4f7a-9c61-0a8e5b7d2c11"] {
        w.stage("alice", event)
            .handle(call(
                "economy.wager",
                serde_json::json!({"user": a, "stake": 50, "payout": 5_000}),
            ))
            .await
            .unwrap();
    }
    assert_eq!(
        scalar(&w.su, "SELECT SUM(payout)::BIGINT FROM economy_ledger").await,
        10_000
    );
    // This stage's in-memory gate has never seen those wagers; the durable
    // store window has: a payout of 1 is over the mint cap.
    let err = w
        .stage("alice", "0b6a1c2d-3e4f-4a5b-8c6d-7e8f9a0b1c2d")
        .handle(call(
            "economy.wager",
            serde_json::json!({"user": a, "stake": 1, "payout": 1}),
        ))
        .await
        .unwrap_err();
    assert_eq!(err.code, "quota_exceeded", "{}", err.message);
    // A losing wager mints nothing.
    w.stage("alice", "5c4b3a29-1807-4f6e-9d5c-4b3a29180716")
        .handle(call(
            "economy.wager",
            serde_json::json!({"user": a, "stake": 50, "payout": 0}),
        ))
        .await
        .unwrap();
}
