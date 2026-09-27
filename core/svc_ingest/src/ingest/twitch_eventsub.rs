//! Twitch EventSub webhook receiver: verification, replay/dedup guarding,
//! and notification normalization for `POST /eventsub/twitch/webhook`
//! (`crate::http::eventsub`, spec §4.1 of
//! `docs/superpowers/specs/2026-09-28-connections-credentials-design.md`).
//!
//! Ports `core/svc_ingest/eventsub.py`'s already-correct logic (real
//! HMAC-SHA256 signature verification, byte-identical to the legacy
//! `trigger/receiver/twitch_module/services/eventsub_handler.py`'s own
//! `_verify_signature`: `sha256=` + hex HMAC of `message_id + timestamp +
//! body`, and the `webhook_callback_verification` challenge handshake) into
//! Rust, and closes the gaps that module's own docstring called out rather
//! than porting them forward:
//!
//! | Gap in `eventsub.py` | Fix here |
//! |---|---|
//! | No message-id dedup (explicitly not ported) | [`ReplayGuard`] -- Valkey `SET NX PX` |
//! | No replay-window check | [`timestamp_within_replay_window`] -- reject timestamps older than [`REPLAY_WINDOW`] |
//! | `revocation` only logs + acks | [`RevocationSink`] -- emits a control-plane event instead of processing inline |
//! | Single-tenant `TWITCH_EVENTSUB_SECRET` env var | [`SubscriptionSecretResolver`] trait -- [`EnvSecretResolver`] is the alpha/test-only impl; a credential-broker impl (per-connection secret keyed by subscription/broadcaster) lands in a later increment, behind the same trait |
//!
//! This is the fast path the design doc calls for: no inline processing
//! beyond signature verify + dedup + normalize + one `XADD` -- p99 stays
//! well under Twitch's delivery deadline. `webhook_callback_verification`
//! and `revocation` never touch the spine at all.

use std::collections::HashMap;
use std::time::Duration;

use hmac::{Hmac, Mac};
use penguin_spine::{KeyRing, Scope};
use sha2::Sha256;

use crate::config::Secret;
use crate::publish::{deterministic_workstream_id, publish_event, EventAppender};
use crate::telemetry::IngestMetrics;

type HmacSha256 = Hmac<Sha256>;

/// `Twitch-Eventsub-Message-Type` header name.
pub const HEADER_MESSAGE_TYPE: &str = "Twitch-Eventsub-Message-Type";
/// `Twitch-Eventsub-Message-Signature` header name.
pub const HEADER_MESSAGE_SIGNATURE: &str = "Twitch-Eventsub-Message-Signature";
/// `Twitch-Eventsub-Message-Timestamp` header name.
pub const HEADER_MESSAGE_TIMESTAMP: &str = "Twitch-Eventsub-Message-Timestamp";
/// `Twitch-Eventsub-Message-Id` header name.
pub const HEADER_MESSAGE_ID: &str = "Twitch-Eventsub-Message-Id";

/// Reject any `Twitch-Eventsub-Message-Timestamp` older than this.
pub const REPLAY_WINDOW: Duration = Duration::from_secs(600);
/// A signed message timestamp is also rejected if it claims to be more than
/// this far in the future -- defends against a forged-but-correctly-signed
/// (impossible without the secret, but defense in depth) or clock-skewed
/// delivery being treated as fresh forever.
const FUTURE_SKEW_TOLERANCE_SECS: i64 = 120;
/// Message-id dedup TTL -- covers Twitch's own redelivery window (§3.2 of
/// the design doc).
pub const DEDUP_TTL: Duration = Duration::from_secs(900);
/// Body size cap, checked before any HMAC computation (cheapest reject
/// first) -- Twitch EventSub notification payloads are small JSON; nothing
/// legitimate approaches this.
pub const MAX_EVENTSUB_BODY_BYTES: usize = 64 * 1024;

/// EventSub subscription types this receiver normalizes -- byte-identical
/// set to the legacy `eventsub.py::DEFAULT_SUBSCRIPTION_TYPES`.
pub const KNOWN_EVENT_TYPES: &[&str] = &[
    "channel.follow",
    "channel.subscribe",
    "channel.subscription.gift",
    "channel.cheer",
    "channel.raid",
    "stream.online",
    "stream.offline",
];

/// Valkey key prefix for the message-id dedup guard.
const DEDUP_KEY_PREFIX: &str = "waddles:eventsub:dedup:twitch:";
/// Valkey list key the control-plane reconciler drains revocation events
/// from -- this receiver only ever `LPUSH`es, never processes a revocation
/// inline (design doc §3.2/§8 increment 4).
pub const REVOCATION_QUEUE_KEY: &str = "waddles:control:twitch:eventsub-revocations";

/// The subset of an inbound EventSub delivery's `subscription` object needed
/// to resolve which secret verifies it.
#[derive(Debug, Clone, Copy, Default)]
pub struct SubscriptionContext<'a> {
    pub subscription_id: &'a str,
    pub conduit_id: Option<&'a str>,
    pub broadcaster_user_id: Option<&'a str>,
}

/// Errors a [`SubscriptionSecretResolver`] can return.
#[derive(Debug, thiserror::Error)]
pub enum SecretResolverError {
    /// No secret is registered for this subscription/broadcaster.
    #[error("no eventsub secret configured for this subscription")]
    NotFound,
}

/// Resolves the HMAC secret that verifies one Twitch EventSub subscription's
/// deliveries. Looked up by subscription id (and, once the credential
/// broker lands, conduit/broadcaster) -- never a single global secret in the
/// production impl, so one compromised/rotated secret never affects every
/// tenant's subscriptions at once.
///
/// `#[allow(async_fn_in_trait)]`: this trait must be `pub` (it appears in
/// [`handle_webhook`]'s public signature), so the crate-level "you can
/// suppress this if the trait is only used in your own code" escape hatch
/// applies literally -- `svc-ingest` is a deployed service binary, not a
/// published library other crates implement this trait against (same
/// justification as `crate::publish::EventAppender`'s own doc comment).
#[allow(async_fn_in_trait)]
pub trait SubscriptionSecretResolver: Send + Sync {
    /// Resolves the secret for `ctx`, or `Err(SecretResolverError::NotFound)`
    /// if nothing is registered.
    async fn resolve_secret(
        &self,
        ctx: &SubscriptionContext<'_>,
    ) -> Result<String, SecretResolverError>;
}

