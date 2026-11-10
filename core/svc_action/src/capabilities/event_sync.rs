//! Hub-api -> Discord guild scheduled-event producer (event sync, Waddles =
//! source of truth).
//!
//! hub-api owns the calendar but must not hold Discord credentials or the
//! outbound `pin_sig` key. It therefore enqueues a signed *request* on
//! [`EVENT_SYNC_REQUEST_QUEUE`]; this module verifies it, builds the
//! `event.create|update|cancel|delete` outbound envelope (`pin_sig`-signed,
//! community/tenant MAC-bound, `op_id` + `exp_ms` handshake -- exactly as for
//! `role.add`), queues it for svc-ingest's executor-backed Discord drain, waits
//! for the drain's confirmation and posts the outcome (including a created
//! event's Discord id) under [`EVENT_SYNC_RESULT_PREFIX`]`<request_id>` for
//! hub-api to pop.
//!
//! ```text
//!  hub-api --RPUSH signed request--> waddles:event-sync:discord:requests
//!      ^                                   | BRPOP (this consumer)
//!      |                      verify MAC -> replay -> rate -> drain-ready
//!      |                                   v
//!      |                  sign envelope --LPUSH--> discord outbound queue
//!      |                                   |  (svc-ingest drain: pin_sig, replay
//!      |                                   |   guard, executor, connector template)
//!      +--BLPOP result <-- {"ok":..,"id":..} <-- ack key (op_id)
//! ```
//!
//! **Request MAC.** The request reuses [`PinKeys`]' construction under a DEDICATED
//! key ring (`EVENT_SYNC_REQUEST_KEYS`, never the outbound ring) with the
//! domain-separating platform label [`REQUEST_MAC_PLATFORM`]: `op` = the action,
//! `origin_channel` = the guild the producer expects, `message_id` = the Discord
//! event id (`""` for create), `text` = the opaque event JSON string, `op_id` =
//! `request_id`, plus `exp_ms`, `community_id`, `tenant_id`. An unauthenticated
//! writer of the shared Valkey list therefore cannot make svc-action create,
//! edit or delete a guild event, and gets no answer (nothing is posted for an
//! unverified request).
//!
//! **Guild.** The producer never names a destination: the guild is pinned from
//! the community's binding by svc-ingest. `expect_guild_id` is only an assertion
//! the executor compares to that binding and refuses (`guild_mismatch`) on
//! difference. Logs and results carry fixed codes only -- never event text, ids
//! or guild ids.

use std::time::Duration;

use bundle_host_kv::KvBackend;
use serde::{Deserialize, Serialize};

use super::{
    now_epoch_ms, RelayQueue, StageCapabilities, DISCORD_DRAIN_READY_KEY,
    DISCORD_OP_ACK_KEY_PREFIX, DISCORD_OP_ACK_POLL, OUTBOUND_SCHEMA_VERSION,
};
use crate::pin_sig::{PinFields, PinKeys, PinSigError};

/// Valkey list hub-api pushes signed event-sync requests onto.
pub const EVENT_SYNC_REQUEST_QUEUE: &str = "waddles:event-sync:discord:requests";
/// Prefix of the per-request result list (`<prefix><request_id>`) hub-api pops.
pub const EVENT_SYNC_RESULT_PREFIX: &str = "waddles:event-sync:discord:result:";
/// Wire `origin_channel` of an event op that asserts no guild (the host pins it
/// from the community's binding alone).
pub const NO_GUILD_ASSERTION: &str = "-";
/// Prefix of the single-use marker for a verified request id.
const EVENT_SYNC_SEEN_PREFIX: &str = "waddles:event-sync:discord:seen:";
/// Prefix of the per-community fixed-window request counter.
const EVENT_SYNC_RATE_PREFIX: &str = "waddles:event-sync:discord:rate:";

