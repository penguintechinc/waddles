//! Per-service ES256 machine JWTs: verification + a caching client.
//!
//! Replaces the platform-wide HS256 shared `SECRET_KEY`
//! (`libs/flask_core/flask_core/auth.py`) and static API keys for
//! service-to-service calls. hub-api issues short-lived (<=1h) ES256
//! (ECDSA P-256 / SHA-256) JWTs per service identity (`sub` = a SPIFFE ID,
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
//! (Kubernetes ServiceAccount token -> hub-api `ServiceAuthService.MintServiceToken` over gRPC, TLS 1.3 -- see [`grpc`]) is the part
//! that's replaced outright by a SPIFFE Workload API call once Skauswatch
//! is adopted; it's isolated behind the same client type so callers
//! (`svc_process`, `svc_action`) don't change either.

pub mod grpc;
pub mod jwt_hardening;
#[cfg(any(test, feature = "test-support"))]
pub mod mock_hub;
#[cfg(any(test, feature = "test-support"))]
pub mod test_support;

pub use grpc::HubChannel;

use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine as _;
use jsonwebtoken::errors::ErrorKind;
use jsonwebtoken::{Algorithm, AlgorithmFamily, DecodingKey, Validation};
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

/// The algorithm hub-api signs machine JWTs with and verifiers accept:
/// ECDSA P-256 / SHA-256 (RFC 7518 section 3.4); mirrors
/// `libs/flask_core/flask_core/service_jwt.py::SERVICE_JWT_ALGORITHM`.
/// `alg=none`, HS* and every other algorithm are always refused.
pub const SERVICE_JWT_ALGORITHM: &str = "ES256";

/// Legacy algorithm, accepted ONLY while `SERVICE_JWT_ACCEPT_LEGACY_EDDSA`
/// is true (a bounded rollout window); never issued. Mirrors
/// `LEGACY_SERVICE_JWT_ALGORITHM` in the Python module.
pub const LEGACY_SERVICE_JWT_ALGORITHM: &str = "EdDSA";

/// Env flag opening the legacy EdDSA verification window. Default: false.
pub const ENV_ACCEPT_LEGACY_EDDSA: &str = "SERVICE_JWT_ACCEPT_LEGACY_EDDSA";

/// Parse [`ENV_ACCEPT_LEGACY_EDDSA`] strictly: unset/empty/`false`/`0` is off,
/// `true`/`1` is on, anything else is a loud [`ServiceAuthError::Config`] --
/// a typo in a security toggle must never silently mean "off".
pub fn accept_legacy_eddsa_from_env() -> Result<bool, ServiceAuthError> {
    parse_legacy_flag(std::env::var(ENV_ACCEPT_LEGACY_EDDSA).ok().as_deref())
}

