//! `economy.*` host-capability tests (issue #714): the REAL
//! [`CapabilityGate`] (grants, declared `max_bet`/`max_amount`, the economy's
//! own amount quotas, membership pre-filter for every named user), the REAL
//! `handle` dispatch and the REAL actor binding / idempotency-key derivation
//! ([`InvocationIdentity`], [`identity::resolve_actor`]) run against a
//! recording store double and a scripted member directory; the store's own SQL
//! is proven against a real Postgres in
//! `core/bundle_host_economy/tests/postgres_integration.rs` and the two joined
//! in `tests/economy_pg_e2e.rs` / `tests/identity_pg_e2e.rs`.
//!
//! The #751 money-safety regressions live at the bottom: theft (a bundle
//! naming another member as the payer), the mint cap (payout, not stake) and
//! double-spend (replay / retry idempotency).

use super::*;
use crate::identity::{
    BoxFuture as IdentityFuture, IdentityWiring, InvocationIdentity, MemberDirectory,
};
use crate::license::test_support::FixedGate;
use bundle_capability_gate::{
    GrantScopeKey, GrantSet, GrantedPermission, InMemoryGrantSnapshot,
    InMemoryInstancePolicySnapshot, InMemoryMembership, InMemoryQuotaLedger,
};
use bundle_host_economy::{EconomyCaps, IdempotencyKey, LeaderboardEntry};
use bundle_host_http::egress::{ReqwestTransport, StaticFlag};
use std::sync::Mutex;
use uuid::Uuid;

const TENANT_ID: i32 = 7;
const COMMUNITY_ID: i32 = 3;
const APP_ID: &str = "waddles.core.test-economy";
const VERSION: i64 = 1;
/// The spine event this invocation handles (a UUID v4, as the hop MAC covers).
const EVENT_ID: &str = "3fa85f64-5717-4562-b3fc-2c963f66afa6";
/// A different spine event.
const OTHER_EVENT_ID: &str = "6f1f5a2e-9d0b-4c53-8a7e-1b2c3d4e5f60";
/// The platform account id of the account that triggered the invocation.
const ACTOR_ACCOUNT: &str = "1001";

/// One recorded store call: `(op, scope, users, numbers)`.
type Recorded = (String, EconomyScope, Vec<Uuid>, Vec<i64>);

/// Records every store call; answers with canned results.
#[derive(Default)]
struct RecordingStore {
    calls: Mutex<Vec<Recorded>>,
    /// The idempotency key of every money-moving call, in call order.
    keys: Mutex<Vec<String>>,
    next_i64: Mutex<Option<Result<i64, EconomyError>>>,
    next_unit: Mutex<Option<Result<(), EconomyError>>>,
    next_board: Mutex<Option<Result<Vec<LeaderboardEntry>, EconomyError>>>,
}

impl RecordingStore {
    fn answer(&self, r: Result<i64, EconomyError>) {
        *self.next_i64.lock().unwrap() = Some(r);
    }
    fn call_count(&self) -> usize {
        self.calls.lock().unwrap().len()
    }
    fn keys(&self) -> Vec<String> {
        self.keys.lock().unwrap().clone()
    }
    /// Scripts the next transfer's answer.
    fn answer_unit(&self, r: Result<(), EconomyError>) {
        *self.next_unit.lock().unwrap() = Some(r);
    }
    fn record(&self, op: &str, scope: &EconomyScope, users: Vec<Uuid>, nums: Vec<i64>) {
        self.calls
            .lock()
            .unwrap()
            .push((op.to_string(), scope.clone(), users, nums));
    }
    fn take_i64(&self) -> Result<i64, EconomyError> {
        self.next_i64.lock().unwrap().take().unwrap_or(Ok(0))
    }
}

impl EconomyStore for RecordingStore {
    fn balance<'a>(
        &'a self,
        scope: &'a EconomyScope,
        user: Uuid,
    ) -> bundle_host_economy::BoxFuture<'a, Result<i64, EconomyError>> {
        Box::pin(async move {
            self.record("balance", scope, vec![user], vec![]);
            self.take_i64()
        })
    }
    fn max_bet<'a>(
        &'a self,
        scope: &'a EconomyScope,
        user: Uuid,
        cap: i64,
    ) -> bundle_host_economy::BoxFuture<'a, Result<i64, EconomyError>> {
        Box::pin(async move {
            self.record("max_bet", scope, vec![user], vec![cap]);
            self.take_i64()
        })
    }
    fn wager<'a>(
        &'a self,
        scope: &'a EconomyScope,
        user: Uuid,
        stake: i64,
        payout: i64,
        max_bet: i64,
        caps: EconomyCaps,
        key: &'a IdempotencyKey,
    ) -> bundle_host_economy::BoxFuture<'a, Result<i64, EconomyError>> {
        Box::pin(async move {
            self.keys.lock().unwrap().push(key.to_string());
            self.record(
                "wager",
                scope,
                vec![user],
                vec![
                    stake,
                    payout,
                    max_bet,
                    caps.per_user_daily_max,
                    caps.per_scope_daily_max,
                ],
            );
            self.take_i64()
        })
    }
    fn transfer<'a>(
        &'a self,
        scope: &'a EconomyScope,
        from: Uuid,
        to: Uuid,
        amount: i64,
        max_amount: i64,
        caps: EconomyCaps,
        key: &'a IdempotencyKey,
    ) -> bundle_host_economy::BoxFuture<'a, Result<(), EconomyError>> {
        Box::pin(async move {
            self.keys.lock().unwrap().push(key.to_string());
            self.record(
                "transfer",
                scope,
                vec![from, to],
                vec![
                    amount,
                    max_amount,
                    caps.per_user_daily_max,
                    caps.per_scope_daily_max,
                ],
            );
            self.next_unit.lock().unwrap().take().unwrap_or(Ok(()))
        })
    }
    fn leaderboard<'a>(
        &'a self,
        scope: &'a EconomyScope,
        limit: u32,
    ) -> bundle_host_economy::BoxFuture<'a, Result<Vec<LeaderboardEntry>, EconomyError>> {
        Box::pin(async move {
            self.record("leaderboard", scope, vec![], vec![i64::from(limit)]);
            self.next_board
                .lock()
                .unwrap()
                .take()
                .unwrap_or_else(|| Ok(vec![]))
        })
    }
}

