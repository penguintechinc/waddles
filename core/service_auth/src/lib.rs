//! Per-service EdDSA machine JWTs: verification + a caching client.
//!
//! Replaces the platform-wide HS256 shared `SECRET_KEY`
//! (`libs/flask_core/flask_core/auth.py`) and static API keys for
//! service-to-service calls. hub-api issues short-lived (<=1h) Ed25519
//! JWTs per service identity (`sub` = a SPIFFE ID,
//! `spiffe://penguintech.io/<env>/<service>`), scoped to specific
//! `scope` claims; this crate is the Rust-side counterpart to
//! `libs/flask_core/flask_core/service_jwt.py`.
//!
//! # SPIFFE / Skauswatch migration
//!
//! This is deliberately SPIFFE-ready, not a one-off: `sub` is already a
//! real SPIFFE ID, and [`TrustBundle`] is a trait so verification is
//! decoupled from *who* issued the token. Today [`JwksTrustBundle`] fetches
//! hub-api's own JWKS; adopting Skauswatch/SPIRE JWT-SVIDs later means
//! adding a `SkauswatchTrustBundle` implementing the same trait (fetching
//! Skauswatch's JWT-SVID bundle endpoint instead) -- [`verify`] and every
//! caller of it are unchanged. [`MachineJwtClient`]'s bootstrap
//! (Kubernetes ServiceAccount token -> hub-api token endpoint) is the part
//! that's replaced outright by a SPIFFE Workload API call once Skauswatch
//! is adopted; it's isolated behind the same client type so callers
//! (`svc_process`, `svc_action`) don't change either.

pub mod jwt_hardening;
#[cfg(any(test, feature = "test-support"))]
pub mod test_support;

use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use jsonwebtoken::errors::ErrorKind;
use jsonwebtoken::{Algorithm, DecodingKey, Validation};
use serde::Deserialize;
use serde_json::{Map, Value};
use tokio::sync::{Mutex, RwLock};
use tracing::{debug, error, warn};

use crate::jwt_hardening::{
    inspect_header, report_outcome, JwtMetrics, JwtRejection, KidPolicy, ALG_LABEL_ABSENT,
    OUTCOME_OK, REASON_ALG_MISMATCH, REASON_BAD_AUDIENCE, REASON_BAD_ISSUER, REASON_BAD_SIGNATURE,
    REASON_EXPIRED, REASON_IMMATURE, REASON_INVALID, REASON_INVALID_CLAIM, REASON_MALFORMED,
    REASON_MISSING_CLAIM, REASON_SCOPE_DENIED, REASON_UNKNOWN_KID, VERIFIER_SERVICE_EDDSA,
};

/// The ONE algorithm the machine-JWT verifier accepts (RFC 8725
/// one-alg-per-verifier). Ed25519 per RFC 8037; mirrors
/// `libs/flask_core/flask_core/service_jwt.py::SERVICE_JWT_ALGORITHM`.
pub const SERVICE_JWT_ALGORITHM: &str = "EdDSA";

/// Claims every machine JWT must carry -- mirrors the Python verifier's
/// `require` list. `tenant` is deliberately absent: hub-api only stamps it
/// for identities bound to a tenant, and non-tenant-aware scopes such as
/// `egress:connect` are minted without one. When present it must still be a
/// non-empty string (see [`parse_claims`]).
const REQUIRED_SERVICE_CLAIMS: [&str; 8] =
    ["exp", "iat", "nbf", "iss", "aud", "sub", "scope", "jti"];

/// Clock skew tolerance applied to `exp`/`iat`, mirrored from
/// `libs/flask_core/flask_core/service_jwt.py::CLOCK_SKEW_SECONDS`.
pub const CLOCK_SKEW_SECONDS: u64 = 30;

/// Platform ceiling on machine JWT lifetime (security.md JWT Claims).
pub const MAX_TOKEN_TTL_SECONDS: u64 = 3600;

/// Every failure this crate can raise, split by whether it's an
/// authn/authz rejection (never leak which check failed to a remote
/// caller) or an operational failure (network, malformed config).
#[derive(thiserror::Error, Debug)]
pub enum ServiceAuthError {
    #[error("unknown key id {0:?}")]
    UnknownKeyId(Option<String>),
    #[error("invalid service token: {0}")]
    InvalidToken(String),
    #[error("bootstrap rejected: {0}")]
    BootstrapRejected(String),
    #[error("http error: {0}")]
    Http(#[from] reqwest::Error),
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
}

/// One verified claim set. Field names mirror the JWT payload directly
/// (`libs/flask_core/flask_core/service_jwt.py::ServiceJwtIssuer::issue`).
#[derive(Debug, Clone, Deserialize, serde::Serialize, PartialEq, Eq)]
pub struct ServiceClaims {
    pub iss: String,
    pub aud: String,
    /// SPIFFE ID of the calling service, e.g.
    /// `spiffe://penguintech.io/alpha/svc-process`.
    pub sub: String,
    pub scope: String,
    pub iat: u64,
    /// Not-before -- required (security review MEDIUM finding), mirrors
    /// `libs/flask_core/flask_core/service_jwt.py::ServiceJwtIssuer.issue`
    /// setting `nbf = iat` at mint time.
    pub nbf: u64,
    pub exp: u64,
    pub jti: String,
}

/// A pluggable source of verification public keys, keyed by `kid`.
///
/// See the module-level doc for why this exists: swapping hub-api
/// issuance for Skauswatch JWT-SVIDs later is "implement this trait
/// against Skauswatch's bundle endpoint", not a rewrite of [`verify`].
#[async_trait::async_trait]
pub trait TrustBundle: Send + Sync {
    async fn public_key(&self, kid: &str) -> Option<DecodingKey>;
}

/// Verify a machine JWT's header, signature, `iss`, `aud`, `exp`/`nbf`/`iat`
/// (with clock skew), required claims and `scope` against `trust_bundle`.
/// Returns [`ServiceAuthError::UnknownKeyId`] for an absent or unrecognized
/// `kid` and [`ServiceAuthError::InvalidToken`] for every other validation
/// failure -- callers should treat both identically (401/403), never surface
/// which check failed.
///
/// H-2 Phase 0 (RFC 8725), mirroring `ServiceJwtVerifier.verify` in
/// `libs/flask_core/flask_core/service_jwt.py`: the header is vetted before
/// any key lookup -- `alg` must be exactly [`SERVICE_JWT_ALGORITHM`] (so
/// `alg: none`, an HMAC token "signed" with the public key, or any other
/// algorithm is refused outright), `jku`/`jwk`/`x5u`/`x5c`/`crit` are
/// refused, and a `kid` outside the pinned charset never reaches the trust
/// bundle. An empty expected audience or issuer is a misconfiguration and
/// fails closed. Every call emits `waddles_jwt_verifications_total` with
/// `verifier=service_eddsa`; nothing logged ever contains token material.
pub async fn verify(
    token: &str,
    trust_bundle: &dyn TrustBundle,
    expected_audience: &str,
    trusted_issuers: &[&str],
    required_scope: &str,
) -> Result<ServiceClaims, ServiceAuthError> {
    verify_with_metrics(
        JwtMetrics::global(),
        token,
        trust_bundle,
        expected_audience,
        trusted_issuers,
        required_scope,
    )
    .await
}

/// [`verify`] against an explicit [`JwtMetrics`] (tests inject an in-memory
/// meter provider; production goes through the global one).
pub(crate) async fn verify_with_metrics(
    metrics: &JwtMetrics,
    token: &str,
    trust_bundle: &dyn TrustBundle,
    expected_audience: &str,
    trusted_issuers: &[&str],
    required_scope: &str,
) -> Result<ServiceClaims, ServiceAuthError> {
    let started = Instant::now();
    let mut alg = ALG_LABEL_ABSENT;
    let result = verify_checked(
        token,
        trust_bundle,
        expected_audience,
        trusted_issuers,
        required_scope,
        &mut alg,
    )
    .await;
    let outcome = match &result {
        Ok(_) => OUTCOME_OK,
        Err(failure) => failure.rejection.reason,
    };
    report_outcome(metrics, VERIFIER_SERVICE_EDDSA, started, alg, outcome);
    result.map_err(Failure::into_error)
}

/// A failed verification: the closed-vocabulary rejection plus, for an
/// unknown `kid`, the (charset-vetted) value to echo in the error.
struct Failure {
    rejection: JwtRejection,
    kid: Option<String>,
}

impl Failure {
    fn new(rejection: JwtRejection) -> Self {
        Self {
            rejection,
            kid: None,
        }
    }

    fn reason(reason: &'static str, alg: &'static str) -> Self {
        Self::new(JwtRejection::new(reason, alg))
    }

