#![allow(clippy::unwrap_used, clippy::expect_used)]
//! Per-kind tests for the pre-populated v3.0 op families. Each kind gets one template the
//! platform bundle is expected to emit (accepted) plus one-mutation refusals proving the gate
//! that matters for THAT kind fires: path/slot/duty enforcement (a guild or token can never be a
//! literal), body-key allowlists, credential scope, and fail-closed on any mismatch.

use super::*;
use crate::connector::sender_exec::verify::{verify, TemplateError};
use crate::connector::sender_exec::RequestTpl;

fn refs(pairs: &[(&str, &str)]) -> Vec<(String, String)> {
    pairs
        .iter()
        .map(|(a, b)| ((*a).to_string(), (*b).to_string()))
        .collect()
}

fn tpl(
    method: &str,
    url: &str,
    headers: &[(&str, &str)],
    body: Option<serde_json::Value>,
    secret_refs: &[(&str, &str)],
) -> RequestTpl {
    RequestTpl {
        method: method.into(),
        url: url.into(),
        headers: refs(headers),
        body: body.map(|b| b.to_string().into_bytes()),
        secret_refs: refs(secret_refs),
    }
}

fn check(p: &PlatformPolicy, kind: &str, t: &RequestTpl) -> Result<(), TemplateError> {
    verify(p, p.kind(kind).expect("kind exists"), t).map(|_| ())
}

const TW_HDRS: [(&str, &str); 3] = [
    ("Authorization", "Bearer {{TWITCH_TOKEN}}"),
    ("Client-Id", "{{TWITCH_CLIENT_ID}}"),
    ("Content-Type", "application/json"),
];
const TW_REFS: [(&str, &str); 4] = [
    ("{{TWITCH_TOKEN}}", "token"),
    ("{{TWITCH_CLIENT_ID}}", "client_id"),
    ("{{TWITCH_BROADCASTER_ID}}", "broadcaster_id"),
    ("{{TWITCH_SENDER_ID}}", "sender_id"),
];
const TW_MOD: &str = "broadcaster_id={{TWITCH_BROADCASTER_ID}}&moderator_id={{TWITCH_SENDER_ID}}";

#[test]
fn twitch_moderation_kinds_are_exact_and_use_host_ids_only() {
    let p = PlatformPolicy::twitch();
    for (kind, body) in [
        (
            "mod.ban",
            serde_json::json!({"data": {"user_id": "123", "reason": "spam"}}),
        ),
        (
            "mod.timeout",
            serde_json::json!({"data": {"user_id": "123", "duration": 600}}),
        ),
    ] {
        let url = format!("https://api.twitch.tv/helix/moderation/bans?{TW_MOD}");
        let good = tpl("POST", &url, &TW_HDRS, Some(body.clone()), &TW_REFS);
        assert_eq!(check(&p, kind, &good), Ok(()), "{kind}");
        // A literal broadcaster id (bundle-chosen) is refused: it must be the host slot.
        let url = "https://api.twitch.tv/helix/moderation/bans?broadcaster_id=999&moderator_id={{TWITCH_SENDER_ID}}";
        let bad = tpl("POST", url, &TW_HDRS, Some(body.clone()), &TW_REFS);
        assert_eq!(
            check(&p, kind, &bad),
            Err(TemplateError::SlotMisplaced),
            "{kind}"
        );
        // Wrong path / wrong method / extra query key all refused.
        let url = format!("https://api.twitch.tv/helix/moderation/ban?{TW_MOD}");
        let bad = tpl("POST", &url, &TW_HDRS, Some(body.clone()), &TW_REFS);
        assert_eq!(check(&p, kind, &bad), Err(TemplateError::PathMismatch));
        let url = format!("https://api.twitch.tv/helix/moderation/bans?{TW_MOD}");
        let bad = tpl("PUT", &url, &TW_HDRS, Some(body.clone()), &TW_REFS);
        assert_eq!(check(&p, kind, &bad), Err(TemplateError::BadMethod));
        let url = format!("https://api.twitch.tv/helix/moderation/bans?{TW_MOD}&extra=1");
        let bad = tpl("POST", &url, &TW_HDRS, Some(body), &TW_REFS);
        assert_eq!(check(&p, kind, &bad), Err(TemplateError::QueryMismatch));
    }
}