/// Scripted membership directory: platform account id -> the community user
/// that account is. Only the ACTOR path (`resolve_actor`) is exercised here.
#[derive(Default)]
struct ActorDirectory {
    by_account: Mutex<std::collections::HashMap<String, Result<Uuid, IdentityError>>>,
    looked_up: Mutex<usize>,
}

impl ActorDirectory {
    fn set(&self, account: &str, answer: Result<Uuid, IdentityError>) {
        self.by_account
            .lock()
            .unwrap()
            .insert(account.to_string(), answer);
    }

    /// How many platform-account lookups the directory served.
    fn lookups(&self) -> usize {
        *self.looked_up.lock().unwrap()
    }
}

impl MemberDirectory for ActorDirectory {
    fn member_by_platform_id<'a>(
        &'a self,
        _scope: IdentityScope,
        _platform: &'a str,
        platform_user_id: &'a str,
    ) -> IdentityFuture<'a, Result<Uuid, IdentityError>> {
        Box::pin(async move {
            *self.looked_up.lock().unwrap() += 1;
            self.by_account
                .lock()
                .unwrap()
                .get(platform_user_id)
                .cloned()
                .unwrap_or(Err(IdentityError::NotAMember))
        })
    }

    fn confirm_member<'a>(
        &'a self,
        _scope: IdentityScope,
        user: Uuid,
    ) -> IdentityFuture<'a, Result<Uuid, IdentityError>> {
        Box::pin(async move { Ok(user) })
    }
}

/// What the invocation knows about WHO triggered it and WHICH event it is.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Facts {
    /// Identity wired; the event carries alice's account id and an event id.
    Alice,
    /// No identity capability wiring at all.
    NoIdentityWiring,
    /// Identity wired, but the event carries no event id.
    NoEventId,
    /// Identity wired and an event id, but no triggering account id on it.
    NoActorAccount,
    /// As [`Facts::Alice`], but for a different event.
    OtherEvent,
    /// As [`Facts::Alice`] with the `identity` capability's own flag OFF: the
    /// actor binding is a platform guarantee, not that bundle-facing feature.
    IdentityFlagOff,
}

struct Fixture {
    caps: StageCapabilities,
    store: Arc<RecordingStore>,
    directory: Arc<ActorDirectory>,
    alice: Uuid,
    bob: Uuid,
}

/// A fresh invocation's host-derived facts for event `event_id`.
fn invocation_for(event_id: Option<&str>, account: Option<&str>) -> Arc<InvocationIdentity> {
    let inv = InvocationIdentity::new("twitch", account.map(str::to_string), vec![]);
    Arc::new(match event_id {
        Some(id) => inv.with_event_id(id),
        None => inv,
    })
}

fn identity_wiring_with_flag(directory: &Arc<ActorDirectory>, flag_on: bool) -> IdentityWiring {
    IdentityWiring {
        directory: Arc::clone(directory) as Arc<dyn MemberDirectory>,
        handles: None,
        flag: Arc::new(FixedGate(flag_on)),
    }
}

fn identity_wiring(directory: &Arc<ActorDirectory>) -> IdentityWiring {
    identity_wiring_with_flag(directory, true)
}

fn grants(ids: &[(&str, serde_json::Value)]) -> GrantSet {
    GrantSet {
        permission_snapshot_hash: "test".to_string(),
        grants: ids
            .iter()
            .map(|(id, params)| {
                (
                    (*id).to_string(),
                    GrantedPermission {
                        permission_id: (*id).to_string(),
                        params: params.clone(),
                    },
                )
            })
            .collect(),
    }
}

fn fixture_with(
    grant_set: GrantSet,
    flag_on: bool,
    wired: bool,
    community: Option<(&str, i32)>,
) -> Fixture {
    fixture_facts(grant_set, flag_on, wired, community, Facts::Alice)
}

fn fixture_facts(
    grant_set: GrantSet,
    flag_on: bool,
    wired: bool,
    community: Option<(&str, i32)>,
    facts: Facts,
) -> Fixture {
    let snapshot = InMemoryGrantSnapshot::new();
    snapshot.set(
        GrantScopeKey {
            tenant_id: TENANT_ID,
            community_id: community.map_or(0, |c| c.1),
            app_id: APP_ID.to_string(),
            app_version: VERSION,
        },
        grant_set,
    );
    let (alice, bob) = (Uuid::new_v4(), Uuid::new_v4());
    let membership = InMemoryMembership::new();
    membership.add_community_member(TENANT_ID, COMMUNITY_ID, alice);
    membership.add_community_member(TENANT_ID, COMMUNITY_ID, bob);
    let gate = Arc::new(CapabilityGate::new(
        Arc::new(snapshot),
        Arc::new(membership),
        Arc::new(InMemoryQuotaLedger::new()),
        Arc::new(InMemoryInstancePolicySnapshot::new()),
    ));
    let egress = Arc::new(EgressGuard::new(
        Arc::new(ReqwestTransport::new()),
        bundle_host_http::egress::EgressLimits {
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
            prometheus::Opts::new("test_eco_egress_denied_total", "test"),
            &["app_id", "reason"],
        )
        .unwrap(),
        bundle_host_http::egress::boxed(StaticFlag(true)),
    ));
    let mut caps = StageCapabilities::new(
        "acme".to_string(),
        community.map(|c| c.0.to_string()),
        APP_ID.to_string(),
        TENANT_ID,
        community.map_or(0, |c| c.1),
        VERSION,
        egress,
        gate,
    );
    let store = Arc::new(RecordingStore::default());
    if wired {
        caps = caps.with_economy(EconomyWiring {
            store: Arc::clone(&store) as Arc<dyn EconomyStore>,
            flag: Arc::new(FixedGate(flag_on)),
        });
    }
    let directory = Arc::new(ActorDirectory::default());
    directory.set(ACTOR_ACCOUNT, Ok(alice));
    let caps = match facts {
        Facts::NoIdentityWiring => caps,
        Facts::Alice => caps.with_identity(
            identity_wiring(&directory),
            invocation_for(Some(EVENT_ID), Some(ACTOR_ACCOUNT)),
        ),
        Facts::NoEventId => caps.with_identity(
            identity_wiring(&directory),
            invocation_for(None, Some(ACTOR_ACCOUNT)),
        ),
        Facts::NoActorAccount => caps.with_identity(
            identity_wiring(&directory),
            invocation_for(Some(EVENT_ID), None),
        ),
        Facts::OtherEvent => caps.with_identity(
            identity_wiring(&directory),
            invocation_for(Some(OTHER_EVENT_ID), Some(ACTOR_ACCOUNT)),
        ),
        Facts::IdentityFlagOff => caps.with_identity(
            identity_wiring_with_flag(&directory, false),
            invocation_for(Some(EVENT_ID), Some(ACTOR_ACCOUNT)),
        ),
    };
    Fixture {
        caps,
        store,
        directory,
        alice,
        bob,
    }
}

