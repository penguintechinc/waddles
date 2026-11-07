//! Host-mediated YouTube live-chat tools (`youtube.chat.*`).
//!
//! The YouTube SERVICE bundle (`waddles.core.service.youtube`) DECLARES these tools
//! (`exported_tools`: names, JSON arg schemas, `access: write`, `risk: dangerous`); this module is
//! where the HOST serves them. An authorized `tool.invoke` is NOT a nested guest call (the guest
//! has no mutating egress): it is translated into the generic confirmed platform producer
//! (`handle_platform_queued_op`), so the live chat is the verified origin of the triggering
//! YouTube event (never an argument), the community's OAuth credential and the detokenized target
//! user stay host-side in svc-ingest, and the op is only reported done after the drain confirms.
//!
//! Authorization stacks three layers: the generic planner (the caller's manifest
//! `requested_tools` + an admin-approved `tool.call` grant + the write key -- every tool here is
//! `access: write`, `risk: dangerous`, so the grant IS the moderation permission for that app),
//! the origin pin (the op only acts in the chat of a triggering YouTube event), and the drain's own
//! ownership + quota duties. (A direct `relay.push` of the same ops is separately gated on the
//! caller's `chat.delete:youtube` / `moderation.youtube` permission in `handle_relay`.) A per-community daily op cap gives the caller the explicit
//! `quota_exhausted` refusal (the drain additionally enforces the Data API unit budget).

use bundle_host_kv::KvBackend;
use bundle_tool_call::ToolRefusal;
use serde_json::{json, Value};

use super::{InvokeScope, RelayOp, RelayQueue, StageCapabilities};

/// `app_id` of the YouTube service bundle that declares these tools.
pub(crate) const YOUTUBE_SERVICE_APP_ID: &str = "waddles.core.service.youtube";

/// Tool name: delete a chat message.
pub(crate) const TOOL_DELETE_MESSAGE: &str = "youtube.chat.delete_message";
/// Tool name: ban a viewer (permanent, or temporary with `duration_seconds`).
pub(crate) const TOOL_BAN: &str = "youtube.chat.ban";
/// Tool name: lift a ban by its ban id.
pub(crate) const TOOL_UNBAN: &str = "youtube.chat.unban";
/// Tool name: make a viewer a chat moderator.
pub(crate) const TOOL_ADD_MODERATOR: &str = "youtube.chat.add_moderator";
/// Tool name: remove a chat moderator by its moderator id.
pub(crate) const TOOL_REMOVE_MODERATOR: &str = "youtube.chat.remove_moderator";

/// Most moderation ops one community may run per UTC-agnostic 24h window through these tools.
/// Conservative: the cheapest op costs 20 Data API units and the default project quota is 10 000.
pub(crate) const DAILY_OP_CAP: u64 = 150;
/// Window of [`DAILY_OP_CAP`] in seconds.
const DAILY_WINDOW_SECS: u64 = 24 * 60 * 60;
/// Longest temporary ban the tool accepts (matches the connector's cap).
const MAX_BAN_SECONDS: u64 = 14 * 24 * 60 * 60;

/// `true` for a canonical lowercase UUID (the tokenized-user spelling).
fn is_canonical_uuid(s: &str) -> bool {
    let b = s.as_bytes();
    b.len() == 36
        && b.iter().enumerate().all(|(i, c)| match i {
            8 | 13 | 18 | 23 => *c == b'-',
            _ => c.is_ascii_digit() || (b'a'..=b'f').contains(c),
        })
}

/// `^[A-Za-z0-9_.-]{1,128}$` -- a YouTube message / ban / moderator resource id.
fn is_resource_id(s: &str) -> bool {
    (1..=128).contains(&s.len())
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-' | b'.'))
}

/// The op a tool name maps to; `None` for any other tool (the caller continues its dispatch).
fn tool_op(tool: &str) -> Option<&'static str> {
    match tool {
        TOOL_DELETE_MESSAGE => Some("chat.delete"),
        TOOL_BAN => Some("mod.ban"),
        TOOL_UNBAN => Some("mod.unban"),
        TOOL_ADD_MODERATOR => Some("mod.add"),
        TOOL_REMOVE_MODERATOR => Some("mod.remove"),
        _ => None,
    }
}

