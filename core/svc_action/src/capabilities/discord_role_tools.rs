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

use super::{is_role_key, RelayOp, RelayQueue, StageCapabilities};
use bundle_host_kv::KvBackend;

/// Tool name: grant a managed role.
pub(crate) const TOOL_ROLES_ADD: &str = "discord.member.roles.add";
/// Tool name: revoke a managed role.
pub(crate) const TOOL_ROLES_REMOVE: &str = "discord.member.roles.remove";

/// The outbound op a role tool NAME maps to; `None` for any other tool (the caller has already
/// matched the Discord service target).
fn role_tool_op(tool: &str) -> Option<RelayOp> {
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
    let obj = args
        .as_object()
        .ok_or_else(|| bad("args must be an object"))?;
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

impl<Q: RelayQueue, K: KvBackend> StageCapabilities<Q, K> {
    /// Executes a `discord.member.roles.add|remove` tool call authorized by the generic
    /// `tool.invoke` planner (manifest `requested_tools` + approved `tool.call` grant + write
    /// key). The HOST runs it through the SAME guild-pinned producer as `relay.push`
    /// `role.add|role.remove` (`handle_discord_queued_op`): managed-role KEY allowlist, origin
    /// channel pin, tokenized user, per-community throttle, `pin_sig`, `op_id` and drain
    /// confirmation. Returns `None` for any other tool (the caller continues its dispatch).
    pub(crate) async fn run_discord_role_tool(
        &self,
        scope: &super::InvokeScope,
        tool: &str,
        args: &Value,
    ) -> Option<Result<Value, ToolRefusal>> {
        let op = role_tool_op(tool)?;
        let result = async {
            let message = relay_message(op, args)?;
            match self
                .handle_discord_queued_op_scoped(scope, op, &message, true)
                .await
            {
                Ok(_confirmed) => Ok(json!({"ok": true, "op": op.as_str()})),
                Err(e) if e.code == "invalid_args" => Err(ToolRefusal::InvalidArgs(e.message)),
                Err(e) => Err(ToolRefusal::TargetUnavailable(format!(
                    "discord role tool refused: {}",
                    e.code
                ))),
            }
        }
        .await;
        Some(result)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const UUID_A: &str = "3f2b8c1e-9d4a-4c55-8a10-0a1b2c3d4e5f";

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
        let r = relay_message(
            RelayOp::RoleRemove,
            &json!({"user": UUID_A, "role_key": "a-b_1"}),
        )
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

    use super::super::tests::{caps, discord_scope, scope, FakeRelayQueue};

    fn args(extra: Value) -> Value {
        let mut v = json!({"user": UUID_A, "role_key": "vip"});
        for (k, val) in extra.as_object().unwrap() {
            v[k] = val.clone();
        }
        v
    }

    #[tokio::test]
    async fn tool_queues_the_guild_pinned_role_op_and_returns_ok() {
        for (tool, op) in [
            (TOOL_ROLES_ADD, "role.add"),
            (TOOL_ROLES_REMOVE, "role.remove"),
        ] {
            let c = caps(FakeRelayQueue::with_live_drain());
            let out = c
                .run_discord_role_tool(&discord_scope(), tool, &args(json!({})))
                .await
                .unwrap()
                .unwrap();
            assert_eq!(out, json!({"ok": true, "op": op}));
            let envs = c.relay_queue.envelopes();
            let e = &envs[0];
            assert_eq!(e["op"], op);
            assert_eq!(e["platform"], "discord");
            assert_eq!(e["text"], "vip", "role KEY rides in the MAC-covered text");
            assert_eq!(e["user_id"], format!("{{user:{UUID_A}}}"));
            assert_eq!(
                e["origin_channel"], "123456789012345678",
                "host-pinned origin"
            );
            assert_eq!(
                (e["tenant_id"].as_i64(), e["community_id"].as_i64()),
                (Some(7), Some(3))
            );
            for never in ["guild_id", "role_id", "channel"] {
                assert!(e.get(never).is_none(), "{never} must not reach the queue");
            }
            assert!(e["pin_sig"].as_str().is_some() && e["op_id"].as_str().is_some());
        }
    }

    #[tokio::test]
    async fn injected_arguments_are_refused_and_queue_nothing() {
        let c = caps(FakeRelayQueue::with_live_drain());
        for extra in [
            json!({"guild_id": "111111111111111111"}),
            json!({"role_id": "222222222222222222"}),
            json!({"channel": "9"}),
            json!({"tenant_id": 99}),
            json!({"origin_channel": "9"}),
        ] {
            let r = c
                .run_discord_role_tool(&discord_scope(), TOOL_ROLES_ADD, &args(extra.clone()))
                .await
                .unwrap();
            assert!(matches!(r, Err(ToolRefusal::InvalidArgs(_))), "{extra}");
        }
        assert!(c.relay_queue.envelopes().is_empty());
    }

    #[tokio::test]
    async fn unready_drain_drain_refusal_and_other_tools_are_handled_loudly() {
        // No drain advertising readiness: refused before queueing, as target_unavailable.
        let c = caps(FakeRelayQueue::default());
        let r = c
            .run_discord_role_tool(&discord_scope(), TOOL_ROLES_REMOVE, &args(json!({})))
            .await
            .unwrap();
        assert!(matches!(r, Err(ToolRefusal::TargetUnavailable(_))));
        assert!(c.relay_queue.envelopes().is_empty());
        // A drain refusal (e.g. role key not in the binding allowlist) is surfaced, not swallowed.
        let c = caps(FakeRelayQueue::with_drain_result(
            r#"{"ok":false,"code":"role_not_managed"}"#,
        ));
        let r = c
            .run_discord_role_tool(&discord_scope(), TOOL_ROLES_ADD, &args(json!({})))
            .await
            .unwrap();
        assert!(matches!(r, Err(ToolRefusal::TargetUnavailable(_))), "{r:?}");
        // Not a role tool -> not handled (dispatch continues).
        for t in [
            "classify_frame",
            "discord.scheduled_event.create",
            "discord.member.roles.set",
        ] {
            assert!(
                c.run_discord_role_tool(&discord_scope(), t, &json!({}))
                    .await
                    .is_none(),
                "{t}"
            );
        }
    }

    fn queued_scope(c: &StageCapabilities<FakeRelayQueue>) -> String {
        c.relay_queue.envelopes()[0]["origin_channel"]
            .as_str()
            .unwrap()
            .to_string()
    }

    /// External (Wix-style) event: no Discord channel, no guild -> the op is queued guild-scoped
    /// with NO assertion, i.e. pinned to the community's own paired guild by the drain.
    #[tokio::test]
    async fn an_external_event_role_op_is_guild_scoped_with_no_assertion() {
        let c = caps(FakeRelayQueue::with_live_drain());
        let mut sc = scope();
        sc.origin_platform = Some("wix".to_string());
        sc.origin_channel_id = None;
        let out = c
            .run_discord_role_tool(&sc, TOOL_ROLES_ADD, &args(json!({})))
            .await
            .unwrap()
            .unwrap();
        assert_eq!(out["ok"], true);
        assert_eq!(queued_scope(&c), "guild:-");
    }

    /// Discord gateway event (guild known, no channel): the SOURCE guild is asserted so the
    /// drain refuses when it is not the community's paired guild.
    #[tokio::test]
    async fn a_gateway_event_role_op_asserts_its_source_guild() {
        let c = caps(FakeRelayQueue::with_live_drain());
        let mut sc = discord_scope();
        sc.origin_channel_id = None;
        sc.origin_guild_id = Some("123456789012345679".to_string());
        c.run_discord_role_tool(&sc, TOOL_ROLES_REMOVE, &args(json!({})))
            .await
            .unwrap()
            .unwrap();
        assert_eq!(queued_scope(&c), "guild:123456789012345679");
    }

    /// A channel-bearing Discord event keeps the channel pin (unchanged); the guild scope is never
    /// taken from the tool args (injection is refused before this point).
    #[tokio::test]
    async fn a_channel_event_keeps_the_channel_pin() {
        let c = caps(FakeRelayQueue::with_live_drain());
        let mut sc = discord_scope();
        sc.origin_guild_id = Some("999".to_string());
        c.run_discord_role_tool(&sc, TOOL_ROLES_ADD, &args(json!({})))
            .await
            .unwrap()
            .unwrap();
        assert_eq!(queued_scope(&c), "123456789012345678");
    }

    /// The `relay.push` role path is NOT widened: a channel-less event is still refused there.
    #[tokio::test]
    async fn relay_push_role_ops_still_require_a_channel() {
        let c = caps(FakeRelayQueue::with_live_drain());
        let mut sc = scope();
        sc.origin_platform = Some("wix".to_string());
        sc.origin_channel_id = None;
        let msg = json!({"op":"role.add","user_id":format!("{{user:{UUID_A}}}"),"role_key":"vip"});
        let e = c
            .handle_discord_queued_op(&sc, RelayOp::RoleAdd, &msg)
            .await
            .unwrap_err();
        assert_eq!(e.code, "invalid_args");
        assert!(c.relay_queue.envelopes().is_empty());
    }
}
