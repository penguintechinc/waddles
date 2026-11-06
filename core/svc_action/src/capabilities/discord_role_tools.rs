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

use super::{
    denied, is_discord_snowflake, is_role_key, now_epoch_ms, InvokeScope, RelayOp, RelayQueue,
    StageCapabilities, DISCORD_DRAIN_READY_KEY, DISCORD_OP_ACK_KEY_PREFIX, DISCORD_OP_ACK_POLL,
    OUTBOUND_SCHEMA_VERSION,
};
use bundle_host_kv::KvBackend;
use penguin_bundle_host::wire::HostResultError;

/// Tool name: grant a managed role.
pub(crate) const TOOL_ROLES_ADD: &str = "discord.member.roles.add";
/// Tool name: revoke a managed role.
pub(crate) const TOOL_ROLES_REMOVE: &str = "discord.member.roles.remove";

/// Tool name: make a member hold exactly the given managed roles (read-then-diff, host-side).
pub(crate) const TOOL_ROLES_SET: &str = "discord.member.roles.set";
/// Tool name: list the community's managed roles with their assignability.
pub(crate) const TOOL_ROLES_LIST: &str = "discord.roles.list";

/// Verified external, non-chat platforms whose events may drive channel-less role ops
/// (binding guild only, `guild:-`). Everything else is refused; extend deliberately.
const EXTERNAL_ROLE_PLATFORMS: &[&str] = &["wix"];

/// The MAC-covered wire `origin_channel` a role tool op is queued with, decided by the ONE
/// platform field (`scope.origin_platform`): a Discord event with a channel keeps the channel pin;
/// a Discord event without one asserts its host-derived source guild (`guild:<id>`) or is refused;
/// an allowlisted external platform gets `guild:-` (binding guild only); every other origin
/// (twitch/youtube/unknown) is refused.
pub(crate) fn tool_origin_scope(
    scope: &InvokeScope,
    what: &str,
) -> Result<String, HostResultError> {
    let platform = scope.origin_platform.as_deref();
    if platform == Some("discord") {
        if let Some(channel) = scope.origin_channel_id.as_deref() {
            return if is_discord_snowflake(channel) {
                Ok(channel.to_string())
            } else {
                Err(denied(
                    "invalid_args",
                    format!("discord {what} origin channel id is not a valid snowflake"),
                ))
            };
        }
    }
    match (platform, scope.origin_guild_id.as_deref()) {
        (Some("discord"), Some(g)) => Ok(format!("guild:{g}")),
        (Some("discord"), None) => Err(denied(
            "invalid_args",
            format!("discord {what} from a discord event needs a valid source guild"),
        )),
        (Some(p), _) if EXTERNAL_ROLE_PLATFORMS.contains(&p) => Ok("guild:-".to_string()),
        _ => Err(denied(
            "invalid_args",
            format!("discord {what} cannot be triggered from this event origin"),
        )),
    }
}