/// Validates the tool args and builds the relay message the pinned producer consumes. Exactly the
/// keys the tool declares are accepted; ANY other key (`live_chat_id`, `channel_id`, `tenant_id`,
/// ...) is REFUSED, never ignored -- the chat and credential are the host's to pin.
///
/// # Errors
/// [`ToolRefusal::InvalidArgs`] for a non-object, an extra/missing key, a non-UUID user, a bad id
/// or an out-of-range duration.
pub(crate) fn relay_message(op: &str, args: &Value) -> Result<Value, ToolRefusal> {
    let bad = |m: &str| ToolRefusal::InvalidArgs(m.to_string());
    let obj = args
        .as_object()
        .ok_or_else(|| bad("args must be an object"))?;
    let allowed: &[&str] = match op {
        "chat.delete" => &["message_id"],
        "mod.unban" => &["ban_id"],
        "mod.remove" => &["moderator_id"],
        "mod.add" => &["user"],
        "mod.ban" => &["user", "duration_seconds"],
        _ => return Err(bad("unknown youtube tool")),
    };
    if let Some(extra) = obj.keys().find(|k| !allowed.contains(&k.as_str())) {
        return Err(ToolRefusal::InvalidArgs(format!(
            "unexpected argument {extra:?}: the live chat and credential are host-pinned"
        )));
    }
    let id_arg = |key: &str| -> Result<String, ToolRefusal> {
        obj.get(key)
            .and_then(Value::as_str)
            .filter(|s| is_resource_id(s))
            .map(str::to_string)
            .ok_or_else(|| ToolRefusal::InvalidArgs(format!("`{key}` must be a YouTube id")))
    };
    let user_arg = || -> Result<String, ToolRefusal> {
        obj.get("user")
            .and_then(Value::as_str)
            .filter(|u| is_canonical_uuid(u))
            .map(|u| format!("{{user:{u}}}"))
            .ok_or_else(|| bad("`user` must be a lowercase canonical UUID"))
    };
    let mut msg = json!({ "op": op });
    let m = msg
        .as_object_mut()
        .ok_or_else(|| bad("internal: message is not an object"))?;
    match op {
        "chat.delete" => {
            m.insert("message_id".into(), Value::String(id_arg("message_id")?));
        }
        "mod.unban" => {
            m.insert("message_id".into(), Value::String(id_arg("ban_id")?));
        }
        "mod.remove" => {
            m.insert("message_id".into(), Value::String(id_arg("moderator_id")?));
        }
        "mod.add" => {
            m.insert("user_id".into(), Value::String(user_arg()?));
        }
        "mod.ban" => {
            m.insert("user_id".into(), Value::String(user_arg()?));
            match obj.get("duration_seconds") {
                None | Some(Value::Null) => {}
                Some(v) => {
                    let secs = v
                        .as_u64()
                        .filter(|s| (1..=MAX_BAN_SECONDS).contains(s))
                        .ok_or_else(|| {
                            ToolRefusal::InvalidArgs(format!(
                                "`duration_seconds` must be an integer in 1..={MAX_BAN_SECONDS}"
                            ))
                        })?;
                    m.insert("text".into(), Value::String(secs.to_string()));
                }
            }
        }
        _ => return Err(bad("unknown youtube tool")),
    }
    Ok(msg)
}

/// Maps a producer refusal onto the tool framework's taxonomy (typed, never swallowed).
fn refusal_for(e: &penguin_bundle_host::wire::HostResultError, op: &str) -> ToolRefusal {
    let msg = format!("youtube {op} refused: {}", e.code);
    match e.code.as_str() {
        "invalid_args" => ToolRefusal::InvalidArgs(e.message.clone()),
        "quota_exhausted" | "rate_limited" => ToolRefusal::TargetUnavailable(msg),
        "target_not_in_community" | "unsupported_op" | "permission_denied" | "not_granted" => {
            ToolRefusal::Forbidden(msg)
        }
        _ => ToolRefusal::TargetUnavailable(msg),
    }
}

impl<Q: RelayQueue, K: KvBackend> StageCapabilities<Q, K> {
    /// Per-community daily op cap with the explicit `quota_exhausted` refusal.
    async fn youtube_daily_cap(&self, scope: &InvokeScope) -> Result<(), ToolRefusal> {
        let key = format!(
            "waddles:ratelimit:youtube.quota.ops:{}:{}",
            scope.tenant_id, scope.community_id
        );
        match self.relay_queue.incr_window(&key, DAILY_WINDOW_SECS).await {
            Ok(n) if n <= DAILY_OP_CAP => Ok(()),
            Ok(_) => Err(ToolRefusal::TargetUnavailable(format!(
                "quota_exhausted: this community used its {DAILY_OP_CAP} YouTube moderation ops \
                 for the day (Data API quota); retry after the window resets"
            ))),
            Err(e) => Err(ToolRefusal::TargetUnavailable(format!(
                "youtube quota counter unavailable: {e}"
            ))),
        }
    }

