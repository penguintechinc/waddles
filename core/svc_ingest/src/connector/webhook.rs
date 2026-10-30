//! Generic registered-webhook ingress core (P6a Task 1): the route registry,
//! the host-side secret source, the per-route dispatch hub, and
//! [`WebhookTransport`] -- the [`Transport`] impl that feeds VERIFIED bodies
//! to the owning connector bundle's `on-frame`.
//!
//! ```text
//!  POST /intake/webhook/{route_key}        (http::webhook adapter)
//!        |
//!        v   registry lookup BY ROUTE KEY  -> scheme, secret_ref, owner (HOST config)
//!   WebhookIngress::handle
//!        | rate limit -> body limit -> Scheme::prepare (headers/ts window)
//!        | -> secret resolved per call via connection_resolver (SecretString,
//!        |    never cached, never leaves this fn) -> Prepared::check (const-time)
//!        | -> dedup (ReplayGuard, namespaced by route) -> owner check
//!        v
//!   mpsc -> WebhookTransport::next_frame -> ConnectorDriver -> on-frame
//! ```
//!
//! Security properties (each has a test): scheme/secret_ref/owner come only
//! from the registration; a missing/unresolvable secret rejects; the guest
//! sees only the verified body (the frame carries no secret, headers or
//! route key -- `source_id` is an opaque hash); a transport can only be bound
//! to its route's owner; per-route body size and rate limits.

use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex, RwLock};
use std::time::{Duration, Instant};

use axum::http::HeaderMap;
use connection_resolver::{
    ConnectionRef, ConnectionResolve, ConnectionSecret, ResolveError, SecretString,
};
use sha2::{Digest, Sha256};
use tokio::sync::mpsc;

use super::transport::{Transport, TransportError};
use super::types::{ConnectionCtx, Handshake};
use super::webhook_verify::{hex_encode, Scheme};
use crate::ingest::twitch_eventsub::{RedisReplayGuard, ReplayGuard, DEDUP_TTL};

/// Absolute per-route ceiling on `body_limit`.
pub const HARD_MAX_BODY_BYTES: usize = 1024 * 1024;
/// Verified-frame queue depth per route; full => 503 (backpressure, never a
/// silent drop).
pub const FRAME_QUEUE_DEPTH: usize = 256;

/// Boxed future so the secret source / dedup stay object-safe.
pub type BoxFut<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// Per-route fixed-window rate limit.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RateLimit {
    /// Requests allowed per `per`.
    pub max: u32,
    /// Window length.
    pub per: Duration,
}

/// One registered webhook route. Everything here is HOST configuration.
#[derive(Debug, Clone)]
pub struct WebhookRoute {
    /// Opaque key in the URL; the only request-derived lookup input.
    pub route_key: String,
    /// Connector platform slug that owns (receives) this route's frames.
    pub owner_bundle: String,
    /// Signature scheme.
    pub scheme: Scheme,
    /// Opaque secret reference: the community-bound connection holding the
    /// HMAC key / Ed25519 public key. Resolved per call.
    pub secret_ref: ConnectionRef,
    /// Replay window (timestamp tolerance); <= [`DEDUP_TTL`].
    pub replay_window: Duration,
    /// Rate limit.
    pub rate_limit: RateLimit,
    /// Max body bytes; <= [`HARD_MAX_BODY_BYTES`].
    pub body_limit: usize,
}

impl WebhookRoute {
    /// Validates registration-time invariants (fail loud at register, not at
    /// request time).
    pub fn validate(&self) -> Result<(), RegistryError> {
        let bad = |m: &str| Err(RegistryError::Invalid(m.to_string()));
        let k = &self.route_key;
        if !(8..=128).contains(&k.len())
            || !k
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
        {
            return bad("route_key must be 8-128 chars of [A-Za-z0-9_-]");
        }
        if self.owner_bundle.is_empty() {
            return bad("owner_bundle required");
        }
        if self.replay_window.is_zero() || self.replay_window > DEDUP_TTL {
            return bad("replay_window must be >0 and <= dedup TTL");
        }
        if self.body_limit == 0 || self.body_limit > HARD_MAX_BODY_BYTES {
            return bad("body_limit out of range");
        }
        if self.rate_limit.max == 0 || self.rate_limit.per.is_zero() {
            return bad("rate_limit must be positive");
        }
        self.scheme.validate().map_err(RegistryError::Invalid)
    }
}

/// Registry/bind failures.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum RegistryError {
    /// A route failed validation.
    #[error("invalid route: {0}")]
    Invalid(String),
    /// No such route.
    #[error("unknown route")]
    Unknown,
    /// The bundle trying to bind is not the route's owner.
    #[error("owner mismatch")]
    OwnerMismatch,
    /// The caller is not authorized for the route's community.
    #[error("community mismatch")]
    CommunityMismatch,
}

/// Failed-verification budget per source per rate window, and the cap on
/// distinct tracked sources (overflow shares one bucket; bounded memory).
pub const MAX_FAILURES_PER_SOURCE: u32 = 20;
const MAX_TRACKED_SOURCES: usize = 4096;

struct RouteEntry {
    route: WebhookRoute,
    /// Limiter charged only by VERIFIED deliveries (`route.rate_limit`).
    window: Mutex<(Instant, u32)>,
    /// Per-source failed-verification counters: unauthenticated callers can
    /// only exhaust their OWN budget, never the verified budget.
    failures: Mutex<HashMap<String, (Instant, u32)>>,
}