#[test]
fn twitch_unban_is_a_bodyless_delete_with_an_exact_query() {
    let p = PlatformPolicy::twitch();
    let url = format!("https://api.twitch.tv/helix/moderation/bans?{TW_MOD}&user_id=123");
    let good = tpl("DELETE", &url, &TW_HDRS[..2], None, &TW_REFS);
    assert_eq!(check(&p, "mod.unban", &good), Ok(()));
    let with_body = tpl(
        "DELETE",
        &url,
        &TW_HDRS,
        Some(serde_json::json!({"x": 1})),
        &TW_REFS,
    );
    assert_eq!(
        check(&p, "mod.unban", &with_body),
        Err(TemplateError::BodyPresence)
    );
    let missing_user = format!("https://api.twitch.tv/helix/moderation/bans?{TW_MOD}");
    let bad = tpl("DELETE", &missing_user, &TW_HDRS[..2], None, &TW_REFS);
    assert_eq!(
        check(&p, "mod.unban", &bad),
        Err(TemplateError::QueryMismatch)
    );
}

const APP_HDRS: [(&str, &str); 3] = [
    ("Authorization", "Bearer {{TWITCH_APP_TOKEN}}"),
    ("Client-Id", "{{TWITCH_APP_CLIENT_ID}}"),
    ("Content-Type", "application/json"),
];
const APP_REFS: [(&str, &str); 5] = [
    ("{{TWITCH_APP_TOKEN}}", "app.token"),
    ("{{TWITCH_APP_CLIENT_ID}}", "app.client_id"),
    ("{{TWITCH_APP_BROADCASTER_ID}}", "app.broadcaster_id"),
    ("{{TWITCH_EVENTSUB_CALLBACK}}", "app.eventsub_callback"),
    ("{{TWITCH_EVENTSUB_SECRET}}", "app.eventsub_secret"),
];

fn eventsub_body() -> serde_json::Value {
    serde_json::json!({
        "type": "channel.follow",
        "version": "2",
        "condition": {
            "broadcaster_user_id": "{{TWITCH_APP_BROADCASTER_ID}}",
        },
        "transport": {
            "method": "webhook",
            "callback": "{{TWITCH_EVENTSUB_CALLBACK}}",
            "secret": "{{TWITCH_EVENTSUB_SECRET}}",
        },
    })
}

