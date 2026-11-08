//! Twitch Helix template policy: chat, moderation, whispers and EventSub subscription
//! management. Every endpoint below is verified against the official Helix reference
//! (<https://dev.twitch.tv/docs/api/reference/>); broadcaster / moderator / sender ids are
//! ALWAYS host-resolved slots (the community's connection record), never bundle input.

use super::{
    resolver_slot, KindPolicy, PathSeg, PlatformPolicy, Position, ResultMode, SlotShape, SlotSyntax,
};

/// Slots a user-token (community connection) Helix kind may use.
const USER_SLOTS: [&str; 4] = [
    "TWITCH_TOKEN",
    "TWITCH_CLIENT_ID",
    "TWITCH_BROADCASTER_ID",
    "TWITCH_SENDER_ID",
];

/// Slots an APP-token (client-credentials) EventSub kind may use.
const APP_SLOTS: [&str; 5] = [
    "TWITCH_APP_TOKEN",
    "TWITCH_APP_CLIENT_ID",
    "TWITCH_APP_BROADCASTER_ID",
    "TWITCH_EVENTSUB_CALLBACK",
    "TWITCH_EVENTSUB_SECRET",
];

/// `/helix/<rest...>` as exact literal path segments.
fn helix(rest: &[&'static str]) -> Vec<PathSeg> {
    let mut p = vec![PathSeg::Lit("helix")];
    p.extend(rest.iter().map(|s| PathSeg::Lit(s)));
    p
}

/// A moderator-scoped Helix kind: `broadcaster_id` + `moderator_id` are the host's resolved
/// ids, the pinned channel must be proven, and the user token carries the moderator scope.
fn moderation_kind(
    kind: &'static str,
    method: &'static str,
    query: Vec<&'static str>,
    max_body: Option<usize>,
) -> KindPolicy {
    KindPolicy {
        query,
        slots: USER_SLOTS.to_vec(),
        max_body,
        requires_pin: true,
        ..KindPolicy::new(kind, method, helix(&["moderation", "bans"]))
    }
}

/// An EventSub kind: APP token only (EventSub webhook subscriptions are created with an app
/// access token), no pinned channel (the broadcaster comes from the host binding).
fn eventsub_kind(
    kind: &'static str,
    method: &'static str,
    query: Vec<&'static str>,
    max_body: Option<usize>,
    result: ResultMode,
) -> KindPolicy {
    KindPolicy {
        query,
        slots: APP_SLOTS.to_vec(),
        max_body,
        result,
        body_keys: if max_body.is_some() {
            vec!["type", "version", "condition", "transport"]
        } else {
            vec![]
        },
        ..KindPolicy::new(kind, method, helix(&["eventsub", "subscriptions"]))
    }
}

impl PlatformPolicy {
    /// Twitch Helix policy (connector README "Slot set for T6d").
    #[must_use]
    pub fn twitch() -> Self {
        Self {
            platform: "twitch",
            syntax: SlotSyntax::Double,
            hosts: vec!["api.twitch.tv".to_string()],
            slots: vec![
                resolver_slot(
                    "TWITCH_TOKEN",
                    "token",
                    SlotShape::Secret,
                    vec![Position::Header("authorization")],
                ),
                resolver_slot(
                    "TWITCH_CLIENT_ID",
                    "client_id",
                    SlotShape::Id,
                    vec![Position::Header("client-id")],
                ),
                resolver_slot(
                    "TWITCH_BROADCASTER_ID",
                    "broadcaster_id",
                    SlotShape::Id,
                    vec![
                        Position::Body("/broadcaster_id"),
                        Position::Query("broadcaster_id"),
                    ],
                ),
                resolver_slot(
                    "TWITCH_SENDER_ID",
                    "sender_id",
                    SlotShape::Id,
                    vec![
                        Position::Body("/sender_id"),
                        Position::Query("moderator_id"),
                    ],
                ),
                // App-scoped credential slots (client-credentials token). The "app."
                // handle prefix routes them through the app-credential seam.
                resolver_slot(
                    "TWITCH_APP_TOKEN",
                    "app.token",
                    SlotShape::Secret,
                    vec![Position::Header("authorization")],
                ),
                resolver_slot(
                    "TWITCH_APP_CLIENT_ID",
                    "app.client_id",
                    SlotShape::Id,
                    vec![Position::Header("client-id")],
                ),
                resolver_slot(
                    "TWITCH_APP_BROADCASTER_ID",
                    "app.broadcaster_id",
                    SlotShape::Id,
                    vec![
                        Position::Body("/condition/broadcaster_user_id"),
                        Position::Body("/condition/moderator_user_id"),
                    ],
                ),
                resolver_slot(
                    "TWITCH_EVENTSUB_CALLBACK",
                    "app.eventsub_callback",
                    SlotShape::Secret,
                    vec![Position::Body("/transport/callback")],
                ),
                resolver_slot(
                    "TWITCH_EVENTSUB_SECRET",
                    "app.eventsub_secret",
                    SlotShape::Secret,
                    vec![Position::Body("/transport/secret")],
                ),
            ],
            kinds: vec![
                KindPolicy {
                    slots: USER_SLOTS.to_vec(),
                    max_body: Some(4096),
                    ..KindPolicy::new("chat.send", "POST", helix(&["chat", "messages"]))
                },
                // https://dev.twitch.tv/docs/api/reference/#delete-chat-messages
                KindPolicy {
                    query: vec!["broadcaster_id", "moderator_id", "message_id"],
                    slots: USER_SLOTS.to_vec(),
                    ..KindPolicy::new("chat.delete", "DELETE", helix(&["moderation", "chat"]))
                },
                // https://dev.twitch.tv/docs/api/reference/#delete-chat-messages -- the same
                // endpoint WITHOUT `message_id` clears the whole chat (moderator scope
                // `moderator:manage:chat_messages`); its own kind keeps a distinct audit identity.
                KindPolicy {
                    query: vec!["broadcaster_id", "moderator_id"],
                    slots: USER_SLOTS.to_vec(),
                    requires_pin: true,
                    ..KindPolicy::new("chat.clear", "DELETE", helix(&["moderation", "chat"]))
                },
                // https://dev.twitch.tv/docs/api/reference/#send-whisper
                KindPolicy {
                    query: vec!["from_user_id", "to_user_id"],
                    slots: vec!["TWITCH_TOKEN", "TWITCH_CLIENT_ID", "TWITCH_SENDER_ID"],
                    max_body: Some(4096),
                    needs_duty: true,
                    ..KindPolicy::new("dm.send", "POST", helix(&["whispers"]))
                },
                // https://dev.twitch.tv/docs/api/reference/#ban-user (a `duration` in the
                // body makes it a timeout; the two ops are separate kinds so each carries
                // its own permission/audit identity).
                moderation_kind(
                    "mod.ban",
                    "POST",
                    vec!["broadcaster_id", "moderator_id"],
                    Some(1024),
                ),
                moderation_kind(
                    "mod.timeout",
                    "POST",
                    vec!["broadcaster_id", "moderator_id"],
                    Some(1024),
                ),
                // https://dev.twitch.tv/docs/api/reference/#unban-user
                moderation_kind(
                    "mod.unban",
                    "DELETE",
                    vec!["broadcaster_id", "moderator_id", "user_id"],
                    None,
                ),
                // https://dev.twitch.tv/docs/api/reference/#create-eventsub-subscription
                eventsub_kind(
                    "eventsub.create",
                    "POST",
                    vec![],
                    Some(4096),
                    ResultMode::Body,
                ),
                // https://dev.twitch.tv/docs/api/reference/#delete-eventsub-subscription
                eventsub_kind(
                    "eventsub.delete",
                    "DELETE",
                    vec!["id"],
                    None,
                    ResultMode::None,
                ),
                // https://dev.twitch.tv/docs/api/reference/#get-eventsub-subscriptions
                // (first page; the reply body is returned to the producer).
                eventsub_kind("eventsub.list", "GET", vec![], None, ResultMode::Body),
            ],
            auth_prefixes: vec!["Bearer "],
            allowed_headers: vec![
                "authorization",
                "client-id",
                "content-type",
                "accept",
                "user-agent",
            ],
            allow_http: false,
        }
    }
}

#[cfg(test)]
mod chat_clear_tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use crate::connector::sender_exec::verify::{verify, TemplateError};
    use crate::connector::sender_exec::RequestTpl;

    type Pairs = Vec<(&'static str, &'static str)>;

    fn tpl(method: &str, url: &str, with_creds: bool, body: Option<&str>) -> RequestTpl {
        let (headers, refs): (Pairs, Pairs) = if with_creds {
            (
                vec![
                    ("Authorization", "Bearer {{TWITCH_TOKEN}}"),
                    ("Client-Id", "{{TWITCH_CLIENT_ID}}"),
                ],
                vec![
                    ("{{TWITCH_TOKEN}}", "token"),
                    ("{{TWITCH_CLIENT_ID}}", "client_id"),
                    ("{{TWITCH_BROADCASTER_ID}}", "broadcaster_id"),
                    ("{{TWITCH_SENDER_ID}}", "sender_id"),
                ],
            )
        } else {
            (vec![], vec![])
        };
        let owned = |p: Vec<(&str, &str)>| {
            p.into_iter()
                .map(|(a, b)| (a.to_string(), b.to_string()))
                .collect::<Vec<_>>()
        };
        RequestTpl {
            method: method.into(),
            url: url.into(),
            headers: owned(headers),
            body: body.map(|b| b.as_bytes().to_vec()),
            secret_refs: owned(refs),
        }
    }

    const MOD: &str = "broadcaster_id={{TWITCH_BROADCASTER_ID}}&moderator_id={{TWITCH_SENDER_ID}}";

    fn check(t: &RequestTpl) -> Result<(), TemplateError> {
        let p = PlatformPolicy::twitch();
        verify(&p, p.kind("chat.clear").expect("kind exists"), t).map(|_| ())
    }

    #[test]
    fn chat_clear_is_the_bodyless_delete_without_a_message_id_on_host_ids() {
        let url = format!("https://api.twitch.tv/helix/moderation/chat?{MOD}");
        assert_eq!(check(&tpl("DELETE", &url, true, None)), Ok(()));
        assert!(PlatformPolicy::twitch().kind("chat.clear").unwrap().requires_pin);
        // The delete-one-message query key is NOT part of the clear kind.
        let with_msg = format!("{url}&message_id=abc");
        assert_eq!(
            check(&tpl("DELETE", &with_msg, true, None)),
            Err(TemplateError::QueryMismatch)
        );
        // A literal broadcaster id (bundle-chosen) is refused: it must be the host slot.
        let lit = "https://api.twitch.tv/helix/moderation/chat?broadcaster_id=999&moderator_id={{TWITCH_SENDER_ID}}";
        assert_eq!(
            check(&tpl("DELETE", lit, true, None)),
            Err(TemplateError::SlotMisplaced)
        );
        // Wrong method / wrong path / a body are refused.
        assert_eq!(check(&tpl("POST", &url, true, None)), Err(TemplateError::BadMethod));
        let wrong = format!("https://api.twitch.tv/helix/moderation/bans?{MOD}");
        assert_eq!(
            check(&tpl("DELETE", &wrong, true, None)),
            Err(TemplateError::PathMismatch)
        );
        assert_eq!(
            check(&tpl("DELETE", &url, true, Some("{}"))),
            Err(TemplateError::BodyPresence)
        );
    }
}
