//! Real-path tests of the Discord moderation, message, embed and webhook tools: the declarations
//! come from the bundle's REAL manifest (`bundles/rust/discord/bundle.yaml` -> consent summary
//! fixture) and every call runs through the generic host tool planner + producer pipeline. No
//! hand-written copy of a declaration appears here, so a manifest edit that breaks a tool breaks
//! these tests.

use serde_json::{json, Value};

use super::super::super::{ActorId, CapabilityHandler, StageCapabilities};
use super::{
    build, invoke_to, manifest_tools, platform_event, svc_for, FakeRelayQueue, Rig, UUID_A, UUID_B,
    UUID_C,
};

/// `(tool, registered op, required scope)` for every tool this file covers.
const EXPECTED: [(&str, &str, Option<&str>); 9] = [
    (
        "discord.moderation.kick",
        "mod.kick",
        Some("community:moderate"),
    ),
    (
        "discord.moderation.unban",
        "mod.unban",
        Some("community:moderate"),
    ),
    (
        "discord.moderation.ban",
        "mod.ban",
        Some("community:moderate"),
    ),
    (
        "discord.moderation.timeout",
        "mod.timeout",
        Some("community:moderate"),
    ),
    (
        "discord.moderation.untimeout",
        "mod.timeout",
        Some("community:moderate"),
    ),
    (
        "discord.message.edit",
        "message.edit",
        Some("community:moderate"),
    ),
    (
        "discord.message.react",
        "reaction.add",
        Some("community:moderate"),
    ),
    (
        "discord.message.delete",
        "chat.delete",
        Some("community:moderate"),
    ),
    (
        "discord.webhook.create",
        "webhook.create",
        Some("community:moderate"),
    ),
];

const CHANNEL: &str = "900000000000000001";

fn rig(extra_grants: &[&str]) -> Rig {
    let mut svc_grants: Vec<String> = [
        "moderation.discord",
        "chat.delete:discord",
        "message.edit:discord",
        "message.reaction:discord",
        "chat.send:discord",
        "webhook.manage:discord",
    ]
    .iter()
    .map(ToString::to_string)
    .collect();
    svc_grants.retain(|g| !extra_grants.contains(&format!("-{g}").as_str()));
    Rig {
        svc_grants,
        ..Rig::default()
    }
}

fn stage_with(rig: Rig, queue: FakeRelayQueue) -> StageCapabilities<FakeRelayQueue> {
    build(rig, manifest_tools("discord"), queue)
}

fn stage() -> StageCapabilities<FakeRelayQueue> {
    stage_with(rig(&[]), FakeRelayQueue::with_live_drain())
}

async fn call_as(
    c: &StageCapabilities<FakeRelayQueue>,
    author: &str,
    tool: &str,
    args: Value,
) -> Result<Value, String> {
    let mut sc = platform_event("discord");
    sc.origin_user_id = ActorId::new(author);
    c.handle(&sc, invoke_to(&svc_for("discord"), tool, args))
        .await
        .map_err(|e| format!("{}: {}", e.code, e.message))
}

async fn call(
    c: &StageCapabilities<FakeRelayQueue>,
    tool: &str,
    args: Value,
) -> Result<Value, String> {
    call_as(c, "500", tool, args).await
}

#[test]
fn the_manifest_declares_every_tool_with_its_op_and_scope() {
    let tools = manifest_tools("discord");
    for (name, op, scope) in EXPECTED {
        let t = tools
            .iter()
            .find(|t| t.name == name)
            .unwrap_or_else(|| panic!("{name} missing from the manifest"));
        let d = t.host_op.as_ref().expect("filtered to host-op tools");
        assert_eq!(
            (d.platform.as_str(), d.op.as_str()),
            ("discord", op),
            "{name}"
        );
        assert!(t.write, "{name} is a write tool");
        assert_eq!(t.required_scope.as_deref(), scope, "{name}");
    }
    for name in [
        "discord.message.embed",
        "discord.member.roles.add",
        "discord.scheduled_event.create",
    ] {
        assert!(tools.iter().any(|t| t.name == name), "{name}");
    }
    // 4 events + 4 role tools + 9 above + embed
    assert_eq!(tools.len(), 18, "denominator");
}