impl RouteEntry {
    fn bucket<'a>(map: &HashMap<String, (Instant, u32)>, source: &'a str) -> &'a str {
        if map.contains_key(source) || map.len() < MAX_TRACKED_SOURCES {
            source
        } else {
            "~overflow"
        }
    }

    /// True when `source` has spent its failure budget (pre-auth, cheap).
    fn source_blocked(&self, source: &str, now: Instant) -> bool {
        let Ok(map) = self.failures.lock() else {
            return true;
        };
        let key = Self::bucket(&map, source);
        map.get(key).is_some_and(|(start, n)| {
            now.saturating_duration_since(*start) < self.route.rate_limit.per
                && *n >= MAX_FAILURES_PER_SOURCE
        })
    }

    fn charge_failure(&self, source: &str, now: Instant) {
        let Ok(mut map) = self.failures.lock() else {
            return;
        };
        if map.len() >= MAX_TRACKED_SOURCES {
            let per = self.route.rate_limit.per;
            map.retain(|_, (start, _)| now.saturating_duration_since(*start) < per);
        }
        let key = Self::bucket(&map, source).to_string();
        let e = map.entry(key).or_insert((now, 0));
        if now.saturating_duration_since(e.0) >= self.route.rate_limit.per {
            *e = (now, 0);
        }
        e.1 = e.1.saturating_add(1);
    }

    fn admit(&self, now: Instant) -> bool {
        // Poisoned lock => fail closed (reject).
        let Ok(mut w) = self.window.lock() else {
            return false;
        };
        if now.saturating_duration_since(w.0) >= self.route.rate_limit.per {
            *w = (now, 0);
        }
        if w.1 >= self.route.rate_limit.max {
            return false;
        }
        w.1 += 1;
        true
    }
}

/// Why a secret could not be produced.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SecretError {
    /// Deterministically absent/unusable: reject (403).
    Missing,
    /// Backend transiently unavailable: reject (503), nothing dispatched.
    Unavailable,
}

/// Resolves a route's secret from its `secret_ref`. Called once per request;
/// implementations MUST NOT cache.
pub trait WebhookSecretSource: Send + Sync {
    /// Resolve the secret for `secret_ref`.
    fn resolve(&self, secret_ref: ConnectionRef) -> BoxFut<'_, Result<SecretString, SecretError>>;
}

/// Production [`WebhookSecretSource`] over `connection_resolver`: one
/// community-bound hub call per request, zeroizing `SecretString`, no cache.
pub struct ConnectionResolverSecrets<R: ConnectionResolve>(pub Arc<R>);

impl<R: ConnectionResolve + 'static> WebhookSecretSource for ConnectionResolverSecrets<R> {
    fn resolve(&self, secret_ref: ConnectionRef) -> BoxFut<'_, Result<SecretString, SecretError>> {
        Box::pin(async move {
            let resolved = self.0.resolve(secret_ref).await.map_err(|e| match e {
                ResolveError::Transient(_) | ResolveError::CircuitOpen => SecretError::Unavailable,
                _ => SecretError::Missing,
            })?;
            // Defense in depth: never accept a secret for a different
            // connection/community than the route's secret_ref.
            if resolved.connection_id != secret_ref.connection_id
                || resolved.community_id != secret_ref.community_id
            {
                return Err(SecretError::Missing);
            }
            match resolved.secret {
                ConnectionSecret::Bearer { token } => Ok(token),
                ConnectionSecret::ApiKeyHeader { value, .. } => Ok(value),
                _ => Err(SecretError::Missing),
            }
        })
    }
}

/// Object-safe dedup: first-seen check for a namespaced key.
pub trait WebhookDedup: Send + Sync {
    /// `Ok(true)` first time, `Ok(false)` duplicate, `Err` backend failure
    /// (the caller fails closed).
    fn first_seen<'a>(&'a self, key: &'a str) -> BoxFut<'a, Result<bool, ()>>;
}

/// Production dedup: the existing Valkey `SET NX PX` [`ReplayGuard`]
/// (a concrete impl -- the trait's own async fn is not provably `Send`
/// through a generic bound, but this concrete future is).
impl WebhookDedup for RedisReplayGuard {
    fn first_seen<'a>(&'a self, key: &'a str) -> BoxFut<'a, Result<bool, ()>> {
        Box::pin(async move { self.check_and_mark(key).await.map_err(|_| ()) })
    }
}

/// Terminal outcome of one webhook request. The HTTP layer maps these to
/// status codes; every rejection variant is content-free.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WebhookOutcome {
    /// Verified and queued to the owning connector.
    Accepted,
    /// Verified but a duplicate delivery; acknowledged, not re-dispatched.
    Duplicate,
    /// Unknown route key.
    NotFound,
    /// Body over the route's limit.
    TooLarge,
    /// Over the route's rate limit.
    RateLimited,
    /// Verification failed (uniform 403).
    Rejected,
    /// Secret backend / dedup / dispatch unavailable; nothing dispatched.
    Unavailable,
}

impl WebhookOutcome {
    /// Bounded metric label.
    #[must_use]
    pub fn label(self) -> &'static str {
        match self {
            Self::Accepted => "accepted",
            Self::Duplicate => "duplicate",
            Self::NotFound => "not_found",
            Self::TooLarge => "too_large",
            Self::RateLimited => "rate_limited",
            Self::Rejected => "rejected",
            Self::Unavailable => "unavailable",
        }
    }
}

struct Bound {
    owner: String,
    tx: mpsc::Sender<Vec<u8>>,
}

