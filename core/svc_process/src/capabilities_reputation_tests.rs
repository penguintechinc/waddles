//! `reputation.*` host-capability tests (issue #726): the REAL
//! [`CapabilityGate`] (grants, declared delta bounds, membership pre-filter)
//! and the REAL `handle` dispatch run against a recording store double; the
//! store's own SQL is proven against a real Postgres in
//! `core/bundle_host_reputation/tests/postgres_integration.rs`.

use super::*;
use crate::license::test_support::FixedGate;
use bundle_capability_gate::{
    GrantScopeKey, GrantSet, GrantedPermission, InMemoryGrantSnapshot,
    InMemoryInstancePolicySnapshot, InMemoryMembership, InMemoryQuotaLedger,
};
use bundle_host_http::egress::{ReqwestTransport, StaticFlag};
use std::sync::Mutex;
use uuid::Uuid;

const TENANT_ID: i32 = 7;
const COMMUNITY_ID: i32 = 3;
const APP_ID: &str = "waddles.core.test-reputation";
const VERSION: i64 = 1;

type RecordedCall = (
    String,
    ReputationScope,
    Uuid,
    Option<(i32, String, ReputationCaps)>,
);

/// One recorded call of the P5 extension ops.
#[derive(Debug, PartialEq)]
enum ExtCall {
    Override {
        scope: ReputationScope,
        user: Uuid,
        absolute: i64,
        reason: String,
        key: String,
    },
    AdjustMany {
        scope: ReputationScope,
        items: Vec<(Uuid, i32)>,
        reason: String,
        caps: ReputationCaps,
        key: String,
    },
    GetMany(ReputationScope, Vec<Uuid>),
    Leaderboard(ReputationScope, u32),
}

/// Records every store call; answers with a canned result.
#[derive(Default)]
struct RecordingStore {
    calls: Mutex<Vec<RecordedCall>>,
    next: Mutex<Option<Result<i64, ReputationError>>>,
    ext: Mutex<Vec<ExtCall>>,
    next_unit: Mutex<Option<Result<(), ReputationError>>>,
    next_entries: Mutex<Option<Result<Vec<bundle_host_reputation::ScoreEntry>, ReputationError>>>,
}

impl RecordingStore {
    fn answer(&self, r: Result<i64, ReputationError>) {
        *self.next.lock().unwrap() = Some(r);
    }
    fn call_count(&self) -> usize {
        self.calls.lock().unwrap().len()
    }
    fn take(&self) -> Result<i64, ReputationError> {
        self.next.lock().unwrap().take().unwrap_or(Ok(0))
    }
    fn ext_calls(&self) -> Vec<ExtCall> {
        std::mem::take(&mut *self.ext.lock().unwrap())
    }
    fn ext_count(&self) -> usize {
        self.ext.lock().unwrap().len()
    }
}