/// Domain-separation label of the request MAC (never a real outbound platform).
pub const REQUEST_MAC_PLATFORM: &str = "event-sync-request";
/// Largest accepted request, bytes.
const MAX_REQUEST_BYTES: usize = 16 * 1024;
/// A request's deadline may be at most this far ahead of now.
const REQUEST_MAX_AHEAD_MS: u64 = 5 * 60_000;
/// Max event-sync requests per community per [`RATE_WINDOW_SECS`] (Discord's
/// scheduled-event write limits are stricter than message limits).
const COMMUNITY_RATE_LIMIT: u64 = 30;
/// Rate window, seconds.
const RATE_WINDOW_SECS: u64 = 60;
/// How long a verified request id stays claimed (>= the max request lifetime).
const SEEN_TTL_SECS: u64 = 600;
/// TTL of a posted result: long enough for hub-api to pop, short enough to
/// never linger.
const RESULT_TTL_SECS: u64 = 120;
/// Blocking-pop timeout, seconds (bounds shutdown latency).
const POP_TIMEOUT_SECS: u64 = 5;
/// Longest accepted `request_id` (it becomes part of Valkey keys).
const MAX_REQUEST_ID_LEN: usize = 64;

/// Discord's field limits (mirrors the connector's checks; refusing early keeps
/// a doomed request off the shared drain).
const MAX_NAME_CHARS: usize = 100;
const MAX_DESCRIPTION_CHARS: usize = 1000;
const MAX_LOCATION_CHARS: usize = 100;

/// The event field object (full set -- never a diff).
#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct EventFields {
    pub name: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
    pub scheduled_start_time: String,
    pub scheduled_end_time: String,
    pub location: String,
}

/// One signed request as hub-api writes it.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    v: u32,
    request_id: String,
    tenant_id: i64,
    community_id: i64,
    /// `create` | `update` | `cancel` | `delete`.
    action: String,
    /// The Discord scheduled-event id (update/cancel/delete); absent for create.
    #[serde(default)]
    discord_event_id: Option<String>,
    /// The guild hub-api expects the community's binding to resolve to.
    expect_guild_id: String,
    /// Opaque event-fields JSON string (create/update); the MAC covers it
    /// byte-for-byte.
    #[serde(default)]
    event_json: Option<String>,
    exp_ms: u64,
    sig: String,
}

/// What became of one request. Serialized (fixed codes only) as the result.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum EventSyncOutcome {
    /// The platform performed the op; `id` is the created event's snowflake.
    Done { id: Option<String> },
    /// Not performed; `code` is a stable machine code.
    Refused(String),
}

impl EventSyncOutcome {
    fn refused(code: &str) -> Self {
        Self::Refused(code.to_string())
    }

    /// The JSON hub-api reads: `{"ok":true[,"id":".."]}` / `{"ok":false,"code":".."}`.
    pub(crate) fn to_json(&self) -> String {
        match self {
            Self::Done { id: None } => serde_json::json!({"ok": true}),
            Self::Done { id: Some(id) } => serde_json::json!({"ok": true, "id": id}),
            Self::Refused(code) => serde_json::json!({"ok": false, "code": code}),
        }
        .to_string()
    }
}

/// `true` for a Discord snowflake.
fn is_snowflake(s: &str) -> bool {
    !s.is_empty() && s.len() <= 20 && s.bytes().all(|b| b.is_ascii_digit())
}

/// `true` for a safe request id (1..=64 ASCII alphanumerics / `-`).
fn is_safe_request_id(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= MAX_REQUEST_ID_LEN
        && s.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
}

/// A stable ack code from the drain, or `failed` for anything unexpected (an
/// arbitrary string in the result key never becomes an arbitrary code).
fn clean_code(raw: &str) -> String {
    let ok = !raw.is_empty()
        && raw.len() <= 48
        && raw
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_');
    if ok {
        raw.to_string()
    } else {
        "failed".to_string()
    }
}