#[test]
fn twitch_eventsub_kinds_are_app_scoped_and_key_restricted() {
    let p = PlatformPolicy::twitch();
    let url = "https://api.twitch.tv/helix/eventsub/subscriptions";
    let good = tpl("POST", url, &APP_HDRS, Some(eventsub_body()), &APP_REFS);
    assert_eq!(check(&p, "eventsub.create", &good), Ok(()));
    // The USER token slot is not allowed on an app-scoped kind.
    let user_hdrs = [
        ("Authorization", "Bearer {{TWITCH_TOKEN}}"),
        ("Client-Id", "{{TWITCH_APP_CLIENT_ID}}"),
        ("Content-Type", "application/json"),
    ];
    let bad = tpl("POST", url, &user_hdrs, Some(eventsub_body()), &APP_REFS);
    assert_eq!(
        check(&p, "eventsub.create", &bad),
        Err(TemplateError::SlotNotAllowedForKind)
    );
    // A callback / secret literal is not a slot position: refused (host-held only).
    let mut body = eventsub_body();
    body["transport"]["secret"] = serde_json::json!("hunter2");
    let lit = tpl("POST", url, &APP_HDRS, Some(body), &APP_REFS[..4]);
    assert_eq!(
        check(&p, "eventsub.create", &lit),
        Err(TemplateError::SlotMisplaced)
    );
    // ...and so is a literal broadcaster id in the condition (an app token could otherwise
    // subscribe to ANY channel).
    let mut body = eventsub_body();
    body["condition"]["broadcaster_user_id"] = serde_json::json!("12345");
    let lit = tpl("POST", url, &APP_HDRS, Some(body), &APP_REFS);
    assert_eq!(
        check(&p, "eventsub.create", &lit),
        Err(TemplateError::SlotMisplaced)
    );
    // An unknown top-level body key is refused.
    let mut body = eventsub_body();
    body["extra"] = serde_json::json!(1);
    let bad = tpl("POST", url, &APP_HDRS, Some(body), &APP_REFS);
    assert_eq!(
        check(&p, "eventsub.create", &bad),
        Err(TemplateError::BodyKeyNotAllowed)
    );
    // delete: exact `id` query, no body.
    let del = tpl(
        "DELETE",
        "https://api.twitch.tv/helix/eventsub/subscriptions?id=abc-123",
        &APP_HDRS[..2],
        None,
        &APP_REFS[..2],
    );
    assert_eq!(check(&p, "eventsub.delete", &del), Ok(()));
    let del_no_id = tpl("DELETE", url, &APP_HDRS[..2], None, &APP_REFS[..2]);
    assert_eq!(
        check(&p, "eventsub.delete", &del_no_id),
        Err(TemplateError::QueryMismatch)
    );
    // list: GET, no query, no body.
    let list = tpl("GET", url, &APP_HDRS[..2], None, &APP_REFS[..2]);
    assert_eq!(check(&p, "eventsub.list", &list), Ok(()));
    let list_post = tpl("POST", url, &APP_HDRS[..2], None, &APP_REFS[..2]);
    assert_eq!(
        check(&p, "eventsub.list", &list_post),
        Err(TemplateError::BadMethod)
    );
}

const DC_REFS: [(&str, &str); 1] = [("Authorization", "discord-bot")];
const DC_JSON: [(&str, &str); 1] = [("Content-Type", "application/json")];

#[test]
fn discord_moderation_guild_and_user_are_always_host_duty_slots() {
    let p = PlatformPolicy::discord();
    let guild = "https://discord.com/api/v10/guilds/{target.guild_id}";
    let cases: [(&str, &str, String, Option<serde_json::Value>); 4] = [
        (
            "mod.kick",
            "DELETE",
            format!("{guild}/members/{{target.user}}"),
            None,
        ),
        (
            "mod.ban",
            "PUT",
            format!("{guild}/bans/{{target.user}}"),
            Some(serde_json::json!({"delete_message_seconds": 0})),
        ),
        (
            "mod.unban",
            "DELETE",
            format!("{guild}/bans/{{target.user}}"),
            None,
        ),
        (
            "mod.timeout",
            "PATCH",
            format!("{guild}/members/{{target.user}}"),
            Some(serde_json::json!({"communication_disabled_until": "2026-12-01T00:00:00Z"})),
        ),
    ];
    for (kind, method, url, body) in cases {
        let headers: &[(&str, &str)] = if body.is_some() { &DC_JSON } else { &[] };
        let good = tpl(method, &url, headers, body.clone(), &DC_REFS);
        assert_eq!(check(&p, kind, &good), Ok(()), "{kind}");
        // A literal guild snowflake (bundle-chosen guild) is refused.
        let lit = url.replace("{target.guild_id}", "123456789012345678");
        let bad = tpl(method, &lit, headers, body.clone(), &DC_REFS);
        assert_eq!(
            check(&p, kind, &bad),
            Err(TemplateError::PathMismatch),
            "{kind}"
        );
        // A literal target user is refused too (tokenized duty slot only).
        let lit = url.replace("{target.user}", "987654321");
        let bad = tpl(method, &lit, headers, body.clone(), &DC_REFS);
        assert_eq!(
            check(&p, kind, &bad),
            Err(TemplateError::PathMismatch),
            "{kind}"
        );
        // No bot credential => the template is incomplete: refused.
        let bad = tpl(method, &url, headers, body.clone(), &[]);
        assert!(
            check(&p, kind, &bad).is_err(),
            "{kind}: missing credential accepted"
        );
        // Every guild-scoped moderation kind needs the host duty (fail-closed without one).
        assert!(p.kind(kind).unwrap().needs_duty, "{kind}");
    }
}

