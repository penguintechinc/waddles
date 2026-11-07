//! The relay-reachable platform op table: which `(platform, op)` pairs a bundle may name through
//! `relay.send`, and the catalog permission each one requires.
//!
//! This is the producer-side twin of `svc_ingest`'s sender-exec policy table (the consumer
//! side). The two crates cannot import each other, so both are parity-tested against
//! `tests/fixtures/platform_relay_ops.json`: an op added to one table and not the other fails a
//! test. An op absent here is an explicit unknown-op refusal at the producer, never a default.

use crate::permission::PermissionId;

/// `(platform, ops)` rows. Role ops (`role.*`) and scheduled-event ops (`event.*`) are not
/// listed: they have their own typed producer paths.
pub const RELAY_OPS: &[(&str, &[&str])] = &[
    (
        "twitch",
        &[
            "chat.send",
            "chat.delete",
            "dm.send",
            "mod.ban",
            "mod.timeout",
            "mod.unban",
            "eventsub.create",
            "eventsub.delete",
            "eventsub.list",
        ],
    ),
    (
        "discord",
        &[
            "chat.send",
            "chat.delete",
            "dm.send",
            "mod.kick",
            "mod.ban",
            "mod.unban",
            "mod.timeout",
            "message.edit",
            "reaction.add",
            "commands.register",
            "commands.register.guild",
            "interaction.callback",
            "interaction.followup",
        ],
    ),
    (
        "youtube",
        &[
            "chat.send",
            "chat.delete",
            "mod.ban",
            "mod.unban",
            "mod.add",
            "mod.remove",
        ],
    ),
    // Kick: `chat.send` only. `chat.delete` (global message id, needs a channel pin the executor
    // cannot enforce for kick yet) and `mod.*` (integer body ids) are absent on purpose, so they
    // stay unknown-op refusals until their host seams exist.
    ("kick", &["chat.send"]),
];

/// The static op spelling registered for `(platform, op)`, or `None`.
#[must_use]
pub fn registered(platform: &str, op: &str) -> Option<&'static str> {
    RELAY_OPS
        .iter()
        .find(|(p, _)| *p == platform)
        .and_then(|(_, ops)| ops.iter().copied().find(|o| *o == op))
}

/// The static op spelling if ANY platform registers `op` (the platform is checked later, against
/// the call's provider, by [`registered`]).
#[must_use]
pub fn registered_on_any_platform(op: &str) -> Option<&'static str> {
    RELAY_OPS
        .iter()
        .flat_map(|(_, ops)| ops.iter().copied())
        .find(|o| *o == op)
}

/// The catalog permission `(platform, op)` requires, or `None` when the pair is not registered
/// (the caller refuses; there is no fallback permission).
#[must_use]
pub fn permission_for(platform: &str, op: &str) -> Option<PermissionId> {
    let op = registered(platform, op)?;
    let p = platform.to_string();
    Some(match op {
        "chat.send" => PermissionId::ChatSend(p),
        "chat.delete" => PermissionId::ChatDelete(p),
        "dm.send" => PermissionId::DmSend(p),
        "message.edit" => PermissionId::MessageEdit(p),
        "reaction.add" => PermissionId::MessageReaction(p),
        o if o.starts_with("mod.") => PermissionId::Moderation(p),
        o if o.starts_with("commands.register") => PermissionId::CommandsRegister(p),
        o if o.starts_with("eventsub.") => PermissionId::EventsubManage(p),
        o if o.starts_with("interaction.") => PermissionId::InteractionRespond(p),
        // Every op in RELAY_OPS is mapped above; the parity test enforces it, and an unmapped
        // op is refused rather than guessed at.
        _ => return None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn table_matches_the_parity_fixture() {
        let fixture: serde_json::Value =
            serde_json::from_str(include_str!("../tests/fixtures/platform_relay_ops.json"))
                .unwrap();
        let mut from_fixture: Vec<(String, Vec<String>)> = fixture
            .as_object()
            .unwrap()
            .iter()
            .map(|(p, ops)| {
                let mut ops: Vec<String> = ops
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|o| o.as_str().unwrap().to_string())
                    .collect();
                ops.sort();
                (p.clone(), ops)
            })
            .collect();
        let mut from_code: Vec<(String, Vec<String>)> = RELAY_OPS
            .iter()
            .map(|(p, ops)| {
                let mut ops: Vec<String> = ops.iter().map(|o| (*o).to_string()).collect();
                ops.sort();
                ((*p).to_string(), ops)
            })
            .collect();
        from_fixture.sort();
        from_code.sort();
        assert_eq!(from_code, from_fixture);
    }

    #[test]
    fn every_registered_op_maps_to_a_permission_on_its_own_platform() {
        let mut n = 0;
        for (platform, ops) in RELAY_OPS {
            for op in *ops {
                let id = permission_for(platform, op)
                    .unwrap_or_else(|| panic!("{platform}/{op} has no permission"));
                assert!(
                    id.canonical_id().ends_with(platform) && id.canonical_id().contains(['.', ':']),
                    "{id:?}"
                );
                n += 1;
            }
        }
        assert_eq!(n, 9 + 13 + 6 + 1);
    }

    #[test]
    fn spot_check_permission_families() {
        for (p, op, want) in [
            ("twitch", "mod.ban", "moderation.twitch"),
            ("twitch", "chat.delete", "chat.delete:twitch"),
            ("twitch", "eventsub.create", "eventsub.manage:twitch"),
            ("discord", "mod.timeout", "moderation.discord"),
            ("discord", "message.edit", "message.edit:discord"),
            ("discord", "reaction.add", "message.reaction:discord"),
            ("discord", "commands.register", "commands.register:discord"),
            (
                "discord",
                "commands.register.guild",
                "commands.register:discord",
            ),
            (
                "discord",
                "interaction.followup",
                "interaction.respond:discord",
            ),
            ("youtube", "mod.add", "moderation.youtube"),
            ("youtube", "chat.delete", "chat.delete:youtube"),
        ] {
            assert_eq!(permission_for(p, op).unwrap().canonical_id(), want);
        }
    }

    #[test]
    fn unknown_or_cross_platform_ops_are_refused_never_defaulted() {
        assert!(permission_for("twitch", "mod.kick").is_none());
        assert!(permission_for("youtube", "eventsub.list").is_none());
        assert!(permission_for("kick", "mod.ban").is_none());
        assert!(permission_for("kick", "chat.delete").is_none());
        assert!(permission_for("slack", "chat.send").is_none());
        assert!(permission_for("discord", "role.add").is_none());
        assert!(registered_on_any_platform("mod.kick").is_some());
        assert!(registered_on_any_platform("nope").is_none());
        assert!(registered("twitch", "").is_none());
    }
}
