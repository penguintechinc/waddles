//! Host side of hub-api's generic bundle-admin gateway (`docs/BUNDLE_ADMIN_GATEWAY.md`).
//!
//! hub-api enqueues a SIGNED request on `waddles:bundle-admin:requests`; this consumer verifies
//! it, authorizes it exactly like a guest `tool.invoke` (P1 matrix + `tool.call` gate, with the
//! signed `caller_app_id` as the caller), runs the target's `tool-provider.call` and answers on
//! `waddles:bundle-admin:result:<request_id>` (TTL'd).
//!
//! * **Authentication.** `sig` = `kid:hex(HMAC-SHA256)` under the `BUNDLE_ADMIN_REQUEST_KEYS`
//!   ring (label `waddles.bundle-admin.v1`, 4-byte-big-endian length-prefixed fields in the exact
//!   order of hub-api's `bundle_tool_transport.sign_call`). The MAC covers every routing and
//!   identity field including `actor_scope`, so a writer with only Valkey access can neither
//!   invoke a tool nor claim a role. An unverifiable request is dropped WITHOUT a reply (its
//!   `request_id` is untrusted; replying would let a forger poison a pending result).
//! * **Replay.** Expired (`exp_ms` in the past) and implausibly far-future requests are refused;
//!   a `request_id` is honoured once (`SET NX`, kept until it can no longer be valid).
//! * **Actor.** The signed `actor_scope` is the host-verified role: it expands to the fixed
//!   cumulative community scopes and gates `required_scope` tools. Tenant/community come from the
//!   signed request, never from `args`. Bundles see the actor and scopes, never a token.
//! * **Fail loud.** Every refusal is answered `{"ok":false,"code":...}`; nothing falls back to a
//!   direct call. Logs carry ids, tool name and a fixed code only -- never args or results.

use std::collections::HashMap;
use std::future::Future;
use std::sync::Arc;
use std::time::Duration;

use bundle_tool_call::{ActorContext, ToolRefusal};
use hmac::{Hmac, Mac};
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::Sha256;

/// Valkey list hub-api pushes signed requests onto.
pub const REQUEST_QUEUE: &str = "waddles:bundle-admin:requests";
/// Per-request reply list prefix.
pub const RESULT_PREFIX: &str = "waddles:bundle-admin:result:";
const SEEN_PREFIX: &str = "waddles:bundle-admin:seen:";
const MAC_PLATFORM: &str = "bundle-admin-request";
const MAC_LABEL: &[u8] = b"waddles.bundle-admin.v1";
/// How long a result stays readable.
pub const RESULT_TTL_S: u64 = 120;
/// A request's longest legitimate validity (hub-api signs `now + 120s`) plus clock skew.
const MAX_EXP_AHEAD_MS: i64 = 150_000;
const MAX_REQUEST_BYTES: usize = 128 * 1024;

type HmacSha256 = Hmac<Sha256>;

/// Why a request was refused (fixed code; never request content).
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum Reject {
    /// Not a well-formed v1 request.
    #[error("malformed request")]
    Malformed,
    /// Unknown `kid` or MAC mismatch.
    #[error("signature rejected")]
    BadSignature,
    /// `exp_ms` is in the past.
    #[error("request expired")]
    Expired,
    /// `exp_ms` is further ahead than a legitimate request can be.
    #[error("request expiry implausible")]
    ExpiryTooFar,
    /// The `request_id` was already honoured.
    #[error("request replayed")]
    Replay,
    /// `actor_scope` is not a community scope the gateway can sign.
    #[error("unsupported actor scope")]
    BadActorScope,
    /// A numeric id is outside the executor's scope range.
    #[error("scope out of range")]
    BadScope,
}

impl Reject {
    /// The stable reply code.
    #[must_use]
    pub fn code(self) -> &'static str {
        match self {
            Self::Malformed => "malformed_request",
            Self::BadSignature => "bad_signature",
            Self::Expired => "request_expired",
            Self::ExpiryTooFar => "request_expiry_implausible",
            Self::Replay => "request_replayed",
            Self::BadActorScope => "bad_actor_scope",
            Self::BadScope => "bad_scope",
        }
    }
}

/// `BUNDLE_ADMIN_REQUEST_KEYS`: `kid:hexkey[,kid:hexkey...]`.
#[derive(Clone)]
pub struct KeyRing {
    keys: HashMap<String, Vec<u8>>,
}