fn parse_legacy_flag(raw: Option<&str>) -> Result<bool, ServiceAuthError> {
    match raw.map(|v| v.trim().to_ascii_lowercase()).as_deref() {
        None | Some("" | "false" | "0") => Ok(false),
        Some("true" | "1") => Ok(true),
        Some(_) => Err(ServiceAuthError::Config(format!(
            "{ENV_ACCEPT_LEGACY_EDDSA} must be 'true' or 'false'"
        ))),
    }
}

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
    #[error("hub-api gRPC call failed: {0}")]
    Grpc(#[from] tonic::Status),
    #[error("invalid hub-api gRPC configuration: {0}")]
    Config(String),
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

    /// Whether this verifier's legacy EdDSA window is open
    /// (`SERVICE_JWT_ACCEPT_LEGACY_EDDSA`). Closed (`false`) unless a bundle
    /// is explicitly configured otherwise, so ES256 is the only algorithm
    /// accepted by default.
    fn accept_legacy_eddsa(&self) -> bool {
        false
    }
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
/// any key lookup -- `alg` must be exactly [`SERVICE_JWT_ALGORITHM`] (or [`LEGACY_SERVICE_JWT_ALGORITHM`] while the bundle's legacy window is open; so
/// `alg: none`, an HMAC token "signed" with the public key, or any other
/// algorithm is refused outright), `jku`/`jwk`/`x5u`/`x5c`/`crit` are
/// refused, and a `kid` outside the pinned charset never reaches the trust
/// bundle. An empty expected audience or issuer is a misconfiguration and
/// fails closed. Every call emits `waddles_jwt_verifications_total` with
/// `verifier=service_eddsa` (label kept for dashboard continuity; the `alg` label says `es256`/`eddsa`); nothing logged ever contains token material.
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

    let allowed: &[&str] = if trust_bundle.accept_legacy_eddsa() {
        &[SERVICE_JWT_ALGORITHM, LEGACY_SERVICE_JWT_ALGORITHM]
    } else {
        &[SERVICE_JWT_ALGORITHM]
    };
    let header = inspect_header(token, allowed, KidPolicy::Vet).map_err(|r| {
        *alg = r.alg;
        Failure::new(r)
    })?;
    // `inspect_header` only returns an allow-listed alg, so this is exhaustive.
    let (algorithm, family, label) = if header.alg == LEGACY_SERVICE_JWT_ALGORITHM {
        (Algorithm::EdDSA, AlgorithmFamily::Ed, "eddsa")
    } else {
        (Algorithm::ES256, AlgorithmFamily::Ec, "es256")
    };
    *alg = label;
    let Some(kid) = header.kid else {
        return Err(Failure::reason(REASON_UNKNOWN_KID, label));
    };
    let Some(key) = trust_bundle.public_key(&kid).await else {
        return Err(Failure {
            rejection: JwtRejection::new(REASON_UNKNOWN_KID, label),
            kid: Some(kid),
        });
    };
    // Key/alg confusion guard: the key found for `kid` must be of the family
    // the token's `alg` claims (an EdDSA token naming an EC `kid` is refused
    // before any signature work).
    if key.family() != family {
        return Err(Failure::reason(REASON_ALG_MISMATCH, label));
    }

    let mut validation = Validation::new(algorithm);
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
        .map_err(|e| Failure::reason(classify_decode_error(e.kind()), label))?;
    let claims = parse_claims(&data.claims, expected_audience, now_secs())
        .map_err(|reason| Failure::reason(reason, label))?;

    if claims.scope != required_scope {
        return Err(Failure::reason(REASON_SCOPE_DENIED, label));
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

/// Explicit connect + request timeouts applied to every outbound hub-api gRPC
/// call in this crate, so a hung hub-api can never wedge a verifier or a
/// token refresh indefinitely. (The per-RPC deadline actually sent is the request
/// timeout capped at [`grpc::MAX_RPC_DEADLINE`], hub-api's ceiling.)
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

/// Tunables for [`JwksTrustBundle`]: HTTP timeouts + unknown-`kid` negative TTL.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct JwksConfig {
    /// Timeouts for the JWKS fetch.
    pub http: HttpTimeouts,
    /// How long a `kid` absent from the JWKS (or whose refetch failed) is
    /// rejected without touching the network.
    pub negative_ttl: Duration,
    /// Legacy EdDSA window (`SERVICE_JWT_ACCEPT_LEGACY_EDDSA`): when false
    /// (the default) only ES256 / `EC` P-256 keys are trusted and `OKP`
    /// entries in the served JWKS are skipped with a warning.
    pub accept_legacy_eddsa: bool,
}

impl Default for JwksConfig {
    fn default() -> Self {
        Self {
            http: HttpTimeouts::default(),
            negative_ttl: DEFAULT_JWKS_NEGATIVE_TTL,
            accept_legacy_eddsa: false,
        }
    }
}

impl JwksConfig {
    /// Defaults overridden from the `SERVICE_AUTH_*` env vars. The legacy
    /// EdDSA window stays closed here -- call [`JwksTrustBundle::with_legacy_eddsa`]
    /// with [`accept_legacy_eddsa_from_env`] to honour `SERVICE_JWT_ACCEPT_LEGACY_EDDSA`.
    pub fn from_env() -> Self {
        Self {
            http: HttpTimeouts::from_env(),
            negative_ttl: duration_from_env(ENV_JWKS_NEGATIVE_TTL_SECS, DEFAULT_JWKS_NEGATIVE_TTL),
            accept_legacy_eddsa: false,
        }
    }
}

/// [`TrustBundle`] backed by hub-api's own JWKS (`ServiceAuthService.GetServiceJwks`, gRPC), refreshed on a
/// cache miss (new `kid` seen, e.g. mid-rotation).
///
/// Resilience: the fetch has connect + request timeouts; concurrent misses
/// are coalesced (single-flight) into one refetch; and a `kid` the JWKS does
/// not contain is negatively cached for a short TTL so a flood of bogus
/// `kid`s cannot hammer the endpoint. An unknown `kid` is still rejected
/// (fail closed) -- the caches only bound how often we re-ask.
pub struct JwksTrustBundle {
    hub: HubChannel,
    cache: RwLock<HashMap<String, DecodingKey>>,
    /// Serialises refetches: whoever holds it is the one in-flight request.
    refresh_gate: Mutex<()>,
    /// `kid` -> instant until which it is known-unknown.
    negative: std::sync::Mutex<HashMap<String, Instant>>,
    negative_ttl: Duration,
    accept_legacy_eddsa: bool,
}

impl JwksTrustBundle {
    /// Bundle for hub-api's gRPC endpoint (`https://host:port`; CA from `HUB_API_GRPC_CA_FILE`),
    /// timeouts/TTL from the environment (or defaults). Connects lazily; a bad endpoint/CA is
    /// reported as an error on every verification attempt (fail closed, loud). The legacy
    /// EdDSA window is closed; see [`JwksTrustBundle::with_legacy_eddsa`].
    pub fn new(endpoint: impl Into<String>) -> Self {
        let config = JwksConfig::from_env();
        Self::build(HubChannel::from_env(endpoint, config.http), config)
    }

    /// Open or close the legacy EdDSA window on this bundle (builder style). Production
    /// verifiers pass [`accept_legacy_eddsa_from_env`]`()?` so a malformed
    /// `SERVICE_JWT_ACCEPT_LEGACY_EDDSA` fails startup rather than silently meaning "off".
    /// Must be set before the first lookup (the JWKS cache is filtered by it).
    #[must_use]
    pub fn with_legacy_eddsa(mut self, accept: bool) -> Self {
        self.accept_legacy_eddsa = accept;
        self
    }

    /// Bundle with explicit [`JwksConfig`]; validates the endpoint and CA up front.
    pub fn with_config(
        endpoint: impl Into<String>,
        config: JwksConfig,
    ) -> Result<Self, ServiceAuthError> {
        Ok(Self::build(
            HubChannel::validated_from_env(endpoint, config.http)?,
            config,
        ))
    }

    /// Bundle over an existing (possibly shared) [`HubChannel`].
    pub fn with_channel(hub: HubChannel, config: JwksConfig) -> Self {
        Self::build(hub, config)
    }

    fn build(hub: HubChannel, config: JwksConfig) -> Self {
        Self {
            hub,
            cache: RwLock::new(HashMap::new()),
            refresh_gate: Mutex::new(()),
            negative: std::sync::Mutex::new(HashMap::new()),
            negative_ttl: config.negative_ttl,
            accept_legacy_eddsa: config.accept_legacy_eddsa,
        }
    }

    async fn refresh(&self) -> Result<(), ServiceAuthError> {
        let keys = self.hub.jwks().await?;
        // Build the replacement set first: a malformed entry fails the WHOLE refresh closed
        // and leaves the previous cache untouched.
        let mut fresh = HashMap::with_capacity(keys.len());
        for entry in keys {
            if let Some(key) = decoding_key(&entry, self.accept_legacy_eddsa)? {
                fresh.insert(entry.kid.clone(), key);
            }
        }
        let mut cache = self.cache.write().await;
        cache.clear();
        cache.extend(fresh);
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

/// Build the verification key for one served JWK. `kty=EC`/`crv=P-256` (ES256) is always
/// accepted; `kty=OKP`/`crv=Ed25519` only while the legacy window is open (otherwise the entry is
/// skipped with a warning -- `Ok(None)` -- since hub-api may still publish it during rollout).
/// Anything else, or a malformed coordinate (wrong length / not base64url), fails the refresh
/// closed instead of caching a key that verifies nothing.
fn decoding_key(
    entry: &grpc::pb::Jwk,
    accept_legacy_eddsa: bool,
) -> Result<Option<DecodingKey>, ServiceAuthError> {
    let bad =
        |why: &str| ServiceAuthError::InvalidToken(format!("bad JWKS entry {}: {why}", entry.kid));
    match (entry.kty.as_str(), entry.crv.as_str()) {
        ("EC", "P-256") => {
            for (name, coord) in [("x", &entry.x), ("y", &entry.y)] {
                let raw = URL_SAFE_NO_PAD
                    .decode(coord)
                    .map_err(|_| bad(&format!("{name} is not base64url")))?;
                if raw.len() != 32 {
                    return Err(bad(&format!("{name} is not a 32-byte P-256 coordinate")));
                }
            }
            DecodingKey::from_ec_components(&entry.x, &entry.y)
                .map(Some)
                .map_err(|e| bad(&e.to_string()))
        }
        ("OKP", "Ed25519") => {
            if !accept_legacy_eddsa {
                warn!(kid = %entry.kid, "service_auth.jwks_legacy_eddsa_key_skipped");
                return Ok(None);
            }
            if entry.x.is_empty() {
                return Err(bad("empty key"));
            }
            DecodingKey::from_ed_components(&entry.x)
                .map(Some)
                .map_err(|e| bad(&e.to_string()))
        }
        _ => Err(bad(
            "unsupported key type (only EC/P-256, or legacy OKP/Ed25519)",
        )),
    }
}

#[async_trait::async_trait]
impl TrustBundle for JwksTrustBundle {
    fn accept_legacy_eddsa(&self) -> bool {
        self.accept_legacy_eddsa
    }

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

/// Fraction (percent) of a token's lifetime after which [`MachineJwtClient`] proactively
/// refreshes it -- or `exp - CLOCK_SKEW_SECONDS`, whichever is earlier. The old token stays
/// usable (until `exp - skew`) while the refresh runs, so a refresh never stalls callers.
pub const TOKEN_REFRESH_PERCENT: u32 = 80;

/// Base of the jittered window after a failed refresh during which callers get the same
/// failure replayed (or the still-valid old token) instead of each re-hitting hub-api.
pub const TOKEN_REFRESH_FAILURE_BACKOFF: Duration = Duration::from_millis(500);

/// Counter `waddles_hub_service_jwt{result,scope}` (exposed as `..._total`): every
/// [`MachineJwtClient::get_token`] outcome. `reuse` = served from the cache (the steady
/// state: ~every call), `refresh` = a mint round trip to hub-api succeeded, `refresh_failed`
/// = the mint failed (callers then got the old still-valid token, or the shared error).
const METRIC_SERVICE_JWT: &str = "waddles_hub_service_jwt";

/// Records one `get_token` outcome on the process-global meter (a no-op until the host
/// installs a provider; telemetry never affects a token decision). `scope` is a closed,
/// configured vocabulary (a handful of hub-api scopes), never caller-supplied data.
fn record_token_result(scope: &str, result: &'static str) {
    static COUNTER: std::sync::OnceLock<opentelemetry::metrics::Counter<u64>> =
        std::sync::OnceLock::new();
    let counter = COUNTER.get_or_init(|| {
        opentelemetry::global::meter("waddles.service_auth.token")
            .u64_counter(METRIC_SERVICE_JWT)
            .with_description(
                "Machine-JWT acquisitions by result: reuse (cache hit), refresh (hub-api mint), \
                 refresh_failed (mint failed; old valid token or shared error returned).",
            )
            .build()
    });
    counter.add(
        1,
        &[
            opentelemetry::KeyValue::new("result", result),
            opentelemetry::KeyValue::new("scope", scope.to_owned()),
        ],
    );
}

/// `base` scaled by a pseudo-random factor in `[0.75, 1.25)`: de-synchronises retry/backoff
/// windows so a fleet of waiters that failed together do not all retry together. Not
/// cryptographic -- only spreads load.
#[must_use]
pub fn jittered(base: Duration) -> Duration {
    use std::sync::atomic::{AtomicU64, Ordering};
    static SEQ: AtomicU64 = AtomicU64::new(0x9E37_79B9_7F4A_7C15);
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| u64::from(d.subsec_nanos()));
    // splitmix64 over (clock nanos ^ a per-call sequence): cheap, well mixed.
    let mut z = nanos ^ SEQ.fetch_add(0x9E37_79B9_7F4A_7C15, Ordering::Relaxed);
    z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
    z ^= z >> 31;
    #[allow(clippy::cast_precision_loss)]
    let fraction = (z >> 11) as f64 / (1u64 << 53) as f64;
    base.mul_f64(0.75 + 0.5 * fraction)
}

/// A refresh failure replayable to every caller that queued behind it (the error type itself
/// is not `Clone`: `io::Error`).
struct SharedFailure {
    until: tokio::time::Instant,
    error: ServiceAuthError,
}

/// Rebuilds an equal [`ServiceAuthError`] from `err` (all variants but `io::Error` clone as-is).
fn replicate(err: &ServiceAuthError) -> ServiceAuthError {
    match err {
        ServiceAuthError::UnknownKeyId(k) => ServiceAuthError::UnknownKeyId(k.clone()),
        ServiceAuthError::InvalidToken(m) => ServiceAuthError::InvalidToken(m.clone()),
        ServiceAuthError::BootstrapRejected(m) => ServiceAuthError::BootstrapRejected(m.clone()),
        ServiceAuthError::Grpc(status) => ServiceAuthError::Grpc(status.clone()),
        ServiceAuthError::Config(m) => ServiceAuthError::Config(m.clone()),
        ServiceAuthError::Io(e) => {
            ServiceAuthError::Io(std::io::Error::new(e.kind(), e.to_string()))
        }
    }
}

/// Obtains, caches and refreshes a machine JWT for one calling service
/// (`svc_process`, `svc_action`, ...). Bootstraps against hub-api's
/// `ServiceAuthService.MintServiceToken` (gRPC, TLS 1.3) using the pod's own
/// projected Kubernetes ServiceAccount token -- no shared secret is ever
/// distributed to callers.
///
/// A client is **single-scope** (hub-api mints a token per requested scope, so a cached token
/// is valid exactly for that scope and is never reused for another one); the token is a
/// process-wide asset shared by every caller of the same client:
///
/// - **Reuse:** a cached token is returned until [`TOKEN_REFRESH_PERCENT`]% of its lifetime
///   (or `exp - CLOCK_SKEW_SECONDS`, whichever is earlier) has elapsed.
/// - **Single-flight refresh:** concurrent callers that miss queue behind one refresh; the
///   leader mints, the rest re-check the cache it just filled -- N callers cost one mint.
/// - **Shared failure:** a failed refresh is recorded for a short jittered window
///   ([`TOKEN_REFRESH_FAILURE_BACKOFF`]); callers inside it get the same error replayed (never
///   a fresh mint), or -- if the previous token is still valid -- that token, so a hub-api
///   blip at refresh time does not fail traffic that already holds a good credential.
pub struct MachineJwtClient {
    hub: HubChannel,
    sa_token_path: String,
    scope: String,
    cached: RwLock<Option<CachedToken>>,
    /// Single-flight gate: held across a refresh (an `.await`), hence a tokio mutex.
    flight: Mutex<()>,
    /// The last refresh failure, replayed inside its window. Only touched in short sync
    /// sections (never across an `.await`).
    failure: std::sync::Mutex<Option<SharedFailure>>,
}

#[derive(Clone)]
struct CachedToken {
    token: Arc<str>,
    /// Past this instant the token is refreshed on the next call (but still served while
    /// `usable_until` holds and a refresh is failing).
    refresh_at: tokio::time::Instant,
    /// `exp - CLOCK_SKEW_SECONDS`: the last instant the token may be handed out.
    usable_until: tokio::time::Instant,
}

impl MachineJwtClient {
    /// Client for hub-api's gRPC endpoint (`https://host:port`; CA from `HUB_API_GRPC_CA_FILE`),
    /// timeouts from the environment (or defaults). Connects lazily; a bad endpoint/CA is
    /// reported by [`MachineJwtClient::get_token`] (loud, never silently degraded).
    pub fn new(
        token_endpoint: impl Into<String>,
        sa_token_path: impl Into<String>,
        scope: impl Into<String>,
    ) -> Self {
        Self::with_channel(
            HubChannel::from_env(token_endpoint, HttpTimeouts::from_env()),
            sa_token_path,
            scope,
        )
    }

    /// Client with explicit [`HttpTimeouts`]; validates the endpoint and CA up front.
    pub fn with_timeouts(
        token_endpoint: impl Into<String>,
        sa_token_path: impl Into<String>,
        scope: impl Into<String>,
        timeouts: HttpTimeouts,
    ) -> Result<Self, ServiceAuthError> {
        Ok(Self::with_channel(
            HubChannel::validated_from_env(token_endpoint, timeouts)?,
            sa_token_path,
            scope,
        ))
    }

    /// Client over an existing (possibly shared) [`HubChannel`].
    pub fn with_channel(
        hub: HubChannel,
        sa_token_path: impl Into<String>,
        scope: impl Into<String>,
    ) -> Self {
        Self {
            hub,
            sa_token_path: sa_token_path.into(),
            scope: scope.into(),
            cached: RwLock::new(None),
            flight: Mutex::new(()),
            failure: std::sync::Mutex::new(None),
        }
    }

    /// The scope this client's tokens are minted for.
    #[must_use]
    pub fn scope(&self) -> &str {
        &self.scope
    }

    /// Cached token still before its proactive-refresh point, if any.
    async fn fresh(&self) -> Option<Arc<str>> {
        let now = tokio::time::Instant::now();
        let guard = self.cached.read().await;
        guard
            .as_ref()
            .filter(|c| now < c.refresh_at)
            .map(|c| c.token.clone())
    }

    /// Cached token that may still be handed out (before `exp - skew`), refresh due or not.
    async fn usable(&self) -> Option<Arc<str>> {
        let now = tokio::time::Instant::now();
        let guard = self.cached.read().await;
        guard
            .as_ref()
            .filter(|c| now < c.usable_until)
            .map(|c| c.token.clone())
    }

    /// Locks the failure slot, recovering a poisoned lock (plain data, nothing torn).
    fn lock_failure(&self) -> std::sync::MutexGuard<'_, Option<SharedFailure>> {
        self.failure
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    /// A still-live shared failure, replayed (`None` once its window has passed).
    fn replayed_failure(&self) -> Option<ServiceAuthError> {
        let guard = self.lock_failure();
        guard
            .as_ref()
            .filter(|f| tokio::time::Instant::now() < f.until)
            .map(|f| replicate(&f.error))
    }

    /// Return a currently-valid machine JWT: the cached one until [`TOKEN_REFRESH_PERCENT`]%
    /// of its lifetime has passed, otherwise one (single-flighted) refresh from hub-api.
    /// See the type docs for the reuse / single-flight / shared-failure contract.
    pub async fn get_token(&self) -> Result<Arc<str>, ServiceAuthError> {
        if let Some(token) = self.fresh().await {
            record_token_result(&self.scope, "reuse");
            return Ok(token);
        }
        // Single-flight: concurrent misses queue here; only the first actually mints.
        let _flight = self.flight.lock().await;
        if let Some(token) = self.fresh().await {
            record_token_result(&self.scope, "reuse");
            return Ok(token);
        }
        if let Some(err) = self.replayed_failure() {
            // A refresh just failed for every caller queued with us: do not re-hit hub-api.
            if let Some(token) = self.usable().await {
                record_token_result(&self.scope, "reuse");
                return Ok(token);
            }
            return Err(err);
        }
        match self.refresh().await {
            Ok(token) => {
                *self.lock_failure() = None;
                record_token_result(&self.scope, "refresh");
                Ok(token)
            }
            Err(err) => {
                record_token_result(&self.scope, "refresh_failed");
                warn!(scope = %self.scope, error = %err, "service_auth.token_refresh_failed");
                *self.lock_failure() = Some(SharedFailure {
                    until: tokio::time::Instant::now() + jittered(TOKEN_REFRESH_FAILURE_BACKOFF),
                    error: replicate(&err),
                });
                // The previous token is still inside its validity: keep serving it.
                match self.usable().await {
                    Some(token) => Ok(token),
                    None => Err(err),
                }
            }
        }
    }

    /// One mint round trip; stores the token with its refresh point and last-usable instant.
    async fn refresh(&self) -> Result<Arc<str>, ServiceAuthError> {
        let issued = tokio::time::Instant::now();
        let sa_token = tokio::fs::read_to_string(&self.sa_token_path).await?;
        let reply = self
            .hub
            .mint(sa_token.trim(), &self.scope, None, None)
            .await?;
        let ttl = Duration::from_secs(reply.expires_in.min(MAX_TOKEN_TTL_SECONDS));
        let skew = Duration::from_secs(CLOCK_SKEW_SECONDS);
        let usable_for = ttl.saturating_sub(skew);
        let refresh_after = (ttl * TOKEN_REFRESH_PERCENT / 100).min(usable_for);
        let token: Arc<str> = Arc::from(reply.token.as_str());
        *self.cached.write().await = Some(CachedToken {
            token: token.clone(),
            refresh_at: issued + refresh_after,
            usable_until: issued + usable_for,
        });
        debug!(scope = %self.scope, ttl_secs = ttl.as_secs(), "service_auth.token_refreshed");
        Ok(token)
    }
}

/// Max distinct `(community, connection)` bearers held by [`BoundMachineJwtClient`].
pub const BOUND_TOKEN_CACHE_CAP: usize = 256;

type BindingKey = (i64, Option<i64>);

/// A cached bound bearer. Expiry is on the monotonic tokio clock so tests can
/// advance time without touching wall-clock state. `Debug` never prints the token.
#[derive(Clone)]
struct BoundCachedToken {
    token: Arc<str>,
    expires_at: tokio::time::Instant,
    last_used: u64,
}

impl std::fmt::Debug for BoundCachedToken {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("BoundCachedToken")
            .field("token", &"<redacted>")
            .finish_non_exhaustive()
    }
}

/// Bounded LRU of bound bearers (bearer JWTs only -- never connection secrets).
#[derive(Default)]
struct BoundCache {
    map: HashMap<BindingKey, BoundCachedToken>,
    tick: u64,
}

impl BoundCache {
    /// Fresh (outside the skew window) token for `key`, marking it recently used.
    fn get_fresh(&mut self, key: &BindingKey, now: tokio::time::Instant) -> Option<Arc<str>> {
        self.tick += 1;
        let tick = self.tick;
        let entry = self.map.get_mut(key)?;
        if entry.expires_at > now + Duration::from_secs(CLOCK_SKEW_SECONDS) {
            entry.last_used = tick;
            Some(entry.token.clone())
        } else {
            None
        }
    }

    /// Insert `key`, evicting the least-recently-used binding beyond the cap.
    fn insert(&mut self, key: BindingKey, token: Arc<str>, expires_at: tokio::time::Instant) {
        self.tick += 1;
        if !self.map.contains_key(&key) && self.map.len() >= BOUND_TOKEN_CACHE_CAP {
            let oldest = self
                .map
                .iter()
                .min_by_key(|(_, v)| v.last_used)
                .map(|(k, _)| *k);
            if let Some(k) = oldest {
                self.map.remove(&k);
            }
        }
        self.map.insert(
            key,
            BoundCachedToken {
                token,
                expires_at,
                last_used: self.tick,
            },
        );
    }
}

/// Mints and caches community-bound machine JWTs (bearer cache only; never
/// connection secrets). Each token is bound by hub-api to exactly one
/// `(community_id, optional connection_id)`; the cache is an LRU of
/// [`BOUND_TOKEN_CACHE_CAP`] bindings with per-binding single-flight minting.
pub struct BoundMachineJwtClient {
    hub: HubChannel,
    sa_token_path: String,
    scope: String,
    cache: Mutex<BoundCache>,
    gates: std::sync::Mutex<HashMap<BindingKey, Arc<Mutex<()>>>>,
}

impl std::fmt::Debug for BoundMachineJwtClient {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("BoundMachineJwtClient")
            .field("endpoint", &self.hub.endpoint())
            .field("scope", &self.scope)
            .finish_non_exhaustive()
    }
}

impl BoundMachineJwtClient {
    /// Client with explicit [`HttpTimeouts`]; validates the endpoint and CA up front.
    pub fn with_timeouts(
        token_endpoint: impl Into<String>,
        sa_token_path: impl Into<String>,
        scope: impl Into<String>,
        timeouts: HttpTimeouts,
    ) -> Result<Self, ServiceAuthError> {
        Ok(Self::with_channel(
            HubChannel::validated_from_env(token_endpoint, timeouts)?,
            sa_token_path,
            scope,
        ))
    }

    /// Client over an existing (possibly shared) [`HubChannel`].
    pub fn with_channel(
        hub: HubChannel,
        sa_token_path: impl Into<String>,
        scope: impl Into<String>,
    ) -> Self {
        Self {
            hub,
            sa_token_path: sa_token_path.into(),
            scope: scope.into(),
            cache: Mutex::new(BoundCache::default()),
            gates: std::sync::Mutex::new(HashMap::new()),
        }
    }

    /// Bearer bound to exactly (community_id, optional connection_id); refreshed
    /// within `CLOCK_SKEW_SECONDS` of exp. Concurrent callers for the same
    /// binding share one mint.
    pub async fn get_token_for(
        &self,
        community_id: i64,
        connection_id: Option<i64>,
    ) -> Result<Arc<str>, ServiceAuthError> {
        let key = (community_id, connection_id);
        if let Some(t) = self
            .cache
            .lock()
            .await
            .get_fresh(&key, tokio::time::Instant::now())
        {
            return Ok(t);
        }
        let gate = {
            let mut gates = self.lock_gates();
            gates.entry(key).or_default().clone()
        };
        let result = {
            let _flight = gate.lock().await;
            // Another caller may have minted while we waited on the gate.
            let cached = self
                .cache
                .lock()
                .await
                .get_fresh(&key, tokio::time::Instant::now());
            match cached {
                Some(t) => Ok(t),
                None => self.mint(key).await,
            }
        };
        let mut gates = self.lock_gates();
        // Only the map and our local `gate` remain -> no waiters; drop the entry.
        if Arc::strong_count(&gate) <= 2 {
            gates.remove(&key);
        }
        result
    }

    /// Lock the single-flight gate map; a poisoned lock (a panicked holder) is recovered because
    /// the map only holds per-binding `Arc` gates, but the recovery is logged, never silent.
    fn lock_gates(&self) -> std::sync::MutexGuard<'_, HashMap<BindingKey, Arc<Mutex<()>>>> {
        match self.gates.lock() {
            Ok(guard) => guard,
            Err(poisoned) => {
                warn!("service_auth.bound_gates_poisoned_recovered");
                poisoned.into_inner()
            }
        }
    }

    async fn mint(&self, key: BindingKey) -> Result<Arc<str>, ServiceAuthError> {
        let (community_id, connection_id) = key;
        let sa_token = tokio::fs::read_to_string(&self.sa_token_path).await?;
        let reply = match self
            .hub
            .mint(
                sa_token.trim(),
                &self.scope,
                Some(community_id),
                connection_id,
            )
            .await
        {
            Ok(reply) => reply,
            Err(err) => {
                warn!(community_id, error = %err, "service_auth.bound_mint_rejected");
                return Err(err);
            }
        };
        if reply.token.trim().is_empty() {
            warn!(community_id, "service_auth.bound_mint_empty_token");
            return Err(ServiceAuthError::BootstrapRejected(
                "token endpoint returned an empty token".into(),
            ));
        }
        let ttl = reply.expires_in.min(MAX_TOKEN_TTL_SECONDS);
        let token: Arc<str> = Arc::from(reply.token.as_str());
        self.cache.lock().await.insert(
            key,
            token.clone(),
            tokio::time::Instant::now() + Duration::from_secs(ttl),
        );
        debug!(community_id, "service_auth.bound_token_minted");
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
    pub(super) const KEY_A_PRIV_DER: &[u8] = &[
        48, 46, 2, 1, 0, 48, 5, 6, 3, 43, 101, 112, 4, 34, 4, 32, 1, 204, 5, 142, 35, 153, 231, 38,
        150, 122, 1, 218, 34, 237, 70, 125, 233, 62, 126, 103, 151, 16, 11, 238, 95, 122, 209, 74,
        183, 9, 171, 161,
    ];
    // Raw 32-byte Ed25519 public key (the last 32 bytes of the SPKI-DER
    // `openssl pkey -pubout` produced) -- `DecodingKey::from_ed_der` in
    // jsonwebtoken's rust_crypto backend reads only the raw key bytes, not
    // the full ASN.1 SPKI wrapper.
    pub(super) const KEY_A_PUB_RAW: &[u8] = &[
        169, 90, 255, 23, 51, 151, 156, 147, 56, 247, 214, 168, 76, 160, 67, 99, 211, 238, 208, 5,
        69, 236, 245, 115, 4, 81, 1, 42, 23, 107, 4, 187,
    ];

    // PKCS8-DER ECDSA P-256 test keypairs (fixed, test-only; never used outside this test
    // module), generated once with `cryptography` (`ec.generate_private_key(SECP256R1)`).
    // `x`/`y` are the base64url public coordinates of key A (the JWKS `EC` form).
    pub(super) const ES_A_PRIV_DER: &[u8] = &[
        48, 129, 135, 2, 1, 0, 48, 19, 6, 7, 42, 134, 72, 206, 61, 2, 1, 6, 8, 42, 134, 72, 206,
        61, 3, 1, 7, 4, 109, 48, 107, 2, 1, 1, 4, 32, 209, 234, 92, 37, 28, 130, 216, 139, 48, 1,
        225, 220, 88, 158, 30, 88, 20, 126, 43, 7, 182, 151, 62, 129, 248, 115, 187, 119, 18, 12,
        142, 93, 161, 68, 3, 66, 0, 4, 17, 138, 186, 198, 3, 136, 154, 139, 190, 114, 14, 103, 123,
        44, 247, 26, 31, 171, 203, 86, 236, 40, 175, 97, 236, 72, 32, 22, 179, 142, 109, 144, 173,
        115, 191, 94, 95, 104, 151, 149, 88, 212, 250, 138, 99, 63, 147, 180, 150, 85, 29, 12, 130,
        136, 7, 86, 162, 99, 191, 4, 123, 168, 238, 207,
    ];
    pub(super) const ES_A_X: &str = "EYq6xgOImou-cg5neyz3Gh-ry1bsKK9h7EggFrOObZA";
    pub(super) const ES_A_Y: &str = "rXO_Xl9ol5VY1PqKYz-TtJZVHQyCiAdWomO_BHuo7s8";
    pub(super) const ES_B_X: &str = "YtHRfUTX7D18UCEeYunBn5Dh2RzjqHtJkygZTTHf4NQ";
    pub(super) const ES_B_Y: &str = "sM_n5KoIvy1BSAhobF_sySiib-uC67WEy_WjZhtVWrA";
    pub(super) const ES_B_PRIV_DER: &[u8] = &[
        48, 129, 135, 2, 1, 0, 48, 19, 6, 7, 42, 134, 72, 206, 61, 2, 1, 6, 8, 42, 134, 72, 206,
        61, 3, 1, 7, 4, 109, 48, 107, 2, 1, 1, 4, 32, 20, 206, 40, 170, 137, 187, 67, 100, 72, 148,
        97, 198, 42, 118, 251, 253, 181, 175, 151, 157, 190, 138, 70, 223, 254, 35, 143, 55, 95,
        203, 51, 94, 161, 68, 3, 66, 0, 4, 98, 209, 209, 125, 68, 215, 236, 61, 124, 80, 33, 30,
        98, 233, 193, 159, 144, 225, 217, 28, 227, 168, 123, 73, 147, 40, 25, 77, 49, 223, 224,
        212, 176, 207, 231, 228, 170, 8, 191, 45, 65, 72, 8, 104, 108, 95, 236, 201, 40, 162, 111,
        235, 130, 235, 181, 132, 203, 245, 163, 102, 27, 85, 90, 176,
    ];

    /// ES256 keypair A: the signing key hub-api would hold and the verification key a trust
    /// bundle serves for it.
    pub(super) fn es256_keypair() -> (EncodingKey, DecodingKey) {
        (
            EncodingKey::from_ec_der(ES_A_PRIV_DER),
            DecodingKey::from_ec_components(ES_A_X, ES_A_Y).expect("test P-256 key"),
        )
    }

    /// Legacy Ed25519 keypair (only for the legacy-window tests).
    pub(super) fn ed25519_keypair() -> (EncodingKey, DecodingKey) {
        (
            EncodingKey::from_ed_der(KEY_A_PRIV_DER),
            DecodingKey::from_ed_der(KEY_A_PUB_RAW),
        )
    }

    pub(super) fn make_token(
        encoding_key: &EncodingKey,
        kid: &str,
        claims: &ServiceClaims,
    ) -> String {
        make_token_alg(Algorithm::ES256, encoding_key, kid, claims)
    }

    pub(super) fn make_token_alg(
        alg: Algorithm,
        encoding_key: &EncodingKey,
        kid: &str,
        claims: &ServiceClaims,
    ) -> String {
        let mut header = Header::new(alg);
        header.kid = Some(kid.to_string());
        jsonwebtoken::encode(&header, claims, encoding_key).unwrap()
    }

    pub(super) fn base_claims(now: u64) -> ServiceClaims {
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
        let (enc, dec) = es256_keypair();
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
        let (enc, _dec) = es256_keypair();
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
        let (enc, dec) = es256_keypair();
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
        let (enc, dec) = es256_keypair();
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
        let (enc, dec) = es256_keypair();
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
        let (enc, dec) = es256_keypair();
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
        let (enc, dec) = es256_keypair();
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
        let (_enc, dec) = es256_keypair();
        // A second, different test keypair to sign with -- simulates an
        // attacker who doesn't hold hub-api's private key but guesses/
        // reuses a known `kid`. Verification stays pinned to key A's
        // public key, so a token signed by key B must be rejected.
        let forged_enc = EncodingKey::from_ec_der(ES_B_PRIV_DER);
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

    use crate::mock_hub::{JwksReply, MintReply, MockHub};
    use std::sync::atomic::{AtomicUsize, Ordering};
    use tokio::net::TcpListener;

    /// Hit counter + keep-alive for a [`MockHub`] (call sites read `hits.load(..)`).
    struct Hits(Arc<MockHub>);

    impl Hits {
        /// Total calls the mock hub served (each test drives only one of mint/JWKS).
        fn load(&self, _order: Ordering) -> usize {
            self.0.mint_hits() + self.0.jwks_hits()
        }
    }

    /// Translate one canned `(status, body)` into a JWKS reply: a `{"keys":[..]}` body serves
    /// those keys, anything else is a failed fetch.
    fn jwks_reply(status: u16, body: &str) -> JwksReply {
        let parsed: Option<serde_json::Value> = serde_json::from_str(body).ok();
        match parsed
            .as_ref()
            .and_then(|v| v.get("keys")?.as_array().cloned())
        {
            Some(keys) if status == 200 => JwksReply::Keys(
                keys.iter()
                    .map(|k| crate::grpc::pb::Jwk {
                        kid: k["kid"].as_str().unwrap_or_default().to_owned(),
                        kty: k["kty"].as_str().unwrap_or("EC").to_owned(),
                        crv: k["crv"].as_str().unwrap_or("P-256").to_owned(),
                        alg: "ES256".into(),
                        r#use: "sig".into(),
                        x: k["x"].as_str().unwrap_or_default().to_owned(),
                        y: k["y"].as_str().unwrap_or_default().to_owned(),
                    })
                    .collect(),
            ),
            _ => JwksReply::Reject(tonic::Code::Unavailable),
        }
    }

    /// Translate one canned `(status, body)` into a mint reply: a `{"token","expires_in"}` body
    /// is a success, a non-200 status is a refusal, anything else is a server fault.
    fn mint_reply(status: u16, body: &str) -> MintReply {
        let parsed: Option<serde_json::Value> = serde_json::from_str(body).ok();
        if status != 200 {
            return MintReply::Reject(tonic::Code::Unauthenticated);
        }
        match parsed {
            Some(v) if v.get("token").is_some() => MintReply::Token {
                token: v["token"].as_str().unwrap_or_default().to_owned(),
                expires_in: v["expires_in"].as_u64().unwrap_or(0),
            },
            _ => MintReply::Reject(tonic::Code::Internal),
        }
    }

    /// Real gRPC mock hub serving the canned replies in order (last repeats), as BOTH the
    /// mint and the JWKS answers; returns its endpoint and a hit counter. The hub is leaked on
    /// purpose: it must outlive call sites that discard the counter, and ends with the test's
    /// runtime.
    async fn mock_server(responses: Vec<(u16, String)>) -> (String, Hits) {
        mock_server_delayed(responses, Duration::ZERO).await
    }

    /// As [`mock_server`] but waits `delay` before answering, so concurrent callers genuinely
    /// overlap an in-flight request.
    async fn mock_server_delayed(responses: Vec<(u16, String)>, delay: Duration) -> (String, Hits) {
        let hub = MockHub::builder()
            .mint(responses.iter().map(|(s, b)| mint_reply(*s, b)).collect())
            .jwks(responses.iter().map(|(s, b)| jwks_reply(*s, b)).collect())
            .delay(delay)
            .start()
            .await;
        let endpoint = hub.endpoint.clone();
        let hub = Arc::new(hub);
        std::mem::forget(hub.clone());
        (endpoint, Hits(hub))
    }

    pub(super) fn b64url(data: &[u8]) -> String {
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
        serde_json::json!({"keys":[{"kid": kid, "kty":"EC", "crv":"P-256", "x": ES_A_X, "y": ES_A_Y}]})
            .to_string()
    }

    pub(super) fn temp_sa_token(name: &str, contents: &str) -> String {
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
        let (enc, dec) = es256_keypair();
        let bundle = StaticTrustBundle(Mutex::new(HashMap::from([("k1".to_string(), dec)])));
        let header = Header::new(Algorithm::ES256);
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
        let (enc, dec) = es256_keypair();
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
        let (_enc, dec) = es256_keypair();
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
        let (enc, _) = es256_keypair();
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
        let bundle = JwksTrustBundle::new("http://127.0.0.1:1");
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
            accept_legacy_eddsa: false,
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
            matches!(&err, ServiceAuthError::Grpc(st)
                if matches!(st.code(), tonic::Code::DeadlineExceeded | tonic::Code::Cancelled)),
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
            accept_legacy_eddsa: false,
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
        let before = tokio::time::Instant::now();
        client.get_token().await.expect("mint");
        let usable_until = client
            .cached
            .read()
            .await
            .as_ref()
            .expect("cached")
            .usable_until;
        assert!(
            usable_until
                <= before + Duration::from_secs(MAX_TOKEN_TTL_SECONDS) + Duration::from_secs(5)
        );
        let _ = std::fs::remove_file(sa);
    }

    #[tokio::test]
    async fn machine_client_bootstrap_rejected_on_non_success() {
        let (url, _) = mock_server(vec![(403, "{}".into())]).await;
        let sa = temp_sa_token("reject", "sa");
        let client = MachineJwtClient::new(url, sa.clone(), "s");
        let err = client.get_token().await.unwrap_err();
        assert!(
            matches!(err, ServiceAuthError::BootstrapRejected(m) if m.contains("Unauthenticated"))
        );
        assert!(
            client.cached.read().await.is_none(),
            "nothing cached on rejection"
        );
        let _ = std::fs::remove_file(sa);
    }

    #[tokio::test]
    async fn machine_client_missing_sa_token_is_io_error() {
        let client = MachineJwtClient::new("http://127.0.0.1:1", "/nonexistent/sa/token", "s");
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
            ServiceAuthError::Grpc(_)
        ));
        let _ = std::fs::remove_file(sa);
    }

    #[tokio::test]
    async fn machine_client_unreachable_endpoint_is_http_error() {
        let sa = temp_sa_token("unreach", "sa");
        let client = MachineJwtClient::new("http://127.0.0.1:1", sa.clone(), "s");
        assert!(matches!(
            client.get_token().await.unwrap_err(),
            ServiceAuthError::Grpc(_)
        ));
        let _ = std::fs::remove_file(sa);
    }

    // ---- BoundMachineJwtClient -------------------------------------------

    /// The mint requests a [`mock_server`]-style hub received, rendered as the JSON object the
    /// old REST body carried (`{"scope", "community_id"?, "connection_id"?}`).
    struct Bodies(Arc<MockHub>);

    impl Bodies {
        fn lock(&self) -> Result<Vec<String>, std::convert::Infallible> {
            Ok(self
                .0
                .mint_calls()
                .iter()
                .map(|c| {
                    let mut o = serde_json::json!({ "scope": c.scope });
                    if let Some(id) = c.community_id {
                        o["community_id"] = id.into();
                    }
                    if let Some(id) = c.connection_id {
                        o["connection_id"] = id.into();
                    }
                    o.to_string()
                })
                .collect())
        }
    }

    /// Hub that records each mint request and answers `(status, body)` by hit index.
    async fn capture_server(responses: Vec<(u16, String)>) -> (String, Bodies) {
        let (endpoint, hits) = mock_server(responses).await;
        (endpoint, Bodies(hits.0))
    }

    fn tok_body(t: &str, ttl: u64) -> (u16, String) {
        (
            200,
            serde_json::json!({"token": t, "expires_in": ttl}).to_string(),
        )
    }

    fn bound_client(url: String, sa: &str) -> BoundMachineJwtClient {
        BoundMachineJwtClient::with_timeouts(
            url,
            sa,
            "connections:resolve",
            HttpTimeouts::default(),
        )
        .expect("client")
    }

    #[tokio::test]
    async fn token_request_body_carries_community_and_connection() {
        let (url, bodies) = capture_server(vec![tok_body("t1", 300)]).await;
        let sa = temp_sa_token("bound_body", "sa");
        let c = bound_client(url, &sa);
        assert_eq!(&*c.get_token_for(7, Some(12)).await.expect("mint"), "t1");
        c.get_token_for(8, None).await.expect("mint2");
        let got: Vec<serde_json::Value> = bodies
            .lock()
            .expect("lock")
            .iter()
            .map(|b| serde_json::from_str(b).expect("json"))
            .collect();
        assert_eq!(
            got[0],
            serde_json::json!({"scope":"connections:resolve","community_id":7,"connection_id":12})
        );
        assert_eq!(
            got[1],
            serde_json::json!({"scope":"connections:resolve","community_id":8})
        );
        let _ = std::fs::remove_file(sa);
    }

    #[tokio::test]
    async fn distinct_communities_get_distinct_cached_tokens() {
        let (url, hits) = mock_server(vec![tok_body("a", 300), tok_body("b", 300)]).await;
        let sa = temp_sa_token("bound_distinct", "sa");
        let c = bound_client(url, &sa);
        assert_eq!(&*c.get_token_for(1, None).await.expect("a"), "a");
        assert_eq!(&*c.get_token_for(2, None).await.expect("b"), "b");
        assert_eq!(&*c.get_token_for(1, None).await.expect("a again"), "a");
        assert_eq!(&*c.get_token_for(1, Some(5)).await.expect("conn"), "b");
        assert_eq!(
            hits.load(Ordering::SeqCst),
            3,
            "3 distinct bindings -> 3 mints"
        );
        let _ = std::fs::remove_file(sa);
    }

    #[tokio::test]
    async fn same_binding_hits_cache_until_skew() {
        let (url, hits) = mock_server(vec![tok_body("t1", 100), tok_body("t2", 100)]).await;
        let sa = temp_sa_token("bound_skew", "sa");
        let c = bound_client(url, &sa);
        for _ in 0..5 {
            assert_eq!(&*c.get_token_for(3, None).await.expect("t1"), "t1");
        }
        assert_eq!(hits.load(Ordering::SeqCst), 1);
        // Advance to inside the skew window (100 - 30 = 70s usable).
        tokio::time::pause();
        tokio::time::advance(Duration::from_secs(69)).await;
        tokio::time::resume();
        assert_eq!(&*c.get_token_for(3, None).await.expect("still t1"), "t1");
        assert_eq!(hits.load(Ordering::SeqCst), 1);
        tokio::time::pause();
        tokio::time::advance(Duration::from_secs(2)).await;
        tokio::time::resume();
        assert_eq!(&*c.get_token_for(3, None).await.expect("t2"), "t2");
        assert_eq!(hits.load(Ordering::SeqCst), 2);
        let _ = std::fs::remove_file(sa);
    }

    #[tokio::test]
    async fn bootstrap_rejected_maps_to_error_not_panic() {
        let (url, _) = mock_server(vec![(403, "{}".into())]).await;
        let sa = temp_sa_token("bound_reject", "sa");
        let c = bound_client(url, &sa);
        assert!(matches!(
            c.get_token_for(1, None).await.unwrap_err(),
            ServiceAuthError::BootstrapRejected(m) if m.contains("Unauthenticated")
        ));
        assert!(c.cache.lock().await.map.is_empty(), "nothing cached");
        let _ = std::fs::remove_file(sa);
    }

    #[tokio::test]
    async fn bound_missing_sa_token_malformed_body_and_unreachable_are_errors() {
        let c = bound_client("http://127.0.0.1:1".into(), "/nonexistent/sa/token");
        assert!(matches!(
            c.get_token_for(1, None).await.unwrap_err(),
            ServiceAuthError::Io(_)
        ));
        let sa = temp_sa_token("bound_errs", "sa");
        let c = bound_client("http://127.0.0.1:1".into(), &sa);
        assert!(matches!(
            c.get_token_for(1, None).await.unwrap_err(),
            ServiceAuthError::Grpc(_)
        ));
        let (url, _) = mock_server(vec![(200, "garbage".into())]).await;
        let c = bound_client(url, &sa);
        assert!(matches!(
            c.get_token_for(1, None).await.unwrap_err(),
            ServiceAuthError::Grpc(_)
        ));
        let _ = std::fs::remove_file(sa);
    }

    #[tokio::test]
    async fn bound_clamps_ttl_to_platform_max() {
        let (url, _) = mock_server(vec![tok_body("t", 10 * MAX_TOKEN_TTL_SECONDS)]).await;
        let sa = temp_sa_token("bound_clamp", "sa");
        let c = bound_client(url, &sa);
        c.get_token_for(1, None).await.expect("mint");
        let exp = c.cache.lock().await.map[&(1, None)].expires_at;
        assert!(exp <= tokio::time::Instant::now() + Duration::from_secs(MAX_TOKEN_TTL_SECONDS));
        let _ = std::fs::remove_file(sa);
    }

    #[tokio::test]
    async fn bound_concurrent_callers_share_one_mint() {
        let (url, hits) =
            mock_server_delayed(vec![tok_body("t", 300)], Duration::from_millis(150)).await;
        let sa = temp_sa_token("bound_flight", "sa");
        let c = Arc::new(bound_client(url, &sa));
        let mut tasks = Vec::new();
        for _ in 0..16 {
            let c = c.clone();
            tasks.push(tokio::spawn(async move {
                c.get_token_for(9, None).await.is_ok()
            }));
        }
        for t in tasks {
            assert!(t.await.expect("join"));
        }
        assert_eq!(hits.load(Ordering::SeqCst), 1, "single-flight per binding");
        assert!(c.gates.lock().expect("lock").is_empty(), "gate pruned");
        let _ = std::fs::remove_file(sa);
    }

    #[tokio::test]
    async fn lru_evicts_beyond_capacity() {
        let (url, hits) = mock_server(vec![tok_body("t", 300)]).await;
        let sa = temp_sa_token("bound_lru", "sa");
        let c = bound_client(url, &sa);
        for i in 0..=(BOUND_TOKEN_CACHE_CAP as i64) {
            c.get_token_for(i, None).await.expect("mint");
        }
        assert_eq!(c.cache.lock().await.map.len(), BOUND_TOKEN_CACHE_CAP);
        assert_eq!(hits.load(Ordering::SeqCst), BOUND_TOKEN_CACHE_CAP + 1);
        // Binding 0 (oldest) was evicted -> re-mint; binding 2 still cached.
        c.get_token_for(2, None).await.expect("cached");
        assert_eq!(hits.load(Ordering::SeqCst), BOUND_TOKEN_CACHE_CAP + 1);
        c.get_token_for(0, None).await.expect("re-mint");
        assert_eq!(hits.load(Ordering::SeqCst), BOUND_TOKEN_CACHE_CAP + 2);
        assert_eq!(c.cache.lock().await.map.len(), BOUND_TOKEN_CACHE_CAP);
        let _ = std::fs::remove_file(sa);
    }

    #[tokio::test]
    async fn bound_empty_minted_token_is_rejected_and_not_cached() {
        let (url, hits) = mock_server(vec![tok_body("  ", 300)]).await;
        let sa = temp_sa_token("bound_empty", "sa");
        let c = bound_client(url, &sa);
        for expected in 1..=2 {
            assert!(matches!(
                c.get_token_for(1, None).await.unwrap_err(),
                ServiceAuthError::BootstrapRejected(m) if m.contains("empty token")
            ));
            assert_eq!(
                hits.load(Ordering::SeqCst),
                expected,
                "re-mints, never cached"
            );
        }
        assert!(c.cache.lock().await.map.is_empty());
        let _ = std::fs::remove_file(sa);
    }

    #[tokio::test]
    async fn debug_never_prints_token() {
        let (url, _) = mock_server(vec![tok_body("SECRET-BEARER-XYZ", 300)]).await;
        let sa = temp_sa_token("bound_debug", "sa");
        let c = bound_client(url, &sa);
        c.get_token_for(1, None).await.expect("mint");
        let client_dbg = format!("{c:?}");
        let entry_dbg = format!("{:?}", c.cache.lock().await.map[&(1, None)]);
        assert!(!client_dbg.contains("SECRET-BEARER-XYZ"));
        assert!(!entry_dbg.contains("SECRET-BEARER-XYZ"));
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
            &EncodingKey::from_ec_der(ES_A_PRIV_DER),
            Algorithm::ES256,
        )
        .expect("sign");
        format!("{message}.{signature}")
    }

    fn good_header() -> Value {
        json!({"alg": "ES256", "typ": "JWT", "kid": "k1"})
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
        let (_enc, dec) = es256_keypair();
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
        assert_eq!(capture.count("service_eddsa", "es256", "ok"), 1);
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
            (json!("ES384"), "es384"),
            (json!("EdDSA"), "eddsa"),
            (json!("HS256"), "hs256"),
            (json!("PS512"), "ps512"),
            (json!("es256"), "es256"),
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
            assert_refused(&token, REASON_FORBIDDEN_HEADER, "es256", true).await;
        }
    }

    #[tokio::test]
    async fn crit_header_is_rejected_whatever_it_names() {
        let mut header = good_header();
        header["crit"] = json!(["exp"]);
        assert_refused(
            &raw_token(&header, &good_claims()),
            REASON_FORBIDDEN_HEADER,
            "es256",
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
                "es256",
                true,
            )
            .await;
        }
    }

    #[tokio::test]
    async fn absent_and_unknown_kid_are_unknown_kid_and_do_not_leak_a_value() {
        let bundle = counting_bundle();
        let header = json!({"alg": "ES256"});
        let (result, capture) = run(&raw_token(&header, &good_claims()), &bundle).await;
        assert!(matches!(result, Err(ServiceAuthError::UnknownKeyId(None))));
        assert_eq!(capture.count("service_eddsa", "es256", "unknown_kid"), 1);
        assert_eq!(
            bundle.lookups.load(Ordering::SeqCst),
            0,
            "no kid, no lookup"
        );

        let header = json!({"alg": "ES256", "kid": "rotated-away"});
        let (result, capture) = run(&raw_token(&header, &good_claims()), &bundle).await;
        assert!(
            matches!(&result, Err(ServiceAuthError::UnknownKeyId(Some(k))) if k == "rotated-away")
        );
        assert_eq!(capture.count("service_eddsa", "es256", "unknown_kid"), 1);
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
            assert_refused(&token, REASON_MISSING_CLAIM, "es256", false).await;
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
            assert_eq!(capture.count("service_eddsa", "es256", "ok"), 0, "{name}");
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
                capture.count("service_eddsa", "es256", "ok"),
                0,
                "{name}={value}"
            );
        }
        assert_refused(
            &raw_token(&good_header(), &claims_with("sub", json!(""))),
            REASON_INVALID_CLAIM,
            "es256",
            false,
        )
        .await;
        assert_refused(
            &raw_token(&good_header(), &claims_with("scope", json!(1))),
            REASON_INVALID_CLAIM,
            "es256",
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
            assert_refused(&token, REASON_INVALID_CLAIM, "es256", false).await;
        }
    }

    #[tokio::test]
    async fn issuer_and_audience_are_enforced_not_optional() {
        assert_refused(
            &raw_token(&good_header(), &claims_without("iss")),
            REASON_MISSING_CLAIM,
            "es256",
            false,
        )
        .await;
        assert_refused(
            &raw_token(&good_header(), &claims_without("aud")),
            REASON_MISSING_CLAIM,
            "es256",
            false,
        )
        .await;
        assert_refused(
            &raw_token(&good_header(), &claims_with("iss", json!("evil"))),
            REASON_BAD_ISSUER,
            "es256",
            false,
        )
        .await;
        assert_refused(
            &raw_token(&good_header(), &claims_with("aud", json!("other"))),
            REASON_BAD_AUDIENCE,
            "es256",
            false,
        )
        .await;
        assert_refused(
            &raw_token(&good_header(), &claims_with("aud", json!(["a", "b"]))),
            REASON_BAD_AUDIENCE,
            "es256",
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
            "es256",
            false,
        )
        .await;
        assert_refused(
            &raw_token(
                &good_header(),
                &claims_with("nbf", json!(now + CLOCK_SKEW_SECONDS + 300)),
            ),
            REASON_IMMATURE,
            "es256",
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
            "es256",
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
        let forged = EncodingKey::from_ec_der(ES_B_PRIV_DER);
        let message = format!("{}.{}", b64_json(&good_header()), b64_json(&good_claims()));
        let sig = jsonwebtoken::crypto::sign(message.as_bytes(), &forged, Algorithm::ES256)
            .expect("sign");
        assert_refused(
            &format!("{message}.{sig}"),
            REASON_BAD_SIGNATURE,
            "es256",
            false,
        )
        .await;

        assert_refused(
            &raw_token(
                &good_header(),
                &claims_with("scope", json!("some:other:scope")),
            ),
            REASON_SCOPE_DENIED,
            "es256",
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
        assert!(matches!(err, ServiceAuthError::InvalidToken(m) if m.contains("32-byte")));
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
        assert_eq!(SERVICE_JWT_ALGORITHM, "ES256");
    }
}

/// Real-path tests for the gRPC bootstrap: a real tonic server (`MockHub`) that signs REAL EdDSA
/// JWTs and serves the matching JWKS, real clients over real HTTP/2, and -- for the TLS tests --
/// real TLS 1.3 handshakes against a throwaway ECDSA P-256 CA generated per run.
#[cfg(test)]
mod grpc_path_tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::tests::{
        b64url, base_claims, ed25519_keypair, es256_keypair, make_token, make_token_alg,
        temp_sa_token, ES_A_X, ES_A_Y, ES_B_PRIV_DER, ES_B_X, ES_B_Y, KEY_A_PUB_RAW,
    };
    use super::*;
    use crate::mock_hub::{es256_jwk, JwksReply, MintReply, MockHub};
    use jsonwebtoken::EncodingKey;
    use rustls_pki_types::pem::PemObject as _;
    use std::io::Write as _;
    use tonic::transport::{Identity, ServerTlsConfig};

    const AUD: &str = "waddlebot-internal";

    /// Mock hub whose mint signs a real token with key A (`kid` k1) and whose JWKS serves key A.
    async fn signing_hub() -> MockHub {
        let (enc, _) = es256_keypair();
        let enc = Arc::new(enc);
        let sign = move |call: &crate::mock_hub::MintCall| {
            let now = now_secs();
            let mut claims = base_claims(now);
            claims.scope = call.scope.clone();
            Ok((make_token(&enc, "k1", &claims), 900))
        };
        MockHub::builder()
            .mint(vec![MintReply::With(Arc::new(sign))])
            .jwks(vec![JwksReply::es256("k1", ES_A_X, ES_A_Y)])
            .start()
            .await
    }

    async fn verify_via(
        bundle: &JwksTrustBundle,
        token: &str,
        scope: &str,
    ) -> Result<ServiceClaims, ServiceAuthError> {
        verify(token, bundle, AUD, &["hub-api"], scope).await
    }

    #[tokio::test]
    async fn real_path_mint_then_verify_over_grpc() {
        let hub = signing_hub().await;
        let sa = temp_sa_token("gp_mint_verify", "projected-sa\n");
        let client =
            MachineJwtClient::new(hub.endpoint.clone(), sa.clone(), "identity:ephemeral:mint");
        let token = client.get_token().await.expect("mint over gRPC");

        let bundle = JwksTrustBundle::new(hub.endpoint.clone());
        let claims = verify_via(&bundle, &token, "identity:ephemeral:mint")
            .await
            .expect("token minted over gRPC verifies against the gRPC-served JWKS");
        assert_eq!(claims.sub, "spiffe://penguintech.io/alpha/svc-process");

        let calls = hub.mint_calls();
        assert_eq!(calls.len(), 1);
        assert_eq!(
            calls[0].bearer.as_deref(),
            Some("projected-sa"),
            "SA token, trimmed, as bearer metadata"
        );
        assert_eq!(
            (calls[0].community_id, calls[0].connection_id),
            (None, None)
        );
        let deadline = calls[0].deadline.expect("every RPC carries a deadline");
        assert!(
            deadline <= crate::grpc::MAX_RPC_DEADLINE,
            "{deadline:?} must stay under hub-api's ceiling"
        );
        assert_eq!(hub.jwks_hits(), 1);
        let _ = std::fs::remove_file(sa);
    }

    #[tokio::test]
    async fn bound_mint_then_verify_carries_the_binding_over_grpc() {
        let hub = signing_hub().await;
        let sa = temp_sa_token("gp_bound", "sa");
        let client = BoundMachineJwtClient::with_timeouts(
            hub.endpoint.clone(),
            sa.clone(),
            "connections:resolve",
            HttpTimeouts::default(),
        )
        .expect("client");
        let token = client.get_token_for(7, Some(12)).await.expect("bound mint");
        client.get_token_for(8, None).await.expect("second binding");
        let calls = hub.mint_calls();
        assert_eq!(
            calls
                .iter()
                .map(|c| (c.community_id, c.connection_id))
                .collect::<Vec<_>>(),
            vec![(Some(7), Some(12)), (Some(8), None)]
        );
        let bundle = JwksTrustBundle::new(hub.endpoint.clone());
        verify_via(&bundle, &token, "connections:resolve")
            .await
            .expect("verifies");
        let _ = std::fs::remove_file(sa);
    }

    #[tokio::test]
    async fn a_token_signed_by_another_key_is_not_trusted_by_the_served_jwks() {
        let hub = signing_hub().await;
        let forged_key = EncodingKey::from_ec_der(ES_B_PRIV_DER);
        let forged = make_token(&forged_key, "k1", &base_claims(now_secs()));
        let bundle = JwksTrustBundle::new(hub.endpoint.clone());
        let err = verify_via(&bundle, &forged, "identity:ephemeral:mint")
            .await
            .unwrap_err();
        assert!(matches!(err, ServiceAuthError::InvalidToken(_)), "{err:?}");
    }

    #[tokio::test]
    async fn one_shared_channel_serves_mint_and_jwks() {
        let hub = signing_hub().await;
        let sa = temp_sa_token("gp_shared", "sa");
        let chan = HubChannel::lazy(hub.endpoint.clone(), None, HttpTimeouts::default());
        let client =
            MachineJwtClient::with_channel(chan.clone(), sa.clone(), "identity:ephemeral:mint");
        let bundle = JwksTrustBundle::with_channel(chan, JwksConfig::default());
        let token = client.get_token().await.expect("mint");
        verify_via(&bundle, &token, "identity:ephemeral:mint")
            .await
            .expect("verify");
        assert_eq!((hub.mint_hits(), hub.jwks_hits()), (1, 1));
        let _ = std::fs::remove_file(sa);
    }

    #[tokio::test]
    async fn unsupported_jwks_entries_fail_the_refresh_closed() {
        let mut rsa = es256_jwk("k1", ES_A_X, ES_A_Y);
        rsa.kty = "RSA".into();
        rsa.crv = String::new();
        let hub = MockHub::builder()
            .jwks(vec![JwksReply::Keys(vec![rsa])])
            .start()
            .await;
        let bundle = JwksTrustBundle::new(hub.endpoint.clone());
        assert!(
            bundle.public_key("k1").await.is_none(),
            "unsupported key type must not be cached"
        );
    }

    #[tokio::test]
    async fn malformed_p256_coordinates_fail_the_refresh_closed() {
        let short = b64url(&[7u8; 31]);
        for (x, y) in [(short.as_str(), ES_A_Y), (ES_A_X, ""), ("!!!", ES_A_Y)] {
            let hub = MockHub::builder()
                .jwks(vec![JwksReply::es256("k1", x, y)])
                .start()
                .await;
            let bundle = JwksTrustBundle::new(hub.endpoint.clone());
            assert!(bundle.public_key("k1").await.is_none(), "x={x:?} y={y:?}");
        }
    }

    #[tokio::test]
    async fn es256_kid_rotation_new_kid_is_picked_up_by_refetch() {
        let hub = MockHub::builder()
            .jwks(vec![
                JwksReply::es256("k1", ES_A_X, ES_A_Y),
                JwksReply::Keys(vec![
                    es256_jwk("k1", ES_A_X, ES_A_Y),
                    es256_jwk("k2", ES_B_X, ES_B_Y),
                ]),
            ])
            .start()
            .await;
        let bundle = JwksTrustBundle::new(hub.endpoint.clone());
        let now = now_secs();
        let (enc_a, _) = es256_keypair();
        let tok_a = make_token(&enc_a, "k1", &base_claims(now));
        verify_via(&bundle, &tok_a, "identity:ephemeral:mint")
            .await
            .expect("k1 verifies");
        // k2 is unknown to the cached set -> one refetch (rotation) -> now trusted.
        let enc_b = EncodingKey::from_ec_der(ES_B_PRIV_DER);
        let tok_b = make_token(&enc_b, "k2", &base_claims(now));
        verify_via(&bundle, &tok_b, "identity:ephemeral:mint")
            .await
            .expect("k2 verifies after rotation refetch");
        // A token signed by B but naming k1 is a bad signature, never accepted.
        let swapped = make_token(&enc_b, "k1", &base_claims(now));
        assert!(verify_via(&bundle, &swapped, "identity:ephemeral:mint")
            .await
            .is_err());
    }

    /// Hub serving an ES256 key `k1` AND a legacy Ed25519 key `old`.
    async fn dual_key_hub() -> MockHub {
        MockHub::builder()
            .jwks(vec![JwksReply::Keys(vec![
                es256_jwk("k1", ES_A_X, ES_A_Y),
                crate::mock_hub::ed25519_jwk("old", &b64url(KEY_A_PUB_RAW)),
            ])])
            .start()
            .await
    }

    #[tokio::test]
    async fn legacy_eddsa_token_rejected_by_default() {
        let hub = dual_key_hub().await;
        let bundle = JwksTrustBundle::new(hub.endpoint.clone());
        assert!(!bundle.accept_legacy_eddsa());
        let (enc, _) = ed25519_keypair();
        let token = make_token_alg(Algorithm::EdDSA, &enc, "old", &base_claims(now_secs()));
        let err = verify_via(&bundle, &token, "identity:ephemeral:mint")
            .await
            .unwrap_err();
        assert!(matches!(err, ServiceAuthError::InvalidToken(_)), "{err:?}");
        // The ES256 key published beside it still works (OKP entry was skipped, not fatal).
        let (enc_a, _) = es256_keypair();
        let ok = make_token(&enc_a, "k1", &base_claims(now_secs()));
        verify_via(&bundle, &ok, "identity:ephemeral:mint")
            .await
            .expect("es256 still verifies");
    }

    #[tokio::test]
    async fn legacy_eddsa_token_accepted_only_with_the_flag() {
        let hub = dual_key_hub().await;
        let config = JwksConfig {
            accept_legacy_eddsa: true,
            ..JwksConfig::default()
        };
        let bundle = JwksTrustBundle::with_config(hub.endpoint.clone(), config).expect("bundle");
        assert!(bundle.accept_legacy_eddsa());
        let (enc, _) = ed25519_keypair();
        let token = make_token_alg(Algorithm::EdDSA, &enc, "old", &base_claims(now_secs()));
        let claims = verify_via(&bundle, &token, "identity:ephemeral:mint")
            .await
            .expect("legacy window open");
        assert_eq!(claims.scope, "identity:ephemeral:mint");
    }

    #[tokio::test]
    async fn key_alg_confusion_is_refused_even_in_the_legacy_window() {
        let hub = dual_key_hub().await;
        let config = JwksConfig {
            accept_legacy_eddsa: true,
            ..JwksConfig::default()
        };
        let bundle = JwksTrustBundle::with_config(hub.endpoint.clone(), config).expect("bundle");
        let (enc_ed, _) = ed25519_keypair();
        // EdDSA-signed token naming the ES256 kid, and an ES256 token naming the Ed25519 kid.
        let (enc_es, _) = es256_keypair();
        let now = now_secs();
        for token in [
            make_token_alg(Algorithm::EdDSA, &enc_ed, "k1", &base_claims(now)),
            make_token_alg(Algorithm::ES256, &enc_es, "old", &base_claims(now)),
        ] {
            let err = verify_via(&bundle, &token, "identity:ephemeral:mint")
                .await
                .unwrap_err();
            assert!(matches!(err, ServiceAuthError::InvalidToken(_)), "{err:?}");
        }
    }

    #[test]
    fn legacy_flag_parses_strictly() {
        for (raw, want) in [
            (None, false),
            (Some(""), false),
            (Some("false"), false),
            (Some("0"), false),
            (Some("true"), true),
            (Some("TRUE"), true),
            (Some("1"), true),
        ] {
            assert_eq!(parse_legacy_flag(raw).expect("valid"), want, "{raw:?}");
        }
        for raw in ["yes", "on", "tru", "2"] {
            assert!(
                matches!(
                    parse_legacy_flag(Some(raw)),
                    Err(ServiceAuthError::Config(_))
                ),
                "{raw:?} must fail loud"
            );
        }
    }

    #[tokio::test]
    async fn plaintext_to_a_non_loopback_host_fails_loud_without_dialling() {
        let sa = temp_sa_token("gp_plain", "sa");
        let client = MachineJwtClient::new("http://10.255.255.1:50204", sa.clone(), "s");
        let err = client.get_token().await.unwrap_err();
        assert!(
            matches!(&err, ServiceAuthError::Config(m) if m.contains("non-loopback")),
            "{err:?}"
        );
        let _ = std::fs::remove_file(sa);
    }

    #[tokio::test]
    async fn https_without_a_ca_fails_loud_never_falls_back() {
        let sa = temp_sa_token("gp_noca", "sa");
        let client = MachineJwtClient::with_channel(
            HubChannel::lazy("https://127.0.0.1:1", None, HttpTimeouts::default()),
            sa.clone(),
            "s",
        );
        let err = client.get_token().await.unwrap_err();
        assert!(
            matches!(&err, ServiceAuthError::Config(m) if m.contains(crate::grpc::ENV_GRPC_CA_FILE)),
            "{err:?}"
        );
        let _ = std::fs::remove_file(sa);
    }

    /// Throwaway ECDSA P-256 PKI (rcgen's default key algorithm): a CA and a `127.0.0.1` leaf.
    struct Pki {
        ca_path: std::path::PathBuf,
        leaf_cert: String,
        leaf_key: String,
    }

    fn pki(label: &str) -> Pki {
        let ca_key = rcgen::KeyPair::generate().expect("ca key");
        let ca_params = rcgen::CertificateParams::new(vec![format!("service-auth-{label}-ca")])
            .expect("ca params");
        let ca_cert = ca_params.self_signed(&ca_key).expect("self-sign ca");
        let issuer = rcgen::Issuer::from_params(&ca_params, ca_key);
        let leaf_key = rcgen::KeyPair::generate().expect("leaf key");
        let leaf_params =
            rcgen::CertificateParams::new(vec!["127.0.0.1".to_string()]).expect("leaf params");
        let leaf = leaf_params
            .signed_by(&leaf_key, &issuer)
            .expect("sign leaf");
        let ca_path = std::env::temp_dir().join(format!(
            "service_auth_{}_{label}_ca.pem",
            std::process::id()
        ));
        std::fs::File::create(&ca_path)
            .and_then(|mut f| f.write_all(ca_cert.pem().as_bytes()))
            .expect("write ca");
        Pki {
            ca_path,
            leaf_cert: leaf.pem(),
            leaf_key: leaf_key.serialize_pem(),
        }
    }

    async fn tls_signing_hub(pki: &Pki) -> MockHub {
        let (enc, _) = es256_keypair();
        let enc = Arc::new(enc);
        let sign = move |call: &crate::mock_hub::MintCall| {
            let mut claims = base_claims(now_secs());
            claims.scope = call.scope.clone();
            Ok((make_token(&enc, "k1", &claims), 900))
        };
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind");
        let addr = listener.local_addr().expect("addr");
        MockHub::builder()
            .mint(vec![MintReply::With(Arc::new(sign))])
            .jwks(vec![JwksReply::es256("k1", ES_A_X, ES_A_Y)])
            .spawn(
                listener,
                addr,
                Some(
                    ServerTlsConfig::new()
                        .identity(Identity::from_pem(&pki.leaf_cert, &pki.leaf_key)),
                ),
            )
    }

    fn pinned(endpoint: &str, ca: &std::path::Path) -> HubChannel {
        HubChannel::lazy(
            endpoint,
            ca.to_str().map(str::to_owned),
            HttpTimeouts::default(),
        )
    }

    #[tokio::test]
    async fn tls13_mint_and_jwks_succeed_against_the_pinned_ca() {
        let pki = pki("ok");
        let hub = tls_signing_hub(&pki).await;
        assert!(hub.endpoint.starts_with("https://127.0.0.1:"));
        let sa = temp_sa_token("gp_tls_ok", "sa");
        let chan = pinned(&hub.endpoint, &pki.ca_path);
        let client =
            MachineJwtClient::with_channel(chan.clone(), sa.clone(), "identity:ephemeral:mint");
        let token = client.get_token().await.expect("mint over TLS");
        let bundle = JwksTrustBundle::with_channel(chan, JwksConfig::default());
        verify_via(&bundle, &token, "identity:ephemeral:mint")
            .await
            .expect("verify over TLS");
        let _ = std::fs::remove_file(sa);
        let _ = std::fs::remove_file(&pki.ca_path);
    }

    #[tokio::test]
    async fn tls_server_signed_by_an_untrusted_ca_is_refused() {
        let server_pki = pki("srv");
        let other_pki = pki("other");
        let hub = tls_signing_hub(&server_pki).await;
        let sa = temp_sa_token("gp_tls_bad", "sa");
        let timeouts = HttpTimeouts {
            connect: Duration::from_secs(2),
            request: Duration::from_secs(2),
        };
        let chan = HubChannel::lazy(
            hub.endpoint.clone(),
            other_pki.ca_path.to_str().map(str::to_owned),
            timeouts,
        );
        let client = MachineJwtClient::with_channel(chan, sa.clone(), "s");
        let err = client.get_token().await.unwrap_err();
        assert!(matches!(err, ServiceAuthError::Grpc(_)), "{err:?}");
        assert_eq!(
            hub.mint_hits(),
            0,
            "no RPC may reach a server the client does not trust"
        );
        for path in [&server_pki.ca_path, &other_pki.ca_path] {
            let _ = std::fs::remove_file(path);
        }
        let _ = std::fs::remove_file(sa);
    }

    /// The handshake the client performs is TLS 1.3 with ALPN `h2` -- observed on the server side
    /// of a raw rustls acceptor, which offers every protocol version it has (so 1.3 is negotiated
    /// because the CLIENT insists on it, not because the server forced it).
    #[tokio::test]
    async fn client_handshake_is_tls13_with_h2_alpn() {
        let pki = pki("v13");
        let certs: Vec<_> =
            rustls_pki_types::CertificateDer::pem_slice_iter(pki.leaf_cert.as_bytes())
                .collect::<Result<_, _>>()
                .expect("certs");
        let key =
            rustls_pki_types::PrivateKeyDer::from_pem_slice(pki.leaf_key.as_bytes()).expect("key");
        let provider = Arc::new(rustls::crypto::ring::default_provider());
        let mut server_cfg = rustls::ServerConfig::builder_with_provider(provider)
            .with_safe_default_protocol_versions()
            .expect("versions")
            .with_no_client_auth()
            .with_single_cert(certs, key)
            .expect("server cfg");
        server_cfg.alpn_protocols = vec![b"h2".to_vec()];
        let acceptor = tokio_rustls::TlsAcceptor::from(Arc::new(server_cfg));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind");
        let addr = listener.local_addr().expect("addr");
        let seen = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.expect("accept");
            let tls = acceptor.accept(tcp).await.expect("handshake");
            let (_, conn) = tls.get_ref();
            (
                conn.protocol_version(),
                conn.alpn_protocol().map(<[u8]>::to_vec),
            )
        });
        let sa = temp_sa_token("gp_tls_v", "sa");
        let timeouts = HttpTimeouts {
            connect: Duration::from_secs(2),
            request: Duration::from_millis(500),
        };
        let chan = HubChannel::lazy(
            format!("https://{addr}"),
            pki.ca_path.to_str().map(str::to_owned),
            timeouts,
        );
        // The raw acceptor never speaks HTTP/2, so the RPC itself fails; only the handshake matters.
        let _ = MachineJwtClient::with_channel(chan, sa.clone(), "s")
            .get_token()
            .await;
        let (version, alpn) = seen.await.expect("server task");
        assert_eq!(version, Some(rustls::ProtocolVersion::TLSv1_3));
        assert_eq!(alpn.as_deref(), Some(b"h2".as_slice()));
        let _ = std::fs::remove_file(&pki.ca_path);
        let _ = std::fs::remove_file(sa);
    }
}