/// The generic webhook ingress: registry + secret source + dedup + the
/// per-route transport channels.
pub struct WebhookIngress {
    routes: RwLock<HashMap<String, Arc<RouteEntry>>>,
    bound: RwLock<HashMap<String, Bound>>,
    secrets: Arc<dyn WebhookSecretSource>,
    dedup: Arc<dyn WebhookDedup>,
    requests: Option<prometheus::IntCounterVec>,
}

impl WebhookIngress {
    /// Builds an ingress; registers `webhook_requests_total{outcome}` on
    /// `registry` when given (a duplicate registration is ignored).
    pub fn new(
        secrets: Arc<dyn WebhookSecretSource>,
        dedup: Arc<dyn WebhookDedup>,
        registry: Option<&prometheus::Registry>,
    ) -> Self {
        let requests = registry.and_then(|r| {
            let c = prometheus::IntCounterVec::new(
                prometheus::Opts::new(
                    "webhook_requests_total",
                    "Generic webhook intake requests by terminal outcome",
                ),
                &["outcome"],
            )
            .ok()?;
            r.register(Box::new(c.clone())).ok()?;
            Some(c)
        });
        Self {
            routes: RwLock::new(HashMap::new()),
            bound: RwLock::new(HashMap::new()),
            secrets,
            dedup,
            requests,
        }
    }

    /// Registers (or replaces) a route after validation.
    ///
    /// `authorized_community` is the community the (trusted, host
    /// control-plane) caller has authorized for this registration. This API
    /// is host-internal -- it is not reachable from a guest/bundle or any
    /// HTTP route -- but is nonetheless fail-closed: a route whose
    /// `secret_ref.community_id` differs from `authorized_community` is
    /// rejected, and an existing route key can only be replaced by the
    /// community that owns it (no cross-community takeover).
    pub fn register(
        &self,
        route: WebhookRoute,
        authorized_community: i64,
    ) -> Result<(), RegistryError> {
        route.validate()?;
        if route.secret_ref.community_id != authorized_community {
            return Err(RegistryError::CommunityMismatch);
        }
        let entry = Arc::new(RouteEntry {
            window: Mutex::new((Instant::now(), 0)),
            failures: Mutex::new(HashMap::new()),
            route,
        });
        let mut g = self
            .routes
            .write()
            .map_err(|_| RegistryError::Invalid("registry poisoned".into()))?;
        if let Some(existing) = g.get(&entry.route.route_key) {
            if existing.route.secret_ref.community_id != authorized_community {
                return Err(RegistryError::CommunityMismatch);
            }
        }
        g.insert(entry.route.route_key.clone(), entry);
        Ok(())
    }

    /// Removes a route and its transport binding; only the owning community
    /// may remove it.
    pub fn unregister(
        &self,
        route_key: &str,
        authorized_community: i64,
    ) -> Result<(), RegistryError> {
        match self.entry(route_key) {
            Some(e) if e.route.secret_ref.community_id != authorized_community => {
                return Err(RegistryError::CommunityMismatch)
            }
            None => return Err(RegistryError::Unknown),
            Some(_) => {}
        }
        if let Ok(mut g) = self.routes.write() {
            g.remove(route_key);
        }
        if let Ok(mut g) = self.bound.write() {
            g.remove(route_key);
        }
        Ok(())
    }

    /// Binds a [`WebhookTransport`] for `platform` to `route_key`. Refused
    /// unless `platform` is the route's registered owner.
    pub fn bind_transport(
        &self,
        route_key: &str,
        platform: &str,
    ) -> Result<WebhookTransport, RegistryError> {
        let entry = self.entry(route_key).ok_or(RegistryError::Unknown)?;
        if entry.route.owner_bundle != platform {
            return Err(RegistryError::OwnerMismatch);
        }
        let (tx, rx) = mpsc::channel(FRAME_QUEUE_DEPTH);
        let mut g = self
            .bound
            .write()
            .map_err(|_| RegistryError::Invalid("bindings poisoned".into()))?;
        g.insert(
            route_key.to_string(),
            Bound {
                owner: platform.to_string(),
                tx,
            },
        );
        Ok(WebhookTransport {
            platform: platform.to_string(),
            source_id: opaque_source_id(route_key),
            rx,
        })
    }

    fn entry(&self, route_key: &str) -> Option<Arc<RouteEntry>> {
        self.routes.read().ok()?.get(route_key).cloned()
    }

    /// Handles one request with no source attribution (no per-source
    /// failure throttle). `now_unix` is injected for tests.
    pub async fn handle(
        &self,
        route_key: &str,
        headers: &HeaderMap,
        body: &[u8],
        now_unix: i64,
    ) -> WebhookOutcome {
        self.handle_from(route_key, None, headers, body, now_unix)
            .await
    }

    /// Handles one request. `source` is an opaque caller identity (peer IP)
    /// used ONLY to throttle that source's failed verifications.
    pub async fn handle_from(
        &self,
        route_key: &str,
        source: Option<&str>,
        headers: &HeaderMap,
        body: &[u8],
        now_unix: i64,
    ) -> WebhookOutcome {
        let outcome = self
            .handle_inner(route_key, source, headers, body, now_unix)
            .await;
        if let Some(c) = &self.requests {
            c.with_label_values(&[outcome.label()]).inc();
        }
        // Never log the route key, headers, body or secret.
        tracing::debug!(outcome = outcome.label(), "webhook intake");
        outcome
    }

