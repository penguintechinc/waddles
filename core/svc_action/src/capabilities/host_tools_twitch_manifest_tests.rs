//! Real-path tests of the Twitch moderation tools: the declarations come from the bundle's REAL
//! manifest (`bundles/rust/twitch/bundle.yaml` -> consent summary fixture), and every call runs
//! through the generic host tool planner + producer pipeline. No hand-written copy of a
//! declaration appears here, so a manifest edit that breaks a tool breaks these tests.

use serde_json::{json, Value};

use super::super::super::{CapabilityHandler, StageCapabilities};
use super::{
    build, invoke, manifest_tools, twitch_event, FakeRelayQueue, Rig, UUID_A, UUID_B, UUID_C,
};

const EXPECTED: [(&str, &str); 4] = [
    ("twitch.moderation.ban", "mod.ban"),
    ("twitch.moderation.timeout", "mod.timeout"),
    ("twitch.moderation.unban", "mod.unban"),
    ("twitch.moderation.delete_message", "chat.delete"),
];

fn rig() -> Rig {
    Rig {
        svc_grants: vec![
            "moderation.twitch".to_string(),
            "chat.delete:twitch".to_string(),
        ],
        ..Rig::default()
    }
}

fn stage() -> StageCapabilities<FakeRelayQueue> {
    build(
        rig(),
        manifest_tools("twitch"),
        FakeRelayQueue::with_live_drain(),
    )
}

async fn call(
    c: &StageCapabilities<FakeRelayQueue>,
    author: &str,
    tool: &str,
    args: Value,
) -> Result<Value, String> {
    c.handle(&twitch_event(Some(author)), invoke(tool, args))
        .await
        .map_err(|e| format!("{}: {}", e.code, e.message))
}

#[test]
fn the_manifest_declares_exactly_the_moderation_host_ops_behind_the_moderate_scope() {
    let tools = manifest_tools("twitch");
    assert_eq!(tools.len(), EXPECTED.len(), "denominator");
    for (name, op) in EXPECTED {
        let t = tools
            .iter()
            .find(|t| t.name == name)
            .unwrap_or_else(|| panic!("{name} missing from the manifest"));
        let d = t.host_op.as_ref().expect("filtered to host-op tools");
        assert_eq!((d.platform.as_str(), d.op.as_str()), ("twitch", op), "{name}");
        assert!(t.write, "{name} is a write tool");
        assert_eq!(t.required_scope.as_deref(), Some("community:moderate"), "{name}");
    }
}

#[tokio::test]
async fn ban_and_timeout_detokenize_host_side_and_carry_params_in_text() {
    let c = stage();
    let out = call(
        &c,
        "500",
        "twitch.moderation.ban",
        json!({"user": UUID_B, "reason": "spam"}),
    )
    .await
    .unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(out, json!({"ok": true, "op": "mod.ban"}));
    call(
        &c,
        "500",
        "twitch.moderation.timeout",
        json!({"user": UUID_B, "duration": 60, "reason": "cool off"}),
    )
    .await
    .unwrap_or_else(|e| panic!("{e}"));
    let envs = c.relay_queue.envelopes();
    assert_eq!(envs.len(), 2);
    assert_eq!((envs[0]["op"].as_str(), envs[0]["user_id"].as_str()), (Some("mod.ban"), Some("999000111")));
    assert_eq!(envs[0]["text"], r#"{"reason":"spam"}"#);
    assert_eq!(envs[1]["op"], "mod.timeout");
    let text: Value = serde_json::from_str(envs[1]["text"].as_str().unwrap_or_default())
        .unwrap_or_else(|e| panic!("text is JSON: {e}"));
    assert_eq!(text, json!({"duration": 60, "reason": "cool off"}));
    for e in &envs {
        assert_eq!(e["channel"], "#somechannel");
        assert!(!e.to_string().contains(UUID_B), "the user UUID never leaves the host");
    }
}

#[tokio::test]
async fn unban_and_delete_message_map_their_wire_fields() {
    let c = stage();
    call(&c, "500", "twitch.moderation.unban", json!({"user": UUID_B}))
        .await
        .unwrap_or_else(|e| panic!("{e}"));
    call(
        &c,
        "500",
        "twitch.moderation.delete_message",
        json!({"message_id": "abc-123"}),
    )
    .await
    .unwrap_or_else(|e| panic!("{e}"));
    let envs = c.relay_queue.envelopes();
    assert_eq!(envs.len(), 2);
    assert_eq!((envs[0]["op"].as_str(), envs[0]["user_id"].as_str()), (Some("mod.unban"), Some("999000111")));
    assert_eq!(envs[1]["op"], "chat.delete");
    assert_eq!(envs[1]["message_id"], "abc-123");
    assert!(envs[1].get("user_id").is_none(), "delete names no user");
}

#[tokio::test]
async fn invalid_calls_are_refused_loudly_and_queue_nothing() {
    let c = stage();
    let cases = [
        ("twitch.moderation.ban", json!({})),
        ("twitch.moderation.ban", json!({"user": "1234"})),
        // `duration` is not mapped on ban: an unmapped argument is refused, never ignored.
        ("twitch.moderation.ban", json!({"user": UUID_B, "duration": 60})),
        ("twitch.moderation.timeout", json!({"user": UUID_B})),
        ("twitch.moderation.unban", json!({"user": UUID_B, "broadcaster_id": "9"})),
        ("twitch.moderation.ban", json!({"user": UUID_B, "moderator_id": "9"})),
        ("twitch.moderation.delete_message", json!({})),
        ("twitch.moderation.delete_message", json!({"message_id": "x", "channel": "c"})),
        ("twitch.moderation.ban", json!([])),
    ];
    for (tool, args) in cases {
        let err = call(&c, "500", tool, args.clone()).await.unwrap_err();
        assert!(err.starts_with("invalid_args"), "{tool} {args}: {err}");
    }
    assert!(c.relay_queue.envelopes().is_empty());
}

#[tokio::test]
async fn only_a_verified_moderator_may_call_and_protected_targets_are_refused() {
    let c = stage();
    // 501 is a plain member: scope_denied before anything is queued.
    let err = call(&c, "501", "twitch.moderation.ban", json!({"user": UUID_A}))
        .await
        .unwrap_err();
    assert!(err.starts_with("forbidden") && err.contains("scope_denied"), "{err}");
    // 500 is a moderator, but UUID_C resolves to platform id 600, a community admin.
    for tool in ["twitch.moderation.ban", "twitch.moderation.unban"] {
        let args = json!({"user": UUID_C});
        let r = call(&c, "500", tool, args).await;
        if tool.ends_with("ban") && !tool.ends_with("unban") {
            let err = r.unwrap_err();
            assert!(err.starts_with("forbidden") && err.contains("protected_target"), "{err}");
        } else {
            r.unwrap_or_else(|e| panic!("unban of a protected user is allowed: {e}"));
        }
    }
    assert_eq!(c.relay_queue.envelopes().len(), 1, "only the unban was queued");
}
