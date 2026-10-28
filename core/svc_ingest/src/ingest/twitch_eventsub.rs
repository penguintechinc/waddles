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
//!
//! **Secret resolution never parses the body.** The webhook path carries an
//! opaque `callback_key` segment (`POST /eventsub/twitch/webhook/
//! {callback_key}`, `crate::http::eventsub::router`; the legacy bare
//! `POST /eventsub/twitch/webhook` path maps to [`LEGACY_CALLBACK_KEY`] for
//! alpha backward compatibility) that [`SubscriptionSecretResolver`]
//! resolves against -- never a field read out of the JSON body. This is
//! load-bearing for [`handle_webhook`]'s ordering (see that function's own
//! doc): an unauthenticated body must never reach `serde_json::from_slice`,
//! and resolving the secret from a body field would have required parsing
//! first, defeating that guarantee (the vulnerability this module's own
//! history records: JSON was originally parsed before HMAC verification to
//! read `subscription.id` for secret lookup -- fixed by moving secret
//! resolution to the URL path instead).

use std::collections::HashMap;
use std::time::Duration;

use hmac::{Hmac, Mac};
use penguin_spine::{KeyRing, Scope};
use sha2::Sha256;
use subtle::ConstantTimeEq;

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

/// The `callback_key` the legacy bare `POST /eventsub/twitch/webhook` path
/// (no path segment) resolves its secret under -- alpha backward
/// compatibility with the pre-existing Twitch subscription callback URL.
/// New subscriptions should register under `POST /eventsub/twitch/webhook/
/// {callback_key}` with a real opaque key instead.
pub const LEGACY_CALLBACK_KEY: &str = "legacy-default";

/// Errors a [`SubscriptionSecretResolver`] can return.
#[derive(Debug, thiserror::Error)]
pub enum SecretResolverError {
    /// No secret is registered for this callback key.
    #[error("no eventsub secret configured for this callback key")]
    NotFound,
}

/// Resolves the HMAC secret that verifies deliveries to one Twitch EventSub
/// webhook callback URL. Looked up by `callback_key` -- an opaque identifier
/// carried in the URL path (`POST /eventsub/twitch/webhook/{callback_key}`,
/// `crate::http::eventsub::router`), **never a field read out of the request
/// body**: resolving the secret must not require parsing the (as yet
/// unauthenticated) JSON payload -- see this module's own doc comment.
/// Never a single global secret in the production impl, so one compromised/
/// rotated secret never affects every tenant's subscriptions at once.
///
/// `#[allow(async_fn_in_trait)]`: this trait must be `pub` (it appears in
/// [`handle_webhook`]'s public signature), so the crate-level "you can
/// suppress this if the trait is only used in your own code" escape hatch
/// applies literally -- `svc-ingest` is a deployed service binary, not a
/// published library other crates implement this trait against (same
/// justification as `crate::publish::EventAppender`'s own doc comment).
#[allow(async_fn_in_trait)]
pub trait SubscriptionSecretResolver: Send + Sync {
    /// Resolves the secret for `callback_key`, or
    /// `Err(SecretResolverError::NotFound)` if nothing is registered.
    /// `headers` is passed through for a future broker impl's own audit
    /// logging/keying needs (e.g. correlating by message-id) -- never
    /// consulted for the secret value itself in [`EnvSecretResolver`].
    async fn resolve_secret(
        &self,
        callback_key: &str,
        headers: &RawHeaders<'_>,
    ) -> Result<String, SecretResolverError>;
}

/// **Alpha/test-only** [`SubscriptionSecretResolver`]: a single fallback
/// secret from `TWITCH_EVENTSUB_SECRET` (mirrors the legacy Python module's
/// single-tenant env var), plus an optional in-memory per-callback-key
/// override map for tests. The credential-broker-backed implementation
/// (per-connection secret resolved from `connection_credentials` via
/// hub-api, design doc §2) lands behind this same trait in a later
/// increment -- this impl is never meant to serve a real multi-tenant
/// deployment.
#[derive(Clone, Default)]
pub struct EnvSecretResolver {
    default_secret: Option<Secret>,
    per_callback_key: HashMap<String, Secret>,
}

impl EnvSecretResolver {
    /// Builds a resolver whose fallback secret is `TWITCH_EVENTSUB_SECRET`,
    /// with no per-callback-key overrides.
    #[must_use]
    pub fn from_env(default_secret: Option<Secret>) -> Self {
        Self {
            default_secret,
            per_callback_key: HashMap::new(),
        }
    }