    /// Executes a `youtube.chat.*` tool call authorized by the generic `tool.invoke` planner.
    /// Returns `None` for any other tool (the caller continues its dispatch).
    pub(crate) async fn run_youtube_tool(
        &self,
        scope: &InvokeScope,
        tool: &str,
        args: &Value,
    ) -> Option<Result<Value, ToolRefusal>> {
        let op_name = tool_op(tool)?;
        let result = async {
            let message = relay_message(op_name, args)?;
            if scope.tenant_id < 1 || scope.community_id < 1 {
                return Err(ToolRefusal::Forbidden(
                    "youtube chat tools need a community-scoped invoke".to_string(),
                ));
            }
            let op = RelayOp::parse(op_name)
                .ok_or_else(|| ToolRefusal::InvalidArgs("unknown youtube op".to_string()))?;
            self.youtube_daily_cap(scope).await?;
            match self
                .handle_platform_queued_op(scope, "youtube", op, &message)
                .await
            {
                Ok(confirmed) => {
                    let mut out = json!({"ok": true, "op": op_name});
                    if let (Some(d), Some(o)) = (confirmed.get("data"), out.as_object_mut()) {
                        // A created ban / moderator returns its resource id for later removal.
                        let parsed = d
                            .as_str()
                            .and_then(|s| serde_json::from_str::<Value>(s).ok());
                        let body = parsed.as_ref().unwrap_or(d);
                        if let Some(id) = body
                            .get("id")
                            .and_then(Value::as_str)
                            .filter(|i| is_resource_id(i))
                            .map(str::to_string)
                        {
                            o.insert("id".into(), Value::String(id));
                        }
                    }
                    Ok(out)
                }
                Err(e) => Err(refusal_for(&e, op_name)),
            }
        }
        .await;
        Some(result)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const USER: &str = "123e4567-e89b-12d3-a456-426614174000";

    #[test]
    fn every_tool_maps_to_a_registered_youtube_op() {
        for (tool, op) in [
            (TOOL_DELETE_MESSAGE, "chat.delete"),
            (TOOL_BAN, "mod.ban"),
            (TOOL_UNBAN, "mod.unban"),
            (TOOL_ADD_MODERATOR, "mod.add"),
            (TOOL_REMOVE_MODERATOR, "mod.remove"),
        ] {
            assert_eq!(tool_op(tool), Some(op));
            assert!(bundle_capability_gate::platform_ops::registered("youtube", op).is_some());
        }
        assert_eq!(tool_op("youtube.chat.send"), None);
        assert_eq!(tool_op("discord.member.roles.add"), None);
    }

    #[test]
    fn messages_carry_only_validated_fields() {
        let m = relay_message("chat.delete", &json!({"message_id": "LCC.abc-1"})).unwrap();
        assert_eq!(m, json!({"op": "chat.delete", "message_id": "LCC.abc-1"}));
        let m = relay_message("mod.ban", &json!({"user": USER})).unwrap();
        assert_eq!(m["user_id"], format!("{{user:{USER}}}"));
        assert!(m.get("text").is_none());
        let m = relay_message("mod.ban", &json!({"user": USER, "duration_seconds": 600})).unwrap();
        assert_eq!(m["text"], "600");
        let m = relay_message("mod.unban", &json!({"ban_id": "b1"})).unwrap();
        assert_eq!(m["message_id"], "b1");
        let m = relay_message("mod.remove", &json!({"moderator_id": "m1"})).unwrap();
        assert_eq!(m["message_id"], "m1");
        let m = relay_message("mod.add", &json!({"user": USER})).unwrap();
        assert_eq!(m["op"], "mod.add");
    }

    #[test]
    fn host_pinned_fields_and_bad_values_are_refused_never_ignored() {
        for (op, args) in [
            (
                "chat.delete",
                json!({"message_id": "m", "live_chat_id": "x"}),
            ),
            ("mod.ban", json!({"user": USER, "channel_id": "UCx"})),
            ("mod.ban", json!({"user": "UCrawchannel"})),
            ("mod.ban", json!({"user": USER, "duration_seconds": 0})),
            (
                "mod.ban",
                json!({"user": USER, "duration_seconds": 1209601}),
            ),
            ("mod.ban", json!({"user": USER, "duration_seconds": "5"})),
            ("mod.add", json!({"user": USER.to_uppercase()})),
            ("chat.delete", json!({"message_id": "a&b"})),
            ("chat.delete", json!({})),
            ("mod.unban", json!({"message_id": "b1"})),
            ("chat.delete", json!("nope")),
        ] {
            assert!(
                matches!(relay_message(op, &args), Err(ToolRefusal::InvalidArgs(_))),
                "{op} {args}"
            );
        }
    }

    #[test]
    fn producer_refusals_map_to_typed_tool_refusals() {
        let e = |c: &str| penguin_bundle_host::wire::HostResultError {
            code: c.to_string(),
            message: "m".to_string(),
        };
        assert!(matches!(
            refusal_for(&e("invalid_args"), "mod.ban"),
            ToolRefusal::InvalidArgs(_)
        ));
        assert!(matches!(
            refusal_for(&e("unsupported_op"), "mod.ban"),
            ToolRefusal::Forbidden(_)
        ));
        assert!(matches!(
            refusal_for(&e("relay_unconfirmed"), "mod.ban"),
            ToolRefusal::TargetUnavailable(_)
        ));
    }
}

#[cfg(test)]
mod queue_tests {
    use super::super::tests::{caps, scope, FakeRelayQueue};
    use super::*;