fn full_grants() -> GrantSet {
    grants(&[
        ("economy.read", serde_json::json!({})),
        ("economy.wager", serde_json::json!({"max_bet": 50})),
        ("economy.transfer", serde_json::json!({"max_amount": 200})),
    ])
}

fn fixture() -> Fixture {
    fixture_with(full_grants(), true, true, Some(("main", COMMUNITY_ID)))
}

fn eco_call(op: &str, args: serde_json::Value) -> HostCallBody {
    HostCallBody {
        app_id: APP_ID.to_string(),
        capability: CapabilityKind::Db,
        op: op.to_string(),
        args,
        call_id: 1,
    }
}

async fn code_of(f: &Fixture, op: &str, args: serde_json::Value) -> String {
    f.caps
        .handle(eco_call(op, args))
        .await
        .expect_err("expected a denial")
        .code
}

fn expected_scope() -> EconomyScope {
    EconomyScope {
        tenant_id: TENANT_ID,
        community_id: COMMUNITY_ID,
        app_id: APP_ID.to_string(),
    }
}

#[tokio::test]
async fn wager_reaches_the_store_with_host_derived_scope_and_the_declared_cap() {
    let f = fixture();
    f.store.answer(Ok(115));
    // Guest-supplied tenant/community/app/cap fields must be ignored entirely.
    let out = f
        .caps
        .handle(eco_call(
            "economy.wager",
            serde_json::json!({
                "user": f.alice.to_string(), "stake": 10, "payout": 25,
                "tenant_id": 999, "community_id": 999, "app_id": "evil", "max_bet": 1_000_000,
            }),
        ))
        .await
        .unwrap();
    assert_eq!(out, serde_json::json!({ "balance": 115 }));
    let calls = f.store.calls.lock().unwrap();
    assert_eq!(calls.len(), 1);
    let (op, scope, users, nums) = &calls[0];
    assert_eq!(op, "wager");
    assert_eq!(scope, &expected_scope());
    assert_eq!(users, &vec![f.alice]);
    // stake, payout, the per-call cap = the GRANT's declared 50 (not the
    // guest's), then the catalog's durable per-user / per-scope daily caps.
    assert_eq!(nums, &vec![10, 25, 50, 10_000, 250_000]);
}

#[tokio::test]
async fn transfer_reaches_the_store_with_both_users_and_the_declared_cap() {
    let f = fixture();
    let out = f
        .caps
        .handle(eco_call(
            "economy.transfer",
            serde_json::json!({
                "from": f.alice.to_string(), "to": f.bob.to_string(), "amount": 30,
            }),
        ))
        .await
        .unwrap();
    assert_eq!(out, serde_json::json!({}));
    let calls = f.store.calls.lock().unwrap();
    let (op, scope, users, nums) = &calls[0];
    assert_eq!(op, "transfer");
    assert_eq!(scope, &expected_scope());
    assert_eq!(users, &vec![f.alice, f.bob]);
    // amount, the grant's declared max_amount, then the transfer family's own
    // durable per-sender / per-scope daily caps.
    assert_eq!(nums, &vec![30, 200, 5_000, 100_000]);
}

#[tokio::test]
async fn balance_max_bet_and_leaderboard_reach_the_store() {
    let f = fixture();
    f.store.answer(Ok(7));
    let out = f
        .caps
        .handle(eco_call(
            "economy.balance",
            serde_json::json!({ "user": f.alice.to_string() }),
        ))
        .await
        .unwrap();
    assert_eq!(out, serde_json::json!({ "balance": 7 }));

    f.store.answer(Ok(50));
    let out = f
        .caps
        .handle(eco_call(
            "economy.max_bet",
            serde_json::json!({ "user": f.alice.to_string() }),
        ))
        .await
        .unwrap();
    assert_eq!(out, serde_json::json!({ "max_bet": 50 }));

    *f.store.next_board.lock().unwrap() = Some(Ok(vec![
        LeaderboardEntry {
            user: f.bob,
            balance: 300,
        },
        LeaderboardEntry {
            user: f.alice,
            balance: 10,
        },
    ]));
    let out = f
        .caps
        .handle(eco_call(
            "economy.leaderboard",
            serde_json::json!({ "limit": 5 }),
        ))
        .await
        .unwrap();
    assert_eq!(
        out,
        serde_json::json!({ "entries": [
            { "user": f.bob.to_string(), "balance": 300 },
            { "user": f.alice.to_string(), "balance": 10 },
        ]})
    );

    let calls = f.store.calls.lock().unwrap();
    assert_eq!(
        calls
            .iter()
            .map(|c| (c.0.as_str(), c.3.clone()))
            .collect::<Vec<_>>(),
        vec![
            ("balance", vec![]),
            // max_bet is told the wager cap = the grant's declared 50.
            ("max_bet", vec![50]),
            ("leaderboard", vec![5]),
        ]
    );
}

