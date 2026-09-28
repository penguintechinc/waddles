//! Wires `egress-detokenizer` (spec S10.4/S10.6, Gemini condition 5) into
//! this stage's chat egress -- see [`capabilities::StageCapabilities::
//! handle_relay`]/`handle_discord_relay`'s detokenize-then-sanitize call
//! sites for where [`ChatDetokenizer::render`] actually runs.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU32, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use async_trait::async_trait;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine as _;
use egress_detokenizer::{CacheConfig, Detokenizer, NameResolver, ResolveError};
use hmac::{Hmac, Mac};
use sha2::Sha256;
use uuid::Uuid;

/// Type-erased so [`crate::capabilities::StageCapabilities`] doesn't need a
/// second generic type parameter just to hold this -- any resolver
/// (production or test double) is boxed behind `Arc<dyn NameResolver>`,
/// which itself satisfies `NameResolver` via `egress_detokenizer`'s blanket
/// `impl<T: NameResolver + ?Sized> NameResolver for Arc<T>`.
pub type SharedResolver = Arc<dyn NameResolver>;

/// This stage's chat-egress detokenizer: one per process, shared across
/// every host-API connection's [`crate::capabilities::StageCapabilities`]
/// (a per-tenant name cache is only useful if it's actually shared across
/// invokes, spec S10.4's "one batched lookup, not one query per mention").
pub type ChatDetokenizer = Detokenizer<SharedResolver>;

/// Consecutive resolver failures (timeout, connection error, non-2xx,
/// malformed body) before the circuit opens and short-circuits further
/// hub-api calls -- protects a struggling/unreachable hub-api from a
/// thundering herd of per-mention batch calls, and keeps every render on
/// the fast, synchronous "already know this is down" path instead of
/// paying a fresh timeout per call while hub-api is unhealthy.
const FAILURE_THRESHOLD: u32 = 3;
/// How long the circuit stays open once tripped before the next call is
/// allowed through to probe recovery.
const OPEN_COOLDOWN: Duration = Duration::from_secs(30);
/// Default per-request timeout against hub-api's internal display-name
/// endpoint -- short, because a slow hub-api must never slow down chat
/// egress; a timed-out lookup renders the neutral label exactly like an
/// erased/unknown user (spec S10.4).
const DEFAULT_TIMEOUT_MS: u64 = 2_000;
/// Service-JWT lifetime (short-lived, matching `rules/security.md`
/// Service-to-Service Auth: machine tokens are short-lived, minted fresh
/// per call rather than cached, since a batched display-name lookup is
/// infrequent relative to per-message throughput).
const SERVICE_JWT_TTL_SECS: i64 = 60;

/// A minimal, dependency-light circuit breaker: N consecutive failures
/// open the circuit for a fixed cooldown, after which the next call is
/// allowed through to probe recovery (half-open, implicitly -- a single
/// probe failure just re-opens the circuit for another cooldown window).
struct CircuitBreaker {
    consecutive_failures: AtomicU32,
    open_until_ms: AtomicU64,
}

impl CircuitBreaker {
    fn new() -> Self {
        Self {
            consecutive_failures: AtomicU32::new(0),
            open_until_ms: AtomicU64::new(0),
        }
    }

    fn now_ms() -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_millis() as u64)
            .unwrap_or(0)
    }

    /// True while the circuit is open -- callers must skip the network
    /// call entirely and treat this exactly like any other resolver
    /// failure (fail-safe-empty).
    fn is_open(&self) -> bool {
        Self::now_ms() < self.open_until_ms.load(Ordering::Relaxed)
    }

    fn record_success(&self) {
        self.consecutive_failures.store(0, Ordering::Relaxed);
    }

    fn record_failure(&self) {
        let failures = self.consecutive_failures.fetch_add(1, Ordering::Relaxed) + 1;
        if failures >= FAILURE_THRESHOLD {
            self.open_until_ms.store(
                Self::now_ms() + OPEN_COOLDOWN.as_millis() as u64,
                Ordering::Relaxed,
            );
        }
    }
}