/// Validates the event fields' bounds (the connector re-validates shape).
fn validate_fields(f: &EventFields) -> Result<(), &'static str> {
    let chars = |s: &str| s.chars().count();
    if f.name.trim().is_empty() || chars(&f.name) > MAX_NAME_CHARS {
        return Err("bad_event_name");
    }
    if f.location.trim().is_empty() || chars(&f.location) > MAX_LOCATION_CHARS {
        return Err("bad_event_location");
    }
    if f.description
        .as_deref()
        .is_some_and(|d| chars(d) > MAX_DESCRIPTION_CHARS)
    {
        return Err("bad_event_description");
    }
    if f.scheduled_start_time.is_empty() || f.scheduled_end_time.is_empty() {
        return Err("bad_event_time");
    }
    Ok(())
}

/// The MAC fields of a request (see the module doc).
fn mac_fields(r: &Request) -> PinFields<'_> {
    PinFields {
        platform: REQUEST_MAC_PLATFORM,
        op: &r.action,
        channel: "",
        origin_channel: &r.expect_guild_id,
        message_id: r.discord_event_id.as_deref().unwrap_or(""),
        text: r.event_json.as_deref().unwrap_or(""),
        user_id: "",
        op_id: &r.request_id,
        exp_ms: r.exp_ms,
        community_id: r.community_id,
        tenant_id: r.tenant_id,
    }
}

/// The outbound op spelling for an action.
fn outbound_op(action: &str) -> Option<&'static str> {
    match action {
        "create" => Some("event.create"),
        "update" => Some("event.update"),
        "cancel" => Some("event.cancel"),
        "delete" => Some("event.delete"),
        _ => None,
    }
}

impl<Q: RelayQueue, K: KvBackend> StageCapabilities<Q, K> {
    /// Wires the verify-only key ring (`EVENT_SYNC_REQUEST_KEYS`) hub-api signs
    /// event-sync requests under. Without it every request is dropped unanswered
    /// (hub-api then times out loudly) -- never executed unauthenticated.
    #[must_use]
    pub fn with_event_sync_request_keys(mut self, keys: PinKeys) -> Self {
        self.event_sync_keys = Some(std::sync::Arc::new(keys));
        self
    }

    /// Verifies and executes one raw request. `None` = the request is not
    /// authentic (unparsable, unsigned, bad MAC, no key ring): dropped with a
    /// WARN and NO answer, so an unauthenticated queue writer can neither settle
    /// nor spoof a result. `Some((request_id, outcome))` otherwise.
    pub(crate) async fn handle_event_sync_request(
        &self,
        raw: &str,
    ) -> Option<(String, EventSyncOutcome)> {
        if raw.len() > MAX_REQUEST_BYTES {
            tracing::warn!("event-sync request too large; dropped");
            return None;
        }
        let Ok(req) = serde_json::from_str::<Request>(raw) else {
            tracing::warn!("event-sync request unparsable; dropped");
            return None;
        };
        let Some(keys) = self.event_sync_keys.as_ref() else {
            tracing::error!(
                "EVENT_SYNC_REQUEST_KEYS not configured; event-sync request dropped unanswered"
            );
            return None;
        };
        if !is_safe_request_id(&req.request_id) {
            tracing::warn!("event-sync request id unsafe; dropped");
            return None;
        }
        match keys.verify(&mac_fields(&req), &req.sig) {
            Ok(()) => {}
            Err(PinSigError::LegacyUnscoped) | Err(_) => {
                tracing::warn!("event-sync request signature invalid; dropped");
                return None;
            }
        }
        let outcome = self.execute_event_sync(&req).await;
        match &outcome {
            EventSyncOutcome::Done { .. } => {
                tracing::info!(action = %req.action, "event-sync op confirmed");
            }
            EventSyncOutcome::Refused(code) => {
                tracing::warn!(action = %req.action, code = %code, "event-sync op not performed");
            }
        }
        Some((req.request_id, outcome))
    }