#[tokio::test]
async fn an_ungranted_call_is_denied_not_granted_and_never_reaches_the_store() {
    let f = fixture_with(grants(&[]), true, true, Some(("main", COMMUNITY_ID)));
    let a = f.alice.to_string();
    let b = f.bob.to_string();
    for (op, args) in [
        (
            "economy.wager",
            serde_json::json!({"user": a, "stake": 1, "payout": 0}),
        ),
        (
            "economy.transfer",
            serde_json::json!({"from": a, "to": b, "amount": 1}),
        ),
        ("economy.balance", serde_json::json!({"user": a})),
        ("economy.max_bet", serde_json::json!({"user": a})),
        ("economy.leaderboard", serde_json::json!({"limit": 3})),
    ] {
        assert_eq!(code_of(&f, op, args).await, "not_granted", "{op}");
    }
    assert_eq!(f.store.call_count(), 0);
}

/// Each economy permission is distinct: read alone cannot wager or transfer,
/// wager alone cannot transfer, and `max_bet` needs the WAGER grant.
#[tokio::test]
async fn permissions_are_distinct_read_wager_transfer() {
    let a = |f: &Fixture| f.alice.to_string();
    let read_only = fixture_with(
        grants(&[("economy.read", serde_json::json!({}))]),
        true,
        true,
        Some(("main", COMMUNITY_ID)),
    );
    let args = serde_json::json!({"user": a(&read_only), "stake": 1, "payout": 0});
    assert_eq!(
        code_of(&read_only, "economy.wager", args).await,
        "not_granted"
    );
    assert_eq!(
        code_of(
            &read_only,
            "economy.max_bet",
            serde_json::json!({"user": a(&read_only)})
        )
        .await,
        "not_granted"
    );
    let wager_only = fixture_with(
        grants(&[("economy.wager", serde_json::json!({}))]),
        true,
        true,
        Some(("main", COMMUNITY_ID)),
    );
    let args = serde_json::json!({
        "from": a(&wager_only), "to": wager_only.bob.to_string(), "amount": 1
    });
    assert_eq!(
        code_of(&wager_only, "economy.transfer", args).await,
        "not_granted"
    );
    assert_eq!(
        code_of(
            &wager_only,
            "economy.balance",
            serde_json::json!({"user": a(&wager_only)})
        )
        .await,
        "not_granted"
    );
    assert_eq!(
        read_only.store.call_count() + wager_only.store.call_count(),
        0
    );
}

#[tokio::test]
async fn a_non_member_on_either_side_is_denied_by_the_gate_before_the_store() {
    let f = fixture();
    let stranger = Uuid::new_v4().to_string();
    let alice = f.alice.to_string();
    for (op, args) in [
        (
            "economy.wager",
            serde_json::json!({"user": stranger, "stake": 1, "payout": 0}),
        ),
        (
            "economy.transfer",
            serde_json::json!({"from": alice, "to": stranger, "amount": 1}),
        ),
        (
            "economy.transfer",
            serde_json::json!({"from": stranger, "to": alice, "amount": 1}),
        ),
        ("economy.balance", serde_json::json!({"user": stranger})),
        ("economy.max_bet", serde_json::json!({"user": stranger})),
    ] {
        assert_eq!(code_of(&f, op, args).await, "user_not_in_scope", "{op}");
    }
    assert_eq!(f.store.call_count(), 0);
}

#[tokio::test]
async fn amounts_beyond_the_declared_bounds_are_denied_before_the_store() {
    let f = fixture();
    let a = f.alice.to_string();
    let b = f.bob.to_string();
    // declared max_bet = 50, max_amount = 200
    assert_eq!(
        code_of(
            &f,
            "economy.wager",
            serde_json::json!({"user": a, "stake": 51, "payout": 0})
        )
        .await,
        "amount_out_of_bounds"
    );
    assert_eq!(
        code_of(
            &f,
            "economy.transfer",
            serde_json::json!({"from": a, "to": b, "amount": 201})
        )
        .await,
        "amount_out_of_bounds"
    );
    assert_eq!(f.store.call_count(), 0);
}

#[tokio::test]
async fn the_daily_amount_quota_is_the_economys_own_and_denies_with_quota_exceeded() {
    let f = fixture_with(
        grants(&[("economy.wager", serde_json::json!({}))]),
        true,
        true,
        Some(("main", COMMUNITY_ID)),
    );
    let args = serde_json::json!({"user": f.alice.to_string(), "stake": 1_000, "payout": 0});
    // 10 x 1_000 exhausts the per-user daily stake ceiling (10_000).
    for _ in 0..10 {
        f.caps
            .handle(eco_call("economy.wager", args.clone()))
            .await
            .unwrap();
    }
    assert_eq!(f.store.call_count(), 10);
    assert_eq!(code_of(&f, "economy.wager", args).await, "quota_exceeded");
    assert_eq!(f.store.call_count(), 10, "the 11th never reached the store");
}

#[tokio::test]
async fn flag_off_and_unwired_both_fail_loud_after_the_gate() {
    let off = fixture_with(full_grants(), false, true, Some(("main", COMMUNITY_ID)));
    let args = serde_json::json!({"user": off.alice.to_string(), "stake": 1, "payout": 0});
    assert_eq!(
        code_of(&off, "economy.wager", args).await,
        "feature_disabled"
    );
    assert_eq!(off.store.call_count(), 0);

    let unwired = fixture_with(full_grants(), true, false, Some(("main", COMMUNITY_ID)));
    for (op, args) in [
        (
            "economy.wager",
            serde_json::json!({"user": unwired.alice.to_string(), "stake": 1, "payout": 0}),
        ),
        (
            "economy.balance",
            serde_json::json!({"user": unwired.alice.to_string()}),
        ),
        ("economy.leaderboard", serde_json::json!({"limit": 3})),
    ] {
        assert_eq!(code_of(&unwired, op, args).await, "not_implemented", "{op}");
    }
}

/// Ungranted + unwired reports `not_granted` (gate first), never
/// `not_implemented`.
#[tokio::test]
async fn gate_runs_before_the_wiring_check() {
    let f = fixture_with(grants(&[]), true, false, Some(("main", COMMUNITY_ID)));
    let code = code_of(
        &f,
        "economy.balance",
        serde_json::json!({"user": f.alice.to_string()}),
    )
    .await;
    assert_eq!(code, "not_granted");
}

