//! Real-path tests of the YouTube chat moderation tools: the declarations come from the bundle's
//! REAL manifest (`bundles/rust/youtube/bundle.yaml` -> consent summary fixture) and every call
//! runs through the generic host tool planner + producer pipeline. The YouTube-specific
//! id-ownership / quota duties live in svc_ingest's drain (`YoutubeDuties`); here we prove the
//! producer side: origin pin, scope, host-side protected-target check, placeholder user wire,
//! closed argument set and the ban/moderator id surfaced back.

use serde_json::{json, Value};

use super::super::super::{CapabilityHandler, InvokeScope, StageCapabilities};
use super::{
    build, invoke_to, manifest_tools, platform_event, svc_for, ActorId, FakeRelayQueue, Rig,
    UUID_A, UUID_B, UUID_C,
};

const EXPECTED: [(&str, &str); 5] = [
    ("youtube.chat.delete_message", "chat.delete"),
    ("youtube.chat.ban", "mod.ban"),
    ("youtube.chat.unban", "mod.unban"),
    ("youtube.chat.add_moderator", "mod.add"),
    ("youtube.chat.remove_moderator", "mod.remove"),
];

fn rig() -> Rig {
    Rig {
        svc_grants: vec![
            "moderation.youtube".to_string(),
            "chat.delete:youtube".to_string(),
        ],
        ..Rig::default()
    }
}

fn stage(queue: FakeRelayQueue) -> StageCapabilities<FakeRelayQueue> {
    build(rig(), manifest_tools("youtube"), queue)
}

fn event(author: &str) -> InvokeScope {
    InvokeScope {
        origin_user_id: ActorId::new(author),
        ..platform_event("youtube")
    }
}

async fn call(
    c: &StageCapabilities<FakeRelayQueue>,
    author: &str,
    tool: &str,
    args: Value,
) -> Result<Value, String> {
    c.handle(&event(author), invoke_to(&svc_for("youtube"), tool, args))
        .await
        .map_err(|e| format!("{}: {}", e.code, e.message))
}

#[test]
fn the_manifest_declares_exactly_the_youtube_moderation_host_ops_behind_the_moderate_scope() {
    let tools = manifest_tools("youtube");
    assert_eq!(tools.len(), EXPECTED.len(), "denominator");
    for (name, op) in EXPECTED {
        let t = tools
            .iter()
            .find(|t| t.name == name)
            .unwrap_or_else(|| panic!("{name} missing from the manifest"));
        let d = t.host_op.as_ref().expect("filtered to host-op tools");
        assert_eq!(
            (d.platform.as_str(), d.op.as_str()),
            ("youtube", op),
            "{name}"
        );
        assert!(t.write, "{name} is a write tool");
        assert_eq!(
            t.required_scope.as_deref(),
            Some("community:moderate"),
            "{name}"
        );
    }
}

#[tokio::test]
async fn ban_and_add_moderator_keep_the_user_a_placeholder_and_return_the_resource_id() {
    let ack = r#"{"ok":true,"data":{"id":"banNew1","snippet":{"secret":"x"}}}"#;
    let c = stage(FakeRelayQueue::with_drain_result(ack));
    let out = call(
        &c,
        "500",
        "youtube.chat.ban",
        json!({"user": UUID_B, "duration_seconds": "600"}),
    )
    .await
    .unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(
        out,
        json!({"ok": true, "id": "banNew1"}),
        "only declared returns"
    );
    let out = call(
        &c,
        "500",
        "youtube.chat.add_moderator",
        json!({"user": UUID_B}),
    )
    .await
    .unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(out["id"], "banNew1");
    let envs = c.relay_queue.envelopes();
    assert_eq!(envs.len(), 2);
    assert_eq!(envs[0]["op"], "mod.ban");
    assert_eq!(envs[0]["platform"], "youtube");
    assert_eq!(envs[0]["user_id"], format!("{{user:{UUID_B}}}"));
    assert_eq!(envs[0]["text"], "600");
    assert_eq!(envs[0]["origin_channel"], "UCabc123_-x");
    assert_eq!(envs[1]["op"], "mod.add");
    assert!(envs[1].get("text").is_none());
    for e in &envs {
        assert!(
            !e.to_string().contains("999000111"),
            "raw platform id never queued"
        );
        assert!(e["pin_sig"].as_str().is_some(), "signed envelope");
    }
}

#[tokio::test]
async fn permanent_ban_carries_no_duration() {
    let c = stage(FakeRelayQueue::with_drain_result(
        r#"{"ok":true,"data":{"id":"b2"}}"#,
    ));
    call(&c, "500", "youtube.chat.ban", json!({"user": UUID_B}))
        .await
        .unwrap_or_else(|e| panic!("{e}"));
    assert!(c.relay_queue.envelopes()[0].get("text").is_none());
}