#[test]
fn discord_timeout_and_ban_bodies_cannot_carry_other_member_fields() {
    let p = PlatformPolicy::discord();
    let url = "https://discord.com/api/v10/guilds/{target.guild_id}/members/{target.user}";
    for body in [
        serde_json::json!({"roles": ["1"]}),
        serde_json::json!({"communication_disabled_until": null, "nick": "x"}),
        serde_json::json!({"mute": true}),
    ] {
        let bad = tpl("PATCH", url, &DC_JSON, Some(body), &DC_REFS);
        assert_eq!(
            check(&p, "mod.timeout", &bad),
            Err(TemplateError::BodyKeyNotAllowed)
        );
    }
    let ban = "https://discord.com/api/v10/guilds/{target.guild_id}/bans/{target.user}";
    let bad = tpl(
        "PUT",
        ban,
        &DC_JSON,
        Some(serde_json::json!({"delete_message_seconds": 0, "foo": 1})),
        &DC_REFS,
    );
    assert_eq!(
        check(&p, "mod.ban", &bad),
        Err(TemplateError::BodyKeyNotAllowed)
    );
}

#[test]
fn discord_message_ops_pin_the_stamp_channel() {
    let p = PlatformPolicy::discord();
    let edit = "https://discord.com/api/v10/channels/{target.channel_id}/messages/123456789";
    let good = tpl(
        "PATCH",
        edit,
        &DC_JSON,
        Some(serde_json::json!({"content": "x", "embeds": []})),
        &DC_REFS,
    );
    assert_eq!(check(&p, "message.edit", &good), Ok(()));
    let bad_key = tpl(
        "PATCH",
        edit,
        &DC_JSON,
        Some(serde_json::json!({"content": "x", "attachments": []})),
        &DC_REFS,
    );
    assert_eq!(
        check(&p, "message.edit", &bad_key),
        Err(TemplateError::BodyKeyNotAllowed)
    );
    let lit_channel = tpl(
        "PATCH",
        "https://discord.com/api/v10/channels/42/messages/123456789",
        &DC_JSON,
        Some(serde_json::json!({"content": "x"})),
        &DC_REFS,
    );
    assert_eq!(
        check(&p, "message.edit", &lit_channel),
        Err(TemplateError::PathMismatch)
    );
    assert!(p.kind("message.edit").unwrap().requires_pin);

    let react = "https://discord.com/api/v10/channels/{target.channel_id}/messages/123456789/reactions/%F0%9F%91%8D/@me";
    let good = tpl("PUT", react, &[], None, &DC_REFS);
    assert_eq!(check(&p, "reaction.add", &good), Ok(()));
    for bad_url in [
        "https://discord.com/api/v10/channels/{target.channel_id}/messages/1/reactions/%ZZ/@me",
        "https://discord.com/api/v10/channels/{target.channel_id}/messages/1/reactions/a%2/@me",
        "https://discord.com/api/v10/channels/{target.channel_id}/messages/1/reactions/%F0%9F%91%8D/@you",
        "https://discord.com/api/v10/channels/{target.channel_id}/messages/1/reactions/%F0%9F%91%8D",
    ] {
        let bad = tpl("PUT", bad_url, &[], None, &DC_REFS);
        assert!(check(&p, "reaction.add", &bad).is_err(), "{bad_url}");
    }
}

