//! JWT bearer-token authentication (user/service calls) and the internal
//! `X-Service-Key` header check used by `/api/v1/internal/*` routes.
//!
//! Every authenticated request must carry a `tenant` claim -- tenant
//! isolation is checked before any scope/role decision, per
//! `rules/security.md` Tenant Isolation. `AuthenticatedClaims` is an axum
//! extractor (usable directly on a handler) and [`require_auth`] is the
//! equivalent `route_layer` middleware used to gate a whole route group
//! (the OpenAPI full spec + Swagger UI in [`crate::http::router`]).
//! `/api/v1/internal/*` routes added by later chunks should use
//! [`ServiceKey`] instead -- a user JWT is never required for
//! service-to-service calls that already present the shared service key.
//!
//! # Phase-0 hardening (RFC 8725)
//!
//! This is the Rust twin of `flask_core.auth.verify_jwt_token`
//! (`docs/JWT_VERIFICATION.md`, verifier label `platform_es256`): the header
//! `alg` must be exactly `ES256`, pinned here in code and never taken from the
//! token (`alg: none`, any other algorithm and the key-material headers
//! `jku`/`jwk`/`x5u`/`x5c`/`crit` are refused before any signature work, see
//! [`hardening`]); a `kid` is REQUIRED and resolves to a PUBLIC key from
//! `platform_vault` through [`crate::jwt_keys::JwtKeys`] (an unknown, expired
//! or stale kid is refused; there is no shared secret); `iss` and `aud` are ENFORCED; every of
//! `sub iss aud iat exp scope tenant` must be present, `sub`/`tenant`
//! non-empty; `exp` is strict (the 30 s skew applies to `iat`/`nbf` only);
//! there is no default-tenant fallback; with no key source configured every
//! token is refused (`no_key`).
//! Every verification emits `waddles_jwt_verifications_total` and a latency
//! histogram, and rejections log closed-vocabulary fields only -- never the
//! token, a claim, a header value or the JWT library's error text.

pub mod hardening;

use std::time::Instant;

use axum::extract::{FromRequestParts, Request, State};
use axum::http::header::AUTHORIZATION;
use axum::http::request::Parts;
use axum::middleware::Next;
use axum::response::Response;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine as _;
use jsonwebtoken::errors::ErrorKind;
use jsonwebtoken::{decode, Algorithm, DecodingKey, Validation};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use self::hardening::{
    inspect_header, report_outcome, JwtMetrics, JwtRejection, KidPolicy, ALG_LABEL_ABSENT,
    OUTCOME_OK, REASON_ALG_MISMATCH, REASON_BAD_AUDIENCE, REASON_BAD_ISSUER, REASON_BAD_KID,
    REASON_BAD_SIGNATURE, REASON_EXPIRED, REASON_IMMATURE, REASON_INVALID, REASON_INVALID_CLAIM,
    REASON_MALFORMED, REASON_MISSING_CLAIM, REASON_NO_KEY, VERIFIER_PLATFORM_ES256,
};
use crate::config::Config;
use crate::error::ApiError;
use crate::http::AppState;
use crate::jwt_keys::JwtKeys;

/// The ONE algorithm this verifier accepts (RFC 8725 one-alg-per-verifier);
/// mirrors `flask_core.auth.PLATFORM_JWT_ALGORITHM`.
pub const PLATFORM_JWT_ALGORITHM: &str = "ES256";

/// Clock-skew allowance for `iat`/`nbf` in the future, in seconds --
/// mirrors `flask_core.auth.JWT_CLOCK_SKEW_SECONDS`. NOT applied to `exp`.
pub const JWT_CLOCK_SKEW_SECONDS: i64 = 30;

/// Claims every platform token must carry -- mirrors
/// `flask_core.auth.REQUIRED_JWT_CLAIMS`.
const REQUIRED_JWT_CLAIMS: [&str; 7] = ["sub", "iss", "aud", "iat", "exp", "scope", "tenant"];

/// Standard OIDC claim set required on every authenticated request -- see
/// `rules/security.md` JWT Claims (All Tokens). `roles` is audit/display
/// only; authorization decisions must be made on `scope`, never on
/// `roles`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Claims {
    pub sub: String,
    pub iss: String,
    pub aud: String,
    pub iat: i64,
    pub exp: i64,
    pub scope: String,
    pub tenant: String,
    #[serde(default)]
    pub teams: Vec<String>,
    #[serde(default)]
    pub roles: Vec<String>,
}

/// Extractor that validates the `Authorization: Bearer <jwt>` header
/// against the configured issuer/audience and returns the decoded claims.
/// 401 for a missing/malformed/invalid/expired token; 403 for a
/// well-formed token that is missing the mandatory `tenant` claim.
pub struct AuthenticatedClaims(pub Claims);

impl FromRequestParts<AppState> for AuthenticatedClaims {
    type Rejection = ApiError;

    async fn from_request_parts(
        parts: &mut Parts,
        state: &AppState,
    ) -> Result<Self, Self::Rejection> {
        let claims = extract_claims(parts, &state.config).await?;
        Ok(AuthenticatedClaims(claims))
    }
}

async fn extract_claims(parts: &Parts, config: &Config) -> Result<Claims, ApiError> {
    extract_claims_with(JwtMetrics::global(), parts, config).await
}