    /// Everything after authentication: shape, freshness, single use, rate,
    /// drain readiness, then queue + await the confirmation.
    async fn execute_event_sync(&self, req: &Request) -> EventSyncOutcome {
        if req.v != 1 {
            return EventSyncOutcome::refused("unsupported_version");
        }
        let now = now_epoch_ms();
        if req.exp_ms < now {
            return EventSyncOutcome::refused("expired");
        }
        if req.exp_ms > now.saturating_add(REQUEST_MAX_AHEAD_MS) {
            return EventSyncOutcome::refused("bad_request");
        }
        let Some(op) = outbound_op(&req.action) else {
            return EventSyncOutcome::refused("bad_request");
        };
        if req.tenant_id < 1 || req.community_id < 1 || !is_snowflake(&req.expect_guild_id) {
            return EventSyncOutcome::refused("bad_request");
        }
        let needs_id = op != "event.create";
        let has_fields = matches!(op, "event.create" | "event.update");
        let event_id_ok = match (needs_id, req.discord_event_id.as_deref()) {
            (true, Some(id)) => is_snowflake(id),
            (false, None) => true,
            _ => false,
        };
        if !event_id_ok || has_fields != req.event_json.is_some() {
            return EventSyncOutcome::refused("bad_request");
        }
        let text = match req.event_json.as_deref() {
            None => None,
            Some(raw) => match serde_json::from_str::<EventFields>(raw) {
                Ok(f) => {
                    if let Err(code) = validate_fields(&f) {
                        return EventSyncOutcome::refused(code);
                    }
                    match serde_json::to_string(&f) {
                        Ok(t) => Some(t),
                        Err(_) => return EventSyncOutcome::refused("bad_request"),
                    }
                }
                Err(_) => return EventSyncOutcome::refused("bad_request"),
            },
        };

        // Single use: the id is claimed once (atomic INCR); a replay is refused.
        let seen_key = format!("{EVENT_SYNC_SEEN_PREFIX}{}", req.request_id);
        match self.relay_queue.incr_window(&seen_key, SEEN_TTL_SECS).await {
            Ok(1) => {}
            Ok(_) => return EventSyncOutcome::refused("replayed"),
            Err(e) => {
                tracing::error!(error = %e, "event-sync replay check unavailable; refusing");
                return EventSyncOutcome::refused("relay_unavailable");
            }
        }
        self.queue_event_op(
            op,
            req.community_id,
            req.tenant_id,
            &req.expect_guild_id,
            req.discord_event_id.as_deref(),
            text,
        )
        .await
    }