impl ReputationStore for RecordingStore {
    fn get<'a>(
        &'a self,
        scope: &'a ReputationScope,
        user: Uuid,
    ) -> bundle_host_reputation::BoxFuture<'a, Result<i64, ReputationError>> {
        Box::pin(async move {
            self.calls
                .lock()
                .unwrap()
                .push(("get".into(), scope.clone(), user, None));
            self.take()
        })
    }
    fn adjust<'a>(
        &'a self,
        scope: &'a ReputationScope,
        user: Uuid,
        delta: i32,
        reason: &'a str,
        caps: ReputationCaps,
    ) -> bundle_host_reputation::BoxFuture<'a, Result<i64, ReputationError>> {
        Box::pin(async move {
            self.calls.lock().unwrap().push((
                "adjust".into(),
                scope.clone(),
                user,
                Some((delta, reason.to_string(), caps)),
            ));
            self.take()
        })
    }
    fn override_score<'a>(
        &'a self,
        scope: &'a ReputationScope,
        user: Uuid,
        absolute_score: i64,
        reason: &'a str,
        key: &'a bundle_host_reputation::IdempotencyKey,
    ) -> bundle_host_reputation::BoxFuture<'a, Result<i64, ReputationError>> {
        Box::pin(async move {
            self.ext.lock().unwrap().push(ExtCall::Override {
                scope: scope.clone(),
                user,
                absolute: absolute_score,
                reason: reason.to_string(),
                key: key.as_str().to_string(),
            });
            self.take()
        })
    }
    fn adjust_many<'a>(
        &'a self,
        scope: &'a ReputationScope,
        items: &'a [(Uuid, i32)],
        reason: &'a str,
        caps: ReputationCaps,
        key: &'a bundle_host_reputation::IdempotencyKey,
    ) -> bundle_host_reputation::BoxFuture<'a, Result<(), ReputationError>> {
        Box::pin(async move {
            self.ext.lock().unwrap().push(ExtCall::AdjustMany {
                scope: scope.clone(),
                items: items.to_vec(),
                reason: reason.to_string(),
                caps,
                key: key.as_str().to_string(),
            });
            self.next_unit.lock().unwrap().take().unwrap_or(Ok(()))
        })
    }
    fn get_many<'a>(
        &'a self,
        scope: &'a ReputationScope,
        users: &'a [Uuid],
    ) -> bundle_host_reputation::BoxFuture<
        'a,
        Result<Vec<bundle_host_reputation::ScoreEntry>, ReputationError>,
    > {
        Box::pin(async move {
            self.ext
                .lock()
                .unwrap()
                .push(ExtCall::GetMany(scope.clone(), users.to_vec()));
            self.next_entries
                .lock()
                .unwrap()
                .take()
                .unwrap_or(Ok(vec![]))
        })
    }
    fn leaderboard<'a>(
        &'a self,
        scope: &'a ReputationScope,
        limit: u32,
    ) -> bundle_host_reputation::BoxFuture<
        'a,
        Result<Vec<bundle_host_reputation::ScoreEntry>, ReputationError>,
    > {
        Box::pin(async move {
            self.ext
                .lock()
                .unwrap()
                .push(ExtCall::Leaderboard(scope.clone(), limit));
            self.next_entries
                .lock()
                .unwrap()
                .take()
                .unwrap_or(Ok(vec![]))
        })
    }
}

struct Fixture {
    caps: StageCapabilities,
    store: Arc<RecordingStore>,
    member: Uuid,
    /// A second active member of the same community.
    other: Uuid,
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
    let member = Uuid::new_v4();
    let membership = InMemoryMembership::new();
    membership.add_community_member(TENANT_ID, COMMUNITY_ID, member);
    let other = Uuid::new_v4();
    membership.add_community_member(TENANT_ID, COMMUNITY_ID, other);
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
            prometheus::Opts::new("test_rep_egress_denied_total", "test"),
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
        caps = caps.with_reputation(ReputationWiring {
            store: Arc::clone(&store) as Arc<dyn ReputationStore>,
            flag: Arc::new(FixedGate(flag_on)),
        });
    }
    Fixture {
        caps,
        store,
        member,
        other,
    }
}

fn full_grants() -> GrantSet {
    grants(&[
        ("reputation.read", serde_json::json!({})),
        (
            "reputation.community.write",
            serde_json::json!({"delta_min": -5, "delta_max": 10}),
        ),
    ])
}

fn fixture() -> Fixture {
    fixture_with(full_grants(), true, true, Some(("main", COMMUNITY_ID)))
}

fn rep_call(op: &str, args: serde_json::Value) -> HostCallBody {
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
        .handle(rep_call(op, args))
        .await
        .expect_err("expected a denial")
        .code
}

#[tokio::test]
async fn adjust_reaches_the_store_with_host_derived_scope_and_the_catalog_cap() {
    let f = fixture();
    f.store.answer(Ok(42));
    // Guest-supplied tenant/community fields must be ignored entirely.
    let out = f
        .caps
        .handle(rep_call(
            "reputation.adjust",
            serde_json::json!({
                "user": f.member.to_string(), "delta": 5, "reason": "game.win",
                "tenant_id": 999, "community_id": 999, "app_id": "evil",
            }),
        ))
        .await
        .unwrap();
    assert_eq!(out, serde_json::json!({ "balance": 42 }));
    let calls = f.store.calls.lock().unwrap();
    assert_eq!(calls.len(), 1);
    let (op, scope, user, adjust) = &calls[0];
    assert_eq!(op, "adjust");
    assert_eq!(
        scope,
        &ReputationScope {
            tenant_id: TENANT_ID,
            community_id: COMMUNITY_ID,
            app_id: APP_ID.to_string()
        }
    );
    assert_eq!(*user, f.member);
    assert_eq!(
        adjust.as_ref().unwrap(),
        &(5, "game.win".to_string(), reputation_caps())
    );
    assert!(
        reputation_caps().per_user_daily_abs_max > 0
            && reputation_caps().per_scope_daily_abs_max > 0,
        "catalog caps must be non-zero"
    );
}