#[test]
fn discord_command_registration_is_a_bulk_array_on_the_app_scoped_slot() {
    let p = PlatformPolicy::discord();
    let url = "https://discord.com/api/v10/applications/{app.application_id}/commands";
    let refs_app = [
        ("Authorization", "discord-bot"),
        ("{app.application_id}", "app.application_id"),
    ];
    let body = serde_json::json!([{"name": "ping", "description": "pong", "type": 1}]);
    let good = tpl("PUT", url, &DC_JSON, Some(body.clone()), &refs_app);
    assert_eq!(check(&p, "commands.register", &good), Ok(()));
    // An object body is refused: bulk overwrite takes an array of command objects.
    let obj = tpl(
        "PUT",
        url,
        &DC_JSON,
        Some(serde_json::json!({"name": "x"})),
        &refs_app,
    );
    assert_eq!(
        check(&p, "commands.register", &obj),
        Err(TemplateError::BodyNotJson)
    );
    // An array of non-objects is refused.
    let nonobj = tpl(
        "PUT",
        url,
        &DC_JSON,
        Some(serde_json::json!(["x"])),
        &refs_app,
    );
    assert_eq!(
        check(&p, "commands.register", &nonobj),
        Err(TemplateError::BodyNotJson)
    );
    // A literal application id is refused.
    let lit = tpl(
        "PUT",
        "https://discord.com/api/v10/applications/1234/commands",
        &DC_JSON,
        Some(body.clone()),
        &DC_REFS,
    );
    assert_eq!(
        check(&p, "commands.register", &lit),
        Err(TemplateError::PathMismatch)
    );
    // The guild variant takes the host-bound guild duty slot.
    let g = "https://discord.com/api/v10/applications/{app.application_id}/guilds/{target.guild_id}/commands";
    let good = tpl("PUT", g, &DC_JSON, Some(body), &refs_app);
    assert_eq!(check(&p, "commands.register.guild", &good), Ok(()));
    assert!(p.kind("commands.register.guild").unwrap().needs_duty);
    assert!(p.kind("commands.register").unwrap().is_app_scoped(&p));
}

#[test]
fn discord_interaction_replies_use_host_slots_and_never_an_authorization_header() {
    let p = PlatformPolicy::discord();
    let cb =
        "https://discord.com/api/v10/interactions/{interaction.id}/{interaction.token}/callback";
    let body = serde_json::json!({"type": 4, "data": {"content": "hi"}});
    let good = tpl("POST", cb, &DC_JSON, Some(body.clone()), &[]);
    assert_eq!(check(&p, "interaction.callback", &good), Ok(()));
    // The token is a host (duty) slot: a literal token in the path is refused.
    let lit = tpl(
        "POST",
        "https://discord.com/api/v10/interactions/{interaction.id}/abcDEF123/callback",
        &DC_JSON,
        Some(body.clone()),
        &[],
    );
    assert_eq!(
        check(&p, "interaction.callback", &lit),
        Err(TemplateError::PathMismatch)
    );
    // The URL carries the credential; an Authorization header from the template is refused.
    let with_auth = tpl(
        "POST",
        cb,
        &[
            ("Authorization", "Bot x"),
            ("Content-Type", "application/json"),
        ],
        Some(body.clone()),
        &[],
    );
    assert!(check(&p, "interaction.callback", &with_auth).is_err());
    let bad_key = tpl(
        "POST",
        cb,
        &DC_JSON,
        Some(serde_json::json!({"type": 4, "tts_all": true})),
        &[],
    );
    assert_eq!(
        check(&p, "interaction.callback", &bad_key),
        Err(TemplateError::BodyKeyNotAllowed)
    );
    let fu =
        "https://discord.com/api/v10/webhooks/{interaction.application_id}/{interaction.token}";
    let good = tpl(
        "POST",
        fu,
        &DC_JSON,
        Some(serde_json::json!({"content": "later", "flags": 64})),
        &[],
    );
    assert_eq!(check(&p, "interaction.followup", &good), Ok(()));
    for k in ["interaction.callback", "interaction.followup"] {
        assert!(p.kind(k).unwrap().needs_duty, "{k}");
        assert!(p.kind(k).unwrap().header_creds.is_empty(), "{k}");
    }
}