    async fn handle_inner(
        &self,
        route_key: &str,
        source: Option<&str>,
        headers: &HeaderMap,
        body: &[u8],
        now_unix: i64,
    ) -> WebhookOutcome {
        let Some(entry) = self.entry(route_key) else {
            return WebhookOutcome::NotFound;
        };
        let route = &entry.route;
        let now = Instant::now();
        // Pre-auth: a source that already burned its failure budget is
        // refused cheaply, before any secret fetch. It can only starve itself.
        if source.is_some_and(|s| entry.source_blocked(s, now)) {
            return WebhookOutcome::RateLimited;
        }
        let fail = |o: WebhookOutcome| {
            if let Some(s) = source {
                entry.charge_failure(s, now);
            }
            o
        };
        if body.len() > route.body_limit {
            return WebhookOutcome::TooLarge;
        }
        let prepared =
            match route
                .scheme
                .prepare(headers, body, route.replay_window.as_secs(), now_unix)
            {
                Ok(p) => p,
                Err(_) => return fail(WebhookOutcome::Rejected),
            };
        // Secret by ROUTE KEY's secret_ref, per call, scoped to this block so
        // it zeroizes on drop and never crosses an await after the check.
        let verified = {
            let secret = match self.secrets.resolve(route.secret_ref).await {
                Ok(s) => s,
                Err(SecretError::Missing) => return fail(WebhookOutcome::Rejected),
                Err(SecretError::Unavailable) => return WebhookOutcome::Unavailable,
            };
            prepared.check(secret.expose())
        };
        if verified.is_err() {
            return fail(WebhookOutcome::Rejected);
        }
        // Verified traffic is charged to the route's own budget, which
        // unauthenticated requests can never spend.
        if !entry.admit(now) {
            return WebhookOutcome::RateLimited;
        }
        // Reserve queue capacity and re-check the owner binding BEFORE
        // marking dedup: a delivery we cannot dispatch must stay
        // redeliverable, never be recorded as seen.
        let permit = {
            let tx = {
                let Ok(g) = self.bound.read() else {
                    return WebhookOutcome::Unavailable;
                };
                match g.get(route_key) {
                    // Owner re-check at dispatch: a route re-registered to a
                    // different owner is never driven into the old binding.
                    Some(b) if b.owner == route.owner_bundle => b.tx.clone(),
                    _ => return WebhookOutcome::Unavailable,
                }
            };
            match tx.try_reserve_owned() {
                Ok(p) => p,
                Err(_) => return WebhookOutcome::Unavailable,
            }
        };
        // Dedup (keyed on the digest of the verified SIGNED message) only
        // AFTER the signature verifies, so an unauthenticated caller cannot
        // poison the dedup namespace. Dropping `permit` on Duplicate/Err
        // releases the reserved slot.
        let key = format!("wh:{}:{}", route.route_key, prepared.dedup_id);
        match self.dedup.first_seen(&key).await {
            Ok(true) => {}
            Ok(false) => return WebhookOutcome::Duplicate,
            Err(()) => return WebhookOutcome::Unavailable,
        }
        permit.send(body.to_vec());
        WebhookOutcome::Accepted
    }

    /// Per-route hard body cap used by the HTTP layer's coarse limit.
    #[must_use]
    pub fn body_limit_for(&self, route_key: &str) -> Option<usize> {
        self.entry(route_key).map(|e| e.route.body_limit)
    }
}

/// Opaque `source_id` for a route: a hash, never the key or any name/PII.
#[must_use]
pub fn opaque_source_id(route_key: &str) -> String {
    let d = Sha256::digest(route_key.as_bytes());
    format!("wh-{}", &hex_encode(&d)[..16])
}

/// [`Transport`] yielding verified webhook bodies for one route to its owning
/// connector. Stateless: a dropped frame is simply lost (the sender already
/// got its 2xx and may redeliver).
pub struct WebhookTransport {
    platform: String,
    source_id: String,
    rx: mpsc::Receiver<Vec<u8>>,
}

impl Transport for WebhookTransport {
    fn platform(&self) -> &str {
        &self.platform
    }

    async fn connect(&mut self) -> Result<ConnectionCtx, TransportError> {
        Ok(ConnectionCtx {
            platform: self.platform.clone(),
            source_id: self.source_id.clone(),
            connection_id: uuid::Uuid::new_v4().to_string(),
            session_token: None,
        })
    }

    async fn send_handshake(&mut self, handshake: Handshake) -> Result<(), TransportError> {
        // Webhooks have no outbound handshake; a connector asking for secret
        // substitution here is a misconfiguration -- refuse loudly.
        if handshake.secret_refs.is_empty() && handshake.bytes.is_empty() {
            Ok(())
        } else {
            Err(TransportError::Handshake(
                "webhook transport takes no handshake".into(),
            ))
        }
    }

    async fn next_frame(&mut self) -> Result<Option<Vec<u8>>, TransportError> {
        Ok(self.rx.recv().await)
    }

    async fn close(&mut self, _reason: &str) {
        self.rx.close();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::connector::webhook_verify::{
        Encoding, HmacSpec, SignedTemplate, StripeSpec, TimestampFormat,
    };
    use axum::http::HeaderName;
    use hmac::{Hmac, Mac};
    use ring::signature::{Ed25519KeyPair, KeyPair};

    const NOW: i64 = 1_800_000_000;
    const SECRET: &str = "whsec_route_secret";

    struct FakeSecrets {
        secret: Option<String>,
        calls: Mutex<u32>,
        fail: Option<SecretError>,
    }
    impl WebhookSecretSource for FakeSecrets {
        fn resolve(&self, _r: ConnectionRef) -> BoxFut<'_, Result<SecretString, SecretError>> {
            *self.calls.lock().unwrap() += 1;
            let out = match (&self.fail, &self.secret) {
                (Some(e), _) => Err(*e),
                (None, Some(s)) => Ok(SecretString::new(s.clone())),
                (None, None) => Err(SecretError::Missing),
            };
            Box::pin(async move { out })
        }
    }