#[tokio::test]
async fn get_reaches_the_store_and_returns_the_balance() {
    let f = fixture();
    f.store.answer(Ok(7));
    let out = f
        .caps
        .handle(rep_call(
            "reputation.get",
            serde_json::json!({ "user": f.member.to_string() }),
        ))
        .await
        .unwrap();
    assert_eq!(out, serde_json::json!({ "balance": 7 }));
    assert_eq!(f.store.calls.lock().unwrap()[0].0, "get");
}

#[tokio::test]
async fn an_ungranted_call_is_denied_not_granted_and_never_reaches_the_store() {
    let f = fixture_with(grants(&[]), true, true, Some(("main", COMMUNITY_ID)));
    for (op, args) in [
        (
            "reputation.adjust",
            serde_json::json!({"user": f.member.to_string(), "delta": 1, "reason": "r"}),
        ),
        (
            "reputation.get",
            serde_json::json!({"user": f.member.to_string()}),
        ),
    ] {
        assert_eq!(code_of(&f, op, args).await, "not_granted");
    }
    assert_eq!(f.store.call_count(), 0);
}

/// Read grant alone must not authorize a write (distinct permissions).
#[tokio::test]
async fn read_grant_does_not_authorize_adjust() {
    let f = fixture_with(
        grants(&[("reputation.read", serde_json::json!({}))]),
        true,
        true,
        Some(("main", COMMUNITY_ID)),
    );
    let code = code_of(
        &f,
        "reputation.adjust",
        serde_json::json!({"user": f.member.to_string(), "delta": 1, "reason": "r"}),
    )
    .await;
    assert_eq!(code, "not_granted");
    assert_eq!(f.store.call_count(), 0);
}

#[tokio::test]
async fn a_non_member_target_is_denied_by_the_gate_before_the_store() {
    let f = fixture();
    let stranger = Uuid::new_v4();
    let code = code_of(
        &f,
        "reputation.adjust",
        serde_json::json!({"user": stranger.to_string(), "delta": 1, "reason": "r"}),
    )
    .await;
    assert_eq!(code, "user_not_in_scope");
    assert_eq!(f.store.call_count(), 0);
}

#[tokio::test]
async fn a_delta_outside_the_declared_bounds_is_denied_before_the_store() {
    let f = fixture();
    // declared bounds are [-5, 10]
    for delta in [11, -6] {
        let code = code_of(
            &f,
            "reputation.adjust",
            serde_json::json!({"user": f.member.to_string(), "delta": delta, "reason": "r"}),
        )
        .await;
        assert_eq!(code, "delta_out_of_bounds", "delta {delta}");
    }
    assert_eq!(f.store.call_count(), 0);
}

#[tokio::test]
async fn flag_off_and_unwired_both_fail_loud_after_the_gate() {
    let off = fixture_with(full_grants(), false, true, Some(("main", COMMUNITY_ID)));
    let args = serde_json::json!({"user": off.member.to_string(), "delta": 1, "reason": "r"});
    assert_eq!(
        code_of(&off, "reputation.adjust", args.clone()).await,
        "feature_disabled"
    );
    assert_eq!(off.store.call_count(), 0);

    let unwired = fixture_with(full_grants(), true, false, Some(("main", COMMUNITY_ID)));
    let args = serde_json::json!({"user": unwired.member.to_string(), "delta": 1, "reason": "r"});
    assert_eq!(
        code_of(&unwired, "reputation.adjust", args).await,
        "not_implemented"
    );
}

/// Ungranted + unwired reports `not_granted` (gate first), never
/// `not_implemented` -- the db/http arms' established ordering.
#[tokio::test]
async fn gate_runs_before_the_wiring_check() {
    let f = fixture_with(grants(&[]), true, false, Some(("main", COMMUNITY_ID)));
    let code = code_of(
        &f,
        "reputation.get",
        serde_json::json!({"user": f.member.to_string()}),
    )
    .await;
    assert_eq!(code, "not_granted");
}