    const USER: &str = "123e4567-e89b-12d3-a456-426614174000";

    fn yt_scope() -> InvokeScope {
        let mut s = scope();
        s.origin_platform = Some("youtube".to_string());
        s.origin_channel_id = Some("Cg0KC-lc_1".to_string());
        s
    }

    #[tokio::test]
    async fn ban_queues_a_signed_envelope_pinned_to_the_origin_chat_and_returns_the_ban_id() {
        let c = caps(FakeRelayQueue::with_drain_result(
            r#"{"ok":true,"data":"{\"id\":\"banNew1\"}"}"#,
        ));
        let out = c
            .run_youtube_tool(
                &yt_scope(),
                TOOL_BAN,
                &json!({"user": USER, "duration_seconds": 600}),
            )
            .await
            .unwrap()
            .unwrap();
        assert_eq!(out, json!({"ok": true, "op": "mod.ban", "id": "banNew1"}));
        let queued = c.relay_queue.envelopes();
        let e = &queued[0];
        assert_eq!(e["op"], "mod.ban");
        assert_eq!(e["platform"], "youtube");
        assert_eq!(e["origin_channel"], "Cg0KC-lc_1");
        assert_eq!(e["user_id"], format!("{{user:{USER}}}"));
        assert_eq!(e["text"], "600");
        assert_eq!(
            (e["community_id"].as_i64(), e["tenant_id"].as_i64()),
            (Some(3), Some(7))
        );
        assert!(e["pin_sig"].as_str().is_some());
    }

    #[tokio::test]
    async fn a_call_without_a_youtube_origin_chat_is_refused_and_queues_nothing() {
        for origin in [None, Some(("twitch", "#chan"))] {
            let c = caps(FakeRelayQueue::with_live_drain());
            let mut s = scope();
            if let Some((p, ch)) = origin {
                s.origin_platform = Some(p.to_string());
                s.origin_channel_id = Some(ch.to_string());
            }
            let r = c
                .run_youtube_tool(&s, TOOL_DELETE_MESSAGE, &json!({"message_id": "LCC.m1"}))
                .await
                .unwrap();
            assert!(matches!(r, Err(ToolRefusal::InvalidArgs(_))), "{r:?}");
            assert!(c.relay_queue.envelopes().is_empty());
        }
    }

    #[tokio::test]
    async fn an_unscoped_invoke_and_a_bad_argument_queue_nothing() {
        let c = caps(FakeRelayQueue::with_live_drain());
        let mut s = yt_scope();
        s.community_id = 0;
        let r = c
            .run_youtube_tool(&s, TOOL_ADD_MODERATOR, &json!({"user": USER}))
            .await
            .unwrap();
        assert!(matches!(r, Err(ToolRefusal::Forbidden(_))), "{r:?}");
        let r = c
            .run_youtube_tool(&yt_scope(), TOOL_ADD_MODERATOR, &json!({"user": "UCraw"}))
            .await
            .unwrap();
        assert!(matches!(r, Err(ToolRefusal::InvalidArgs(_))), "{r:?}");
        assert!(c
            .run_youtube_tool(&yt_scope(), "youtube.chat.send", &json!({}))
            .await
            .is_none());
        assert!(c.relay_queue.envelopes().is_empty());
    }

    #[tokio::test]
    async fn the_daily_op_cap_is_the_explicit_quota_exhausted_refusal() {
        let c = caps(FakeRelayQueue::with_live_drain());
        let mut ok = 0;
        let mut last = None;
        // The per-op producer throttle (20/min per app) is a separate gate; vary the app per
        // call so only the daily cap can stop us.
        for i in 0..(DAILY_OP_CAP + 1) {
            let mut s = yt_scope();
            s.app_id = format!("waddles.bot.app{i}");
            let r = c
                .run_youtube_tool(&s, TOOL_DELETE_MESSAGE, &json!({"message_id": "LCC.m1"}))
                .await
                .unwrap();
            match r {
                Ok(_) => ok += 1,
                Err(e) => last = Some(e),
            }
        }
        assert_eq!(ok, DAILY_OP_CAP, "denominator: ops allowed before the cap");
        match last {
            Some(ToolRefusal::TargetUnavailable(m)) => assert!(m.starts_with("quota_exhausted")),
            other => panic!("expected quota_exhausted, got {other:?}"),
        }
    }
}