/// The key ring configuration was unusable.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("invalid BUNDLE_ADMIN_REQUEST_KEYS: {0}")]
pub struct KeyRingError(pub &'static str);

impl KeyRing {
    /// Parses `kid:hexkey,...`; every key must be >= 16 bytes of hex, kids unique and non-empty.
    ///
    /// # Errors
    /// [`KeyRingError`] naming the first problem (never echoing key material).
    pub fn parse(raw: &str) -> Result<Self, KeyRingError> {
        let mut keys = HashMap::new();
        for entry in raw.split(',').map(str::trim).filter(|e| !e.is_empty()) {
            let (kid, hexkey) = entry
                .split_once(':')
                .ok_or(KeyRingError("entry is not kid:hexkey"))?;
            if kid.is_empty() || kid.len() > 64 || kid.contains(':') {
                return Err(KeyRingError("bad kid"));
            }
            let key = hex::decode(hexkey).map_err(|_| KeyRingError("key is not hex"))?;
            if key.len() < 16 {
                return Err(KeyRingError("key shorter than 16 bytes"));
            }
            if keys.insert(kid.to_owned(), key).is_some() {
                return Err(KeyRingError("duplicate kid"));
            }
        }
        if keys.is_empty() {
            return Err(KeyRingError("no keys"));
        }
        Ok(Self { keys })
    }

    /// Number of keys (rotation overlap shows as >1).
    #[must_use]
    pub fn len(&self) -> usize {
        self.keys.len()
    }

    /// Always `false` for a parsed ring.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.keys.is_empty()
    }
}

/// One signed request, as hub-api enqueues it.
#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
pub struct AdminRequest {
    v: u8,
    /// Correlation id; also the write tool's idempotency key.
    pub request_id: String,
    /// Authoritative tenant.
    pub tenant_id: i64,
    /// Authoritative community.
    pub community_id: i64,
    /// hub-api user id of the acting admin.
    pub actor_user_id: i64,
    /// hub-api-verified community scope of the actor.
    pub actor_scope: String,
    /// The bundle whose admin UI made the call.
    pub caller_app_id: String,
    /// The bundle exporting the tool.
    pub target_app_id: String,
    /// Exported tool name.
    pub tool: String,
    /// Canonical JSON text of the tool args (the exact bytes that were signed).
    pub args_json: String,
    exp_ms: i64,
    sig: String,
}

fn mac_for(key: &[u8], r: &AdminRequest) -> Result<HmacSha256, Reject> {
    let mut mac = HmacSha256::new_from_slice(key).map_err(|_| Reject::BadSignature)?;
    mac.update(MAC_LABEL);
    let ids = [
        r.community_id.to_string(),
        r.tenant_id.to_string(),
        r.actor_user_id.to_string(),
    ];
    let exp = r.exp_ms.to_string();
    let parts: [&str; 11] = [
        MAC_PLATFORM,
        &r.target_app_id,
        &r.tool,
        &r.caller_app_id,
        &r.args_json,
        &r.request_id,
        &exp,
        &ids[0],
        &ids[1],
        &ids[2],
        &r.actor_scope,
    ];
    for part in parts {
        // A field over 4 GiB cannot occur (the whole request is capped far below).
        let len = u32::try_from(part.len()).map_err(|_| Reject::Malformed)?;
        mac.update(&len.to_be_bytes());
        mac.update(part.as_bytes());
    }
    Ok(mac)
}

/// Computes the `kid:hexmac` signature hub-api would produce (used by tests and tooling).
///
/// # Errors
/// [`Reject::Malformed`] if a field is absurdly large.
pub fn sign(key: &[u8], kid: &str, r: &AdminRequest) -> Result<String, Reject> {
    Ok(format!(
        "{kid}:{}",
        hex::encode(mac_for(key, r)?.finalize().into_bytes())
    ))
}

impl AdminRequest {
    /// Test/tooling constructor that signs under `kid`.
    ///
    /// # Errors
    /// [`Reject::Malformed`] if a field is absurdly large.
    #[allow(clippy::too_many_arguments)]
    pub fn signed(
        key: &[u8],
        kid: &str,
        request_id: &str,
        scope: (i64, i64),
        actor: (i64, &str),
        apps: (&str, &str),
        tool: &str,
        args_json: &str,
        exp_ms: i64,
    ) -> Result<Self, Reject> {
        let mut r = Self {
            v: 1,
            request_id: request_id.to_owned(),
            tenant_id: scope.0,
            community_id: scope.1,
            actor_user_id: actor.0,
            actor_scope: actor.1.to_owned(),
            caller_app_id: apps.0.to_owned(),
            target_app_id: apps.1.to_owned(),
            tool: tool.to_owned(),
            args_json: args_json.to_owned(),
            exp_ms,
            sig: String::new(),
        };
        r.sig = sign(key, kid, &r)?;
        Ok(r)
    }