#[tokio::test]
async fn moderation_ops_carry_a_placeholder_user_and_their_params_in_text() {
    let c = stage();
    for (tool, args) in [
        ("discord.moderation.kick", json!({"user": UUID_B})),
        ("discord.moderation.unban", json!({"user": UUID_B})),
        (
            "discord.moderation.ban",
            json!({"user": UUID_B, "delete_message_seconds": 3600}),
        ),
        ("discord.moderation.ban", json!({"user": UUID_B})),
        (
            "discord.moderation.timeout",
            json!({"user": UUID_B, "duration_seconds": 600}),
        ),
        (
            "discord.moderation.untimeout",
            json!({"user": UUID_B, "clear": true}),
        ),
    ] {
        call(&c, tool, args.clone())
            .await
            .unwrap_or_else(|e| panic!("{tool} {args}: {e}"));
    }
    let envs = c.relay_queue.envelopes();
    assert_eq!(envs.len(), 6);
    let ops: Vec<_> = envs
        .iter()
        .map(|e| e["op"].as_str().unwrap_or(""))
        .collect();
    assert_eq!(
        ops,
        [
            "mod.kick",
            "mod.unban",
            "mod.ban",
            "mod.ban",
            "mod.timeout",
            "mod.timeout"
        ]
    );
    let text = |i: usize| -> Option<Value> {
        envs[i]["text"]
            .as_str()
            .map(|t| serde_json::from_str(t).unwrap_or_else(|e| panic!("text is JSON: {e}")))
    };
    assert_eq!(text(0), None);
    assert_eq!(text(2), Some(json!({"delete_message_seconds": 3600})));
    assert_eq!(text(3), None);
    assert_eq!(text(4), Some(json!({"duration_seconds": 600})));
    assert_eq!(text(5), Some(json!({"clear": true})));
    for e in &envs {
        assert_eq!(e["platform"], "discord");
        assert_eq!(
            e["user_id"],
            format!("{{user:{UUID_B}}}"),
            "tokenized, never raw"
        );
        assert_eq!(e["origin_channel"], CHANNEL, "guild-scope origin pin");
        assert!(e.get("channel").is_none() && e.get("guild_id").is_none());
        assert!(e["pin_sig"].as_str().is_some());
    }
}

#[tokio::test]
async fn only_a_verified_moderator_may_call_and_protected_targets_are_refused() {
    let c = stage();
    // 501 is a plain member.
    let err = call_as(
        &c,
        "501",
        "discord.moderation.kick",
        json!({"user": UUID_A}),
    )
    .await
    .unwrap_err();
    assert!(
        err.starts_with("forbidden") && err.contains("scope_denied"),
        "{err}"
    );
    // UUID_C resolves to platform id 600, a community admin: destructive ops are refused ...
    for (tool, args) in [
        ("discord.moderation.kick", json!({"user": UUID_C})),
        ("discord.moderation.ban", json!({"user": UUID_C})),
        (
            "discord.moderation.timeout",
            json!({"user": UUID_C, "duration_seconds": 60}),
        ),
    ] {
        let err = call(&c, tool, args).await.unwrap_err();
        assert!(
            err.starts_with("forbidden") && err.contains("protected_target"),
            "{tool}: {err}"
        );
    }
    // ... lifting a ban is not destructive.
    call(&c, "discord.moderation.unban", json!({"user": UUID_C}))
        .await
        .unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(
        c.relay_queue.envelopes().len(),
        1,
        "only the unban was queued"
    );
}