    /// Registers a per-callback-key secret override, taking precedence over
    /// the fallback -- test-only helper (also usable for a small, static
    /// alpha deployment with a handful of manually-configured webhook
    /// callback URLs).
    #[must_use]
    pub fn with_callback_key_secret(
        mut self,
        callback_key: impl Into<String>,
        secret: impl Into<String>,
    ) -> Self {
        self.per_callback_key
            .insert(callback_key.into(), Secret::new(secret.into()));
        self
    }
}

impl SubscriptionSecretResolver for EnvSecretResolver {
    async fn resolve_secret(
        &self,
        callback_key: &str,
        _headers: &RawHeaders<'_>,
    ) -> Result<String, SecretResolverError> {
        if let Some(secret) = self.per_callback_key.get(callback_key) {
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

/// Decodes a lowercase-or-uppercase hex string into bytes. Pure format
/// validation of attacker-supplied input (the `Twitch-Eventsub-Message-
/// Signature` header's hex portion) -- not itself the security-sensitive
/// comparison (that's [`verify_signature`]'s `subtle::ConstantTimeEq` step),
/// so an early return on a malformed character leaks nothing beyond what
/// the attacker already knows about their own header.
fn decode_hex(s: &str) -> Result<Vec<u8>, ()> {
    if !s.len().is_multiple_of(2) {
        return Err(());
    }
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len() / 2);
    let mut i = 0;
    while i < bytes.len() {
        let hi = hex_nibble(bytes[i])?;
        let lo = hex_nibble(bytes[i + 1])?;
        out.push((hi << 4) | lo);
        i += 2;
    }
    Ok(out)
}

fn hex_nibble(b: u8) -> Result<u8, ()> {
    match b {
        b'0'..=b'9' => Ok(b - b'0'),
        b'a'..=b'f' => Ok(b - b'a' + 10),
        b'A'..=b'F' => Ok(b - b'A' + 10),
        _ => Err(()),
    }
}

/// Verifies the `Twitch-Eventsub-Message-Signature` header: `"sha256=" +
/// hex(HMAC-SHA256(secret, message_id + timestamp + body))`. Byte-identical
/// algorithm to the legacy `eventsub.py::verify_signature`/`trigger/
/// receiver/twitch_module/services/eventsub_handler.py::_verify_signature`,
/// but compared as raw decoded bytes via the audited `subtle::
/// ConstantTimeEq` -- never a hand-rolled comparison loop -- rather than a
/// hex-string comparison.
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
    let expected_digest = mac.finalize().into_bytes();

    let Some(hex_part) = signature.strip_prefix("sha256=") else {
        return false;
    };
    let Ok(provided_digest) = decode_hex(hex_part) else {
        return false;
    };

    // `ConstantTimeEq::ct_eq` on `&[u8]` handles a length mismatch itself
    // (returns a false `Choice` rather than panicking or branching on
    // length before comparing content) -- no separate length pre-check of
    // our own that could short-circuit ahead of the constant-time compare.
    expected_digest.as_slice().ct_eq(&provided_digest).into()
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
/// is the thin axum adapter that extracts the path's `callback_key`/
/// headers/body and calls this.
///
/// **Order of operations is deliberate and security-load-bearing**
/// (cheapest/least-trusting checks first, per `security.md`'s hardening
/// baseline, and -- critically -- every check up through the replay-window
/// check runs on the raw header/body bytes alone, *before* `serde_json`
/// ever touches the body):
///
/// content-type -> body size -> required headers present -> secret
/// resolvable (by [`SubscriptionSecretResolver`]'s own contract, from
/// `callback_key` alone, never a body field) -> HMAC signature valid over
/// the raw body -> timestamp within replay window -> dedup -> **only now**
/// `serde_json::from_slice` -> message-type dispatch. An attacker without
/// the secret can therefore never reach the JSON parser at all -- closing
/// both a JSON-parser-as-oracle side channel and a cheap unauthenticated-
/// parsing DoS surface. Every step from "secret resolvable" through
/// "timestamp within replay window" fails identically
/// ([`EventSubError::VerificationFailed`]) -- see that variant's doc.
#[allow(clippy::too_many_arguments)]
pub async fn handle_webhook<R, D, V, A, Dk>(
    resolver: &R,
    dedup: &D,
    revocation: &V,
    appender: &A,
    metrics: &IngestMetrics,
    keyring: &KeyRing,
    active_kid: &str,
    scope: &Scope,
    dek_provider: &Dk,
    callback_key: &str,
    content_type: Option<&str>,
    headers: &RawHeaders<'_>,
    body: &[u8],
) -> Result<EventSubResponse, EventSubError>
where
    R: SubscriptionSecretResolver,
    D: ReplayGuard,
    V: RevocationSink,
    A: EventAppender,
    Dk: crate::identity_crypto::DekProvider,
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

    let (Some(message_id), Some(timestamp), Some(signature), Some(message_type)) = (
        headers.message_id,
        headers.timestamp,
        headers.signature,
        headers.message_type,
    ) else {
        metrics.record_eventsub_verification("missing_header");
        return Err(EventSubError::VerificationFailed);
    };

    // Secret resolution reads only `callback_key` (URL path) + headers --
    // never the body, which is not yet authenticated (and not yet parsed).
    let Ok(secret) = resolver.resolve_secret(callback_key, headers).await else {
        metrics.record_eventsub_verification("secret_not_found");
        tracing::warn!(callback_key, "eventsub.secret_not_found");
        return Err(EventSubError::VerificationFailed);
    };

    if !verify_signature(&secret, message_id, timestamp, body, signature) {
        metrics.record_eventsub_verification("bad_signature");
        tracing::warn!(callback_key, "eventsub.invalid_signature");
        return Err(EventSubError::VerificationFailed);
    }

    if !timestamp_within_replay_window(timestamp, chrono::Utc::now()) {
        metrics.record_eventsub_verification("replay_rejected");
        tracing::warn!(callback_key, timestamp, "eventsub.replay_window_exceeded");
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
            // Fails OPEN, never closed: a dedup-store outage degrades to
            // "no dedup guarantee this request", never "reject the
            // delivery". This is safe because a duplicate that slips
            // through is still bounded -- Twitch's own `message_id` is
            // stable across redeliveries, so any downstream consumer that
            // wants exactly-once semantics can key its own idempotency
            // check off that same id (this receiver's job ends at "publish
            // at-least-once, never lose a delivery to a Valkey blip"; a
            // rare duplicate is a downstream idempotency concern, not a
            // security problem -- contrast with the DENY-forever-safe
            // asymmetry in the design doc's relay-authz cache, §6.1).
            tracing::warn!(error = %err, callback_key, message_id, "eventsub.dedup_check_failed; proceeding without dedup guarantee (fails open -- duplicates are bounded by downstream idempotency keyed on message_id)");
        }
    }

