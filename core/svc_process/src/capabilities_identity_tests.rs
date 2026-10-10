//! `identity.*` host-capability tests: the REAL [`CapabilityGate`] (grant, rate
//! limit, instance policy) and the REAL `handle` dispatch run against scripted
//! directory / hub-api resolver doubles. The directory's own SQL is proven
//! against a real Postgres, with the shipped view DDL, in
//! `tests/identity_pg_e2e.rs`; the executor half (a real wasm component calling
//! the import and emitting this exact wire shape) is
//! `core/bundle_executor/tests/stage_next_identity.rs`.

use super::*;
use crate::identity::{
    BoxFuture as IdentityFuture, HandleResolver, MemberDirectory, MentionBinding, MentionRef,
};
use crate::license::test_support::FixedGate;
use bundle_capability_gate::{
    GrantScopeKey, GrantSet, GrantedPermission, InMemoryGrantSnapshot,
    InMemoryInstancePolicySnapshot, InMemoryMembership, InMemoryQuotaLedger,
};
use bundle_host_http::egress::{ReqwestTransport, StaticFlag};
use std::collections::{HashMap, HashSet};
use std::sync::Mutex;
use uuid::Uuid;

const TENANT_ID: i32 = 7;
const COMMUNITY_ID: i32 = 3;
const APP_ID: &str = "waddles.core.test-identity";
const VERSION: i64 = 1;
/// A raw platform account id and handle that must NEVER appear in a result.
const RAW_ACTOR_ID: &str = "1001";
const RAW_TARGET_ID: &str = "2002";
const RAW_HANDLE: &str = "secret_handle_bob";

/// Scripted directory: platform id -> answer, plus the set of confirmed
/// members. Records the scope of every lookup.
#[derive(Default)]
struct ScriptedDirectory {
    by_platform: Mutex<HashMap<String, Result<Uuid, IdentityError>>>,
    members: Mutex<HashSet<Uuid>>,
    scopes: Mutex<Vec<IdentityScope>>,
    platforms: Mutex<Vec<String>>,
}

impl ScriptedDirectory {
    fn set(&self, platform_user_id: &str, answer: Result<Uuid, IdentityError>) {
        self.by_platform
            .lock()
            .unwrap()
            .insert(platform_user_id.to_string(), answer);
    }
    fn lookups(&self) -> usize {
        self.scopes.lock().unwrap().len()
    }
}

impl MemberDirectory for ScriptedDirectory {
    fn member_by_platform_id<'a>(
        &'a self,
        scope: IdentityScope,
        platform: &'a str,
        platform_user_id: &'a str,
    ) -> IdentityFuture<'a, Result<Uuid, IdentityError>> {
        Box::pin(async move {
            self.scopes.lock().unwrap().push(scope);
            self.platforms.lock().unwrap().push(platform.to_string());
            self.by_platform
                .lock()
                .unwrap()
                .get(platform_user_id)
                .cloned()
                .unwrap_or(Err(IdentityError::NotAMember))
        })
    }

    fn confirm_member<'a>(
        &'a self,
        scope: IdentityScope,
        user: Uuid,
    ) -> IdentityFuture<'a, Result<Uuid, IdentityError>> {
        Box::pin(async move {
            self.scopes.lock().unwrap().push(scope);
            if self.members.lock().unwrap().contains(&user) {
                Ok(user)
            } else {
                Err(IdentityError::NotAMember)
            }
        })
    }
}

struct ScriptedHandles(Mutex<Option<Result<Uuid, IdentityError>>>);

impl HandleResolver for ScriptedHandles {
    fn resolve_handle<'a>(
        &'a self,
        _tenant_id: i32,
        _platform: &'a str,
        _reference: &'a str,
    ) -> IdentityFuture<'a, Result<Uuid, IdentityError>> {
        Box::pin(async move {
            self.0
                .lock()
                .unwrap()
                .clone()
                .unwrap_or(Err(IdentityError::NotFound))
        })
    }
}

struct Fixture {
    caps: StageCapabilities,
    directory: Arc<ScriptedDirectory>,
    alice: Uuid,
    bob: Uuid,
    /// The opaque tokens the bundle was shown for two mentions.
    id_token: String,
    handle_token: String,
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
            prometheus::Opts::new("test_identity_egress_denied_total", "test"),
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