/// Validates `discord.member.roles.set` args: exactly `{user: uuid, role_keys: [1..=16 keys]}`.
/// Any other key is REFUSED (guild/role ids are the host's to pin). Returns `(user, keys)` with
/// the keys sorted and deduplicated.
fn bulk_set_args(args: &Value) -> Result<(String, Vec<String>), ToolRefusal> {
    let bad = |m: &str| ToolRefusal::InvalidArgs(m.to_string());
    let obj = args
        .as_object()
        .ok_or_else(|| bad("args must be an object"))?;
    if let Some(extra) = obj.keys().find(|k| *k != "user" && *k != "role_keys") {
        return Err(ToolRefusal::InvalidArgs(format!(
            "unexpected argument {extra:?}: the guild, channel and role id are host-pinned"
        )));
    }
    let user = obj
        .get("user")
        .and_then(Value::as_str)
        .filter(|u| is_canonical_uuid(u))
        .ok_or_else(|| bad("`user` must be a lowercase canonical UUID"))?;
    let mut keys: Vec<String> = obj
        .get("role_keys")
        .and_then(Value::as_array)
        .ok_or_else(|| bad("`role_keys` must be an array of managed-role keys"))?
        .iter()
        .map(|k| k.as_str().filter(|k| is_role_key(k)).map(str::to_string))
        .collect::<Option<_>>()
        .ok_or_else(|| bad("`role_keys` entries must be managed-role keys"))?;
    keys.sort();
    keys.dedup();
    if keys.is_empty() || keys.len() > 16 {
        return Err(bad("`role_keys` must hold 1..=16 distinct keys"));
    }
    Ok((format!("{{user:{user}}}"), keys))
}

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
        if tool == TOOL_ROLES_SET || tool == TOOL_ROLES_LIST {
            return Some(self.run_bulk_role_tool(scope, tool, args).await);
        }
        let op = role_tool_op(tool)?;
        let result = async {
            let message = relay_message(op, args)?;
            match self
                .handle_discord_queued_op_scoped(scope, op, &message, true)
                .await
            {
                Ok(_confirmed) => Ok(json!({"ok": true, "op": op.as_str()})),
                Err(e) if e.code == "invalid_args" => Err(ToolRefusal::InvalidArgs(e.message)),
                // Several paired guilds: a typed refusal, never a guess (and not retryable).
                Err(e) if e.code == "ambiguous_paired_guild" => Err(ToolRefusal::Forbidden(
                    format!("ambiguous_paired_guild: {}", e.message),
                )),
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

impl<Q: RelayQueue, K: KvBackend> StageCapabilities<Q, K> {
    /// `discord.member.roles.set` / `discord.roles.list`: queues the host-side bulk drain op
    /// (`role.set` / `role.list`) under the same origin scope rules as add/remove and returns the
    /// drain's confirmed result (`list` returns its `roles` payload to the caller).
    async fn run_bulk_role_tool(
        &self,
        scope: &InvokeScope,
        tool: &str,
        args: &Value,
    ) -> Result<Value, ToolRefusal> {
        let is_set = tool == TOOL_ROLES_SET;
        let op: &'static str = if is_set { "role.set" } else { "role.list" };
        let (user, keys) = if is_set {
            let (u, k) = bulk_set_args(args)?;
            (Some(u), k)
        } else {
            if args.as_object().is_none_or(|o| !o.is_empty()) {
                return Err(ToolRefusal::InvalidArgs(
                    "discord.roles.list takes no arguments".to_string(),
                ));
            }
            (None, Vec::new())
        };
        if scope.tenant_id < 1 || scope.community_id < 1 {
            return Err(ToolRefusal::Forbidden(
                "discord role tools need a community-scoped invoke".to_string(),
            ));
        }
        let origin =
            tool_origin_scope(scope, op).map_err(|e| ToolRefusal::InvalidArgs(e.message))?;
        self.throttle_role_op(scope)
            .await
            .map_err(|e| ToolRefusal::TargetUnavailable(e.code))?;
        let unavailable =
            |c: &str| ToolRefusal::TargetUnavailable(format!("discord {op} refused: {c}"));
        match self.relay_queue.key_exists(DISCORD_DRAIN_READY_KEY).await {
            Ok(true) => {}
            Ok(false) | Err(_) => return Err(unavailable("relay_unavailable")),
        }
        let op_id = uuid::Uuid::new_v4().to_string();
        let ack_timeout = self.discord_ack_timeout;
        let exp_ms = now_epoch_ms()
            .saturating_add(u64::try_from(ack_timeout.as_millis()).unwrap_or(u64::MAX));
        let mut env = json!({
            "v": OUTBOUND_SCHEMA_VERSION, "op": op, "platform": "discord",
            "origin_channel": origin,
            "community_id": i64::from(scope.community_id),
            "tenant_id": i64::from(scope.tenant_id),
            "op_id": op_id, "exp_ms": exp_ms,
        });
        if let (Some(obj), Some(u)) = (env.as_object_mut(), user) {
            obj.insert("user_id".to_string(), Value::String(u));
            obj.insert("text".to_string(), Value::String(keys.join(",")));
        }
        self.sign_envelope(&mut env)
            .map_err(|_| unavailable("relay_unavailable"))?;
        self.relay_queue
            .lpush(&super::outbound_relay_queue_key("discord"), env.to_string())
            .await
            .map_err(|_| unavailable("relay_unavailable"))?;
        let ack = self.await_bulk_ack(&op_id, ack_timeout).await?;
        match ack.get("ok").and_then(Value::as_bool) {
            Some(true) => Ok(if is_set {
                json!({"ok": true, "op": op})
            } else {
                let roles = ack
                    .pointer("/data/roles")
                    .cloned()
                    .ok_or_else(|| unavailable("bad_result"))?;
                json!({"ok": true, "roles": roles})
            }),
            _ => Err(refusal_for_code(
                ack.get("code").and_then(Value::as_str),
                op,
            )),
        }
    }

    /// Polls the drain's result key for `op_id` until `timeout`; a result that never arrives is
    /// `not_confirmed` (the op may or may not have run -- never reported as done).
    async fn await_bulk_ack(
        &self,
        op_id: &str,
        timeout: std::time::Duration,
    ) -> Result<Value, ToolRefusal> {
        let key = format!("{DISCORD_OP_ACK_KEY_PREFIX}{op_id}");
        let deadline = tokio::time::Instant::now() + timeout;
        loop {
            match self.relay_queue.take(&key).await {
                Ok(Some(raw)) => {
                    return serde_json::from_str(&raw).map_err(|_| {
                        ToolRefusal::TargetUnavailable(
                            "discord bulk role result unreadable".to_string(),
                        )
                    })
                }
                Ok(None) => {}
                Err(_) => {
                    return Err(ToolRefusal::TargetUnavailable(
                        "relay_unavailable".to_string(),
                    ))
                }
            }
            if tokio::time::Instant::now() >= deadline {
                return Err(ToolRefusal::TargetUnavailable("not_confirmed".to_string()));
            }
            tokio::time::sleep(DISCORD_OP_ACK_POLL).await;
        }
    }
}

/// Maps a drain refusal code onto the tool framework's taxonomy (typed, never swallowed).
fn refusal_for_code(code: Option<&str>, op: &str) -> ToolRefusal {
    let c = code.unwrap_or("relay_failed");
    let msg = format!("discord {op} refused: {c}");
    match c {
        "role_not_managed"
        | "role_privileged"
        | "role_not_found"
        | "user_not_in_community"
        | "not_in_community"
        | "bad_request" => ToolRefusal::InvalidArgs(msg),
        "ambiguous_paired_guild" | "guild_mismatch" | "channel_not_owned" | "no_origin_pin" => {
            ToolRefusal::Forbidden(msg)
        }
        _ => ToolRefusal::TargetUnavailable(msg),
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
            "discord.member.roles.explode",
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

    /// A community with several paired guilds is refused with the typed `ambiguous_paired_guild`
    /// (the drain acks it; the producer never guesses and never retries it as transient).
    #[tokio::test]
    async fn several_paired_guilds_are_a_typed_refusal() {
        let c = caps(FakeRelayQueue::with_drain_result(
            r#"{"ok":false,"code":"ambiguous_paired_guild"}"#,
        ));
        let r = c
            .run_discord_role_tool(&discord_scope(), TOOL_ROLES_ADD, &args(json!({})))
            .await
            .unwrap();
        match r {
            Err(ToolRefusal::Forbidden(m)) => {
                assert!(m.starts_with("ambiguous_paired_guild"), "{m}")
            }
            other => panic!("{other:?}"),
        }
    }

    /// Fail-closed origin rules for channel-less role ops (one platform field decides): a Discord
    /// event with no valid source guild is refused; chat origins (twitch/youtube/...), unknown
    /// and absent platforms can never reach `guild:-`; only the explicit external allowlist can.
    #[tokio::test]
    async fn channel_less_origins_are_allowlisted_and_discord_needs_a_guild() {
        let cases: [(Option<&str>, Option<&str>, Option<&str>); 9] = [
            // (platform, origin_guild_id, expected wire scope; None = refused invalid_args)
            (Some("discord"), None, None),
            (Some("discord"), Some("123"), Some("guild:123")),
            (Some("wix"), None, Some("guild:-")),
            (Some("wix"), Some("123"), Some("guild:-")),
            (Some("twitch"), None, None),
            (Some("youtube"), None, None),
            (Some("kick"), Some("123"), None),
            (Some("Wix"), None, None),
            (None, None, None),
        ];
        for (platform, guild, want) in cases {
            let c = caps(FakeRelayQueue::with_live_drain());
            let mut sc = scope();
            sc.origin_platform = platform.map(str::to_string);
            sc.origin_channel_id = None;
            sc.origin_guild_id = guild.map(str::to_string);
            let r = c
                .run_discord_role_tool(&sc, TOOL_ROLES_ADD, &args(json!({})))
                .await
                .unwrap();
            match want {
                Some(w) => {
                    r.unwrap();
                    assert_eq!(queued_scope(&c), w, "{platform:?} {guild:?}");
                }
                None => {
                    assert!(
                        matches!(r, Err(ToolRefusal::InvalidArgs(_))),
                        "{platform:?} {guild:?}: {r:?}"
                    );
                    assert!(c.relay_queue.envelopes().is_empty(), "{platform:?} queued");
                }
            }
        }
    }

    fn set_args(extra: Value) -> Value {
        let mut v = json!({"user": UUID_A, "role_keys": ["vip", "mod", "vip"]});
        for (k, val) in extra.as_object().unwrap() {
            v[k] = val.clone();
        }
        v
    }

    #[tokio::test]
    async fn roles_set_queues_one_bulk_op_with_sorted_keys_and_the_pinned_scope() {
        let c = caps(FakeRelayQueue::with_live_drain());
        let mut sc = discord_scope();
        sc.origin_channel_id = None;
        sc.origin_guild_id = Some("123456789012345679".to_string());
        let out = c
            .run_discord_role_tool(&sc, TOOL_ROLES_SET, &set_args(json!({})))
            .await
            .unwrap()
            .unwrap();
        assert_eq!(out, json!({"ok": true, "op": "role.set"}));
        let envs = c.relay_queue.envelopes();
        assert_eq!(envs.len(), 1, "one bulk op; the HOST expands the diff");
        let e = &envs[0];
        assert_eq!(e["op"], "role.set");
        assert_eq!(e["text"], "mod,vip");
        assert_eq!(e["user_id"], format!("{{user:{UUID_A}}}"));
        assert_eq!(e["origin_channel"], "guild:123456789012345679");
        assert_eq!(
            (e["tenant_id"].as_i64(), e["community_id"].as_i64()),
            (Some(7), Some(3))
        );
        for never in ["guild_id", "role_id", "channel"] {
            assert!(e.get(never).is_none());
        }
        assert!(e["pin_sig"].as_str().is_some());
    }

    #[tokio::test]
    async fn roles_list_returns_the_drains_roles_payload_and_carries_no_user() {
        let c = caps(FakeRelayQueue::with_drain_result(
            r#"{"ok":true,"data":{"roles":[{"key":"vip","assignable":true}]}}"#,
        ));
        let mut sc = scope();
        sc.origin_platform = Some("wix".to_string());
        sc.origin_channel_id = None;
        let out = c
            .run_discord_role_tool(&sc, TOOL_ROLES_LIST, &json!({}))
            .await
            .unwrap()
            .unwrap();
        assert_eq!(
            out,
            json!({"ok": true, "roles": [{"key": "vip", "assignable": true}]})
        );
        let e = &c.relay_queue.envelopes()[0];
        assert_eq!(
            (e["op"].as_str(), e["origin_channel"].as_str()),
            (Some("role.list"), Some("guild:-"))
        );
        assert!(e.get("user_id").is_none() && e.get("text").is_none());
    }

    #[tokio::test]
    async fn bulk_tool_arguments_and_origins_are_validated_before_anything_is_queued() {
        let c = caps(FakeRelayQueue::with_live_drain());
        for a in [
            set_args(json!({"guild_id": "1"})),
            set_args(json!({"role_id": "1"})),
            set_args(json!({"role_keys": []})),
            set_args(json!({"role_keys": ["222222222222222222"]})),
            set_args(json!({"role_keys": "vip"})),
            set_args(json!({"role_keys": (0..17).map(|i| format!("k{i}")).collect::<Vec<_>>()})),
            set_args(json!({"user": "333333333333333333"})),
            json!([]),
        ] {
            let r = c
                .run_discord_role_tool(&discord_scope(), TOOL_ROLES_SET, &a)
                .await
                .unwrap();
            assert!(matches!(r, Err(ToolRefusal::InvalidArgs(_))), "{a}");
        }
        for a in [json!({"x": 1}), json!([])] {
            let r = c
                .run_discord_role_tool(&discord_scope(), TOOL_ROLES_LIST, &a)
                .await
                .unwrap();
            assert!(matches!(r, Err(ToolRefusal::InvalidArgs(_))), "{a}");
        }
        // Chat origins cannot reach the bulk ops either.
        let mut sc = scope();
        sc.origin_platform = Some("twitch".to_string());
        sc.origin_channel_id = None;
        let r = c
            .run_discord_role_tool(&sc, TOOL_ROLES_SET, &set_args(json!({})))
            .await
            .unwrap();
        assert!(matches!(r, Err(ToolRefusal::InvalidArgs(_))));
        assert!(c.relay_queue.envelopes().is_empty());
    }

    #[tokio::test]
    async fn bulk_drain_refusals_map_to_typed_errors() {
        for (code, want) in [
            ("role_privileged", "invalid"),
            ("role_not_managed", "invalid"),
            ("ambiguous_paired_guild", "forbidden"),
            ("guild_mismatch", "forbidden"),
            ("upstream_down", "unavailable"),
        ] {
            let c = caps(FakeRelayQueue::with_drain_result(&format!(
                r#"{{"ok":false,"code":"{code}"}}"#
            )));
            let r = c
                .run_discord_role_tool(&discord_scope(), TOOL_ROLES_SET, &set_args(json!({})))
                .await
                .unwrap();
            let got = match r {
                Err(ToolRefusal::InvalidArgs(_)) => "invalid",
                Err(ToolRefusal::Forbidden(_)) => "forbidden",
                Err(ToolRefusal::TargetUnavailable(_)) => "unavailable",
                other => panic!("{code}: {other:?}"),
            };
            assert_eq!(got, want, "{code}");
        }
        // No drain: refused before queueing; unanswered drain: not_confirmed (never "done").
        let c = caps(FakeRelayQueue::default());
        let r = c
            .run_discord_role_tool(&discord_scope(), TOOL_ROLES_LIST, &json!({}))
            .await
            .unwrap();
        assert!(matches!(r, Err(ToolRefusal::TargetUnavailable(_))));
        assert!(c.relay_queue.envelopes().is_empty());
    }
}