    #[derive(Default)]
    struct MemDedup {
        seen: Mutex<std::collections::HashSet<String>>,
        broken: bool,
    }
    impl WebhookDedup for MemDedup {
        fn first_seen<'a>(&'a self, key: &'a str) -> BoxFut<'a, Result<bool, ()>> {
            let r = if self.broken {
                Err(())
            } else {
                Ok(self.seen.lock().unwrap().insert(key.to_string()))
            };
            Box::pin(async move { r })
        }
    }

    fn hmac_hex(secret: &str, msg: &[u8]) -> String {
        let mut m = Hmac::<sha2::Sha256>::new_from_slice(secret.as_bytes()).unwrap();
        m.update(msg);
        hex_encode(&m.finalize().into_bytes())
    }

    fn route(key: &str, owner: &str, scheme: Scheme) -> WebhookRoute {
        WebhookRoute {
            route_key: key.into(),
            owner_bundle: owner.into(),
            scheme,
            secret_ref: ConnectionRef {
                community_id: 7,
                connection_id: 9,
            },
            replay_window: Duration::from_secs(300),
            rate_limit: RateLimit {
                max: 100,
                per: Duration::from_secs(60),
            },
            body_limit: 1024,
        }
    }

    fn generic_hmac() -> Scheme {
        Scheme::HmacSha256(HmacSpec {
            signature_header: HeaderName::from_static("x-sig"),
            timestamp_header: HeaderName::from_static("x-ts"),
            id_header: None,
            template: SignedTemplate::parse("{ts}.{body}").unwrap(),
            signature_prefix: String::new(),
            encoding: Encoding::Hex,
            timestamp_format: TimestampFormat::UnixSeconds,
        })
    }

    fn ingress(secret: Option<&str>) -> (WebhookIngress, Arc<FakeSecrets>) {
        let s = Arc::new(FakeSecrets {
            secret: secret.map(str::to_string),
            calls: Mutex::new(0),
            fail: None,
        });
        let ing = WebhookIngress::new(s.clone(), Arc::new(MemDedup::default()), None);
        (ing, s)
    }

    fn signed_headers(body: &[u8]) -> HeaderMap {
        let msg = [format!("{NOW}.").as_bytes(), body].concat();
        let mut h = HeaderMap::new();
        h.insert("x-ts", NOW.to_string().parse().unwrap());
        h.insert("x-sig", hmac_hex(SECRET, &msg).parse().unwrap());
        h
    }

    const KEY: &str = "route-key-aaaa1111";

    #[tokio::test]
    async fn valid_request_is_dispatched_to_the_owner_as_the_exact_verified_body() {
        let (ing, _) = ingress(Some(SECRET));
        ing.register(route(KEY, "acme", generic_hmac()), 7).unwrap();
        let mut t = ing.bind_transport(KEY, "acme").unwrap();
        let body = b"{\"hello\":1}";
        let out = ing.handle(KEY, &signed_headers(body), body, NOW).await;
        assert_eq!(out, WebhookOutcome::Accepted);
        assert_eq!(t.next_frame().await.unwrap().unwrap(), body);
        // The guest-visible ctx carries no secret/route key.
        let ctx = t.connect().await.unwrap();
        let dump = format!("{ctx:?}");
        assert!(!dump.contains(SECRET));
        assert!(!dump.contains(KEY));
        assert!(ctx.source_id.starts_with("wh-"));
        assert_eq!(ctx.session_token, None);
    }

    #[tokio::test]
    async fn tampered_body_is_rejected_and_nothing_is_dispatched() {
        let (ing, _) = ingress(Some(SECRET));
        ing.register(route(KEY, "acme", generic_hmac()), 7).unwrap();
        let mut t = ing.bind_transport(KEY, "acme").unwrap();
        let h = signed_headers(b"original");
        let out = ing.handle(KEY, &h, b"tampered", NOW).await;
        assert_eq!(out, WebhookOutcome::Rejected);
        assert!(t.rx.try_recv().is_err());
    }

    #[tokio::test]
    async fn replayed_request_is_deduplicated_and_not_redispatched() {
        let (ing, _) = ingress(Some(SECRET));
        ing.register(route(KEY, "acme", generic_hmac()), 7).unwrap();
        let mut t = ing.bind_transport(KEY, "acme").unwrap();
        let h = signed_headers(b"x");
        assert_eq!(
            ing.handle(KEY, &h, b"x", NOW).await,
            WebhookOutcome::Accepted
        );
        assert_eq!(
            ing.handle(KEY, &h, b"x", NOW).await,
            WebhookOutcome::Duplicate
        );
        assert!(t.rx.try_recv().is_ok());
        assert!(t.rx.try_recv().is_err());
    }

    #[tokio::test]
    async fn stale_timestamp_is_rejected_before_the_secret_is_fetched() {
        let (ing, s) = ingress(Some(SECRET));
        ing.register(route(KEY, "acme", generic_hmac()), 7).unwrap();
        let _t = ing.bind_transport(KEY, "acme").unwrap();
        let h = signed_headers(b"x");
        let out = ing.handle(KEY, &h, b"x", NOW + 10_000).await;
        assert_eq!(out, WebhookOutcome::Rejected);
        assert_eq!(*s.calls.lock().unwrap(), 0);
    }