    fn into_error(self) -> ServiceAuthError {
        if self.rejection.reason == REASON_UNKNOWN_KID {
            ServiceAuthError::UnknownKeyId(self.kid)
        } else {
            ServiceAuthError::InvalidToken(format!("rejected ({})", self.rejection.reason))
        }
    }
}

/// Every check of [`verify`], in order, raising the first [`Failure`]. Kept
/// free of metrics/logging so the policy reads as one sequence; `alg` is
/// updated as soon as the header yields a label-safe value so even a later
/// rejection is counted under the right algorithm.
async fn verify_checked(
    token: &str,
    trust_bundle: &dyn TrustBundle,
    expected_audience: &str,
    trusted_issuers: &[&str],
    required_scope: &str,
    alg: &mut &'static str,
) -> Result<ServiceClaims, Failure> {
    // A verifier with nothing to compare against would accept anything that
    // happens to carry the empty string -- fail closed instead.
    if expected_audience.is_empty() {
        return Err(Failure::reason(REASON_BAD_AUDIENCE, ALG_LABEL_ABSENT));
    }
    if trusted_issuers.is_empty() || trusted_issuers.iter().any(|issuer| issuer.is_empty()) {
        return Err(Failure::reason(REASON_BAD_ISSUER, ALG_LABEL_ABSENT));
    }

    let header = inspect_header(token, &[SERVICE_JWT_ALGORITHM], KidPolicy::Vet).map_err(|r| {
        *alg = r.alg;
        Failure::new(r)
    })?;
    *alg = "eddsa";
    let Some(kid) = header.kid else {
        return Err(Failure::reason(REASON_UNKNOWN_KID, "eddsa"));
    };
    let Some(key) = trust_bundle.public_key(&kid).await else {
        return Err(Failure {
            rejection: JwtRejection::new(REASON_UNKNOWN_KID, "eddsa"),
            kid: Some(kid),
        });
    };

    let mut validation = Validation::new(Algorithm::EdDSA);
    validation.set_audience(&[expected_audience]);
    validation.set_issuer(trusted_issuers);
    validation.leeway = CLOCK_SKEW_SECONDS;
    // `nbf` is mandatory (security review MEDIUM finding) -- `validate_nbf`
    // defaults to `false` in jsonwebtoken 10.x, so it must be opted into
    // explicitly; `leeway` above bounds it against issuer/verifier clock
    // skew the same way it already bounds `exp`.
    validation.validate_nbf = true;
    // `sub` is left to `parse_claims`, which tells a missing `sub` from a
    // mistyped one (the library would call both "missing").
    validation.set_required_spec_claims(&["exp", "nbf", "iss", "aud"]);

    let data = jsonwebtoken::decode::<Value>(token, &key, &validation)
        .map_err(|e| Failure::reason(classify_decode_error(e.kind()), "eddsa"))?;
    let claims = parse_claims(&data.claims, expected_audience, now_secs())
        .map_err(|reason| Failure::reason(reason, "eddsa"))?;

    if claims.scope != required_scope {
        return Err(Failure::reason(REASON_SCOPE_DENIED, "eddsa"));
    }
    Ok(claims)
}

/// Map a `jsonwebtoken` failure to a closed `REASON_*` -- never its text.
fn classify_decode_error(kind: &ErrorKind) -> &'static str {
    match kind {
        ErrorKind::ExpiredSignature => REASON_EXPIRED,
        ErrorKind::ImmatureSignature => REASON_IMMATURE,
        ErrorKind::InvalidSignature => REASON_BAD_SIGNATURE,
        ErrorKind::InvalidIssuer => REASON_BAD_ISSUER,
        ErrorKind::InvalidAudience => REASON_BAD_AUDIENCE,
        ErrorKind::MissingRequiredClaim(_) => REASON_MISSING_CLAIM,
        ErrorKind::InvalidClaimFormat(_) | ErrorKind::Json(_) => REASON_INVALID_CLAIM,
        ErrorKind::InvalidAlgorithm
        | ErrorKind::InvalidAlgorithmName
        | ErrorKind::MissingAlgorithm => REASON_ALG_MISMATCH,
        ErrorKind::InvalidToken | ErrorKind::Base64(_) | ErrorKind::Utf8(_) => REASON_MALFORMED,
        _ => REASON_INVALID,
    }
}

/// Shape-check a signature-verified payload and build [`ServiceClaims`].
///
/// `jsonwebtoken` already proved `exp`/`nbf`/`iss`/`aud`/`sub` are present
/// and valid; this adds what it does not look at: `iat`, `scope`, `jti` and
/// `tenant`, and that identity-bearing strings are non-empty (an empty `sub`
/// is "missing" by another name). `aud` may be a string or a list containing
/// the expected audience -- the library already proved membership.
fn parse_claims(
    payload: &Value,
    expected_audience: &str,
    now: u64,
) -> Result<ServiceClaims, &'static str> {
    let Some(object) = payload.as_object() else {
        return Err(REASON_INVALID_CLAIM);
    };
    for name in REQUIRED_SERVICE_CLAIMS {
        if object.get(name).is_none_or(Value::is_null) {
            return Err(REASON_MISSING_CLAIM);
        }
    }
    let iat = unsigned_claim(object, "iat")?;
    if iat > now.saturating_add(CLOCK_SKEW_SECONDS) {
        return Err(REASON_IMMATURE);
    }
    // `tenant` is optional (see REQUIRED_SERVICE_CLAIMS) but never empty.
    if object.get("tenant").is_some_and(|tenant| !tenant.is_null()) {
        non_empty_string_claim(object, "tenant")?;
    }
    Ok(ServiceClaims {
        iss: non_empty_string_claim(object, "iss")?,
        aud: expected_audience.to_string(),
        sub: non_empty_string_claim(object, "sub")?,
        scope: string_claim(object, "scope")?,
        iat,
        nbf: unsigned_claim(object, "nbf")?,
        exp: unsigned_claim(object, "exp")?,
        jti: non_empty_string_claim(object, "jti")?,
    })
}

/// A claim that must be a string (possibly empty, e.g. `scope`).
fn string_claim(object: &Map<String, Value>, name: &str) -> Result<String, &'static str> {
    match object.get(name) {
        Some(Value::String(value)) => Ok(value.clone()),
        _ => Err(REASON_INVALID_CLAIM),
    }
}

/// A claim that must be a string with at least one non-whitespace character.
fn non_empty_string_claim(object: &Map<String, Value>, name: &str) -> Result<String, &'static str> {
    let value = string_claim(object, name)?;
    if value.trim().is_empty() {
        return Err(REASON_INVALID_CLAIM);
    }
    Ok(value)
}

/// A claim that must be a non-negative JSON integer (NumericDate).
fn unsigned_claim(object: &Map<String, Value>, name: &str) -> Result<u64, &'static str> {
    object
        .get(name)
        .and_then(Value::as_u64)
        .ok_or(REASON_INVALID_CLAIM)
}

/// Default TCP+TLS connect timeout for every outbound HTTP client.
pub const DEFAULT_HTTP_CONNECT_TIMEOUT: Duration = Duration::from_secs(3);
/// Default total per-request timeout (connect through last body byte).
pub const DEFAULT_HTTP_REQUEST_TIMEOUT: Duration = Duration::from_secs(10);
/// Default time an unknown `kid` stays negatively cached before one more
/// JWKS refetch is allowed.
pub const DEFAULT_JWKS_NEGATIVE_TTL: Duration = Duration::from_secs(30);
/// Hard cap on negative-cache entries so a flood of distinct bogus `kid`s
/// cannot grow memory without bound.
const NEGATIVE_CACHE_MAX_ENTRIES: usize = 1024;

/// Env override: connect timeout in whole seconds.
pub const ENV_HTTP_CONNECT_TIMEOUT_SECS: &str = "SERVICE_AUTH_HTTP_CONNECT_TIMEOUT_SECS";
/// Env override: total request timeout in whole seconds.
pub const ENV_HTTP_REQUEST_TIMEOUT_SECS: &str = "SERVICE_AUTH_HTTP_REQUEST_TIMEOUT_SECS";
/// Env override: JWKS unknown-`kid` negative-cache TTL in whole seconds.
pub const ENV_JWKS_NEGATIVE_TTL_SECS: &str = "SERVICE_AUTH_JWKS_NEGATIVE_TTL_SECS";

/// Explicit connect + request timeouts applied to every outbound HTTP
/// client in this crate, so a hung hub-api can never wedge a verifier or a
/// token refresh indefinitely.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct HttpTimeouts {
    /// Max time to establish the connection.
    pub connect: Duration,
    /// Max total time for one request/response exchange.
    pub request: Duration,
}

impl Default for HttpTimeouts {
    fn default() -> Self {
        Self {
            connect: DEFAULT_HTTP_CONNECT_TIMEOUT,
            request: DEFAULT_HTTP_REQUEST_TIMEOUT,
        }
    }
}

impl HttpTimeouts {
    /// Defaults overridden by `SERVICE_AUTH_HTTP_CONNECT_TIMEOUT_SECS` /
    /// `SERVICE_AUTH_HTTP_REQUEST_TIMEOUT_SECS`; invalid or zero values are
    /// logged and ignored (never "no timeout").
    pub fn from_env() -> Self {
        Self {
            connect: duration_from_env(ENV_HTTP_CONNECT_TIMEOUT_SECS, DEFAULT_HTTP_CONNECT_TIMEOUT),
            request: duration_from_env(ENV_HTTP_REQUEST_TIMEOUT_SECS, DEFAULT_HTTP_REQUEST_TIMEOUT),
        }
    }

    /// Build a `reqwest::Client` with both timeouts set.
    fn build_client(self) -> Result<reqwest::Client, ServiceAuthError> {
        Ok(reqwest::Client::builder()
            .connect_timeout(self.connect)
            .timeout(self.request)
            .build()?)
    }
}

/// Parse a positive whole-seconds value; `None`, zero or garbage -> `default`.
fn parse_secs(raw: Option<&str>, default: Duration) -> Duration {
    match raw.map(str::trim) {
        None | Some("") => default,
        Some(v) => match v.parse::<u64>() {
            Ok(n) if n > 0 => Duration::from_secs(n),
            _ => default,
        },
    }
}

fn duration_from_env(name: &str, default: Duration) -> Duration {
    let raw = std::env::var(name).ok();
    let parsed = parse_secs(raw.as_deref(), default);
    if raw.is_some() && parsed == default {
        debug!(
            env = name,
            "service_auth.env_override_ignored_using_default"
        );
    }
    parsed
}

/// Build the client for `new()` constructors. Same failure mode (and
/// invariant) as `reqwest::Client::new()`, which panics only if the TLS
/// backend cannot initialise -- never silently fall back to a client
/// without timeouts.
fn client_or_die(timeouts: HttpTimeouts) -> reqwest::Client {
    match timeouts.build_client() {
        Ok(client) => client,
        Err(err) => panic!("service_auth: cannot build HTTP client (TLS backend init): {err}"),
    }
}

/// Tunables for [`JwksTrustBundle`]: HTTP timeouts + unknown-`kid` negative TTL.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct JwksConfig {
    /// Timeouts for the JWKS fetch.
    pub http: HttpTimeouts,
    /// How long a `kid` absent from the JWKS (or whose refetch failed) is
    /// rejected without touching the network.
    pub negative_ttl: Duration,
}

impl Default for JwksConfig {
    fn default() -> Self {
        Self {
            http: HttpTimeouts::default(),
            negative_ttl: DEFAULT_JWKS_NEGATIVE_TTL,
        }
    }
}

impl JwksConfig {
    /// Defaults overridden from the `SERVICE_AUTH_*` env vars.
    pub fn from_env() -> Self {
        Self {
            http: HttpTimeouts::from_env(),
            negative_ttl: duration_from_env(ENV_JWKS_NEGATIVE_TTL_SECS, DEFAULT_JWKS_NEGATIVE_TTL),
        }
    }
}

