//! Tests of the generic host tool path: through the REAL `tool.invoke` planner (manifest
//! request, `tool.call` grant, write key, `required_scope` against a verified actor) down to the
//! queued, signed producer envelope; plus the migrated Discord role/event tools (behaviour of
//! the retired hand-written `discord_role_tools`).

use std::collections::HashMap;
use std::sync::Arc;

use bundle_active_set::{
    ActiveBundleRow, ActiveVersionSnapshot, ExportedToolInfo, HostOpDecl, RequestedToolInfo,
    ToolManifestInfo,
};
use bundle_capability_gate::{
    CapabilityGate, GrantScopeKey, GrantSet, GrantedPermission, InMemoryGrantSnapshot,
    InMemoryInstancePolicySnapshot, InMemoryMembership, InMemoryQuotaLedger, PermissionId,
};
use bundle_tool_call::{ActorContext, ToolPlan, ToolRefusal};
use penguin_bundle_host::wire::{CapabilityKind, HostCallBody};
use serde_json::{json, Value};

use super::super::tests::{caps, discord_scope, scope, FakeRelayQueue};
use super::super::{
    ActorId, CapabilityHandler, InvokeScope, RelayOp, StageCapabilities, ToolWiring,
};
use super::{transitional_decl, validate_args, OpClass};
use crate::actor::{ActorDirectory, ActorError, BoxFuture as ActorFuture};
use crate::host_api::ConnectionRegistry;
use crate::platform_user::{BoxFuture as UserFuture, PlatformUserResolver, ResolveError};

#[path = "host_tools_twitch_manifest_tests.rs"]
mod twitch_manifest_tests;
#[path = "host_tools_youtube_manifest_tests.rs"]
mod youtube_manifest_tests;

const APP: &str = "waddles.test.app.caller";
const TWITCH_SVC: &str = "waddles.core.service.twitch";
const DISCORD_SVC: &str = super::super::event_sync::DISCORD_SERVICE_APP_ID;
const UUID_A: &str = "3f2b8c1e-9d4a-4c55-8a10-0a1b2c3d4e5f";
const UUID_B: &str = "7a1d2c3e-1111-4222-8333-444455556666";
const UUID_C: &str = "9c9c9c9c-1111-4222-8333-444455556666";
const UUID_D: &str = "d0d0d0d0-1111-4222-8333-444455556666";
/// A syntactically valid snowflake for the pinned-origin tests (not a real id).
const DISCORD_TEST_SNOWFLAKE: &str = "900000000000000001";
const TENANT_ID: i32 = 7;
const COMMUNITY_ID: i32 = 3;
const APP_VERSION: i64 = 11;
const SVC_VERSION: i64 = 44;
const DIGEST: &str = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

/// A fake hub-api: uuid -> platform id, or a canned failure.
struct FakeUsers {
    map: HashMap<String, String>,
    fail: Option<ResolveError>,
}

impl PlatformUserResolver for FakeUsers {
    fn resolve<'a>(
        &'a self,
        tenant_id: i32,
        community_id: i32,
        platform: &'a str,
        uuid: &'a str,
    ) -> UserFuture<'a, Result<String, ResolveError>> {
        Box::pin(async move {
            assert_eq!((tenant_id, community_id), (TENANT_ID, COMMUNITY_ID));
            assert!(!platform.is_empty());
            if let Some(e) = self.fail {
                return Err(e);
            }
            self.map.get(uuid).cloned().ok_or(ResolveError::NotFound)
        })
    }
}

/// A fake membership directory: platform account id -> (user uuid, role).
struct FakeDirectory(HashMap<String, (String, String)>);

impl ActorDirectory for FakeDirectory {
    fn member<'a>(
        &'a self,
        tenant_id: i32,
        community_id: i32,
        _platform: &'a str,
        platform_user_id: &'a str,
    ) -> ActorFuture<'a, Result<(String, String), ActorError>> {
        Box::pin(async move {
            assert_eq!((tenant_id, community_id), (TENANT_ID, COMMUNITY_ID));
            self.0
                .get(platform_user_id)
                .cloned()
                .ok_or(ActorError::NotAMember)
        })
    }
}

fn decl(v: Value) -> HostOpDecl {
    HostOpDecl::from_summary(&v).unwrap_or_else(|| panic!("bad decl {v}"))
}

fn exported(name: &str, scope: Option<&str>, host_op: Option<HostOpDecl>) -> ExportedToolInfo {
    ExportedToolInfo {
        name: name.to_string(),
        write: true,
        required_scope: scope.map(str::to_string),
        host_op,
    }
}

/// The host-op tools of a platform bundle's REAL manifest, loaded from the consent summary the
/// hub approval pipeline derives from it (`scripts/host_tool_summary.py` regenerates the
/// fixture; a Python test fails when it drifts). Zero tools is a failure, never an empty pass.
fn manifest_tools(platform: &str) -> Vec<ExportedToolInfo> {
    let path = format!(
        "{}/tests/fixtures/host_tools/{platform}.json",
        env!("CARGO_MANIFEST_DIR")
    );
    let raw = std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("{path}: {e}"));
    let exported: Value = serde_json::from_str(&raw).unwrap_or_else(|e| panic!("{path}: {e}"));
    let info = ToolManifestInfo::from_summary(
        &json!({"capabilities": ["mcp.tools"], "exportedTools": exported}),
    );
    let tools: Vec<ExportedToolInfo> = info
        .exported
        .into_iter()
        .filter(|t| t.host_op.is_some())
        .collect();
    assert!(!tools.is_empty(), "{path}: no host-op tools parsed");
    tools
}

fn row(app: &str, version_id: i64, tools: ToolManifestInfo) -> ActiveBundleRow {
    ActiveBundleRow {
        app_id: app.to_string(),
        version: "1.0.0".to_string(),
        version_id,
        digest: DIGEST.to_string(),
        component_key: String::new(),
        sidecar_key: String::new(),
        artifact_signature: None,
        artifact_signature_key_id: None,
        artifact_signed_approval_id: None,
        declared_capabilities: Vec::new(),
        egress: Vec::new(),
        egress_rps: None,
        tools,
    }
}

/// A gate granting each `(grantee, version)` the given canonical permission ids.
fn gate(grants: &[(&str, i64, Vec<String>)]) -> Arc<CapabilityGate> {
    let snapshot = InMemoryGrantSnapshot::new();
    for (grantee, version, ids) in grants {
        let map = ids
            .iter()
            .map(|id| {
                (
                    id.clone(),
                    GrantedPermission {
                        permission_id: id.clone(),
                        params: json!({}),
                    },
                )
            })
            .collect();
        snapshot.set(
            GrantScopeKey {
                tenant_id: TENANT_ID,
                community_id: COMMUNITY_ID,
                app_id: (*grantee).to_string(),
                app_version: *version,
            },
            GrantSet {
                permission_snapshot_hash: "h".to_string(),
                grants: map,
            },
        );
    }
    Arc::new(CapabilityGate::new(
        Arc::new(snapshot),
        Arc::new(InMemoryMembership::new()),
        Arc::new(InMemoryQuotaLedger::new()),
        Arc::new(InMemoryInstancePolicySnapshot::new()),
    ))
}

fn tool_call_id(target: &str, tool: &str) -> String {
    PermissionId::ToolCall {
        target: target.to_string(),
        tool: tool.to_string(),
    }
    .canonical_id()
}

struct Rig {
    users: FakeUsers,
    members: HashMap<String, (String, String)>,
    svc_grants: Vec<String>,
    caller_grants: bool,
    with_directory: bool,
}

