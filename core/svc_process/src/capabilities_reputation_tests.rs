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
    Apply {
        scope: ReputationScope,
        user: Uuid,
        delta: i32,
        class: bundle_host_reputation::ReasonClass,
        source: String,
        caps: ReputationCaps,
        key: String,
    },
    GetTenant(ReputationScope, Uuid),
    TenantPolicy(ReputationScope),
    History(ReputationScope, Uuid, u32, Option<i64>),
    Below(ReputationScope, i64, u32),
    IsSoftBanned(ReputationScope, Uuid),
    SoftBan(ReputationScope, Uuid, String),
    LiftSoftBan(ReputationScope, Uuid, String),
}

/// Records every store call; answers with a canned result.
#[derive(Default)]
struct RecordingStore {
    calls: Mutex<Vec<RecordedCall>>,
    next: Mutex<Option<Result<i64, ReputationError>>>,
    ext: Mutex<Vec<ExtCall>>,
    next_unit: Mutex<Option<Result<(), ReputationError>>>,
    next_entries: Mutex<Option<Result<Vec<bundle_host_reputation::ScoreEntry>, ReputationError>>>,
    /// When set, the next soft-ban call answers `RateLimited { cap }`.
    toggle_cap: Mutex<Option<i64>>,
    /// What `active_logic_app` answers: `None` = the test app itself (so
    /// pre-exclusivity tests keep passing), `Some(x)` = exactly `x`.
    active_logic: Mutex<Option<Option<String>>>,
    /// Idempotency keys `adjust` was called with.
    adjust_keys: Mutex<Vec<String>>,
}