/// [`TrustBundle`] backed by hub-api's own JWKS endpoint, refreshed on a
/// cache miss (new `kid` seen, e.g. mid-rotation).
///
/// Resilience: the fetch has connect + request timeouts; concurrent misses
/// are coalesced (single-flight) into one refetch; and a `kid` the JWKS does
/// not contain is negatively cached for a short TTL so a flood of bogus
/// `kid`s cannot hammer the endpoint. An unknown `kid` is still rejected
/// (fail closed) -- the caches only bound how often we re-ask.
pub struct JwksTrustBundle {
    jwks_url: String,
    http: reqwest::Client,
    cache: RwLock<HashMap<String, DecodingKey>>,
    /// Serialises refetches: whoever holds it is the one in-flight request.
    refresh_gate: Mutex<()>,
    /// `kid` -> instant until which it is known-unknown.
    negative: std::sync::Mutex<HashMap<String, Instant>>,
    negative_ttl: Duration,
}

#[derive(Deserialize)]
struct Jwks {
    keys: Vec<JwkEntry>,
}

#[derive(Deserialize)]
struct JwkEntry {
    kid: String,
    x: String,
}

impl JwksTrustBundle {
    /// Bundle with timeouts/TTL from the environment (or defaults).
    pub fn new(jwks_url: impl Into<String>) -> Self {
        let config = JwksConfig::from_env();
        Self::build(jwks_url.into(), config, client_or_die(config.http))
    }

    /// Bundle with explicit [`JwksConfig`]; errors if the HTTP client cannot be built.
    pub fn with_config(
        jwks_url: impl Into<String>,
        config: JwksConfig,
    ) -> Result<Self, ServiceAuthError> {
        let client = config.http.build_client()?;
        Ok(Self::build(jwks_url.into(), config, client))
    }

    fn build(jwks_url: String, config: JwksConfig, http: reqwest::Client) -> Self {
        Self {
            jwks_url,
            http,
            cache: RwLock::new(HashMap::new()),
            refresh_gate: Mutex::new(()),
            negative: std::sync::Mutex::new(HashMap::new()),
            negative_ttl: config.negative_ttl,
        }
    }

    async fn refresh(&self) -> Result<(), ServiceAuthError> {
        let jwks: Jwks = self.http.get(&self.jwks_url).send().await?.json().await?;
        let mut cache = self.cache.write().await;
        cache.clear();
        for entry in jwks.keys {
            // An empty `x` can never be a real Ed25519 key -- fail the whole
            // refresh closed rather than cache a key that "verifies" nothing.
            if entry.x.is_empty() {
                return Err(ServiceAuthError::InvalidToken(format!(
                    "bad JWKS entry {}: empty key",
                    entry.kid
                )));
            }
            let key = DecodingKey::from_ed_components(&entry.x).map_err(|e| {
                ServiceAuthError::InvalidToken(format!("bad JWKS entry {}: {e}", entry.kid))
            })?;
            cache.insert(entry.kid, key);
        }
        debug!(count = cache.len(), "service_auth.jwks_refreshed");
        Ok(())
    }

    /// Lock the negative cache, failing loudly if a prior panic poisoned it.
    /// Returns `None` (after an ERROR log) rather than silently recovering the
    /// possibly-corrupt inner map; callers must fail closed on `None`.
    fn lock_negative(&self) -> Option<std::sync::MutexGuard<'_, HashMap<String, Instant>>> {
        match self.negative.lock() {
            Ok(guard) => Some(guard),
            Err(_) => {
                error!("service_auth negative cache mutex poisoned; failing closed");
                None
            }
        }
    }

    /// True while `kid` is inside its negative-cache window. A poisoned cache
    /// fails closed (reports the `kid` as denied) so no refetch is attempted.
    fn is_negatively_cached(&self, kid: &str) -> bool {
        match self.lock_negative() {
            Some(map) => map.get(kid).is_some_and(|until| Instant::now() < *until),
            None => true,
        }
    }

    /// Remember `kid` as unknown until `negative_ttl` elapses. Expired
    /// entries are purged on insert and the map is hard-capped.
    fn remember_unknown(&self, kid: &str) {
        let now = Instant::now();
        let Some(mut map) = self.lock_negative() else {
            return;
        };
        map.retain(|_, until| now < *until);
        if map.len() >= NEGATIVE_CACHE_MAX_ENTRIES {
            let oldest = map
                .iter()
                .min_by_key(|(_, until)| **until)
                .map(|(k, _)| k.clone());
            if let Some(k) = oldest {
                map.remove(&k);
            }
        }
        map.insert(kid.to_owned(), now + self.negative_ttl);
    }
}

#[async_trait::async_trait]
impl TrustBundle for JwksTrustBundle {
    async fn public_key(&self, kid: &str) -> Option<DecodingKey> {
        if let Some(key) = self.cache.read().await.get(kid) {
            return Some(key.clone());
        }
        if self.is_negatively_cached(kid) {
            debug!("service_auth.jwks_negative_cache_hit");
            return None;
        }
        // Single-flight: concurrent misses queue on the gate; only the first
        // actually fetches, the rest re-check the caches it just filled.
        let _flight = self.refresh_gate.lock().await;
        if let Some(key) = self.cache.read().await.get(kid) {
            return Some(key.clone());
        }
        if self.is_negatively_cached(kid) {
            return None;
        }
        // Cache miss -- refresh once (covers key rotation) before giving up.
        if let Err(err) = self.refresh().await {
            warn!(error = %err, "service_auth.jwks_refresh_failed");
            // Fail closed, and back off so a down endpoint isn't re-hit per token.
            self.remember_unknown(kid);
            return None;
        }
        let found = self.cache.read().await.get(kid).cloned();
        if found.is_none() {
            self.remember_unknown(kid);
        }
        found
    }
}

/// Obtains, caches and refreshes a machine JWT for one calling service
/// (`svc_process`, `svc_action`, ...). Bootstraps against hub-api's token
/// endpoint using the pod's own projected Kubernetes ServiceAccount token
/// -- no shared secret is ever distributed to callers.
pub struct MachineJwtClient {
    token_endpoint: String,
    sa_token_path: String,
    scope: String,
    http: reqwest::Client,
    cached: RwLock<Option<CachedToken>>,
}

#[derive(Clone)]
struct CachedToken {
    token: Arc<str>,
    expires_at: u64,
}

#[derive(Deserialize)]
struct TokenResponse {
    token: String,
    expires_in: u64,
}

impl MachineJwtClient {
    /// Client with timeouts from the environment (or defaults).
    pub fn new(
        token_endpoint: impl Into<String>,
        sa_token_path: impl Into<String>,
        scope: impl Into<String>,
    ) -> Self {
        Self::build(
            token_endpoint.into(),
            sa_token_path.into(),
            scope.into(),
            client_or_die(HttpTimeouts::from_env()),
        )
    }

    /// Client with explicit [`HttpTimeouts`]; errors if the HTTP client cannot be built.
    pub fn with_timeouts(
        token_endpoint: impl Into<String>,
        sa_token_path: impl Into<String>,
        scope: impl Into<String>,
        timeouts: HttpTimeouts,
    ) -> Result<Self, ServiceAuthError> {
        Ok(Self::build(
            token_endpoint.into(),
            sa_token_path.into(),
            scope.into(),
            timeouts.build_client()?,
        ))
    }

    fn build(
        token_endpoint: String,
        sa_token_path: String,
        scope: String,
        http: reqwest::Client,
    ) -> Self {
        Self {
            token_endpoint,
            sa_token_path,
            scope,
            http,
            cached: RwLock::new(None),
        }
    }

    /// Return a currently-valid machine JWT, minting/refreshing one from
    /// hub-api if the cache is empty or within `CLOCK_SKEW_SECONDS` of
    /// expiry.
    pub async fn get_token(&self) -> Result<Arc<str>, ServiceAuthError> {
        let now = now_secs();
        if let Some(cached) = self.cached.read().await.as_ref() {
            if cached.expires_at > now + CLOCK_SKEW_SECONDS {
                return Ok(cached.token.clone());
            }
        }
        self.refresh(now).await
    }

    async fn refresh(&self, now: u64) -> Result<Arc<str>, ServiceAuthError> {
        let sa_token = tokio::fs::read_to_string(&self.sa_token_path).await?;
        let response = self
            .http
            .post(&self.token_endpoint)
            .bearer_auth(sa_token.trim())
            .json(&serde_json::json!({ "scope": self.scope }))
            .send()
            .await?;
        if !response.status().is_success() {
            return Err(ServiceAuthError::BootstrapRejected(format!(
                "token endpoint returned {}",
                response.status()
            )));
        }
        let body: TokenResponse = response.json().await?;
        let expires_at = now + body.expires_in.min(MAX_TOKEN_TTL_SECONDS);
        let token: Arc<str> = Arc::from(body.token.as_str());
        *self.cached.write().await = Some(CachedToken {
            token: token.clone(),
            expires_at,
        });
        Ok(token)
    }
}

fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or(Duration::ZERO)
        .as_secs()
}

#[cfg(test)]
mod tests {
    use super::*;
    use base64::engine::general_purpose::URL_SAFE_NO_PAD;
    use base64::Engine as _;
    use jsonwebtoken::{EncodingKey, Header};
    use std::sync::Mutex;

    struct StaticTrustBundle(Mutex<HashMap<String, DecodingKey>>);

    #[async_trait::async_trait]
    impl TrustBundle for StaticTrustBundle {
        async fn public_key(&self, kid: &str) -> Option<DecodingKey> {
            self.0.lock().unwrap().get(kid).cloned()
        }
    }