    // JSON parsing happens only after the delivery is content-type-checked,
    // size-capped, authenticated (HMAC), and replay/dedup-checked -- see
    // this function's own doc comment for why that ordering is the actual
    // point, not an implementation detail.
    let body_json: serde_json::Value =
        serde_json::from_slice(body).map_err(|_| EventSubError::MalformedBody)?;

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
                dek_provider,
                &metrics.identity_encryption_total,
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

    const CALLBACK_KEY: &str = "cb-key-1";

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
            _callback_key: &str,
            _headers: &RawHeaders<'_>,
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

    /// Test-only hex encoder for building a *correct* signature in `sign()`
    /// -- production `verify_signature` no longer encodes hex at all (it
    /// decodes the attacker-supplied hex and compares raw bytes via
    /// `subtle::ConstantTimeEq`), so this exists solely to construct
    /// expected-valid fixtures.
    fn encode_hex_lower(bytes: &[u8]) -> String {
        use std::fmt::Write;
        let mut s = String::with_capacity(bytes.len() * 2);
        for b in bytes {
            let _ = write!(&mut s, "{b:02x}");
        }
        s
    }

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

    /// Helper bundling every fixed dependency so each test only has to name
    /// the resolver/dedup it cares about varying.
    #[allow(clippy::too_many_arguments)]
    async fn call(
        resolver: &FakeResolver,
        dedup: &FakeDedup,
        revocation: &FakeRevocationSink,
        appender: &RecordingAppender,
        metrics: &IngestMetrics,
        callback_key: &str,
        content_type: Option<&str>,
        h: &RawHeaders<'_>,
        body: &[u8],
    ) -> Result<EventSubResponse, EventSubError> {
        struct FakeDekProvider;
        impl crate::identity_crypto::DekProvider for FakeDekProvider {
            async fn get_dek(
                &self,
                _tenant_id: &str,
            ) -> Result<
                (crate::identity_crypto::Dek, u32),
                crate::identity_crypto::DekUnavailableError,
            > {
                Ok((zeroize::Zeroizing::new([9u8; 32]), 1))
            }
        }

        handle_webhook(
            resolver,
            dedup,
            revocation,
            appender,
            metrics,
            &test_keyring(),
            "k1",
            &test_scope(),
            &FakeDekProvider,
            callback_key,
            content_type,
            h,
            body,
        )
        .await
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

        let outcome = call(
            &resolver,
            &dedup,
            &revocation,
            &appender,
            &metrics,
            CALLBACK_KEY,
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

        let err = call(
            &resolver,
            &dedup,
            &revocation,
            &appender,
            &metrics,
            CALLBACK_KEY,
            Some("application/json"),
            &h,
            &body,
        )
        .await
        .unwrap_err();

        assert!(matches!(err, EventSubError::VerificationFailed));
        assert!(appender.calls.lock().unwrap().is_empty());
    }

    /// CRITICAL regression test: proves the JSON parser is never reached
    /// when the signature is wrong -- the body here is deliberately *not*
    /// valid JSON at all. If `handle_webhook` still parsed before verifying
    /// (the original ordering), this would fail with `MalformedBody`
    /// instead of `VerificationFailed`, since a malformed-but-unparsed body
    /// never gets the chance to report itself as malformed.
    #[tokio::test]
    async fn bad_signature_with_non_json_body_never_reaches_the_parser() {
        let body: &[u8] = b"this is not json at all {{{";
        let ts = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
        let h = headers("msg-1", &ts, "sha256=deadbeef", "notification");

        let resolver = FakeResolver {
            secret: Some(SECRET.to_string()),
        };
        let dedup = FakeDedup::default();
        let revocation = FakeRevocationSink::default();
        let appender = RecordingAppender::default();
        let metrics = test_metrics();

        let err = call(
            &resolver,
            &dedup,
            &revocation,
            &appender,
            &metrics,
            CALLBACK_KEY,
            Some("application/json"),
            &h,
            body,
        )
        .await
        .unwrap_err();

        assert!(
            matches!(err, EventSubError::VerificationFailed),
            "a bad signature must fail as VerificationFailed even over a non-JSON body \
             -- MalformedBody would prove the parser ran before verification"
        );
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

        let err = call(
            &resolver,
            &dedup,
            &revocation,
            &appender,
            &metrics,
            CALLBACK_KEY,
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

        let first = call(
            &resolver,
            &dedup,
            &revocation,
            &appender,
            &metrics,
            CALLBACK_KEY,
            Some("application/json"),
            &h,
            &body,
        )
        .await
        .unwrap();
        assert_eq!(first, EventSubResponse::Ack);

        let second = call(
            &resolver,
            &dedup,
            &revocation,
            &appender,
            &metrics,
            CALLBACK_KEY,
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

        let outcome = call(
            &resolver,
            &dedup,
            &revocation,
            &appender,
            &metrics,
            CALLBACK_KEY,
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

        let outcome = call(
            &resolver,
            &dedup,
            &revocation,
            &appender,
            &metrics,
            CALLBACK_KEY,
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

        let err = call(
            &resolver,
            &dedup,
            &revocation,
            &appender,
            &metrics,
            CALLBACK_KEY,
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

        let err = call(
            &resolver,
            &dedup,
            &revocation,
            &appender,
            &metrics,
            CALLBACK_KEY,
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

        let outcome = call(
            &resolver,
            &dedup,
            &revocation,
            &appender,
            &metrics,
            CALLBACK_KEY,
            Some("application/json; charset=utf-8"),
            &h,
            &body,
        )
        .await
        .unwrap();
        assert_eq!(outcome, EventSubResponse::Ack);
    }

    #[tokio::test]
    async fn unknown_callback_key_secret_fails_closed_uniformly_with_bad_signature() {
        let body = notification_body("sub-unknown", "channel.raid", "broadcaster-1");
        let ts = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
        let sig = sign(SECRET, "msg-1", &ts, &body);
        let h = headers("msg-1", &ts, &sig, "notification");

        let resolver = FakeResolver { secret: None };
        let dedup = FakeDedup::default();
        let revocation = FakeRevocationSink::default();
        let appender = RecordingAppender::default();
        let metrics = test_metrics();

        let err = call(
            &resolver,
            &dedup,
            &revocation,
            &appender,
            &metrics,
            "unknown-callback-key",
            Some("application/json"),
            &h,
            &body,
        )
        .await
        .unwrap_err();

        // Same variant/message as a bad signature -- no oracle distinguishing
        // "unknown callback key" from "wrong signature".
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

        let err = call(
            &resolver,
            &dedup,
            &revocation,
            &appender,
            &metrics,
            CALLBACK_KEY,
            Some("application/json"),
            &h,
            &body,
        )
        .await
        .unwrap_err();

        assert!(matches!(err, EventSubError::VerificationFailed));
    }

    #[tokio::test]
    async fn malformed_json_body_is_rejected_after_a_valid_signature() {
        let body = b"not json";
        let ts = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
        let sig = sign(SECRET, "msg-1", &ts, body);
        let h = headers("msg-1", &ts, &sig, "notification");

        let resolver = FakeResolver {
            secret: Some(SECRET.to_string()),
        };
        let dedup = FakeDedup::default();
        let revocation = FakeRevocationSink::default();
        let appender = RecordingAppender::default();
        let metrics = test_metrics();

        let err = call(
            &resolver,
            &dedup,
            &revocation,
            &appender,
            &metrics,
            CALLBACK_KEY,
            Some("application/json"),
            &h,
            body,
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

        let outcome = call(
            &resolver,
            &dedup,
            &revocation,
            &appender,
            &metrics,
            CALLBACK_KEY,
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

        let outcome = call(
            &resolver,
            &dedup,
            &revocation,
            &appender,
            &metrics,
            CALLBACK_KEY,
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

        let outcome = call(
            &resolver,
            &dedup,
            &revocation,
            &appender,
            &metrics,
            CALLBACK_KEY,
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
    fn verify_signature_rejects_missing_prefix_and_odd_length_hex() {
        let secret = "topsecret";
        let message_id = "abc-123";
        let timestamp = "2026-09-28T00:00:00.000Z";
        let body = b"{}";
        assert!(!verify_signature(
            secret, message_id, timestamp, body, "deadbeef"
        ));
        assert!(!verify_signature(
            secret,
            message_id,
            timestamp,
            body,
            "sha256=abc"
        ));
        assert!(!verify_signature(
            secret,
            message_id,
            timestamp,
            body,
            "sha256=zz"
        ));
        assert!(!verify_signature(secret, message_id, timestamp, body, ""));
    }

    #[test]
    fn decode_hex_round_trips_and_rejects_malformed_input() {
        assert_eq!(decode_hex("00ff").unwrap(), vec![0x00, 0xff]);
        assert_eq!(decode_hex("").unwrap(), Vec::<u8>::new());
        assert!(decode_hex("abc").is_err(), "odd length must be rejected");
        assert!(decode_hex("zz").is_err(), "non-hex chars must be rejected");
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

    #[tokio::test]
    async fn env_secret_resolver_prefers_per_callback_key_override_then_falls_back() {
        let resolver = EnvSecretResolver::from_env(Some(Secret::new("fallback-secret")))
            .with_callback_key_secret("cb-special", "special-secret");
        let no_headers = RawHeaders::default();
        assert_eq!(
            resolver
                .resolve_secret("cb-special", &no_headers)
                .await
                .unwrap(),
            "special-secret"
        );
        assert_eq!(
            resolver
                .resolve_secret("cb-other", &no_headers)
                .await
                .unwrap(),
            "fallback-secret"
        );
    }

    #[tokio::test]
    async fn env_secret_resolver_with_no_secret_configured_fails_closed() {
        let resolver = EnvSecretResolver::from_env(None);
        let no_headers = RawHeaders::default();
        assert!(matches!(
            resolver
                .resolve_secret("cb-1", &no_headers)
                .await
                .unwrap_err(),
            SecretResolverError::NotFound
        ));
    }

    #[test]
    fn legacy_callback_key_constant_is_stable() {
        // Regression guard: this is a wire-visible mapping (the bare
        // `POST /eventsub/twitch/webhook` path resolves under this exact
        // key) -- changing it silently would break every alpha subscription
        // still pointed at the legacy URL.
        assert_eq!(LEGACY_CALLBACK_KEY, "legacy-default");
    }
}