#[tokio::test]
async fn message_ops_map_their_wire_fields_and_pin_the_origin_channel() {
    let c = stage();
    call(
        &c,
        "discord.message.edit",
        json!({"message_id": "42", "content": "fixed"}),
    )
    .await
    .unwrap_or_else(|e| panic!("{e}"));
    call(
        &c,
        "discord.message.react",
        json!({"message_id": "42", "emoji": "\u{1F44D}"}),
    )
    .await
    .unwrap_or_else(|e| panic!("{e}"));
    call(&c, "discord.message.delete", json!({"message_id": "42"}))
        .await
        .unwrap_or_else(|e| panic!("{e}"));
    let envs = c.relay_queue.envelopes();
    let shape: Vec<_> = envs
        .iter()
        .map(|e| {
            (
                e["op"].as_str().unwrap_or(""),
                e["message_id"].as_str().unwrap_or(""),
                e["text"].as_str(),
                e["channel"].as_str().unwrap_or(""),
            )
        })
        .collect();
    assert_eq!(
        shape,
        [
            ("message.edit", "42", Some("fixed"), CHANNEL),
            ("reaction.add", "42", Some("\u{1F44D}"), CHANNEL),
            ("chat.delete", "42", None, CHANNEL),
        ]
    );
    assert!(envs.iter().all(|e| e.get("user_id").is_none()));
}