const YT_HDRS: [(&str, &str); 2] = [
    ("Authorization", "Bearer {{YT_OAUTH_TOKEN}}"),
    ("Content-Type", "application/json"),
];
const YT_REFS: [(&str, &str); 2] = [
    ("{{YT_OAUTH_TOKEN}}", "youtube.oauth"),
    ("{{YT_LIVE_CHAT_ID}}", "youtube.live_chat_id"),
];

#[test]
fn youtube_moderation_inserts_take_only_a_snippet_with_the_host_chat_id() {
    let p = PlatformPolicy::youtube();
    for (kind, resource, snippet) in [
        (
            "mod.ban",
            "bans",
            serde_json::json!({"liveChatId": "{{YT_LIVE_CHAT_ID}}", "type": "permanent",
                "bannedUserDetails": {"channelId": "UCabc"}}),
        ),
        (
            "mod.add",
            "moderators",
            serde_json::json!({"liveChatId": "{{YT_LIVE_CHAT_ID}}",
                "moderatorDetails": {"channelId": "UCabc"}}),
        ),
    ] {
        let url = format!("https://www.googleapis.com/youtube/v3/liveChat/{resource}?part=snippet");
        let good = tpl(
            "POST",
            &url,
            &YT_HDRS,
            Some(serde_json::json!({ "snippet": snippet })),
            &YT_REFS,
        );
        assert_eq!(check(&p, kind, &good), Ok(()), "{kind}");
        let bad = tpl(
            "POST",
            &url,
            &YT_HDRS,
            Some(serde_json::json!({ "snippet": snippet, "extra": 1 })),
            &YT_REFS,
        );
        assert_eq!(
            check(&p, kind, &bad),
            Err(TemplateError::BodyKeyNotAllowed),
            "{kind}"
        );
        // A literal live chat id is a body string that is not a declared slot position
        // match for the host id; the slot must be used for the chat id.
        assert!(p.kind(kind).unwrap().requires_pin, "{kind}");
    }
}

#[test]
fn youtube_deletes_are_bodyless_with_an_exact_id_query() {
    let p = PlatformPolicy::youtube();
    for (kind, resource) in [
        ("chat.delete", "messages"),
        ("mod.unban", "bans"),
        ("mod.remove", "moderators"),
    ] {
        let url = format!("https://www.googleapis.com/youtube/v3/liveChat/{resource}?id=abc.DEF-1");
        let good = tpl("DELETE", &url, &YT_HDRS[..1], None, &YT_REFS[..1]);
        assert_eq!(check(&p, kind, &good), Ok(()), "{kind}");
        let no_id = tpl(
            "DELETE",
            &format!("https://www.googleapis.com/youtube/v3/liveChat/{resource}"),
            &YT_HDRS[..1],
            None,
            &YT_REFS[..1],
        );
        assert_eq!(
            check(&p, kind, &no_id),
            Err(TemplateError::QueryMismatch),
            "{kind}"
        );
        let body = tpl(
            "DELETE",
            &url,
            &YT_HDRS,
            Some(serde_json::json!({"a": 1})),
            &YT_REFS[..1],
        );
        assert_eq!(
            check(&p, kind, &body),
            Err(TemplateError::BodyPresence),
            "{kind}"
        );
        let inject = tpl(
            "DELETE",
            &format!("https://www.googleapis.com/youtube/v3/liveChat/{resource}?id=a%26b"),
            &YT_HDRS[..1],
            None,
            &YT_REFS[..1],
        );
        assert!(
            check(&p, kind, &inject).is_err(),
            "{kind}: encoded separator accepted"
        );
    }
}

const KK_HDRS: [(&str, &str); 2] = [
    ("Authorization", "Bearer {{KICK_TOKEN}}"),
    ("Content-Type", "application/json"),
];
const KK_TOKEN_REF: [(&str, &str); 1] = [("{{KICK_TOKEN}}", "token")];
const KK_MOD_REFS: [(&str, &str); 2] = [
    ("{{KICK_TOKEN}}", "token"),
    ("{{KICK_BROADCASTER_ID}}", "broadcaster_id"),
];
const KK_BANS: &str = "https://api.kick.com/public/v1/moderation/bans";