    /// Rate-limits, checks the drain, signs and queues one `event.*` envelope
    /// and awaits its confirmation. Shared by the hub-api request consumer and
    /// the `discord.scheduled_event.*` tool path (both authenticated by their
    /// caller). `expect_guild` is a snowflake assertion or [`NO_GUILD_ASSERTION`]
    /// (the tool path: the host pins the guild from the binding alone).
    pub(crate) async fn queue_event_op(
        &self,
        op: &'static str,
        community_id: i64,
        tenant_id: i64,
        expect_guild: &str,
        discord_event_id: Option<&str>,
        text: Option<String>,
    ) -> EventSyncOutcome {
        // Per-community throttle; a limiter that cannot be read fails closed.
        let rate_key = format!("{EVENT_SYNC_RATE_PREFIX}{}", community_id);
        match self
            .relay_queue
            .incr_window(&rate_key, RATE_WINDOW_SECS)
            .await
        {
            Ok(n) if n <= COMMUNITY_RATE_LIMIT => {}
            Ok(_) => return EventSyncOutcome::refused("rate_limited"),
            Err(e) => {
                tracing::error!(error = %e, "event-sync limiter unavailable; refusing");
                return EventSyncOutcome::refused("relay_unavailable");
            }
        }
        match self.relay_queue.key_exists(DISCORD_DRAIN_READY_KEY).await {
            Ok(true) => {}
            Ok(false) => {
                tracing::error!("discord outbound drain not ready; event-sync request refused");
                return EventSyncOutcome::refused("relay_unavailable");
            }
            Err(e) => {
                tracing::error!(error = %e, "could not verify the discord drain; refusing");
                return EventSyncOutcome::refused("relay_unavailable");
            }
        }

        let op_id = uuid::Uuid::new_v4().to_string();
        let ack_timeout = self.discord_ack_timeout;
        let exp_ms = now_epoch_ms()
            .saturating_add(u64::try_from(ack_timeout.as_millis()).unwrap_or(u64::MAX));
        let mut env = serde_json::json!({
            "v": OUTBOUND_SCHEMA_VERSION,
            "op": op,
            "platform": "discord",
            // The guild the producer expects (an assertion, never a destination).
            "origin_channel": expect_guild,
            "community_id": community_id,
            "tenant_id": tenant_id,
            "op_id": op_id,
            "exp_ms": exp_ms,
        });
        if let Some(obj) = env.as_object_mut() {
            if let Some(t) = text {
                obj.insert("text".to_string(), serde_json::Value::String(t));
            }
            if let Some(id) = discord_event_id {
                obj.insert(
                    "message_id".to_string(),
                    serde_json::Value::String(id.to_string()),
                );
            }
        }
        // Signed AFTER the handshake fields so the MAC binds `exp_ms`/`op_id`.
        if self.sign_envelope(&mut env).is_err() {
            return EventSyncOutcome::refused("relay_unavailable");
        }
        if let Err(e) = self
            .relay_queue
            .lpush(&super::outbound_relay_queue_key("discord"), env.to_string())
            .await
        {
            tracing::error!(error = %e, "event-sync envelope could not be queued");
            return EventSyncOutcome::refused("relay_unavailable");
        }
        self.await_event_sync_ack(&op_id, ack_timeout).await
    }

    /// Polls the drain's result key for `op_id` until `timeout`.
    async fn await_event_sync_ack(&self, op_id: &str, timeout: Duration) -> EventSyncOutcome {
        let key = format!("{DISCORD_OP_ACK_KEY_PREFIX}{op_id}");
        let deadline = tokio::time::Instant::now() + timeout;
        loop {
            match self.relay_queue.take(&key).await {
                Ok(Some(raw)) => return parse_event_ack(&raw),
                Ok(None) => {}
                Err(e) => {
                    tracing::error!(error = %e, "could not read the event-sync op result");
                    return EventSyncOutcome::refused("relay_unavailable");
                }
            }
            if tokio::time::Instant::now() >= deadline {
                tracing::error!("event-sync op queued but not confirmed in time");
                return EventSyncOutcome::refused("not_confirmed");
            }
            tokio::time::sleep(DISCORD_OP_ACK_POLL).await;
        }
    }
}

/// The Discord service bundle's app id (the `target` of `tool.invoke`).
#[cfg(test)]
pub(crate) const DISCORD_SERVICE_APP_ID: &str = "waddles.core.service.discord";
/// Tool-name prefix the HOST executes for the Discord service bundle.
pub(crate) const EVENT_TOOL_PREFIX: &str = "discord.scheduled_event.";

/// Maps a tool name to its outbound op, `None` for anything else.
fn tool_op(tool: &str) -> Option<&'static str> {
    match tool.strip_prefix(EVENT_TOOL_PREFIX)? {
        "create" => Some("event.create"),
        "update" => Some("event.update"),
        "cancel" => Some("event.cancel"),
        "delete" => Some("event.delete"),
        _ => None,
    }
}

/// Maps a refused outcome code onto the tool framework's refusal taxonomy.
fn refusal_for(code: &str) -> bundle_tool_call::ToolRefusal {
    use bundle_tool_call::ToolRefusal;
    let msg = format!("discord scheduled event tool refused: {code}");
    match code {
        "bad_request"
        | "bad_event_name"
        | "bad_event_location"
        | "bad_event_description"
        | "bad_event_time"
        | "build_refused" => ToolRefusal::InvalidArgs(msg),
        "rate_limited" | "relay_unavailable" | "not_confirmed" | "unconfirmed" => {
            ToolRefusal::TargetUnavailable(msg)
        }
        _ => ToolRefusal::Forbidden(msg),
    }
}

