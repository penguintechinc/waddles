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
            "chat.clear",
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
            "message.embed",
            "webhook.create",
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
    // Kick: chat.send + moderation. `chat.delete` is absent on purpose (fail closed): a message id
    // is global and not bound to the community channel. `mod.*` bodies carry integer ids (host
    // `SlotShape::Int` broadcaster slot, protected-target check).
    (
        "kick",
        &["chat.send", "mod.ban", "mod.timeout", "mod.unban"],
    ),
];

/// Ops reachable ONLY through the host tool path (a manifest `host_op` declaration behind the
/// planner's grant / verified-actor / protected-target checks), never through the guest
/// `relay.push` path: `relay.push` has none of those gates, and `webhook.create`'s reply carries
/// a secret token the host must keep. `relay.push` refuses these as `host_tool_only`.
///
/// EVERY destructive moderation op (`mod.*`: ban, timeout, kick, unban, YouTube moderator
/// add/remove) is host-tool-only: the scope check against the verified actor and the
/// protected-target refusal exist only on the host tool path. Listed explicitly for audit;
/// [`host_tool_only`] additionally treats any registered `mod.*` op as host-tool-only so a
/// future moderation op (e.g. Kick's) is closed by default.
pub const HOST_TOOL_ONLY_OPS: &[(&str, &str)] = &[
    ("twitch", "chat.clear"),
    ("discord", "webhook.create"),
    ("twitch", "mod.ban"),
    ("twitch", "mod.timeout"),
    ("twitch", "mod.unban"),
    ("discord", "mod.kick"),
    ("discord", "mod.ban"),
    ("discord", "mod.unban"),
    ("discord", "mod.timeout"),
    ("youtube", "mod.ban"),
    ("youtube", "mod.unban"),
    ("youtube", "mod.add"),
    ("youtube", "mod.remove"),
    ("kick", "mod.ban"),
    ("kick", "mod.timeout"),
    ("kick", "mod.unban"),
    // EventSub create/delete mutate app-wide subscriptions bound to the host callback URL and
    // secret: only the host reconcile path may create or tear them down. `eventsub.list` is
    // read-only and stays relay-reachable.
    ("twitch", "eventsub.create"),
    ("twitch", "eventsub.delete"),
];

/// `true` when `(platform, op)` may only be executed by the host tool path: listed in
/// [`HOST_TOOL_ONLY_OPS`], or any registered `mod.*` moderation op.
#[must_use]
pub fn host_tool_only(platform: &str, op: &str) -> bool {
    HOST_TOOL_ONLY_OPS
        .iter()
        .any(|(p, o)| *p == platform && *o == op)
        || (op.starts_with("mod.") && registered(platform, op).is_some())
}

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
        "chat.clear" => PermissionId::ChatClear(p),
        "message.embed" => PermissionId::ChatSend(p),
        "webhook.create" => PermissionId::WebhookManage(p),
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
        assert_eq!(n, 10 + 15 + 6 + 4);
    }

    #[test]
    fn spot_check_permission_families() {
        for (p, op, want) in [
            ("twitch", "mod.ban", "moderation.twitch"),
            ("twitch", "chat.delete", "chat.delete:twitch"),
            ("twitch", "chat.clear", "chat.clear:twitch"),
            ("discord", "webhook.create", "webhook.manage:discord"),
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
            ("kick", "chat.send", "chat.send:kick"),
            ("kick", "mod.ban", "moderation.kick"),
            ("kick", "mod.timeout", "moderation.kick"),
            ("kick", "mod.unban", "moderation.kick"),
        ] {
            assert_eq!(permission_for(p, op).unwrap().canonical_id(), want);
        }
    }

    #[test]
    fn host_tool_only_ops_are_registered_and_distinguished() {
        for (p, o) in HOST_TOOL_ONLY_OPS {
            assert!(registered(p, o).is_some(), "{p}/{o} must be registered");
            assert!(host_tool_only(p, o));
        }
        assert!(!host_tool_only("discord", "chat.clear"));
        assert!(!host_tool_only("twitch", "chat.send"));
        assert!(host_tool_only("twitch", "eventsub.create"));
        assert!(host_tool_only("twitch", "eventsub.delete"));
        assert!(!host_tool_only("twitch", "eventsub.list"));
        assert!(!host_tool_only("discord", "eventsub.create"));
        // Still registered with its permission, so the host reconcile path keeps working.
        assert_eq!(
            permission_for("twitch", "eventsub.create")
                .unwrap()
                .canonical_id(),
            "eventsub.manage:twitch"
        );
        // Every registered moderation op on every platform is host-tool-only, and the explicit
        // list covers them all (so the audit table cannot drift from the structural rule).
        let mut seen = 0;
        for (platform, ops) in RELAY_OPS {
            for op in *ops {
                if op.starts_with("mod.") {
                    seen += 1;
                    assert!(host_tool_only(platform, op), "{platform}/{op}");
                    assert!(
                        HOST_TOOL_ONLY_OPS.contains(&(*platform, *op)),
                        "{platform}/{op} missing from the explicit list"
                    );
                }
            }
        }
        assert_eq!(seen, 3 + 4 + 4 + 3, "denominator: moderation ops checked");
        // Unregistered ops are not silently host-only: they stay unknown-op.
        assert!(!host_tool_only("kick", "mod.nonexistent"));
        // A host-tool-only op never shares a permission with a relay-reachable op.
        assert_ne!(
            permission_for("twitch", "chat.clear"),
            permission_for("twitch", "chat.delete")
        );
        assert_ne!(
            permission_for("discord", "webhook.create"),
            permission_for("discord", "mod.ban")
        );
    }

    #[test]
    fn unknown_or_cross_platform_ops_are_refused_never_defaulted() {
        assert!(permission_for("twitch", "mod.kick").is_none());
        assert!(permission_for("youtube", "eventsub.list").is_none());
        assert!(permission_for("kick", "mod.kick").is_none());
        assert!(permission_for("kick", "dm.send").is_none());
        assert!(permission_for("kick", "chat.delete").is_none());
        assert!(permission_for("slack", "chat.send").is_none());
        assert!(permission_for("discord", "role.add").is_none());
        assert!(registered_on_any_platform("mod.kick").is_some());
        assert!(registered_on_any_platform("nope").is_none());
        assert!(registered("twitch", "").is_none());
    }
}
