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
//! (`docs/JWT_VERIFICATION.md`, verifier label `platform_hs256`): the header
//! `alg` must be exactly `HS256` (`alg: none`, any other algorithm and the
//! key-material headers `jku`/`jwk`/`x5u`/`x5c`/`crit` are refused before any
//! signature work, see [`hardening`]); `iss` and `aud` are ENFORCED; every of
//! `sub iss aud iat exp scope tenant` must be present, `sub`/`tenant`
//! non-empty; `exp` is strict (the 30 s skew applies to `iat`/`nbf` only);
//! there is no default-tenant fallback; an empty or unset HMAC secret is
//! refused (an empty key would otherwise verify tokens anyone can forge).
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
use jsonwebtoken::errors::ErrorKind;
use jsonwebtoken::{decode, Algorithm, DecodingKey, Validation};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use self::hardening::{
    inspect_header, report_outcome, JwtMetrics, JwtRejection, KidPolicy, ALG_LABEL_ABSENT,
    OUTCOME_OK, REASON_ALG_MISMATCH, REASON_BAD_AUDIENCE, REASON_BAD_ISSUER, REASON_BAD_SIGNATURE,
    REASON_EXPIRED, REASON_IMMATURE, REASON_INVALID, REASON_INVALID_CLAIM, REASON_MALFORMED,
    REASON_MISSING_CLAIM, REASON_NO_KEY, VERIFIER_PLATFORM_HS256,
};
use crate::config::Config;
use crate::error::ApiError;
use crate::http::AppState;

/// The ONE algorithm this verifier accepts (RFC 8725 one-alg-per-verifier);
/// mirrors `flask_core.auth.PLATFORM_JWT_ALGORITHM`.
pub const PLATFORM_JWT_ALGORITHM: &str = "HS256";

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
        let claims = extract_claims(parts, &state.config)?;
        Ok(AuthenticatedClaims(claims))
    }
}

fn extract_claims(parts: &Parts, config: &Config) -> Result<Claims, ApiError> {
    extract_claims_with(JwtMetrics::global(), parts, config)
}

