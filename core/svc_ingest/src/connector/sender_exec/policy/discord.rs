//! Discord REST (v10) template policy: chat, DM, managed roles, scheduled events,
//! moderation, message ops, application-command registration and interaction replies.
//! Endpoints are verified against <https://discord.com/developers/docs/>; the GUILD of every
//! guild-scoped op is the host-bound `target.guild_id` duty slot (the community's binding),
//! never a bundle-supplied value, and the target user is the tokenized `target.user` duty slot.

use super::{
    duty_slot, resolver_slot, HeaderCred, KindPolicy, PathSeg, PlatformPolicy, Position,
    ResultMode, SlotDecl, SlotOrigin, SlotShape, SlotSyntax,
};

/// The per-community bot credential (an app-level token by nature).
const BOT: HeaderCred = HeaderCred {
    header: "authorization",
    handle: "discord-bot",
    prefix: "Bot ",
};

/// `/api/v10/<rest...>` with each element as given.
fn v10(rest: Vec<PathSeg>) -> Vec<PathSeg> {
    let mut p = vec![PathSeg::Lit("api"), PathSeg::Lit("v10")];
    p.extend(rest);
    p
}

/// `/api/v10/guilds/{target.guild_id}/<rest...>`.
fn guild(rest: Vec<PathSeg>) -> Vec<PathSeg> {
    let mut p = vec![PathSeg::Lit("guilds"), PathSeg::Slot("target.guild_id")];
    p.extend(rest);
    v10(p)
}

/// `/api/v10/channels/{target.channel_id}/messages[/<tail...>]`.
fn channel_messages(tail: Vec<PathSeg>) -> Vec<PathSeg> {
    let mut p = vec![
        PathSeg::Lit("channels"),
        PathSeg::Slot("target.channel_id"),
        PathSeg::Lit("messages"),
    ];
    p.extend(tail);
    v10(p)
}

/// Discord `PUT|DELETE /guilds/{guild}/members/{user}/roles/{role}`: every
/// path slot is host-filled and the request has no body.
fn role_kind(kind: &'static str, method: &'static str) -> KindPolicy {
    KindPolicy {
        slots: vec!["target.guild_id", "target.user", "target.role"],
        header_creds: vec![BOT],
        text_key: "content",
        needs_duty: true,
        ..KindPolicy::new(
            kind,
            method,
            guild(vec![
                PathSeg::Lit("members"),
                PathSeg::Slot("target.user"),
                PathSeg::Lit("roles"),
                PathSeg::Slot("target.role"),
            ]),
        )
    }
}

/// Discord `POST|PATCH|DELETE /guilds/{guild}/scheduled-events[/{id}]`: the
/// guild is the host-filled binding slot; the optional event id is a validated
/// snowflake path segment. The credential is the same per-community bot handle.
fn event_kind(
    kind: &'static str,
    method: &'static str,
    with_id: bool,
    max_body: Option<usize>,
) -> KindPolicy {
    let mut tail = vec![PathSeg::Lit("scheduled-events")];
    if with_id {
        tail.push(PathSeg::Id);
    }
    KindPolicy {
        slots: vec!["target.guild_id"],
        header_creds: vec![BOT],
        max_body,
        text_key: "content",
        needs_duty: true,
        result: if kind == "event.create" {
            ResultMode::CreatedSnowflake
        } else {
            ResultMode::None
        },
        ..KindPolicy::new(kind, method, guild(tail))
    }
}