#[tokio::test]
async fn malformed_arguments_are_invalid_args() {
    let f = fixture();
    let a = f.alice.to_string();
    let b = f.bob.to_string();
    for (op, args) in [
        ("economy.balance", serde_json::json!({})),
        ("economy.balance", serde_json::json!({"user": "not-a-uuid"})),
        ("economy.balance", serde_json::json!({"user": 5})),
        ("economy.wager", serde_json::json!({"user": a, "payout": 0})),
        ("economy.wager", serde_json::json!({"user": a, "stake": 5})),
        (
            "economy.wager",
            serde_json::json!({"user": a, "stake": 0, "payout": 0}),
        ),
        (
            "economy.wager",
            serde_json::json!({"user": a, "stake": -1, "payout": 0}),
        ),
        (
            "economy.wager",
            serde_json::json!({"user": a, "stake": 1.5, "payout": 0}),
        ),
        (
            "economy.wager",
            serde_json::json!({"user": a, "stake": "5", "payout": 0}),
        ),
        (
            "economy.wager",
            serde_json::json!({"user": a, "stake": 1, "payout": -1}),
        ),
        (
            "economy.wager",
            serde_json::json!({"user": a, "stake": 1, "payout": u64::MAX}),
        ),
        (
            "economy.transfer",
            serde_json::json!({"from": a, "to": b, "amount": 0}),
        ),
        (
            "economy.transfer",
            serde_json::json!({"from": a, "to": "nope", "amount": 1}),
        ),
        (
            "economy.transfer",
            serde_json::json!({"from": a, "amount": 1}),
        ),
        // transfer to oneself
        (
            "economy.transfer",
            serde_json::json!({"from": a, "to": a, "amount": 1}),
        ),
        ("economy.leaderboard", serde_json::json!({})),
        ("economy.leaderboard", serde_json::json!({"limit": 0})),
        ("economy.leaderboard", serde_json::json!({"limit": 101})),
        (
            "economy.leaderboard",
            serde_json::json!({"limit": 99999999999_u64}),
        ),
    ] {
        assert_eq!(
            code_of(&f, op, args.clone()).await,
            "invalid_args",
            "{op} {args}"
        );
    }
    assert_eq!(f.store.call_count(), 0);
}

#[tokio::test]
async fn a_tenant_wide_activation_has_no_community_to_hold_currency_in() {
    let f = fixture_with(full_grants(), true, true, None);
    let code = code_of(
        &f,
        "economy.balance",
        serde_json::json!({"user": f.alice.to_string()}),
    )
    .await;
    assert_eq!(code, "invalid_args");
    assert_eq!(f.store.call_count(), 0);
}

#[tokio::test]
async fn unknown_economy_op_is_denied() {
    let f = fixture();
    assert_eq!(
        code_of(&f, "economy.mint", serde_json::json!({})).await,
        "unknown_op"
    );
}

#[tokio::test]
async fn store_refusals_map_to_their_wire_codes_and_numbers_and_backend_detail_never_leaks() {
    let f = fixture();
    let args = serde_json::json!({"user": f.alice.to_string(), "stake": 1, "payout": 0});
    for (err, want_code, want_message) in [
        (EconomyError::NotAMember, "not_a_member", None),
        (
            EconomyError::InsufficientFunds { balance: 7 },
            "insufficient_funds",
            Some("7"),
        ),
        (EconomyError::OverCap { cap: 50 }, "over_cap", Some("50")),
        (EconomyError::Invalid("x".into()), "invalid_args", None),
    ] {
        f.store.answer(Err(err));
        let denied = f
            .caps
            .handle(eco_call("economy.wager", args.clone()))
            .await
            .unwrap_err();
        assert_eq!(denied.code, want_code);
        if let Some(m) = want_message {
            assert_eq!(denied.message, m, "numeric refusals carry the bare number");
        }
    }
    f.store.answer(Err(EconomyError::Backend(
        "password=hunter2 host=db".into(),
    )));
    let denied = f
        .caps
        .handle(eco_call("economy.wager", args))
        .await
        .unwrap_err();
    assert_eq!(denied.code, "backend");
    assert!(!denied.message.contains("hunter2"), "{}", denied.message);
}

/// `economy.*` must route to its own handler, never the `storage.tables`
/// path -- proven by granting ONLY `storage.tables` and observing
/// `not_granted` for the economy permission rather than a db result.
#[tokio::test]
async fn economy_ops_do_not_ride_the_storage_tables_or_reputation_permission() {
    let f = fixture_with(
        grants(&[
            ("storage.tables", serde_json::json!({})),
            ("reputation.read", serde_json::json!({})),
            (
                "reputation.community.write",
                serde_json::json!({"delta_min": -5, "delta_max": 5}),
            ),
        ]),
        true,
        true,
        Some(("main", COMMUNITY_ID)),
    );
    let code = code_of(
        &f,
        "economy.wager",
        serde_json::json!({"user": f.alice.to_string(), "stake": 1, "payout": 0}),
    )
    .await;
    assert_eq!(code, "not_granted");
    assert_eq!(f.store.call_count(), 0);
}

#[test]
fn durable_caps_match_the_catalog_per_family_and_fail_closed_otherwise() {
    assert_eq!(
        economy_caps(PermissionFamily::EconomyWager),
        EconomyCaps {
            per_user_daily_max: 10_000,
            per_scope_daily_max: 250_000
        }
    );
    assert_eq!(
        economy_caps(PermissionFamily::EconomyTransfer),
        EconomyCaps {
            per_user_daily_max: 5_000,
            per_scope_daily_max: 100_000
        }
    );
    // A non-EconomyAmount family refuses everything rather than guessing.
    for family in [PermissionFamily::EconomyRead, PermissionFamily::StorageKv] {
        assert_eq!(
            economy_caps(family),
            EconomyCaps {
                per_user_daily_max: 0,
                per_scope_daily_max: 0
            }
        );
    }
}