impl Default for Rig {
    fn default() -> Self {
        Self {
            users: FakeUsers {
                map: HashMap::from([
                    (UUID_A.to_string(), "141981764".to_string()),
                    (UUID_B.to_string(), "999000111".to_string()),
                    (UUID_C.to_string(), "600".to_string()),
                    (UUID_D.to_string(), "601".to_string()),
                ]),
                fail: None,
            },
            members: HashMap::from([
                (
                    "500".to_string(),
                    (UUID_A.to_string(), "moderator".to_string()),
                ),
                (
                    "501".to_string(),
                    (UUID_B.to_string(), "member".to_string()),
                ),
                (
                    "600".to_string(),
                    (UUID_C.to_string(), "community-admin".to_string()),
                ),
                ("601".to_string(), (UUID_A.to_string(), "owner".to_string())),
            ]),
            svc_grants: vec!["moderation.twitch".to_string()],
            caller_grants: true,
            with_directory: true,
        }
    }
}

/// Builds the stage handler over a twitch service bundle exporting `tools`, a caller app that
/// requests them all, the given grants, and `queue`.
fn build(
    rig: Rig,
    tools: Vec<ExportedToolInfo>,
    queue: FakeRelayQueue,
) -> StageCapabilities<FakeRelayQueue> {
    let svc = tools
        .iter()
        .find_map(|t| t.host_op.as_ref().map(|d| svc_for(&d.platform)))
        .unwrap_or_else(|| TWITCH_SVC.to_string());
    let requested: Vec<RequestedToolInfo> = tools
        .iter()
        .map(|t| RequestedToolInfo {
            target: svc.clone(),
            tool: t.name.clone(),
            write: true,
        })
        .collect();
    let rows = vec![
        row(
            APP,
            APP_VERSION,
            ToolManifestInfo {
                is_tool_service: false,
                exported: vec![],
                requested,
            },
        ),
        row(
            &svc,
            SVC_VERSION,
            ToolManifestInfo {
                is_tool_service: true,
                exported: tools.clone(),
                requested: vec![],
            },
        ),
    ];
    let snapshot = ActiveVersionSnapshot::new();
    snapshot.update_scoped_all(
        &rows
            .into_iter()
            .map(|r| ((TENANT_ID, COMMUNITY_ID, r.app_id.clone()), r))
            .collect(),
    );
    let caller_ids: Vec<String> = if rig.caller_grants {
        tools.iter().map(|t| tool_call_id(&svc, &t.name)).collect()
    } else {
        vec![]
    };
    let mut c = caps(queue);
    c.gate = gate(&[
        (APP, APP_VERSION, caller_ids),
        (&svc, SVC_VERSION, rig.svc_grants),
    ]);
    let c = c
        .with_tool(ToolWiring {
            connections: Arc::new(ConnectionRegistry::new()),
            snapshot,
        })
        .with_platform_users(Arc::new(rig.users));
    if rig.with_directory {
        c.with_actor_directory(Arc::new(FakeDirectory(rig.members)))
    } else {
        c
    }
}

fn ban_decl() -> HostOpDecl {
    decl(json!({
        "platform": "twitch", "op": "mod.ban", "user": "user",
        "textObject": {"reason": "reason"}, "required": ["user"], "origin": "channel"
    }))
}

/// The registered platform service bundle id of `platform`.
fn svc_for(platform: &str) -> String {
    format!("waddles.core.service.{platform}")
}

fn invoke(tool: &str, args: Value) -> HostCallBody {
    invoke_to(TWITCH_SVC, tool, args)
}

fn invoke_to(target: &str, tool: &str, args: Value) -> HostCallBody {
    HostCallBody {
        app_id: APP.to_string(),
        capability: CapabilityKind::Db,
        op: "tool.invoke".to_string(),
        args: json!({"target": target, "tool": tool, "args": args, "idempotency_key": "k-1"}),
        call_id: 1,
    }
}

/// A twitch chat event authored by platform account `author`, in channel `#somechannel`.
fn twitch_event(author: Option<&str>) -> InvokeScope {
    InvokeScope {
        app_id: APP.to_string(),
        app_version: APP_VERSION,
        origin_channel_id: Some("somechannel".to_string()),
        origin_platform: Some("twitch".to_string()),
        origin_user_id: author.and_then(ActorId::new),
        ..scope()
    }
}

fn ban_tools() -> Vec<ExportedToolInfo> {
    vec![
        exported(
            "twitch.moderation.ban",
            Some("community:moderate"),
            Some(ban_decl()),
        ),
        exported(
            "twitch.moderation.unban",
            Some("community:moderate"),
            Some(decl(json!({
                "platform": "twitch", "op": "mod.unban", "user": "user",
                "required": ["user"], "origin": "channel"
            }))),
        ),
    ]
}