    // PKCS8/SPKI-DER-encoded Ed25519 test keypairs (fixed, test-only),
    // generated once with `openssl genpkey -algorithm ed25519` /
    // `openssl pkey -pubout`; never used outside this test module.
    const KEY_A_PRIV_DER: &[u8] = &[
        48, 46, 2, 1, 0, 48, 5, 6, 3, 43, 101, 112, 4, 34, 4, 32, 1, 204, 5, 142, 35, 153, 231, 38,
        150, 122, 1, 218, 34, 237, 70, 125, 233, 62, 126, 103, 151, 16, 11, 238, 95, 122, 209, 74,
        183, 9, 171, 161,
    ];
    // Raw 32-byte Ed25519 public key (the last 32 bytes of the SPKI-DER
    // `openssl pkey -pubout` produced) -- `DecodingKey::from_ed_der` in
    // jsonwebtoken's rust_crypto backend reads only the raw key bytes, not
    // the full ASN.1 SPKI wrapper.
    const KEY_A_PUB_RAW: &[u8] = &[
        169, 90, 255, 23, 51, 151, 156, 147, 56, 247, 214, 168, 76, 160, 67, 99, 211, 238, 208, 5,
        69, 236, 245, 115, 4, 81, 1, 42, 23, 107, 4, 187,
    ];
    const KEY_B_PRIV_DER: &[u8] = &[
        48, 46, 2, 1, 0, 48, 5, 6, 3, 43, 101, 112, 4, 34, 4, 32, 145, 56, 92, 35, 32, 192, 103,
        161, 66, 249, 233, 0, 174, 22, 45, 100, 136, 104, 59, 129, 251, 81, 20, 214, 221, 250, 219,
        227, 139, 109, 70, 185,
    ];

    fn ed25519_keypair() -> (EncodingKey, DecodingKey) {
        (
            EncodingKey::from_ed_der(KEY_A_PRIV_DER),
            DecodingKey::from_ed_der(KEY_A_PUB_RAW),
        )
    }

    fn make_token(encoding_key: &EncodingKey, kid: &str, claims: &ServiceClaims) -> String {
        let mut header = Header::new(Algorithm::EdDSA);
        header.kid = Some(kid.to_string());
        jsonwebtoken::encode(&header, claims, encoding_key).unwrap()
    }

    fn base_claims(now: u64) -> ServiceClaims {
        ServiceClaims {
            iss: "hub-api".into(),
            aud: "waddlebot-internal".into(),
            sub: "spiffe://penguintech.io/alpha/svc-process".into(),
            scope: "identity:ephemeral:mint".into(),
            iat: now,
            nbf: now,
            exp: now + 900,
            jti: "test-jti".into(),
        }
    }

    #[tokio::test]
    async fn verify_accepts_valid_token() {
        let (enc, dec) = ed25519_keypair();
        let bundle = StaticTrustBundle(Mutex::new(HashMap::from([("k1".to_string(), dec)])));
        let now = now_secs();
        let token = make_token(&enc, "k1", &base_claims(now));
        let claims = verify(
            &token,
            &bundle,
            "waddlebot-internal",
            &["hub-api"],
            "identity:ephemeral:mint",
        )
        .await
        .expect("valid token verifies");
        assert_eq!(claims.sub, "spiffe://penguintech.io/alpha/svc-process");
    }

    #[tokio::test]
    async fn verify_rejects_unknown_kid() {
        let (enc, _dec) = ed25519_keypair();
        let bundle = StaticTrustBundle(Mutex::new(HashMap::new()));
        let now = now_secs();
        let token = make_token(&enc, "missing-kid", &base_claims(now));
        let err = verify(
            &token,
            &bundle,
            "waddlebot-internal",
            &["hub-api"],
            "identity:ephemeral:mint",
        )
        .await
        .unwrap_err();
        assert!(matches!(err, ServiceAuthError::UnknownKeyId(_)));
    }

    #[tokio::test]
    async fn verify_rejects_expired_token() {
        let (enc, dec) = ed25519_keypair();
        let bundle = StaticTrustBundle(Mutex::new(HashMap::from([("k1".to_string(), dec)])));
        let now = now_secs();
        let mut claims = base_claims(now - 3600);
        claims.exp = now - 1800;
        let token = make_token(&enc, "k1", &claims);
        let err = verify(
            &token,
            &bundle,
            "waddlebot-internal",
            &["hub-api"],
            "identity:ephemeral:mint",
        )
        .await
        .unwrap_err();
        assert!(matches!(err, ServiceAuthError::InvalidToken(_)));
    }

    #[tokio::test]
    async fn verify_rejects_wrong_audience() {
        let (enc, dec) = ed25519_keypair();
        let bundle = StaticTrustBundle(Mutex::new(HashMap::from([("k1".to_string(), dec)])));
        let now = now_secs();
        let mut claims = base_claims(now);
        claims.aud = "some-other-audience".into();
        let token = make_token(&enc, "k1", &claims);
        let err = verify(
            &token,
            &bundle,
            "waddlebot-internal",
            &["hub-api"],
            "identity:ephemeral:mint",
        )
        .await
        .unwrap_err();
        assert!(matches!(err, ServiceAuthError::InvalidToken(_)));
    }

    #[tokio::test]
    async fn verify_rejects_wrong_scope() {
        let (enc, dec) = ed25519_keypair();
        let bundle = StaticTrustBundle(Mutex::new(HashMap::from([("k1".to_string(), dec)])));
        let now = now_secs();
        let token = make_token(&enc, "k1", &base_claims(now));
        let err = verify(
            &token,
            &bundle,
            "waddlebot-internal",
            &["hub-api"],
            "some:other:scope",
        )
        .await
        .unwrap_err();
        assert!(matches!(err, ServiceAuthError::InvalidToken(_)));
    }

    #[tokio::test]
    async fn verify_rejects_token_not_yet_valid() {
        let (enc, dec) = ed25519_keypair();
        let bundle = StaticTrustBundle(Mutex::new(HashMap::from([("k1".to_string(), dec)])));
        let now = now_secs();
        let mut claims = base_claims(now);
        // Well beyond CLOCK_SKEW_SECONDS -- a token whose `nbf` is still in
        // the future (clock-skew-adjusted) must be rejected, not silently
        // accepted because `exp` alone still passes.
        claims.nbf = now + CLOCK_SKEW_SECONDS + 300;
        let token = make_token(&enc, "k1", &claims);
        let err = verify(
            &token,
            &bundle,
            "waddlebot-internal",
            &["hub-api"],
            "identity:ephemeral:mint",
        )
        .await
        .unwrap_err();
        assert!(matches!(err, ServiceAuthError::InvalidToken(_)));
    }

    #[tokio::test]
    async fn verify_accepts_token_within_nbf_clock_skew() {
        let (enc, dec) = ed25519_keypair();
        let bundle = StaticTrustBundle(Mutex::new(HashMap::from([("k1".to_string(), dec)])));
        let now = now_secs();
        let mut claims = base_claims(now);
        // Just within the bounded skew -- must still verify (a strict
        // `nbf == now` check would spuriously reject legitimate tokens on
        // any real clock drift between issuer and verifier).
        claims.nbf = now + CLOCK_SKEW_SECONDS - 5;
        let token = make_token(&enc, "k1", &claims);
        verify(
            &token,
            &bundle,
            "waddlebot-internal",
            &["hub-api"],
            "identity:ephemeral:mint",
        )
        .await
        .expect("token within clock skew leeway verifies");
    }

    #[tokio::test]
    async fn verify_rejects_forged_signature() {
        let (_enc, dec) = ed25519_keypair();
        // A second, different test keypair to sign with -- simulates an
        // attacker who doesn't hold hub-api's private key but guesses/
        // reuses a known `kid`. Verification stays pinned to key A's
        // public key, so a token signed by key B must be rejected.
        let forged_enc = EncodingKey::from_ed_der(KEY_B_PRIV_DER);
        let bundle = StaticTrustBundle(Mutex::new(HashMap::from([("k1".to_string(), dec)])));
        let now = now_secs();
        let forged = make_token(&forged_enc, "k1", &base_claims(now));
        let err = verify(
            &forged,
            &bundle,
            "waddlebot-internal",
            &["hub-api"],
            "identity:ephemeral:mint",
        )
        .await
        .unwrap_err();
        assert!(matches!(err, ServiceAuthError::InvalidToken(_)));
    }

    use std::sync::atomic::{AtomicUsize, Ordering};
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    /// Minimal HTTP server returning canned `(status, body)` responses in
    /// order (last one repeats); returns its base URL and a hit counter.
    async fn mock_server(responses: Vec<(u16, String)>) -> (String, Arc<AtomicUsize>) {
        mock_server_delayed(responses, Duration::ZERO).await
    }

    /// As [`mock_server`] but waits `delay` before answering, so concurrent
    /// callers genuinely overlap an in-flight request.
    async fn mock_server_delayed(
        responses: Vec<(u16, String)>,
        delay: Duration,
    ) -> (String, Arc<AtomicUsize>) {
        let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
        let addr = listener.local_addr().expect("addr");
        let hits = Arc::new(AtomicUsize::new(0));
        let hits_task = hits.clone();
        tokio::spawn(async move {
            loop {
                let Ok((mut sock, _)) = listener.accept().await else {
                    return;
                };
                let n = hits_task.fetch_add(1, Ordering::SeqCst);
                let (status, body) = responses[n.min(responses.len() - 1)].clone();
                tokio::spawn(async move {
                    let mut buf = vec![0u8; 8192];
                    let _ = sock.read(&mut buf).await;
                    tokio::time::sleep(delay).await;
                    let resp = format!(
                        "HTTP/1.1 {status} X\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                        body.len()
                    );
                    let _ = sock.write_all(resp.as_bytes()).await;
                    let _ = sock.shutdown().await;
                });
            }
        });
        (format!("http://{addr}"), hits)
    }

    fn b64url(data: &[u8]) -> String {
        const T: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
        let mut out = String::new();
        for chunk in data.chunks(3) {
            let b = [
                chunk[0],
                *chunk.get(1).unwrap_or(&0),
                *chunk.get(2).unwrap_or(&0),
            ];
            let n = (u32::from(b[0]) << 16) | (u32::from(b[1]) << 8) | u32::from(b[2]);
            for i in 0..=chunk.len() {
                out.push(T[((n >> (18 - 6 * i)) & 63) as usize] as char);
            }
        }
        out
    }

    fn jwks_body(kid: &str) -> String {
        serde_json::json!({"keys":[{"kid": kid, "kty":"OKP", "crv":"Ed25519", "x": b64url(KEY_A_PUB_RAW)}]})
            .to_string()
    }

    fn temp_sa_token(name: &str, contents: &str) -> String {
        let path =
            std::env::temp_dir().join(format!("service_auth_test_{}_{name}", std::process::id()));
        std::fs::write(&path, contents).expect("write sa token");
        path.to_string_lossy().into_owned()
    }