impl<Q: RelayQueue, K: KvBackend> StageCapabilities<Q, K> {
    /// Executes a `discord.scheduled_event.*` tool call authorized by the generic
    /// `tool.invoke` planner (manifest `requested_tools` + approved `tool.call`
    /// grant). The Discord service bundle owns the tool DEFINITIONS; the HOST runs
    /// them because the guild and credential are binding-pinned host duties. The
    /// community/tenant come from the verified invoke scope; the guild is never an
    /// argument (the schema is `additionalProperties:false`, and a `guild_id`/
    /// `channel_id` is refused here). Returns `None` when the tool is not one of
    /// these (the caller then does the normal nested invoke).
    pub(crate) async fn run_discord_event_tool(
        &self,
        scope: &super::InvokeScope,
        tool: &str,
        args: &serde_json::Value,
    ) -> Option<Result<serde_json::Value, bundle_tool_call::ToolRefusal>> {
        use bundle_tool_call::ToolRefusal;
        let op = tool_op(tool)?;
        let bad = |m: &str| ToolRefusal::InvalidArgs(m.to_string());
        let result = async {
            if scope.tenant_id < 1 || scope.community_id < 1 {
                return Err(ToolRefusal::Forbidden(
                    "discord scheduled event tools need a community-scoped invoke".to_string(),
                ));
            }
            let obj = args.as_object().ok_or_else(|| bad("args must be an object"))?;
            let takes_fields = matches!(op, "event.create" | "event.update");
            let takes_id = op != "event.create";
            let mut fields = serde_json::Map::new();
            let mut event_id: Option<String> = None;
            for (k, v) in obj {
                match k.as_str() {
                    "event_id" if takes_id => {
                        let id = v.as_str().filter(|i| is_snowflake(i));
                        event_id = Some(id.ok_or_else(|| bad("event_id must be a snowflake"))?.to_string());
                    }
                    "name" | "description" | "scheduled_start_time" | "scheduled_end_time"
                    | "location"
                        if takes_fields =>
                    {
                        fields.insert(k.clone(), v.clone());
                    }
                    other => return Err(ToolRefusal::InvalidArgs(format!("argument `{other}` is not accepted"))),
                }
            }
            if takes_id && event_id.is_none() {
                return Err(bad("event_id is required"));
            }
            let text = if takes_fields {
                let f: EventFields = serde_json::from_value(serde_json::Value::Object(fields))
                    .map_err(|_| bad("name, scheduled_start_time, scheduled_end_time and location are required strings"))?;
                validate_fields(&f).map_err(|c| ToolRefusal::InvalidArgs(format!("invalid event: {c}")))?;
                Some(serde_json::to_string(&f).map_err(|_| bad("event not encodable"))?)
            } else {
                None
            };
            let outcome = self
                .queue_event_op(
                    op,
                    i64::from(scope.community_id),
                    i64::from(scope.tenant_id),
                    NO_GUILD_ASSERTION,
                    event_id.as_deref(),
                    text,
                )
                .await;
            match outcome {
                EventSyncOutcome::Done { id } => {
                    // The tool result carries the platform id the calling app bundle must keep
                    // (`id`, plus the legacy `event_id` spelling) and what happened: a create
                    // returns the NEW snowflake, the others echo the event they acted on.
                    let id = id.or(event_id);
                    let status = match op {
                        "event.create" => "created",
                        "event.update" => "updated",
                        "event.cancel" => "cancelled",
                        _ => "deleted",
                    };
                    Ok(serde_json::json!({
                        "ok": true, "id": id, "event_id": id, "status": status
                    }))
                }
                EventSyncOutcome::Refused(code) => Err(refusal_for(&code)),
            }
        }
        .await;
        Some(result)
    }
}