#[tokio::test]
async fn moderator_ban_is_detokenized_host_side_and_queued_pinned_and_signed() {
    let c = build(
        Rig::default(),
        ban_tools(),
        FakeRelayQueue::with_live_drain(),
    );
    let out = c
        .handle(
            &twitch_event(Some("500")),
            invoke(
                "twitch.moderation.ban",
                json!({"user": UUID_B, "reason": "spam"}),
            ),
        )
        .await
        .unwrap_or_else(|e| panic!("{e:?}"));
    assert_eq!(out, json!({"ok": true, "op": "mod.ban"}));
    let envs = c.relay_queue.envelopes();
    assert_eq!(envs.len(), 1);
    let e = &envs[0];
    assert_eq!(
        (e["op"].as_str(), e["platform"].as_str()),
        (Some("mod.ban"), Some("twitch"))
    );
    // The tokenized UUID became the platform numeric id, host-side; the UUID never leaves.
    assert_eq!(e["user_id"], "999000111");
    assert!(!e.to_string().contains(UUID_B));
    assert_eq!(e["text"], r#"{"reason":"spam"}"#);
    // Destination = the verified origin channel only; ids stamped from the verified scope.
    assert_eq!(
        (e["channel"].as_str(), e["origin_channel"].as_str()),
        (Some("#somechannel"), Some("#somechannel"))
    );
    assert_eq!(
        (e["tenant_id"].as_i64(), e["community_id"].as_i64()),
        (Some(7), Some(3))
    );
    assert!(e["pin_sig"].as_str().is_some() && e["op_id"].as_str().is_some());
}

#[tokio::test]
async fn missing_or_insufficient_scope_is_refused_and_queues_nothing() {
    // (author, directory wired) -> refused
    for (author, with_directory, why) in [
        (Some("501"), true, "member holds no moderate scope"),
        (Some("777"), true, "not a community member"),
        (None, true, "event names no author"),
        (Some("500"), false, "no directory wired"),
    ] {
        let c = build(
            Rig {
                with_directory,
                ..Rig::default()
            },
            ban_tools(),
            FakeRelayQueue::with_live_drain(),
        );
        let err = c
            .handle(
                &twitch_event(author),
                invoke("twitch.moderation.ban", json!({"user": UUID_A})),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "forbidden", "{why}");
        assert!(c.relay_queue.envelopes().is_empty(), "{why}");
    }
}

#[tokio::test]
async fn unresolvable_or_unavailable_user_is_refused_fail_closed() {
    let unknown = "11111111-2222-4333-8444-555566667777";
    let c = build(
        Rig::default(),
        ban_tools(),
        FakeRelayQueue::with_live_drain(),
    );
    let err = c
        .handle(
            &twitch_event(Some("500")),
            invoke("twitch.moderation.ban", json!({"user": unknown})),
        )
        .await
        .unwrap_err();
    assert_eq!(err.code, "invalid_args");
    assert!(
        err.message.starts_with("no_linked_account"),
        "{}",
        err.message
    );
    assert!(c.relay_queue.envelopes().is_empty());

    for (fail, code) in [
        (ResolveError::Unavailable, "target_unavailable"),
        (ResolveError::Denied, "target_unavailable"),
        (ResolveError::Invalid, "invalid_args"),
    ] {
        let mut rig = Rig::default();
        rig.users.fail = Some(fail);
        let c = build(rig, ban_tools(), FakeRelayQueue::with_live_drain());
        let err = c
            .handle(
                &twitch_event(Some("500")),
                invoke("twitch.moderation.ban", json!({"user": UUID_A})),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, code, "{fail:?}");
        assert!(c.relay_queue.envelopes().is_empty());
    }
}

#[tokio::test]
async fn no_platform_user_resolver_refuses_a_user_naming_tool() {
    let queue = FakeRelayQueue::with_live_drain();
    let mut c = build(Rig::default(), ban_tools(), queue);
    c.platform_users = None;
    let err = c
        .handle(
            &twitch_event(Some("500")),
            invoke("twitch.moderation.ban", json!({"user": UUID_A})),
        )
        .await
        .unwrap_err();
    assert_eq!(err.code, "target_unavailable");
    assert!(c.relay_queue.envelopes().is_empty());
}

#[tokio::test]
async fn injected_malformed_and_missing_arguments_are_refused_before_any_io() {
    let c = build(
        Rig::default(),
        ban_tools(),
        FakeRelayQueue::with_live_drain(),
    );
    for args in [
        json!({"user": UUID_A, "broadcaster_id": "1"}),
        json!({"user": UUID_A, "moderator_id": "1"}),
        json!({"user": UUID_A, "channel": "#other"}),
        json!({"user": UUID_A, "user_id": "141981764"}),
        json!({"user": "141981764"}),
        json!({"user": UUID_A.to_uppercase()}),
        json!({"user": format!("{{user:{UUID_A}}}")}),
        json!({}),
        json!({"user": 5}),
        json!([]),
    ] {
        let err = c
            .handle(
                &twitch_event(Some("500")),
                invoke("twitch.moderation.ban", args.clone()),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "invalid_args", "{args}");
    }
    assert!(c.relay_queue.envelopes().is_empty());
}

#[tokio::test]
async fn ungranted_caller_or_service_without_the_platform_permission_is_refused() {
    let c = build(
        Rig {
            caller_grants: false,
            ..Rig::default()
        },
        ban_tools(),
        FakeRelayQueue::with_live_drain(),
    );
    let err = c
        .handle(
            &twitch_event(Some("500")),
            invoke("twitch.moderation.ban", json!({"user": UUID_A})),
        )
        .await
        .unwrap_err();
    assert_eq!(err.code, "not_granted");

    let c = build(
        Rig {
            svc_grants: vec![],
            ..Rig::default()
        },
        ban_tools(),
        FakeRelayQueue::with_live_drain(),
    );
    let err = c
        .handle(
            &twitch_event(Some("500")),
            invoke("twitch.moderation.ban", json!({"user": UUID_A})),
        )
        .await
        .unwrap_err();
    assert_eq!(err.code, "forbidden");
    assert!(c.relay_queue.envelopes().is_empty());
}

#[tokio::test]
async fn an_op_the_platform_table_does_not_register_is_refused_loudly() {
    let tools = vec![exported(
        "twitch.moderation.kick",
        None,
        Some(decl(
            json!({"platform": "twitch", "op": "mod.kick", "user": "user", "required": ["user"]}),
        )),
    )];
    let c = build(Rig::default(), tools, FakeRelayQueue::with_live_drain());
    let err = c
        .handle(
            &twitch_event(Some("500")),
            invoke("twitch.moderation.kick", json!({"user": UUID_A})),
        )
        .await
        .unwrap_err();
    assert_eq!(err.code, "forbidden");
    assert!(err.message.contains("not registered"), "{}", err.message);
    assert!(c.relay_queue.envelopes().is_empty());
}

#[tokio::test]
async fn chat_clear_is_a_registered_twitch_op_pinned_to_the_origin_channel() {
    let tools = vec![exported(
        "twitch.chat.clear",
        Some("community:moderate"),
        Some(decl(
            json!({"platform": "twitch", "op": "chat.clear", "origin": "channel"}),
        )),
    )];
    let rig = Rig {
        svc_grants: vec!["chat.clear:twitch".to_string()],
        ..Rig::default()
    };
    let c = build(rig, tools, FakeRelayQueue::with_live_drain());
    let out = c
        .handle(
            &twitch_event(Some("500")),
            invoke("twitch.chat.clear", json!({})),
        )
        .await
        .unwrap_or_else(|e| panic!("{e:?}"));
    assert_eq!(out, json!({"ok": true, "op": "chat.clear"}));
    let e = &c.relay_queue.envelopes()[0];
    assert_eq!(
        (e["op"].as_str(), e["channel"].as_str()),
        (Some("chat.clear"), Some("#somechannel"))
    );
    assert!(e.get("user_id").is_none() && e.get("message_id").is_none());
}

#[tokio::test]
async fn only_allowlisted_reply_keys_reach_the_caller_and_a_webhook_token_is_dropped() {
    let tools = vec![exported(
        "discord.webhook.create",
        None,
        Some(decl(json!({
            "platform": "twitch", "op": "mod.ban", "user": "user",
            "returns": ["id"], "required": ["user"]
        }))),
    )];
    // The drain's data object carries a secret the manifest did not allowlist.
    let c = build(
        Rig::default(),
        tools,
        FakeRelayQueue::with_drain_result(
            r#"{"ok":true,"data":{"id":"77","token":"SECRET-TOKEN"}}"#,
        ),
    );
    let out = c
        .handle(
            &twitch_event(Some("500")),
            invoke("discord.webhook.create", json!({"user": UUID_A})),
        )
        .await
        .unwrap_or_else(|e| panic!("{e:?}"));
    assert_eq!(out, json!({"ok": true, "id": "77"}));
    assert!(!out.to_string().contains("SECRET"));
    // A required return key the platform did not send is a refusal, not a fabricated value.
    let tools = vec![exported(
        "discord.webhook.create",
        None,
        Some(decl(
            json!({"platform": "twitch", "op": "mod.ban", "user": "user", "returns": ["id"], "required": ["user"]}),
        )),
    )];
    let c = build(
        Rig::default(),
        tools,
        FakeRelayQueue::with_drain_result(r#"{"ok":true}"#),
    );
    let err = c
        .handle(
            &twitch_event(Some("500")),
            invoke("discord.webhook.create", json!({"user": UUID_A})),
        )
        .await
        .unwrap_err();
    assert_eq!(err.code, "target_unavailable");
}

#[tokio::test]
async fn a_snowflake_reply_id_is_returned_for_create_style_ops() {
    let tools = vec![exported(
        "twitch.moderation.ban",
        None,
        Some(decl(
            json!({"platform": "twitch", "op": "mod.ban", "user": "user", "returns": ["id"], "required": ["user"]}),
        )),
    )];
    let c = build(
        Rig::default(),
        tools,
        FakeRelayQueue::with_drain_result(r#"{"ok":true,"id":"900000000000000001"}"#),
    );
    let out = c
        .handle(
            &twitch_event(Some("500")),
            invoke("twitch.moderation.ban", json!({"user": UUID_A})),
        )
        .await
        .unwrap_or_else(|e| panic!("{e:?}"));
    assert_eq!(out, json!({"ok": true, "id": "900000000000000001"}));
}

#[tokio::test]
async fn an_unanswered_or_refused_op_is_never_reported_done() {
    // The drain is ready but never answers (no auto-ack).
    let mut q = FakeRelayQueue::with_live_drain();
    q.auto_ack = None;
    let mut c = build(Rig::default(), ban_tools(), q);
    c.discord_ack_timeout = std::time::Duration::from_millis(120);
    let err = c
        .handle(
            &twitch_event(Some("500")),
            invoke("twitch.moderation.ban", json!({"user": UUID_A})),
        )
        .await
        .unwrap_err();
    assert_eq!(
        (err.code.as_str(), err.message.as_str()),
        ("target_unavailable", "not_confirmed")
    );

    for (ack, code) in [
        (r#"{"ok":false,"code":"rejected"}"#, "target_unavailable"),
        (
            r#"{"ok":false,"code":"ambiguous_paired_guild"}"#,
            "forbidden",
        ),
        (r#"{"ok":false,"code":"bad_request"}"#, "invalid_args"),
        (r#"{"nope":1}"#, "target_unavailable"),
    ] {
        let c = build(
            Rig::default(),
            ban_tools(),
            FakeRelayQueue::with_drain_result(ack),
        );
        let err = c
            .handle(
                &twitch_event(Some("500")),
                invoke("twitch.moderation.ban", json!({"user": UUID_A})),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, code, "{ack}");
    }
}

#[test]
fn argument_validation_unit_cases() {
    let d = decl(json!({
        "platform": "twitch", "op": "mod.timeout", "user": "user",
        "textObject": {"duration": "duration_seconds", "reason": "reason"},
        "required": ["user", "duration_seconds"]
    }));
    let class = OpClass::Registered("mod.timeout");
    let w = validate_args(&d, class, &json!({"user": UUID_A, "duration_seconds": 300}))
        .unwrap_or_else(|e| panic!("{e:?}"));
    assert_eq!(w.text.as_deref(), Some(r#"{"duration":300}"#));
    assert_eq!(w.user_uuid.as_deref(), Some(UUID_A));
    assert!(validate_args(&d, class, &json!({"user": UUID_A})).is_err());
    assert!(validate_args(
        &d,
        class,
        &json!({"user": UUID_A, "duration_seconds": 1, "x": 1})
    )
    .is_err());
}

// ---- migrated Discord role / event tools (behaviour of the retired discord_role_tools) -----

/// Stage capabilities whose gate grants the Discord SERVICE bundle `moderation.discord` (the
/// permission role and scheduled-event host ops require of the service itself).
fn dcaps(queue: FakeRelayQueue) -> StageCapabilities<FakeRelayQueue> {
    let mut c = caps(queue);
    c.gate = gate(&[(
        DISCORD_SVC,
        SVC_VERSION,
        vec!["moderation.discord".to_string()],
    )]);
    c
}

fn plan_for(tool: &str, args: Value) -> ToolPlan {
    ToolPlan {
        target_app_id: DISCORD_SVC.to_string(),
        tool: tool.to_string(),
        args,
        idempotency_key: Some("k".to_string()),
        target_digest: DIGEST.to_string(),
        target_version: SVC_VERSION,
        write: true,
        next_chain: vec![APP.to_string(), DISCORD_SVC.to_string()],
        caller_app_id: APP.to_string(),
        actor: ActorContext::none(),
        tenant_id: TENANT_ID,
        community_id: COMMUNITY_ID,
    }
}

/// Runs a transitional-declared Discord tool through the generic engine (as `tool.invoke` does
/// after the planner).
async fn run_role(
    c: &StageCapabilities<FakeRelayQueue>,
    sc: &InvokeScope,
    tool: &str,
    args: Value,
) -> Result<Value, ToolRefusal> {
    let d = transitional_decl(DISCORD_SVC, tool).unwrap_or_else(|| panic!("no decl for {tool}"));
    c.run_host_tool(sc, &plan_for(tool, args), &d).await
}

const ROLES_ADD: &str = "discord.member.roles.add";
const ROLES_REMOVE: &str = "discord.member.roles.remove";
const ROLES_SET: &str = "discord.member.roles.set";
const ROLES_LIST: &str = "discord.roles.list";

fn role_args(extra: Value) -> Value {
    let mut v = json!({"user": UUID_A, "role_key": "vip"});
    for (k, val) in extra.as_object().unwrap_or(&serde_json::Map::new()) {
        v[k] = val.clone();
    }
    v
}

#[tokio::test]
async fn transitional_declarations_cover_exactly_the_pre_declaration_discord_tools() {
    for t in [
        ROLES_ADD,
        ROLES_REMOVE,
        ROLES_SET,
        ROLES_LIST,
        "discord.scheduled_event.create",
        "discord.scheduled_event.update",
        "discord.scheduled_event.cancel",
        "discord.scheduled_event.delete",
    ] {
        assert!(transitional_decl(DISCORD_SVC, t).is_some(), "{t}");
        assert!(
            transitional_decl(TWITCH_SVC, t).is_none(),
            "{t} is discord-only"
        );
    }
    assert!(transitional_decl(DISCORD_SVC, "classify_frame").is_none());
}

#[tokio::test]
async fn role_tool_queues_the_guild_pinned_role_op_and_returns_ok() {
    for (tool, op) in [(ROLES_ADD, "role.add"), (ROLES_REMOVE, "role.remove")] {
        let c = dcaps(FakeRelayQueue::with_live_drain());
        let out = run_role(&c, &discord_scope(), tool, role_args(json!({}))).await;
        assert_eq!(
            out.unwrap_or_else(|e| panic!("{e:?}")),
            json!({"ok": true, "op": op})
        );
        let envs = c.relay_queue.envelopes();
        let e = &envs[0];
        assert_eq!(
            (e["op"].as_str(), e["platform"].as_str()),
            (Some(op), Some("discord"))
        );
        assert_eq!(e["text"], "vip", "role KEY rides in the MAC-covered text");
        assert_eq!(e["user_id"], format!("{{user:{UUID_A}}}"));
        assert_eq!(e["origin_channel"], "123456789012345678");
        assert_eq!(
            (e["tenant_id"].as_i64(), e["community_id"].as_i64()),
            (Some(7), Some(3))
        );
        for never in ["guild_id", "role_id", "channel"] {
            assert!(e.get(never).is_none(), "{never} must not reach the queue");
        }
        assert!(e["pin_sig"].as_str().is_some() && e["op_id"].as_str().is_some());
    }
}

#[tokio::test]
async fn injected_role_arguments_are_refused_and_queue_nothing() {
    let c = dcaps(FakeRelayQueue::with_live_drain());
    for extra in [
        json!({"guild_id": "111111111111111111"}),
        json!({"role_id": "222222222222222222"}),
        json!({"channel": "9"}),
        json!({"tenant_id": 99}),
        json!({"origin_channel": "9"}),
        json!({"role_key": "222222222222222222"}),
        json!({"role_key": "VIP"}),
        json!({"user": "333333333333333333"}),
        json!({"user": UUID_A.to_uppercase()}),
    ] {
        let r = run_role(&c, &discord_scope(), ROLES_ADD, role_args(extra.clone())).await;
        assert!(
            matches!(r, Err(ToolRefusal::InvalidArgs(_))),
            "{extra}: {r:?}"
        );
    }
    for bad in [
        json!("x"),
        json!({"user": UUID_A}),
        json!({"role_key": "vip"}),
    ] {
        let r = run_role(&c, &discord_scope(), ROLES_ADD, bad.clone()).await;
        assert!(matches!(r, Err(ToolRefusal::InvalidArgs(_))), "{bad}");
    }
    assert!(c.relay_queue.envelopes().is_empty());
}

#[tokio::test]
async fn unready_drain_and_drain_refusals_are_handled_loudly() {
    let c = dcaps(FakeRelayQueue::default());
    let r = run_role(&c, &discord_scope(), ROLES_REMOVE, role_args(json!({}))).await;
    assert!(matches!(r, Err(ToolRefusal::TargetUnavailable(_))));
    assert!(c.relay_queue.envelopes().is_empty());
    // A drain refusal is surfaced with its typed class (not collapsed to unavailable).
    let c = dcaps(FakeRelayQueue::with_drain_result(
        r#"{"ok":false,"code":"role_not_managed"}"#,
    ));
    let r = run_role(&c, &discord_scope(), ROLES_ADD, role_args(json!({}))).await;
    assert!(matches!(r, Err(ToolRefusal::InvalidArgs(_))), "{r:?}");
    let c = dcaps(FakeRelayQueue::with_drain_result(
        r#"{"ok":false,"code":"ambiguous_paired_guild"}"#,
    ));
    match run_role(&c, &discord_scope(), ROLES_ADD, role_args(json!({}))).await {
        Err(ToolRefusal::Forbidden(m)) => assert!(m.starts_with("ambiguous_paired_guild"), "{m}"),
        other => panic!("{other:?}"),
    }
}

fn queued_scope(c: &StageCapabilities<FakeRelayQueue>) -> String {
    c.relay_queue.envelopes()[0]["origin_channel"]
        .as_str()
        .unwrap_or_default()
        .to_string()
}

#[tokio::test]
async fn channel_less_origins_are_allowlisted_and_discord_needs_a_guild() {
    let cases: [(Option<&str>, Option<&str>, Option<&str>); 9] = [
        (Some("discord"), None, None),
        (Some("discord"), Some("123"), Some("guild:123")),
        (Some("wix"), None, Some("guild:-")),
        (Some("wix"), Some("123"), Some("guild:-")),
        (Some("twitch"), None, None),
        (Some("youtube"), None, None),
        (Some("kick"), Some("123"), None),
        (Some("Wix"), None, None),
        (None, None, None),
    ];
    for (platform, guild, want) in cases {
        let c = dcaps(FakeRelayQueue::with_live_drain());
        let mut sc = scope();
        sc.origin_platform = platform.map(str::to_string);
        sc.origin_channel_id = None;
        sc.origin_guild_id = guild.map(str::to_string);
        let r = run_role(&c, &sc, ROLES_ADD, role_args(json!({}))).await;
        match want {
            Some(w) => {
                r.unwrap_or_else(|e| panic!("{platform:?} {guild:?}: {e:?}"));
                assert_eq!(queued_scope(&c), w, "{platform:?} {guild:?}");
            }
            None => {
                assert!(
                    matches!(r, Err(ToolRefusal::InvalidArgs(_))),
                    "{platform:?} {guild:?}: {r:?}"
                );
                assert!(c.relay_queue.envelopes().is_empty());
            }
        }
    }
    // A channel-bearing Discord event keeps the channel pin even when a guild is known.
    let c = dcaps(FakeRelayQueue::with_live_drain());
    let mut sc = discord_scope();
    sc.origin_guild_id = Some("999".to_string());
    run_role(&c, &sc, ROLES_ADD, role_args(json!({})))
        .await
        .unwrap_or_else(|e| panic!("{e:?}"));
    assert_eq!(queued_scope(&c), "123456789012345678");
}

#[tokio::test]
async fn roles_set_queues_one_bulk_op_with_sorted_keys_and_list_returns_roles() {
    let c = dcaps(FakeRelayQueue::with_live_drain());
    let mut sc = discord_scope();
    sc.origin_channel_id = None;
    sc.origin_guild_id = Some("123456789012345679".to_string());
    let args = json!({"user": UUID_A, "role_keys": ["vip", "mod", "vip"]});
    let out = run_role(&c, &sc, ROLES_SET, args)
        .await
        .unwrap_or_else(|e| panic!("{e:?}"));
    assert_eq!(out, json!({"ok": true, "op": "role.set"}));
    let envs = c.relay_queue.envelopes();
    assert_eq!(envs.len(), 1, "one bulk op; the HOST expands the diff");
    let e = &envs[0];
    assert_eq!(
        (e["op"].as_str(), e["text"].as_str()),
        (Some("role.set"), Some("mod,vip"))
    );
    assert_eq!(e["origin_channel"], "guild:123456789012345679");

    let c = dcaps(FakeRelayQueue::with_drain_result(
        r#"{"ok":true,"data":{"roles":[{"key":"vip","assignable":true}]}}"#,
    ));
    let mut sc = scope();
    sc.origin_platform = Some("wix".to_string());
    let out = run_role(&c, &sc, ROLES_LIST, json!({}))
        .await
        .unwrap_or_else(|e| panic!("{e:?}"));
    assert_eq!(
        out,
        json!({"ok": true, "roles": [{"key": "vip", "assignable": true}]})
    );
    let e = &c.relay_queue.envelopes()[0];
    assert_eq!(
        (e["op"].as_str(), e["origin_channel"].as_str()),
        (Some("role.list"), Some("guild:-"))
    );
    assert!(e.get("user_id").is_none() && e.get("text").is_none());

    let c = dcaps(FakeRelayQueue::with_live_drain());
    for a in [
        json!({"user": UUID_A, "role_keys": []}),
        json!({"user": UUID_A, "role_keys": ["222222222222222222"]}),
        json!({"user": UUID_A, "role_keys": "vip"}),
        json!({"user": UUID_A, "role_keys": (0..17).map(|i| format!("k{i}")).collect::<Vec<_>>()}),
        json!({"user": UUID_A, "role_keys": ["vip"], "guild_id": "1"}),
    ] {
        let r = run_role(&c, &discord_scope(), ROLES_SET, a.clone()).await;
        assert!(matches!(r, Err(ToolRefusal::InvalidArgs(_))), "{a}");
    }
    let r = run_role(&c, &discord_scope(), ROLES_LIST, json!({"x": 1})).await;
    assert!(matches!(r, Err(ToolRefusal::InvalidArgs(_))));
    assert!(c.relay_queue.envelopes().is_empty());
}

#[tokio::test]
async fn bulk_drain_refusals_map_to_typed_errors_and_silence_is_not_confirmed() {
    for (code, want) in [
        ("role_privileged", "invalid"),
        ("role_not_managed", "invalid"),
        ("ambiguous_paired_guild", "forbidden"),
        ("guild_mismatch", "forbidden"),
        ("upstream_down", "unavailable"),
    ] {
        let c = dcaps(FakeRelayQueue::with_drain_result(&format!(
            r#"{{"ok":false,"code":"{code}"}}"#
        )));
        let args = json!({"user": UUID_A, "role_keys": ["vip"]});
        let got = match run_role(&c, &discord_scope(), ROLES_SET, args).await {
            Err(ToolRefusal::InvalidArgs(_)) => "invalid",
            Err(ToolRefusal::Forbidden(_)) => "forbidden",
            Err(ToolRefusal::TargetUnavailable(_)) => "unavailable",
            other => panic!("{code}: {other:?}"),
        };
        assert_eq!(got, want, "{code}");
    }
}

#[tokio::test]
async fn scheduled_event_tools_return_the_platform_id_and_status() {
    let c = dcaps(FakeRelayQueue::with_drain_result(
        r#"{"ok":true,"id":"4242"}"#,
    ));

    let args = json!({
        "name": "Movie night", "scheduled_start_time": "2026-12-01T19:00:00Z",
        "scheduled_end_time": "2026-12-01T20:00:00Z", "location": "Online",
    });
    let out = run_role(&c, &scope(), "discord.scheduled_event.create", args.clone())
        .await
        .unwrap_or_else(|e| panic!("{e:?}"));
    assert_eq!(out["id"], "4242");
    assert_eq!(out["status"], "created");
    // update/cancel/delete echo the event they acted on.
    let c = dcaps(FakeRelayQueue::with_live_drain());
    let out = run_role(
        &c,
        &scope(),
        "discord.scheduled_event.cancel",
        json!({"event_id": "55"}),
    )
    .await
    .unwrap_or_else(|e| panic!("{e:?}"));
    assert_eq!(
        (out["id"].as_str(), out["status"].as_str()),
        (Some("55"), Some("cancelled"))
    );
}

// ---- protected targets + stable refusal codes, per platform --------------------------------

/// An event of `platform` in a valid origin channel, authored by the moderator account `500`.
fn platform_event(platform: &str) -> InvokeScope {
    let channel = match platform {
        "discord" => DISCORD_TEST_SNOWFLAKE,
        "youtube" => "UCabc123_-x",
        _ => "somechannel",
    };
    InvokeScope {
        app_id: APP.to_string(),
        app_version: APP_VERSION,
        origin_channel_id: Some(channel.to_string()),
        origin_platform: Some(platform.to_string()),
        origin_user_id: ActorId::new("500"),
        ..scope()
    }
}

fn mod_tools(platform: &str, op: &str) -> Vec<ExportedToolInfo> {
    vec![exported(
        "platform.moderation.act",
        Some("community:moderate"),
        Some(decl(json!({
            "platform": platform, "op": op, "user": "user",
            "required": ["user"], "origin": "channel"
        }))),
    )]
}

fn rig_for(platform: &str) -> Rig {
    Rig {
        svc_grants: vec![format!("moderation.{platform}")],
        ..Rig::default()
    }
}

#[tokio::test]
async fn protected_targets_are_refused_on_every_platform_and_ordinary_users_are_not() {
    for (platform, op) in [
        ("twitch", "mod.ban"),
        ("twitch", "mod.timeout"),
        ("discord", "mod.ban"),
        ("discord", "mod.kick"),
        ("discord", "mod.timeout"),
        ("youtube", "mod.ban"),
    ] {
        // admin (UUID_C), owner (UUID_D) and moderator targets are protected.
        for protected in [UUID_C, UUID_D] {
            let c = build(
                rig_for(platform),
                mod_tools(platform, op),
                FakeRelayQueue::with_live_drain(),
            );
            let err = c
                .handle(
                    &platform_event(platform),
                    invoke_to(
                        &svc_for(platform),
                        "platform.moderation.act",
                        json!({"user": protected}),
                    ),
                )
                .await
                .unwrap_err();
            assert_eq!(err.code, "forbidden", "{platform} {op}");
            assert!(
                err.message.starts_with("protected_target"),
                "{platform} {op}: {}",
                err.message
            );
            assert!(
                c.relay_queue.envelopes().is_empty(),
                "{platform} {op}: nothing queued"
            );
        }
        // A moderator target (member 500's own account) is protected too.
        let mut rig = rig_for(platform);
        rig.users.map.insert(UUID_B.to_string(), "500".to_string());
        let c = build(
            rig,
            mod_tools(platform, op),
            FakeRelayQueue::with_live_drain(),
        );
        let err = c
            .handle(
                &platform_event(platform),
                invoke_to(
                    &svc_for(platform),
                    "platform.moderation.act",
                    json!({"user": UUID_B}),
                ),
            )
            .await
            .unwrap_err();
        assert!(
            err.message.starts_with("protected_target"),
            "{platform} {op}: {}",
            err.message
        );

        // An ordinary viewer (not a community member) is a valid target.
        let c = build(
            rig_for(platform),
            mod_tools(platform, op),
            FakeRelayQueue::with_live_drain(),
        );
        c.handle(
            &platform_event(platform),
            invoke_to(
                &svc_for(platform),
                "platform.moderation.act",
                json!({"user": UUID_A}),
            ),
        )
        .await
        .unwrap_or_else(|e| panic!("{platform} {op}: {e:?}"));
        let e = &c.relay_queue.envelopes()[0];
        assert_eq!(
            (e["op"].as_str(), e["platform"].as_str()),
            (Some(op), Some(platform))
        );
        assert_eq!(e["user_id"], "141981764");
    }
}

#[tokio::test]
async fn non_destructive_moderation_ops_skip_the_protected_check() {
    // Unbanning an admin's id is not an attack; the check is for ban/timeout/kick only.
    let c = build(
        Rig::default(),
        mod_tools("twitch", "mod.unban"),
        FakeRelayQueue::with_live_drain(),
    );
    c.handle(
        &platform_event("twitch"),
        invoke_to(
            &svc_for("twitch"),
            "platform.moderation.act",
            json!({"user": UUID_C}),
        ),
    )
    .await
    .unwrap_or_else(|e| panic!("{e:?}"));
}

#[tokio::test]
async fn an_unverifiable_target_is_refused_not_attacked() {
    let mut c = build(
        Rig::default(),
        mod_tools("twitch", "mod.ban"),
        FakeRelayQueue::with_live_drain(),
    );
    c.actor_directory = None;
    // (the planner needs the directory for the scope too, so wire a directory for the actor
    // only through a failing one)
    struct Down;
    impl ActorDirectory for Down {
        fn member<'a>(
            &'a self,
            _t: i32,
            _c: i32,
            _p: &'a str,
            id: &'a str,
        ) -> ActorFuture<'a, Result<(String, String), ActorError>> {
            Box::pin(async move {
                if id == "500" {
                    Ok((UUID_A.to_string(), "moderator".to_string()))
                } else {
                    Err(ActorError::Unavailable)
                }
            })
        }
    }
    c.actor_directory = Some(Arc::new(Down));
    let err = c
        .handle(
            &platform_event("twitch"),
            invoke_to(
                &svc_for("twitch"),
                "platform.moderation.act",
                json!({"user": UUID_A}),
            ),
        )
        .await
        .unwrap_err();
    assert_eq!(err.code, "target_unavailable");
    assert!(c.relay_queue.envelopes().is_empty());
}

#[tokio::test]
async fn stable_refusal_codes_are_the_message_prefix() {
    // scope_denied: a member (no moderate scope).
    let c = build(
        Rig::default(),
        mod_tools("twitch", "mod.ban"),
        FakeRelayQueue::with_live_drain(),
    );
    let mut sc = platform_event("twitch");
    sc.origin_user_id = ActorId::new("501");
    let err = c
        .handle(
            &sc,
            invoke("platform.moderation.act", json!({"user": UUID_A})),
        )
        .await
        .unwrap_err();
    assert!(err.message.starts_with("scope_denied"), "{}", err.message);

    // no_bound_channel: a twitch tool invoked from a discord event.
    let err = c
        .handle(
            &InvokeScope {
                origin_user_id: ActorId::new("500"),
                ..platform_event("discord")
            },
            invoke("platform.moderation.act", json!({"user": UUID_A})),
        )
        .await
        .unwrap_err();
    assert!(
        err.message.starts_with("no_bound_channel"),
        "{}",
        err.message
    );

    // no_linked_account: the target has no linked account in this community.
    let err = c
        .handle(
            &platform_event("twitch"),
            invoke(
                "platform.moderation.act",
                json!({"user": "11111111-2222-4333-8444-555566667777"}),
            ),
        )
        .await
        .unwrap_err();
    assert!(
        err.message.starts_with("no_linked_account"),
        "{}",
        err.message
    );

    // not_supported: kick registers no ops; an unregistered op is explicit.
    let c = build(
        rig_for("kick"),
        mod_tools("kick", "mod.ban"),
        FakeRelayQueue::with_live_drain(),
    );
    let err = c
        .handle(
            &platform_event("kick"),
            invoke_to(
                &svc_for("kick"),
                "platform.moderation.act",
                json!({"user": UUID_A}),
            ),
        )
        .await
        .unwrap_err();
    assert!(err.message.starts_with("not_supported"), "{}", err.message);
    assert!(c.relay_queue.envelopes().is_empty());

    // not_supported also covers a consumer that reports the op unsupported.
    let c = build(
        Rig::default(),
        mod_tools("twitch", "mod.ban"),
        FakeRelayQueue::with_drain_result(r#"{"ok":false,"code":"unsupported"}"#),
    );
    let err = c
        .handle(
            &platform_event("twitch"),
            invoke_to(
                &svc_for("twitch"),
                "platform.moderation.act",
                json!({"user": UUID_A}),
            ),
        )
        .await
        .unwrap_err();
    assert!(err.message.starts_with("not_supported"), "{}", err.message);
}

#[tokio::test]
async fn an_exhausted_platform_quota_is_a_distinct_refusal_not_a_generic_failure() {
    let c = build(
        rig_for("youtube"),
        mod_tools("youtube", "mod.ban"),
        FakeRelayQueue::with_drain_result(r#"{"ok":false,"code":"quota_exhausted"}"#),
    );
    let err = c
        .handle(
            &platform_event("youtube"),
            invoke_to(
                &svc_for("youtube"),
                "platform.moderation.act",
                json!({"user": UUID_A}),
            ),
        )
        .await
        .unwrap_err();
    assert_eq!(err.code, "target_unavailable");
    assert!(
        err.message.starts_with("quota_exhausted"),
        "{}",
        err.message
    );
}

#[tokio::test]
async fn every_moderation_tool_declaring_a_required_scope_needs_a_verified_moderator() {
    // The retired per-platform dispatch dropped `required_scope` (the action stage could not
    // verify an actor). With actor resolution, an app holding only the `tool.call` grant but
    // acting on behalf of a non-moderator (or no one) can never moderate.
    for (platform, op) in [
        ("twitch", "mod.ban"),
        ("discord", "mod.timeout"),
        ("youtube", "mod.ban"),
    ] {
        for author in [Some("501"), Some("777"), None] {
            let c = build(
                rig_for(platform),
                mod_tools(platform, op),
                FakeRelayQueue::with_live_drain(),
            );
            let mut sc = platform_event(platform);
            sc.origin_user_id = author.and_then(ActorId::new);
            let err = c
                .handle(
                    &sc,
                    invoke_to(
                        &svc_for(platform),
                        "platform.moderation.act",
                        json!({"user": UUID_A}),
                    ),
                )
                .await
                .unwrap_err();
            assert_eq!(err.code, "forbidden", "{platform} {author:?}");
            assert!(
                err.message.starts_with("scope_denied"),
                "{platform} {author:?}: {}",
                err.message
            );
            assert!(c.relay_queue.envelopes().is_empty());
        }
    }
}

// ---- security-review hardening ---------------------------------------------------------------

#[tokio::test]
async fn relay_push_can_never_reach_host_tool_only_ops_even_with_every_grant() {
    // `caps()` holds a permissive gate; the ops must still be refused before any grant check.
    for (provider, op) in [("twitch", "chat.clear"), ("discord", "webhook.create")] {
        let c = caps(FakeRelayQueue::with_live_drain());
        let err = c
            .handle(
                &scope(),
                HostCallBody {
                    app_id: "waddles.bot.commands.default".to_string(),
                    capability: CapabilityKind::Relay,
                    op: "send".to_string(),
                    args: json!({
                        "provider": provider,
                        "message_json": json!({"op": op, "text": "x"}).to_string(),
                    }),
                    call_id: 1,
                },
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "host_tool_only", "{provider} {op}");
        assert!(c.relay_queue.envelopes().is_empty(), "{provider} {op}");
    }
}

#[tokio::test]
async fn relay_push_refuses_eventsub_create_and_delete_but_list_still_queues() {
    // The host reconcile path alone may create/delete EventSub subscriptions; `caps()` holds
    // every grant, so the refusal must be `host_tool_only`, before any grant check or queueing.
    let mut refused = 0;
    for op in ["eventsub.create", "eventsub.delete"] {
        let c = caps(FakeRelayQueue::with_live_drain());
        let err = c
            .handle(
                &scope(),
                HostCallBody {
                    app_id: "waddles.bot.commands.default".to_string(),
                    capability: CapabilityKind::Relay,
                    op: "send".to_string(),
                    args: json!({
                        "provider": "twitch",
                        "message_json": json!({"op": op}).to_string(),
                    }),
                    call_id: 1,
                },
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "host_tool_only", "{op}");
        assert!(c.relay_queue.envelopes().is_empty(), "{op}");
        refused += 1;
    }
    assert_eq!(refused, 2, "denominator: eventsub ops refused");
    let c = caps(FakeRelayQueue::with_live_drain());
    c.handle(
        &scope(),
        HostCallBody {
            app_id: "waddles.bot.commands.default".to_string(),
            capability: CapabilityKind::Relay,
            op: "send".to_string(),
            args: json!({
                "provider": "twitch",
                "message_json": json!({"op": "eventsub.list"}).to_string(),
            }),
            call_id: 1,
        },
    )
    .await
    .unwrap();
    assert_eq!(c.relay_queue.envelopes().len(), 1);
}

#[tokio::test]
async fn chat_clear_needs_its_own_permission_not_chat_delete() {
    let tools = vec![exported(
        "twitch.chat.clear",
        Some("community:moderate"),
        Some(decl(
            json!({"platform": "twitch", "op": "chat.clear", "origin": "channel"}),
        )),
    )];
    let rig = Rig {
        svc_grants: vec![
            "chat.delete:twitch".to_string(),
            "moderation.twitch".to_string(),
        ],
        ..Rig::default()
    };
    let c = build(rig, tools, FakeRelayQueue::with_live_drain());
    let err = c
        .handle(
            &twitch_event(Some("500")),
            invoke("twitch.chat.clear", json!({})),
        )
        .await
        .unwrap_err();
    assert_eq!(err.code, "forbidden");
    assert!(c.relay_queue.envelopes().is_empty());
}

#[tokio::test]
async fn webhook_create_needs_webhook_manage_not_moderation() {
    let tools = vec![exported(
        "discord.webhook.create",
        None,
        Some(decl(json!({
            "platform": "discord", "op": "webhook.create", "text": "name",
            "required": ["name"], "returns": ["id"], "origin": "channel"
        }))),
    )];
    let args = json!({"name": "bridge"});
    // Holding moderation.discord is not enough ...
    let rig = Rig {
        svc_grants: vec!["moderation.discord".to_string()],
        ..Rig::default()
    };
    let c = build(
        rig,
        tools.clone(),
        FakeRelayQueue::with_drain_result(r#"{"ok":true,"data":{"id":"5","token":"SECRET"}}"#),
    );
    let err = c
        .handle(
            &platform_event("discord"),
            invoke_to(&svc_for("discord"), "discord.webhook.create", args.clone()),
        )
        .await
        .unwrap_err();
    assert_eq!(err.code, "forbidden");
    // ... webhook.manage is, and the token is still never returned.
    let rig = Rig {
        svc_grants: vec!["webhook.manage:discord".to_string()],
        ..Rig::default()
    };
    let c = build(
        rig,
        tools,
        FakeRelayQueue::with_drain_result(r#"{"ok":true,"data":{"id":"5","token":"SECRET"}}"#),
    );
    let mut sc = platform_event("discord");
    sc.origin_user_id = ActorId::new("500");
    let out = c
        .handle(
            &sc,
            invoke_to(&svc_for("discord"), "discord.webhook.create", args),
        )
        .await
        .unwrap_or_else(|e| panic!("{e:?}"));
    assert_eq!(out, json!({"ok": true, "id": "5"}));
}

#[tokio::test]
async fn host_ops_run_only_from_the_registered_platform_service_bundle() {
    let c = dcaps(FakeRelayQueue::with_live_drain());
    let d =
        decl(json!({"platform": "twitch", "op": "mod.ban", "user": "user", "required": ["user"]}));
    let mut plan = plan_for("twitch.moderation.ban", json!({"user": UUID_A}));
    for target in [
        "waddles.bot.other",
        DISCORD_SVC,
        "waddles.core.service.youtube",
    ] {
        plan.target_app_id = target.to_string();
        let r = c.run_host_tool(&twitch_event(Some("500")), &plan, &d).await;
        match r {
            Err(ToolRefusal::Forbidden(m)) => {
                assert!(m.starts_with("not_supported"), "{target}: {m}")
            }
            other => panic!("{target}: {other:?}"),
        }
    }
    assert!(c.relay_queue.envelopes().is_empty());
}

#[tokio::test]
async fn role_and_event_host_ops_require_the_service_bundles_moderation_grant() {
    // `caps()` grants the Discord service nothing: the typed classes are refused too.
    let c = caps(FakeRelayQueue::with_live_drain());
    let r = run_role(&c, &discord_scope(), ROLES_ADD, role_args(json!({}))).await;
    assert!(matches!(r, Err(ToolRefusal::Forbidden(_))), "{r:?}");
    let r = run_role(
        &c,
        &scope(),
        "discord.scheduled_event.cancel",
        json!({"event_id": "55"}),
    )
    .await;
    assert!(matches!(r, Err(ToolRefusal::Forbidden(_))), "{r:?}");
    assert!(c.relay_queue.envelopes().is_empty());
}

#[tokio::test]
async fn a_placeholder_user_wire_still_clears_the_protected_check_before_a_destructive_op() {
    let tools = vec![exported(
        "twitch.moderation.ban",
        None,
        Some(decl(json!({
            "platform": "twitch", "op": "mod.ban", "user": "user",
            "userWire": "placeholder", "required": ["user"]
        }))),
    )];
    let c = build(Rig::default(), tools, FakeRelayQueue::with_live_drain());
    // UUID_C is an admin: the placeholder wire must not skip the host-side check.
    let err = c
        .handle(
            &twitch_event(Some("500")),
            invoke("twitch.moderation.ban", json!({"user": UUID_C})),
        )
        .await
        .unwrap_err();
    assert_eq!(err.code, "forbidden");
    assert!(err.message.contains("protected_target"), "{}", err.message);
    assert!(c.relay_queue.envelopes().is_empty());
    // A non-protected target passes the check and rides the wire as the placeholder only.
    c.handle(
        &twitch_event(Some("500")),
        invoke("twitch.moderation.ban", json!({"user": UUID_B})),
    )
    .await
    .unwrap_or_else(|e| panic!("{}: {}", e.code, e.message));
    let envs = c.relay_queue.envelopes();
    assert_eq!(envs.len(), 1);
    assert_eq!(envs[0]["user_id"], format!("{{user:{UUID_B}}}"));
    assert!(
        !envs[0].to_string().contains("999000111"),
        "raw id never queued"
    );
}

#[tokio::test]
async fn relay_push_refuses_every_destructive_moderation_op_on_every_platform() {
    // The guest relay path has no verified actor, scope check or protected-target check: every
    // moderation op is host-tool-only. `caps()` holds a permissive gate (moderation.<p> granted),
    // so the refusal must come from `host_tool_only`, before any grant check.
    let mut checked = 0;
    for (provider, ops) in [
        ("twitch", vec!["mod.ban", "mod.timeout", "mod.unban"]),
        (
            "discord",
            vec!["mod.kick", "mod.ban", "mod.unban", "mod.timeout"],
        ),
        (
            "youtube",
            vec!["mod.ban", "mod.unban", "mod.add", "mod.remove"],
        ),
    ] {
        for op in ops {
            let c = caps(FakeRelayQueue::with_live_drain());
            let err = c
                .handle(
                    &twitch_event(Some("500")),
                    HostCallBody {
                        app_id: "waddles.bot.commands.default".to_string(),
                        capability: CapabilityKind::Relay,
                        op: "send".to_string(),
                        args: json!({
                            "provider": provider,
                            "message_json": json!({"op": op, "user_id": "141981764"}).to_string(),
                        }),
                        call_id: 1,
                    },
                )
                .await
                .unwrap_err();
            assert_eq!(err.code, "host_tool_only", "{provider} {op}");
            assert!(c.relay_queue.envelopes().is_empty(), "{provider} {op}");
            checked += 1;
        }
    }
    assert_eq!(checked, 3 + 4 + 4, "denominator: moderation ops attempted");
    // Kick registers no ops and is not a relay provider: refused outright, never queued.
    let c = caps(FakeRelayQueue::with_live_drain());
    let err = c
        .handle(
            &twitch_event(Some("500")),
            HostCallBody {
                app_id: "waddles.bot.commands.default".to_string(),
                capability: CapabilityKind::Relay,
                op: "send".to_string(),
                args: json!({
                    "provider": "kick",
                    "message_json": json!({"op": "mod.ban", "user_id": "1"}).to_string(),
                }),
                call_id: 1,
            },
        )
        .await
        .unwrap_err();
    assert_eq!(err.code, "unknown_provider");
    assert!(c.relay_queue.envelopes().is_empty());
}

#[tokio::test]
async fn the_generic_queued_producer_itself_refuses_host_tool_only_ops() {
    // Defense in depth: even called directly (a future caller forgetting the upstream check),
    // `handle_platform_queued_op` never queues a host-tool-only op.
    for (provider, op) in [
        ("twitch", "mod.ban"),
        ("discord", "mod.kick"),
        ("youtube", "mod.add"),
    ] {
        let c = caps(FakeRelayQueue::with_live_drain());
        let reg = bundle_capability_gate::platform_ops::registered(provider, op)
            .unwrap_or_else(|| panic!("{provider}/{op} must be registered"));
        let err = c
            .handle_platform_queued_op(
                &twitch_event(Some("500")),
                provider,
                RelayOp::Registered(reg),
                &json!({"user_id": "141981764"}),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "host_tool_only", "{provider} {op}");
        assert!(c.relay_queue.envelopes().is_empty());
    }
}