    #[tokio::test]
    async fn missing_secret_fails_closed() {
        let (ing, _) = ingress(None);
        ing.register(route(KEY, "acme", generic_hmac()), 7).unwrap();
        let mut t = ing.bind_transport(KEY, "acme").unwrap();
        let out = ing.handle(KEY, &signed_headers(b"x"), b"x", NOW).await;
        assert_eq!(out, WebhookOutcome::Rejected);
        assert!(t.rx.try_recv().is_err());
    }

    #[tokio::test]
    async fn empty_secret_fails_closed() {
        let (ing, _) = ingress(Some(""));
        ing.register(route(KEY, "acme", generic_hmac()), 7).unwrap();
        let _t = ing.bind_transport(KEY, "acme").unwrap();
        let out = ing.handle(KEY, &signed_headers(b"x"), b"x", NOW).await;
        assert_eq!(out, WebhookOutcome::Rejected);
    }

    #[tokio::test]
    async fn secret_backend_outage_is_unavailable_and_dispatches_nothing() {
        let s = Arc::new(FakeSecrets {
            secret: Some(SECRET.into()),
            calls: Mutex::new(0),
            fail: Some(SecretError::Unavailable),
        });
        let ing = WebhookIngress::new(s, Arc::new(MemDedup::default()), None);
        ing.register(route(KEY, "acme", generic_hmac()), 7).unwrap();
        let mut t = ing.bind_transport(KEY, "acme").unwrap();
        let out = ing.handle(KEY, &signed_headers(b"x"), b"x", NOW).await;
        assert_eq!(out, WebhookOutcome::Unavailable);
        assert!(t.rx.try_recv().is_err());
    }

    #[tokio::test]
    async fn dedup_backend_failure_fails_closed() {
        let s = Arc::new(FakeSecrets {
            secret: Some(SECRET.into()),
            calls: Mutex::new(0),
            fail: None,
        });
        let d = Arc::new(MemDedup {
            broken: true,
            ..Default::default()
        });
        let ing = WebhookIngress::new(s, d, None);
        ing.register(route(KEY, "acme", generic_hmac()), 7).unwrap();
        let mut t = ing.bind_transport(KEY, "acme").unwrap();
        let out = ing.handle(KEY, &signed_headers(b"x"), b"x", NOW).await;
        assert_eq!(out, WebhookOutcome::Unavailable);
        assert!(t.rx.try_recv().is_err());
    }

    #[tokio::test]
    async fn wrong_owner_bind_is_refused_and_reowned_route_is_not_driven() {
        let (ing, _) = ingress(Some(SECRET));
        ing.register(route(KEY, "acme", generic_hmac()), 7).unwrap();
        assert_eq!(
            ing.bind_transport(KEY, "mallory").err(),
            Some(RegistryError::OwnerMismatch)
        );
        assert_eq!(
            ing.bind_transport("nope-nope-nope", "acme").err(),
            Some(RegistryError::Unknown)
        );
        // Bound to acme, then the route is re-registered to another owner:
        // dispatch must refuse rather than feed acme's transport.
        let mut t = ing.bind_transport(KEY, "acme").unwrap();
        ing.register(route(KEY, "other", generic_hmac()), 7)
            .unwrap();
        let out = ing.handle(KEY, &signed_headers(b"x"), b"x", NOW).await;
        assert_eq!(out, WebhookOutcome::Unavailable);
        assert!(t.rx.try_recv().is_err());
    }

    #[tokio::test]
    async fn routes_are_isolated_by_key_scheme_and_secret_ref() {
        let (ing, _) = ingress(Some(SECRET));
        ing.register(route(KEY, "acme", generic_hmac()), 7).unwrap();
        ing.register(
            route(
                "route-key-bbbb2222",
                "globex",
                Scheme::Stripe(StripeSpec {
                    signature_header: HeaderName::from_static("stripe-signature"),
                }),
            ),
            7,
        )
        .unwrap();
        let _a = ing.bind_transport(KEY, "acme").unwrap();
        let mut b = ing.bind_transport("route-key-bbbb2222", "globex").unwrap();
        // acme's signed request replayed against globex's route (different
        // scheme) is rejected; nothing reaches globex.
        let out = ing
            .handle("route-key-bbbb2222", &signed_headers(b"x"), b"x", NOW)
            .await;
        assert_eq!(out, WebhookOutcome::Rejected);
        assert!(b.rx.try_recv().is_err());
    }

    #[tokio::test]
    async fn oversized_body_is_rejected_before_any_secret_fetch() {
        let (ing, s) = ingress(Some(SECRET));
        ing.register(route(KEY, "acme", generic_hmac()), 7).unwrap();
        let _t = ing.bind_transport(KEY, "acme").unwrap();
        let big = vec![b'a'; 1025];
        let out = ing.handle(KEY, &signed_headers(&big), &big, NOW).await;
        assert_eq!(out, WebhookOutcome::TooLarge);
        assert_eq!(*s.calls.lock().unwrap(), 0);
    }