/// [`extract_claims`] against an explicit [`JwtMetrics`] (tests inject an
/// in-memory meter provider; production goes through the global one). A
/// missing / non-Bearer header carries no token, so it is not a
/// verification and is not counted.
fn extract_claims_with(
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
    verify_with_metrics(metrics, token, config)
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
/// sample (`verifier=platform_hs256`) and, for a rejection, one PII-free log
/// line.
fn verify_with_metrics(
    metrics: &JwtMetrics,
    token: &str,
    config: &Config,
) -> Result<Claims, ApiError> {
    let started = Instant::now();
    let mut alg = ALG_LABEL_ABSENT;
    let result = verify_token(token, config, &mut alg, now_unix());
    let outcome = match &result {
        Ok(_) => OUTCOME_OK,
        Err(failure) => failure.rejection.reason,
    };
    report_outcome(metrics, VERIFIER_PLATFORM_HS256, started, alg, outcome);
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
fn verify_token(
    token: &str,
    config: &Config,
    alg: &mut &'static str,
    now: i64,
) -> Result<Claims, Failure> {
    // Deployment bug (unset/unresolved secret), not attacker input -- but an
    // empty HMAC key would otherwise *verify* tokens anyone can forge.
    let Some(secret) = config
        .jwt_hmac_secret
        .as_ref()
        .filter(|secret| !secret.expose().is_empty())
    else {
        return Err(Failure::reason(REASON_NO_KEY, ALG_LABEL_ABSENT));
    };
    // Nothing to compare `iss`/`aud` against -> nothing may pass.
    if config.cli.jwt_issuer.is_empty() {
        return Err(Failure::reason(REASON_BAD_ISSUER, ALG_LABEL_ABSENT));
    }
    if config.cli.jwt_audience.is_empty() {
        return Err(Failure::reason(REASON_BAD_AUDIENCE, ALG_LABEL_ABSENT));
    }

    inspect_header(token, &[PLATFORM_JWT_ALGORITHM], KidPolicy::Vet).map_err(|r| {
        *alg = r.alg;
        Failure::new(r)
    })?;
    *alg = "hs256";

    let mut validation = Validation::new(Algorithm::HS256);
    validation.set_issuer(std::slice::from_ref(&config.cli.jwt_issuer));
    validation.set_audience(std::slice::from_ref(&config.cli.jwt_audience));
    validation.leeway = JWT_CLOCK_SKEW_SECONDS.unsigned_abs();
    // `iss`/`aud` are ENFORCED (MED-5): the library only compares them when
    // present, so presence is demanded explicitly. `sub` is deliberately left
    // to `parse_claims`, which tells a missing `sub` from a mistyped one (the
    // library would call both "missing").
    validation.set_required_spec_claims(&["exp", "iss", "aud"]);
    validation.validate_nbf = true;

    let key = DecodingKey::from_secret(secret.expose().as_bytes());
    let data = decode::<Value>(token, &key, &validation)
        .map_err(|e| Failure::reason(classify_decode_error(e.kind()), "hs256"))?;
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
        return Err(Failure::reason(REASON_INVALID_CLAIM, "hs256"));
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
        rejection: JwtRejection::new(reason, "hs256"),
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
    let claims = extract_claims(&parts, &state.config)?;
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
    use axum::http::HeaderValue;
    use base64::engine::general_purpose::URL_SAFE_NO_PAD;
    use base64::Engine as _;
    use clap::Parser;
    use jsonwebtoken::{encode, EncodingKey, Header};
    use serde_json::json;
    use std::sync::{Arc, Mutex, OnceLock};

    const SECRET: &str = "secret";

    fn test_config(hmac_secret: Option<&str>) -> Config {
        let cli = CliConfig::parse_from(["svc-streaming"]);
        Config {
            cli,
            db_password: Secret::new("db-pass"),
            cache_password: None,
            service_api_key: Secret::new("service-key-value"),
            jwt_hmac_secret: hmac_secret.map(Secret::new),
        }
    }

    fn sign(claims: &Claims, secret: &str) -> String {
        encode(
            &Header::new(Algorithm::HS256),
            claims,
            &EncodingKey::from_secret(secret.as_bytes()),
        )
        .unwrap()
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

    #[test]
    fn missing_header_is_unauthorized() {
        let config = test_config(Some("secret"));
        let parts = parts_with_auth(None);
        let err = extract_claims(&parts, &config).unwrap_err();
        assert!(matches!(err, ApiError::Unauthorized(_)));
    }

    #[test]
    fn non_bearer_header_is_unauthorized() {
        let config = test_config(Some("secret"));
        let parts = parts_with_auth(Some("Basic abc123"));
        let err = extract_claims(&parts, &config).unwrap_err();
        assert!(matches!(err, ApiError::Unauthorized(_)));
    }

    #[test]
    fn valid_token_decodes_claims() {
        let config = test_config(Some("secret"));
        let claims = valid_claims(&config);
        let token = sign(&claims, "secret");
        let parts = parts_with_auth(Some(&format!("Bearer {token}")));
        let decoded = extract_claims(&parts, &config).expect("token should validate");
        assert_eq!(decoded.tenant, "tenant-abc");
        assert_eq!(decoded.sub, "user-123");
    }

    #[test]
    fn wrong_signing_secret_is_rejected() {
        let config = test_config(Some("secret"));
        let claims = valid_claims(&config);
        let token = sign(&claims, "wrong-secret");
        let parts = parts_with_auth(Some(&format!("Bearer {token}")));
        let err = extract_claims(&parts, &config).unwrap_err();
        assert!(matches!(err, ApiError::Unauthorized(_)));
    }

    #[test]
    fn expired_token_is_rejected() {
        let config = test_config(Some("secret"));
        let mut claims = valid_claims(&config);
        claims.iat -= 7200;
        claims.exp -= 7200;
        let token = sign(&claims, "secret");
        let parts = parts_with_auth(Some(&format!("Bearer {token}")));
        let err = extract_claims(&parts, &config).unwrap_err();
        assert!(matches!(err, ApiError::Unauthorized(_)));
    }

    #[test]
    fn wrong_audience_is_rejected() {
        let config = test_config(Some("secret"));
        let mut claims = valid_claims(&config);
        claims.aud = "someone-else".into();
        let token = sign(&claims, "secret");
        let parts = parts_with_auth(Some(&format!("Bearer {token}")));
        let err = extract_claims(&parts, &config).unwrap_err();
        assert!(matches!(err, ApiError::Unauthorized(_)));
    }

    #[test]
    fn missing_tenant_claim_is_forbidden() {
        let config = test_config(Some("secret"));
        let mut claims = valid_claims(&config);
        claims.tenant = String::new();
        let token = sign(&claims, "secret");
        let parts = parts_with_auth(Some(&format!("Bearer {token}")));
        let err = extract_claims(&parts, &config).unwrap_err();
        assert!(matches!(err, ApiError::Forbidden(_)));
    }

    #[test]
    fn no_verification_key_configured_is_unauthorized() {
        let config = test_config(None);
        let claims = valid_claims(&config);
        let token = sign(&claims, "secret");
        let parts = parts_with_auth(Some(&format!("Bearer {token}")));
        let err = extract_claims(&parts, &config).unwrap_err();
        assert!(matches!(err, ApiError::Unauthorized(_)));
    }

    #[test]
    fn service_key_header_matches_is_accepted() {
        let config = test_config(None);
        assert_eq!(config.service_api_key.expose(), "service-key-value");
    }

    // ---- Phase-0 hardening (RFC 8725) --------------------------------------

    fn b64(value: &Value) -> String {
        URL_SAFE_NO_PAD.encode(value.to_string())
    }

    /// Sign arbitrary header + claims JSON with `secret` (HS256 MAC), so a
    /// test controls exactly which property of an otherwise validly-signed
    /// token is hostile.
    fn raw_token(header: &Value, claims: &Value, secret: &str) -> String {
        let message = format!("{}.{}", b64(header), b64(claims));
        let signature = jsonwebtoken::crypto::sign(
            message.as_bytes(),
            &EncodingKey::from_secret(secret.as_bytes()),
            Algorithm::HS256,
        )
        .expect("sign");
        format!("{message}.{signature}")
    }

    fn good_header() -> Value {
        json!({"alg": "HS256", "typ": "JWT", "kid": "hs256-v1"})
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
    fn run(token: &str, config: &Config) -> (Result<Claims, ApiError>, Capture) {
        let capture = Capture::new();
        let result = extract_claims_with(&capture.metrics, &parts_with_token(token), config);
        (result, capture)
    }

    /// Assert `token` is refused with `status` semantics and counted exactly
    /// once as `platform_hs256` under (`alg`, `outcome`).
    fn assert_refused(token: &str, config: &Config, outcome: &str, alg: &str, forbidden: bool) {
        let (result, capture) = run(token, config);
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
            capture.count("platform_hs256", alg, outcome),
            1,
            "{outcome}/{alg}"
        );
        assert_eq!(capture.total(), 1, "exactly one verification recorded");
    }

    #[test]
    fn a_valid_token_is_counted_ok_with_the_hs256_label() {
        let config = test_config(Some(SECRET));
        let token = raw_token(&good_header(), &good_claims(&config), SECRET);
        let (result, capture) = run(&token, &config);
        let claims = result.expect("valid token verifies");
        assert_eq!(claims.tenant, "tenant-abc");
        assert_eq!(claims.aud, config.cli.jwt_audience);
        assert_eq!(capture.count("platform_hs256", "hs256", "ok"), 1);
        assert_eq!(capture.total(), 1);
        let latency = capture.points("waddles_jwt_verification_seconds");
        assert_eq!(latency.len(), 1);
        assert_eq!(latency[0].value, 1);
    }

    #[test]
    fn tokens_without_a_kid_or_nbf_still_verify() {
        // Tokens minted before `kid` stamping live up to 24 h; nbf is optional.
        let config = test_config(Some(SECRET));
        let header = json!({"alg": "HS256", "typ": "JWT"});
        let token = raw_token(&header, &good_claims(&config), SECRET);
        run(&token, &config).0.expect("kid-less token verifies");
    }

    #[test]
    fn alg_none_is_rejected_in_every_letter_case() {
        let config = test_config(Some(SECRET));
        for alg in ["none", "None", "NONE", "nOnE"] {
            let header = json!({"alg": alg, "typ": "JWT"});
            let token = format!("{}.{}.", b64(&header), b64(&good_claims(&config)));
            assert_refused(&token, &config, REASON_ALG_NONE, "none", false);
        }
    }

    #[test]
    fn alg_none_with_a_signature_attached_is_still_rejected() {
        let config = test_config(Some(SECRET));
        let token = raw_token(&json!({"alg": "none"}), &good_claims(&config), SECRET);
        assert_refused(&token, &config, REASON_ALG_NONE, "none", false);
    }

    #[test]
    fn alg_confusion_other_algorithms_are_rejected_even_with_the_right_secret() {
        let config = test_config(Some(SECRET));
        // HS384/HS512 MACs keyed with the CORRECT secret: only the pinned
        // single algorithm may verify.
        for (alg, label) in [(Algorithm::HS384, "hs384"), (Algorithm::HS512, "hs512")] {
            let token = encode(
                &Header::new(alg),
                &good_claims(&config),
                &EncodingKey::from_secret(SECRET.as_bytes()),
            )
            .expect("encode");
            assert_refused(&token, &config, REASON_ALG_MISMATCH, label, false);
        }
        for (alg, label) in [("RS256", "rs256"), ("ES256", "es256"), ("EdDSA", "eddsa")] {
            let token = raw_token(&json!({"alg": alg}), &good_claims(&config), SECRET);
            assert_refused(&token, &config, REASON_ALG_MISMATCH, label, false);
        }
        for (alg, label) in [(json!(5), "other"), (Value::Null, "absent")] {
            let token = raw_token(&json!({"alg": alg}), &good_claims(&config), SECRET);
            assert_refused(&token, &config, REASON_ALG_MISMATCH, label, false);
        }
    }

    #[test]
    fn key_material_headers_are_rejected_even_with_a_valid_signature() {
        let config = test_config(Some(SECRET));
        for param in FORBIDDEN_HEADER_PARAMS {
            let mut header = good_header();
            header[param] = json!("https://attacker.example/keys");
            let token = raw_token(&header, &good_claims(&config), SECRET);
            assert_refused(&token, &config, REASON_FORBIDDEN_HEADER, "hs256", false);
        }
    }

    #[test]
    fn hostile_kids_are_rejected() {
        let config = test_config(Some(SECRET));
        for kid in [
            json!("a b"),
            json!("../../x"),
            json!(9),
            json!("k".repeat(65)),
        ] {
            let mut header = good_header();
            header["kid"] = kid;
            let token = raw_token(&header, &good_claims(&config), SECRET);
            assert_refused(&token, &config, REASON_BAD_KID, "hs256", false);
        }
    }

    #[test]
    fn structurally_broken_tokens_are_malformed() {
        let config = test_config(Some(SECRET));
        for token in ["abc", "a.b", "a.b.c.d", "%%%.e30.sig", ".e30.sig"] {
            assert_refused(token, &config, REASON_MALFORMED, "absent", false);
        }
    }

    #[test]
    fn an_empty_or_unset_secret_never_verifies_anything() {
        // The forged token an empty HMAC key would otherwise accept.
        let unset = test_config(None);
        let empty = test_config(Some(""));
        for config in [&unset, &empty] {
            let forged = raw_token(&good_header(), &good_claims(config), "");
            assert_refused(&forged, config, REASON_NO_KEY, "absent", false);
            let signed = raw_token(&good_header(), &good_claims(config), SECRET);
            assert_refused(&signed, config, REASON_NO_KEY, "absent", false);
        }
    }

    #[test]
    fn every_required_claim_missing_is_rejected_and_tenant_is_forbidden() {
        let config = test_config(Some(SECRET));
        for name in REQUIRED_JWT_CLAIMS {
            let token = raw_token(&good_header(), &without(good_claims(&config), name), SECRET);
            assert_refused(
                &token,
                &config,
                REASON_MISSING_CLAIM,
                "hs256",
                name == "tenant",
            );
        }
    }

    #[test]
    fn null_required_claims_count_as_missing() {
        let config = test_config(Some(SECRET));
        for name in ["sub", "scope", "iat", "tenant"] {
            let claims = with(good_claims(&config), name, Value::Null);
            let token = raw_token(&good_header(), &claims, SECRET);
            assert_refused(
                &token,
                &config,
                REASON_MISSING_CLAIM,
                "hs256",
                name == "tenant",
            );
        }
    }

    #[test]
    fn there_is_no_default_tenant_fallback() {
        let config = test_config(Some(SECRET));
        for tenant in [
            json!(""),
            json!("   "),
            json!(0),
            json!(["t"]),
            json!({"a": 1}),
        ] {
            let claims = with(good_claims(&config), "tenant", tenant);
            let token = raw_token(&good_header(), &claims, SECRET);
            assert_refused(&token, &config, REASON_INVALID_CLAIM, "hs256", true);
        }
        let token = raw_token(
            &good_header(),
            &without(good_claims(&config), "tenant"),
            SECRET,
        );
        let err = run(&token, &config).0.expect_err("no tenant, no entry");
        assert!(matches!(err, ApiError::Forbidden(_)));
    }

    #[test]
    fn empty_or_mistyped_identity_claims_are_invalid() {
        let config = test_config(Some(SECRET));
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
            let token = raw_token(&good_header(), &claims, SECRET);
            assert_refused(&token, &config, REASON_INVALID_CLAIM, "hs256", false);
        }
    }

    #[test]
    fn an_empty_scope_string_is_allowed_but_the_claim_must_exist() {
        let config = test_config(Some(SECRET));
        let claims = with(good_claims(&config), "scope", json!(""));
        let token = raw_token(&good_header(), &claims, SECRET);
        assert_eq!(run(&token, &config).0.expect("no scopes granted").scope, "");
    }

    #[test]
    fn teams_and_roles_are_optional_lists() {
        let config = test_config(Some(SECRET));
        let claims = with(good_claims(&config), "teams", json!(["a", "b"]));
        let claims = with(claims, "roles", json!(["viewer"]));
        let token = raw_token(&good_header(), &claims, SECRET);
        let decoded = run(&token, &config).0.expect("verifies");
        assert_eq!(decoded.teams, ["a", "b"]);
        assert_eq!(decoded.roles, ["viewer"]);
        let bare = without(without(good_claims(&config), "teams"), "roles");
        let token = raw_token(&good_header(), &bare, SECRET);
        assert!(run(&token, &config).0.expect("verifies").teams.is_empty());
    }

    #[test]
    fn issuer_and_audience_are_enforced_not_optional() {
        let config = test_config(Some(SECRET));
        for name in ["iss", "aud"] {
            let token = raw_token(&good_header(), &without(good_claims(&config), name), SECRET);
            assert_refused(&token, &config, REASON_MISSING_CLAIM, "hs256", false);
        }
        let claims = with(good_claims(&config), "iss", json!("https://evil.example"));
        assert_refused(
            &raw_token(&good_header(), &claims, SECRET),
            &config,
            REASON_BAD_ISSUER,
            "hs256",
            false,
        );
        let claims = with(good_claims(&config), "aud", json!("other"));
        assert_refused(
            &raw_token(&good_header(), &claims, SECRET),
            &config,
            REASON_BAD_AUDIENCE,
            "hs256",
            false,
        );
        let claims = with(good_claims(&config), "aud", json!(["a", "b"]));
        assert_refused(
            &raw_token(&good_header(), &claims, SECRET),
            &config,
            REASON_BAD_AUDIENCE,
            "hs256",
            false,
        );
    }

    #[test]
    fn an_audience_list_containing_the_expected_audience_is_accepted() {
        let config = test_config(Some(SECRET));
        let aud = json!(["other", config.cli.jwt_audience]);
        let token = raw_token(
            &good_header(),
            &with(good_claims(&config), "aud", aud),
            SECRET,
        );
        let decoded = run(&token, &config)
            .0
            .expect("list aud containing the expected value");
        assert_eq!(decoded.aud, config.cli.jwt_audience);
    }

    #[test]
    fn empty_expected_issuer_or_audience_fails_closed() {
        let mut config = test_config(Some(SECRET));
        let claims = with(
            with(good_claims(&config), "iss", json!("")),
            "aud",
            json!(""),
        );
        let token = raw_token(&good_header(), &claims, SECRET);
        config.cli.jwt_issuer = String::new();
        assert_refused(&token, &config, REASON_BAD_ISSUER, "absent", false);
        config.cli.jwt_issuer = "https://auth.penguintech.io".into();
        config.cli.jwt_audience = String::new();
        assert_refused(&token, &config, REASON_BAD_AUDIENCE, "absent", false);
    }

    #[test]
    fn exp_is_strict_and_the_skew_only_applies_to_iat_and_nbf() {
        let config = test_config(Some(SECRET));
        let now = chrono::Utc::now().timestamp();
        // Expired 5 s ago: inside the 30 s library leeway, still refused.
        let claims = with(good_claims(&config), "exp", json!(now - 5));
        assert_refused(
            &raw_token(&good_header(), &claims, SECRET),
            &config,
            REASON_EXPIRED,
            "hs256",
            false,
        );
        // Expired long ago: refused by the library itself.
        let claims = with(good_claims(&config), "exp", json!(now - 3600));
        assert_refused(
            &raw_token(&good_header(), &claims, SECRET),
            &config,
            REASON_EXPIRED,
            "hs256",
            false,
        );
        // `exp == now` is expired (<=), matching the Python re-check.
        let claims = with(good_claims(&config), "exp", json!(now));
        let (result, _) = run(&raw_token(&good_header(), &claims, SECRET), &config);
        assert!(result.is_err(), "exp == now must not verify");
    }

    #[test]
    fn iat_and_nbf_in_the_future_are_bounded_by_the_skew() {
        let config = test_config(Some(SECRET));
        let now = chrono::Utc::now().timestamp();
        let claims = with(
            good_claims(&config),
            "iat",
            json!(now + JWT_CLOCK_SKEW_SECONDS + 300),
        );
        assert_refused(
            &raw_token(&good_header(), &claims, SECRET),
            &config,
            REASON_IMMATURE,
            "hs256",
            false,
        );
        let claims = with(
            good_claims(&config),
            "nbf",
            json!(now + JWT_CLOCK_SKEW_SECONDS + 300),
        );
        assert_refused(
            &raw_token(&good_header(), &claims, SECRET),
            &config,
            REASON_IMMATURE,
            "hs256",
            false,
        );
        let claims = with(
            good_claims(&config),
            "iat",
            json!(now + JWT_CLOCK_SKEW_SECONDS - 5),
        );
        run(&raw_token(&good_header(), &claims, SECRET), &config)
            .0
            .expect("iat inside the skew allowance verifies");
    }

    #[test]
    fn a_wrong_secret_is_bad_signature_and_the_body_never_names_the_check() {
        let config = test_config(Some(SECRET));
        let token = raw_token(&good_header(), &good_claims(&config), "another-secret");
        let (result, capture) = run(&token, &config);
        let err = result.unwrap_err();
        assert_eq!(err.to_string(), "unauthorized: invalid token");
        assert_eq!(capture.count("platform_hs256", "hs256", "bad_signature"), 1);
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
            (ErrorKind::InvalidKeyFormat, REASON_INVALID),
        ] {
            assert_eq!(classify_decode_error(&kind), reason, "{kind:?}");
        }
    }

    #[test]
    fn scope_denied_is_part_of_the_shared_vocabulary() {
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

    #[test]
    fn rejections_log_the_closed_vocabulary_and_never_token_material() {
        captured_logs();
        let config = test_config(Some(SECRET));
        let mut header = good_header();
        header["jku"] = json!("https://attacker.example/SECRET-JKU-MARKER");
        let claims = with(good_claims(&config), "sub", json!("SECRET-SUBJECT-MARKER"));
        let hostile = raw_token(&header, &claims, SECRET);
        let ok = raw_token(&good_header(), &claims, SECRET);
        let wrong_key = raw_token(&good_header(), &claims, "SECRET-KEY-MARKER");
        let capture = Capture::new();
        for token in [&hostile, &ok, &wrong_key] {
            let _ = extract_claims_with(&capture.metrics, &parts_with_token(token), &config);
        }

        let logged =
            String::from_utf8(captured_logs().0.lock().expect("log lock").clone()).expect("utf8");
        assert!(logged.contains("JWT rejected"), "{logged}");
        assert!(logged.contains("forbidden_header"), "{logged}");
        assert!(logged.contains("platform_hs256"), "{logged}");
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

    #[test]
    fn no_key_is_logged_critical() {
        captured_logs();
        let config = test_config(None);
        let token = raw_token(&good_header(), &good_claims(&config), SECRET);
        let _ = run(&token, &config);
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
}