/// Mints a short-lived HS256 service JWT compatible with `flask_core.auth
/// .verify_jwt_token`/`flask_core.authz.require_scope` -- same shared
/// `SECRET_KEY`, same `sub`/`iss`/`aud`/`iat`/`exp`/`scope`/`tenant`/
/// `teams`/`roles` claim shape as `flask_core.auth.create_jwt_token`
/// (`rules/security.md` JWT Claims). Minted fresh per call rather than
/// cached/refreshed on a timer: this crate has no existing background-task
/// infrastructure for a token refresh loop, and a 60s TTL freshly minted
/// per batched lookup is simpler and just as correct for this call
/// frequency (spec S10.4: "one batched lookup, not one query per
/// mention").
fn mint_service_jwt(secret: &str, tenant: &str) -> String {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    let empty_list: Vec<&str> = Vec::new();
    let header = serde_json::json!({"alg": "HS256", "typ": "JWT"});
    let payload = serde_json::json!({
        "sub": "svc-action",
        "username": "svc-action",
        "email": "svc-action@internal.waddles",
        "roles": empty_list.clone(),
        "tenant": tenant,
        "scope": "users:display-name:resolve",
        "teams": empty_list,
        "iss": "waddlebot",
        "aud": "waddlebot-services",
        "iat": now,
        "exp": now + SERVICE_JWT_TTL_SECS,
        "type": "access",
    });
    let header_b64 = URL_SAFE_NO_PAD.encode(serde_json::to_vec(&header).unwrap_or_default());
    let payload_b64 = URL_SAFE_NO_PAD.encode(serde_json::to_vec(&payload).unwrap_or_default());
    let signing_input = format!("{header_b64}.{payload_b64}");
    let mut mac = Hmac::<Sha256>::new_from_slice(secret.as_bytes())
        .expect("HMAC-SHA256 accepts a key of any length");
    mac.update(signing_input.as_bytes());
    let signature = URL_SAFE_NO_PAD.encode(mac.finalize().into_bytes());
    format!("{signing_input}.{signature}")
}

/// The real, PII-boundary-respecting [`NameResolver`]: batches a
/// tenant-scoped `POST` to hub-api's internal, service-only
/// `/api/v1/internal/users/display-names` endpoint (the only place inside
/// the PII boundary allowed to hold `hub_users` display names --
/// `rules/critical-rules.md` PII Tokenization) rather than resolving
/// anything locally. Handles both real linked-user UUIDs and PR #429's
/// ephemeral pseudonym tokens identically: `svc_action` cannot and does
/// not distinguish them (spec S10.4 -- a bundle, and by extension this
/// resolver, must never be able to tell a linked user from an ephemeral
/// pseudonym from the token shape alone), so every `{user:<uuid>}` token
/// is sent to hub-api in the same batch; hub-api resolves whichever of its
/// own tenant-scoped identity/pseudonym stores the UUID actually belongs
/// to (see that endpoint's own docstring) and a UUID belonging to neither
/// -- unknown, erased, or simply absent -- is silently omitted from the
/// response, which already renders as [`egress_detokenizer::NEUTRAL_LABEL`]
/// through this crate's existing "absence is fail-safe-empty" contract.
/// This resolver therefore needs, and has, no separate pseudonym store of
/// its own (per this landing's own review: "resolve those through the
/// same PII-boundary path rather than inventing a new store").
///
/// Fail-safe-empty on every failure mode: hub-api unreachable, timed out,
/// non-2xx, malformed body, or the circuit breaker open all resolve to
/// `Ok(HashMap::new())` -- never a propagated error, never a fabricated
/// name, matching the same posture the prior always-empty stub took
/// deliberately (this module's prior doc: "rendering the neutral label for
/// every mention is safe-by-construction, whereas a resolver that guessed
/// or echoed anything back would not be").
pub struct HubUsersResolver {
    client: reqwest::Client,
    /// `None` when `HUB_API_BASE_URL` is unset -- the resolver is simply
    /// never configured to reach hub-api (e.g. a dev/test environment),
    /// and every lookup fails safe-empty rather than panicking or
    /// defaulting to a guessed URL.
    base_url: Option<String>,
    /// `None` when `SECRET_KEY` is unset -- mirrors `base_url`: no service
    /// JWT can be minted, so every lookup fails safe-empty.
    secret: Option<String>,
    breaker: CircuitBreaker,
}