/// Parses the drain's result JSON into an outcome. A malformed result is an
/// unconfirmed op, never a success.
fn parse_event_ack(raw: &str) -> EventSyncOutcome {
    let Ok(v) = serde_json::from_str::<serde_json::Value>(raw) else {
        return EventSyncOutcome::refused("unconfirmed");
    };
    match v.get("ok").and_then(serde_json::Value::as_bool) {
        Some(true) => {
            let id = v
                .get("id")
                .and_then(serde_json::Value::as_str)
                .filter(|id| is_snowflake(id))
                .map(str::to_string);
            EventSyncOutcome::Done { id }
        }
        Some(false) => EventSyncOutcome::Refused(clean_code(
            v.get("code")
                .and_then(serde_json::Value::as_str)
                .unwrap_or("failed"),
        )),
        None => EventSyncOutcome::refused("unconfirmed"),
    }
}

/// Posts `outcome` for `request_id` (`RPUSH` + `EXPIRE`, atomic) on `conn` --
/// a connection that is NOT the one blocked in `BRPOP`.
async fn post_result(
    conn: &mut redis::aio::MultiplexedConnection,
    request_id: &str,
    outcome: &EventSyncOutcome,
) -> Result<(), redis::RedisError> {
    let key = format!("{EVENT_SYNC_RESULT_PREFIX}{request_id}");
    redis::pipe()
        .atomic()
        .cmd("RPUSH")
        .arg(&key)
        .arg(outcome.to_json())
        .ignore()
        .cmd("EXPIRE")
        .arg(&key)
        .arg(RESULT_TTL_SECS)
        .ignore()
        .query_async::<()>(conn)
        .await
}

/// Runs the request consumer until `shutdown` resolves: `BRPOP`s
/// [`EVENT_SYNC_REQUEST_QUEUE`] on `pop_conn`, handles each request, and posts
/// the outcome on `post_conn`. Valkey errors back off (capped) and retry; a
/// failed result post is logged loudly (hub-api then times out, fail closed).
pub async fn run_event_sync_consumer<Q, K, S>(
    caps: std::sync::Arc<StageCapabilities<Q, K>>,
    mut pop_conn: redis::aio::MultiplexedConnection,
    mut post_conn: redis::aio::MultiplexedConnection,
    shutdown: S,
) where
    Q: RelayQueue + 'static,
    K: KvBackend + 'static,
    S: std::future::Future<Output = ()>,
{
    tokio::pin!(shutdown);
    let mut failures: u32 = 0;
    loop {
        let mut pop = redis::cmd("BRPOP");
        pop.arg(EVENT_SYNC_REQUEST_QUEUE).arg(POP_TIMEOUT_SECS);
        let popped = tokio::select! {
            () = &mut shutdown => return,
            r = pop.query_async::<Option<(String, String)>>(&mut pop_conn) => r,
        };
        match popped {
            Ok(Some((_key, raw))) => {
                failures = 0;
                if let Some((request_id, outcome)) = caps.handle_event_sync_request(&raw).await {
                    if let Err(e) = post_result(&mut post_conn, &request_id, &outcome).await {
                        tracing::error!(error = %e, "failed to post the event-sync result; hub-api will time out");
                    }
                }
            }
            Ok(None) => failures = 0,
            Err(e) => {
                failures = failures.saturating_add(1);
                let delay = Duration::from_millis(500u64.saturating_mul(1 << failures.min(6)));
                tracing::error!(error = %e, retry_in_ms = u64::try_from(delay.as_millis()).unwrap_or(u64::MAX), "event-sync queue read failed");
                tokio::select! {
                    () = &mut shutdown => return,
                    () = tokio::time::sleep(delay) => {}
                }
            }
        }
    }
}

#[cfg(test)]
#[path = "event_sync_tests.rs"]
mod event_sync_tests;
