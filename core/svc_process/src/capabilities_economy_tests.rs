//! `economy.*` host-capability tests (issue #714): the REAL
//! [`CapabilityGate`] (grants, declared `max_bet`/`max_amount`, the economy's
//! own amount quotas, membership pre-filter for every named user) and the REAL
//! `handle` dispatch run against a recording store double; the store's own SQL
//! is proven against a real Postgres in
//! `core/bundle_host_economy/tests/postgres_integration.rs` and the two joined
//! in `tests/economy_pg_e2e.rs`.

use super::*;
use crate::license::test_support::FixedGate;
use bundle_capability_gate::{
    GrantScopeKey, GrantSet, GrantedPermission, InMemoryGrantSnapshot,
    InMemoryInstancePolicySnapshot, InMemoryMembership, InMemoryQuotaLedger,
};
use bundle_host_economy::LeaderboardEntry;
use bundle_host_http::egress::{ReqwestTransport, StaticFlag};
use std::sync::Mutex;
use uuid::Uuid;

const TENANT_ID: i32 = 7;
const COMMUNITY_ID: i32 = 3;
const APP_ID: &str = "waddles.core.test-economy";
const VERSION: i64 = 1;

/// One recorded store call: `(op, scope, users, numbers)`.
type Recorded = (String, EconomyScope, Vec<Uuid>, Vec<i64>);

/// Records every store call; answers with canned results.
#[derive(Default)]
struct RecordingStore {
    calls: Mutex<Vec<Recorded>>,
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
    ) -> bundle_host_economy::BoxFuture<'a, Result<i64, EconomyError>> {
        Box::pin(async move {
            self.record("wager", scope, vec![user], vec![stake, payout, max_bet]);
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
    ) -> bundle_host_economy::BoxFuture<'a, Result<(), EconomyError>> {
        Box::pin(async move {
            self.record("transfer", scope, vec![from, to], vec![amount, max_amount]);
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

struct Fixture {
    caps: StageCapabilities,
    store: Arc<RecordingStore>,
    alice: Uuid,
    bob: Uuid,
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
    Fixture {
        caps,
        store,
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
    // stake, payout, and the cap = the GRANT's declared 50, not the guest's.
    assert_eq!(nums, &vec![10, 25, 50]);
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
    assert_eq!(nums, &vec![30, 200]);
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