    /// The wire JSON hub-api would enqueue.
    #[must_use]
    pub fn to_wire(&self) -> String {
        json!({
            "v": self.v, "request_id": self.request_id, "tenant_id": self.tenant_id,
            "community_id": self.community_id, "actor_user_id": self.actor_user_id,
            "actor_scope": self.actor_scope, "caller_app_id": self.caller_app_id,
            "target_app_id": self.target_app_id, "tool": self.tool,
            "args_json": self.args_json, "exp_ms": self.exp_ms, "sig": self.sig,
        })
        .to_string()
    }

    /// The numeric scope narrowed to the executor's `i32` range.
    ///
    /// # Errors
    /// [`Reject::BadScope`] for a non-positive tenant, negative community or an id over `i32`.
    pub fn scope(&self) -> Result<(i32, i32), Reject> {
        let t = i32::try_from(self.tenant_id).map_err(|_| Reject::BadScope)?;
        let c = i32::try_from(self.community_id).map_err(|_| Reject::BadScope)?;
        if t < 1 || c < 0 {
            return Err(Reject::BadScope);
        }
        Ok((t, c))
    }

    /// The host-verified actor: the signed scope expanded to its cumulative community scopes.
    ///
    /// # Errors
    /// [`Reject::BadActorScope`] for anything but a known community scope.
    pub fn actor(&self) -> Result<ActorContext, Reject> {
        let role = match self.actor_scope.as_str() {
            "community:moderate" => "moderator",
            "community:admin" => "community-admin",
            "community:owner" => "community-owner",
            _ => return Err(Reject::BadActorScope),
        };
        Ok(ActorContext::for_role(self.actor_user_id.to_string(), role))
    }

    /// The `tool.invoke` argument object the shared planner parses. The `request_id` doubles as
    /// the write idempotency key, so a retried delivery of one request cannot double-apply.
    ///
    /// # Errors
    /// [`Reject::Malformed`] when `args_json` is not a JSON object.
    pub fn tool_args(&self) -> Result<Value, Reject> {
        let args: Value = serde_json::from_str(&self.args_json).map_err(|_| Reject::Malformed)?;
        if !args.is_object() {
            return Err(Reject::Malformed);
        }
        Ok(json!({
            "target": self.target_app_id, "tool": self.tool, "args": args,
            "idempotency_key": self.request_id,
        }))
    }
}

/// Parses and authenticates one raw request: shape, signature, expiry window. Replay and scope
/// checks are separate (they need state / run after authentication).
///
/// # Errors
/// The first [`Reject`] in the order malformed, signature, expiry.
pub fn verify(raw: &str, ring: &KeyRing, now_ms: i64) -> Result<AdminRequest, Reject> {
    if raw.len() > MAX_REQUEST_BYTES {
        return Err(Reject::Malformed);
    }
    let r: AdminRequest = serde_json::from_str(raw).map_err(|_| Reject::Malformed)?;
    if r.v != 1 || r.request_id.is_empty() || r.request_id.len() > 64 {
        return Err(Reject::Malformed);
    }
    if !r
        .request_id
        .bytes()
        .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
    {
        return Err(Reject::Malformed);
    }
    let (kid, hexmac) = r.sig.split_once(':').ok_or(Reject::BadSignature)?;
    let key = ring.keys.get(kid).ok_or(Reject::BadSignature)?;
    let given = hex::decode(hexmac).map_err(|_| Reject::BadSignature)?;
    // Constant-time compare inside `verify_slice`.
    mac_for(key, &r)?
        .verify_slice(&given)
        .map_err(|_| Reject::BadSignature)?;
    if r.exp_ms <= now_ms {
        return Err(Reject::Expired);
    }
    if r.exp_ms - now_ms > MAX_EXP_AHEAD_MS {
        return Err(Reject::ExpiryTooFar);
    }
    Ok(r)
}

/// Maps the nested call's `{ok}` / `{tool_error}` envelope or typed refusal onto hub-api's reply.
#[must_use]
pub fn reply_for(outcome: Result<Value, ToolRefusal>) -> Value {
    match outcome {
        Ok(env) => {
            if let Some(inner) = env.get("ok") {
                let result = if inner.is_object() {
                    inner.clone()
                } else {
                    json!({ "value": inner })
                };
                json!({"ok": true, "result": result})
            } else {
                let code = env
                    .get("tool_error")
                    .and_then(|e| match e {
                        Value::String(s) => Some(s.as_str()),
                        // Externally tagged variant (`{"InvalidArgs": ".."}`) or `{code: ".."}`.
                        Value::Object(o) if o.len() == 1 => o
                            .get("code")
                            .and_then(Value::as_str)
                            .or_else(|| o.keys().next().map(String::as_str)),
                        Value::Object(o) => o.get("code").and_then(Value::as_str),
                        _ => None,
                    })
                    .map(snake_case)
                    .filter(|c| is_code(c))
                    .unwrap_or_else(|| "tool_error".to_string());
                json!({"ok": false, "code": code, "tool_error": true})
            }
        }
        Err(refusal) => json!({"ok": false, "code": refusal.code()}),
    }
}