impl RecordingStore {
    fn answer(&self, r: Result<i64, ReputationError>) {
        *self.next.lock().unwrap() = Some(r);
    }
    fn answer_unit(&self, r: Result<(), ReputationError>) {
        *self.next_unit.lock().unwrap() = Some(r);
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
    fn active_logic_app<'a>(
        &'a self,
        _scope: &'a ReputationScope,
    ) -> bundle_host_reputation::BoxFuture<'a, Result<Option<String>, ReputationError>> {
        Box::pin(async move {
            Ok(self
                .active_logic
                .lock()
                .unwrap()
                .clone()
                .unwrap_or_else(|| Some(APP_ID.to_string())))
        })
    }
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
        key: &'a bundle_host_reputation::IdempotencyKey,
    ) -> bundle_host_reputation::BoxFuture<'a, Result<i64, ReputationError>> {
        Box::pin(async move {
            self.adjust_keys
                .lock()
                .unwrap()
                .push(key.as_str().to_string());
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

    fn apply<'a>(
        &'a self,
        scope: &'a ReputationScope,
        user: Uuid,
        delta: i32,
        class: bundle_host_reputation::ReasonClass,
        source_event: &'a str,
        caps: ReputationCaps,
        key: &'a bundle_host_reputation::IdempotencyKey,
    ) -> bundle_host_reputation::BoxFuture<
        'a,
        Result<bundle_host_reputation::ApplyOutcome, ReputationError>,
    > {
        Box::pin(async move {
            self.ext.lock().unwrap().push(ExtCall::Apply {
                scope: scope.clone(),
                user,
                delta,
                class,
                source: source_event.to_string(),
                caps,
                key: key.as_str().to_string(),
            });
            self.take()
                .map(|new_score| bundle_host_reputation::ApplyOutcome {
                    new_score,
                    applied: true,
                })
        })
    }
    fn get_tenant<'a>(
        &'a self,
        scope: &'a ReputationScope,
        user: Uuid,
    ) -> bundle_host_reputation::BoxFuture<
        'a,
        Result<bundle_host_reputation::TenantScore, ReputationError>,
    > {
        Box::pin(async move {
            self.ext
                .lock()
                .unwrap()
                .push(ExtCall::GetTenant(scope.clone(), user));
            self.take()
                .map(|score| bundle_host_reputation::TenantScore {
                    score,
                    communities: 2,
                })
        })
    }
    fn tenant_policy<'a>(
        &'a self,
        scope: &'a ReputationScope,
    ) -> bundle_host_reputation::BoxFuture<
        'a,
        Result<Option<bundle_host_reputation::TenantPolicy>, ReputationError>,
    > {
        Box::pin(async move {
            self.ext
                .lock()
                .unwrap()
                .push(ExtCall::TenantPolicy(scope.clone()));
            Ok(Some(bundle_host_reputation::TenantPolicy {
                auto_ban_enabled: false,
                auto_ban_floor: 350,
                at_risk_threshold: 450,
            }))
        })
    }
    fn history<'a>(
        &'a self,
        scope: &'a ReputationScope,
        user: Uuid,
        limit: u32,
        before: Option<i64>,
    ) -> bundle_host_reputation::BoxFuture<
        'a,
        Result<Vec<bundle_host_reputation::HistoryEntry>, ReputationError>,
    > {
        Box::pin(async move {
            self.ext
                .lock()
                .unwrap()
                .push(ExtCall::History(scope.clone(), user, limit, before));
            Ok(vec![bundle_host_reputation::HistoryEntry {
                id: 9,
                delta: -3,
                reason_code: "activity:evt".into(),
                kind: "adjust".into(),
                balance_after: Some(597),
                occurred_at: "2026-11-11T00:00:00Z".into(),
            }])
        })
    }
    fn below_threshold<'a>(
        &'a self,
        scope: &'a ReputationScope,
        threshold: i64,
        limit: u32,
    ) -> bundle_host_reputation::BoxFuture<
        'a,
        Result<Vec<bundle_host_reputation::ScoreEntry>, ReputationError>,
    > {
        Box::pin(async move {
            self.ext
                .lock()
                .unwrap()
                .push(ExtCall::Below(scope.clone(), threshold, limit));
            Ok(vec![])
        })
    }
    fn is_soft_banned<'a>(
        &'a self,
        scope: &'a ReputationScope,
        user: Uuid,
    ) -> bundle_host_reputation::BoxFuture<'a, Result<bool, ReputationError>> {
        Box::pin(async move {
            self.ext
                .lock()
                .unwrap()
                .push(ExtCall::IsSoftBanned(scope.clone(), user));
            Ok(true)
        })
    }
    fn soft_ban<'a>(
        &'a self,
        scope: &'a ReputationScope,
        user: Uuid,
        reason: &'a str,
    ) -> bundle_host_reputation::BoxFuture<'a, Result<bool, ReputationError>> {
        Box::pin(async move {
            self.ext.lock().unwrap().push(ExtCall::SoftBan(
                scope.clone(),
                user,
                reason.to_string(),
            ));
            Ok(true)
        })
    }
    fn lift_soft_ban<'a>(
        &'a self,
        scope: &'a ReputationScope,
        user: Uuid,
        only_reason: &'a str,
    ) -> bundle_host_reputation::BoxFuture<'a, Result<bool, ReputationError>> {
        Box::pin(async move {
            self.ext.lock().unwrap().push(ExtCall::LiftSoftBan(
                scope.clone(),
                user,
                only_reason.to_string(),
            ));
            match self.toggle_cap.lock().unwrap().take() {
                Some(cap) => Err(ReputationError::RateLimited { cap }),
                None => Ok(true),
            }
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
    fixture_app(APP_ID, grant_set, flag_on, wired, community)
}

/// [`fixture_with`] for an arbitrary `app_id` (core vs third-party).
fn fixture_app(
    app_id: &str,
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
            app_id: app_id.to_string(),
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
        app_id.to_string(),
        TENANT_ID,
        community.map_or(0, |c| c.1),
        VERSION,
        egress,
        gate,
    );
    // Every write needs a replay-stable event id (the idempotency key's anchor).
    caps.invocation_identity = Some(Arc::new(
        InvocationIdentity::new("twitch", None, vec![]).with_event_id(EVENT_ID),
    ));
    let store = Arc::new(RecordingStore::default());
    if wired {
        caps = caps.with_reputation(ReputationWiring {
            store: Arc::clone(&store) as Arc<dyn ReputationStore>,
            flag: Arc::new(FixedGate(flag_on)),
            auto_ban_flag: None,
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
            app_id: APP_ID.to_string(),
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

const ACTOR_UUID: &str = "11111111-1111-4111-8111-111111111111";

/// Membership directory double: one platform account holding `role`
/// (`None` = no role on record -> `NotLinked`, never defaulted).
struct RoleDirectory {
    role: Option<&'static str>,
}

impl crate::identity::MemberDirectory for RoleDirectory {
    fn member_by_platform_id<'a>(
        &'a self,
        _scope: crate::identity::IdentityScope,
        _platform: &'a str,
        _platform_user_id: &'a str,
    ) -> crate::identity::BoxFuture<'a, Result<Uuid, crate::identity::IdentityError>> {
        Box::pin(async move { Ok(Uuid::parse_str(ACTOR_UUID).unwrap()) })
    }
    fn role_for<'a>(
        &'a self,
        _scope: crate::identity::IdentityScope,
        _platform: &'a str,
        _platform_user_id: &'a str,
    ) -> crate::identity::BoxFuture<'a, Result<String, crate::identity::IdentityError>> {
        Box::pin(async move {
            self.role
                .map(str::to_string)
                .ok_or(crate::identity::IdentityError::NotLinked)
        })
    }
    fn confirm_member<'a>(
        &'a self,
        _scope: crate::identity::IdentityScope,
        user: Uuid,
    ) -> crate::identity::BoxFuture<'a, Result<Uuid, crate::identity::IdentityError>> {
        Box::pin(async move { Ok(user) })
    }
}

/// A fixture whose invocation carries a replay-stable event id (the mutating
/// ops derive their idempotency key from it) and a host-verified actor holding
/// the community role `role` (`None` = actor with no role on record).
fn ext_fixture_as(grant_set: GrantSet, role: Option<&'static str>) -> Fixture {
    let Fixture {
        caps,
        store,
        member,
        other,
    } = fixture_with(grant_set, true, true, Some(("main", COMMUNITY_ID)));
    let caps = caps.with_identity(
        crate::identity::IdentityWiring {
            directory: Arc::new(RoleDirectory { role }),
            handles: None,
            flag: Arc::new(FixedGate(true)),
        },
        Arc::new(
            InvocationIdentity::new("twitch", Some("1001".to_string()), vec![])
                .with_event_id(EVENT_ID),
        ),
    );
    Fixture {
        caps,
        store,
        member,
        other,
    }
}

/// [`ext_fixture_as`] with a community admin (the legitimate `override` caller).
fn ext_fixture(grant_set: GrantSet) -> Fixture {
    ext_fixture_as(grant_set, Some("community-admin"))
}