/// **Alpha/test-only** [`SubscriptionSecretResolver`]: a single fallback
/// secret from `TWITCH_EVENTSUB_SECRET` (mirrors the legacy Python module's
/// single-tenant env var), plus an optional in-memory per-subscription
/// override map for tests. The credential-broker-backed implementation
/// (per-connection secret resolved from `connection_credentials` via
/// hub-api, design doc §2) lands behind this same trait in a later
/// increment -- this impl is never meant to serve a real multi-tenant
/// deployment.
#[derive(Clone, Default)]
pub struct EnvSecretResolver {
    default_secret: Option<Secret>,
    per_subscription: HashMap<String, Secret>,
}

impl EnvSecretResolver {
    /// Builds a resolver whose fallback secret is `TWITCH_EVENTSUB_SECRET`,
    /// with no per-subscription overrides.
    #[must_use]
    pub fn from_env(default_secret: Option<Secret>) -> Self {
        Self {
            default_secret,
            per_subscription: HashMap::new(),
        }
    }

    /// Registers a per-subscription secret override, taking precedence over
    /// the fallback -- test-only helper (also usable for a small, static
    /// alpha deployment with a handful of manually-configured subscriptions).
    #[must_use]
    pub fn with_subscription_secret(
        mut self,
        subscription_id: impl Into<String>,
        secret: impl Into<String>,
    ) -> Self {
        self.per_subscription
            .insert(subscription_id.into(), Secret::new(secret.into()));
        self
    }
}

impl SubscriptionSecretResolver for EnvSecretResolver {
    async fn resolve_secret(
        &self,
        ctx: &SubscriptionContext<'_>,
    ) -> Result<String, SecretResolverError> {
        if let Some(secret) = self.per_subscription.get(ctx.subscription_id) {
            return Ok(secret.expose().to_string());
        }
        self.default_secret
            .as_ref()
            .map(|s| s.expose().to_string())
            .ok_or(SecretResolverError::NotFound)
    }
}

/// Errors a [`ReplayGuard`] can return.
#[derive(Debug, thiserror::Error)]
#[error("replay guard error: {0}")]
pub struct ReplayGuardError(pub String);

/// Message-id dedup guard: an atomic "have I seen this id before" check.
/// `#[allow(async_fn_in_trait)]`: see [`SubscriptionSecretResolver`]'s doc.
#[allow(async_fn_in_trait)]
pub trait ReplayGuard: Send + Sync {
    /// Atomically marks `message_id` as seen. Returns `Ok(true)` the first
    /// time a given id is seen (safe to process), `Ok(false)` on every
    /// subsequent call within the TTL window (a duplicate -- must not
    /// re-process).
    async fn check_and_mark(&self, message_id: &str) -> Result<bool, ReplayGuardError>;
}

/// Production [`ReplayGuard`]: Valkey `SET NX PX` on a per-message-id key.
/// Wraps a [`redis::aio::MultiplexedConnection`], cloned per call (cheap --
/// multiplexed connections are designed for concurrent shared use, the same
/// pattern `crate::outbound`'s drain loop and `penguin_spine::SpineClient`
/// rely on internally).
#[derive(Clone)]
pub struct RedisReplayGuard {
    conn: redis::aio::MultiplexedConnection,
    ttl_ms: usize,
}

impl RedisReplayGuard {
    /// Builds a guard over an already-connected Valkey connection, with the
    /// given dedup TTL.
    #[must_use]
    pub fn new(conn: redis::aio::MultiplexedConnection, ttl: Duration) -> Self {
        Self {
            conn,
            ttl_ms: usize::try_from(ttl.as_millis()).unwrap_or(usize::MAX),
        }
    }
}

impl ReplayGuard for RedisReplayGuard {
    async fn check_and_mark(&self, message_id: &str) -> Result<bool, ReplayGuardError> {
        let key = format!("{DEDUP_KEY_PREFIX}{message_id}");
        let mut conn = self.conn.clone();
        // `SET key 1 NX PX <ttl>` -- returns "OK" (bulk string) when the key
        // was newly set (first time seen), nil when it already existed (a
        // duplicate). Atomic: no separate EXISTS+SET race.
        let result: Option<String> = redis::cmd("SET")
            .arg(&key)
            .arg(1)
            .arg("NX")
            .arg("PX")
            .arg(self.ttl_ms)
            .query_async(&mut conn)
            .await
            .map_err(|e| ReplayGuardError(e.to_string()))?;
        Ok(result.is_some())
    }
}

/// Errors a [`RevocationSink`] can return.
#[derive(Debug, thiserror::Error)]
#[error("revocation sink error: {0}")]
pub struct RevocationSinkError(pub String);

/// One revocation notification, wire-shaped for [`RevocationSink::emit`]'s
/// queue payload -- the control-plane reconciler (hub-api, design doc §3.2)
/// reads these and owns the actual `Delete Subscription` call + `connections`
/// row update; this receiver never touches either.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize, PartialEq, Eq)]
pub struct RevocationEvent {
    pub subscription_id: String,
    pub subscription_type: String,
    pub status: String,
    pub broadcaster_user_id: Option<String>,
}

/// Emits a revocation notification for the control plane to act on
/// asynchronously -- this receiver deliberately never calls the Twitch
/// Delete Subscription API or touches the `connections` table inline (that
/// is the reconciler's job, design doc §3.2/§8 increment 5).
/// `#[allow(async_fn_in_trait)]`: see [`SubscriptionSecretResolver`]'s doc.
#[allow(async_fn_in_trait)]
pub trait RevocationSink: Send + Sync {
    async fn emit(&self, event: &RevocationEvent) -> Result<(), RevocationSinkError>;
}

/// Production [`RevocationSink`]: `LPUSH`es onto [`REVOCATION_QUEUE_KEY`].
/// Same cheap-clone-per-call `MultiplexedConnection` pattern as
/// [`RedisReplayGuard`].
#[derive(Clone)]
pub struct RedisRevocationSink {
    conn: redis::aio::MultiplexedConnection,
}