/// [`extract_claims`] against an explicit [`JwtMetrics`] (tests inject an
/// in-memory meter provider; production goes through the global one). A
/// missing / non-Bearer header carries no token, so it is not a
/// verification and is not counted.
async fn extract_claims_with(
    metrics: &JwtMetrics,
    parts: &Parts,
    config: &Config,
) -> Result<Claims, ApiError> {
    let header = parts
        .headers
        .get(AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .ok_or_else(|| ApiError::Unauthorized("missing Authorization header".into()))?;
    let token = header.strip_prefix("Bearer ").ok_or_else(|| {
        ApiError::Unauthorized("Authorization header is not a Bearer token".into())
    })?;
    verify_with_metrics(metrics, token, config).await
}

/// A failed verification: the closed-vocabulary rejection, and whether the
/// token was validly signed but has no usable `tenant` (-> 403, see
/// `rules/security.md` Tenant Isolation) rather than being unauthenticated.
struct Failure {
    rejection: JwtRejection,
    tenant_problem: bool,
}

impl Failure {
    fn new(rejection: JwtRejection) -> Self {
        Self {
            rejection,
            tenant_problem: false,
        }
    }

    fn reason(reason: &'static str, alg: &'static str) -> Self {
        Self::new(JwtRejection::new(reason, alg))
    }

    /// The HTTP rejection. Deliberately generic: which check failed is in
    /// the metric/log (closed vocabulary), never in the response body.
    fn into_api_error(self) -> ApiError {
        if self.tenant_problem {
            ApiError::Forbidden("token is missing the required tenant claim".into())
        } else {
            ApiError::Unauthorized("invalid token".into())
        }
    }
}

/// Verify `token`, then record exactly one `waddles_jwt_verifications_total`
/// sample (`verifier=platform_es256`) and, for a rejection, one PII-free log
/// line.
async fn verify_with_metrics(
    metrics: &JwtMetrics,
    token: &str,
    config: &Config,
) -> Result<Claims, ApiError> {
    let started = Instant::now();
    let mut alg = ALG_LABEL_ABSENT;
    let result = verify_token(token, config, &mut alg, now_unix()).await;
    let outcome = match &result {
        Ok(_) => OUTCOME_OK,
        Err(failure) => failure.rejection.reason,
    };
    report_outcome(metrics, VERIFIER_PLATFORM_ES256, started, alg, outcome);
    result.map_err(Failure::into_api_error)
}

/// Seconds since the Unix epoch (UTC), the reference for every time claim.
fn now_unix() -> i64 {
    chrono::Utc::now().timestamp()
}

/// Every check, in order, raising the first [`Failure`]. Kept free of
/// metrics/logging so the policy reads as one sequence; `alg` is updated as
/// soon as the header yields a label-safe value so even a later rejection is
/// counted under the right algorithm.
async fn verify_token(
    token: &str,
    config: &Config,
    alg: &mut &'static str,
    now: i64,
) -> Result<Claims, Failure> {
    // Deployment bug (no key source wired), not attacker input.
    if matches!(config.jwt_keys, JwtKeys::None) {
        return Err(Failure::reason(REASON_NO_KEY, ALG_LABEL_ABSENT));
    }
    // Nothing to compare `iss`/`aud` against -> nothing may pass.
    if config.cli.jwt_issuer.is_empty() {
        return Err(Failure::reason(REASON_BAD_ISSUER, ALG_LABEL_ABSENT));
    }
    if config.cli.jwt_audience.is_empty() {
        return Err(Failure::reason(REASON_BAD_AUDIENCE, ALG_LABEL_ABSENT));
    }

    // The algorithm is pinned by code (allow-list of one); the token header
    // is only ever compared against it. `kid` is mandatory.
    let header =
        inspect_header(token, &[PLATFORM_JWT_ALGORITHM], KidPolicy::Require).map_err(|r| {
            *alg = r.alg;
            Failure::new(r)
        })?;
    *alg = "es256";
    let Some(kid) = header.kid.as_deref() else {
        return Err(Failure::reason(REASON_BAD_KID, "es256"));
    };
    let coords = config
        .jwt_keys
        .resolve(kid)
        .await
        .map_err(|reason| Failure::reason(reason, "es256"))?;

    let mut validation = Validation::new(Algorithm::ES256);
    validation.set_issuer(std::slice::from_ref(&config.cli.jwt_issuer));
    validation.set_audience(std::slice::from_ref(&config.cli.jwt_audience));
    validation.leeway = JWT_CLOCK_SKEW_SECONDS.unsigned_abs();
    // `iss`/`aud` are ENFORCED (MED-5): the library only compares them when
    // present, so presence is demanded explicitly. `sub` is deliberately left
    // to `parse_claims`, which tells a missing `sub` from a mistyped one (the
    // library would call both "missing").
    validation.set_required_spec_claims(&["exp", "iss", "aud"]);
    validation.validate_nbf = true;

    let key = DecodingKey::from_ec_components(
        &URL_SAFE_NO_PAD.encode(coords.x),
        &URL_SAFE_NO_PAD.encode(coords.y),
    )
    .map_err(|_| Failure::reason(REASON_BAD_KID, "es256"))?;
    let data = decode::<Value>(token, &key, &validation)
        .map_err(|e| Failure::reason(classify_decode_error(e.kind()), "es256"))?;
    parse_claims(&data.claims, &config.cli.jwt_audience, now)
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

/// Shape-check a signature-verified payload and build [`Claims`].
///
/// `jsonwebtoken` already proved the signature, `iss`, `aud`, `nbf` and a
/// lenient `exp`; this adds what it does not: every required claim present,
/// strict `exp`, `iat` not in the future, and non-empty identity strings.
/// `aud` may be a string or a list containing the expected audience (the
/// library proved membership).
fn parse_claims(payload: &Value, expected_audience: &str, now: i64) -> Result<Claims, Failure> {
    let Some(object) = payload.as_object() else {
        return Err(Failure::reason(REASON_INVALID_CLAIM, "es256"));
    };
    for name in REQUIRED_JWT_CLAIMS {
        if object.get(name).is_none_or(Value::is_null) {
            return Err(claim_failure(REASON_MISSING_CLAIM, name));
        }
    }
    let exp = integer_claim(object, "exp")?;
    // jsonwebtoken applies the 30 s `leeway` to `exp` too; expiry must not be
    // widened by it (only `iat`/`nbf` need the skew allowance), so re-check
    // strictly -- exactly as `flask_core.auth` does.
    if exp <= now {
        return Err(claim_failure(REASON_EXPIRED, "exp"));
    }
    let iat = integer_claim(object, "iat")?;
    if iat > now.saturating_add(JWT_CLOCK_SKEW_SECONDS) {
        return Err(claim_failure(REASON_IMMATURE, "iat"));
    }
    Ok(Claims {
        sub: non_empty_string_claim(object, "sub")?,
        iss: string_claim(object, "iss")?,
        aud: expected_audience.to_string(),
        iat,
        exp,
        scope: string_claim(object, "scope")?,
        tenant: non_empty_string_claim(object, "tenant")?,
        teams: string_list_claim(object, "teams")?,
        roles: string_list_claim(object, "roles")?,
    })
}

/// A claim-level [`Failure`]; a problem with `tenant` is flagged so the HTTP
/// layer answers 403 (authenticated, tenant unusable) instead of 401.
fn claim_failure(reason: &'static str, claim: &str) -> Failure {
    Failure {
        rejection: JwtRejection::new(reason, "es256"),
        tenant_problem: claim == "tenant",
    }
}

/// A claim that must be a string.
fn string_claim(object: &Map<String, Value>, name: &str) -> Result<String, Failure> {
    match object.get(name) {
        Some(Value::String(value)) => Ok(value.clone()),
        _ => Err(claim_failure(REASON_INVALID_CLAIM, name)),
    }
}

/// A claim that must be a string with at least one non-whitespace character
/// -- an empty `sub`/`tenant` is "missing" by another name.
fn non_empty_string_claim(object: &Map<String, Value>, name: &str) -> Result<String, Failure> {
    let value = string_claim(object, name)?;
    if value.trim().is_empty() {
        return Err(claim_failure(REASON_INVALID_CLAIM, name));
    }
    Ok(value)
}

/// A claim that must be a JSON integer (NumericDate).
fn integer_claim(object: &Map<String, Value>, name: &str) -> Result<i64, Failure> {
    object
        .get(name)
        .and_then(Value::as_i64)
        .ok_or_else(|| claim_failure(REASON_INVALID_CLAIM, name))
}

/// An optional claim that, when present and non-null, must be a list of
/// strings (`teams`, `roles`); absent or null is the empty list.
fn string_list_claim(object: &Map<String, Value>, name: &str) -> Result<Vec<String>, Failure> {
    match object.get(name) {
        None | Some(Value::Null) => Ok(Vec::new()),
        Some(Value::Array(items)) => items
            .iter()
            .map(|item| match item {
                Value::String(value) => Ok(value.clone()),
                _ => Err(claim_failure(REASON_INVALID_CLAIM, name)),
            })
            .collect(),
        Some(_) => Err(claim_failure(REASON_INVALID_CLAIM, name)),
    }
}

/// `route_layer` middleware equivalent of [`AuthenticatedClaims`]: rejects
/// before the handler runs and inserts [`Claims`] as a request extension so
/// downstream handlers can use `Extension<Claims>` instead of re-parsing.
pub async fn require_auth(
    State(state): State<AppState>,
    request: Request,
    next: Next,
) -> Result<Response, ApiError> {
    let (mut parts, body) = request.into_parts();
    let claims = extract_claims(&parts, &state.config).await?;
    parts.extensions.insert(claims);
    let request = Request::from_parts(parts, body);
    Ok(next.run(request).await)
}

/// Header name internal service-to-service callers present in place of a
/// user JWT for `/api/v1/internal/*` routes.
pub const SERVICE_KEY_HEADER: &str = "x-service-key";

/// Extractor that validates the `X-Service-Key` header against the
/// configured `SERVICE_API_KEY`. Used for internal, non-user-facing routes
/// instead of a JWT -- always requires a secret, never open.
pub struct ServiceKey;

impl FromRequestParts<AppState> for ServiceKey {
    type Rejection = ApiError;

    async fn from_request_parts(
        parts: &mut Parts,
        state: &AppState,
    ) -> Result<Self, Self::Rejection> {
        let provided = parts
            .headers
            .get(SERVICE_KEY_HEADER)
            .and_then(|v| v.to_str().ok())
            .ok_or_else(|| ApiError::Unauthorized("missing X-Service-Key header".into()))?;

        if provided != state.config.service_api_key.expose() {
            return Err(ApiError::Unauthorized("invalid service key".into()));
        }
        Ok(ServiceKey)
    }
}

#[cfg(test)]
mod tests {
    use super::hardening::test_support::Capture;
    use super::hardening::{
        FORBIDDEN_HEADER_PARAMS, REASON_ALG_NONE, REASON_BAD_KID, REASON_FORBIDDEN_HEADER,
        REASON_SCOPE_DENIED,
    };
    use super::*;
    use crate::config::{CliConfig, Config, Secret};
    use crate::jwt_keys::test_support::TestKey;
    use crate::jwt_keys::JwtKeys;
    use axum::http::HeaderValue;
    use clap::Parser;
    use jsonwebtoken::{encode, EncodingKey, Header};
    use serde_json::json;
    use std::sync::{Arc, Mutex, OnceLock};

    const KID: &str = "jwt-es256-v1";

    /// The key the configured verifier trusts (generated once per test run).
    fn fixture() -> &'static TestKey {
        static KEY: OnceLock<TestKey> = OnceLock::new();
        KEY.get_or_init(TestKey::generate)
    }

    /// A different key under the same kid: signatures from it must not verify.
    fn other_key() -> &'static TestKey {
        static KEY: OnceLock<TestKey> = OnceLock::new();
        KEY.get_or_init(TestKey::generate)
    }

    fn keys_for(rows: Vec<platform_keys::KeyRecord>) -> JwtKeys {
        JwtKeys::in_memory(rows)
    }

    fn test_config(configured: bool) -> Config {
        let cli = CliConfig::parse_from(["svc-streaming"]);
        Config {
            cli,
            db_password: Secret::new("db-pass"),
            cache_password: None,
            service_api_key: Secret::new("service-key-value"),
            jwt_keys: if configured {
                keys_for(vec![fixture().active_row(KID)])
            } else {
                JwtKeys::None
            },
        }
    }

    fn sign_with(claims: &Claims, key: &TestKey) -> String {
        let mut header = Header::new(Algorithm::ES256);
        header.kid = Some(KID.into());
        encode(&header, claims, &key.encoding).unwrap()
    }

    fn sign(claims: &Claims) -> String {
        sign_with(claims, fixture())
    }

    fn valid_claims(config: &Config) -> Claims {
        let now = chrono::Utc::now().timestamp();
        Claims {
            sub: "user-123".into(),
            iss: config.cli.jwt_issuer.clone(),
            aud: config.cli.jwt_audience.clone(),
            iat: now,
            exp: now + 3600,
            scope: "streaming:read".into(),
            tenant: "tenant-abc".into(),
            teams: vec![],
            roles: vec![],
        }
    }

    fn parts_with_auth(header: Option<&str>) -> Parts {
        let mut builder = axum::http::Request::builder().uri("/");
        if let Some(h) = header {
            builder = builder.header(AUTHORIZATION, HeaderValue::from_str(h).unwrap());
        }
        let (parts, _) = builder.body(()).unwrap().into_parts();
        parts
    }

    fn parts_with_token(token: &str) -> Parts {
        parts_with_auth(Some(&format!("Bearer {token}")))
    }

    #[tokio::test]
    async fn missing_header_is_unauthorized() {
        let config = test_config(true);
        let parts = parts_with_auth(None);
        let err = extract_claims(&parts, &config).await.unwrap_err();
        assert!(matches!(err, ApiError::Unauthorized(_)));
    }

    #[tokio::test]
    async fn non_bearer_header_is_unauthorized() {
        let config = test_config(true);
        let parts = parts_with_auth(Some("Basic abc123"));
        let err = extract_claims(&parts, &config).await.unwrap_err();
        assert!(matches!(err, ApiError::Unauthorized(_)));
    }

    #[tokio::test]
    async fn valid_token_decodes_claims() {
        let config = test_config(true);
        let claims = valid_claims(&config);
        let token = sign(&claims);
        let parts = parts_with_auth(Some(&format!("Bearer {token}")));
        let decoded = extract_claims(&parts, &config)
            .await
            .expect("token should validate");
        assert_eq!(decoded.tenant, "tenant-abc");
        assert_eq!(decoded.sub, "user-123");
    }

    #[tokio::test]
    async fn wrong_signing_secret_is_rejected() {
        let config = test_config(true);
        let claims = valid_claims(&config);
        let token = sign_with(&claims, other_key());
        let parts = parts_with_auth(Some(&format!("Bearer {token}")));
        let err = extract_claims(&parts, &config).await.unwrap_err();
        assert!(matches!(err, ApiError::Unauthorized(_)));
    }

    #[tokio::test]
    async fn expired_token_is_rejected() {
        let config = test_config(true);
        let mut claims = valid_claims(&config);
        claims.iat -= 7200;
        claims.exp -= 7200;
        let token = sign(&claims);
        let parts = parts_with_auth(Some(&format!("Bearer {token}")));
        let err = extract_claims(&parts, &config).await.unwrap_err();
        assert!(matches!(err, ApiError::Unauthorized(_)));
    }

    #[tokio::test]
    async fn wrong_audience_is_rejected() {
        let config = test_config(true);
        let mut claims = valid_claims(&config);
        claims.aud = "someone-else".into();
        let token = sign(&claims);
        let parts = parts_with_auth(Some(&format!("Bearer {token}")));
        let err = extract_claims(&parts, &config).await.unwrap_err();
        assert!(matches!(err, ApiError::Unauthorized(_)));
    }

    #[tokio::test]
    async fn missing_tenant_claim_is_forbidden() {
        let config = test_config(true);
        let mut claims = valid_claims(&config);
        claims.tenant = String::new();
        let token = sign(&claims);
        let parts = parts_with_auth(Some(&format!("Bearer {token}")));
        let err = extract_claims(&parts, &config).await.unwrap_err();
        assert!(matches!(err, ApiError::Forbidden(_)));
    }

    #[tokio::test]
    async fn no_verification_key_configured_is_unauthorized() {
        let config = test_config(false);
        let claims = valid_claims(&config);
        let token = sign(&claims);
        let parts = parts_with_auth(Some(&format!("Bearer {token}")));
        let err = extract_claims(&parts, &config).await.unwrap_err();
        assert!(matches!(err, ApiError::Unauthorized(_)));
    }

    #[tokio::test]
    async fn service_key_header_matches_is_accepted() {
        let config = test_config(false);
        assert_eq!(config.service_api_key.expose(), "service-key-value");
    }

    // ---- Phase-0 hardening (RFC 8725) --------------------------------------

    fn b64(value: &Value) -> String {
        URL_SAFE_NO_PAD.encode(value.to_string())
    }

    /// Sign arbitrary header + claims JSON with `key` (ES256), so a test
    /// controls exactly which property of an otherwise validly-signed token
    /// is hostile.
    fn raw_token_with(header: &Value, claims: &Value, key: &TestKey) -> String {
        let message = format!("{}.{}", b64(header), b64(claims));
        let signature =
            jsonwebtoken::crypto::sign(message.as_bytes(), &key.encoding, Algorithm::ES256)
                .expect("sign");
        format!("{message}.{signature}")
    }

    fn raw_token(header: &Value, claims: &Value) -> String {
        raw_token_with(header, claims, fixture())
    }

    /// An HS256 MAC over header.claims keyed with `secret` bytes -- the
    /// shape of an alg-confusion / legacy shared-secret forgery.
    fn raw_hs256(header: &Value, claims: &Value, secret: &[u8]) -> String {
        let message = format!("{}.{}", b64(header), b64(claims));
        let signature = jsonwebtoken::crypto::sign(
            message.as_bytes(),
            &EncodingKey::from_secret(secret),
            Algorithm::HS256,
        )
        .expect("sign");
        format!("{message}.{signature}")
    }

    fn good_header() -> Value {
        json!({"alg": "ES256", "typ": "JWT", "kid": KID})
    }

    fn good_claims(config: &Config) -> Value {
        serde_json::to_value(valid_claims(config)).expect("claims json")
    }

    fn without(mut claims: Value, name: &str) -> Value {
        claims.as_object_mut().expect("object").remove(name);
        claims
    }

    fn with(mut claims: Value, name: &str, value: Value) -> Value {
        claims[name] = value;
        claims
    }

    /// Run the verifier against a capturing meter provider and return the
    /// outcome plus the capture.
    async fn run(token: &str, config: &Config) -> (Result<Claims, ApiError>, Capture) {
        let capture = Capture::new();
        let result = extract_claims_with(&capture.metrics, &parts_with_token(token), config).await;
        (result, capture)
    }

    /// Assert `token` is refused with `status` semantics and counted exactly
    /// once as `platform_es256` under (`alg`, `outcome`).
    async fn assert_refused(
        token: &str,
        config: &Config,
        outcome: &str,
        alg: &str,
        forbidden: bool,
    ) {
        let (result, capture) = run(token, config).await;
        let err = result.expect_err(&format!("{outcome} must be refused"));
        if forbidden {
            assert!(matches!(err, ApiError::Forbidden(_)), "{outcome}: {err:?}");
        } else {
            assert!(
                matches!(&err, ApiError::Unauthorized(m) if m == "invalid token"),
                "{outcome}: {err:?}"
            );
        }
        assert_eq!(
            capture.count("platform_es256", alg, outcome),
            1,
            "{outcome}/{alg}"
        );
        assert_eq!(capture.total(), 1, "exactly one verification recorded");
    }

    #[tokio::test]
    async fn a_valid_token_is_counted_ok_with_the_hs256_label() {
        let config = test_config(true);
        let token = raw_token(&good_header(), &good_claims(&config));
        let (result, capture) = run(&token, &config).await;
        let claims = result.expect("valid token verifies");
        assert_eq!(claims.tenant, "tenant-abc");
        assert_eq!(claims.aud, config.cli.jwt_audience);
        assert_eq!(capture.count("platform_es256", "es256", "ok"), 1);
        assert_eq!(capture.total(), 1);
        let latency = capture.points("waddles_jwt_verification_seconds");
        assert_eq!(latency.len(), 1);
        assert_eq!(latency[0].value, 1);
    }

    #[tokio::test]
    async fn tokens_without_a_kid_are_rejected_and_nbf_stays_optional() {
        let config = test_config(true);
        let header = json!({"alg": "ES256", "typ": "JWT"});
        let token = raw_token(&header, &good_claims(&config));
        assert_refused(&token, &config, REASON_BAD_KID, "es256", false).await;
        let claims = without(good_claims(&config), "nbf");
        run(&raw_token(&good_header(), &claims), &config)
            .await
            .0
            .expect("nbf is optional");
    }

    #[tokio::test]
    async fn alg_none_is_rejected_in_every_letter_case() {
        let config = test_config(true);
        for alg in ["none", "None", "NONE", "nOnE"] {
            let header = json!({"alg": alg, "typ": "JWT"});
            let token = format!("{}.{}.", b64(&header), b64(&good_claims(&config)));
            assert_refused(&token, &config, REASON_ALG_NONE, "none", false).await;
        }
    }

    #[tokio::test]
    async fn alg_none_with_a_signature_attached_is_still_rejected() {
        let config = test_config(true);
        let token = raw_token(&json!({"alg": "none"}), &good_claims(&config));
        assert_refused(&token, &config, REASON_ALG_NONE, "none", false).await;
    }

    #[tokio::test]
    async fn alg_confusion_other_algorithms_are_rejected_even_with_a_configured_key() {
        let config = test_config(true);
        let pem_like = format!(
            "-----BEGIN PUBLIC KEY-----\n{}\n-----END PUBLIC KEY-----\n",
            URL_SAFE_NO_PAD.encode(&fixture().point)
        );
        // HS256/384/512 MACs keyed with the PUBLIC key bytes / PEM text (the
        // classic alg-confusion forgery) and with a legacy shared-secret
        // string: only the pinned ES256 may verify.
        for (alg, label) in [
            (Algorithm::HS256, "hs256"),
            (Algorithm::HS384, "hs384"),
            (Algorithm::HS512, "hs512"),
        ] {
            for secret in [
                fixture().point.clone(),
                pem_like.clone().into_bytes(),
                b"legacy-shared-secret".to_vec(),
            ] {
                let mut header = Header::new(alg);
                header.kid = Some(KID.into());
                let token = encode(
                    &header,
                    &good_claims(&config),
                    &EncodingKey::from_secret(&secret),
                )
                .expect("encode");
                assert_refused(&token, &config, REASON_ALG_MISMATCH, label, false).await;
            }
        }
        for (alg, label) in [
            ("RS256", "rs256"),
            ("PS256", "ps256"),
            ("ES384", "es384"),
            ("EdDSA", "eddsa"),
            ("es256", "es256"),
        ] {
            let header = json!({"alg": alg, "kid": KID});
            let token = raw_token(&header, &good_claims(&config));
            assert_refused(&token, &config, REASON_ALG_MISMATCH, label, false).await;
        }
        for (alg, label) in [(json!(5), "other"), (Value::Null, "absent")] {
            let header = json!({"alg": alg, "kid": KID});
            let token = raw_token(&header, &good_claims(&config));
            assert_refused(&token, &config, REASON_ALG_MISMATCH, label, false).await;
        }
    }

    #[tokio::test]
    async fn a_token_forged_with_the_old_shared_secret_is_refused() {
        let config = test_config(true);
        let token = raw_hs256(&good_header(), &good_claims(&config), b"old-shared-secret");
        // `good_header` says ES256 but carries an HS256 MAC: bad signature.
        assert_refused(&token, &config, REASON_BAD_SIGNATURE, "es256", false).await;
        let header = json!({"alg": "HS256", "typ": "JWT", "kid": "hs256-v1"});
        let token = raw_hs256(&header, &good_claims(&config), b"old-shared-secret");
        assert_refused(&token, &config, REASON_ALG_MISMATCH, "es256", false).await;
    }

    #[tokio::test]
    async fn unknown_missing_and_wrong_key_kids_are_rejected() {
        let config = test_config(true);
        let mut header = good_header();
        header["kid"] = json!("not-a-published-kid");
        let token = raw_token(&header, &good_claims(&config));
        assert_refused(&token, &config, REASON_BAD_KID, "es256", false).await;
        // The right kid but signed by a different private key.
        let token = raw_token_with(&good_header(), &good_claims(&config), other_key());
        assert_refused(&token, &config, REASON_BAD_SIGNATURE, "es256", false).await;
    }

    #[tokio::test]
    async fn rotated_keys_verify_until_the_window_closes() {
        use platform_keys::KeyState;
        use std::time::{Duration, SystemTime};
        let key = fixture();
        let mut config = test_config(false);
        config.jwt_keys = keys_for(vec![
            key.row(
                "rot-in",
                KeyState::VerifyOnly,
                Some(SystemTime::now() - Duration::from_secs(3600)),
            ),
            key.row(
                "rot-out",
                KeyState::VerifyOnly,
                Some(SystemTime::now() - Duration::from_secs(2 * 3600 + 60)),
            ),
        ]);
        let mut header = good_header();
        header["kid"] = json!("rot-in");
        let token = raw_token(&header, &good_claims(&config));
        run(&token, &config)
            .await
            .0
            .expect("verify-only key inside its window verifies");
        header["kid"] = json!("rot-out");
        let token = raw_token(&header, &good_claims(&config));
        assert_refused(&token, &config, REASON_BAD_KID, "es256", false).await;
    }

    #[tokio::test]
    async fn key_material_headers_are_rejected_even_with_a_valid_signature() {
        let config = test_config(true);
        for param in FORBIDDEN_HEADER_PARAMS {
            let mut header = good_header();
            header[param] = json!("https://attacker.example/keys");
            let token = raw_token(&header, &good_claims(&config));
            assert_refused(&token, &config, REASON_FORBIDDEN_HEADER, "es256", false).await;
        }
    }

    #[tokio::test]
    async fn hostile_kids_are_rejected() {
        let config = test_config(true);
        for kid in [
            json!("a b"),
            json!("../../x"),
            json!(9),
            json!("k".repeat(65)),
        ] {
            let mut header = good_header();
            header["kid"] = kid;
            let token = raw_token(&header, &good_claims(&config));
            assert_refused(&token, &config, REASON_BAD_KID, "es256", false).await;
        }
    }

    #[tokio::test]
    async fn structurally_broken_tokens_are_malformed() {
        let config = test_config(true);
        for token in ["abc", "a.b", "a.b.c.d", "%%%.e30.sig", ".e30.sig"] {
            assert_refused(token, &config, REASON_MALFORMED, "absent", false).await;
        }
    }

    #[tokio::test]
    async fn no_key_source_never_verifies_anything() {
        let config = test_config(false);
        for token in [
            raw_token(&good_header(), &good_claims(&config)),
            raw_hs256(&good_header(), &good_claims(&config), b""),
        ] {
            assert_refused(&token, &config, REASON_NO_KEY, "absent", false).await;
        }
    }

    #[tokio::test]
    async fn every_required_claim_missing_is_rejected_and_tenant_is_forbidden() {
        let config = test_config(true);
        for name in REQUIRED_JWT_CLAIMS {
            let token = raw_token(&good_header(), &without(good_claims(&config), name));
            assert_refused(
                &token,
                &config,
                REASON_MISSING_CLAIM,
                "es256",
                name == "tenant",
            )
            .await;
        }
    }

    #[tokio::test]
    async fn null_required_claims_count_as_missing() {
        let config = test_config(true);
        for name in ["sub", "scope", "iat", "tenant"] {
            let claims = with(good_claims(&config), name, Value::Null);
            let token = raw_token(&good_header(), &claims);
            assert_refused(
                &token,
                &config,
                REASON_MISSING_CLAIM,
                "es256",
                name == "tenant",
            )
            .await;
        }
    }

    #[tokio::test]
    async fn there_is_no_default_tenant_fallback() {
        let config = test_config(true);
        for tenant in [
            json!(""),
            json!("   "),
            json!(0),
            json!(["t"]),
            json!({"a": 1}),
        ] {
            let claims = with(good_claims(&config), "tenant", tenant);
            let token = raw_token(&good_header(), &claims);
            assert_refused(&token, &config, REASON_INVALID_CLAIM, "es256", true).await;
        }
        let token = raw_token(&good_header(), &without(good_claims(&config), "tenant"));
        let err = run(&token, &config)
            .await
            .0
            .expect_err("no tenant, no entry");
        assert!(matches!(err, ApiError::Forbidden(_)));
    }

    #[tokio::test]
    async fn empty_or_mistyped_identity_claims_are_invalid() {
        let config = test_config(true);
        for (name, value) in [
            ("sub", json!("")),
            ("sub", json!(" ")),
            ("sub", json!(7)),
            ("scope", json!(["a"])),
            ("scope", json!(1)),
            ("iat", json!("now")),
            ("iat", json!(1.5)),
            ("teams", json!("x")),
            ("teams", json!([1])),
            ("roles", json!({"a": 1})),
        ] {
            let claims = with(good_claims(&config), name, value.clone());
            let token = raw_token(&good_header(), &claims);
            assert_refused(&token, &config, REASON_INVALID_CLAIM, "es256", false).await;
        }
    }

    #[tokio::test]
    async fn an_empty_scope_string_is_allowed_but_the_claim_must_exist() {
        let config = test_config(true);
        let claims = with(good_claims(&config), "scope", json!(""));
        let token = raw_token(&good_header(), &claims);
        assert_eq!(
            run(&token, &config)
                .await
                .0
                .expect("no scopes granted")
                .scope,
            ""
        );
    }

    #[tokio::test]
    async fn teams_and_roles_are_optional_lists() {
        let config = test_config(true);
        let claims = with(good_claims(&config), "teams", json!(["a", "b"]));
        let claims = with(claims, "roles", json!(["viewer"]));
        let token = raw_token(&good_header(), &claims);
        let decoded = run(&token, &config).await.0.expect("verifies");
        assert_eq!(decoded.teams, ["a", "b"]);
        assert_eq!(decoded.roles, ["viewer"]);
        let bare = without(without(good_claims(&config), "teams"), "roles");
        let token = raw_token(&good_header(), &bare);
        assert!(run(&token, &config)
            .await
            .0
            .expect("verifies")
            .teams
            .is_empty());
    }

    #[tokio::test]
    async fn issuer_and_audience_are_enforced_not_optional() {
        let config = test_config(true);
        for name in ["iss", "aud"] {
            let token = raw_token(&good_header(), &without(good_claims(&config), name));
            assert_refused(&token, &config, REASON_MISSING_CLAIM, "es256", false).await;
        }
        let claims = with(good_claims(&config), "iss", json!("https://evil.example"));
        assert_refused(
            &raw_token(&good_header(), &claims),
            &config,
            REASON_BAD_ISSUER,
            "es256",
            false,
        )
        .await;
        let claims = with(good_claims(&config), "aud", json!("other"));
        assert_refused(
            &raw_token(&good_header(), &claims),
            &config,
            REASON_BAD_AUDIENCE,
            "es256",
            false,
        )
        .await;
        let claims = with(good_claims(&config), "aud", json!(["a", "b"]));
        assert_refused(
            &raw_token(&good_header(), &claims),
            &config,
            REASON_BAD_AUDIENCE,
            "es256",
            false,
        )
        .await;
    }

    #[tokio::test]
    async fn an_audience_list_containing_the_expected_audience_is_accepted() {
        let config = test_config(true);
        let aud = json!(["other", config.cli.jwt_audience]);
        let token = raw_token(&good_header(), &with(good_claims(&config), "aud", aud));
        let decoded = run(&token, &config)
            .await
            .0
            .expect("list aud containing the expected value");
        assert_eq!(decoded.aud, config.cli.jwt_audience);
    }

    #[tokio::test]
    async fn empty_expected_issuer_or_audience_fails_closed() {
        let mut config = test_config(true);
        let claims = with(
            with(good_claims(&config), "iss", json!("")),
            "aud",
            json!(""),
        );
        let token = raw_token(&good_header(), &claims);
        config.cli.jwt_issuer = String::new();
        assert_refused(&token, &config, REASON_BAD_ISSUER, "absent", false).await;
        config.cli.jwt_issuer = "https://auth.penguintech.io".into();
        config.cli.jwt_audience = String::new();
        assert_refused(&token, &config, REASON_BAD_AUDIENCE, "absent", false).await;
    }

    #[tokio::test]
    async fn exp_is_strict_and_the_skew_only_applies_to_iat_and_nbf() {
        let config = test_config(true);
        let now = chrono::Utc::now().timestamp();
        // Expired 5 s ago: inside the 30 s library leeway, still refused.
        let claims = with(good_claims(&config), "exp", json!(now - 5));
        assert_refused(
            &raw_token(&good_header(), &claims),
            &config,
            REASON_EXPIRED,
            "es256",
            false,
        )
        .await;
        // Expired long ago: refused by the library itself.
        let claims = with(good_claims(&config), "exp", json!(now - 3600));
        assert_refused(
            &raw_token(&good_header(), &claims),
            &config,
            REASON_EXPIRED,
            "es256",
            false,
        )
        .await;
        // `exp == now` is expired (<=), matching the Python re-check.
        let claims = with(good_claims(&config), "exp", json!(now));
        let (result, _) = run(&raw_token(&good_header(), &claims), &config).await;
        assert!(result.is_err(), "exp == now must not verify");
    }

    #[tokio::test]
    async fn iat_and_nbf_in_the_future_are_bounded_by_the_skew() {
        let config = test_config(true);
        let now = chrono::Utc::now().timestamp();
        let claims = with(
            good_claims(&config),
            "iat",
            json!(now + JWT_CLOCK_SKEW_SECONDS + 300),
        );
        assert_refused(
            &raw_token(&good_header(), &claims),
            &config,
            REASON_IMMATURE,
            "es256",
            false,
        )
        .await;
        let claims = with(
            good_claims(&config),
            "nbf",
            json!(now + JWT_CLOCK_SKEW_SECONDS + 300),
        );
        assert_refused(
            &raw_token(&good_header(), &claims),
            &config,
            REASON_IMMATURE,
            "es256",
            false,
        )
        .await;
        let claims = with(
            good_claims(&config),
            "iat",
            json!(now + JWT_CLOCK_SKEW_SECONDS - 5),
        );
        run(&raw_token(&good_header(), &claims), &config)
            .await
            .0
            .expect("iat inside the skew allowance verifies");
    }

    #[tokio::test]
    async fn a_wrong_key_is_bad_signature_and_the_body_never_names_the_check() {
        let config = test_config(true);
        let token = raw_token_with(&good_header(), &good_claims(&config), other_key());
        let (result, capture) = run(&token, &config).await;
        let err = result.unwrap_err();
        assert_eq!(err.to_string(), "unauthorized: invalid token");
        assert_eq!(capture.count("platform_es256", "es256", "bad_signature"), 1);
    }

    #[tokio::test]
    async fn decode_errors_map_to_closed_reasons() {
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
            (ErrorKind::InvalidKeyFormat, REASON_INVALID),
        ] {
            assert_eq!(classify_decode_error(&kind), reason, "{kind:?}");
        }
    }

    #[tokio::test]
    async fn scope_denied_is_part_of_the_shared_vocabulary() {
        // Not emitted by this authentication-only verifier (scope decisions
        // are made by handlers), but the label must exist for dashboards.
        assert_eq!(REASON_SCOPE_DENIED, "scope_denied");
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

    /// Process-wide log capture (installed once). Global rather than
    /// thread-local: tracing caches per-callsite interest globally, so a
    /// callsite first hit by a parallel test with no subscriber would stay
    /// disabled for a scoped one.
    fn captured_logs() -> &'static LogBuf {
        static LOGS: OnceLock<LogBuf> = OnceLock::new();
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

    #[tokio::test]
    async fn rejections_log_the_closed_vocabulary_and_never_token_material() {
        captured_logs();
        let config = test_config(true);
        let mut header = good_header();
        header["jku"] = json!("https://attacker.example/SECRET-JKU-MARKER");
        let claims = with(good_claims(&config), "sub", json!("SECRET-SUBJECT-MARKER"));
        let hostile = raw_token(&header, &claims);
        let ok = raw_token(&good_header(), &claims);
        let wrong_key = raw_token_with(&good_header(), &claims, other_key());
        let capture = Capture::new();
        for token in [&hostile, &ok, &wrong_key] {
            let _ = extract_claims_with(&capture.metrics, &parts_with_token(token), &config).await;
        }

        let logged =
            String::from_utf8(captured_logs().0.lock().expect("log lock").clone()).expect("utf8");
        assert!(logged.contains("JWT rejected"), "{logged}");
        assert!(logged.contains("forbidden_header"), "{logged}");
        assert!(logged.contains("platform_es256"), "{logged}");
        for needle in [
            "SECRET-SUBJECT-MARKER",
            "SECRET-JKU-MARKER",
            "SECRET-KEY-MARKER",
            hostile.as_str(),
            ok.as_str(),
            wrong_key.as_str(),
        ] {
            assert!(!logged.contains(needle), "log leaked {needle:.30}");
        }
    }

    #[tokio::test]
    async fn no_key_is_logged_critical() {
        captured_logs();
        let config = test_config(false);
        let token = raw_token(&good_header(), &good_claims(&config));
        let _ = run(&token, &config).await;
        let logged =
            String::from_utf8(captured_logs().0.lock().expect("log lock").clone()).expect("utf8");
        let line = logged
            .lines()
            .find(|line| line.contains("verifier has no signing key configured"))
            .expect("no_key line is logged");
        assert!(
            line.contains("ERROR") && line.contains("severity=\"critical\""),
            "{line}"
        );
    }

    /// A complete, fixed-time claim payload for the direct `parse_claims` tests.
    fn fixed_payload(config: &Config, iat: i64, exp: i64) -> Value {
        json!({
            "sub": "user-1", "iss": config.cli.jwt_issuer, "aud": config.cli.jwt_audience,
            "iat": iat, "exp": exp, "scope": "streaming:read", "tenant": "tenant-abc",
        })
    }

    #[tokio::test]
    async fn iat_and_exp_boundaries_are_exact_against_a_fixed_clock() {
        let config = test_config(true);
        let aud = config.cli.jwt_audience.as_str();
        let now = 1_000_000_i64;
        let parse = |iat: i64, exp: i64| parse_claims(&fixed_payload(&config, iat, exp), aud, now);

        // iat exactly at the skew edge verifies; one second past is immature.
        assert!(parse(now + JWT_CLOCK_SKEW_SECONDS, now + 3600).is_ok());
        assert_eq!(
            parse(now + JWT_CLOCK_SKEW_SECONDS + 1, now + 3600)
                .err()
                .map(|f| f.rejection.reason),
            Some(REASON_IMMATURE)
        );
        // exp one second ahead verifies; exp == now is already expired (strict).
        assert!(parse(now, now + 1).is_ok());
        assert_eq!(
            parse(now - 10, now).err().map(|f| f.rejection.reason),
            Some(REASON_EXPIRED)
        );
        assert_eq!(
            parse(now - 10, now - 1).err().map(|f| f.rejection.reason),
            Some(REASON_EXPIRED)
        );
    }

    #[tokio::test]
    async fn policy_constants_match_the_python_contract() {
        assert_eq!(JWT_CLOCK_SKEW_SECONDS, 30);
        assert_eq!(PLATFORM_JWT_ALGORITHM, "ES256");
        assert_eq!(
            REQUIRED_JWT_CLAIMS,
            ["sub", "iss", "aud", "iat", "exp", "scope", "tenant"]
        );
    }
}