    #[tokio::test]
    async fn over_rate_requests_are_rejected() {
        let (ing, s) = ingress(Some(SECRET));
        let mut r = route(KEY, "acme", generic_hmac());
        r.rate_limit = RateLimit {
            max: 2,
            per: Duration::from_secs(3600),
        };
        ing.register(r, 7).unwrap();
        let _t = ing.bind_transport(KEY, "acme").unwrap();
        let mut outs = Vec::new();
        for i in 0..4 {
            let body = format!("b{i}");
            let h = signed_headers(body.as_bytes());
            outs.push(ing.handle(KEY, &h, body.as_bytes(), NOW).await);
        }
        assert_eq!(
            outs,
            vec![
                WebhookOutcome::Accepted,
                WebhookOutcome::Accepted,
                WebhookOutcome::RateLimited,
                WebhookOutcome::RateLimited
            ]
        );
        // Verified-only charge: every request still resolved the secret.
        assert_eq!(*s.calls.lock().unwrap(), 4);
    }

    #[tokio::test]
    async fn unbound_route_and_unknown_route_are_loud() {
        let (ing, _) = ingress(Some(SECRET));
        ing.register(route(KEY, "acme", generic_hmac()), 7).unwrap();
        assert_eq!(
            ing.handle(KEY, &signed_headers(b"x"), b"x", NOW).await,
            WebhookOutcome::Unavailable
        );
        assert_eq!(
            ing.handle("unknown-route-key", &HeaderMap::new(), b"", NOW)
                .await,
            WebhookOutcome::NotFound
        );
    }

    #[tokio::test]
    async fn ed25519_route_end_to_end_and_secret_is_the_public_key() {
        let kp = Ed25519KeyPair::from_seed_unchecked(&[3u8; 32]).unwrap();
        let pubkey = hex_encode(kp.public_key().as_ref());
        let s = Arc::new(FakeSecrets {
            secret: Some(pubkey),
            calls: Mutex::new(0),
            fail: None,
        });
        let ing = WebhookIngress::new(s, Arc::new(MemDedup::default()), None);
        let scheme = Scheme::Ed25519(crate::connector::webhook_verify::Ed25519Spec {
            signature_header: HeaderName::from_static("x-sig"),
            timestamp_header: HeaderName::from_static("x-ts"),
            id_header: None,
            template: SignedTemplate::parse("{ts}{body}").unwrap(),
            encoding: Encoding::Hex,
            timestamp_format: TimestampFormat::UnixSeconds,
        });
        ing.register(route(KEY, "acme", scheme), 7).unwrap();
        let mut t = ing.bind_transport(KEY, "acme").unwrap();
        let body = b"payload";
        let msg = [NOW.to_string().as_bytes(), body].concat();
        let mut h = HeaderMap::new();
        h.insert("x-ts", NOW.to_string().parse().unwrap());
        h.insert("x-sig", hex_encode(kp.sign(&msg).as_ref()).parse().unwrap());
        assert_eq!(
            ing.handle(KEY, &h, body, NOW).await,
            WebhookOutcome::Accepted
        );
        assert_eq!(t.next_frame().await.unwrap().unwrap(), body);
        assert_eq!(
            ing.handle(KEY, &h, b"payloaD", NOW).await,
            WebhookOutcome::Rejected
        );
    }

    #[test]
    fn registration_validation_rejects_bad_routes() {
        let (ing, _) = ingress(Some(SECRET));
        let mut r = route("short", "acme", generic_hmac());
        assert!(ing.register(r.clone(), 7).is_err());
        r = route(KEY, "", generic_hmac());
        assert!(ing.register(r, 7).is_err());
        let mut r = route(KEY, "acme", generic_hmac());
        r.replay_window = Duration::from_secs(10_000);
        assert!(ing.register(r, 7).is_err());
        let mut r = route(KEY, "acme", generic_hmac());
        r.body_limit = HARD_MAX_BODY_BYTES + 1;
        assert!(ing.register(r, 7).is_err());
        let mut r = route(KEY, "acme", generic_hmac());
        r.rate_limit.max = 0;
        assert!(ing.register(r, 7).is_err());
        let r = route("bad key with spaces!", "acme", generic_hmac());
        assert!(ing.register(r, 7).is_err());
    }

    #[tokio::test]
    async fn transport_handshake_refuses_secret_substitution_and_closes() {
        let (ing, _) = ingress(Some(SECRET));
        ing.register(route(KEY, "acme", generic_hmac()), 7).unwrap();
        let mut t = ing.bind_transport(KEY, "acme").unwrap();
        assert_eq!(t.platform(), "acme");
        assert!(t
            .send_handshake(Handshake {
                bytes: vec![],
                secret_refs: vec![]
            })
            .await
            .is_ok());
        assert!(t
            .send_handshake(Handshake {
                bytes: vec![],
                secret_refs: vec![("a".into(), "b".into())]
            })
            .await
            .is_err());
        t.close("test").await;
        assert_eq!(t.next_frame().await.unwrap(), None);
    }

    struct Resolver(ConnectionSecretKind, i64, i64);
    enum ConnectionSecretKind {
        Bearer,
        Basic,
    }
    // Trait signature is `-> impl Future + Send`; mirroring it keeps the fake exact.
    #[allow(clippy::manual_async_fn)]
    impl ConnectionResolve for Resolver {
        fn resolve(
            &self,
            r: ConnectionRef,
        ) -> impl Future<Output = Result<connection_resolver::ResolvedConnection, ResolveError>> + Send
        {
            let secret = match self.0 {
                ConnectionSecretKind::Bearer => ConnectionSecret::Bearer {
                    token: SecretString::new("k".into()),
                },
                ConnectionSecretKind::Basic => ConnectionSecret::Basic {
                    username: SecretString::new("u".into()),
                    password: SecretString::new("p".into()),
                },
            };
            let out = connection_resolver::ResolvedConnection {
                connection_id: self.2,
                community_id: self.1,
                provider: "x".into(),
                kind: connection_resolver::ConnectionKind::ExternalHttp,
                endpoint: connection_resolver::Endpoint::default(),
                secret,
                expires_at: None,
            };
            let _ = r;
            async move { Ok(out) }
        }
        fn list_bindings(
            &self,
            _p: &str,
            _c: Option<&str>,
            _a: i64,
            _l: u32,
        ) -> impl Future<Output = Result<connection_resolver::BindingPage, ResolveError>> + Send
        {
            async { Err(ResolveError::NotFound) }
        }
    }