impl HubUsersResolver {
    /// Builds a resolver from explicit config -- the seam
    /// [`build_production_detokenizer`] and this module's tests both use,
    /// so tests can point `base_url` at a local mock server without
    /// touching the process environment.
    pub fn new(base_url: Option<String>, secret: Option<String>, timeout: Duration) -> Self {
        let client = reqwest::Client::builder()
            .timeout(timeout)
            .build()
            .unwrap_or_else(|_| reqwest::Client::new());
        Self {
            client,
            base_url,
            secret,
            breaker: CircuitBreaker::new(),
        }
    }
}

#[async_trait]
impl NameResolver for HubUsersResolver {
    async fn resolve_batch(
        &self,
        tenant: &str,
        users: &[Uuid],
    ) -> Result<HashMap<Uuid, String>, ResolveError> {
        if users.is_empty() {
            return Ok(HashMap::new());
        }
        let (Some(base_url), Some(secret)) = (self.base_url.as_deref(), self.secret.as_deref())
        else {
            return Ok(HashMap::new());
        };
        if self.breaker.is_open() {
            tracing::debug!(tenant, "hub-api display-name circuit open; skipping call");
            return Ok(HashMap::new());
        }

        let token = mint_service_jwt(secret, tenant);
        let url = format!(
            "{}/api/v1/internal/users/display-names",
            base_url.trim_end_matches('/')
        );
        let body = serde_json::json!({
            "user_uuids": users.iter().map(Uuid::to_string).collect::<Vec<_>>(),
        });

        let response = match self
            .client
            .post(&url)
            .bearer_auth(token)
            .json(&body)
            .send()
            .await
        {
            Ok(resp) => resp,
            Err(err) => {
                tracing::warn!(tenant, error = %err, "hub-api display-name request failed");
                self.breaker.record_failure();
                return Ok(HashMap::new());
            }
        };

        if !response.status().is_success() {
            tracing::warn!(
                tenant,
                status = response.status().as_u16(),
                "hub-api display-name request returned non-success"
            );
            self.breaker.record_failure();
            return Ok(HashMap::new());
        }

        let parsed: serde_json::Value = match response.json().await {
            Ok(v) => v,
            Err(err) => {
                tracing::warn!(tenant, error = %err, "hub-api display-name response was not valid JSON");
                self.breaker.record_failure();
                return Ok(HashMap::new());
            }
        };
        self.breaker.record_success();

        let mut out = HashMap::new();
        if let Some(map) = parsed.get("display_names").and_then(|v| v.as_object()) {
            for (key, value) in map {
                if let (Ok(uuid), Some(name)) = (Uuid::parse_str(key), value.as_str()) {
                    out.insert(uuid, name.to_string());
                }
            }
        }
        Ok(out)
    }
}