/// Real-path tests of [`MachineJwtClient`]'s reuse / single-flight / shared-failure contract:
/// a real tonic `MintServiceToken` server (`MockHub`) that counts mints, real concurrent
/// callers, and the tokio clock moved with `pause`/`advance`/`resume` (never auto-advance,
/// which would skip real I/O deadlines).
#[cfg(test)]
mod machine_client_single_flight_tests {
    use std::sync::Arc;
    use std::time::Duration;

    use tokio::task::JoinSet;

    use super::tests::temp_sa_token;
    use super::*;
    use crate::mock_hub::{MintReply, MockHub};

    const SCOPE: &str = "key:stream-dek:read";

    async fn skip(by: Duration) {
        tokio::time::pause();
        tokio::time::advance(by).await;
        tokio::time::resume();
    }

    async fn hub(replies: Vec<MintReply>, delay: Duration) -> MockHub {
        MockHub::builder().mint(replies).delay(delay).start().await
    }

    fn client(hub: &MockHub, tag: &str) -> Arc<MachineJwtClient> {
        let sa = temp_sa_token(&format!("sf_{tag}"), "sa-token");
        Arc::new(MachineJwtClient::new(hub.endpoint.clone(), sa, SCOPE))
    }

    /// `n` concurrent `get_token` calls; each result is `Ok(token)` or the error's display.
    async fn get_all(client: &Arc<MachineJwtClient>, n: usize) -> Vec<Result<String, String>> {
        let mut set = JoinSet::new();
        for _ in 0..n {
            let client = client.clone();
            set.spawn(async move {
                client
                    .get_token()
                    .await
                    .map(|t| t.to_string())
                    .map_err(|e| e.to_string())
            });
        }
        let mut out = Vec::with_capacity(n);
        while let Some(joined) = set.join_next().await {
            out.push(joined.expect("task"));
        }
        out
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn cold_start_stampede_is_exactly_one_mint() {
        let hub = hub(
            vec![MintReply::token("tok-1", 900)],
            Duration::from_millis(150),
        )
        .await;
        let c = client(&hub, "cold");
        let results = get_all(&c, 160).await;
        assert!(results.iter().all(|r| r.as_deref() == Ok("tok-1")));
        assert_eq!(hub.mint_hits(), 1, "160 concurrent cold callers = 1 mint");
        // And the steady state is pure reuse.
        let again = get_all(&c, 160).await;
        assert!(again.iter().all(|r| r.as_deref() == Ok("tok-1")));
        assert_eq!(hub.mint_hits(), 1);
    }

    #[tokio::test]
    async fn token_is_reused_until_eighty_percent_of_its_life_then_refreshed_once() {
        let hub = hub(
            vec![
                MintReply::token("tok-1", 1000),
                MintReply::token("tok-2", 1000),
            ],
            Duration::from_millis(50),
        )
        .await;
        let c = client(&hub, "refresh");
        assert_eq!(&*c.get_token().await.unwrap(), "tok-1");
        skip(Duration::from_secs(799)).await; // 79.9% of 1000 s
        assert!(get_all(&c, 50)
            .await
            .iter()
            .all(|r| r.as_deref() == Ok("tok-1")));
        assert_eq!(
            hub.mint_hits(),
            1,
            "still inside 80% of the lifetime: reuse"
        );
        skip(Duration::from_secs(2)).await; // 80.1%
        let after = get_all(&c, 160).await;
        assert!(
            after.iter().all(|r| r.as_deref() == Ok("tok-2")),
            "every caller got the refreshed token"
        );
        assert_eq!(
            hub.mint_hits(),
            2,
            "160 callers at the refresh point = one refresh"
        );
    }

    #[tokio::test]
    async fn a_failed_refresh_keeps_serving_the_still_valid_token_without_re_minting() {
        let hub = hub(
            vec![
                MintReply::token("tok-1", 1000),
                MintReply::Reject(tonic::Code::Unavailable),
            ],
            Duration::from_millis(50),
        )
        .await;
        let c = client(&hub, "stale");
        assert_eq!(&*c.get_token().await.unwrap(), "tok-1");
        skip(Duration::from_secs(850)).await; // refresh due, token valid until 970 s
        let results = get_all(&c, 160).await;
        assert!(
            results.iter().all(|r| r.as_deref() == Ok("tok-1")),
            "a hub-api blip at refresh time must not fail callers holding a valid token"
        );
        assert_eq!(hub.mint_hits(), 2, "one refresh attempt, shared by all 160");
        // Inside the failure window nobody re-hits the hub.
        assert!(get_all(&c, 50)
            .await
            .iter()
            .all(|r| r.as_deref() == Ok("tok-1")));
        assert_eq!(hub.mint_hits(), 2);
    }

    #[tokio::test]
    async fn a_cold_failure_is_shared_once_then_retried_after_the_jittered_backoff() {
        let hub = hub(
            vec![
                MintReply::Reject(tonic::Code::Unavailable),
                MintReply::token("tok-ok", 900),
            ],
            Duration::from_millis(100),
        )
        .await;
        let c = client(&hub, "cold_fail");
        let results = get_all(&c, 160).await;
        assert!(results.iter().all(Result::is_err), "no token: fail closed");
        assert_eq!(hub.mint_hits(), 1, "160 waiters share one failed mint");
        // Still inside the window: replayed, no new mint.
        assert!(get_all(&c, 20).await.iter().all(Result::is_err));
        assert_eq!(hub.mint_hits(), 1);
        // Past the (jittered, <= 625 ms) window the hub is tried again and recovers.
        skip(Duration::from_secs(2)).await;
        assert_eq!(&*c.get_token().await.unwrap(), "tok-ok");
        assert_eq!(hub.mint_hits(), 2);
    }

    #[tokio::test]
    async fn the_cached_token_is_scope_specific() {
        let hub = hub(
            vec![MintReply::With(Arc::new(|call| {
                Ok((format!("tok-for-{}", call.scope), 900))
            }))],
            Duration::ZERO,
        )
        .await;
        let sa = temp_sa_token("sf_scope", "sa-token");
        let a = MachineJwtClient::new(hub.endpoint.clone(), sa.clone(), "key:stream-dek:read");
        let b = MachineJwtClient::new(hub.endpoint.clone(), sa, "identity:ephemeral:mint");
        assert_eq!(
            &*a.get_token().await.unwrap(),
            "tok-for-key:stream-dek:read"
        );
        assert_eq!(
            &*b.get_token().await.unwrap(),
            "tok-for-identity:ephemeral:mint",
            "a token minted for one scope is never reused for another"
        );
        assert_eq!(hub.mint_hits(), 2);
    }

    #[test]
    fn jitter_stays_within_a_quarter_of_the_base_and_actually_varies() {
        let base = Duration::from_millis(1000);
        let samples: Vec<Duration> = (0..200).map(|_| jittered(base)).collect();
        assert!(samples
            .iter()
            .all(|d| *d >= Duration::from_millis(750) && *d < Duration::from_millis(1250)));
        let first = samples[0];
        assert!(samples.iter().any(|d| *d != first), "jitter must vary");
    }
}