/// `InvalidArgs` -> `invalid_args` (the SDK renders tool errors as variant names).
fn snake_case(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 4);
    for (i, ch) in s.chars().enumerate() {
        if ch.is_ascii_uppercase() {
            if i > 0 {
                out.push('_');
            }
            out.push(ch.to_ascii_lowercase());
        } else {
            out.push(ch);
        }
    }
    out
}

fn is_code(s: &str) -> bool {
    let mut b = s.bytes();
    matches!(b.next(), Some(c) if c.is_ascii_lowercase())
        && s.len() <= 64
        && b.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == b'_' || c == b'.')
}

/// Replay guard + reply channel + clock (Valkey in production, in-memory in unit tests).
pub trait AdminBackend: Send + Sync {
    /// `true` iff `request_id` had not been claimed before; the claim outlives `ttl`.
    fn claim(
        &self,
        request_id: &str,
        ttl: Duration,
    ) -> impl Future<Output = Result<bool, BackendError>> + Send;
    /// Publishes `reply` for `request_id` with the result TTL.
    fn reply(
        &self,
        request_id: &str,
        reply: &Value,
    ) -> impl Future<Output = Result<(), BackendError>> + Send;
}

/// Backend failure (type only).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("bundle-admin backend unavailable ({0})")]
pub struct BackendError(pub String);

/// Runs one authorized request on the executor.
pub trait AdminExecutor: Send + Sync {
    /// Runs `args` (the `tool.invoke` object) as `caller_app_id` in `scope` for `actor`.
    fn execute(
        &self,
        req: &AdminRequest,
        scope: (i32, i32),
        args: &Value,
        actor: ActorContext,
    ) -> impl Future<Output = Result<Value, ToolRefusal>> + Send;
}

/// What one dequeued request did.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Handled {
    /// Unauthenticated: dropped silently (no reply).
    Dropped(Reject),
    /// Authenticated but refused before execution; answered with the code.
    Refused(Reject),
    /// Executed (the reply may still be `ok:false` for a refusal or tool error).
    Executed { ok: bool },
}

/// Answers an authenticated-but-refused request with its fixed code.
async fn refuse<B: AdminBackend>(backend: &B, req: &AdminRequest, reason: Reject) -> Handled {
    let reply = json!({"ok": false, "code": reason.code()});
    if backend.reply(&req.request_id, &reply).await.is_err() {
        tracing::error!(code = "reply_failed", "bundle-admin refusal not delivered");
    }
    tracing::warn!(
        app_id = %req.caller_app_id, tool = %req.tool, code = reason.code(),
        "bundle-admin request refused"
    );
    Handled::Refused(reason)
}

/// Authenticates, replay-guards, authorizes and runs one raw request, then replies.
pub async fn handle_one<B: AdminBackend, E: AdminExecutor>(
    raw: &str,
    ring: &KeyRing,
    backend: &B,
    executor: &E,
    now_ms: i64,
) -> Handled {
    let req = match verify(raw, ring, now_ms) {
        Ok(r) => r,
        Err(reason) => {
            // Untrusted request_id: never reply.
            tracing::warn!(code = reason.code(), "bundle-admin request dropped");
            return Handled::Dropped(reason);
        }
    };
    let ttl = Duration::from_millis(u64::try_from(req.exp_ms - now_ms).unwrap_or(0))
        + Duration::from_secs(RESULT_TTL_S);
    match backend.claim(&req.request_id, ttl).await {
        Ok(true) => {}
        Ok(false) => return refuse(backend, &req, Reject::Replay).await,
        Err(_) => {
            // Fail closed: without the replay guard nothing runs; hub-api times out loudly.
            tracing::error!(
                code = "replay_guard_unavailable",
                "bundle-admin request not run"
            );
            return Handled::Dropped(Reject::Replay);
        }
    }
    let (scope, actor, args) = match (req.scope(), req.actor(), req.tool_args()) {
        (Ok(s), Ok(a), Ok(args)) => (s, a, args),
        (Err(e), _, _) | (_, Err(e), _) | (_, _, Err(e)) => return refuse(backend, &req, e).await,
    };
    let outcome = executor.execute(&req, scope, &args, actor).await;
    tracing::info!(
        app_id = %req.caller_app_id, target = %req.target_app_id, tool = %req.tool,
        community_id = req.community_id, ok = outcome.is_ok(),
        "bundle-admin request served"
    );
    let reply = reply_for(outcome);
    let ok = reply["ok"] == json!(true);
    if backend.reply(&req.request_id, &reply).await.is_err() {
        tracing::error!(code = "reply_failed", "bundle-admin reply not delivered");
    }
    Handled::Executed { ok }
}