    #[test]
    fn error_display_strings() {
        assert!(ServiceAuthError::UnknownKeyId(Some("k".into()))
            .to_string()
            .contains("unknown key id"));
        assert!(ServiceAuthError::InvalidToken("x".into())
            .to_string()
            .contains("invalid service token"));
        assert!(ServiceAuthError::BootstrapRejected("x".into())
            .to_string()
            .contains("bootstrap rejected"));
        let io: ServiceAuthError = std::io::Error::other("boom").into();
        assert!(io.to_string().contains("io error"));
    }

    #[tokio::test]
    async fn verify_rejects_malformed_token() {
        let bundle = StaticTrustBundle(Mutex::new(HashMap::new()));
        let err = verify("not-a-jwt", &bundle, "a", &["hub-api"], "s")
            .await
            .unwrap_err();
        assert!(matches!(err, ServiceAuthError::InvalidToken(m) if m == "rejected (malformed)"));
    }

    #[tokio::test]
    async fn verify_rejects_token_without_kid() {
        let (enc, dec) = ed25519_keypair();
        let bundle = StaticTrustBundle(Mutex::new(HashMap::from([("k1".to_string(), dec)])));
        let header = Header::new(Algorithm::EdDSA);
        let token = jsonwebtoken::encode(&header, &base_claims(now_secs()), &enc).expect("encode");
        let err = verify(
            &token,
            &bundle,
            "waddlebot-internal",
            &["hub-api"],
            "identity:ephemeral:mint",
        )
        .await
        .unwrap_err();
        assert!(matches!(err, ServiceAuthError::UnknownKeyId(None)));
    }

    #[tokio::test]
    async fn verify_rejects_untrusted_issuer() {
        let (enc, dec) = ed25519_keypair();
        let bundle = StaticTrustBundle(Mutex::new(HashMap::from([("k1".to_string(), dec)])));
        let mut claims = base_claims(now_secs());
        claims.iss = "evil-issuer".into();
        let token = make_token(&enc, "k1", &claims);
        let err = verify(
            &token,
            &bundle,
            "waddlebot-internal",
            &["hub-api"],
            "identity:ephemeral:mint",
        )
        .await
        .unwrap_err();
        assert!(matches!(err, ServiceAuthError::InvalidToken(_)));
    }

    #[tokio::test]
    async fn verify_rejects_wrong_algorithm_hs256() {
        let (_enc, dec) = ed25519_keypair();
        let bundle = StaticTrustBundle(Mutex::new(HashMap::from([("k1".to_string(), dec)])));
        let mut header = Header::new(Algorithm::HS256);
        header.kid = Some("k1".into());
        let token = jsonwebtoken::encode(
            &header,
            &base_claims(now_secs()),
            &EncodingKey::from_secret(b"shared"),
        )
        .expect("encode");
        let err = verify(
            &token,
            &bundle,
            "waddlebot-internal",
            &["hub-api"],
            "identity:ephemeral:mint",
        )
        .await
        .unwrap_err();
        assert!(matches!(err, ServiceAuthError::InvalidToken(_)));
    }

    #[tokio::test]
    async fn jwks_bundle_fetches_and_caches_key() {
        let (url, hits) = mock_server(vec![(200, jwks_body("k1"))]).await;
        let bundle = JwksTrustBundle::new(url);
        let (enc, _) = ed25519_keypair();
        let token = make_token(&enc, "k1", &base_claims(now_secs()));
        for _ in 0..2 {
            verify(
                &token,
                &bundle,
                "waddlebot-internal",
                &["hub-api"],
                "identity:ephemeral:mint",
            )
            .await
            .expect("verifies via JWKS");
        }
        assert_eq!(hits.load(Ordering::SeqCst), 1, "second lookup is cached");
    }