#[test]
fn the_economy_amount_quota_is_not_reputations() {
    let Quota::EconomyAmount {
        per_call_abs_max, ..
    } = PermissionFamily::EconomyWager.catalog_entry().default_quota
    else {
        panic!("economy.wager must carry an EconomyAmount quota");
    };
    let Quota::ReputationDelta {
        per_call_abs_max: rep_per_call,
        ..
    } = PermissionFamily::ReputationCommunityWrite
        .catalog_entry()
        .default_quota
    else {
        panic!("reputation.community.write must carry a ReputationDelta quota");
    };
    assert!(i64::from(rep_per_call) < per_call_abs_max);
}

#[test]
fn economy_wire_error_helpers_cover_every_variant() {
    for (err, code) in [
        (EconomyError::UserQuotaExceeded { cap: 1 }, "quota_exceeded"),
        (
            EconomyError::ScopeQuotaExceeded { cap: 1 },
            "quota_exceeded",
        ),
        (EconomyError::IdempotencyConflict, "idempotency_conflict"),
        (EconomyError::NotAMember, "not_a_member"),
        (
            EconomyError::InsufficientFunds { balance: 1 },
            "insufficient_funds",
        ),
        (EconomyError::OverCap { cap: 1 }, "over_cap"),
        (EconomyError::Invalid("i".into()), "invalid_args"),
        (EconomyError::Backend("b".into()), "backend"),
    ] {
        assert_eq!(economy_error_to_host(err).code, code);
    }
}

// ---- #751 money-safety regressions -------------------------------------------

fn wager_args(user: Uuid, stake: i64, payout: i64) -> serde_json::Value {
    serde_json::json!({ "user": user.to_string(), "stake": stake, "payout": payout })
}

fn transfer_args(from: Uuid, to: Uuid, amount: i64) -> serde_json::Value {
    serde_json::json!({ "from": from.to_string(), "to": to.to_string(), "amount": amount })
}

fn key(event: &str, kind: &str, ordinal: u32) -> String {
    format!("{event}:{kind}:{ordinal}")
}

/// Regression (#751 review, NO ACTOR-BINDING / theft): a bundle that passes
/// another member's UUID as the payer must NOT be able to move that member's
/// funds. The payer must be the account that triggered the invocation.
#[tokio::test]
async fn a_transfer_from_a_victim_is_rejected_and_never_reaches_the_store() {
    // alice triggered this invocation; bob is the victim.
    let f = fixture();
    // Steal INTO the actor, and out to a third party: both name the victim as
    // the payer.
    let steal = transfer_args(f.bob, f.alice, 10);
    let denied = f
        .caps
        .handle(eco_call("economy.transfer", steal))
        .await
        .unwrap_err();
    assert_eq!(denied.code, "actor_mismatch");
    // The refusal is a fixed constant: it names nobody (no UUID, no account id).
    assert!(
        !denied.message.contains(&f.bob.to_string()),
        "{}",
        denied.message
    );
    assert!(
        !denied.message.contains(&f.alice.to_string()),
        "{}",
        denied.message
    );
    assert!(
        !denied.message.contains(ACTOR_ACCOUNT),
        "{}",
        denied.message
    );
    assert_eq!(
        f.store.call_count(),
        0,
        "the victim's funds were never touched"
    );
    assert!(f.store.keys().is_empty());
}

#[tokio::test]
async fn a_wager_on_a_victims_account_is_rejected_and_never_reaches_the_store() {
    let f = fixture();
    assert_eq!(
        code_of(&f, "economy.wager", wager_args(f.bob, 10, 25)).await,
        "actor_mismatch"
    );
    // ... including a "losing" wager of the victim's funds.
    assert_eq!(
        code_of(&f, "economy.wager", wager_args(f.bob, 10, 0)).await,
        "actor_mismatch"
    );
    assert_eq!(f.store.call_count(), 0);
}

#[tokio::test]
async fn the_actor_can_still_move_its_own_funds_to_anyone() {
    let f = fixture();
    f.caps
        .handle(eco_call(
            "economy.transfer",
            transfer_args(f.alice, f.bob, 10),
        ))
        .await
        .expect("the actor pays a member");
    f.store.answer(Ok(90));
    f.caps
        .handle(eco_call("economy.wager", wager_args(f.alice, 10, 0)))
        .await
        .expect("the actor wagers its own funds");
    let calls = f.store.calls.lock().unwrap();
    assert_eq!(calls.len(), 2);
    assert_eq!(calls[0].2, vec![f.alice, f.bob]);
    assert_eq!(calls[1].2, vec![f.alice]);
}

/// The payer string is parsed to a UUID before the comparison, so alternate
/// spellings of the VICTIM'S uuid cannot slip past a string compare, and
/// alternate spellings of the ACTOR'S uuid are (correctly) still the actor.
#[tokio::test]
async fn the_binding_compares_uuids_not_strings() {
    let f = fixture();
    let bob_upper = f.bob.to_string().to_uppercase();
    let bob_braced = format!("{{{}}}", f.bob);
    for spelling in [bob_upper, bob_braced, f.bob.simple().to_string()] {
        let args = serde_json::json!({ "from": spelling, "to": f.alice.to_string(), "amount": 1 });
        let code = code_of(&f, "economy.transfer", args).await;
        // Either rejected as malformed or as the wrong payer -- never accepted.
        assert!(
            code == "actor_mismatch" || code == "invalid_args",
            "{spelling:?} -> {code}"
        );
    }
    let alice_upper = f.alice.to_string().to_uppercase();
    f.caps
        .handle(eco_call(
            "economy.transfer",
            serde_json::json!({ "from": alice_upper, "to": f.bob.to_string(), "amount": 1 }),
        ))
        .await
        .expect("the actor's own uuid, spelled differently, is still the actor");
    assert_eq!(f.store.call_count(), 1);
}

