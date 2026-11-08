//! Kick public-API template policy (<https://docs.kick.com/apis/chat>,
//! <https://docs.kick.com/apis/moderation>): `chat.send`, `chat.delete`, `mod.ban`,
//! `mod.timeout`, `mod.unban`. The host is exactly `api.kick.com`; credentials and the
//! broadcaster id are slots resolved per send by the connection resolver, never visible to the
//! bundle.
//!
//! `chat.send` posts as the application bot (`type: "bot"`), which Kick delivers to the channel
//! attached to the token, so no channel id appears in the template.
//!
//! `chat.delete` (`DELETE /public/v1/chat/{message_id}`) requires a proven channel
//! (`requires_pin`): `outbound_pin::pin_target` knows `kick`, and `sender_exec::pin_channel` fails
//! closed for a pinned kind on any platform it cannot pin.
//!
//! Moderation bodies carry `broadcaster_user_id` and `user_id` as JSON INTEGERS. The broadcaster
//! is the host slot `{{KICK_BROADCASTER_ID}}` (`SlotShape::Int`, written as a JSON number from
//! the community connection's own binding -- a bundle cannot name another channel). The target
//! `user_id` is a literal integer the bundle shapes from the producer's id. **Protected target:**
//! `distinct_body_ints` refuses any request whose `user_id` equals the broadcaster id (or is not
//! an integer), after substitution, before anything leaves the host.
//!
//! Still ABSENT (an `UnknownOp` at parse time and `UnsupportedKind` at execution -- fail-closed):
//! `dm.send`, `mod.kick`, role/event ops. `unsupported_ops_stay_absent` pins that.

use super::{resolver_slot, KindPolicy, PathSeg, PlatformPolicy, Position, SlotShape, SlotSyntax};

/// `/public/v1/<rest...>` as exact path segments.
fn api(rest: Vec<PathSeg>) -> Vec<PathSeg> {
    let mut p = vec![PathSeg::Lit("public"), PathSeg::Lit("v1")];
    p.extend(rest);
    p
}

/// A moderation kind: the community token + its OWN broadcaster id, a proven channel, and the
/// protected-target rule (`user_id` must be an integer different from the broadcaster).
fn moderation_kind(kind: &'static str, method: &'static str) -> KindPolicy {
    KindPolicy {
        slots: vec!["KICK_TOKEN", "KICK_BROADCASTER_ID"],
        max_body: Some(1024),
        body_keys: vec!["broadcaster_user_id", "user_id", "duration", "reason"],
        requires_pin: true,
        distinct_body_ints: vec![("/broadcaster_user_id", "/user_id")],
        ..KindPolicy::new(
            kind,
            method,
            api(vec![PathSeg::Lit("moderation"), PathSeg::Lit("bans")]),
        )
    }
}

impl PlatformPolicy {
    /// Kick public-API policy.
    #[must_use]
    pub fn kick() -> Self {
        Self {
            platform: "kick",
            syntax: SlotSyntax::Double,
            hosts: vec!["api.kick.com".to_string()],
            slots: vec![
                resolver_slot(
                    "KICK_TOKEN",
                    "token",
                    SlotShape::Secret,
                    vec![Position::Header("authorization")],
                ),
                resolver_slot(
                    "KICK_BROADCASTER_ID",
                    "broadcaster_id",
                    SlotShape::Int,
                    vec![Position::Body("/broadcaster_user_id")],
                ),
            ],
            kinds: vec![
                // https://docs.kick.com/apis/chat (Post Chat Message)
                KindPolicy {
                    slots: vec!["KICK_TOKEN"],
                    max_body: Some(4096),
                    body_keys: vec!["content", "type", "reply_to_message_id"],
                    text_key: "text",
                    ..KindPolicy::new("chat.send", "POST", api(vec![PathSeg::Lit("chat")]))
                },
                // https://docs.kick.com/apis/chat (Delete Chat Message)
                KindPolicy {
                    slots: vec!["KICK_TOKEN"],
                    requires_pin: true,
                    ..KindPolicy::new(
                        "chat.delete",
                        "DELETE",
                        api(vec![PathSeg::Lit("chat"), PathSeg::Id]),
                    )
                },
                // https://docs.kick.com/apis/moderation (Timeout or ban user / Unban user)
                moderation_kind("mod.ban", "POST"),
                moderation_kind("mod.timeout", "POST"),
                moderation_kind("mod.unban", "DELETE"),
            ],
            auth_prefixes: vec!["Bearer "],
            allowed_headers: vec!["authorization", "content-type", "accept", "user-agent"],
            allow_http: false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn host_is_only_the_kick_api_and_http_is_off() {
        let p = PlatformPolicy::kick();
        assert_eq!(p.hosts, ["api.kick.com"]);
        assert!(!p.allow_http);
        assert_eq!(p.platform, "kick");
    }

    #[test]
    fn the_token_slot_lives_in_the_authorization_header_and_is_the_only_secret() {
        let p = PlatformPolicy::kick();
        assert_eq!(p.slots.len(), 2, "token + broadcaster id");
        assert_eq!(p.slots[0].name, "KICK_TOKEN");
        assert_eq!(p.slots[0].handle, "token");
        assert!(!p.slots[0].is_app_scoped());
        assert_eq!(p.slots[0].positions.len(), 1);
    }

    #[test]
    fn chat_send_is_registered_with_the_documented_shape() {
        let p = PlatformPolicy::kick();
        let send = p.kind("chat.send").unwrap();
        assert_eq!(send.method, "POST");
        assert!(send.max_body.is_some() && !send.requires_pin);
        assert!(send.distinct_body_ints.is_empty());
    }

    #[test]
    fn chat_delete_is_a_pinned_delete_of_one_message_id() {
        let p = PlatformPolicy::kick();
        let d = p.kind("chat.delete").unwrap();
        assert_eq!(d.method, "DELETE");
        assert!(d.requires_pin && d.max_body.is_none());
        assert_eq!(d.slots, ["KICK_TOKEN"]);
    }

    #[test]
    fn moderation_kinds_are_pinned_integer_bodied_and_protect_the_broadcaster() {
        let p = PlatformPolicy::kick();
        for (op, method) in [
            ("mod.ban", "POST"),
            ("mod.timeout", "POST"),
            ("mod.unban", "DELETE"),
        ] {
            let k = p.kind(op).unwrap();
            assert_eq!(k.method, method, "{op}");
            assert!(k.requires_pin, "{op}");
            assert_eq!(k.slots, ["KICK_TOKEN", "KICK_BROADCASTER_ID"], "{op}");
            assert_eq!(
                k.distinct_body_ints,
                [("/broadcaster_user_id", "/user_id")],
                "{op}"
            );
        }
        let i = p.slot_index("KICK_BROADCASTER_ID").unwrap();
        assert_eq!(p.slots[i].shape, SlotShape::Int);
        assert_eq!(p.slots[i].handle, "broadcaster_id");
        assert_eq!(
            p.slots[i].positions,
            [Position::Body("/broadcaster_user_id")]
        );
    }

    /// Ops with no Kick sender stay unknown (fail-closed), never defaulted.
    #[test]
    fn unsupported_ops_stay_absent() {
        let p = PlatformPolicy::kick();
        for op in ["dm.send", "mod.kick", "role.add", "mod.add"] {
            assert!(p.kind(op).is_none(), "{op}");
        }
    }
}