fn rep_scope() -> ReputationScope {
    ReputationScope {
        tenant_id: TENANT_ID,
        community_id: COMMUNITY_ID,
        app_id: APP_ID.to_string(),
    }
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

/// regression: #983 review -- a batch refused at a LATER item must hand back the
/// gate quota already consumed by its earlier (authorized) items, or a caller
/// can burn a member's daily budget with batches that never apply.
#[tokio::test]
async fn a_refused_batch_refunds_the_quota_its_earlier_items_consumed() {
    let f = ext_fixture(admin_grants()); // per-user daily cap 5, per-call bound 5
    for _ in 0..20 {
        assert_eq!(
            code_of(
                &f,
                "reputation.adjust_many",
                serde_json::json!({
                    "adjustments": [
                        {"user": f.member.to_string(), "delta": 5},
                        {"user": f.other.to_string(), "delta": 6}, // out of bounds
                    ],
                    "reason": "r",
                })
            )
            .await,
            "delta_out_of_bounds"
        );
    }
    // The member's full daily budget is still intact.
    f.caps
        .handle(rep_call(
            "reputation.adjust_many",
            serde_json::json!({
                "adjustments": [{"user": f.member.to_string(), "delta": 5}],
                "reason": "r",
            }),
        ))
        .await
        .expect("budget must not have been burned by refused batches");
    assert_eq!(f.store.ext_count(), 1);
}

/// MAGNITUDE quota: a batch the store definitively refuses (all-or-nothing,
/// nothing applied) hands its points back, so refused batches cannot drain the
/// shared scope/per-user budget.
#[tokio::test]
async fn a_batch_the_store_refuses_outright_refunds_its_magnitude_quota() {
    let f = ext_fixture(admin_grants()); // per-user daily cap 5
    let body = |u: Uuid| {
        serde_json::json!({
            "adjustments": [{"user": u.to_string(), "delta": 5}], "reason": "r",
        })
    };
    for _ in 0..20 {
        f.store.answer_unit(Err(ReputationError::NotAMember));
        assert_eq!(
            code_of(&f, "reputation.adjust_many", body(f.member)).await,
            "not_a_member"
        );
    }
    f.store.answer_unit(Ok(()));
    f.caps
        .handle(rep_call("reputation.adjust_many", body(f.member)))
        .await
        .expect("magnitude budget intact after store refusals");
}

/// An indeterminate backend failure MAY have committed, so its magnitude stays
/// charged (never refunded).
#[tokio::test]
async fn a_backend_failure_keeps_its_magnitude_charged() {
    let f = ext_fixture(admin_grants());
    let body = serde_json::json!({
        "adjustments": [{"user": f.member.to_string(), "delta": 5}], "reason": "r",
    });
    f.store
        .answer_unit(Err(ReputationError::Backend("boom".into())));
    assert_eq!(
        code_of(&f, "reputation.adjust_many", body.clone()).await,
        "backend"
    );
    assert_eq!(
        code_of(&f, "reputation.adjust_many", body).await,
        "quota_exceeded"
    );
}

/// CALL-RATE limit on the serialized per-community lane: charged on EVERY
/// `adjust_many` call and NEVER refunded, so a loop of refused batches is cut
/// off after N calls even though each refund keeps the magnitude budget whole.
#[tokio::test]
async fn looping_refused_batches_is_stopped_by_the_never_refunded_call_rate_limit() {
    const N: usize = 30; // the gate's adjust_many calls/min
                         // (1) gate-side refusals (one bad target per batch).
    let f = ext_fixture(admin_grants());
    let bad = serde_json::json!({
        "adjustments": [
            {"user": f.member.to_string(), "delta": 5},
            {"user": f.other.to_string(), "delta": 6},
        ],
        "reason": "r",
    });
    for n in 0..N {
        assert_eq!(
            code_of(&f, "reputation.adjust_many", bad.clone()).await,
            "delta_out_of_bounds",
            "call {n}"
        );
    }
    for _ in 0..20 {
        assert_eq!(
            code_of(&f, "reputation.adjust_many", bad.clone()).await,
            "rate_limited"
        );
    }
    // (2) store-side refusals: the store is reached at most N times.
    let g = ext_fixture(admin_grants());
    let one = serde_json::json!({
        "adjustments": [{"user": g.member.to_string(), "delta": 1}], "reason": "r",
    });
    for _ in 0..(N + 20) {
        g.store.answer_unit(Err(ReputationError::NotAMember));
        let _ = g
            .caps
            .handle(rep_call("reputation.adjust_many", one.clone()))
            .await;
    }
    assert_eq!(g.store.ext_count(), N, "the lane saw at most N calls");
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

// ---- review #983: caller scope enforced host-side on override ----

fn override_args_for(user: Uuid) -> serde_json::Value {
    serde_json::json!({
        "user": user.to_string(), "absolute_score": 100, "reason": "admin.fix",
    })
}

fn override_args(f: &Fixture) -> serde_json::Value {
    override_args_for(f.member)
}

/// regression: #983 review -- the bundle holds `reputation.community.admin`, but
/// a non-admin caller reaching `override-score` (any other tool, or the WIT
/// import called directly) must be refused by the HOST, never reaching the store.
#[tokio::test]
async fn override_by_a_non_admin_caller_is_forbidden_even_with_the_admin_grant() {
    for role in [
        Some("member"),
        Some("moderator"),
        Some("vip"),
        Some("bogus"),
        None,
    ] {
        let f = ext_fixture_as(admin_grants(), role);
        assert_eq!(
            code_of(&f, "reputation.override", override_args(&f)).await,
            "forbidden",
            "role {role:?}"
        );
        assert_eq!(f.store.ext_count(), 0, "role {role:?}");
    }
}

#[tokio::test]
async fn override_with_no_verified_actor_at_all_is_forbidden() {
    // No identity wiring and no actor account: fail closed.
    let mut f = fixture_with(admin_grants(), true, true, Some(("main", COMMUNITY_ID)));
    f.caps.invocation_identity = Some(Arc::new(
        InvocationIdentity::new("twitch", None, vec![]).with_event_id(EVENT_ID),
    ));
    assert_eq!(
        code_of(&f, "reputation.override", override_args(&f)).await,
        "forbidden"
    );
    assert_eq!(f.store.ext_count(), 0);
}

/// A fixture whose invocation is SYSTEM-originated (a platform event such as a
/// follow / the automatic floor evaluation): no human actor scope is required.
fn sys_fixture(grant_set: GrantSet) -> Fixture {
    let f = ext_fixture_as(grant_set, None);
    let mut f = f;
    f.caps.invocation_identity = Some(Arc::new(
        InvocationIdentity::new("twitch", Some("1001".to_string()), vec![])
            .with_event_id(EVENT_ID)
            .with_system_originated(true),
    ));
    f
}

fn v2_grants() -> GrantSet {
    grants(&[
        ("reputation.read", serde_json::json!({})),
        (
            "reputation.community.write",
            serde_json::json!({"delta_min": -5, "delta_max": 5}),
        ),
        (
            "reputation.moderation.write",
            serde_json::json!({"delta_min": -200, "delta_max": 200}),
        ),
        ("reputation.history.read", serde_json::json!({})),
        ("reputation.tenant.read", serde_json::json!({})),
    ])
}

#[tokio::test]
async fn apply_derives_the_activity_class_and_catalog_caps_on_the_host() {
    let f = sys_fixture(v2_grants());
    f.store.answer(Ok(605));
    // A guest-supplied `class`/`tenant_id` field is ignored entirely.
    let args = serde_json::json!({
        "user": f.member.to_string(), "delta": 5, "reason": "activity.follow",
        "class": "moderation", "tenant_id": 999,
    });
    let out = f
        .caps
        .handle(rep_call("reputation.apply", args))
        .await
        .unwrap();
    assert_eq!(out, serde_json::json!({"new_score": 605, "applied": true}));
    let caps = reputation_caps_for(PermissionFamily::ReputationCommunityWrite);
    assert_eq!(
        f.store.ext_calls(),
        vec![ExtCall::Apply {
            scope: rep_scope(),
            user: f.member,
            delta: 5,
            class: bundle_host_reputation::ReasonClass::Activity,
            source: "activity.follow".into(),
            caps,
            key: format!("{EVENT_ID}:reputation_apply:0"),
        }]
    );
}

#[tokio::test]
async fn apply_activity_is_bounded_to_five_and_moderation_needs_its_own_grant() {
    let f = sys_fixture(v2_grants());
    let mk =
        |d: i64| serde_json::json!({"user": f.member.to_string(), "delta": d, "reason": "r.x"});
    // Activity op: a moderation-sized delta is refused before the store.
    assert_eq!(
        code_of(&f, "reputation.apply", mk(-200)).await,
        "invalid_args"
    );
    assert_eq!(f.store.ext_count(), 0);
    // Moderation op: -200 passes with the grant and carries the moderation class + caps.
    f.store.answer(Ok(400));
    f.caps
        .handle(rep_call("reputation.apply_moderation", mk(-200)))
        .await
        .unwrap();
    let calls = f.store.ext_calls();
    assert!(matches!(
        &calls[0],
        ExtCall::Apply { class: bundle_host_reputation::ReasonClass::Moderation, delta: -200, caps, .. }
            if *caps == reputation_caps_for(PermissionFamily::ReputationModerationWrite)
    ));
    // Without `reputation.moderation.write` the same call is not_granted.
    let g = ext_fixture(full_grants());
    assert_eq!(
        code_of(&g, "reputation.apply_moderation", mk(-200)).await,
        "not_granted"
    );
    assert_eq!(g.store.ext_count(), 0);
}

#[tokio::test]
async fn core_only_ops_are_forbidden_for_a_third_party_app_even_when_granted() {
    let f = fixture_app(
        "acme.third-party",
        v2_grants(),
        true,
        true,
        Some(("main", COMMUNITY_ID)),
    );
    let u = f.member.to_string();
    let cases = [
        (
            "reputation.history",
            serde_json::json!({"user": u, "limit": 5}),
        ),
        ("reputation.get_tenant", serde_json::json!({"user": u})),
        ("reputation.get_tenant_policy", serde_json::json!({})),
        (
            "reputation.soft_ban",
            serde_json::json!({"user": u, "reason": "community_soft_floor"}),
        ),
        (
            "reputation.lift_soft_ban",
            serde_json::json!({"user": u, "only_reason": "community_soft_floor"}),
        ),
        (
            "reputation.apply_moderation",
            serde_json::json!({"user": u, "delta": -10, "reason": "m.x"}),
        ),
    ];
    for (op, args) in cases {
        assert_eq!(code_of(&f, op, args).await, "forbidden", "{op}");
    }
    assert_eq!(f.store.ext_count(), 0);
}

#[tokio::test]
async fn core_read_ops_need_their_grants_and_reach_the_store_with_host_scope() {
    // Not granted: read-only grants cannot reach history / tenant reads.
    let g = ext_fixture(full_grants());
    let u = g.member.to_string();
    assert_eq!(
        code_of(
            &g,
            "reputation.history",
            serde_json::json!({"user": u, "limit": 5})
        )
        .await,
        "not_granted"
    );
    assert_eq!(
        code_of(&g, "reputation.get_tenant", serde_json::json!({"user": u})).await,
        "not_granted"
    );
    assert_eq!(g.store.ext_count(), 0);

    let f = sys_fixture(v2_grants());
    let u = f.member.to_string();
    f.store.answer(Ok(512));
    let out = f
        .caps
        .handle(rep_call(
            "reputation.get_tenant",
            serde_json::json!({"user": u}),
        ))
        .await
        .unwrap();
    assert_eq!(out, serde_json::json!({"score": 512, "communities": 2}));
    let h = f
        .caps
        .handle(rep_call(
            "reputation.history",
            serde_json::json!({"user": u, "limit": 10, "before": 50}),
        ))
        .await
        .unwrap();
    assert_eq!(h["entries"][0]["id"], 9);
    let p = f
        .caps
        .handle(rep_call(
            "reputation.get_tenant_policy",
            serde_json::json!({}),
        ))
        .await
        .unwrap();
    assert_eq!(p["policy"]["auto_ban_floor"], 350);
    assert_eq!(
        f.store.ext_calls(),
        vec![
            ExtCall::GetTenant(rep_scope(), f.member),
            ExtCall::History(rep_scope(), f.member, 10, Some(50)),
            ExtCall::TenantPolicy(rep_scope()),
        ]
    );
    // Argument validation: limit bounds, non-positive cursor.
    for args in [
        serde_json::json!({"user": u, "limit": 0}),
        serde_json::json!({"user": u, "limit": 101}),
        serde_json::json!({"user": u, "limit": 5, "before": 0}),
    ] {
        assert_eq!(
            code_of(&f, "reputation.history", args).await,
            "invalid_args"
        );
    }
}

#[tokio::test]
async fn community_tenant_isolation_a_non_member_target_is_refused_before_the_store() {
    let f = sys_fixture(v2_grants());
    let stranger = Uuid::new_v4().to_string();
    for (op, args) in [
        (
            "reputation.get_tenant",
            serde_json::json!({"user": stranger}),
        ),
        (
            "reputation.history",
            serde_json::json!({"user": stranger, "limit": 5}),
        ),
        (
            "reputation.is_soft_banned",
            serde_json::json!({"user": stranger}),
        ),
    ] {
        assert_eq!(code_of(&f, op, args).await, "user_not_in_scope", "{op}");
    }
    assert_eq!(f.store.ext_count(), 0);
}

#[tokio::test]
async fn soft_ban_ops_and_below_threshold_validate_and_route() {
    let f = sys_fixture(v2_grants());
    let u = f.member.to_string();
    f.caps
        .handle(rep_call(
            "reputation.is_soft_banned",
            serde_json::json!({"user": u}),
        ))
        .await
        .unwrap();
    f.caps
        .handle(rep_call(
            "reputation.soft_ban",
            serde_json::json!({"user": u, "reason": "community_soft_floor"}),
        ))
        .await
        .unwrap();
    f.caps
        .handle(rep_call(
            "reputation.lift_soft_ban",
            serde_json::json!({"user": u, "only_reason": "community_soft_floor"}),
        ))
        .await
        .unwrap();
    f.caps
        .handle(rep_call(
            "reputation.below_threshold",
            serde_json::json!({"threshold": 450, "limit": 20}),
        ))
        .await
        .unwrap();
    assert_eq!(
        f.store.ext_calls(),
        vec![
            ExtCall::IsSoftBanned(rep_scope(), f.member),
            ExtCall::SoftBan(rep_scope(), f.member, "community_soft_floor".into()),
            ExtCall::LiftSoftBan(rep_scope(), f.member, "community_soft_floor".into()),
            ExtCall::Below(rep_scope(), 450, 20),
        ]
    );
    for args in [
        serde_json::json!({"threshold": 299, "limit": 5}),
        serde_json::json!({"threshold": 900, "limit": 5}),
        serde_json::json!({"threshold": 450, "limit": 101}),
    ] {
        assert_eq!(
            code_of(&f, "reputation.below_threshold", args).await,
            "invalid_args"
        );
    }
    // Bad reason (free text) rejected.
    assert_eq!(
        code_of(
            &f,
            "reputation.soft_ban",
            serde_json::json!({"user": u, "reason": "Bad Reason"})
        )
        .await,
        "invalid_args"
    );
}

#[tokio::test]
async fn every_write_requires_an_idempotency_key_typed_invalid_args() {
    // No event id on the invocation -> invalid_args (never a random/unkeyed
    // write that a retry would double-count), nothing reaches the store.
    let mut f = fixture_with(v2_grants(), true, true, Some(("main", COMMUNITY_ID)));
    f.caps.invocation_identity = None;
    let u = f.member.to_string();
    for (op, args) in [
        (
            "reputation.apply",
            serde_json::json!({"user": u, "delta": 1, "reason": "r.x"}),
        ),
        (
            "reputation.adjust",
            serde_json::json!({"user": u, "delta": 1, "reason": "r.x"}),
        ),
        (
            "reputation.adjust_many",
            serde_json::json!({"adjustments": [{"user": u, "delta": 1}], "reason": "r.x"}),
        ),
    ] {
        assert_eq!(code_of(&f, op, args).await, "invalid_args", "{op}");
    }
    assert_eq!(f.store.ext_count(), 0);
    assert!(f.store.calls.lock().unwrap().is_empty());
}

fn human_fixture(role: Option<&'static str>) -> Fixture {
    // Human-originated (chat command): `ext_fixture_as` builds a non-system invocation.
    ext_fixture_as(v2_grants(), role)
}

fn mod_args(f: &Fixture) -> [(&'static str, serde_json::Value); 3] {
    let u = f.member.to_string();
    [
        (
            "reputation.apply_moderation",
            serde_json::json!({"user": u, "delta": -50, "reason": "moderation.ban"}),
        ),
        (
            "reputation.soft_ban",
            serde_json::json!({"user": u, "reason": "community_soft_floor"}),
        ),
        (
            "reputation.lift_soft_ban",
            serde_json::json!({"user": u, "only_reason": "community_soft_floor"}),
        ),
    ]
}

#[tokio::test]
async fn a_non_moderator_human_actor_is_denied_every_moderation_write() {
    for role in [None, Some("member"), Some("vip")] {
        let f = human_fixture(role);
        for (op, args) in mod_args(&f) {
            assert_eq!(code_of(&f, op, args).await, "forbidden", "{role:?} {op}");
        }
        assert_eq!(f.store.ext_count(), 0, "{role:?}");
    }
}

#[tokio::test]
async fn a_moderator_or_admin_human_actor_may_make_moderation_writes() {
    for role in [Some("moderator"), Some("community-admin")] {
        let f = human_fixture(role);
        f.store.answer(Ok(550));
        for (op, args) in mod_args(&f) {
            f.caps
                .handle(rep_call(op, args))
                .await
                .unwrap_or_else(|e| panic!("{role:?} {op}: {e:?}"));
        }
        assert_eq!(f.store.ext_count(), 3, "{role:?}");
    }
}

#[tokio::test]
async fn a_system_originated_invocation_needs_no_human_actor_scope() {
    // The automatic floor evaluation / a moderation outcome: no human actor.
    let f = sys_fixture(v2_grants());
    f.store.answer(Ok(500));
    for (op, args) in mod_args(&f) {
        f.caps
            .handle(rep_call(op, args))
            .await
            .unwrap_or_else(|e| panic!("{op}: {e:?}"));
    }
    assert_eq!(f.store.ext_count(), 3);
}

#[tokio::test]
async fn a_guest_supplied_system_flag_cannot_bypass_the_scope_check() {
    // A human-originated, non-moderator invocation: the guest smuggles
    // `system`/`system_originated` into every arg position; the flag is
    // host-derived only, so it is ignored and the call is refused.
    let f = human_fixture(Some("member"));
    let u = f.member.to_string();
    for flag in ["system", "system_originated", "origin", "automatic"] {
        let mut args = serde_json::json!({"user": u, "delta": -50, "reason": "moderation.ban"});
        args[flag] = serde_json::json!(true);
        assert_eq!(
            code_of(&f, "reputation.apply_moderation", args).await,
            "forbidden",
            "{flag}"
        );
    }
    assert_eq!(f.store.ext_count(), 0);
}

#[tokio::test]
async fn soft_ban_lift_quota_exhaustion_is_a_typed_rate_limit_and_a_ban_still_applies() {
    let f = sys_fixture(v2_grants());
    let u = f.member.to_string();
    // The store reports the durable LIFT quota exhausted: typed, not generic.
    *f.store.toggle_cap.lock().unwrap() = Some(5);
    assert_eq!(
        code_of(
            &f,
            "reputation.lift_soft_ban",
            serde_json::json!({"user": u, "only_reason": "community_soft_floor"})
        )
        .await,
        "rate_limited"
    );
    // Recording a (floor-triggered) soft ban is never quota-blocked.
    f.caps
        .handle(rep_call(
            "reputation.soft_ban",
            serde_json::json!({"user": u, "reason": "community_soft_floor"}),
        ))
        .await
        .unwrap();
    // The bound is the store's DURABLE count quota (proven against Postgres in
    // `soft_ban_toggles_are_quota_limited_per_user_and_per_community_and_typed`);
    // there is no gate-side refund that could hand a toggle back.
    assert_eq!(f.store.ext_count(), 2);
}

#[tokio::test]
async fn real_human_event_types_are_never_system_originated_so_the_scope_check_applies() {
    for et in [
        "message",
        "interaction.command",
        "chat.message",
        "weird.unknown",
    ] {
        let mut f = ext_fixture_as(v2_grants(), Some("member"));
        f.caps.invocation_identity = Some(Arc::new(
            InvocationIdentity::new("discord", Some("1001".to_string()), vec![])
                .with_event_id(EVENT_ID)
                .with_system_originated(crate::identity::is_system_event_type(et)),
        ));
        let u = f.member.to_string();
        assert_eq!(
            code_of(
                &f,
                "reputation.soft_ban",
                serde_json::json!({"user": u, "reason": "community_soft_floor"})
            )
            .await,
            "forbidden",
            "{et}"
        );
    }
    // The allowlisted platform event passes without a human actor scope.
    let mut f = ext_fixture_as(v2_grants(), Some("member"));
    f.caps.invocation_identity = Some(Arc::new(
        InvocationIdentity::new("twitch", Some("1001".to_string()), vec![])
            .with_event_id(EVENT_ID)
            .with_system_originated(crate::identity::is_system_event_type("channel.follow")),
    ));
    let u = f.member.to_string();
    assert!(f
        .caps
        .handle(rep_call(
            "reputation.soft_ban",
            serde_json::json!({"user": u, "reason": "community_soft_floor"})
        ))
        .await
        .is_ok());
}

#[tokio::test]
async fn a_system_flag_stripped_by_an_untrusted_hop_makes_the_core_write_forbidden() {
    // A third-party logic bundle tool.invokes a core service: the host strips the
    // flag (`for_tool_call`), so the core bundle's moderation write needs a real
    // moderator actor scope.
    let base = InvocationIdentity::new("twitch", Some("1001".to_string()), vec![])
        .with_event_id(EVENT_ID)
        .with_system_originated(true);
    let hop = base.for_tool_call(
        Some("tool:k".into()),
        "acme.third-party-logic",
        "waddles.core.service.reputation",
    );
    assert!(!hop.is_system_originated());
    let mut f = ext_fixture_as(v2_grants(), Some("member"));
    f.caps.invocation_identity = Some(Arc::new(hop));
    let u = f.member.to_string();
    assert_eq!(
        code_of(
            &f,
            "reputation.apply_moderation",
            serde_json::json!({"user": u, "delta": -50, "reason": "moderation.ban"})
        )
        .await,
        "forbidden"
    );
    assert_eq!(f.store.ext_count(), 0);
}

#[tokio::test]
async fn a_positive_moderation_delta_is_forbidden_for_everyone_including_admins() {
    let args = |f: &Fixture| serde_json::json!({"user": f.member.to_string(), "delta": 30, "reason": "moderation.restore"});
    // System-originated (no human actor): still forbidden.
    let f = sys_fixture(v2_grants());
    assert_eq!(
        code_of(&f, "reputation.apply_moderation", args(&f)).await,
        "forbidden"
    );
    // A moderator (human): moderation write is allowed but positive is not.
    let f = human_fixture(Some("moderator"));
    assert_eq!(
        code_of(&f, "reputation.apply_moderation", args(&f)).await,
        "forbidden"
    );
    assert_eq!(f.store.ext_count(), 0);
    // Even a verified admin: a positive moderation delta is an admin_override
    // (class 3), denied to the bundle role -> hub-api only.
    let f = human_fixture(Some("community-admin"));
    assert_eq!(
        code_of(&f, "reputation.apply_moderation", args(&f)).await,
        "forbidden"
    );
    assert_eq!(f.store.ext_count(), 0);
    // Negative moderation is unaffected for a system-originated invocation.
    let f = sys_fixture(v2_grants());
    f.store.answer(Ok(550));
    assert!(f
        .caps
        .handle(rep_call(
            "reputation.apply_moderation",
            serde_json::json!({"user": f.member.to_string(), "delta": -50, "reason": "moderation.ban"})
        ))
        .await
        .is_ok());
}

#[tokio::test]
async fn only_a_successful_apply_marks_the_invocation_for_the_automated_floor_gate() {
    let f = sys_fixture(v2_grants());
    let applied = |f: &Fixture| {
        f.caps
            .invocation_identity
            .as_deref()
            .is_some_and(|i| i.reputation_applied())
    };
    assert!(!applied(&f), "nothing applied yet");
    // A refused apply (store failure) never marks the invocation.
    f.store.answer(Err(ReputationError::Backend("down".into())));
    let args = serde_json::json!({
        "user": f.member.to_string(), "delta": 1, "reason": "activity.follow",
    });
    assert!(f
        .caps
        .handle(rep_call("reputation.apply", args.clone()))
        .await
        .is_err());
    assert!(!applied(&f), "a failed apply is not a successful apply");
    // A read never marks it either.
    f.store.answer(Ok(600));
    f.caps
        .handle(rep_call(
            "reputation.get_tenant",
            serde_json::json!({"user": f.member.to_string()}),
        ))
        .await
        .ok();
    assert!(!applied(&f));
    // A successful apply does.
    f.store.answer(Ok(601));
    f.caps
        .handle(rep_call("reputation.apply", args))
        .await
        .expect("apply succeeds");
    assert!(applied(&f));
}

/// Sets what the store's `reputation_active_logic_app` answers.
fn set_active_logic(f: &Fixture, v: Option<&str>) {
    *f.store.active_logic.lock().unwrap() = Some(v.map(str::to_string));
}

#[tokio::test]
async fn activity_writes_are_forbidden_unless_the_caller_is_the_active_logic_bundle() {
    let activity = |f: &Fixture| {
        [
            (
                "reputation.apply",
                serde_json::json!({"user": f.member.to_string(), "delta": 1, "reason": "activity.follow"}),
            ),
            (
                "reputation.adjust",
                serde_json::json!({"user": f.member.to_string(), "delta": 1, "reason": "game.win"}),
            ),
            (
                "reputation.adjust_many",
                serde_json::json!({"adjustments": [{"user": f.member.to_string(), "delta": 1}], "reason": "game.round"}),
            ),
        ]
    };
    // Another app is the active logic bundle, or there is none (fail closed):
    // forbidden, and the store's write path is never reached.
    for active in [Some("acme.other-logic"), None] {
        let f = sys_fixture(v2_grants());
        set_active_logic(&f, active);
        for (op, args) in activity(&f) {
            assert_eq!(
                code_of(&f, op, args).await,
                "forbidden",
                "{op} / {active:?}"
            );
        }
        assert_eq!(f.store.ext_count(), 0);
        assert!(f.store.calls.lock().unwrap().is_empty());
    }
    // The active logic bundle itself is allowed.
    let f = sys_fixture(v2_grants());
    set_active_logic(&f, Some(APP_ID));
    f.store.answer(Ok(601));
    f.caps
        .handle(rep_call(
            "reputation.apply",
            serde_json::json!({"user": f.member.to_string(), "delta": 1, "reason": "activity.follow"}),
        ))
        .await
        .expect("the active logic bundle may apply");
}

#[tokio::test]
async fn moderation_class_is_not_subject_to_logic_exclusivity() {
    // Moderation has its own permission / core-only / actor-scope gates;
    // exclusivity is for ACTIVITY writes only.
    let f = sys_fixture(v2_grants());
    set_active_logic(&f, Some("acme.other-logic"));
    f.store.answer(Ok(500));
    f.caps
        .handle(rep_call(
            "reputation.apply_moderation",
            serde_json::json!({"user": f.member.to_string(), "delta": -10, "reason": "moderation.ban"}),
        ))
        .await
        .expect("moderation write is gated elsewhere");
}

#[tokio::test]
async fn a_bundle_can_only_set_or_clear_the_floor_soft_ban_never_a_manual_one() {
    let f = sys_fixture(v2_grants());
    let u = f.member.to_string();
    for (op, args) in [
        (
            "reputation.soft_ban",
            serde_json::json!({"user": u, "reason": "manual.admin-ban"}),
        ),
        (
            "reputation.lift_soft_ban",
            serde_json::json!({"user": u, "only_reason": "manual.admin-ban"}),
        ),
    ] {
        assert_eq!(code_of(&f, op, args).await, "forbidden", "{op}");
    }
    assert_eq!(
        f.store.ext_count(),
        0,
        "no manual soft-ban write reached the store"
    );
}

/// Admin override is `admin_override` (class 3): the SQL denies it to the bundle
/// role, so the host refuses it for EVERY caller, admin grant or not, before the
/// store is touched.
#[tokio::test]
async fn override_is_hub_api_only_even_for_a_verified_admin_with_the_admin_grant() {
    let f = ext_fixture(admin_grants());
    assert_eq!(
        code_of(&f, "reputation.override", override_args(&f)).await,
        "forbidden"
    );
    assert_eq!(f.store.ext_count(), 0);
}

#[tokio::test]
async fn adjust_keys_are_event_derived_distinct_per_call_and_reused_on_a_backend_retry() {
    let f = fixture();
    let args = serde_json::json!({"user": f.member.to_string(), "delta": 1, "reason": "r.x"});
    f.store.answer(Err(ReputationError::Backend("boom".into())));
    assert_eq!(
        code_of(&f, "reputation.adjust", args.clone()).await,
        "backend"
    );
    f.store.answer(Ok(601));
    f.caps
        .handle(rep_call("reputation.adjust", args.clone()))
        .await
        .unwrap();
    f.store.answer(Ok(602));
    f.caps
        .handle(rep_call("reputation.adjust", args))
        .await
        .unwrap();
    let keys = f.store.adjust_keys.lock().unwrap().clone();
    assert_eq!(keys[0], format!("{EVENT_ID}:reputation_adjust:0"));
    assert_eq!(keys[1], keys[0], "a failed attempt's retry reuses the key");
    assert_eq!(keys[2], format!("{EVENT_ID}:reputation_adjust:1"));
}

#[tokio::test]
async fn activity_writes_fail_closed_on_flag_off_and_absent_wiring_before_exclusivity_is_consulted()
{
    for (flag_on, wired, want) in [
        (false, true, "feature_disabled"),
        (true, false, "not_implemented"),
    ] {
        let f = fixture_with(v2_grants(), flag_on, wired, Some(("main", COMMUNITY_ID)));
        let u = f.member.to_string();
        for (op, args) in [
            (
                "reputation.adjust",
                serde_json::json!({"user": u, "delta": 1, "reason": "r.x"}),
            ),
            (
                "reputation.adjust_many",
                serde_json::json!({"adjustments": [{"user": u, "delta": 1}], "reason": "r.x"}),
            ),
            (
                "reputation.apply",
                serde_json::json!({"user": u, "delta": 1, "reason": "r.x"}),
            ),
        ] {
            assert_eq!(
                code_of(&f, op, args).await,
                want,
                "{op} flag_on={flag_on} wired={wired}"
            );
        }
        assert_eq!(f.store.ext_count(), 0);
        assert!(f.store.calls.lock().unwrap().is_empty());
    }
}