impl RedisRevocationSink {
    #[must_use]
    pub fn new(conn: redis::aio::MultiplexedConnection) -> Self {
        Self { conn }
    }
}

impl RevocationSink for RedisRevocationSink {
    async fn emit(&self, event: &RevocationEvent) -> Result<(), RevocationSinkError> {
        let payload =
            serde_json::to_string(event).map_err(|e| RevocationSinkError(e.to_string()))?;
        let mut conn = self.conn.clone();
        redis::AsyncCommands::lpush::<_, _, ()>(&mut conn, REVOCATION_QUEUE_KEY, payload)
            .await
            .map_err(|e| RevocationSinkError(e.to_string()))
    }
}

/// Errors the HTTP boundary (`crate::http::eventsub`) maps to a response.
/// Every verification-adjacent failure (missing header, bad signature,
/// stale timestamp, unresolvable subscription secret) collapses to the same
/// [`EventSubError::VerificationFailed`] variant with a fixed message --
/// security.md's "uniform error responses (no oracle)": a caller must never
/// be able to distinguish "wrong signature" from "unknown subscription" from
/// "missing header" by response shape/timing/content.
#[derive(Debug, thiserror::Error)]
pub enum EventSubError {
    /// Anything other than `application/json` (optionally with a
    /// `; charset=...` parameter).
    #[error("unsupported content type")]
    UnsupportedContentType,
    /// The body is not valid JSON.
    #[error("malformed request body")]
    MalformedBody,
    /// Missing header, bad signature, stale timestamp, or an unresolvable
    /// subscription secret -- deliberately one variant, one message.
    #[error("eventsub verification failed")]
    VerificationFailed,
}

/// The non-error outcomes [`handle_webhook`] can produce; `crate::http::
/// eventsub` renders each into its HTTP response. Every variant here is a
/// 200 -- Twitch redelivers on anything else, and a duplicate/ignored/
/// unknown-type delivery is not itself an error condition.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EventSubResponse {
    /// `webhook_callback_verification` handshake -- the body is the raw
    /// challenge string, returned as `text/plain`, not wrapped in JSON.
    Challenge(String),
    /// A notification was normalized and published (or publish failed and
    /// was logged -- one bad event must never fail the webhook, matching
    /// the legacy Python handler's own `except Exception` contract).
    Ack,
    /// This message-id was already processed within the dedup TTL --
    /// acknowledged without re-publishing.
    DuplicateIgnored,
    /// A `revocation` message was received and handed off to the control
    /// plane.
    Acknowledged,
    /// A `notification` whose `subscription.type` isn't in
    /// [`KNOWN_EVENT_TYPES`].
    Ignored,
    /// A `Twitch-Eventsub-Message-Type` this receiver doesn't recognize at
    /// all.
    UnknownType,
}

/// Raw EventSub headers, extracted by the HTTP layer -- kept as a plain
/// struct-of-`&str` (not an `axum::http::HeaderMap`) so this module has no
/// axum dependency and every case is directly constructible in tests.
#[derive(Debug, Clone, Copy, Default)]
pub struct RawHeaders<'a> {
    pub message_type: Option<&'a str>,
    pub message_id: Option<&'a str>,
    pub timestamp: Option<&'a str>,
    pub signature: Option<&'a str>,
}

/// True when `content_type` is exactly `application/json`, ignoring an
/// optional `; charset=...`/other parameter suffix.
#[must_use]
pub fn is_allowed_content_type(content_type: Option<&str>) -> bool {
    matches!(
        content_type
            .and_then(|ct| ct.split(';').next())
            .map(str::trim),
        Some("application/json")
    )
}

/// Constant-time comparison of two equal-length-checked strings -- a length
/// mismatch short-circuits (the same behaviour `hmac::Mac::verify_slice`/
/// Python's `hmac.compare_digest` both have; a valid signature always has a
/// fixed, public length, so this leaks nothing an attacker doesn't already
/// know).
fn constant_time_str_eq(a: &str, b: &str) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff: u8 = 0;
    for (x, y) in a.bytes().zip(b.bytes()) {
        diff |= x ^ y;
    }
    diff == 0
}

fn encode_hex_lower(bytes: &[u8]) -> String {
    use std::fmt::Write;
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        let _ = write!(&mut s, "{b:02x}");
    }
    s
}

/// Verifies the `Twitch-Eventsub-Message-Signature` header: `"sha256=" +
/// hex(HMAC-SHA256(secret, message_id + timestamp + body))`, constant-time
/// compared. Byte-identical algorithm to the legacy `eventsub.py::
/// verify_signature`/`trigger/receiver/twitch_module/services/
/// eventsub_handler.py::_verify_signature`.
#[must_use]
pub fn verify_signature(
    secret: &str,
    message_id: &str,
    timestamp: &str,
    body: &[u8],
    signature: &str,
) -> bool {
    let Ok(mut mac) = HmacSha256::new_from_slice(secret.as_bytes()) else {
        // `new_from_slice` only fails for a key length HMAC-SHA256 rejects,
        // which never happens for a byte string of any length -- HMAC keys
        // are always accepted (shorter ones are zero-padded, longer ones
        // pre-hashed) per RFC 2104. Kept as a fail-closed branch rather than
        // an `.expect()`, since "provably infallible" here rests on the
        // `hmac` crate's own documented behaviour, not this crate's code.
        return false;
    };
    mac.update(message_id.as_bytes());
    mac.update(timestamp.as_bytes());
    mac.update(body);
    let digest = mac.finalize().into_bytes();
    let expected = format!("sha256={}", encode_hex_lower(&digest));
    constant_time_str_eq(&expected, signature)
}

