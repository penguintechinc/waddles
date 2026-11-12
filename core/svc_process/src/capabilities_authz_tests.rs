//! `authz.caller_can` host-capability tests: the REAL `handle` dispatch run
//! against a scripted membership directory. Same fixture recipe as
//! `capabilities_identity_tests.rs`; no gate grant is needed for this op.

use super::*;
use crate::identity::{
    BoxFuture as IdentityFuture, HandleResolver, MemberDirectory, MentionBinding,
};
use crate::license::test_support::FixedGate;
use bundle_capability_gate::{
    GrantScopeKey, GrantSet, InMemoryGrantSnapshot, InMemoryInstancePolicySnapshot,
    InMemoryMembership, InMemoryQuotaLedger,
};
use bundle_host_http::egress::{ReqwestTransport, StaticFlag};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;
use uuid::Uuid;

const TENANT_ID: i32 = 7;
const COMMUNITY_ID: i32 = 3;
const APP_ID: &str = "waddles.core.test-authz";
const ACTOR: &str = "1001";

/// Directory with a scripted role answer and call counters.
struct RoleDirectory {
    role: Mutex<Result<String, IdentityError>>,
    member: Mutex<Result<Uuid, IdentityError>>,
    role_calls: AtomicUsize,
    member_calls: AtomicUsize,
}

impl RoleDirectory {
    fn new(role: Result<String, IdentityError>) -> Arc<Self> {
        Arc::new(Self {
            role: Mutex::new(role),
            member: Mutex::new(Ok(Uuid::new_v4())),
            role_calls: AtomicUsize::new(0),
            member_calls: AtomicUsize::new(0),
        })
    }
}

impl MemberDirectory for RoleDirectory {
    fn role_for<'a>(
        &'a self,
        _scope: IdentityScope,
        _platform: &'a str,
        _platform_user_id: &'a str,
    ) -> IdentityFuture<'a, Result<String, IdentityError>> {
        Box::pin(async move {
            self.role_calls.fetch_add(1, Ordering::SeqCst);
            self.role.lock().unwrap().clone()
        })
    }

    fn member_by_platform_id<'a>(
        &'a self,
        _scope: IdentityScope,
        _platform: &'a str,
        _platform_user_id: &'a str,
    ) -> IdentityFuture<'a, Result<Uuid, IdentityError>> {
        Box::pin(async move {
            self.member_calls.fetch_add(1, Ordering::SeqCst);
            self.member.lock().unwrap().clone()
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

struct NoHandles;
impl HandleResolver for NoHandles {
    fn resolve_handle<'a>(
        &'a self,
        _tenant_id: i32,
        _platform: &'a str,
        _reference: &'a str,
    ) -> IdentityFuture<'a, Result<Uuid, IdentityError>> {
        Box::pin(async { Err(IdentityError::NotFound) })
    }
}