#[tokio::test]
async fn message_embed_carries_its_embeds_through_text_object() {
    let c = stage_with(
        rig(&[]),
        FakeRelayQueue::with_drain_result(r#"{"ok":true,"id":"900000000000000009"}"#),
    );
    let embeds = json!([{"title": "Event", "description": "Tonight", "color": 255}]);
    let out = call(
        &c,
        "discord.message.embed",
        json!({"content": "hello", "embeds": embeds}),
    )
    .await
    .unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(out, json!({"ok": true, "id": "900000000000000009"}));
    let envs = c.relay_queue.envelopes();
    assert_eq!(envs.len(), 1);
    assert_eq!(envs[0]["op"], "message.embed");
    assert_eq!(envs[0]["channel"], CHANNEL);
    let text: Value = serde_json::from_str(envs[0]["text"].as_str().unwrap_or_default())
        .unwrap_or_else(|e| panic!("text is JSON: {e}"));
    assert_eq!(text, json!({"content": "hello", "embeds": embeds}));
    // The content is optional, the embeds are not.
    call(&c, "discord.message.embed", json!({"embeds": embeds}))
        .await
        .unwrap_or_else(|e| panic!("{e}"));
    let err = call(&c, "discord.message.embed", json!({"content": "x"}))
        .await
        .unwrap_err();
    assert!(err.starts_with("invalid_args"), "{err}");
}

#[tokio::test]
async fn webhook_create_needs_its_own_grant_and_never_returns_the_token() {
    let reply = r#"{"ok":true,"data":{"id":"5","token":"SECRET-TOKEN"}}"#;
    let c = stage_with(rig(&[]), FakeRelayQueue::with_drain_result(reply));
    let out = call(&c, "discord.webhook.create", json!({"name": "bridge"}))
        .await
        .unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(out, json!({"ok": true, "id": "5"}));
    assert!(!out.to_string().contains("SECRET"));
    // Without webhook.manage:discord (moderation.discord alone is not enough) nothing is queued.
    let c = stage_with(
        rig(&["-webhook.manage:discord"]),
        FakeRelayQueue::with_drain_result(reply),
    );
    let err = call(&c, "discord.webhook.create", json!({"name": "bridge"}))
        .await
        .unwrap_err();
    assert!(err.starts_with("forbidden"), "{err}");
    assert!(c.relay_queue.envelopes().is_empty());
    // A plain member lacks the moderate scope.
    let c = stage_with(rig(&[]), FakeRelayQueue::with_drain_result(reply));
    let err = call_as(
        &c,
        "501",
        "discord.webhook.create",
        json!({"name": "bridge"}),
    )
    .await
    .unwrap_err();
    assert!(err.contains("scope_denied"), "{err}");
}

#[tokio::test]
async fn embed_and_message_tools_need_their_platform_grants() {
    for (missing, tool, args) in [
        (
            "-chat.send:discord",
            "discord.message.embed",
            json!({"embeds": [{"title": "t"}]}),
        ),
        (
            "-message.edit:discord",
            "discord.message.edit",
            json!({"message_id": "1", "content": "x"}),
        ),
        (
            "-message.reaction:discord",
            "discord.message.react",
            json!({"message_id": "1", "emoji": "\u{1F44D}"}),
        ),
        (
            "-chat.delete:discord",
            "discord.message.delete",
            json!({"message_id": "1"}),
        ),
        (
            "-moderation.discord",
            "discord.moderation.kick",
            json!({"user": UUID_A}),
        ),
    ] {
        let c = stage_with(rig(&[missing]), FakeRelayQueue::with_live_drain());
        let err = call(&c, tool, args).await.unwrap_err();
        assert!(err.starts_with("forbidden"), "{tool}: {err}");
        assert!(c.relay_queue.envelopes().is_empty(), "{tool}");
    }
}

#[tokio::test]
async fn invalid_calls_are_refused_loudly_and_queue_nothing() {
    let c = stage();
    let cases = [
        ("discord.moderation.kick", json!({})),
        (
            "discord.moderation.kick",
            json!({"user": "700000000000000001"}),
        ),
        (
            "discord.moderation.kick",
            json!({"user": UUID_A, "guild_id": "1"}),
        ),
        (
            "discord.moderation.ban",
            json!({"user": UUID_A, "reason": "x"}),
        ),
        ("discord.moderation.timeout", json!({"user": UUID_A})),
        (
            "discord.moderation.timeout",
            json!({"user": UUID_A, "duration_seconds": 5, "channel": "9"}),
        ),
        ("discord.moderation.untimeout", json!({"user": UUID_A})),
        (
            "discord.message.edit",
            json!({"message_id": "1", "content": "x", "channel_id": "9"}),
        ),
        ("discord.message.edit", json!({"message_id": "1"})),
        ("discord.message.react", json!({"emoji": "\u{1F44D}"})),
        ("discord.message.delete", json!([])),
        (
            "discord.message.embed",
            json!({"embeds": [{"title": "t"}], "channel_id": "9"}),
        ),
        (
            "discord.webhook.create",
            json!({"name": "b", "avatar": "x"}),
        ),
        ("discord.webhook.create", json!({})),
    ];
    for (tool, args) in cases {
        let err = call(&c, tool, args.clone()).await.unwrap_err();
        assert!(err.starts_with("invalid_args"), "{tool} {args}: {err}");
    }
    assert!(c.relay_queue.envelopes().is_empty());
}

#[tokio::test]
async fn a_non_moderator_is_refused_every_message_and_embed_tool_and_nothing_is_queued() {
    let c = stage_with(
        rig(&[]),
        FakeRelayQueue::with_drain_result(r#"{"ok":true,"id":"900000000000000009"}"#),
    );
    for (tool, args) in [
        (
            "discord.message.edit",
            json!({"message_id": "42", "content": "x"}),
        ),
        (
            "discord.message.react",
            json!({"message_id": "42", "emoji": "\u{1F44D}"}),
        ),
        ("discord.message.delete", json!({"message_id": "42"})),
        ("discord.message.embed", json!({"embeds": [{"title": "t"}]})),
        ("discord.webhook.create", json!({"name": "bridge"})),
    ] {
        // 501 is a plain member; 500 (a moderator) is accepted for the same call.
        let err = call_as(&c, "501", tool, args.clone()).await.unwrap_err();
        assert!(
            err.starts_with("forbidden") && err.contains("scope_denied"),
            "{tool}: {err}"
        );
        call_as(&c, "500", tool, args)
            .await
            .unwrap_or_else(|e| panic!("{tool} as moderator: {e}"));
    }
    assert_eq!(
        c.relay_queue.envelopes().len(),
        5,
        "only the moderator calls queued"
    );
}
