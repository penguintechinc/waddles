//! Kick public-API template policy (<https://docs.kick.com/apis/chat>): `chat.send`. The host is
//! exactly `api.kick.com`; the only slot is the community's bearer token, resolved per send by
//! the connection resolver and never visible to the bundle.
//!
//! `chat.send` posts as the application bot (`type: "bot"`), which Kick always delivers to the
//! channel attached to the token, so no channel id appears in the template and no pin is needed.
//!
//! Deliberately ABSENT (each is an `UnknownOp` at parse time and `UnsupportedKind` at execution
//! -- fail-closed, never a silent no-op):
//! - `chat.delete` (`DELETE /public/v1/chat/{message_id}`): the message id is global, so the
//!   community token could delete messages in ANY channel its owner moderates unless the stamp
//!   pins the channel. `sender_exec::pin_channel` only enforces a pin for twitch|discord|youtube
//!   and returns `Ok(None)` for any other platform even when `requires_pin` is set, so a Kick
//!   kind cannot be pinned yet. Needs `outbound_pin::pin_target` to learn `kick`.
//! - `mod.ban` / `mod.timeout` / `mod.unban` (<https://docs.kick.com/apis/moderation>): Kick takes
//!   `broadcaster_user_id` / `user_id` as JSON integers, but the substituter writes only JSON
//!   strings, so a registered kind would be sent and rejected upstream. Needs an integer body slot.
//!
//! `unsupported_ops_stay_absent_until_their_host_seams_exist` pins both gaps.

use super::{resolver_slot, KindPolicy, PathSeg, PlatformPolicy, Position, SlotShape, SlotSyntax};

/// `/public/v1/<rest...>` as exact path segments.
fn api(rest: Vec<PathSeg>) -> Vec<PathSeg> {
    let mut p = vec![PathSeg::Lit("public"), PathSeg::Lit("v1")];
    p.extend(rest);
    p
}

impl PlatformPolicy {
    /// Kick public-API policy.
    #[must_use]
    pub fn kick() -> Self {
        Self {
            platform: "kick",
            syntax: SlotSyntax::Double,
            hosts: vec!["api.kick.com".to_string()],
            slots: vec![resolver_slot(
                "KICK_TOKEN",
                "token",
                SlotShape::Secret,
                vec![Position::Header("authorization")],
            )],
            kinds: vec![
                // https://docs.kick.com/apis/chat (Post Chat Message)
                KindPolicy {
                    slots: vec!["KICK_TOKEN"],
                    max_body: Some(4096),
                    body_keys: vec!["content", "type", "reply_to_message_id"],
                    text_key: "text",
                    ..KindPolicy::new("chat.send", "POST", api(vec![PathSeg::Lit("chat")]))
                },
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
    fn the_only_slot_is_the_token_and_it_lives_in_the_authorization_header() {
        let p = PlatformPolicy::kick();
        assert_eq!(p.slots.len(), 1);
        assert_eq!(p.slots[0].name, "KICK_TOKEN");
        assert_eq!(p.slots[0].handle, "token");
        assert!(!p.slots[0].is_app_scoped());
        assert_eq!(p.slots[0].positions.len(), 1);
    }

    #[test]
    fn chat_send_is_registered_with_the_documented_shape() {
        let p = PlatformPolicy::kick();
        assert_eq!(p.kinds.len(), 1);
        let send = p.kind("chat.send").unwrap();
        assert_eq!(send.method, "POST");
        assert!(send.max_body.is_some() && !send.requires_pin);
    }

    /// Pins the host-seam gaps: when the executor can pin a Kick channel / write integer body
    /// slots this test must fail on purpose, prompting the real kinds.
    #[test]
    fn unsupported_ops_stay_absent_until_their_host_seams_exist() {
        let p = PlatformPolicy::kick();
        for op in [
            "chat.delete",
            "mod.ban",
            "mod.timeout",
            "mod.unban",
            "dm.send",
            "mod.kick",
        ] {
            assert!(p.kind(op).is_none(), "{op}");
        }
    }
}