    #[tokio::test]
    async fn resolver_secrets_accept_only_matching_bearer_connections() {
        let r = ConnectionRef {
            community_id: 7,
            connection_id: 9,
        };
        let ok = ConnectionResolverSecrets(Arc::new(Resolver(ConnectionSecretKind::Bearer, 7, 9)));
        assert_eq!(ok.resolve(r).await.unwrap().expose(), "k");
        // Resolved a different community's connection: refused.
        let other =
            ConnectionResolverSecrets(Arc::new(Resolver(ConnectionSecretKind::Bearer, 8, 9)));
        assert_eq!(other.resolve(r).await.err(), Some(SecretError::Missing));
        // Wrong secret kind: refused.
        let basic =
            ConnectionResolverSecrets(Arc::new(Resolver(ConnectionSecretKind::Basic, 7, 9)));
        assert_eq!(basic.resolve(r).await.err(), Some(SecretError::Missing));
    }

    #[test]
    fn source_id_is_opaque() {
        let id = opaque_source_id(KEY);
        assert!(id.starts_with("wh-") && !id.contains(KEY));
        assert_eq!(id, opaque_source_id(KEY));
    }

    #[tokio::test]
    async fn dispatch_failure_does_not_mark_dedup_so_redelivery_is_accepted() {
        let (ing, _) = ingress(Some(SECRET));
        let mut r = route(KEY, "acme", generic_hmac());
        r.rate_limit.max = 10_000;
        ing.register(r, 7).unwrap();
        let h = signed_headers(b"x");
        // Unbound: dispatch impossible -> 503, dedup untouched.
        assert_eq!(
            ing.handle(KEY, &h, b"x", NOW).await,
            WebhookOutcome::Unavailable
        );
        let mut t = ing.bind_transport(KEY, "acme").unwrap();
        // Redelivery now succeeds (not Duplicate).
        assert_eq!(
            ing.handle(KEY, &h, b"x", NOW).await,
            WebhookOutcome::Accepted
        );
        assert!(t.rx.try_recv().is_ok());
        // Queue full also leaves dedup unmarked.
        for i in 0..FRAME_QUEUE_DEPTH {
            let b = format!("f{i}");
            let hh = signed_headers(b.as_bytes());
            assert_eq!(
                ing.handle(KEY, &hh, b.as_bytes(), NOW).await,
                WebhookOutcome::Accepted
            );
        }
        let hf = signed_headers(b"full");
        assert_eq!(
            ing.handle(KEY, &hf, b"full", NOW).await,
            WebhookOutcome::Unavailable
        );
        while t.rx.try_recv().is_ok() {}
        assert_eq!(
            ing.handle(KEY, &hf, b"full", NOW).await,
            WebhookOutcome::Accepted
        );
    }

    #[tokio::test]
    async fn unauthenticated_failures_cannot_block_a_verified_delivery() {
        let (ing, _) = ingress(Some(SECRET));
        let mut r = route(KEY, "acme", generic_hmac());
        r.rate_limit = RateLimit {
            max: 5,
            per: Duration::from_secs(3600),
        };
        ing.register(r, 7).unwrap();
        let mut t = ing.bind_transport(KEY, "acme").unwrap();
        let bad = signed_headers(b"orig");
        for _ in 0..MAX_FAILURES_PER_SOURCE + 10 {
            let _ = ing
                .handle_from(KEY, Some("1.1.1.1"), &bad, b"forged", NOW)
                .await;
        }
        // The attacker is now throttled...
        assert_eq!(
            ing.handle_from(KEY, Some("1.1.1.1"), &bad, b"forged", NOW)
                .await,
            WebhookOutcome::RateLimited
        );
        // ...but a different source's verified delivery still goes through
        // and the route's verified budget is untouched.
        let good = signed_headers(b"real");
        assert_eq!(
            ing.handle_from(KEY, Some("2.2.2.2"), &good, b"real", NOW)
                .await,
            WebhookOutcome::Accepted
        );
        assert!(t.rx.try_recv().is_ok());
    }

    #[test]
    fn cross_community_register_and_takeover_are_rejected() {
        let (ing, _) = ingress(Some(SECRET));
        // secret_ref community is 7; caller authorized for 8.
        assert_eq!(
            ing.register(route(KEY, "acme", generic_hmac()), 8).err(),
            Some(RegistryError::CommunityMismatch)
        );
        ing.register(route(KEY, "acme", generic_hmac()), 7).unwrap();
        // Another community registers a route reusing the key: refused.
        let mut other = route(KEY, "evil", generic_hmac());
        other.secret_ref.community_id = 8;
        assert_eq!(
            ing.register(other, 8).err(),
            Some(RegistryError::CommunityMismatch)
        );
        assert_eq!(
            ing.unregister(KEY, 8).err(),
            Some(RegistryError::CommunityMismatch)
        );
        assert!(ing.unregister(KEY, 7).is_ok());
        assert_eq!(ing.unregister(KEY, 7).err(), Some(RegistryError::Unknown));
    }
}