#[tokio::test]
async fn delete_unban_and_remove_moderator_map_their_id_into_message_id() {
    let c = stage(FakeRelayQueue::with_live_drain());
    for (tool, arg, value, op) in [
        (
            "youtube.chat.delete_message",
            "message_id",
            "LCC.m-1",
            "chat.delete",
        ),
        ("youtube.chat.unban", "ban_id", "ban_9", "mod.unban"),
        (
            "youtube.chat.remove_moderator",
            "moderator_id",
            "mod_3",
            "mod.remove",
        ),
    ] {
        let out = call(&c, "500", tool, json!({ arg: value }))
            .await
            .unwrap_or_else(|e| panic!("{tool}: {e}"));
        assert_eq!(out, json!({"ok": true, "op": op}));
    }
    let envs = c.relay_queue.envelopes();
    assert_eq!(envs.len(), 3, "denominator: ops queued");
    for (e, id) in envs.iter().zip(["LCC.m-1", "ban_9", "mod_3"]) {
        assert_eq!(e["message_id"], id);
        assert!(e.get("user_id").is_none());
    }
}

#[tokio::test]
async fn host_pinned_fields_and_bad_values_are_refused_and_queue_nothing() {
    let c = stage(FakeRelayQueue::with_live_drain());
    let cases = [
        ("youtube.chat.delete_message", json!({})),
        (
            "youtube.chat.delete_message",
            json!({"message_id": "m", "live_chat_id": "x"}),
        ),
        (
            "youtube.chat.ban",
            json!({"user": UUID_B, "channel_id": "UCx"}),
        ),
        ("youtube.chat.ban", json!({"user": "UCrawchannel"})),
        (
            "youtube.chat.ban",
            json!({"user": UUID_B, "duration_seconds": 600}),
        ),
        (
            "youtube.chat.add_moderator",
            json!({"user": UUID_B.to_uppercase()}),
        ),
        ("youtube.chat.unban", json!({"message_id": "b1"})),
        ("youtube.chat.remove_moderator", json!([])),
    ];
    for (tool, args) in cases {
        let err = call(&c, "500", tool, args.clone()).await.unwrap_err();
        assert!(err.starts_with("invalid_args"), "{tool} {args}: {err}");
    }
    assert!(c.relay_queue.envelopes().is_empty());
}

#[tokio::test]
async fn a_call_without_a_youtube_origin_chat_is_refused_and_queues_nothing() {
    let c = stage(FakeRelayQueue::with_live_drain());
    let twitch = InvokeScope {
        origin_platform: Some("twitch".to_string()),
        origin_channel_id: Some("somechannel".to_string()),
        ..event("500")
    };
    let none = InvokeScope {
        origin_platform: None,
        origin_channel_id: None,
        ..event("500")
    };
    for scope in [twitch, none] {
        let err = c
            .handle(
                &scope,
                invoke_to(
                    &svc_for("youtube"),
                    "youtube.chat.delete_message",
                    json!({"message_id": "LCC.m1"}),
                ),
            )
            .await
            .unwrap_err();
        // The verified actor cannot be resolved for a non-youtube / absent origin (scope_denied) or
        // the pin refuses it (no_bound_channel): either way the call dies before the queue.
        assert!(
            err.message.contains("no_bound_channel") || err.message.contains("scope_denied"),
            "{}: {}",
            err.code,
            err.message
        );
    }
    assert!(c.relay_queue.envelopes().is_empty());
}

#[tokio::test]
async fn only_a_verified_moderator_may_call_and_protected_targets_are_refused() {
    let c = stage(FakeRelayQueue::with_live_drain());
    // 501 is a plain member.
    let err = call(&c, "501", "youtube.chat.ban", json!({"user": UUID_A}))
        .await
        .unwrap_err();
    assert!(
        err.starts_with("forbidden") && err.contains("scope_denied"),
        "{err}"
    );
    // 500 is a moderator, but UUID_C resolves to platform id 600, a community admin: the
    // placeholder wire must not skip the host-side protected-target check.
    let err = call(&c, "500", "youtube.chat.ban", json!({"user": UUID_C}))
        .await
        .unwrap_err();
    assert!(
        err.starts_with("forbidden") && err.contains("protected_target"),
        "{err}"
    );
    assert!(
        c.relay_queue.envelopes().is_empty(),
        "nothing queued on any refusal"
    );
}

#[tokio::test]
async fn a_drain_quota_refusal_is_surfaced_as_quota_exhausted() {
    let c = stage(FakeRelayQueue::with_drain_result(
        r#"{"ok":false,"code":"quota_exhausted"}"#,
    ));
    let err = call(
        &c,
        "500",
        "youtube.chat.delete_message",
        json!({"message_id": "LCC.m1"}),
    )
    .await
    .unwrap_err();
    assert!(
        err.starts_with("target_unavailable") && err.contains("quota_exhausted"),
        "{err}"
    );
}