    let (alice, bob) = (Uuid::new_v4(), Uuid::new_v4());
    let directory = Arc::new(ScriptedDirectory::default());
    directory.set(RAW_ACTOR_ID, Ok(alice));
    directory.set(RAW_TARGET_ID, Ok(bob));
    directory.members.lock().unwrap().insert(bob);
    let handles = Arc::new(ScriptedHandles(Mutex::new(Some(Ok(bob)))));

    let (id_token, handle_token) = (Uuid::new_v4().to_string(), Uuid::new_v4().to_string());
    if wired {
        let invocation = InvocationIdentity::new(
            "twitch",
            Some(RAW_ACTOR_ID.to_string()),
            vec![
                MentionBinding {
                    key: id_token.clone(),
                    reference: MentionRef::PlatformId(RAW_TARGET_ID.to_string()),
                },
                MentionBinding {
                    key: handle_token.clone(),
                    reference: MentionRef::Handle(RAW_HANDLE.to_string()),
                },
            ],
        );
        caps = caps.with_identity(
            IdentityWiring {
                directory: Arc::clone(&directory) as Arc<dyn MemberDirectory>,
                handles: Some(handles as Arc<dyn HandleResolver>),
                flag: Arc::new(FixedGate(flag_on)),
            },
            Arc::new(invocation),
        );
    }
    Fixture {
        caps,
        directory,
        alice,
        bob,
        id_token,
        handle_token,
    }
}

fn full_grants() -> GrantSet {
    grants(&[("identity.resolve", serde_json::json!({}))])
}

fn fixture() -> Fixture {
    fixture_with(full_grants(), true, true, Some(("main", COMMUNITY_ID)))
}

fn id_call(op: &str, args: serde_json::Value) -> HostCallBody {
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
        .handle(id_call(op, args))
        .await
        .expect_err("expected a refusal")
        .code
}

#[tokio::test]
async fn the_actor_resolves_to_its_community_user_uuid_and_only_that() {
    let f = fixture();
    let out = f
        .caps
        .handle(id_call("identity.resolve_actor", serde_json::json!({})))
        .await
        .unwrap();
    assert_eq!(out, serde_json::json!({ "user": f.alice.to_string() }));
    // The lookup ran under the HOST-derived scope and platform.
    assert_eq!(
        *f.directory.scopes.lock().unwrap(),
        vec![IdentityScope {
            tenant_id: TENANT_ID,
            community_id: COMMUNITY_ID
        }]
    );
    assert_eq!(*f.directory.platforms.lock().unwrap(), vec!["twitch"]);
}

#[tokio::test]
async fn guest_supplied_identity_scope_or_user_fields_are_ignored_entirely() {
    let f = fixture();
    let out = f
        .caps
        .handle(id_call(
            "identity.resolve_actor",
            serde_json::json!({
                "tenant_id": 999, "community_id": 999, "platform": "discord",
                "platform_user_id": RAW_TARGET_ID, "user": f.bob.to_string(),
                "actor": "someone-else", "app_id": "evil",
            }),
        ))
        .await
        .unwrap();
    // Still the triggering actor, under the host scope -- a bundle can never
    // ask about anyone but the person whose message triggered it.
    assert_eq!(out, serde_json::json!({ "user": f.alice.to_string() }));
    assert_eq!(
        *f.directory.scopes.lock().unwrap(),
        vec![IdentityScope {
            tenant_id: TENANT_ID,
            community_id: COMMUNITY_ID
        }]
    );
    assert_eq!(*f.directory.platforms.lock().unwrap(), vec!["twitch"]);
}

#[tokio::test]
async fn an_unresolved_actor_is_not_linked_never_a_pseudonym_or_default() {
    let f = fixture();
    f.directory.set(RAW_ACTOR_ID, Err(IdentityError::NotLinked));
    assert_eq!(
        code_of(&f, "identity.resolve_actor", serde_json::json!({})).await,
        "not_linked"
    );
}

#[tokio::test]
async fn a_non_member_actor_is_not_a_member() {
    let f = fixture();
    f.directory
        .set(RAW_ACTOR_ID, Err(IdentityError::NotAMember));
    assert_eq!(
        code_of(&f, "identity.resolve_actor", serde_json::json!({})).await,
        "not_a_member"
    );
}

#[tokio::test]
async fn a_platform_id_mention_resolves_by_its_token() {
    let f = fixture();
    let out = f
        .caps
        .handle(id_call(
            "identity.resolve_mention",
            serde_json::json!({ "token": f.id_token }),
        ))
        .await
        .unwrap();
    assert_eq!(out, serde_json::json!({ "user": f.bob.to_string() }));
}