/// Builds capabilities with an EMPTY grant set (authz needs none).
fn caps_with(
    directory: &Arc<RoleDirectory>,
    actor: Option<&str>,
    flag_on: bool,
    wired: bool,
    community: Option<(&str, i32)>,
) -> StageCapabilities {
    let snapshot = InMemoryGrantSnapshot::new();
    snapshot.set(
        GrantScopeKey {
            tenant_id: TENANT_ID,
            community_id: community.map_or(0, |c| c.1),
            app_id: APP_ID.to_string(),
            app_version: 1,
        },
        GrantSet {
            permission_snapshot_hash: "test".to_string(),
            grants: Default::default(),
        },
    );
    let gate = Arc::new(CapabilityGate::new(
        Arc::new(snapshot),
        Arc::new(InMemoryMembership::new()),
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
            prometheus::Opts::new("test_authz_egress_denied_total", "test"),
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
        1,
        egress,
        gate,
    );
    if wired {
        caps = caps.with_identity(
            IdentityWiring {
                directory: Arc::clone(directory) as Arc<dyn MemberDirectory>,
                handles: Some(Arc::new(NoHandles) as Arc<dyn HandleResolver>),
                flag: Arc::new(FixedGate(flag_on)),
            },
            Arc::new(InvocationIdentity::new(
                "twitch",
                actor.map(str::to_string),
                Vec::<MentionBinding>::new(),
            )),
        );
    }
    caps
}

fn caps_for_role(role: &str) -> (StageCapabilities, Arc<RoleDirectory>) {
    let d = RoleDirectory::new(Ok(role.to_string()));
    let c = caps_with(&d, Some(ACTOR), true, true, Some(("main", COMMUNITY_ID)));
    (c, d)
}

fn call(scope: serde_json::Value) -> HostCallBody {
    HostCallBody {
        app_id: APP_ID.to_string(),
        capability: CapabilityKind::Db,
        op: "authz.caller_can".to_string(),
        args: serde_json::json!({ "scope": scope }),
        call_id: 1,
    }
}

async fn allowed(caps: &StageCapabilities, scope: &str) -> bool {
    let out = caps.handle(call(serde_json::json!(scope))).await.unwrap();
    out["allowed"].as_bool().expect("allowed is a bool")
}

const ALL: [&str; 7] = [
    "community:member",
    "community:vip",
    "community:moderate",
    "community:ban",
    "community:timeout",
    "community:admin",
    "community:owner",
];

#[tokio::test]
async fn moderator_can_ban_and_timeout_but_not_admin() {
    let (c, _) = caps_for_role("moderator");
    assert!(allowed(&c, "community:ban").await);
    assert!(allowed(&c, "community:timeout").await);
    assert!(allowed(&c, "community:moderate").await);
    assert!(!allowed(&c, "community:admin").await);
    assert!(!allowed(&c, "community:owner").await);
    assert!(!allowed(&c, "community:vip").await);
}

#[tokio::test]
async fn admin_and_owner_hold_what_the_map_says() {
    let (c, _) = caps_for_role("community-admin");
    for s in &ALL[..6] {
        assert!(allowed(&c, s).await, "{s}");
    }
    assert!(!allowed(&c, "community:owner").await);
    for role in ["community-owner", "owner"] {
        let (c, _) = caps_for_role(role);
        for s in ALL {
            assert!(allowed(&c, s).await, "{role} {s}");
        }
    }
}

#[tokio::test]
async fn member_holds_only_member_and_unknown_role_holds_nothing() {
    let (c, _) = caps_for_role("member");
    assert!(allowed(&c, "community:member").await);
    for s in &ALL[1..] {
        assert!(!allowed(&c, s).await, "{s}");
    }
    let (c, _) = caps_for_role("superuser");
    for s in ALL {
        assert!(!allowed(&c, s).await, "{s}");
    }
}

#[tokio::test]
async fn an_unknown_but_well_formed_scope_is_false() {
    let (c, _) = caps_for_role("owner");
    assert!(!allowed(&c, "community:nope").await);
}

#[tokio::test]
async fn no_actor_unlinked_and_non_member_are_false_not_errors() {
    let d = RoleDirectory::new(Ok("owner".into()));
    let c = caps_with(&d, None, true, true, Some(("main", COMMUNITY_ID)));
    assert!(!allowed(&c, "community:admin").await);

    for err in [IdentityError::NotLinked, IdentityError::NotAMember] {
        let d = RoleDirectory::new(Ok("owner".into()));
        *d.member.lock().unwrap() = Err(err);
        let c = caps_with(&d, Some(ACTOR), true, true, Some(("main", COMMUNITY_ID)));
        assert!(!allowed(&c, "community:admin").await);
    }
    let d = RoleDirectory::new(Err(IdentityError::NotAMember));
    let c = caps_with(&d, Some(ACTOR), true, true, Some(("main", COMMUNITY_ID)));
    assert!(!allowed(&c, "community:member").await);
}

#[tokio::test]
async fn a_non_community_activation_is_false() {
    let d = RoleDirectory::new(Ok("owner".into()));
    let c = caps_with(&d, Some(ACTOR), true, true, None);
    assert!(!allowed(&c, "community:admin").await);
    assert_eq!(d.role_calls.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn the_role_is_cached_for_the_invocation_one_directory_call() {
    let (c, d) = caps_for_role("moderator");
    for s in ALL {
        let _ = allowed(&c, s).await;
    }
    assert_eq!(d.role_calls.load(Ordering::SeqCst), 1, "one role lookup");
    assert_eq!(d.member_calls.load(Ordering::SeqCst), 1, "one actor lookup");
}

#[tokio::test]
async fn directory_failures_are_errors_never_a_default_allow_or_deny() {
    let d = RoleDirectory::new(Err(IdentityError::Unavailable("down".into())));
    let c = caps_with(&d, Some(ACTOR), true, true, Some(("main", COMMUNITY_ID)));
    assert_eq!(
        c.handle(call(serde_json::json!("community:admin")))
            .await
            .unwrap_err()
            .code,
        "unavailable"
    );
    let d = RoleDirectory::new(Err(IdentityError::Backend("boom".into())));
    let c = caps_with(&d, Some(ACTOR), true, true, Some(("main", COMMUNITY_ID)));
    assert_eq!(
        c.handle(call(serde_json::json!("community:admin")))
            .await
            .unwrap_err()
            .code,
        "backend"
    );
}

#[tokio::test]
async fn bad_scopes_are_invalid_args_before_any_directory_call() {
    let (c, d) = caps_for_role("owner");
    for bad in [
        serde_json::json!(""),
        serde_json::json!("admin"),
        serde_json::json!("Community:admin"),
        serde_json::json!("a:b:c"),
        serde_json::json!(format!("a:{}", "b".repeat(63))),
        serde_json::json!(7),
        serde_json::json!(null),
    ] {
        assert_eq!(
            c.handle(call(bad.clone())).await.unwrap_err().code,
            "invalid_args",
            "{bad}"
        );
    }
    assert_eq!(d.role_calls.load(Ordering::SeqCst), 0);
    assert_eq!(d.member_calls.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn unwired_and_flag_off_fail_loud_and_guest_args_cannot_pick_the_actor() {
    let d = RoleDirectory::new(Ok("owner".into()));
    let c = caps_with(&d, Some(ACTOR), true, false, Some(("main", COMMUNITY_ID)));
    assert_eq!(
        c.handle(call(serde_json::json!("community:admin")))
            .await
            .unwrap_err()
            .code,
        "not_implemented"
    );
    let c = caps_with(&d, Some(ACTOR), false, true, Some(("main", COMMUNITY_ID)));
    assert_eq!(
        c.handle(call(serde_json::json!("community:admin")))
            .await
            .unwrap_err()
            .code,
        "feature_disabled"
    );
    // Extra guest-supplied fields are ignored: the actor is host-derived.
    let (c, _) = caps_for_role("member");
    let mut body = call(serde_json::json!("community:admin"));
    body.args = serde_json::json!({
        "scope": "community:admin", "actor": "x", "role": "owner",
        "tenant_id": 999, "user": Uuid::new_v4().to_string(),
    });
    assert_eq!(
        c.handle(body).await.unwrap(),
        serde_json::json!({ "allowed": false })
    );
}

#[tokio::test]
async fn an_unknown_authz_op_is_refused() {
    let (c, _) = caps_for_role("owner");
    let mut body = call(serde_json::json!("community:admin"));
    body.op = "authz.other".to_string();
    assert_eq!(c.handle(body).await.unwrap_err().code, "unknown_op");
}