/// Reads are not mutations: any member's balance/limit can be read, as before.
#[tokio::test]
async fn reads_of_other_members_are_not_actor_bound() {
    let f = fixture();
    f.store.answer(Ok(5));
    let out = f
        .caps
        .handle(eco_call(
            "economy.balance",
            serde_json::json!({ "user": f.bob.to_string() }),
        ))
        .await
        .unwrap();
    assert_eq!(out, serde_json::json!({ "balance": 5 }));
    f.store.answer(Ok(5));
    f.caps
        .handle(eco_call(
            "economy.max_bet",
            serde_json::json!({ "user": f.bob.to_string() }),
        ))
        .await
        .unwrap();
    assert_eq!(f.directory.lookups(), 0, "reads never resolve an actor");
}

/// The gate still answers first: an ungranted bundle learns `not_granted`, not
/// whose account the actor is.
#[tokio::test]
async fn the_gate_runs_before_the_actor_binding() {
    let f = fixture_with(grants(&[]), true, true, Some(("main", COMMUNITY_ID)));
    assert_eq!(
        code_of(&f, "economy.wager", wager_args(f.bob, 1, 0)).await,
        "not_granted"
    );
    assert_eq!(f.directory.lookups(), 0);
}

/// A call refused for the wrong payer claims no idempotency ordinal.
#[tokio::test]
async fn a_rejected_theft_attempt_does_not_consume_a_key_ordinal() {
    let f = fixture();
    assert_eq!(
        code_of(&f, "economy.wager", wager_args(f.bob, 1, 0)).await,
        "actor_mismatch"
    );
    f.caps
        .handle(eco_call("economy.wager", wager_args(f.alice, 1, 0)))
        .await
        .unwrap();
    assert_eq!(f.store.keys(), vec![key(EVENT_ID, "wager", 0)]);
}

/// Fail-closed: no way to bind an actor (or to key the call) means the money
/// mover is refused loudly, never run unbound.
#[tokio::test]
async fn money_movers_fail_closed_when_the_actor_or_event_cannot_be_established() {
    for (facts, want) in [
        (Facts::NoIdentityWiring, "not_implemented"),
        (Facts::NoEventId, "not_implemented"),
        (Facts::NoActorAccount, "not_linked"),
    ] {
        let f = fixture_facts(
            full_grants(),
            true,
            true,
            Some(("main", COMMUNITY_ID)),
            facts,
        );
        assert_eq!(
            code_of(&f, "economy.wager", wager_args(f.alice, 1, 0)).await,
            want
        );
        assert_eq!(
            code_of(&f, "economy.transfer", transfer_args(f.alice, f.bob, 1)).await,
            want
        );
        assert_eq!(f.store.call_count(), 0, "nothing ran unbound or unkeyed");
    }
    // Reads need neither an actor nor an event.
    let f = fixture_facts(
        full_grants(),
        true,
        true,
        Some(("main", COMMUNITY_ID)),
        Facts::NoIdentityWiring,
    );
    f.store.answer(Ok(1));
    f.caps
        .handle(eco_call(
            "economy.balance",
            serde_json::json!({ "user": f.alice.to_string() }),
        ))
        .await
        .expect("reads work without identity wiring");
}

#[tokio::test]
async fn an_unresolvable_actor_surfaces_the_identity_refusal_and_nothing_runs() {
    for (answer, want) in [
        (Err(IdentityError::NotAMember), "not_a_member"),
        (Err(IdentityError::NotLinked), "not_linked"),
        (Err(IdentityError::Unavailable("x".into())), "unavailable"),
        (
            Err(IdentityError::Backend("password=hunter2".into())),
            "backend",
        ),
        // A nil UUID is never an identity.
        (Ok(Uuid::nil()), "backend"),
    ] {
        let f = fixture();
        f.directory.set(ACTOR_ACCOUNT, answer);
        let denied = f
            .caps
            .handle(eco_call("economy.wager", wager_args(f.alice, 1, 0)))
            .await
            .unwrap_err();
        assert_eq!(denied.code, want);
        assert!(!denied.message.contains("hunter2"), "{}", denied.message);
        assert_eq!(f.store.call_count(), 0);
    }
}

/// The binding is a platform guarantee: it needs neither the bundle to hold
/// `identity.resolve` (the full grant set has none) nor the identity
/// capability's own feature flag to be ON.
#[tokio::test]
async fn the_binding_needs_neither_the_identity_grant_nor_the_identity_flag() {
    assert!(!full_grants().grants.contains_key("identity.resolve"));
    let f = fixture_facts(
        full_grants(),
        true,
        true,
        Some(("main", COMMUNITY_ID)),
        Facts::IdentityFlagOff,
    );
    f.caps
        .handle(eco_call("economy.wager", wager_args(f.alice, 1, 0)))
        .await
        .expect("bound with the identity flag OFF");
    assert_eq!(
        code_of(&f, "economy.wager", wager_args(f.bob, 1, 0)).await,
        "actor_mismatch"
    );
}

/// Regression (#751 review, MINT CAP WRONG): the daily cap that bounds how much
/// a wager mints summed the STAKE, so a bundle choosing its own payouts could
/// mint without bound. The mint budget is the PAYOUT.
#[tokio::test]
async fn the_wager_mint_cap_is_the_payout_not_the_stake() {
    let f = fixture_with(
        grants(&[("economy.wager", serde_json::json!({}))]),
        true,
        true,
        Some(("main", COMMUNITY_ID)),
    );
    // Two stake-50 wagers each paying 5_000 (100x): 10_000 = the per-user daily
    // mint ceiling, on a combined STAKE of just 100 (of its 10_000).
    for _ in 0..2 {
        f.caps
            .handle(eco_call("economy.wager", wager_args(f.alice, 50, 5_000)))
            .await
            .expect("within the daily mint budget");
    }
    // A stake-metered cap would wave through a hundred more of these.
    assert_eq!(
        code_of(&f, "economy.wager", wager_args(f.alice, 1, 1)).await,
        "quota_exceeded"
    );
    assert_eq!(
        f.store.call_count(),
        2,
        "the over-budget mint never reached the store"
    );
    // Losing wagers mint nothing: the stake budget is the only thing they spend.
    f.caps
        .handle(eco_call("economy.wager", wager_args(f.alice, 50, 0)))
        .await
        .expect("a losing wager mints nothing, so the spent mint budget does not block it");
}