#[tokio::test]
async fn a_handle_mention_resolves_through_the_hub_resolver_then_membership() {
    let f = fixture();
    let out = f
        .caps
        .handle(id_call(
            "identity.resolve_mention",
            serde_json::json!({ "token": f.handle_token }),
        ))
        .await
        .unwrap();
    assert_eq!(out, serde_json::json!({ "user": f.bob.to_string() }));
}

#[tokio::test]
async fn an_unknown_token_or_a_raw_handle_is_not_found_and_touches_nothing() {
    let f = fixture();
    for probe in [
        Uuid::new_v4().to_string(),
        format!("@{RAW_HANDLE}"),
        RAW_HANDLE.to_string(),
        RAW_TARGET_ID.to_string(),
    ] {
        assert_eq!(
            code_of(
                &f,
                "identity.resolve_mention",
                serde_json::json!({ "token": probe })
            )
            .await,
            "not_found",
            "{probe}"
        );
    }
    assert_eq!(f.directory.lookups(), 0, "no lookup oracle");
}

#[tokio::test]
async fn an_ambiguous_handle_is_an_explicit_error_never_a_guess() {
    let f = fixture_with(full_grants(), true, true, Some(("main", COMMUNITY_ID)));
    // Rebuild with a resolver that reports ambiguity.
    let directory = Arc::new(ScriptedDirectory::default());
    let token = Uuid::new_v4().to_string();
    let mut caps = f.caps;
    caps = caps.with_identity(
        IdentityWiring {
            directory: Arc::clone(&directory) as Arc<dyn MemberDirectory>,
            handles: Some(Arc::new(ScriptedHandles(Mutex::new(Some(Err(
                IdentityError::Ambiguous,
            ))))) as Arc<dyn HandleResolver>),
            flag: Arc::new(FixedGate(true)),
        },
        Arc::new(InvocationIdentity::new(
            "twitch",
            None,
            vec![MentionBinding {
                key: token.clone(),
                reference: MentionRef::Handle(RAW_HANDLE.to_string()),
            }],
        )),
    );
    let err = caps
        .handle(id_call(
            "identity.resolve_mention",
            serde_json::json!({ "token": token }),
        ))
        .await
        .unwrap_err();
    assert_eq!(err.code, "ambiguous");
    assert_eq!(directory.lookups(), 0);
}

#[tokio::test]
async fn a_mention_target_that_is_not_linked_surfaces_not_linked() {
    let f = fixture();
    f.directory
        .set(RAW_TARGET_ID, Err(IdentityError::NotLinked));
    assert_eq!(
        code_of(
            &f,
            "identity.resolve_mention",
            serde_json::json!({ "token": f.id_token })
        )
        .await,
        "not_linked"
    );
}

#[tokio::test]
async fn the_gate_authorizes_first_an_ungranted_call_is_not_granted_not_unwired() {
    // No identity.resolve grant, AND the capability unwired, AND flag off: the
    // gate's refusal must win (never `not_implemented`/`feature_disabled`).
    for (flag_on, wired) in [(true, true), (false, true), (true, false)] {
        let f = fixture_with(
            grants(&[("economy.read", serde_json::json!({}))]),
            flag_on,
            wired,
            Some(("main", COMMUNITY_ID)),
        );
        assert_eq!(
            code_of(&f, "identity.resolve_actor", serde_json::json!({})).await,
            "not_granted",
            "flag_on={flag_on} wired={wired}"
        );
        assert_eq!(f.directory.lookups(), 0);
    }
}

#[tokio::test]
async fn a_granted_call_with_the_flag_off_is_feature_disabled_and_touches_nothing() {
    let f = fixture_with(full_grants(), false, true, Some(("main", COMMUNITY_ID)));
    assert_eq!(
        code_of(&f, "identity.resolve_actor", serde_json::json!({})).await,
        "feature_disabled"
    );
    assert_eq!(f.directory.lookups(), 0);
}

#[tokio::test]
async fn a_granted_call_on_an_unwired_stage_is_not_implemented() {
    let f = fixture_with(full_grants(), true, false, Some(("main", COMMUNITY_ID)));
    assert_eq!(
        code_of(&f, "identity.resolve_actor", serde_json::json!({})).await,
        "not_implemented"
    );
}