/// Valkey [`AdminBackend`].
pub struct ValkeyBackend {
    conn: redis::aio::MultiplexedConnection,
}

impl ValkeyBackend {
    /// Wraps a (non-blocking) connection.
    #[must_use]
    pub fn new(conn: redis::aio::MultiplexedConnection) -> Self {
        Self { conn }
    }
}

fn be(e: &redis::RedisError) -> BackendError {
    // Kind only: a redis error Display can echo the URL and password.
    BackendError(format!("{:?}", e.kind()))
}

impl AdminBackend for ValkeyBackend {
    async fn claim(&self, request_id: &str, ttl: Duration) -> Result<bool, BackendError> {
        let mut c = self.conn.clone();
        let ms = u64::try_from(ttl.as_millis()).unwrap_or(u64::MAX).max(1);
        let res: Option<String> = redis::cmd("SET")
            .arg(format!("{SEEN_PREFIX}{request_id}"))
            .arg("1")
            .arg("NX")
            .arg("PX")
            .arg(ms)
            .query_async(&mut c)
            .await
            .map_err(|e| be(&e))?;
        Ok(res.is_some())
    }

    async fn reply(&self, request_id: &str, reply: &Value) -> Result<(), BackendError> {
        let mut c = self.conn.clone();
        let key = format!("{RESULT_PREFIX}{request_id}");
        let mut pipe = redis::pipe();
        pipe.atomic()
            .cmd("LPUSH")
            .arg(&key)
            .arg(reply.to_string())
            .ignore()
            .cmd("EXPIRE")
            .arg(&key)
            .arg(RESULT_TTL_S)
            .ignore();
        pipe.query_async::<()>(&mut c).await.map_err(|e| be(&e))
    }
}

/// The `(tenant slug, community name)` the event path keys on for numeric `scope`, as resolved by
/// the changelog consumer from the database.
///
/// # Errors
/// [`ToolRefusal::TargetUnavailable`] when this stage has not resolved that scope (fail closed).
pub fn display_scope(scope: (i32, i32)) -> Result<(String, Option<String>), ToolRefusal> {
    crate::scope_names::lookup(scope).ok_or_else(|| {
        ToolRefusal::TargetUnavailable("the scope is not resolved on this stage".into())
    })
}

/// Production executor: builds the CALLER bundle's capability set from the stage's own wirings
/// (the same [`crate::spine::build_stage_capabilities`] the event path uses) and runs the
/// shared admin planner + nested invoke.
pub struct StageAdminExecutor {
    deps: Arc<crate::source_supervisor::SupervisorDeps>,
    spine: penguin_spine::SpineClient,
}

impl StageAdminExecutor {
    /// An executor over the stage's shared wirings.
    #[must_use]
    pub fn new(
        deps: Arc<crate::source_supervisor::SupervisorDeps>,
        spine: penguin_spine::SpineClient,
    ) -> Self {
        Self { deps, spine }
    }
}

impl AdminExecutor for StageAdminExecutor {
    async fn execute(
        &self,
        req: &AdminRequest,
        scope: (i32, i32),
        args: &Value,
        actor: ActorContext,
    ) -> Result<Value, ToolRefusal> {
        let pd = self
            .deps
            .process_deps(&req.caller_app_id, scope.0, scope.1, self.spine.clone());
        // The caller must be active in this scope: its version row is the grants' key.
        let version = pd
            .app_version_snapshot
            .tool_entry_in_scope(scope.0, scope.1, &req.caller_app_id)
            .map(|e| e.version_id)
            .ok_or_else(|| {
                ToolRefusal::TargetUnavailable("the bundle is not active in this scope".into())
            })?;
        // The SAME tenant slug / community name the event path keys its kv namespace on (they
        // come from the DB resolution of this exact numeric scope), so admin calls and event
        // handling read and write one namespace and a numeric scope can only ever address its
        // own tenant's names. Unknown scope: refused, never guessed.
        let (tenant, community) = display_scope(scope)?;
        let invocation = Arc::new(crate::identity::InvocationIdentity::for_verified_admin(
            actor.clone(),
        ));
        let caps = crate::spine::build_stage_capabilities(
            &pd,
            tenant,
            community,
            version,
            Some(scope),
            None,
            Some(invocation),
        );
        caps.invoke_admin_tool(args, actor).await
    }
}