#[test]
fn kick_registers_chat_and_moderation_ops_and_refuses_everything_else() {
    let p = PlatformPolicy::kick();
    assert_eq!(p.platform, "kick");
    assert_eq!(p.hosts, ["api.kick.com"]);
    for op in ["chat.send", "mod.ban", "mod.timeout", "mod.unban"] {
        assert_eq!(registered_op("kick", op), Some(op), "{op}");
    }
    // chat.delete: a bundle-supplied global message id is not bound to the community channel.
    for op in [
        "chat.delete",
        "dm.send",
        "mod.kick",
        "role.add",
        "eventsub.create",
        "",
    ] {
        assert!(registered_op("kick", op).is_none(), "{op}");
    }
}

#[test]
fn kick_moderation_broadcaster_must_be_the_host_slot_and_the_target_a_literal_integer() {
    let p = PlatformPolicy::kick();
    for (op, method) in [
        ("mod.ban", "POST"),
        ("mod.timeout", "POST"),
        ("mod.unban", "DELETE"),
    ] {
        let good = tpl(
            method,
            KK_BANS,
            &KK_HDRS,
            Some(
                serde_json::json!({"broadcaster_user_id": "{{KICK_BROADCASTER_ID}}", "user_id": 42}),
            ),
            &KK_MOD_REFS,
        );
        assert_eq!(check(&p, op, &good), Ok(()), "{op}");
        // A literal broadcaster id (aiming the token at another channel) is refused.
        for bc in [serde_json::json!(999), serde_json::json!("999")] {
            let aimed = tpl(
                method,
                KK_BANS,
                &KK_HDRS,
                Some(serde_json::json!({"broadcaster_user_id": bc, "user_id": 42})),
                &KK_TOKEN_REF,
            );
            assert!(check(&p, op, &aimed).is_err(), "{op}: {bc}");
        }
        // Keys outside the documented set are refused.
        let extra = tpl(
            method,
            KK_BANS,
            &KK_HDRS,
            Some(
                serde_json::json!({"broadcaster_user_id": "{{KICK_BROADCASTER_ID}}", "user_id": 42, "moderator_id": 1}),
            ),
            &KK_MOD_REFS,
        );
        assert_eq!(
            check(&p, op, &extra),
            Err(TemplateError::BodyKeyNotAllowed),
            "{op}"
        );
    }
}

/// Real path: verify -> render (Int slot becomes a JSON integer) -> protected-target check.
#[test]
fn kick_moderation_renders_integers_and_refuses_the_broadcaster_as_target() {
    use crate::connector::sender_exec::subst::{check_distinct_body_ints, render, SlotValue};
    use std::collections::BTreeMap;
    let p = PlatformPolicy::kick();
    let kind = p.kind("mod.ban").unwrap();
    let render_for = |target: serde_json::Value| {
        let t = tpl(
            "POST",
            KK_BANS,
            &KK_HDRS,
            Some(
                serde_json::json!({"broadcaster_user_id": "{{KICK_BROADCASTER_ID}}", "user_id": target, "duration": 10}),
            ),
            &KK_MOD_REFS,
        );
        let v = verify(&p, kind, &t).unwrap();
        let mut values = BTreeMap::new();
        values.insert(
            p.slot_index("KICK_BROADCASTER_ID").unwrap(),
            SlotValue::new("777".to_string()),
        );
        values.insert(
            p.slot_index("KICK_TOKEN").unwrap(),
            SlotValue::new("tok".to_string()),
        );
        render(&p, &v, &values, &BTreeMap::new())
    };
    let ok = render_for(serde_json::json!(42)).unwrap();
    let body: serde_json::Value = serde_json::from_slice(ok.body.as_ref().unwrap()).unwrap();
    assert_eq!(body["broadcaster_user_id"], 777);
    assert!(body["broadcaster_user_id"].is_u64());
    assert_eq!(
        check_distinct_body_ints(&kind.distinct_body_ints, ok.body.as_deref()),
        Ok(())
    );
    for bad in [serde_json::json!(777), serde_json::json!("42")] {
        let r = render_for(bad.clone()).unwrap();
        assert!(
            check_distinct_body_ints(&kind.distinct_body_ints, r.body.as_deref()).is_err(),
            "{bad}"
        );
    }
}