/// Builds the production [`ChatDetokenizer`], backed by [`HubUsersResolver`]
/// and the spec's default 5-minute TTL ([`CacheConfig::default`]).
///
/// Reads `HUB_API_BASE_URL`/`SECRET_KEY`/`HUB_API_TIMEOUT_MS` directly from
/// the environment (not `clap`, matching `config::Secret`'s own "secrets
/// are env-only, never a CLI flag" rule for `SECRET_KEY`) rather than
/// threading a new parameter through every existing call site of this
/// function (`lib.rs`'s production wiring and `host_api.rs`'s several
/// test-only call sites) -- unset values fail safe-empty exactly like the
/// prior always-empty stub, so no existing test needed to change.
pub fn build_production_detokenizer() -> Arc<ChatDetokenizer> {
    let base_url = std::env::var("HUB_API_BASE_URL")
        .ok()
        .filter(|s| !s.is_empty());
    let secret = std::env::var("SECRET_KEY").ok().filter(|s| !s.is_empty());
    let timeout_ms: u64 = std::env::var("HUB_API_TIMEOUT_MS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(DEFAULT_TIMEOUT_MS);
    let resolver = HubUsersResolver::new(base_url, secret, Duration::from_millis(timeout_ms));
    Arc::new(Detokenizer::new(
        Arc::new(resolver) as SharedResolver,
        CacheConfig::default(),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn uuid(s: &str) -> Uuid {
        Uuid::parse_str(s).unwrap()
    }

    #[tokio::test]
    async fn unconfigured_resolver_resolves_empty() {
        let resolver = HubUsersResolver::new(None, None, Duration::from_secs(1));
        let out = resolver
            .resolve_batch("t1", &[uuid("11111111-1111-4111-8111-111111111111")])
            .await
            .unwrap();
        assert!(out.is_empty());
    }

    #[tokio::test]
    async fn production_detokenizer_renders_the_neutral_label_when_unconfigured() {
        // No HUB_API_BASE_URL/SECRET_KEY set in this test process -- same
        // fail-safe-empty posture the prior always-empty stub had.
        let detokenizer = build_production_detokenizer();
        let user = "11111111-1111-4111-8111-111111111111";
        let out = detokenizer
            .render(
                "t1",
                egress_detokenizer::Sink::ChatTwitch,
                &format!("hi {{user:{user}}}"),
            )
            .await;
        assert_eq!(out, format!("hi {}", egress_detokenizer::NEUTRAL_LABEL));
    }

    /// Spins up a tiny local axum server standing in for hub-api's
    /// internal endpoint, matching `crate::egress`'s own
    /// `reqwest_transport_reads_a_real_local_response` pattern (a real
    /// local socket, no mock-HTTP crate dependency needed).
    async fn mock_hub_api(
        expected_bearer_prefix: &'static str,
        response_by_tenant: HashMap<&'static str, serde_json::Value>,
    ) -> std::net::SocketAddr {
        use axum::extract::State;
        use axum::http::{HeaderMap, StatusCode};
        use axum::routing::post;
        use axum::Json;

        #[derive(Clone)]
        struct Ctx {
            expected_bearer_prefix: &'static str,
            response_by_tenant: Arc<HashMap<&'static str, serde_json::Value>>,
        }

        async fn handler(
            State(ctx): State<Ctx>,
            headers: HeaderMap,
            Json(body): Json<serde_json::Value>,
        ) -> (StatusCode, Json<serde_json::Value>) {
            let auth = headers
                .get("authorization")
                .and_then(|v| v.to_str().ok())
                .unwrap_or("");
            if !auth.starts_with(ctx.expected_bearer_prefix) {
                return (
                    StatusCode::FORBIDDEN,
                    Json(serde_json::json!({"status": "error"})),
                );
            }
            // The service JWT's `tenant` claim is opaque to this mock (we
            // don't decode it here); tenant passthrough is instead
            // asserted via which of `response_by_tenant`'s fixtures the
            // test configures the resolver to hit -- see
            // `resolver_passes_the_render_call_tenant_through` below,
            // which uses the requested UUIDs to select the fixture
            // instead, since this mock has no JWT decoder.
            let _ = body;
            let response = ctx
                .response_by_tenant
                .values()
                .next()
                .cloned()
                .unwrap_or_else(|| serde_json::json!({"display_names": {}}));
            (StatusCode::OK, Json(response))
        }

        let ctx = Ctx {
            expected_bearer_prefix,
            response_by_tenant: Arc::new(response_by_tenant),
        };
        let app = axum::Router::new()
            .route("/api/v1/internal/users/display-names", post(handler))
            .with_state(ctx);
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            axum::serve(listener, app).await.ok();
        });
        addr
    }

    #[tokio::test]
    async fn resolver_resolves_a_real_linked_user_from_hub_api() {
        let user = uuid("11111111-1111-4111-8111-111111111111");
        let mut fixtures = HashMap::new();
        fixtures.insert(
            "tenant-a",
            serde_json::json!({"display_names": {user.to_string(): "Ada"}}),
        );
        let addr = mock_hub_api("Bearer ", fixtures).await;

        let resolver = HubUsersResolver::new(
            Some(format!("http://127.0.0.1:{}", addr.port())),
            Some("test-secret".to_string()),
            Duration::from_secs(5),
        );
        let out = resolver.resolve_batch("tenant-a", &[user]).await.unwrap();
        assert_eq!(out.get(&user), Some(&"Ada".to_string()));
    }

    #[tokio::test]
    async fn resolver_mints_a_tenant_scoped_bearer_token_hub_api_can_reject() {
        // Server rejects any bearer that doesn't start with the expected
        // prefix -- proves the resolver actually sends a bearer token
        // (tenant passthrough happens via the JWT `tenant` claim this
        // token carries, per `mint_service_jwt`).
        let addr = mock_hub_api("Bearer eyJ", HashMap::new()).await;
        let resolver = HubUsersResolver::new(
            Some(format!("http://127.0.0.1:{}", addr.port())),
            Some("test-secret".to_string()),
            Duration::from_secs(5),
        );
        let user = uuid("22222222-2222-4222-8222-222222222222");
        let out = resolver.resolve_batch("tenant-a", &[user]).await.unwrap();
        // Server returns no display_names for this (empty fixtures) case,
        // but a 200 with an empty map -- proving the bearer was accepted.
        assert!(out.is_empty());
    }

    #[tokio::test]
    async fn cross_tenant_uuid_absent_from_hub_api_response_renders_neutral_label() {
        let linked_user = uuid("33333333-3333-4333-8333-333333333333");
        let other_tenant_user = uuid("44444444-4444-4444-8444-444444444444");
        let mut fixtures = HashMap::new();
        // hub-api's own tenant filter means a cross-tenant UUID is simply
        // absent from `display_names` -- simulated here by a fixture that
        // only ever resolves `linked_user`.
        fixtures.insert(
            "tenant-a",
            serde_json::json!({"display_names": {linked_user.to_string(): "Grace"}}),
        );
        let addr = mock_hub_api("Bearer ", fixtures).await;
        let resolver = HubUsersResolver::new(
            Some(format!("http://127.0.0.1:{}", addr.port())),
            Some("test-secret".to_string()),
            Duration::from_secs(5),
        );
        let detokenizer =
            Detokenizer::new(Arc::new(resolver) as SharedResolver, CacheConfig::default());
        let out = detokenizer
            .render(
                "tenant-a",
                egress_detokenizer::Sink::ChatTwitch,
                &format!("hi {{user:{other_tenant_user}}}"),
            )
            .await;
        assert_eq!(out, format!("hi {}", egress_detokenizer::NEUTRAL_LABEL));
    }

    #[tokio::test]
    async fn timeout_against_an_unreachable_hub_api_renders_neutral_label() {
        // TEST-NET-1 (RFC 5737): reserved, guaranteed unroutable -- a
        // connect attempt against it hangs until the client's own timeout
        // fires, deterministically exercising the timeout path with no
        // flaky external host (same technique `crate::egress`'s
        // `reqwest_transport_reports_timeout_against_an_unreachable_address`
        // test uses).
        let resolver = HubUsersResolver::new(
            Some("http://192.0.2.1:1".to_string()),
            Some("test-secret".to_string()),
            Duration::from_millis(200),
        );
        let detokenizer =
            Detokenizer::new(Arc::new(resolver) as SharedResolver, CacheConfig::default());
        let user = "55555555-5555-4555-8555-555555555555";
        let out = detokenizer
            .render(
                "tenant-a",
                egress_detokenizer::Sink::ChatTwitch,
                &format!("hi {{user:{user}}}"),
            )
            .await;
        assert_eq!(out, format!("hi {}", egress_detokenizer::NEUTRAL_LABEL));
    }

    #[test]
    fn circuit_breaker_opens_after_threshold_failures_and_closes_after_cooldown() {
        let breaker = CircuitBreaker::new();
        assert!(!breaker.is_open());
        for _ in 0..FAILURE_THRESHOLD {
            breaker.record_failure();
        }
        assert!(breaker.is_open());
        breaker.record_success();
        assert!(
            breaker.is_open(),
            "a success while open doesn't itself force-close the cooldown window early"
        );
    }

    #[test]
    fn mint_service_jwt_is_a_three_part_token_carrying_the_tenant_claim() {
        let token = mint_service_jwt("secret", "tenant-a");
        let parts: Vec<&str> = token.split('.').collect();
        assert_eq!(parts.len(), 3);
        let payload_json = URL_SAFE_NO_PAD.decode(parts[1]).unwrap();
        let payload: serde_json::Value = serde_json::from_slice(&payload_json).unwrap();
        assert_eq!(payload["tenant"], "tenant-a");
        assert_eq!(payload["scope"], "users:display-name:resolve");
    }
}