    #[tokio::test]
    async fn jwks_bundle_unknown_kid_after_refresh_is_none() {
        let (url, hits) = mock_server(vec![(200, jwks_body("k1"))]).await;
        let bundle = JwksTrustBundle::new(url);
        assert!(bundle.public_key("other").await.is_none());
        assert_eq!(hits.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn jwks_bundle_unreachable_endpoint_fails_closed() {
        let bundle = JwksTrustBundle::new("http://127.0.0.1:1/jwks");
        assert!(bundle.public_key("k1").await.is_none());
    }

    #[tokio::test]
    async fn jwks_bundle_malformed_json_fails_closed() {
        let (url, _) = mock_server(vec![(200, "not json".into())]).await;
        let bundle = JwksTrustBundle::new(url);
        assert!(bundle.public_key("k1").await.is_none());
    }

    #[tokio::test]
    async fn jwks_bundle_bad_entry_fails_closed() {
        let body = serde_json::json!({"keys":[{"kid":"k1","x":"!!!not-base64!!!"}]}).to_string();
        let (url, _) = mock_server(vec![(200, body)]).await;
        let bundle = JwksTrustBundle::new(url);
        assert!(bundle.public_key("k1").await.is_none());
        let err = bundle.refresh().await.unwrap_err();
        assert!(
            matches!(err, ServiceAuthError::InvalidToken(m) if m.contains("bad JWKS entry k1"))
        );
    }

    /// Accepts connections and never answers -- an unbounded wait unless the
    /// client carries a request timeout.
    async fn hung_server() -> String {
        let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
        let addr = listener.local_addr().expect("addr");
        tokio::spawn(async move {
            let mut held = Vec::new();
            while let Ok((sock, _)) = listener.accept().await {
                held.push(sock);
            }
        });
        format!("http://{addr}")
    }

    fn fast_timeouts() -> HttpTimeouts {
        HttpTimeouts {
            connect: Duration::from_millis(200),
            request: Duration::from_millis(300),
        }
    }

    #[test]
    fn default_timeouts_are_finite_and_env_parsing_never_yields_unbounded() {
        let d = HttpTimeouts::default();
        assert!(d.connect > Duration::ZERO && d.request >= d.connect);
        let def = Duration::from_secs(7);
        assert_eq!(parse_secs(None, def), def);
        assert_eq!(parse_secs(Some(""), def), def);
        assert_eq!(
            parse_secs(Some("0"), def),
            def,
            "zero must not disable the timeout"
        );
        assert_eq!(parse_secs(Some("abc"), def), def);
        assert_eq!(parse_secs(Some(" 5 "), def), Duration::from_secs(5));
    }

    #[tokio::test]
    async fn jwks_fetch_times_out_instead_of_hanging() {
        let url = hung_server().await;
        let cfg = JwksConfig {
            http: fast_timeouts(),
            negative_ttl: Duration::from_secs(30),
        };
        let bundle = JwksTrustBundle::with_config(url, cfg).expect("client");
        let started = Instant::now();
        let got = tokio::time::timeout(Duration::from_secs(5), bundle.public_key("k1"))
            .await
            .expect("must resolve via the client timeout, not hang");
        assert!(got.is_none(), "fail closed on timeout");
        assert!(started.elapsed() < Duration::from_secs(3));
    }

    #[tokio::test]
    async fn machine_client_request_times_out_instead_of_hanging() {
        let url = hung_server().await;
        let sa = temp_sa_token("hung", "sa-token");
        let client =
            MachineJwtClient::with_timeouts(url, sa.clone(), "s", fast_timeouts()).expect("client");
        let err = tokio::time::timeout(Duration::from_secs(5), client.get_token())
            .await
            .expect("must resolve via the client timeout, not hang")
            .unwrap_err();
        assert!(
            matches!(&err, ServiceAuthError::Http(e) if e.is_timeout()),
            "{err:?}"
        );
        let _ = std::fs::remove_file(sa);
    }

    #[tokio::test]
    async fn concurrent_unknown_kid_lookups_trigger_exactly_one_fetch() {
        // 150ms server delay guarantees all 16 callers overlap the first fetch.
        let (url, hits) =
            mock_server_delayed(vec![(200, jwks_body("k1"))], Duration::from_millis(150)).await;
        let bundle = Arc::new(JwksTrustBundle::new(url));
        let mut tasks = Vec::new();
        for _ in 0..16 {
            let b = bundle.clone();
            tasks.push(tokio::spawn(async move {
                b.public_key("bogus").await.is_none()
            }));
        }
        for t in tasks {
            assert!(t.await.expect("join"), "unknown kid is still rejected");
        }
        assert_eq!(
            hits.load(Ordering::SeqCst),
            1,
            "single-flight: one refetch for N callers"
        );
    }

    #[tokio::test]
    async fn concurrent_lookups_of_a_new_valid_kid_share_one_fetch_and_all_succeed() {
        let (url, hits) =
            mock_server_delayed(vec![(200, jwks_body("k1"))], Duration::from_millis(150)).await;
        let bundle = Arc::new(JwksTrustBundle::new(url));
        let mut tasks = Vec::new();
        for _ in 0..16 {
            let b = bundle.clone();
            tasks.push(tokio::spawn(
                async move { b.public_key("k1").await.is_some() },
            ));
        }
        for t in tasks {
            assert!(t.await.expect("join"));
        }
        assert_eq!(hits.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn bogus_kid_is_negatively_cached_then_refetched_after_ttl() {
        let (url, hits) = mock_server(vec![(200, jwks_body("k1"))]).await;
        let cfg = JwksConfig {
            http: HttpTimeouts::default(),
            negative_ttl: Duration::from_millis(300),
        };
        let bundle = JwksTrustBundle::with_config(url, cfg).expect("client");
        assert!(bundle.public_key("bogus").await.is_none());
        assert_eq!(hits.load(Ordering::SeqCst), 1);
        for _ in 0..5 {
            assert!(bundle.public_key("bogus").await.is_none());
        }
        assert_eq!(hits.load(Ordering::SeqCst), 1, "within TTL: no refetch");
        tokio::time::sleep(Duration::from_millis(400)).await;
        assert!(bundle.public_key("bogus").await.is_none());
        assert_eq!(
            hits.load(Ordering::SeqCst),
            2,
            "after TTL: exactly one more refetch"
        );
    }

    #[tokio::test]
    async fn negative_cache_does_not_block_a_different_valid_kid() {
        let (url, _hits) = mock_server(vec![(200, jwks_body("k1"))]).await;
        let bundle = JwksTrustBundle::new(url);
        assert!(bundle.public_key("bogus").await.is_none());
        assert!(
            bundle.public_key("k1").await.is_some(),
            "rotation to a real kid still works"
        );
    }

    #[tokio::test]
    async fn poisoned_negative_cache_fails_closed_without_refetch() {
        let (url, hits) = mock_server(vec![(200, jwks_body("k1"))]).await;
        let bundle = std::sync::Arc::new(JwksTrustBundle::new(url));
        let b = bundle.clone();
        let _ = std::thread::spawn(move || {
            let _guard = b.negative.lock().expect("lock");
            panic!("poison the negative cache");
        })
        .join();
        assert!(bundle.negative.is_poisoned());
        assert!(bundle.is_negatively_cached("anything"), "fails closed");
        bundle.remember_unknown("x"); // must not panic or recover the map
        assert!(bundle.public_key("bogus").await.is_none());
        assert_eq!(
            hits.load(Ordering::SeqCst),
            0,
            "no refetch on poisoned cache"
        );
    }

    #[tokio::test]
    async fn negative_cache_is_bounded() {
        let (url, _hits) = mock_server(vec![(200, jwks_body("k1"))]).await;
        let bundle = JwksTrustBundle::new(url);
        for i in 0..(NEGATIVE_CACHE_MAX_ENTRIES + 50) {
            bundle.remember_unknown(&format!("kid-{i}"));
        }
        let len = bundle.negative.lock().expect("lock").len();
        assert!(len <= NEGATIVE_CACHE_MAX_ENTRIES);
    }

    #[tokio::test]
    async fn jwks_bundle_refresh_replaces_cache_on_rotation() {
        let (url, hits) = mock_server(vec![(200, jwks_body("old")), (200, jwks_body("new"))]).await;
        let bundle = JwksTrustBundle::new(url);
        assert!(bundle.public_key("old").await.is_some());
        assert!(bundle.public_key("new").await.is_some());
        assert!(
            bundle.public_key("old").await.is_none(),
            "refresh replaces the cache wholesale"
        );
        assert!(hits.load(Ordering::SeqCst) >= 3);
    }

    #[tokio::test]
    async fn machine_client_mints_and_caches_token() {
        let body = serde_json::json!({"token":"tok-1","expires_in":900}).to_string();
        let (url, hits) = mock_server(vec![(200, body)]).await;
        let sa = temp_sa_token("mint", "sa-token\n");
        let client = MachineJwtClient::new(url, sa.clone(), "identity:ephemeral:mint");
        assert_eq!(&*client.get_token().await.expect("mint"), "tok-1");
        assert_eq!(&*client.get_token().await.expect("cached"), "tok-1");
        assert_eq!(hits.load(Ordering::SeqCst), 1);
        let _ = std::fs::remove_file(sa);
    }

    #[tokio::test]
    async fn machine_client_refreshes_near_expiry() {
        let first =
            serde_json::json!({"token":"tok-1","expires_in":CLOCK_SKEW_SECONDS}).to_string();
        let second = serde_json::json!({"token":"tok-2","expires_in":900}).to_string();
        let (url, hits) = mock_server(vec![(200, first), (200, second)]).await;
        let sa = temp_sa_token("refresh", "sa");
        let client = MachineJwtClient::new(url, sa.clone(), "s");
        assert_eq!(&*client.get_token().await.expect("first"), "tok-1");
        assert_eq!(&*client.get_token().await.expect("second"), "tok-2");
        assert_eq!(hits.load(Ordering::SeqCst), 2);
        let _ = std::fs::remove_file(sa);
    }

    #[tokio::test]
    async fn machine_client_clamps_ttl_to_platform_max() {
        let body =
            serde_json::json!({"token":"t","expires_in": 10 * MAX_TOKEN_TTL_SECONDS}).to_string();
        let (url, _) = mock_server(vec![(200, body)]).await;
        let sa = temp_sa_token("clamp", "sa");
        let client = MachineJwtClient::new(url, sa.clone(), "s");
        let before = now_secs();
        client.get_token().await.expect("mint");
        let exp = client
            .cached
            .read()
            .await
            .as_ref()
            .expect("cached")
            .expires_at;
        assert!(exp <= before + MAX_TOKEN_TTL_SECONDS + 5);
        let _ = std::fs::remove_file(sa);
    }

    #[tokio::test]
    async fn machine_client_bootstrap_rejected_on_non_success() {
        let (url, _) = mock_server(vec![(403, "{}".into())]).await;
        let sa = temp_sa_token("reject", "sa");
        let client = MachineJwtClient::new(url, sa.clone(), "s");
        let err = client.get_token().await.unwrap_err();
        assert!(matches!(err, ServiceAuthError::BootstrapRejected(m) if m.contains("403")));
        assert!(
            client.cached.read().await.is_none(),
            "nothing cached on rejection"
        );
        let _ = std::fs::remove_file(sa);
    }

    #[tokio::test]
    async fn machine_client_missing_sa_token_is_io_error() {
        let client = MachineJwtClient::new("http://127.0.0.1:1/t", "/nonexistent/sa/token", "s");
        assert!(matches!(
            client.get_token().await.unwrap_err(),
            ServiceAuthError::Io(_)
        ));
    }

    #[tokio::test]
    async fn machine_client_malformed_body_is_http_error() {
        let (url, _) = mock_server(vec![(200, "garbage".into())]).await;
        let sa = temp_sa_token("badbody", "sa");
        let client = MachineJwtClient::new(url, sa.clone(), "s");
        assert!(matches!(
            client.get_token().await.unwrap_err(),
            ServiceAuthError::Http(_)
        ));
        let _ = std::fs::remove_file(sa);
    }

    #[tokio::test]
    async fn machine_client_unreachable_endpoint_is_http_error() {
        let sa = temp_sa_token("unreach", "sa");
        let client = MachineJwtClient::new("http://127.0.0.1:1/t", sa.clone(), "s");
        assert!(matches!(
            client.get_token().await.unwrap_err(),
            ServiceAuthError::Http(_)
        ));
        let _ = std::fs::remove_file(sa);
    }

    // ---- Phase-0 hardening (RFC 8725) ------------------------------------

    use crate::jwt_hardening::{
        FORBIDDEN_HEADER_PARAMS, REASON_ALG_MISMATCH, REASON_ALG_NONE, REASON_BAD_KID,
        REASON_FORBIDDEN_HEADER, REASON_NO_KEY,
    };
    use crate::test_support::Capture;
    use serde_json::json;

    const AUD: &str = "waddlebot-internal";
    const SCOPE: &str = "identity:ephemeral:mint";

    fn b64_json(value: &Value) -> String {
        URL_SAFE_NO_PAD.encode(value.to_string())
    }

    /// Sign arbitrary header + claims JSON with key A, so a test controls
    /// exactly which property of an otherwise validly-signed token is hostile.
    fn raw_token(header: &Value, claims: &Value) -> String {
        let message = format!("{}.{}", b64_json(header), b64_json(claims));
        let signature = jsonwebtoken::crypto::sign(
            message.as_bytes(),
            &EncodingKey::from_ed_der(KEY_A_PRIV_DER),
            Algorithm::EdDSA,
        )
        .expect("sign");
        format!("{message}.{signature}")
    }

    fn good_header() -> Value {
        json!({"alg": "EdDSA", "typ": "JWT", "kid": "k1"})
    }

    fn good_claims() -> Value {
        let now = now_secs();
        json!({
            "iss": "hub-api", "aud": AUD,
            "sub": "spiffe://penguintech.io/alpha/svc-process",
            "scope": SCOPE, "iat": now, "nbf": now, "exp": now + 900, "jti": "jti-1",
        })
    }

    fn claims_without(name: &str) -> Value {
        let mut claims = good_claims();
        claims.as_object_mut().expect("object").remove(name);
        claims
    }

    fn claims_with(name: &str, value: Value) -> Value {
        let mut claims = good_claims();
        claims[name] = value;
        claims
    }

    /// Trust bundle that counts lookups, to prove a hostile header never
    /// reaches key resolution.
    struct CountingBundle {
        inner: StaticTrustBundle,
        lookups: AtomicUsize,
    }

    #[async_trait::async_trait]
    impl TrustBundle for CountingBundle {
        async fn public_key(&self, kid: &str) -> Option<DecodingKey> {
            self.lookups.fetch_add(1, Ordering::SeqCst);
            self.inner.public_key(kid).await
        }
    }

    fn counting_bundle() -> CountingBundle {
        let (_enc, dec) = ed25519_keypair();
        CountingBundle {
            inner: StaticTrustBundle(Mutex::new(HashMap::from([("k1".to_string(), dec)]))),
            lookups: AtomicUsize::new(0),
        }
    }

    /// Run the real verifier against a capturing meter provider.
    async fn run(
        token: &str,
        bundle: &dyn TrustBundle,
    ) -> (Result<ServiceClaims, ServiceAuthError>, Capture) {
        let capture = Capture::new();
        let result =
            verify_with_metrics(&capture.metrics, token, bundle, AUD, &["hub-api"], SCOPE).await;
        (result, capture)
    }

    /// Assert `token` is refused with exactly `reason`, counted once under
    /// `alg` / `service_eddsa`, and never reached the trust bundle iff
    /// `before_lookup`.
    async fn assert_refused(token: &str, reason: &str, alg: &str, before_lookup: bool) {
        let bundle = counting_bundle();
        let (result, capture) = run(token, &bundle).await;
        let err = result.expect_err(&format!("{reason} must be refused"));
        match reason {
            "unknown_kid" => assert!(matches!(err, ServiceAuthError::UnknownKeyId(_)), "{reason}"),
            _ => assert!(
                matches!(&err, ServiceAuthError::InvalidToken(m) if *m == format!("rejected ({reason})")),
                "{reason}: got {err:?}"
            ),
        }
        assert_eq!(
            capture.count("service_eddsa", alg, reason),
            1,
            "{reason}/{alg}"
        );
        assert_eq!(capture.total(), 1, "exactly one verification recorded");
        if before_lookup {
            assert_eq!(
                bundle.lookups.load(Ordering::SeqCst),
                0,
                "{reason} reached the trust bundle"
            );
        }
    }

    #[tokio::test]
    async fn a_valid_token_is_counted_ok_with_the_eddsa_label() {
        let bundle = counting_bundle();
        let (result, capture) = run(&raw_token(&good_header(), &good_claims()), &bundle).await;
        let claims = result.expect("valid token verifies");
        assert_eq!(claims.sub, "spiffe://penguintech.io/alpha/svc-process");
        assert_eq!(claims.aud, AUD);
        assert_eq!(claims.jti, "jti-1");
        assert_eq!(capture.count("service_eddsa", "eddsa", "ok"), 1);
        assert_eq!(capture.total(), 1);
        let latency = capture.points("waddles_jwt_verification_seconds");
        assert_eq!(latency.len(), 1);
        assert_eq!(latency[0].value, 1);
    }

    #[tokio::test]
    async fn alg_none_is_rejected_in_every_letter_case_before_key_lookup() {
        for alg in ["none", "None", "NONE", "nOnE"] {
            let header = json!({"alg": alg, "typ": "JWT", "kid": "k1"});
            // Unsigned token: empty signature segment.
            let token = format!("{}.{}.", b64_json(&header), b64_json(&good_claims()));
            assert_refused(&token, REASON_ALG_NONE, "none", true).await;
        }
    }

    #[tokio::test]
    async fn alg_none_with_a_real_signature_attached_is_still_rejected() {
        let header = json!({"alg": "none", "kid": "k1"});
        assert_refused(
            &raw_token(&header, &good_claims()),
            REASON_ALG_NONE,
            "none",
            true,
        )
        .await;
    }

    #[tokio::test]
    async fn alg_confusion_hmac_signed_with_the_public_key_is_rejected() {
        // Classic RS/ES -> HS confusion: the attacker HMACs with the (public)
        // verification key bytes and relabels the header HS*.
        for (alg, label) in [
            (Algorithm::HS256, "hs256"),
            (Algorithm::HS384, "hs384"),
            (Algorithm::HS512, "hs512"),
        ] {
            let mut header = Header::new(alg);
            header.kid = Some("k1".into());
            let token = jsonwebtoken::encode(
                &header,
                &base_claims(now_secs()),
                &EncodingKey::from_secret(KEY_A_PUB_RAW),
            )
            .expect("encode");
            assert_refused(&token, REASON_ALG_MISMATCH, label, true).await;
        }
    }

    #[tokio::test]
    async fn other_asymmetric_algorithms_and_odd_alg_values_are_a_mismatch() {
        for (alg_json, label) in [
            (json!("RS256"), "rs256"),
            (json!("ES256"), "es256"),
            (json!("PS512"), "ps512"),
            (json!("eddsa"), "eddsa"),
            (json!("Ed25519"), "other"),
            (json!(7), "other"),
            (json!(["EdDSA"]), "other"),
            (Value::Null, "absent"),
        ] {
            let header = json!({"alg": alg_json, "kid": "k1"});
            assert_refused(
                &raw_token(&header, &good_claims()),
                REASON_ALG_MISMATCH,
                label,
                true,
            )
            .await;
        }
        let header = json!({"kid": "k1"});
        assert_refused(
            &raw_token(&header, &good_claims()),
            REASON_ALG_MISMATCH,
            "absent",
            true,
        )
        .await;
    }

    #[tokio::test]
    async fn key_material_headers_are_rejected_even_with_a_valid_signature() {
        for param in FORBIDDEN_HEADER_PARAMS {
            let mut header = good_header();
            header[param] = json!("https://attacker.example/keys");
            let token = raw_token(&header, &good_claims());
            assert_refused(&token, REASON_FORBIDDEN_HEADER, "eddsa", true).await;
        }
    }

    #[tokio::test]
    async fn crit_header_is_rejected_whatever_it_names() {
        let mut header = good_header();
        header["crit"] = json!(["exp"]);
        assert_refused(
            &raw_token(&header, &good_claims()),
            REASON_FORBIDDEN_HEADER,
            "eddsa",
            true,
        )
        .await;
    }

    #[tokio::test]
    async fn hostile_kids_never_reach_the_trust_bundle() {
        for kid in [
            json!("k1; DROP TABLE"),
            json!("../../etc/passwd"),
            json!("k1\nX"),
            json!(5),
            json!("k".repeat(65)),
        ] {
            let mut header = good_header();
            header["kid"] = kid;
            assert_refused(
                &raw_token(&header, &good_claims()),
                REASON_BAD_KID,
                "eddsa",
                true,
            )
            .await;
        }
    }

    #[tokio::test]
    async fn absent_and_unknown_kid_are_unknown_kid_and_do_not_leak_a_value() {
        let bundle = counting_bundle();
        let header = json!({"alg": "EdDSA"});
        let (result, capture) = run(&raw_token(&header, &good_claims()), &bundle).await;
        assert!(matches!(result, Err(ServiceAuthError::UnknownKeyId(None))));
        assert_eq!(capture.count("service_eddsa", "eddsa", "unknown_kid"), 1);
        assert_eq!(
            bundle.lookups.load(Ordering::SeqCst),
            0,
            "no kid, no lookup"
        );

        let header = json!({"alg": "EdDSA", "kid": "rotated-away"});
        let (result, capture) = run(&raw_token(&header, &good_claims()), &bundle).await;
        assert!(
            matches!(&result, Err(ServiceAuthError::UnknownKeyId(Some(k))) if k == "rotated-away")
        );
        assert_eq!(capture.count("service_eddsa", "eddsa", "unknown_kid"), 1);
        assert_eq!(bundle.lookups.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn structurally_broken_tokens_are_malformed_with_no_lookup() {
        let ok = b64_json(&good_header());
        let payload = b64_json(&good_claims());
        for token in [
            String::new(),
            "a.b".to_string(),
            format!("{ok}.{payload}"),
            format!("{ok}.{payload}.sig.extra"),
            format!("%%%.{payload}.sig"),
            format!("{}.{payload}.sig", URL_SAFE_NO_PAD.encode("[1]")),
        ] {
            assert_refused(&token, REASON_MALFORMED, "absent", true).await;
        }
    }

    #[tokio::test]
    async fn every_required_claim_missing_is_missing_claim() {
        for name in REQUIRED_SERVICE_CLAIMS {
            let token = raw_token(&good_header(), &claims_without(name));
            assert_refused(&token, REASON_MISSING_CLAIM, "eddsa", false).await;
        }
    }

    #[tokio::test]
    async fn null_required_claims_count_as_missing() {
        for name in ["iat", "scope", "jti", "sub"] {
            let token = raw_token(&good_header(), &claims_with(name, Value::Null));
            let bundle = counting_bundle();
            let (result, capture) = run(&token, &bundle).await;
            assert!(result.is_err(), "{name}=null must not verify");
            assert_eq!(capture.total(), 1);
            assert_eq!(capture.count("service_eddsa", "eddsa", "ok"), 0, "{name}");
        }
    }

    #[tokio::test]
    async fn empty_or_mistyped_identity_claims_are_invalid() {
        for (name, value) in [
            ("sub", json!("")),
            ("sub", json!("   ")),
            ("sub", json!(12)),
            ("jti", json!("")),
            ("scope", json!(["a"])),
            ("scope", json!(1)),
            ("iat", json!("now")),
            ("iat", json!(1.5)),
            ("iat", json!(-1)),
        ] {
            let token = raw_token(&good_header(), &claims_with(name, value.clone()));
            let bundle = counting_bundle();
            let (result, capture) = run(&token, &bundle).await;
            assert!(result.is_err(), "{name}={value} must not verify");
            assert_eq!(capture.total(), 1);
            assert_eq!(
                capture.count("service_eddsa", "eddsa", "ok"),
                0,
                "{name}={value}"
            );
        }
        assert_refused(
            &raw_token(&good_header(), &claims_with("sub", json!(""))),
            REASON_INVALID_CLAIM,
            "eddsa",
            false,
        )
        .await;
        assert_refused(
            &raw_token(&good_header(), &claims_with("scope", json!(1))),
            REASON_INVALID_CLAIM,
            "eddsa",
            false,
        )
        .await;
    }

    #[tokio::test]
    async fn tenant_is_optional_but_never_empty() {
        let bundle = counting_bundle();
        let (absent, _) = run(&raw_token(&good_header(), &good_claims()), &bundle).await;
        absent.expect("machine tokens without a tenant (egress:connect) still verify");

        let (null, _) = run(
            &raw_token(&good_header(), &claims_with("tenant", Value::Null)),
            &bundle,
        )
        .await;
        null.expect("a null tenant is the same as absent");

        let (set, _) = run(
            &raw_token(&good_header(), &claims_with("tenant", json!("system"))),
            &bundle,
        )
        .await;
        set.expect("a real tenant verifies");

        for bad in [
            json!(""),
            json!("  "),
            json!(0),
            json!(["system"]),
            json!({"slug": "x"}),
        ] {
            let token = raw_token(&good_header(), &claims_with("tenant", bad.clone()));
            assert_refused(&token, REASON_INVALID_CLAIM, "eddsa", false).await;
        }
    }

    #[tokio::test]
    async fn issuer_and_audience_are_enforced_not_optional() {
        assert_refused(
            &raw_token(&good_header(), &claims_without("iss")),
            REASON_MISSING_CLAIM,
            "eddsa",
            false,
        )
        .await;
        assert_refused(
            &raw_token(&good_header(), &claims_without("aud")),
            REASON_MISSING_CLAIM,
            "eddsa",
            false,
        )
        .await;
        assert_refused(
            &raw_token(&good_header(), &claims_with("iss", json!("evil"))),
            REASON_BAD_ISSUER,
            "eddsa",
            false,
        )
        .await;
        assert_refused(
            &raw_token(&good_header(), &claims_with("aud", json!("other"))),
            REASON_BAD_AUDIENCE,
            "eddsa",
            false,
        )
        .await;
        assert_refused(
            &raw_token(&good_header(), &claims_with("aud", json!(["a", "b"]))),
            REASON_BAD_AUDIENCE,
            "eddsa",
            false,
        )
        .await;
    }

    #[tokio::test]
    async fn an_audience_list_containing_the_expected_audience_is_accepted() {
        let bundle = counting_bundle();
        let token = raw_token(&good_header(), &claims_with("aud", json!(["other", AUD])));
        let (result, _) = run(&token, &bundle).await;
        assert_eq!(
            result.expect("list aud containing the expected value").aud,
            AUD
        );
    }

    #[tokio::test]
    async fn empty_expected_audience_or_issuer_fails_closed() {
        let bundle = counting_bundle();
        let capture = Capture::new();
        // A token that would match an empty expectation exactly.
        let token = raw_token(&good_header(), &claims_with("aud", json!("")));
        let err = verify_with_metrics(&capture.metrics, &token, &bundle, "", &["hub-api"], SCOPE)
            .await
            .unwrap_err();
        assert!(matches!(err, ServiceAuthError::InvalidToken(m) if m == "rejected (bad_audience)"));
        assert_eq!(capture.count("service_eddsa", "absent", "bad_audience"), 1);

        let token = raw_token(&good_header(), &claims_with("iss", json!("")));
        for issuers in [&[""][..], &[][..], &["hub-api", ""][..]] {
            let err = verify_with_metrics(&capture.metrics, &token, &bundle, AUD, issuers, SCOPE)
                .await
                .unwrap_err();
            assert!(
                matches!(err, ServiceAuthError::InvalidToken(m) if m == "rejected (bad_issuer)")
            );
        }
        assert_eq!(capture.count("service_eddsa", "absent", "bad_issuer"), 3);
        assert_eq!(bundle.lookups.load(Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn time_claims_are_enforced_with_a_bounded_skew() {
        let now = now_secs();
        assert_refused(
            &raw_token(&good_header(), &claims_with("exp", json!(now - 3600))),
            REASON_EXPIRED,
            "eddsa",
            false,
        )
        .await;
        assert_refused(
            &raw_token(
                &good_header(),
                &claims_with("nbf", json!(now + CLOCK_SKEW_SECONDS + 300)),
            ),
            REASON_IMMATURE,
            "eddsa",
            false,
        )
        .await;
        // `iat` in the future beyond the skew is immature even when nbf/exp pass.
        assert_refused(
            &raw_token(
                &good_header(),
                &claims_with("iat", json!(now + CLOCK_SKEW_SECONDS + 300)),
            ),
            REASON_IMMATURE,
            "eddsa",
            false,
        )
        .await;
        let bundle = counting_bundle();
        let token = raw_token(
            &good_header(),
            &claims_with("iat", json!(now + CLOCK_SKEW_SECONDS - 5)),
        );
        run(&token, &bundle)
            .await
            .0
            .expect("iat within skew verifies");
    }

    #[tokio::test]
    async fn bad_signature_and_wrong_scope_have_their_own_outcomes() {
        let forged = EncodingKey::from_ed_der(KEY_B_PRIV_DER);
        let message = format!("{}.{}", b64_json(&good_header()), b64_json(&good_claims()));
        let sig = jsonwebtoken::crypto::sign(message.as_bytes(), &forged, Algorithm::EdDSA)
            .expect("sign");
        assert_refused(
            &format!("{message}.{sig}"),
            REASON_BAD_SIGNATURE,
            "eddsa",
            false,
        )
        .await;

        assert_refused(
            &raw_token(
                &good_header(),
                &claims_with("scope", json!("some:other:scope")),
            ),
            REASON_SCOPE_DENIED,
            "eddsa",
            false,
        )
        .await;
    }

    #[test]
    fn decode_errors_map_to_closed_reasons() {
        for (kind, reason) in [
            (ErrorKind::ExpiredSignature, REASON_EXPIRED),
            (ErrorKind::ImmatureSignature, REASON_IMMATURE),
            (ErrorKind::InvalidSignature, REASON_BAD_SIGNATURE),
            (ErrorKind::InvalidIssuer, REASON_BAD_ISSUER),
            (ErrorKind::InvalidAudience, REASON_BAD_AUDIENCE),
            (
                ErrorKind::MissingRequiredClaim("exp".into()),
                REASON_MISSING_CLAIM,
            ),
            (
                ErrorKind::InvalidClaimFormat("exp".into()),
                REASON_INVALID_CLAIM,
            ),
            (ErrorKind::InvalidAlgorithm, REASON_ALG_MISMATCH),
            (ErrorKind::InvalidAlgorithmName, REASON_ALG_MISMATCH),
            (ErrorKind::MissingAlgorithm, REASON_ALG_MISMATCH),
            (ErrorKind::InvalidToken, REASON_MALFORMED),
            (ErrorKind::InvalidEddsaKey, REASON_INVALID),
        ] {
            assert_eq!(classify_decode_error(&kind), reason, "{kind:?}");
        }
    }

    #[tokio::test]
    async fn verify_without_an_installed_meter_provider_still_decides_correctly() {
        // The public entry point records via the global (here: no-op) provider.
        let bundle = counting_bundle();
        let good = raw_token(&good_header(), &good_claims());
        verify(&good, &bundle, AUD, &["hub-api"], SCOPE)
            .await
            .expect("verifies");
        let none = format!(
            "{}.{}.",
            b64_json(&json!({"alg": "none", "kid": "k1"})),
            b64_json(&good_claims())
        );
        assert!(verify(&none, &bundle, AUD, &["hub-api"], SCOPE)
            .await
            .is_err());
    }

    #[derive(Clone, Default)]
    struct LogBuf(Arc<Mutex<Vec<u8>>>);

    impl std::io::Write for LogBuf {
        fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
            self.0.lock().expect("log lock").extend_from_slice(buf);
            Ok(buf.len())
        }

        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }

    impl<'a> tracing_subscriber::fmt::MakeWriter<'a> for LogBuf {
        type Writer = LogBuf;

        fn make_writer(&'a self) -> Self::Writer {
            self.clone()
        }
    }

    /// Process-wide log capture. A *global* default (installed once) rather
    /// than a per-test thread-local one: tracing caches per-callsite interest
    /// globally, so a callsite first hit by a parallel test with no
    /// subscriber would otherwise stay disabled for a scoped one.
    fn captured_logs() -> &'static LogBuf {
        static LOGS: std::sync::OnceLock<LogBuf> = std::sync::OnceLock::new();
        LOGS.get_or_init(|| {
            let buf = LogBuf::default();
            let subscriber = tracing_subscriber::fmt()
                .with_writer(buf.clone())
                .with_max_level(tracing::Level::DEBUG)
                .with_ansi(false)
                .finish();
            tracing::subscriber::set_global_default(subscriber).expect("install log capture");
            buf
        })
    }

    fn logged() -> String {
        let bytes = captured_logs().0.lock().expect("log lock").clone();
        String::from_utf8(bytes).expect("utf8")
    }

    #[tokio::test]
    async fn rejections_log_the_closed_vocabulary_and_never_token_material() {
        captured_logs();
        let secret_sub = "spiffe://penguintech.io/alpha/SECRET-SUBJECT-MARKER";
        let mut header = good_header();
        header["jku"] = json!("https://attacker.example/SECRET-JKU-MARKER");
        let hostile = raw_token(&header, &claims_with("sub", json!(secret_sub)));
        let bundle = counting_bundle();
        let capture = Capture::new();
        verify_with_metrics(
            &capture.metrics,
            &hostile,
            &bundle,
            AUD,
            &["hub-api"],
            SCOPE,
        )
        .await
        .unwrap_err();
        let ok = raw_token(&good_header(), &claims_with("sub", json!(secret_sub)));
        verify_with_metrics(&capture.metrics, &ok, &bundle, AUD, &["hub-api"], SCOPE)
            .await
            .expect("valid");

        let logged = logged();
        assert!(logged.contains("JWT rejected"), "{logged}");
        assert!(logged.contains("forbidden_header"), "{logged}");
        assert!(logged.contains("service_eddsa"), "{logged}");
        assert!(
            logged.contains("JWT verified"),
            "success logged at DEBUG: {logged}"
        );
        for needle in [
            "SECRET-SUBJECT-MARKER",
            "SECRET-JKU-MARKER",
            hostile.as_str(),
            ok.as_str(),
            b64_json(&good_claims()).as_str(),
        ] {
            assert!(!logged.contains(needle), "log leaked {needle:.30}");
        }
    }

    #[test]
    fn no_key_is_logged_critical() {
        captured_logs();
        crate::jwt_hardening::log_rejection(VERIFIER_SERVICE_EDDSA, REASON_NO_KEY, "absent");
        let logged = logged();
        let line = logged
            .lines()
            .find(|line| line.contains("verifier has no signing key configured"))
            .expect("no_key line is logged");
        assert!(
            line.contains("ERROR") && line.contains("severity=\"critical\""),
            "{line}"
        );
    }

    #[tokio::test]
    async fn jwks_entry_with_an_empty_key_is_rejected_fail_closed() {
        let body = serde_json::json!({"keys":[{"kid":"k1","x":""}]}).to_string();
        let (url, _) = mock_server(vec![(200, body)]).await;
        let bundle = JwksTrustBundle::new(url);
        assert!(bundle.public_key("k1").await.is_none());
        let err = bundle.refresh().await.unwrap_err();
        assert!(matches!(err, ServiceAuthError::InvalidToken(m) if m.contains("empty key")));
    }

    /// A complete, fixed-time claim payload for the direct `parse_claims` tests.
    fn fixed_payload(iat: u64) -> Value {
        json!({
            "iss": "hub-api", "aud": AUD, "sub": "spiffe://penguintech.io/alpha/svc-process",
            "scope": SCOPE, "iat": iat, "nbf": iat, "exp": iat + 900, "jti": "jti-1",
        })
    }

    #[test]
    fn iat_skew_boundary_is_inclusive_and_exact() {
        let now = 1_000_000_u64;
        assert!(
            parse_claims(&fixed_payload(now + CLOCK_SKEW_SECONDS), AUD, now).is_ok(),
            "iat exactly at the skew edge verifies"
        );
        assert!(parse_claims(&fixed_payload(now), AUD, now).is_ok());
        assert!(parse_claims(&fixed_payload(0), AUD, now).is_ok());
        assert_eq!(
            parse_claims(&fixed_payload(now + CLOCK_SKEW_SECONDS + 1), AUD, now).unwrap_err(),
            REASON_IMMATURE
        );
    }

    #[test]
    fn skew_and_ttl_constants_match_the_python_contract() {
        assert_eq!(CLOCK_SKEW_SECONDS, 30);
        assert_eq!(MAX_TOKEN_TTL_SECONDS, 3600);
        assert_eq!(SERVICE_JWT_ALGORITHM, "EdDSA");
    }
}