#[tokio::test]
async fn malformed_arguments_are_invalid_args() {
    let f = fixture();
    let m = f.member.to_string();
    for (op, args) in [
        ("reputation.get", serde_json::json!({})),
        ("reputation.get", serde_json::json!({"user": "not-a-uuid"})),
        ("reputation.get", serde_json::json!({"user": 5})),
        (
            "reputation.adjust",
            serde_json::json!({"user": m, "reason": "r"}),
        ),
        (
            "reputation.adjust",
            serde_json::json!({"user": m, "delta": 1}),
        ),
        (
            "reputation.adjust",
            serde_json::json!({"user": m, "delta": 1, "reason": "Has Space"}),
        ),
        (
            "reputation.adjust",
            serde_json::json!({"user": m, "delta": 99999999999_i64, "reason": "r"}),
        ),
        // regression: pr-741 review -- a zero delta is rejected loudly, before
        // the gate's quotas and the store's transaction.
        (
            "reputation.adjust",
            serde_json::json!({"user": m, "delta": 0, "reason": "r"}),
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
async fn a_tenant_wide_activation_has_no_community_to_score_in() {
    let f = fixture_with(full_grants(), true, true, None);
    let code = code_of(
        &f,
        "reputation.get",
        serde_json::json!({"user": f.member.to_string()}),
    )
    .await;
    assert_eq!(code, "invalid_args");
    assert_eq!(f.store.call_count(), 0);
}

#[tokio::test]
async fn unknown_reputation_op_is_denied() {
    let f = fixture();
    assert_eq!(
        code_of(&f, "reputation.reset", serde_json::json!({})).await,
        "unknown_op"
    );
}

#[tokio::test]
async fn store_errors_map_to_their_wire_codes_and_backend_detail_never_leaks() {
    let f = fixture();
    let args = serde_json::json!({"user": f.member.to_string(), "delta": 1, "reason": "r"});
    for (err, want) in [
        (ReputationError::NotAMember, "not_a_member"),
        (
            ReputationError::DailyCapExceeded { cap: 1 },
            "daily_cap_exceeded",
        ),
        (
            ReputationError::ScopeQuotaExceeded { cap: 1 },
            "quota_exceeded",
        ),
        (ReputationError::Invalid("x".into()), "invalid_args"),
    ] {
        f.store.answer(Err(err));
        assert_eq!(code_of(&f, "reputation.adjust", args.clone()).await, want);
    }
    f.store.answer(Err(ReputationError::Backend(
        "password=hunter2 host=db".into(),
    )));
    let denied = f
        .caps
        .handle(rep_call("reputation.adjust", args))
        .await
        .unwrap_err();
    assert_eq!(denied.code, "backend");
    assert!(!denied.message.contains("hunter2"), "{}", denied.message);
}

/// `reputation.*` must route to its own handler, never the `storage.tables`
/// path -- proven by granting ONLY `storage.tables` and observing
/// `not_granted` for the reputation permission rather than a db result.
#[tokio::test]
async fn reputation_ops_do_not_ride_the_storage_tables_permission() {
    let f = fixture_with(
        grants(&[("storage.tables", serde_json::json!({}))]),
        true,
        true,
        Some(("main", COMMUNITY_ID)),
    );
    let code = code_of(
        &f,
        "reputation.get",
        serde_json::json!({"user": f.member.to_string()}),
    )
    .await;
    assert_eq!(code, "not_granted");
    assert_eq!(f.store.call_count(), 0);
}

#[test]
fn daily_caps_match_the_catalog_per_user_and_per_scope_ceilings() {
    let Quota::ReputationDelta {
        per_user_daily_abs_max,
        per_scope_daily_abs_max,
        ..
    } = PermissionFamily::ReputationCommunityWrite
        .catalog_entry()
        .default_quota
    else {
        panic!("reputation.community.write must carry a ReputationDelta quota");
    };
    assert_eq!(
        reputation_caps(),
        ReputationCaps {
            per_user_daily_abs_max,
            per_scope_daily_abs_max
        }
    );
}

// ---------------------------------------------------------------------------
// P5 Task 1: override / adjust_many / get_many / leaderboard
// ---------------------------------------------------------------------------

const EVENT_ID: &str = "evt-1";

fn admin_grants() -> GrantSet {
    grants(&[
        ("reputation.read", serde_json::json!({})),
        (
            "reputation.community.write",
            serde_json::json!({"delta_min": -5, "delta_max": 5}),
        ),
        ("reputation.community.admin", serde_json::json!({})),
    ])
}

/// A fixture whose invocation carries a replay-stable event id (the mutating
/// ops derive their idempotency key from it).
fn ext_fixture(grant_set: GrantSet) -> Fixture {
    let mut f = fixture_with(grant_set, true, true, Some(("main", COMMUNITY_ID)));
    f.caps.invocation_identity = Some(Arc::new(
        InvocationIdentity::new("twitch", None, vec![]).with_event_id(EVENT_ID),
    ));
    f
}

fn rep_scope() -> ReputationScope {
    ReputationScope {
        tenant_id: TENANT_ID,
        community_id: COMMUNITY_ID,
        app_id: APP_ID.to_string(),
    }
}

#[tokio::test]
async fn override_needs_the_admin_grant_not_just_write() {
    let f = ext_fixture(full_grants()); // read + community.write, NO admin
    let args = serde_json::json!({
        "user": f.member.to_string(), "absolute_score": 100, "reason": "admin.fix",
    });
    assert_eq!(
        code_of(&f, "reputation.override", args).await,
        "not_granted"
    );
    assert_eq!(f.store.ext_count(), 0);
}

#[tokio::test]
async fn override_reaches_the_store_with_host_scope_and_an_event_derived_key() {
    let f = ext_fixture(admin_grants());
    f.store.answer(Ok(100));
    let args = serde_json::json!({
        "user": f.member.to_string(), "absolute_score": 100, "reason": "admin.fix",
        "tenant_id": 999, "community_id": 999,
    });
    let out = f
        .caps
        .handle(rep_call("reputation.override", args.clone()))
        .await
        .unwrap();
    assert_eq!(out, serde_json::json!({ "balance": 100 }));
    // A second distinct override in the same invocation gets the next ordinal.
    f.caps
        .handle(rep_call("reputation.override", args))
        .await
        .unwrap();
    let calls = f.store.ext_calls();
    let key = |n: u32| format!("{EVENT_ID}:reputation_override:{n}");
    assert_eq!(
        calls,
        vec![
            ExtCall::Override {
                scope: rep_scope(),
                user: f.member,
                absolute: 100,
                reason: "admin.fix".into(),
                key: key(0),
            },
            ExtCall::Override {
                scope: rep_scope(),
                user: f.member,
                absolute: 100,
                reason: "admin.fix".into(),
                key: key(1),
            },
        ]
    );
}

#[tokio::test]
async fn override_is_gated_on_membership_and_validates_arguments() {
    let f = ext_fixture(admin_grants());
    assert_eq!(
        code_of(
            &f,
            "reputation.override",
            serde_json::json!({"user": Uuid::new_v4().to_string(), "absolute_score": 1, "reason": "r"})
        )
        .await,
        "user_not_in_scope"
    );
    for args in [
        serde_json::json!({"user": "nope", "absolute_score": 1, "reason": "r"}),
        serde_json::json!({"user": f.member.to_string(), "absolute_score": "x", "reason": "r"}),
        serde_json::json!({"user": f.member.to_string(), "absolute_score": 1, "reason": "Bad Reason"}),
        serde_json::json!({"user": f.member.to_string(), "absolute_score": 1}),
    ] {
        assert_eq!(
            code_of(&f, "reputation.override", args).await,
            "invalid_args"
        );
    }
    assert_eq!(f.store.ext_count(), 0);
}

#[tokio::test]
async fn a_mutation_without_a_replay_stable_event_id_is_refused_not_unkeyed() {
    // Plain fixture: no invocation identity at all.
    let f = fixture_with(admin_grants(), true, true, Some(("main", COMMUNITY_ID)));
    let args = serde_json::json!({
        "user": f.member.to_string(), "absolute_score": 1, "reason": "r",
    });
    assert_eq!(
        code_of(&f, "reputation.override", args).await,
        "not_implemented"
    );
    let args = serde_json::json!({
        "adjustments": [{"user": f.member.to_string(), "delta": 1}], "reason": "r",
    });
    assert_eq!(
        code_of(&f, "reputation.adjust_many", args).await,
        "not_implemented"
    );
    assert_eq!(f.store.ext_count(), 0);
}

#[tokio::test]
async fn a_backend_failure_gives_the_ordinal_back_so_a_retry_reuses_the_key() {
    let f = ext_fixture(admin_grants());
    let args = serde_json::json!({
        "user": f.member.to_string(), "absolute_score": 5, "reason": "r",
    });
    f.store.answer(Err(ReputationError::Backend("boom".into())));
    assert_eq!(
        code_of(&f, "reputation.override", args.clone()).await,
        "backend"
    );
    f.caps
        .handle(rep_call("reputation.override", args))
        .await
        .unwrap();
    let keys: Vec<String> = f
        .store
        .ext_calls()
        .into_iter()
        .map(|c| match c {
            ExtCall::Override { key, .. } => key,
            other => panic!("{other:?}"),
        })
        .collect();
    assert_eq!(
        keys[0], keys[1],
        "the retry presents the same idempotency key"
    );
}

#[tokio::test]
async fn adjust_many_gates_every_item_and_reaches_the_store_with_catalog_caps() {
    let f = ext_fixture(admin_grants());
    let b = f.other;
    let args = serde_json::json!({
        "adjustments": [
            {"user": f.member.to_string(), "delta": 3},
            {"user": b.to_string(), "delta": -2},
        ],
        "reason": "game.round",
    });
    let out = f
        .caps
        .handle(rep_call("reputation.adjust_many", args))
        .await
        .unwrap();
    assert_eq!(out, serde_json::json!({}));
    assert_eq!(
        f.store.ext_calls(),
        vec![ExtCall::AdjustMany {
            scope: rep_scope(),
            items: vec![(f.member, 3), (b, -2)],
            reason: "game.round".into(),
            caps: reputation_caps(),
            key: format!("{EVENT_ID}:reputation_adjust_many:0"),
        }]
    );
}

#[tokio::test]
async fn adjust_many_refuses_the_whole_batch_when_any_item_is_denied() {
    let f = ext_fixture(admin_grants()); // declared bounds [-5, 5]
    let items = |second: serde_json::Value| {
        serde_json::json!({
            "adjustments": [{"user": f.member.to_string(), "delta": 1}, second],
            "reason": "r",
        })
    };
    // out-of-bounds delta on item 2
    assert_eq!(
        code_of(
            &f,
            "reputation.adjust_many",
            items(serde_json::json!({"user": f.member.to_string(), "delta": 6}))
        )
        .await,
        "invalid_args", // duplicate user is rejected at parse, before the gate
    );
    let other = f.other;
    assert_eq!(
        code_of(
            &f,
            "reputation.adjust_many",
            items(serde_json::json!({"user": other.to_string(), "delta": 6}))
        )
        .await,
        "delta_out_of_bounds"
    );
    // non-member on item 2
    assert_eq!(
        code_of(
            &f,
            "reputation.adjust_many",
            items(serde_json::json!({"user": Uuid::new_v4().to_string(), "delta": 1}))
        )
        .await,
        "user_not_in_scope"
    );
    assert_eq!(f.store.ext_count(), 0, "nothing reached the store");
}

#[tokio::test]
async fn adjust_many_validates_batch_shape() {
    let f = ext_fixture(admin_grants());
    let one = serde_json::json!({"user": f.member.to_string(), "delta": 1});
    let oversize: Vec<serde_json::Value> = (0..=bundle_host_reputation::MAX_BATCH)
        .map(|_| serde_json::json!({"user": Uuid::new_v4().to_string(), "delta": 1}))
        .collect();
    for args in [
        serde_json::json!({"adjustments": [], "reason": "r"}),
        serde_json::json!({"adjustments": oversize, "reason": "r"}),
        serde_json::json!({"adjustments": [{"user": f.member.to_string(), "delta": 0}], "reason": "r"}),
        serde_json::json!({"adjustments": [one.clone(), one.clone()], "reason": "r"}),
        serde_json::json!({"adjustments": [one.clone()], "reason": ""}),
        serde_json::json!({"reason": "r"}),
    ] {
        assert_eq!(
            code_of(&f, "reputation.adjust_many", args).await,
            "invalid_args"
        );
    }
    assert_eq!(f.store.ext_count(), 0);
}

#[tokio::test]
async fn get_many_returns_entries_and_gates_every_user() {
    let f = ext_fixture(admin_grants());
    f.store
        .next_entries
        .lock()
        .unwrap()
        .replace(Ok(vec![bundle_host_reputation::ScoreEntry {
            user: f.member,
            score: 9,
        }]));
    let out = f
        .caps
        .handle(rep_call(
            "reputation.get_many",
            serde_json::json!({"users": [f.member.to_string()]}),
        ))
        .await
        .unwrap();
    assert_eq!(
        out,
        serde_json::json!({"entries": [{"user": f.member.to_string(), "score": 9}]})
    );
    assert_eq!(
        f.store.ext_calls(),
        vec![ExtCall::GetMany(rep_scope(), vec![f.member])]
    );
    assert_eq!(
        code_of(
            &f,
            "reputation.get_many",
            serde_json::json!({"users": [f.member.to_string(), Uuid::new_v4().to_string()]})
        )
        .await,
        "user_not_in_scope"
    );
    for bad in [
        serde_json::json!({"users": []}),
        serde_json::json!({"users": ["nope"]}),
        serde_json::json!({}),
    ] {
        assert_eq!(
            code_of(&f, "reputation.get_many", bad).await,
            "invalid_args"
        );
    }
    let oversize: Vec<String> = (0..=bundle_host_reputation::MAX_BATCH)
        .map(|_| Uuid::new_v4().to_string())
        .collect();
    assert_eq!(
        code_of(
            &f,
            "reputation.get_many",
            serde_json::json!({"users": oversize})
        )
        .await,
        "invalid_args"
    );
}

#[tokio::test]
async fn leaderboard_needs_read_and_bounds_its_limit() {
    let f = ext_fixture(admin_grants());
    f.store
        .next_entries
        .lock()
        .unwrap()
        .replace(Ok(vec![bundle_host_reputation::ScoreEntry {
            user: f.member,
            score: 4,
        }]));
    let out = f
        .caps
        .handle(rep_call(
            "reputation.leaderboard",
            serde_json::json!({"limit": 10}),
        ))
        .await
        .unwrap();
    assert_eq!(
        out,
        serde_json::json!({"entries": [{"user": f.member.to_string(), "score": 4}]})
    );
    assert_eq!(
        f.store.ext_calls(),
        vec![ExtCall::Leaderboard(rep_scope(), 10)]
    );
    for bad in [0, 101] {
        assert_eq!(
            code_of(
                &f,
                "reputation.leaderboard",
                serde_json::json!({"limit": bad})
            )
            .await,
            "invalid_args"
        );
    }
    let none = ext_fixture(grants(&[]));
    assert_eq!(
        code_of(
            &none,
            "reputation.leaderboard",
            serde_json::json!({"limit": 5})
        )
        .await,
        "not_granted"
    );
    assert_eq!(none.store.ext_count(), 0);
}

#[tokio::test]
async fn extension_ops_fail_loud_on_flag_off_unwired_and_tenant_wide_activation() {
    let off = fixture_with(admin_grants(), false, true, Some(("main", COMMUNITY_ID)));
    assert_eq!(
        code_of(
            &off,
            "reputation.leaderboard",
            serde_json::json!({"limit": 5})
        )
        .await,
        "feature_disabled"
    );
    let unwired = fixture_with(admin_grants(), true, false, Some(("main", COMMUNITY_ID)));
    assert_eq!(
        code_of(
            &unwired,
            "reputation.leaderboard",
            serde_json::json!({"limit": 5})
        )
        .await,
        "not_implemented"
    );
    let tenant_wide = fixture_with(admin_grants(), true, true, None);
    assert_eq!(
        code_of(
            &tenant_wide,
            "reputation.leaderboard",
            serde_json::json!({"limit": 5})
        )
        .await,
        "invalid_args"
    );
}

/// A store failure code maps onto the stable wire code (idempotency conflict
/// reaches the guest as `denied(idempotency_conflict)`).
#[tokio::test]
async fn an_idempotency_conflict_surfaces_with_its_stable_code() {
    let f = ext_fixture(admin_grants());
    f.store.answer(Err(ReputationError::IdempotencyConflict));
    let args = serde_json::json!({
        "user": f.member.to_string(), "absolute_score": 1, "reason": "r",
    });
    assert_eq!(
        code_of(&f, "reputation.override", args).await,
        "idempotency_conflict"
    );
}
