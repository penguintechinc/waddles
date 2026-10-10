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

/// Records every store call; answers with a canned result.
#[derive(Default)]
struct RecordingStore {
    calls: Mutex<Vec<RecordedCall>>,
    next: Mutex<Option<Result<i64, ReputationError>>>,
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
}

struct Fixture {
    caps: StageCapabilities,
    store: Arc<RecordingStore>,
    member: Uuid,
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