/// True when `timestamp` (RFC 3339, Twitch's own format) is within
/// [`REPLAY_WINDOW`] of `now` -- not older than the window, and not more
/// than [`FUTURE_SKEW_TOLERANCE_SECS`] in the future. An unparseable
/// timestamp is never "within window".
#[must_use]
pub fn timestamp_within_replay_window(timestamp: &str, now: chrono::DateTime<chrono::Utc>) -> bool {
    let Ok(parsed) = chrono::DateTime::parse_from_rfc3339(timestamp) else {
        return false;
    };
    let ts_utc = parsed.with_timezone(&chrono::Utc);
    let age = now.signed_duration_since(ts_utc);
    let max_age = chrono::Duration::from_std(REPLAY_WINDOW)
        .unwrap_or_else(|_| chrono::Duration::seconds(600));
    age <= max_age && age >= chrono::Duration::seconds(-FUTURE_SKEW_TOLERANCE_SECS)
}

/// Builds the ingest `source_id` for a Twitch EventSub broadcaster --
/// distinct namespace from `ingest::twitch::source_id`'s IRC-channel-keyed
/// ids (`tw-*`) so the two transports never collide on the same Valkey
/// stream for the same broadcaster.
#[must_use]
fn eventsub_source_id(broadcaster_user_id: &str) -> String {
    format!("tw-eventsub-{broadcaster_user_id}")
}