#[test]
fn registered_op_is_the_single_table_lookup() {
    assert_eq!(registered_op("twitch", "mod.ban"), Some("mod.ban"));
    assert_eq!(registered_op("discord", "mod.ban"), Some("mod.ban"));
    assert_eq!(registered_op("youtube", "mod.add"), Some("mod.add"));
    for (p, o) in [
        ("twitch", "mod.kick"),
        ("discord", "eventsub.list"),
        ("youtube", "eventsub.create"),
        ("slack", "chat.send"),
        ("twitch", ""),
        ("twitch", "MOD.BAN"),
        ("twitch", "mod.ban "),
    ] {
        assert_eq!(registered_op(p, o), None, "{p}/{o}");
    }
    let all = all_registered_ops();
    assert!(all.contains(&"commands.register.guild") && all.contains(&"interaction.followup"));
}

#[test]
fn credential_scope_is_never_mixed_and_guilds_are_never_resolver_or_stamp_slots() {
    for p in PlatformPolicy::production_set() {
        for k in &p.kinds {
            let kind_slots: Vec<&SlotDecl> = k
                .slots
                .iter()
                .map(|n| &p.slots[p.slot_index(n).unwrap_or_else(|| panic!("{}/{n}", k.kind))])
                .collect();
            let app = kind_slots.iter().filter(|s| s.is_app_scoped()).count();
            let user = kind_slots
                .iter()
                .filter(|s| s.origin == SlotOrigin::Resolver && !s.is_app_scoped())
                .count();
            assert!(
                app == 0 || user == 0,
                "{}/{} mixes app and user credential slots",
                p.platform,
                k.kind
            );
            for s in &kind_slots {
                if s.name == "target.guild_id" || s.name == "interaction.token" {
                    assert_eq!(s.origin, SlotOrigin::Duty, "{}/{}", p.platform, k.kind);
                }
            }
            // A guild-scoped kind can only run with a registered host duty.
            if k.slots.contains(&"target.guild_id") {
                assert!(k.needs_duty, "{}/{}", p.platform, k.kind);
            }
        }
    }
}

#[test]
fn the_policy_table_matches_the_relay_op_parity_fixture() {
    let fixture: serde_json::Value = serde_json::from_str(include_str!(
        "../../../../../bundle_capability_gate/tests/fixtures/platform_relay_ops.json"
    ))
    .unwrap();
    // Ops with their own typed producer paths (not `relay.send`-reachable by name).
    let typed = [
        "role.add",
        "role.remove",
        "event.create",
        "event.update",
        "event.cancel",
        "event.delete",
        // Reconciler-only: a bulk overwrite of the guild's shared command set is never
        // bundle-callable (`relay.send`); the host builds + verifies the payload itself.
        "commands.register",
        "commands.register.guild",
    ];
    let mut checked = 0;
    for p in PlatformPolicy::production_set() {
        let want: Vec<String> = fixture[p.platform]
            .as_array()
            .unwrap_or_else(|| panic!("fixture lacks {}", p.platform))
            .iter()
            .map(|o| o.as_str().unwrap().to_string())
            .collect();
        let mut have: Vec<String> = p
            .kinds
            .iter()
            .filter(|k| !typed.contains(&k.kind))
            .map(|k| k.kind.to_string())
            .collect();
        let mut want_sorted = want;
        want_sorted.sort();
        have.sort();
        assert_eq!(have, want_sorted, "{}", p.platform);
        checked += have.len();
    }
    assert_eq!(
        checked,
        10 + 13 + 6 + 4,
        "denominator: ops compared with the fixture"
    );
}