/// Consumes [`REQUEST_QUEUE`] until `shutdown` resolves. Each request runs on its own task,
/// bounded by `max_in_flight`; a dead Valkey backs off and retries, never exits.
pub async fn run_consumer<B, E>(
    client: redis::Client,
    ring: Arc<KeyRing>,
    backend: Arc<B>,
    executor: Arc<E>,
    max_in_flight: usize,
    shutdown: impl Future<Output = ()> + Send,
) where
    B: AdminBackend + 'static,
    E: AdminExecutor + 'static,
{
    let permits = Arc::new(tokio::sync::Semaphore::new(max_in_flight.max(1)));
    tokio::pin!(shutdown);
    loop {
        // Own connection: BRPOP blocks it, and a multiplexed one is shared.
        let cfg =
            redis::AsyncConnectionConfig::new().set_response_timeout(Some(Duration::from_secs(10)));
        let mut conn = match client
            .get_multiplexed_async_connection_with_config(&cfg)
            .await
        {
            Ok(c) => c,
            Err(e) => {
                tracing::error!(code = ?e.kind(), "bundle-admin consumer cannot reach valkey");
                tokio::select! {
                    () = tokio::time::sleep(Duration::from_secs(2)) => continue,
                    () = &mut shutdown => return,
                }
            }
        };
        loop {
            let mut brpop = redis::cmd("BRPOP");
            brpop.arg(REQUEST_QUEUE).arg(2);
            let popped: Result<Option<(String, String)>, _> = tokio::select! {
                r = brpop.query_async(&mut conn) => r,
                () = &mut shutdown => return,
            };
            match popped {
                Ok(Some((_, raw))) => {
                    let Ok(permit) = Arc::clone(&permits).acquire_owned().await else {
                        return;
                    };
                    let (ring, backend, executor) = (
                        Arc::clone(&ring),
                        Arc::clone(&backend),
                        Arc::clone(&executor),
                    );
                    tokio::spawn(async move {
                        let now = chrono::Utc::now().timestamp_millis();
                        handle_one(&raw, &ring, &*backend, &*executor, now).await;
                        drop(permit);
                    });
                }
                Ok(None) => {}
                Err(e) => {
                    tracing::error!(code = ?e.kind(), "bundle-admin BRPOP failed; reconnecting");
                    break;
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use std::collections::HashSet;
    use std::sync::Mutex;

    const KEY: &[u8] = &[7u8; 32];
    const NOW: i64 = 1_800_000_000_000;
    const APP: &str = "waddles.core.service.reputation";

    fn ring() -> KeyRing {
        KeyRing::parse(&format!("k1:{}", hex::encode(KEY))).unwrap()
    }

    fn req(id: &str, exp: i64, scope: &str) -> AdminRequest {
        AdminRequest::signed(
            KEY,
            "k1",
            id,
            (7, 3),
            (42, scope),
            (APP, APP),
            "override",
            r#"{"amount":5}"#,
            exp,
        )
        .unwrap()
    }

    #[derive(Default)]
    struct Mem {
        seen: Mutex<HashSet<String>>,
        replies: Mutex<Vec<(String, Value)>>,
        down: bool,
    }
    impl AdminBackend for Mem {
        async fn claim(&self, id: &str, _t: Duration) -> Result<bool, BackendError> {
            if self.down {
                return Err(BackendError("down".into()));
            }
            Ok(self.seen.lock().unwrap().insert(id.into()))
        }
        async fn reply(&self, id: &str, r: &Value) -> Result<(), BackendError> {
            self.replies.lock().unwrap().push((id.into(), r.clone()));
            Ok(())
        }
    }

    type Call = (String, (i32, i32), Value, ActorContext);
    #[derive(Default)]
    struct Exec(Mutex<Vec<Call>>);
    impl AdminExecutor for Exec {
        async fn execute(
            &self,
            r: &AdminRequest,
            scope: (i32, i32),
            args: &Value,
            actor: ActorContext,
        ) -> Result<Value, ToolRefusal> {
            self.0
                .lock()
                .unwrap()
                .push((r.caller_app_id.clone(), scope, args.clone(), actor));
            Ok(json!({"ok": {"applied": true}}))
        }
    }

    #[test]
    fn the_mac_matches_hub_apis_python_sign_call() {
        // Vector produced by hub_api `bundle_tool_transport.sign_call` with key 0x07*32:
        // ToolCall(tenant 7, community 3, actor 42, "community:admin", caller=target=APP,
        // tool "override", args '{"amount":5}'), request_id "r-1", exp_ms 1800000120000.
        let r = req("r-1", 1_800_000_120_000, "community:admin");
        assert_eq!(
            r.sig, "k1:13eba2d368d58a6ddbcc47ec5f8ff5cfa91e38c7b697548f365a4b7460bb34ec",
            "update this vector from hub-api's sign_call if the field order ever changes"
        );
    }

    #[test]
    fn verify_accepts_a_valid_request_and_binds_every_field() {
        let r = req("r-1", NOW + 60_000, "community:admin");
        let wire = r.to_wire();
        assert_eq!(verify(&wire, &ring(), NOW).unwrap(), r);
        // Tamper each covered field: the MAC must fail.
        for (field, val) in [
            ("tenant_id", json!(8)),
            ("community_id", json!(4)),
            ("actor_user_id", json!(43)),
            ("actor_scope", json!("community:owner")),
            ("caller_app_id", json!("waddles.core.service.other")),
            ("target_app_id", json!("waddles.core.service.other")),
            ("tool", json!("leaderboard")),
            ("args_json", json!(r#"{"amount":500}"#)),
            ("request_id", json!("r-2")),
            ("exp_ms", json!(NOW + 61_000)),
        ] {
            let mut v: Value = serde_json::from_str(&wire).unwrap();
            v[field] = val;
            assert_eq!(
                verify(&v.to_string(), &ring(), NOW).unwrap_err(),
                Reject::BadSignature,
                "{field} must be covered by the MAC"
            );
        }
    }

    #[test]
    fn verify_refuses_unknown_kid_garbage_expired_and_far_future() {
        let ok = req("r-1", NOW + 60_000, "community:admin");
        let other = KeyRing::parse(&format!("k9:{}", hex::encode(KEY))).unwrap();
        assert_eq!(
            verify(&ok.to_wire(), &other, NOW).unwrap_err(),
            Reject::BadSignature
        );
        assert_eq!(
            verify("not json", &ring(), NOW).unwrap_err(),
            Reject::Malformed
        );
        assert_eq!(
            verify(
                &req("r-1", NOW - 1, "community:admin").to_wire(),
                &ring(),
                NOW
            )
            .unwrap_err(),
            Reject::Expired
        );
        assert_eq!(
            verify(
                &req("r-1", NOW + 999_999, "community:admin").to_wire(),
                &ring(),
                NOW
            )
            .unwrap_err(),
            Reject::ExpiryTooFar
        );
        // Wrong key material under a known kid.
        let bad = AdminRequest::signed(
            &[9u8; 32],
            "k1",
            "r-1",
            (7, 3),
            (42, "community:admin"),
            (APP, APP),
            "override",
            "{}",
            NOW + 60_000,
        )
        .unwrap();
        assert_eq!(
            verify(&bad.to_wire(), &ring(), NOW).unwrap_err(),
            Reject::BadSignature
        );
    }

    #[test]
    fn key_ring_rotation_and_validation() {
        let two = KeyRing::parse(&format!(
            "a:{},b:{}",
            hex::encode(KEY),
            hex::encode([1u8; 16])
        ))
        .unwrap();
        assert_eq!(two.len(), 2);
        for bad in [
            "",
            "a",
            "a:zz",
            "a:00",
            "a:00112233445566778899aabbccddeeff,a:00112233445566778899aabbccddeeff",
            ":00112233445566778899aabbccddeeff",
        ] {
            assert!(KeyRing::parse(bad).is_err(), "{bad}");
        }
    }

    #[tokio::test]
    async fn happy_path_runs_as_caller_with_verified_scopes_and_replies() {
        let (b, e) = (Mem::default(), Exec::default());
        let r = req("r-1", NOW + 60_000, "community:admin");
        let out = handle_one(&r.to_wire(), &ring(), &b, &e, NOW).await;
        assert_eq!(out, Handled::Executed { ok: true });
        let calls = e.0.lock().unwrap();
        let (caller, scope, args, actor) = &calls[0];
        assert_eq!((caller.as_str(), *scope), (APP, (7, 3)));
        assert_eq!(args["idempotency_key"], "r-1");
        assert_eq!(args["args"], json!({"amount": 5}));
        assert!(actor.has_scope("community:admin") && actor.has_scope("community:moderate"));
        assert!(!actor.has_scope("community:owner"));
        let replies = b.replies.lock().unwrap();
        assert_eq!(replies[0].0, "r-1");
        assert_eq!(
            replies[0].1,
            json!({"ok": true, "result": {"applied": true}})
        );
    }

    #[tokio::test]
    async fn moderator_scope_does_not_satisfy_admin() {
        let a = req("r-1", NOW + 60_000, "community:moderate")
            .actor()
            .unwrap();
        assert!(a.has_scope("community:moderate") && !a.has_scope("community:admin"));
    }

    #[tokio::test]
    async fn a_replayed_request_id_runs_once() {
        let (b, e) = (Mem::default(), Exec::default());
        let wire = req("r-1", NOW + 60_000, "community:admin").to_wire();
        assert_eq!(
            handle_one(&wire, &ring(), &b, &e, NOW).await,
            Handled::Executed { ok: true }
        );
        assert_eq!(
            handle_one(&wire, &ring(), &b, &e, NOW).await,
            Handled::Refused(Reject::Replay)
        );
        assert_eq!(e.0.lock().unwrap().len(), 1);
        assert_eq!(b.replies.lock().unwrap()[1].1["code"], "request_replayed");
    }

    #[tokio::test]
    async fn unauthenticated_requests_are_dropped_with_no_reply_and_no_execution() {
        let (b, e) = (Mem::default(), Exec::default());
        let mut v: Value =
            serde_json::from_str(&req("r-1", NOW + 60_000, "community:admin").to_wire()).unwrap();
        v["actor_scope"] = json!("community:owner");
        let out = handle_one(&v.to_string(), &ring(), &b, &e, NOW).await;
        assert_eq!(out, Handled::Dropped(Reject::BadSignature));
        assert!(e.0.lock().unwrap().is_empty() && b.replies.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn a_signed_but_unsupported_scope_or_range_is_refused_loudly() {
        let (b, e) = (Mem::default(), Exec::default());
        let r = req("r-1", NOW + 60_000, "platform:admin");
        assert_eq!(
            handle_one(&r.to_wire(), &ring(), &b, &e, NOW).await,
            Handled::Refused(Reject::BadActorScope)
        );
        let r = AdminRequest::signed(
            KEY,
            "k1",
            "r-2",
            (0, 3),
            (1, "community:admin"),
            (APP, APP),
            "override",
            "{}",
            NOW + 60_000,
        )
        .unwrap();
        assert_eq!(
            handle_one(&r.to_wire(), &ring(), &b, &e, NOW).await,
            Handled::Refused(Reject::BadScope)
        );
        let r = AdminRequest::signed(
            KEY,
            "k1",
            "r-3",
            (7, 3),
            (1, "community:admin"),
            (APP, APP),
            "override",
            "[1]",
            NOW + 60_000,
        )
        .unwrap();
        assert_eq!(
            handle_one(&r.to_wire(), &ring(), &b, &e, NOW).await,
            Handled::Refused(Reject::Malformed)
        );
        assert!(e.0.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn a_down_replay_guard_fails_closed() {
        let (b, e) = (
            Mem {
                down: true,
                ..Mem::default()
            },
            Exec::default(),
        );
        let wire = req("r-1", NOW + 60_000, "community:admin").to_wire();
        assert_eq!(
            handle_one(&wire, &ring(), &b, &e, NOW).await,
            Handled::Dropped(Reject::Replay)
        );
        assert!(e.0.lock().unwrap().is_empty());
    }

    #[test]
    fn replies_map_envelopes_and_refusals() {
        assert_eq!(
            reply_for(Ok(json!({"ok": {"a": 1}}))),
            json!({"ok": true, "result": {"a": 1}})
        );
        assert_eq!(
            reply_for(Ok(json!({"ok": 5}))),
            json!({"ok": true, "result": {"value": 5}})
        );
        assert_eq!(
            reply_for(Ok(json!({"tool_error": {"code": "bad_input"}}))),
            json!({"ok": false, "code": "bad_input", "tool_error": true})
        );
        assert_eq!(
            reply_for(Ok(json!({"tool_error": "Not A Code!"}))),
            json!({"ok": false, "code": "tool_error", "tool_error": true})
        );
        assert_eq!(
            reply_for(Ok(json!({"tool_error": "InvalidArgs"}))),
            json!({"ok": false, "code": "invalid_args", "tool_error": true})
        );
        assert_eq!(
            reply_for(Err(ToolRefusal::Forbidden("x".into()))),
            json!({"ok": false, "code": "forbidden"})
        );
    }

    /// A scope this stage never resolved is refused fail-closed, never guessed.
    #[test]
    fn display_scope_refuses_an_unresolved_scope() {
        let err = display_scope((2_000_000_001, 2_000_000_002)).unwrap_err();
        assert!(matches!(err, ToolRefusal::TargetUnavailable(_)));
    }

    /// Backend errors carry only the redis error kind, never URL or password text.
    #[test]
    fn backend_error_exposes_only_the_redis_error_kind() {
        let e = redis::RedisError::from((
            redis::ErrorKind::Io,
            "boom",
            "redis://:hunter2@host:6379".to_string(),
        ));
        let b = be(&e);
        assert_eq!(b.0, format!("{:?}", redis::ErrorKind::Io));
        assert!(!b.0.contains("hunter2"));
    }

    /// With Valkey unreachable the consumer backs off and exits promptly on shutdown.
    #[tokio::test]
    async fn run_consumer_exits_on_shutdown_when_valkey_is_unreachable() {
        let client = redis::Client::open("redis://127.0.0.1:1/").unwrap();
        let done = tokio::time::timeout(
            Duration::from_secs(20),
            run_consumer(
                client,
                Arc::new(ring()),
                Arc::new(Mem::default()),
                Arc::new(Exec::default()),
                2,
                tokio::time::sleep(Duration::from_millis(300)),
            ),
        )
        .await;
        assert!(done.is_ok(), "shutdown must end the consumer loop");
    }
}