/// Verifies, dedups, and routes one EventSub webhook POST. Pure
/// orchestration over injected dependencies -- no axum types, no live
/// Valkey/Twitch required in tests. `crate::http::eventsub::twitch_webhook`
/// is the thin axum adapter that extracts headers/body and calls this.
///
/// Order of operations (cheapest/least-trusting checks first, per
/// `security.md`'s hardening baseline): content-type -> parse JSON (to read
/// `subscription.id` for secret resolution) -> required headers present ->
/// secret resolvable -> signature valid -> timestamp within replay window ->
/// dedup -> message-type dispatch. Every step from "secret resolvable"
/// through "timestamp within replay window" fails identically
/// ([`EventSubError::VerificationFailed`]) -- see that variant's doc.
#[allow(clippy::too_many_arguments)]
pub async fn handle_webhook<R, D, V, A>(
    resolver: &R,
    dedup: &D,
    revocation: &V,
    appender: &A,
    metrics: &IngestMetrics,
    keyring: &KeyRing,
    active_kid: &str,
    scope: &Scope,
    content_type: Option<&str>,
    headers: &RawHeaders<'_>,
    body: &[u8],
) -> Result<EventSubResponse, EventSubError>
where
    R: SubscriptionSecretResolver,
    D: ReplayGuard,
    V: RevocationSink,
    A: EventAppender,
{
    if !is_allowed_content_type(content_type) {
        metrics.record_eventsub_verification("bad_content_type");
        return Err(EventSubError::UnsupportedContentType);
    }
    if body.len() > MAX_EVENTSUB_BODY_BYTES {
        // Defense in depth -- `crate::http::eventsub` also enforces this via
        // `axum::extract::DefaultBodyLimit` before the body is ever fully
        // buffered, but a pure-function caller (tests, or a future
        // non-axum caller) must not skip the check.
        metrics.record_eventsub_verification("oversized_body");
        return Err(EventSubError::UnsupportedContentType);
    }

    let body_json: serde_json::Value =
        serde_json::from_slice(body).map_err(|_| EventSubError::MalformedBody)?;

    let (Some(message_id), Some(timestamp), Some(signature), Some(message_type)) = (
        headers.message_id,
        headers.timestamp,
        headers.signature,
        headers.message_type,
    ) else {
        metrics.record_eventsub_verification("missing_header");
        return Err(EventSubError::VerificationFailed);
    };

    let subscription = body_json.get("subscription").cloned().unwrap_or_default();
    let subscription_id = subscription
        .get("id")
        .and_then(|v| v.as_str())
        .unwrap_or_default()
        .to_string();
    let subscription_type = subscription
        .get("type")
        .and_then(|v| v.as_str())
        .unwrap_or_default()
        .to_string();
    let broadcaster_user_id = subscription
        .get("condition")
        .and_then(|c| c.get("broadcaster_user_id"))
        .and_then(|v| v.as_str())
        .map(str::to_string);
    let conduit_id = subscription
        .get("transport")
        .and_then(|t| t.get("conduit_id"))
        .and_then(|v| v.as_str())
        .map(str::to_string);

    let ctx = SubscriptionContext {
        subscription_id: &subscription_id,
        conduit_id: conduit_id.as_deref(),
        broadcaster_user_id: broadcaster_user_id.as_deref(),
    };

    let Ok(secret) = resolver.resolve_secret(&ctx).await else {
        metrics.record_eventsub_verification("secret_not_found");
        tracing::warn!(subscription_id, "eventsub.secret_not_found");
        return Err(EventSubError::VerificationFailed);
    };

    if !verify_signature(&secret, message_id, timestamp, body, signature) {
        metrics.record_eventsub_verification("bad_signature");
        tracing::warn!(subscription_id, "eventsub.invalid_signature");
        return Err(EventSubError::VerificationFailed);
    }

    if !timestamp_within_replay_window(timestamp, chrono::Utc::now()) {
        metrics.record_eventsub_verification("replay_rejected");
        tracing::warn!(
            subscription_id,
            timestamp,
            "eventsub.replay_window_exceeded"
        );
        return Err(EventSubError::VerificationFailed);
    }

    metrics.record_eventsub_verification("ok");

    match dedup.check_and_mark(message_id).await {
        Ok(true) => {}
        Ok(false) => {
            metrics.record_eventsub_dedup_hit();
            tracing::debug!(message_id, "eventsub.duplicate_message");
            return Ok(EventSubResponse::DuplicateIgnored);
        }
        Err(err) => {
            // A dedup-store outage degrades to "no dedup guarantee this
            // request", never "reject the delivery" -- Twitch would just
            // redeliver a rejected notification later anyway, and a missed
            // dedup risks at-most a duplicate downstream event, not a
            // security problem (contrast with the DENY-forever-safe
            // asymmetry in the design doc's relay-authz cache, §6.1).
            tracing::warn!(error = %err, subscription_id, "eventsub.dedup_check_failed; proceeding without dedup guarantee");
        }
    }

    match message_type {
        "webhook_callback_verification" => {
            let challenge = body_json
                .get("challenge")
                .and_then(|v| v.as_str())
                .unwrap_or_default()
                .to_string();
            tracing::info!(subscription_id, "eventsub.subscription_verified");
            Ok(EventSubResponse::Challenge(challenge))
        }
        "revocation" => {
            let status = subscription
                .get("status")
                .and_then(|v| v.as_str())
                .unwrap_or_default()
                .to_string();
            tracing::warn!(
                subscription_id,
                subscription_type,
                status,
                "eventsub.subscription_revoked"
            );
            let event = RevocationEvent {
                subscription_id: subscription_id.clone(),
                subscription_type,
                status,
                broadcaster_user_id,
            };
            if let Err(err) = revocation.emit(&event).await {
                tracing::error!(error = %err, subscription_id, "eventsub.revocation_emit_failed");
            }
            Ok(EventSubResponse::Acknowledged)
        }
        "notification" => {
            if !KNOWN_EVENT_TYPES.contains(&subscription_type.as_str()) {
                tracing::debug!(subscription_type, "eventsub.unhandled_event_type");
                return Ok(EventSubResponse::Ignored);
            }
            let event_body = body_json.get("event").cloned().unwrap_or_default();
            let platform_event = crate::normalize::normalize_twitch_eventsub(
                &subscription_type,
                &event_body,
                broadcaster_user_id.as_deref(),
            );
            let source_id = eventsub_source_id(broadcaster_user_id.as_deref().unwrap_or("unknown"));
            let workstream_id = deterministic_workstream_id(&source_id);
            if let Err(err) = publish_event(
                appender,
                metrics,
                keyring,
                active_kid,
                scope,
                &source_id,
                &workstream_id,
                Some(&subscription_id),
                platform_event,
            )
            .await
            {
                // One bad event must never fail the webhook -- matches the
                // legacy Python handler's own `except Exception` contract
                // (`eventsub.py::handle_webhook`'s "notification" branch).
                tracing::error!(error = %err, subscription_id, "eventsub.publish_failed");
            }
            Ok(EventSubResponse::Ack)
        }
        _ => Ok(EventSubResponse::UnknownType),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use penguin_spine::SpineError;
    use std::sync::Mutex;

    fn test_keyring() -> KeyRing {
        KeyRing::new(vec![("k1".to_string(), vec![9u8; 32])])
    }

    fn test_scope() -> Scope {
        Scope::new("acme", None)
    }

    fn test_metrics() -> IngestMetrics {
        crate::telemetry::register_ingest_metrics(&prometheus::Registry::new())
    }

    #[derive(Default)]
    struct RecordingAppender {
        calls: Mutex<Vec<(String, penguin_spine::StageEnvelope)>>,
    }

    impl EventAppender for RecordingAppender {
        async fn append(
            &self,
            stream: &str,
            env: &penguin_spine::StageEnvelope,
        ) -> Result<String, SpineError> {
            self.calls
                .lock()
                .unwrap()
                .push((stream.to_string(), env.clone()));
            Ok("1-0".to_string())
        }
    }

    #[derive(Default, Clone)]
    struct FakeResolver {
        secret: Option<String>,
    }
    impl SubscriptionSecretResolver for FakeResolver {
        async fn resolve_secret(
            &self,
            _ctx: &SubscriptionContext<'_>,
        ) -> Result<String, SecretResolverError> {
            self.secret.clone().ok_or(SecretResolverError::NotFound)
        }
    }

    #[derive(Default)]
    struct FakeDedup {
        seen: Mutex<std::collections::HashSet<String>>,
        fail: bool,
    }
    impl ReplayGuard for FakeDedup {
        async fn check_and_mark(&self, message_id: &str) -> Result<bool, ReplayGuardError> {
            if self.fail {
                return Err(ReplayGuardError("simulated valkey outage".to_string()));
            }
            Ok(self.seen.lock().unwrap().insert(message_id.to_string()))
        }
    }

    #[derive(Default)]
    struct FakeRevocationSink {
        calls: Mutex<Vec<RevocationEvent>>,
    }
    impl RevocationSink for FakeRevocationSink {
        async fn emit(&self, event: &RevocationEvent) -> Result<(), RevocationSinkError> {
            self.calls.lock().unwrap().push(event.clone());
            Ok(())
        }
    }

    const SECRET: &str = "s3cr3t-eventsub-key";

    fn sign(secret: &str, message_id: &str, timestamp: &str, body: &[u8]) -> String {
        let mut mac = HmacSha256::new_from_slice(secret.as_bytes()).unwrap();
        mac.update(message_id.as_bytes());
        mac.update(timestamp.as_bytes());
        mac.update(body);
        format!("sha256={}", encode_hex_lower(&mac.finalize().into_bytes()))
    }

    fn notification_body(subscription_id: &str, sub_type: &str, broadcaster_id: &str) -> Vec<u8> {
        serde_json::json!({
            "subscription": {
                "id": subscription_id,
                "type": sub_type,
                "condition": {"broadcaster_user_id": broadcaster_id},
                "transport": {"method": "webhook", "callback": "https://x/eventsub/twitch/webhook"},
            },
            "event": {
                "broadcaster_user_id": broadcaster_id,
                "broadcaster_user_login": "somechannel",
                "user_id": "999",
                "user_login": "someuser",
                "user_name": "SomeUser",
                "viewers": 5,
            }
        })
        .to_string()
        .into_bytes()
    }

    fn headers<'a>(
        message_id: &'a str,
        timestamp: &'a str,
        signature: &'a str,
        message_type: &'a str,
    ) -> RawHeaders<'a> {
        RawHeaders {
            message_type: Some(message_type),
            message_id: Some(message_id),
            timestamp: Some(timestamp),
            signature: Some(signature),
        }
    }

    #[tokio::test]
    async fn valid_signature_publishes_a_notification() {
        let body = notification_body("sub-1", "channel.raid", "broadcaster-1");
        let ts = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
        let sig = sign(SECRET, "msg-1", &ts, &body);
        let h = headers("msg-1", &ts, &sig, "notification");

        let resolver = FakeResolver {
            secret: Some(SECRET.to_string()),
        };
        let dedup = FakeDedup::default();
        let revocation = FakeRevocationSink::default();
        let appender = RecordingAppender::default();
        let metrics = test_metrics();

        let outcome = handle_webhook(
            &resolver,
            &dedup,
            &revocation,
            &appender,
            &metrics,
            &test_keyring(),
            "k1",
            &test_scope(),
            Some("application/json"),
            &h,
            &body,
        )
        .await
        .unwrap();

        assert_eq!(outcome, EventSubResponse::Ack);
        let calls = appender.calls.lock().unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(
            calls[0].0,
            "waddles:t:acme:c:_tenant:src:twitch:tw-eventsub-broadcaster-1:events"
        );
    }

    #[tokio::test]
    async fn bad_signature_is_rejected_and_nothing_publishes() {
        let body = notification_body("sub-1", "channel.raid", "broadcaster-1");
        let ts = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
        let h = headers("msg-1", &ts, "sha256=deadbeef", "notification");

        let resolver = FakeResolver {
            secret: Some(SECRET.to_string()),
        };
        let dedup = FakeDedup::default();
        let revocation = FakeRevocationSink::default();
        let appender = RecordingAppender::default();
        let metrics = test_metrics();

        let err = handle_webhook(
            &resolver,
            &dedup,
            &revocation,
            &appender,
            &metrics,
            &test_keyring(),
            "k1",
            &test_scope(),
            Some("application/json"),
            &h,
            &body,
        )
        .await
        .unwrap_err();

        assert!(matches!(err, EventSubError::VerificationFailed));
        assert!(appender.calls.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn replayed_timestamp_outside_window_is_rejected() {
        let body = notification_body("sub-1", "channel.raid", "broadcaster-1");
        let stale_ts = (chrono::Utc::now() - chrono::Duration::minutes(20))
            .to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
        let sig = sign(SECRET, "msg-1", &stale_ts, &body);
        let h = headers("msg-1", &stale_ts, &sig, "notification");

        let resolver = FakeResolver {
            secret: Some(SECRET.to_string()),
        };
        let dedup = FakeDedup::default();
        let revocation = FakeRevocationSink::default();
        let appender = RecordingAppender::default();
        let metrics = test_metrics();

        let err = handle_webhook(
            &resolver,
            &dedup,
            &revocation,
            &appender,
            &metrics,
            &test_keyring(),
            "k1",
            &test_scope(),
            Some("application/json"),
            &h,
            &body,
        )
        .await
        .unwrap_err();

        assert!(matches!(err, EventSubError::VerificationFailed));
        assert!(appender.calls.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn duplicate_message_id_is_ignored_without_republishing() {
        let body = notification_body("sub-1", "channel.raid", "broadcaster-1");
        let ts = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
        let sig = sign(SECRET, "msg-dup", &ts, &body);
        let h = headers("msg-dup", &ts, &sig, "notification");

        let resolver = FakeResolver {
            secret: Some(SECRET.to_string()),
        };
        let dedup = FakeDedup::default();
        let revocation = FakeRevocationSink::default();
        let appender = RecordingAppender::default();
        let metrics = test_metrics();

        let first = handle_webhook(
            &resolver,
            &dedup,
            &revocation,
            &appender,
            &metrics,
            &test_keyring(),
            "k1",
            &test_scope(),
            Some("application/json"),
            &h,
            &body,
        )
        .await
        .unwrap();
        assert_eq!(first, EventSubResponse::Ack);

        let second = handle_webhook(
            &resolver,
            &dedup,
            &revocation,
            &appender,
            &metrics,
            &test_keyring(),
            "k1",
            &test_scope(),
            Some("application/json"),
            &h,
            &body,
        )
        .await
        .unwrap();
        assert_eq!(second, EventSubResponse::DuplicateIgnored);

        assert_eq!(
            appender.calls.lock().unwrap().len(),
            1,
            "the duplicate must not publish a second time"
        );
    }

    #[tokio::test]
    async fn webhook_callback_verification_echoes_the_challenge() {
        let body = serde_json::json!({
            "challenge": "pogchamp-challenge-123",
            "subscription": {"id": "sub-1", "type": "channel.raid", "condition": {"broadcaster_user_id": "broadcaster-1"}},
        })
        .to_string()
        .into_bytes();
        let ts = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
        let sig = sign(SECRET, "msg-verify", &ts, &body);
        let h = headers("msg-verify", &ts, &sig, "webhook_callback_verification");

        let resolver = FakeResolver {
            secret: Some(SECRET.to_string()),
        };
        let dedup = FakeDedup::default();
        let revocation = FakeRevocationSink::default();
        let appender = RecordingAppender::default();
        let metrics = test_metrics();

        let outcome = handle_webhook(
            &resolver,
            &dedup,
            &revocation,
            &appender,
            &metrics,
            &test_keyring(),
            "k1",
            &test_scope(),
            Some("application/json"),
            &h,
            &body,
        )
        .await
        .unwrap();

        assert_eq!(
            outcome,
            EventSubResponse::Challenge("pogchamp-challenge-123".to_string())
        );
        assert!(appender.calls.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn revocation_emits_a_control_plane_event_and_never_publishes() {
        let body = serde_json::json!({
            "subscription": {
                "id": "sub-1",
                "type": "channel.raid",
                "status": "authorization_revoked",
                "condition": {"broadcaster_user_id": "broadcaster-1"},
            },
        })
        .to_string()
        .into_bytes();
        let ts = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
        let sig = sign(SECRET, "msg-revoke", &ts, &body);
        let h = headers("msg-revoke", &ts, &sig, "revocation");

        let resolver = FakeResolver {
            secret: Some(SECRET.to_string()),
        };
        let dedup = FakeDedup::default();
        let revocation = FakeRevocationSink::default();
        let appender = RecordingAppender::default();
        let metrics = test_metrics();

        let outcome = handle_webhook(
            &resolver,
            &dedup,
            &revocation,
            &appender,
            &metrics,
            &test_keyring(),
            "k1",
            &test_scope(),
            Some("application/json"),
            &h,
            &body,
        )
        .await
        .unwrap();

        assert_eq!(outcome, EventSubResponse::Acknowledged);
        assert!(
            appender.calls.lock().unwrap().is_empty(),
            "revocation must never publish"
        );
        let emitted = revocation.calls.lock().unwrap();
        assert_eq!(emitted.len(), 1);
        assert_eq!(emitted[0].subscription_id, "sub-1");
        assert_eq!(emitted[0].status, "authorization_revoked");
        assert_eq!(
            emitted[0].broadcaster_user_id.as_deref(),
            Some("broadcaster-1")
        );
    }

    #[tokio::test]
    async fn oversized_body_is_rejected_before_json_parsing() {
        let oversized = vec![b'a'; MAX_EVENTSUB_BODY_BYTES + 1];
        let ts = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
        let h = headers("msg-big", &ts, "sha256=irrelevant", "notification");

        let resolver = FakeResolver {
            secret: Some(SECRET.to_string()),
        };
        let dedup = FakeDedup::default();
        let revocation = FakeRevocationSink::default();
        let appender = RecordingAppender::default();
        let metrics = test_metrics();

        let err = handle_webhook(
            &resolver,
            &dedup,
            &revocation,
            &appender,
            &metrics,
            &test_keyring(),
            "k1",
            &test_scope(),
            Some("application/json"),
            &h,
            &oversized,
        )
        .await
        .unwrap_err();

        assert!(matches!(err, EventSubError::UnsupportedContentType));
    }

    #[tokio::test]
    async fn wrong_content_type_is_rejected_before_any_verification() {
        let body = notification_body("sub-1", "channel.raid", "broadcaster-1");
        let ts = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
        let sig = sign(SECRET, "msg-1", &ts, &body);
        let h = headers("msg-1", &ts, &sig, "notification");

        let resolver = FakeResolver {
            secret: Some(SECRET.to_string()),
        };
        let dedup = FakeDedup::default();
        let revocation = FakeRevocationSink::default();
        let appender = RecordingAppender::default();
        let metrics = test_metrics();

        let err = handle_webhook(
            &resolver,
            &dedup,
            &revocation,
            &appender,
            &metrics,
            &test_keyring(),
            "k1",
            &test_scope(),
            Some("text/plain"),
            &h,
            &body,
        )
        .await
        .unwrap_err();

        assert!(matches!(err, EventSubError::UnsupportedContentType));
        assert!(appender.calls.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn content_type_with_charset_parameter_is_still_allowed() {
        let body = notification_body("sub-1", "channel.raid", "broadcaster-1");
        let ts = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
        let sig = sign(SECRET, "msg-1", &ts, &body);
        let h = headers("msg-1", &ts, &sig, "notification");

        let resolver = FakeResolver {
            secret: Some(SECRET.to_string()),
        };
        let dedup = FakeDedup::default();
        let revocation = FakeRevocationSink::default();
        let appender = RecordingAppender::default();
        let metrics = test_metrics();

        let outcome = handle_webhook(
            &resolver,
            &dedup,
            &revocation,
            &appender,
            &metrics,
            &test_keyring(),
            "k1",
            &test_scope(),
            Some("application/json; charset=utf-8"),
            &h,
            &body,
        )
        .await
        .unwrap();
        assert_eq!(outcome, EventSubResponse::Ack);
    }

    #[tokio::test]
    async fn unknown_subscription_secret_fails_closed_uniformly_with_bad_signature() {
        let body = notification_body("sub-unknown", "channel.raid", "broadcaster-1");
        let ts = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
        let sig = sign(SECRET, "msg-1", &ts, &body);
        let h = headers("msg-1", &ts, &sig, "notification");

        let resolver = FakeResolver { secret: None };
        let dedup = FakeDedup::default();
        let revocation = FakeRevocationSink::default();
        let appender = RecordingAppender::default();
        let metrics = test_metrics();

        let err = handle_webhook(
            &resolver,
            &dedup,
            &revocation,
            &appender,
            &metrics,
            &test_keyring(),
            "k1",
            &test_scope(),
            Some("application/json"),
            &h,
            &body,
        )
        .await
        .unwrap_err();

        // Same variant/message as a bad signature -- no oracle distinguishing
        // "unknown subscription" from "wrong signature".
        assert!(matches!(err, EventSubError::VerificationFailed));
    }

    #[tokio::test]
    async fn missing_required_header_fails_closed() {
        let body = notification_body("sub-1", "channel.raid", "broadcaster-1");
        let h = RawHeaders {
            message_type: Some("notification"),
            message_id: None,
            timestamp: Some("2026-01-01T00:00:00.000Z"),
            signature: Some("sha256=whatever"),
        };

        let resolver = FakeResolver {
            secret: Some(SECRET.to_string()),
        };
        let dedup = FakeDedup::default();
        let revocation = FakeRevocationSink::default();
        let appender = RecordingAppender::default();
        let metrics = test_metrics();

        let err = handle_webhook(
            &resolver,
            &dedup,
            &revocation,
            &appender,
            &metrics,
            &test_keyring(),
            "k1",
            &test_scope(),
            Some("application/json"),
            &h,
            &body,
        )
        .await
        .unwrap_err();

        assert!(matches!(err, EventSubError::VerificationFailed));
    }

    #[tokio::test]
    async fn malformed_json_body_is_rejected() {
        let ts = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
        let h = headers("msg-1", &ts, "sha256=whatever", "notification");

        let resolver = FakeResolver {
            secret: Some(SECRET.to_string()),
        };
        let dedup = FakeDedup::default();
        let revocation = FakeRevocationSink::default();
        let appender = RecordingAppender::default();
        let metrics = test_metrics();

        let err = handle_webhook(
            &resolver,
            &dedup,
            &revocation,
            &appender,
            &metrics,
            &test_keyring(),
            "k1",
            &test_scope(),
            Some("application/json"),
            &h,
            b"not json",
        )
        .await
        .unwrap_err();

        assert!(matches!(err, EventSubError::MalformedBody));
    }

    #[tokio::test]
    async fn unhandled_notification_event_type_is_ignored_without_publishing() {
        let body = notification_body("sub-1", "channel.moderator.add", "broadcaster-1");
        let ts = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
        let sig = sign(SECRET, "msg-1", &ts, &body);
        let h = headers("msg-1", &ts, &sig, "notification");

        let resolver = FakeResolver {
            secret: Some(SECRET.to_string()),
        };
        let dedup = FakeDedup::default();
        let revocation = FakeRevocationSink::default();
        let appender = RecordingAppender::default();
        let metrics = test_metrics();

        let outcome = handle_webhook(
            &resolver,
            &dedup,
            &revocation,
            &appender,
            &metrics,
            &test_keyring(),
            "k1",
            &test_scope(),
            Some("application/json"),
            &h,
            &body,
        )
        .await
        .unwrap();

        assert_eq!(outcome, EventSubResponse::Ignored);
        assert!(appender.calls.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn unrecognized_message_type_is_acked_as_unknown() {
        let body = notification_body("sub-1", "channel.raid", "broadcaster-1");
        let ts = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
        let sig = sign(SECRET, "msg-1", &ts, &body);
        let h = headers("msg-1", &ts, &sig, "some_future_message_type");

        let resolver = FakeResolver {
            secret: Some(SECRET.to_string()),
        };
        let dedup = FakeDedup::default();
        let revocation = FakeRevocationSink::default();
        let appender = RecordingAppender::default();
        let metrics = test_metrics();

        let outcome = handle_webhook(
            &resolver,
            &dedup,
            &revocation,
            &appender,
            &metrics,
            &test_keyring(),
            "k1",
            &test_scope(),
            Some("application/json"),
            &h,
            &body,
        )
        .await
        .unwrap();

        assert_eq!(outcome, EventSubResponse::UnknownType);
    }

    #[tokio::test]
    async fn dedup_store_outage_fails_open_and_still_publishes() {
        let body = notification_body("sub-1", "channel.raid", "broadcaster-1");
        let ts = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
        let sig = sign(SECRET, "msg-1", &ts, &body);
        let h = headers("msg-1", &ts, &sig, "notification");

        let resolver = FakeResolver {
            secret: Some(SECRET.to_string()),
        };
        let dedup = FakeDedup {
            fail: true,
            ..Default::default()
        };
        let revocation = FakeRevocationSink::default();
        let appender = RecordingAppender::default();
        let metrics = test_metrics();

        let outcome = handle_webhook(
            &resolver,
            &dedup,
            &revocation,
            &appender,
            &metrics,
            &test_keyring(),
            "k1",
            &test_scope(),
            Some("application/json"),
            &h,
            &body,
        )
        .await
        .unwrap();

        assert_eq!(outcome, EventSubResponse::Ack);
        assert_eq!(appender.calls.lock().unwrap().len(), 1);
    }

    #[test]
    fn verify_signature_matches_the_legacy_python_algorithm_shape() {
        let secret = "topsecret";
        let message_id = "abc-123";
        let timestamp = "2026-09-28T00:00:00.000Z";
        let body = b"{\"subscription\":{}}";
        let sig = sign(secret, message_id, timestamp, body);
        assert!(sig.starts_with("sha256="));
        assert_eq!(sig.len(), "sha256=".len() + 64);
        assert!(verify_signature(secret, message_id, timestamp, body, &sig));
        assert!(!verify_signature(
            "wrong-secret",
            message_id,
            timestamp,
            body,
            &sig
        ));
        assert!(!verify_signature(
            secret,
            "different-id",
            timestamp,
            body,
            &sig
        ));
    }

    #[test]
    fn timestamp_within_replay_window_accepts_recent_and_rejects_stale() {
        let now = chrono::Utc::now();
        let recent = now - chrono::Duration::minutes(5);
        let stale = now - chrono::Duration::minutes(11);
        let future_ok = now + chrono::Duration::seconds(30);
        let future_bad = now + chrono::Duration::minutes(5);
        assert!(timestamp_within_replay_window(
            &recent.to_rfc3339_opts(chrono::SecondsFormat::Millis, true),
            now
        ));
        assert!(!timestamp_within_replay_window(
            &stale.to_rfc3339_opts(chrono::SecondsFormat::Millis, true),
            now
        ));
        assert!(timestamp_within_replay_window(
            &future_ok.to_rfc3339_opts(chrono::SecondsFormat::Millis, true),
            now
        ));
        assert!(!timestamp_within_replay_window(
            &future_bad.to_rfc3339_opts(chrono::SecondsFormat::Millis, true),
            now
        ));
        assert!(!timestamp_within_replay_window("not-a-timestamp", now));
    }

    #[test]
    fn is_allowed_content_type_accepts_json_and_charset_variants_only() {
        assert!(is_allowed_content_type(Some("application/json")));
        assert!(is_allowed_content_type(Some(
            "application/json; charset=utf-8"
        )));
        assert!(!is_allowed_content_type(Some("text/plain")));
        assert!(!is_allowed_content_type(Some("application/xml")));
        assert!(!is_allowed_content_type(None));
    }

    #[test]
    fn env_secret_resolver_prefers_per_subscription_override_then_falls_back() {
        let resolver = EnvSecretResolver::from_env(Some(Secret::new("fallback-secret")))
            .with_subscription_secret("sub-special", "special-secret");
        let rt = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        let ctx_special = SubscriptionContext {
            subscription_id: "sub-special",
            conduit_id: None,
            broadcaster_user_id: None,
        };
        let ctx_other = SubscriptionContext {
            subscription_id: "sub-other",
            conduit_id: None,
            broadcaster_user_id: None,
        };
        assert_eq!(
            rt.block_on(resolver.resolve_secret(&ctx_special)).unwrap(),
            "special-secret"
        );
        assert_eq!(
            rt.block_on(resolver.resolve_secret(&ctx_other)).unwrap(),
            "fallback-secret"
        );
    }

    #[test]
    fn env_secret_resolver_with_no_secret_configured_fails_closed() {
        let resolver = EnvSecretResolver::from_env(None);
        let rt = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        let ctx = SubscriptionContext {
            subscription_id: "sub-1",
            conduit_id: None,
            broadcaster_user_id: None,
        };
        assert!(matches!(
            rt.block_on(resolver.resolve_secret(&ctx)).unwrap_err(),
            SecretResolverError::NotFound
        ));
    }
}
