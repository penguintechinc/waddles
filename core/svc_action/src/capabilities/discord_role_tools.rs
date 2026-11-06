//! Host-mediated Discord role tools (`discord.member.roles.add` / `.remove`).
//!
//! The Discord SERVICE bundle (`waddles.core.service.discord`) DECLARES these tools
//! (`exported_tools`: names + JSON arg schemas); this module is where the HOST serves
//! them. An authorized `tool.invoke` is NOT run as a nested guest call (the guest has no
//! mutating egress): it is translated into the existing guild-pinned `role.add` /
//! `role.remove` outbound producer (`handle_discord_queued_op`), so every pinning rule is
//! inherited unchanged -- guild from the binding, managed-role KEY allowlist, `{user:<uuid>}`
//! tokenized user, origin-channel pinning, per-community rate limit, `pin_sig` MAC, replay
//! `op_id`, and drain confirmation. Spec: `docs/connector-host-capability.md`.

use bundle_tool_call::ToolRefusal;
use serde_json::{json, Value};

use super::{is_role_key, RelayOp};

/// `app_id` of the Discord platform service bundle (the tool target).
pub(crate) const DISCORD_SERVICE_APP_ID: &str = "waddles.core.service.discord";
/// Tool name: grant a managed role.
pub(crate) const TOOL_ROLES_ADD: &str = "discord.member.roles.add";
/// Tool name: revoke a managed role.
pub(crate) const TOOL_ROLES_REMOVE: &str = "discord.member.roles.remove";

/// The outbound op a `(target, tool)` is host-mediated to, `None` when the pair is not one of
/// the host-served Discord role tools (it then takes the normal nested-guest path).
pub(crate) fn host_mediated_op(target: &str, tool: &str) -> Option<RelayOp> {
    if target != DISCORD_SERVICE_APP_ID {
        return None;
    }
    match tool {
        TOOL_ROLES_ADD => Some(RelayOp::RoleAdd),
        TOOL_ROLES_REMOVE => Some(RelayOp::RoleRemove),
        _ => None,
    }
}

/// `true` for a canonical lowercase hyphenated UUID (the tokenized-user spelling).
fn is_canonical_uuid(s: &str) -> bool {
    let b = s.as_bytes();
    b.len() == 36
        && b.iter().enumerate().all(|(i, c)| match i {
            8 | 13 | 18 | 23 => *c == b'-',
            _ => c.is_ascii_digit() || (b'a'..=b'f').contains(c),
        })
}

/// Validates the tool args and builds the relay message the pinned producer consumes.
///
/// Exactly `{user: <uuid>, role_key: <managed key>}`. ANY other key (`guild_id`, `role_id`,
/// `channel`, `tenant_id`, ...) is REFUSED, never ignored: the guild/role/channel are the
/// host's to pin, so a caller naming them is an injection attempt and fails loudly.
///
/// # Errors
/// [`ToolRefusal::InvalidArgs`] for a non-object, an extra/missing key, a non-UUID user, or a
/// role value that is not a managed-role key (a Discord snowflake can never match).
pub(crate) fn relay_message(op: RelayOp, args: &Value) -> Result<Value, ToolRefusal> {
    let bad = |m: &str| ToolRefusal::InvalidArgs(m.to_string());
    let obj = args.as_object().ok_or_else(|| bad("args must be an object"))?;
    if let Some(extra) = obj.keys().find(|k| *k != "user" && *k != "role_key") {
        return Err(ToolRefusal::InvalidArgs(format!(
            "unexpected argument {extra:?}: the guild, channel and role id are host-pinned"
        )));
    }
    let user = obj
        .get("user")
        .and_then(Value::as_str)
        .filter(|u| is_canonical_uuid(u))
        .ok_or_else(|| bad("`user` must be a lowercase canonical UUID"))?;
    let role_key = obj
        .get("role_key")
        .and_then(Value::as_str)
        .filter(|k| is_role_key(k))
        .ok_or_else(|| bad("`role_key` must be a managed-role key ([a-z][a-z0-9_-]{0,31})"))?;
    Ok(json!({
        "op": op.as_str(),
        "user_id": format!("{{user:{user}}}"),
        "role_key": role_key,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    const UUID_A: &str = "3f2b8c1e-9d4a-4c55-8a10-0a1b2c3d4e5f";

    #[test]
    fn only_the_discord_service_role_tools_are_host_mediated() {
        assert_eq!(
            host_mediated_op(DISCORD_SERVICE_APP_ID, TOOL_ROLES_ADD),
            Some(RelayOp::RoleAdd)
        );
        assert_eq!(
            host_mediated_op(DISCORD_SERVICE_APP_ID, TOOL_ROLES_REMOVE),
            Some(RelayOp::RoleRemove)
        );
        assert_eq!(host_mediated_op(DISCORD_SERVICE_APP_ID, "classify_frame"), None);
        assert_eq!(host_mediated_op("waddles.core.service.other", TOOL_ROLES_ADD), None);
    }

    #[test]
    fn valid_args_become_the_pinned_relay_message() {
        let m = relay_message(
            RelayOp::RoleAdd,
            &json!({"user": UUID_A, "role_key": "vip"}),
        )
        .unwrap();
        assert_eq!(
            m,
            json!({"op":"role.add","user_id":format!("{{user:{UUID_A}}}"),"role_key":"vip"})
        );
        let r = relay_message(RelayOp::RoleRemove, &json!({"user": UUID_A, "role_key": "a-b_1"}))
            .unwrap();
        assert_eq!(r["op"], "role.remove");
    }

    #[test]
    fn injection_and_malformed_args_are_refused() {
        for bad in [
            json!("x"),
            json!({"user": UUID_A}),
            json!({"role_key": "vip"}),
            json!({"user": UUID_A, "role_key": "vip", "guild_id": "111111111111111111"}),
            json!({"user": UUID_A, "role_key": "vip", "role_id": "222222222222222222"}),
            json!({"user": UUID_A, "role_key": "vip", "channel": "1"}),
            json!({"user": UUID_A, "role_key": "vip", "tenant_id": 9}),
            json!({"user": "333333333333333333", "role_key": "vip"}),
            json!({"user": UUID_A.to_uppercase(), "role_key": "vip"}),
            json!({"user": format!("{{user:{UUID_A}}}"), "role_key": "vip"}),
            json!({"user": UUID_A, "role_key": "222222222222222222"}),
            json!({"user": UUID_A, "role_key": "VIP"}),
            json!({"user": UUID_A, "role_key": "a".repeat(33)}),
            json!({"user": 5, "role_key": "vip"}),
        ] {
            assert!(
                matches!(
                    relay_message(RelayOp::RoleAdd, &bad),
                    Err(ToolRefusal::InvalidArgs(_))
                ),
                "{bad}"
            );
        }
    }
}