#[tokio::test]
async fn a_tenant_wide_activation_has_no_community_to_resolve_in() {
    let f = fixture_with(full_grants(), true, true, None);
    assert_eq!(
        code_of(&f, "identity.resolve_actor", serde_json::json!({})).await,
        "invalid_args"
    );
    assert_eq!(f.directory.lookups(), 0);
}

#[tokio::test]
async fn malformed_arguments_are_invalid_args() {
    let f = fixture();
    for args in [
        serde_json::json!({}),
        serde_json::json!({ "token": "" }),
        serde_json::json!({ "token": 5 }),
        serde_json::json!({ "token": null }),
        serde_json::json!({ "token": "x".repeat(MAX_MENTION_TOKEN_LEN + 1) }),
    ] {
        assert_eq!(
            code_of(&f, "identity.resolve_mention", args.clone()).await,
            "invalid_args",
            "{args}"
        );
    }
    assert_eq!(f.directory.lookups(), 0);
}

#[tokio::test]
async fn an_unknown_identity_op_is_refused() {
    let f = fixture();
    for op in ["identity.mint", "identity.resolve", "identity.lookup_user"] {
        assert_eq!(
            code_of(&f, op, serde_json::json!({})).await,
            "unknown_op",
            "{op}"
        );
    }
}

#[tokio::test]
async fn resolutions_are_rate_limited_by_the_gate() {
    let f = fixture();
    for n in 0..20 {
        if let Err(e) = f
            .caps
            .handle(id_call("identity.resolve_actor", serde_json::json!({})))
            .await
        {
            panic!("call {n} within the window must succeed: {e:?}");
        }
    }
    assert_eq!(
        code_of(&f, "identity.resolve_actor", serde_json::json!({})).await,
        "rate_limited"
    );
}

#[tokio::test]
async fn backend_detail_is_never_handed_to_a_guest() {
    let f = fixture();
    f.directory.set(
        RAW_ACTOR_ID,
        Err(IdentityError::Backend(
            "connection to db-primary.internal:5432 refused (user=waddles_bundle_reader)"
                .to_string(),
        )),
    );
    let err = f
        .caps
        .handle(id_call("identity.resolve_actor", serde_json::json!({})))
        .await
        .unwrap_err();
    assert_eq!(err.code, "backend");
    assert!(
        !err.message.contains("db-primary") && !err.message.contains("waddles_bundle_reader"),
        "{}",
        err.message
    );
}

/// The whole result surface carries nothing but a canonical UUID: no platform
/// account id, no handle, no placeholder -- for every success and every refusal.
#[tokio::test]
async fn no_raw_identity_ever_appears_in_any_result_or_refusal() {
    let f = fixture();
    f.directory.set("unlinked", Err(IdentityError::NotLinked));
    let mut surfaces = Vec::new();

    for op_args in [
        ("identity.resolve_actor", serde_json::json!({})),
        (
            "identity.resolve_mention",
            serde_json::json!({ "token": f.id_token }),
        ),
        (
            "identity.resolve_mention",
            serde_json::json!({ "token": f.handle_token }),
        ),
        (
            "identity.resolve_mention",
            serde_json::json!({ "token": format!("@{RAW_HANDLE}") }),
        ),
        (
            "identity.resolve_mention",
            serde_json::json!({ "token": RAW_TARGET_ID }),
        ),
    ] {
        let rendered = match f.caps.handle(id_call(op_args.0, op_args.1)).await {
            Ok(value) => {
                let user = value["user"].as_str().expect("user string").to_string();
                assert!(
                    Uuid::parse_str(&user).is_ok(),
                    "success must be a UUID: {user}"
                );
                assert_eq!(
                    value.as_object().unwrap().len(),
                    1,
                    "success carries exactly one field"
                );
                value.to_string()
            }
            Err(e) => format!("{} {}", e.code, e.message),
        };
        surfaces.push(rendered);
    }
    for s in &surfaces {
        for raw in [RAW_ACTOR_ID, RAW_TARGET_ID, RAW_HANDLE, "{user:"] {
            // The raw platform ids are short digit strings; compare on the
            // quoted/structured forms to avoid matching digits inside a UUID.
            let leaked = if raw.chars().all(|c| c.is_ascii_digit()) {
                s.contains(&format!("\"{raw}\"")) || s.contains(&format!(" {raw}"))
            } else {
                s.contains(raw)
            };
            assert!(!leaked, "raw identity {raw:?} leaked into {s:?}");
        }
    }
}