/// A guild moderation kind on `/guilds/{guild}/<tail>`: guild + target user are host duty
/// slots, the bot credential is a header credential, the body (when any) is restricted to the
/// exact top-level keys the endpoint legitimately takes.
fn moderation_kind(
    kind: &'static str,
    method: &'static str,
    tail: Vec<PathSeg>,
    body: Option<(usize, Vec<&'static str>)>,
) -> KindPolicy {
    let (max_body, body_keys) = match body {
        Some((max, keys)) => (Some(max), keys),
        None => (None, vec![]),
    };
    KindPolicy {
        slots: vec!["target.guild_id", "target.user"],
        header_creds: vec![BOT],
        max_body,
        body_keys,
        text_key: "content",
        needs_duty: true,
        ..KindPolicy::new(kind, method, guild(tail))
    }
}

/// An interaction reply kind: authenticated by the interaction token in the URL (NO
/// `Authorization` header). Both the interaction identifiers and the token come from the
/// host-held interaction store (a duty slot), never from the bundle.
fn interaction_kind(
    kind: &'static str,
    path: Vec<PathSeg>,
    slots: Vec<&'static str>,
    keys: Vec<&'static str>,
) -> KindPolicy {
    KindPolicy {
        slots,
        max_body: Some(16_384),
        body_keys: keys,
        text_key: "content",
        needs_duty: true,
        ..KindPolicy::new(kind, "POST", v10(path))
    }
}

impl PlatformPolicy {
    /// Discord REST policy (connector README "Outbound (T6d)"). Only
    /// `{target.channel_id}` is a stamp slot; `dm.send`, `role.*`, `event.*` and the
    /// guild-scoped moderation / interaction kinds are host duties (`discord_duties`): their
    /// placeholders are normalized by the hook, then filled by it (the guild comes only from
    /// the community's binding).
    #[must_use]
    pub fn discord() -> Self {
        Self {
            platform: "discord",
            syntax: SlotSyntax::Single,
            hosts: vec!["discord.com".to_string()],
            slots: vec![
                SlotDecl {
                    name: "target.channel_id",
                    handle: "",
                    origin: SlotOrigin::Stamp,
                    shape: SlotShape::Id,
                    positions: vec![Position::Path],
                },
                duty_slot("dm.channel_id"),
                duty_slot("target.guild_id"),
                duty_slot("target.user"),
                duty_slot("target.role"),
                duty_slot("interaction.id"),
                duty_slot("interaction.application_id"),
                SlotDecl {
                    name: "interaction.token",
                    handle: "",
                    origin: SlotOrigin::Duty,
                    shape: SlotShape::Secret,
                    positions: vec![Position::Path],
                },
                // App-scoped: the application (client) id the community's bot connection
                // belongs to. Resolved through the app-credential seam.
                resolver_slot(
                    "app.application_id",
                    "app.application_id",
                    SlotShape::Id,
                    vec![Position::Path],
                ),
            ],
            kinds: vec![
                KindPolicy {
                    slots: vec!["target.channel_id"],
                    header_creds: vec![BOT],
                    max_body: Some(16_384),
                    text_key: "content",
                    ..KindPolicy::new("chat.send", "POST", channel_messages(vec![]))
                },
                KindPolicy {
                    slots: vec!["target.channel_id"],
                    header_creds: vec![BOT],
                    text_key: "content",
                    ..KindPolicy::new("chat.delete", "DELETE", channel_messages(vec![PathSeg::Id]))
                },
                KindPolicy {
                    slots: vec!["dm.channel_id"],
                    header_creds: vec![BOT],
                    max_body: Some(16_384),
                    text_key: "content",
                    needs_duty: true,
                    ..KindPolicy::new(
                        "dm.send",
                        "POST",
                        v10(vec![
                            PathSeg::Lit("channels"),
                            PathSeg::Slot("dm.channel_id"),
                            PathSeg::Lit("messages"),
                        ]),
                    )
                },
                role_kind("role.add", "PUT"),
                role_kind("role.remove", "DELETE"),
                event_kind("event.create", "POST", false, Some(4_096)),
                event_kind("event.update", "PATCH", true, Some(4_096)),
                event_kind("event.cancel", "PATCH", true, Some(256)),
                event_kind("event.delete", "DELETE", true, None),
                // https://discord.com/developers/docs/resources/guild#remove-guild-member
                moderation_kind(
                    "mod.kick",
                    "DELETE",
                    vec![PathSeg::Lit("members"), PathSeg::Slot("target.user")],
                    None,
                ),
                // https://discord.com/developers/docs/resources/guild#create-guild-ban
                moderation_kind(
                    "mod.ban",
                    "PUT",
                    vec![PathSeg::Lit("bans"), PathSeg::Slot("target.user")],
                    Some((512, vec!["delete_message_seconds"])),
                ),
                // https://discord.com/developers/docs/resources/guild#remove-guild-ban
                moderation_kind(
                    "mod.unban",
                    "DELETE",
                    vec![PathSeg::Lit("bans"), PathSeg::Slot("target.user")],
                    None,
                ),
                // Timeout = modify-guild-member with ONLY `communication_disabled_until`
                // (the key allowlist stops this kind from editing roles / nick / voice).
                // https://discord.com/developers/docs/resources/guild#modify-guild-member
                moderation_kind(
                    "mod.timeout",
                    "PATCH",
                    vec![PathSeg::Lit("members"), PathSeg::Slot("target.user")],
                    Some((512, vec!["communication_disabled_until"])),
                ),
                // https://discord.com/developers/docs/resources/message#edit-message
                KindPolicy {
                    slots: vec!["target.channel_id"],
                    header_creds: vec![BOT],
                    max_body: Some(16_384),
                    body_keys: vec!["content", "embeds", "allowed_mentions", "flags"],
                    text_key: "content",
                    requires_pin: true,
                    ..KindPolicy::new("message.edit", "PATCH", channel_messages(vec![PathSeg::Id]))
                },
                // https://discord.com/developers/docs/resources/message#create-reaction
                KindPolicy {
                    slots: vec!["target.channel_id"],
                    header_creds: vec![BOT],
                    text_key: "content",
                    requires_pin: true,
                    ..KindPolicy::new(
                        "reaction.add",
                        "PUT",
                        channel_messages(vec![
                            PathSeg::Id,
                            PathSeg::Lit("reactions"),
                            PathSeg::Enc,
                            PathSeg::Lit("@me"),
                        ]),
                    )
                },
                // https://discord.com/developers/docs/resources/message#create-message
                // (embed-bearing send): the pinned origin channel only; the reply's new
                // message snowflake is returned to the producer.
                KindPolicy {
                    slots: vec!["target.channel_id"],
                    header_creds: vec![BOT],
                    max_body: Some(16_384),
                    body_keys: vec!["content", "embeds", "allowed_mentions"],
                    text_key: "content",
                    requires_pin: true,
                    result: ResultMode::CreatedSnowflake,
                    ..KindPolicy::new("message.embed", "POST", channel_messages(vec![]))
                },
                // https://discord.com/developers/docs/resources/webhook#create-webhook
                // (MANAGE_WEBHOOKS): the reply carries the webhook TOKEN, a secret -- the
                // producer (svc_action) reads the body host-side and never returns it to a
                // guest.
                KindPolicy {
                    slots: vec!["target.channel_id"],
                    header_creds: vec![BOT],
                    max_body: Some(1024),
                    body_keys: vec!["name"],
                    text_key: "name",
                    requires_pin: true,
                    result: ResultMode::Body,
                    ..KindPolicy::new(
                        "webhook.create",
                        "POST",
                        v10(vec![
                            PathSeg::Lit("channels"),
                            PathSeg::Slot("target.channel_id"),
                            PathSeg::Lit("webhooks"),
                        ]),
                    )
                },
                // https://discord.com/developers/docs/interactions/application-commands#bulk-overwrite-global-application-commands
                KindPolicy {
                    slots: vec!["app.application_id"],
                    header_creds: vec![BOT],
                    max_body: Some(262_144),
                    array_body: true,
                    text_key: "content",
                    result: ResultMode::Body,
                    ..KindPolicy::new(
                        "commands.register",
                        "PUT",
                        v10(vec![
                            PathSeg::Lit("applications"),
                            PathSeg::Slot("app.application_id"),
                            PathSeg::Lit("commands"),
                        ]),
                    )
                },
                // https://discord.com/developers/docs/interactions/application-commands#bulk-overwrite-guild-application-commands
                KindPolicy {
                    slots: vec!["app.application_id", "target.guild_id"],
                    header_creds: vec![BOT],
                    max_body: Some(262_144),
                    array_body: true,
                    text_key: "content",
                    needs_duty: true,
                    result: ResultMode::Body,
                    ..KindPolicy::new(
                        "commands.register.guild",
                        "PUT",
                        v10(vec![
                            PathSeg::Lit("applications"),
                            PathSeg::Slot("app.application_id"),
                            PathSeg::Lit("guilds"),
                            PathSeg::Slot("target.guild_id"),
                            PathSeg::Lit("commands"),
                        ]),
                    )
                },
                // https://discord.com/developers/docs/interactions/receiving-and-responding#create-interaction-response
                interaction_kind(
                    "interaction.callback",
                    vec![
                        PathSeg::Lit("interactions"),
                        PathSeg::Slot("interaction.id"),
                        PathSeg::Slot("interaction.token"),
                        PathSeg::Lit("callback"),
                    ],
                    vec!["interaction.id", "interaction.token"],
                    vec!["type", "data"],
                ),
                // https://discord.com/developers/docs/interactions/receiving-and-responding#create-followup-message
                interaction_kind(
                    "interaction.followup",
                    vec![
                        PathSeg::Lit("webhooks"),
                        PathSeg::Slot("interaction.application_id"),
                        PathSeg::Slot("interaction.token"),
                    ],
                    vec!["interaction.application_id", "interaction.token"],
                    vec![
                        "content",
                        "embeds",
                        "allowed_mentions",
                        "flags",
                        "components",
                        "tts",
                    ],
                ),
            ],
            auth_prefixes: vec![],
            allowed_headers: vec!["content-type", "accept", "user-agent", "x-audit-log-reason"],
            allow_http: false,
        }
    }
}

#[cfg(test)]
mod embed_webhook_tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use crate::connector::sender_exec::verify::{verify, TemplateError};
    use crate::connector::sender_exec::RequestTpl;

    fn tpl(method: &str, url: &str, body: Option<serde_json::Value>) -> RequestTpl {
        let owned = |p: &[(&str, &str)]| {
            p.iter()
                .map(|(a, b)| ((*a).to_string(), (*b).to_string()))
                .collect::<Vec<_>>()
        };
        RequestTpl {
            method: method.into(),
            url: url.into(),
            headers: owned(&[("Content-Type", "application/json")]),
            body: body.map(|b| b.to_string().into_bytes()),
            secret_refs: owned(&[("Authorization", "discord-bot")]),
        }
    }

    fn check(kind: &str, t: &RequestTpl) -> Result<(), TemplateError> {
        let p = PlatformPolicy::discord();
        verify(&p, p.kind(kind).expect("kind exists"), t).map(|_| ())
    }

    #[test]
    fn message_embed_posts_to_the_pinned_channel_and_returns_the_message_snowflake() {
        let url = "https://discord.com/api/v10/channels/{target.channel_id}/messages";
        let good = tpl(
            "POST",
            url,
            Some(serde_json::json!({"content": "x", "embeds": [{"title": "t"}]})),
        );
        assert_eq!(check("message.embed", &good), Ok(()));
        let k = PlatformPolicy::discord()
            .kind("message.embed")
            .unwrap()
            .clone();
        assert!(k.requires_pin);
        assert_eq!(k.result, ResultMode::CreatedSnowflake);
        let bad_key = tpl(
            "POST",
            url,
            Some(serde_json::json!({"content": "x", "attachments": []})),
        );
        assert_eq!(
            check("message.embed", &bad_key),
            Err(TemplateError::BodyKeyNotAllowed)
        );
        let lit = tpl(
            "POST",
            "https://discord.com/api/v10/channels/42/messages",
            Some(serde_json::json!({"content": "x"})),
        );
        assert_eq!(
            check("message.embed", &lit),
            Err(TemplateError::PathMismatch)
        );
    }

    #[test]
    fn webhook_create_is_pinned_to_the_channel_and_reads_its_reply_body_host_side() {
        let url = "https://discord.com/api/v10/channels/{target.channel_id}/webhooks";
        let good = tpl("POST", url, Some(serde_json::json!({"name": "bridge"})));
        assert_eq!(check("webhook.create", &good), Ok(()));
        let k = PlatformPolicy::discord()
            .kind("webhook.create")
            .unwrap()
            .clone();
        assert!(k.requires_pin);
        // The reply carries the webhook token: Body mode keeps it host-side (the producer
        // drops every key the manifest did not allowlist).
        assert_eq!(k.result, ResultMode::Body);
        let extra = tpl(
            "POST",
            url,
            Some(serde_json::json!({"name": "b", "avatar": "x"})),
        );
        assert_eq!(
            check("webhook.create", &extra),
            Err(TemplateError::BodyKeyNotAllowed)
        );
        let lit = tpl(
            "POST",
            "https://discord.com/api/v10/channels/42/webhooks",
            Some(serde_json::json!({"name": "b"})),
        );
        assert_eq!(
            check("webhook.create", &lit),
            Err(TemplateError::PathMismatch)
        );
    }
}