/// Regression (#751 review, NO IDEMPOTENCY / double-spend): the stage derives a
/// replay-stable key per mutation from the event id, the kind and an ordinal.
#[tokio::test]
async fn each_mutation_is_keyed_by_event_kind_and_ordinal() {
    let f = fixture();
    f.caps
        .handle(eco_call("economy.wager", wager_args(f.alice, 1, 0)))
        .await
        .unwrap();
    f.caps
        .handle(eco_call("economy.wager", wager_args(f.alice, 1, 0)))
        .await
        .unwrap();
    f.caps
        .handle(eco_call(
            "economy.transfer",
            transfer_args(f.alice, f.bob, 1),
        ))
        .await
        .unwrap();
    f.caps
        .handle(eco_call("economy.wager", wager_args(f.alice, 1, 0)))
        .await
        .unwrap();
    assert_eq!(
        f.store.keys(),
        vec![
            key(EVENT_ID, "wager", 0),
            // An identical second call in ONE invocation is its own operation.
            key(EVENT_ID, "wager", 1),
            key(EVENT_ID, "transfer", 0),
            key(EVENT_ID, "wager", 2),
        ]
    );
}

/// A redelivered event (a fresh invocation of the same envelope) whose bundle
/// repeats its calls presents the SAME keys, so the store credits once; a
/// different event never collides with it.
#[tokio::test]
async fn a_redelivered_event_reproduces_the_same_keys() {
    async fn run(f: &Fixture) -> Vec<String> {
        f.caps
            .handle(eco_call("economy.wager", wager_args(f.alice, 10, 25)))
            .await
            .unwrap();
        f.caps
            .handle(eco_call(
                "economy.transfer",
                transfer_args(f.alice, f.bob, 3),
            ))
            .await
            .unwrap();
        f.store.keys()
    }
    let first = run(&fixture()).await;
    let replay = run(&fixture()).await;
    assert_eq!(first, replay, "the redelivery maps to the original keys");
    assert_eq!(
        first,
        vec![key(EVENT_ID, "wager", 0), key(EVENT_ID, "transfer", 0)]
    );
    let other = run(&fixture_facts(
        full_grants(),
        true,
        true,
        Some(("main", COMMUNITY_ID)),
        Facts::OtherEvent,
    ))
    .await;
    assert_eq!(
        other,
        vec![
            key(OTHER_EVENT_ID, "wager", 0),
            key(OTHER_EVENT_ID, "transfer", 0)
        ]
    );
    assert!(first.iter().all(|k| !other.contains(k)));
}

/// A replay that re-rolls its payout presents the same key with different
/// parameters: the store's `idempotency_conflict` reaches the bundle as a
/// denial, never as a second credit.
#[tokio::test]
async fn a_store_idempotency_conflict_surfaces_as_a_denial() {
    let f = fixture();
    f.store.answer(Err(EconomyError::IdempotencyConflict));
    let denied = f
        .caps
        .handle(eco_call("economy.wager", wager_args(f.alice, 10, 99)))
        .await
        .unwrap_err();
    assert_eq!(denied.code, "idempotency_conflict");
    f.store.answer_unit(Err(EconomyError::IdempotencyConflict));
    assert_eq!(
        code_of(&f, "economy.transfer", transfer_args(f.alice, f.bob, 3)).await,
        "idempotency_conflict"
    );
}

/// A backend failure is INDETERMINATE (it may have committed): the ordinal is
/// given back so the guest's retry presents the SAME key and applies at most
/// once. A definitive refusal keeps its ordinal.
#[tokio::test]
async fn an_indeterminate_failure_frees_the_ordinal_for_the_retry_but_a_refusal_does_not() {
    let f = fixture();
    // 1. backend failure -> retry reuses wager:0
    f.store
        .answer(Err(EconomyError::Backend("connection reset".into())));
    assert_eq!(
        code_of(&f, "economy.wager", wager_args(f.alice, 10, 25)).await,
        "backend"
    );
    f.caps
        .handle(eco_call("economy.wager", wager_args(f.alice, 10, 25)))
        .await
        .unwrap();
    // 2. a definitive refusal keeps its ordinal (wager:1), the next call gets :2
    f.store
        .answer(Err(EconomyError::InsufficientFunds { balance: 0 }));
    assert_eq!(
        code_of(&f, "economy.wager", wager_args(f.alice, 10, 25)).await,
        "insufficient_funds"
    );
    f.caps
        .handle(eco_call("economy.wager", wager_args(f.alice, 10, 25)))
        .await
        .unwrap();
    assert_eq!(
        f.store.keys(),
        vec![
            key(EVENT_ID, "wager", 0),
            key(EVENT_ID, "wager", 0),
            key(EVENT_ID, "wager", 1),
            key(EVENT_ID, "wager", 2),
        ]
    );
}

#[test]
fn the_mutation_ordinals_are_per_kind_and_only_the_latest_can_be_given_back() {
    let inv = InvocationIdentity::new("twitch", None, vec![]).with_event_id(EVENT_ID);
    assert_eq!(inv.reserve_mutation("wager"), Some((EVENT_ID, 0)));
    assert_eq!(inv.reserve_mutation("wager"), Some((EVENT_ID, 1)));
    assert_eq!(inv.reserve_mutation("transfer"), Some((EVENT_ID, 0)));
    // Giving back an ordinal that is not the latest must not rewind past a
    // later reservation (two overlapping calls).
    inv.release_mutation("wager", 0);
    assert_eq!(inv.reserve_mutation("wager"), Some((EVENT_ID, 2)));
    inv.release_mutation("wager", 2);
    assert_eq!(inv.reserve_mutation("wager"), Some((EVENT_ID, 2)));
    // Releasing an unknown kind is a no-op, and no event id means no key.
    inv.release_mutation("nope", 0);
    let bare = InvocationIdentity::new("twitch", None, vec![]);
    assert_eq!(bare.reserve_mutation("wager"), None);
    assert!(format!("{inv:?}").contains("has_event_id: true"));
}
