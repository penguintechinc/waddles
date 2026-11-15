//! Machine-JWT (ES256) verification for the rooms gRPC listener.
//!
//! Every `RoomsService` call carries a hub-api-issued per-service ES256 JWT in
//! `authorization: Bearer ...` (on top of the mTLS handshake). The verifier is the
//! same shape as `flask_core.service_jwt.ServiceJwtVerifier`: the JOSE header is
//! vetted first (`alg` must be exactly `ES256`, `kid` required, no key-material
//! header params), then signature, `iss`, `aud`, `exp`/`nbf` (with skew) and the
//! required claims are checked, and finally the single `scope` claim must equal
//! the scope the RPC needs. Trust keys come from the environment
//! (`SERVICE_JWT_ACTIVE_KID` + `SERVICE_JWT_PUBLIC_KEY`, plus any
//! `SERVICE_JWT_PUBLIC_KEY_<kid>` rotation keys) -- public keys only.
//!
//! `exp`, `nbf` and `iat` are checked here against an injectable clock (default:
//! system time) with a fixed 30 s skew, so the boundary behaviour is testable without
//! sleeping; the required-claim set is [`REQUIRED_CLAIMS`], pinned equal to
//! `flask_core.service_jwt` by `tests/rooms_grpc.rs`.
//!
//! Failures are counted via the shared JWT hardening metrics
//! (`waddles_jwt_verifications_total{verifier="service_eddsa"}`) and logged with
//! closed-vocabulary reasons only, never token material.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Instant;

use jsonwebtoken::{decode, Algorithm, DecodingKey, Validation};
use serde_json::Value;

use crate::http::auth::hardening::{
    inspect_header, report_outcome, JwtMetrics, JwtRejection, KidPolicy, ALG_LABEL_ABSENT,
    OUTCOME_OK, REASON_BAD_AUDIENCE, REASON_BAD_ISSUER, REASON_BAD_SIGNATURE, REASON_EXPIRED,
    REASON_IMMATURE, REASON_INVALID, REASON_INVALID_CLAIM, REASON_MALFORMED, REASON_MISSING_CLAIM,
    REASON_SCOPE_DENIED, REASON_UNKNOWN_KID, VERIFIER_SERVICE_EDDSA,
};

/// The only accepted algorithm.
pub const SERVICE_JWT_ALGORITHM: &str = "ES256";
/// Scope for read RPCs (list/get/participants/hands) and the `grpc.health.v1` check.
pub const SCOPE_READ: &str = "streaming.rooms:read";
/// Scope for member-level write RPCs (create/delete/join/leave, raise/lower hand).
pub const SCOPE_WRITE: &str = "streaming.rooms:write";
/// The moderator BUNDLE: accepted wherever a per-action moderation scope is (kick, mute, lock,
/// hands), exactly the moderator's powers today -- never `update` or `create`/`close`.
pub const SCOPE_MODERATE: &str = "streaming.rooms:moderate";
/// Per-action scopes, each its own scope (a role is just a predefined bundle of these).
pub const SCOPE_UPDATE: &str = "streaming.rooms:update";
pub const SCOPE_KICK: &str = "streaming.rooms:kick";
pub const SCOPE_MUTE: &str = "streaming.rooms:mute";
pub const SCOPE_LOCK: &str = "streaming.rooms:lock";
pub const SCOPE_HANDS: &str = "streaming.rooms:hands";
/// Claims every service JWT must carry: the exact `require` list of
/// `flask_core.service_jwt.ServiceJwtVerifier.verify` (hub-api's issuer stamps all of them;
/// `tenant` and `community` are optional extras it adds per identity/grant).
pub const REQUIRED_CLAIMS: [&str; 8] = ["exp", "iat", "nbf", "iss", "aud", "sub", "scope", "jti"];
/// Clock skew tolerated on `exp`/`nbf`/`iat` (seconds), same as the Python verifier.
pub const CLOCK_SKEW_SECONDS: i64 = 30;
const DEFAULT_AUDIENCE: &str = "waddlebot-internal";
const DEFAULT_ISSUER: &str = "hub-api";
const PUBLIC_KEY_PREFIX: &str = "SERVICE_JWT_PUBLIC_KEY_";

/// Errors building the verifier from configuration.
#[derive(Debug, thiserror::Error)]
pub enum GrpcAuthError {
    #[error("rooms gRPC auth config error: {0}")]
    Config(String),
}

/// The verified claim subset handlers care about.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ServiceClaims {
    pub sub: String,
    pub scope: String,
    /// `tenant` claim when the issuer stamped one (hub-api does, per identity). Used only
    /// as a span attribute; never taken from a request body.
    pub tenant: Option<String>,
    /// Numeric `community` claim of a community-bound token. When present, every RPC must
    /// address exactly this community (anything else is `NOT_FOUND`).
    pub community: Option<u64>,
}

/// Source of "now" (unix seconds) for `exp`/`nbf`/`iat` checks.
type Clock = Arc<dyn Fn() -> i64 + Send + Sync>;

fn system_now() -> i64 {
    chrono::Utc::now().timestamp()
}

/// Verifies hub-api machine JWTs against a static public-key set.
pub struct ServiceJwtVerifier {
    keys: HashMap<String, DecodingKey>,
    audience: String,
    issuers: Vec<String>,
    /// When non-empty, `sub` must be one of these (the SPIFFE id of the calling service).
    allowed_subjects: Vec<String>,
    clock: Clock,
}

impl ServiceJwtVerifier {
    /// Builds a verifier from already-parsed keys (`kid` -> ES256 public key).
    pub fn new(
        keys: HashMap<String, DecodingKey>,
        audience: impl Into<String>,
        issuers: Vec<String>,
        allowed_subjects: Vec<String>,
    ) -> Self {
        Self {
            keys,
            audience: audience.into(),
            issuers,
            allowed_subjects,
            clock: Arc::new(system_now),
        }
    }

    /// Replaces the clock the time claims are checked against (tests drive the
    /// [`CLOCK_SKEW_SECONDS`] boundary with it; production keeps system time).
    pub fn with_clock(mut self, clock: impl Fn() -> i64 + Send + Sync + 'static) -> Self {
        self.clock = Arc::new(clock);
        self
    }

    /// Builds a key set from PEM (SPKI) strings keyed by `kid`.
    pub fn keys_from_pem<'a>(
        pems: impl IntoIterator<Item = (&'a str, &'a str)>,
    ) -> Result<HashMap<String, DecodingKey>, GrpcAuthError> {
        let mut keys = HashMap::new();
        for (kid, pem) in pems {
            let key = DecodingKey::from_ec_pem(pem.as_bytes()).map_err(|_| {
                GrpcAuthError::Config(format!("public key for kid {kid:?} is not an EC PEM key"))
            })?;
            keys.insert(kid.to_string(), key);
        }
        Ok(keys)
    }

    /// Reads the trust set from the environment: `SERVICE_JWT_ACTIVE_KID` +
    /// `SERVICE_JWT_PUBLIC_KEY` and every `SERVICE_JWT_PUBLIC_KEY_<kid>`;
    /// `SERVICE_JWT_AUDIENCE` / `SERVICE_JWT_ISSUER` (defaults `waddlebot-internal` /
    /// `hub-api`) and `ROOMS_GRPC_ALLOWED_SUBJECTS` (comma-separated SPIFFE ids).
    /// `Ok(None)` when no key is configured at all.
    pub fn from_env() -> Result<Option<Self>, GrpcAuthError> {
        let get = |k: &str| std::env::var(k).ok().filter(|v| !v.trim().is_empty());
        let mut pems: Vec<(String, String)> = Vec::new();
        if let (Some(kid), Some(pem)) =
            (get("SERVICE_JWT_ACTIVE_KID"), get("SERVICE_JWT_PUBLIC_KEY"))
        {
            pems.push((kid, pem));
        }
        for (name, value) in std::env::vars() {
            if let Some(kid) = name.strip_prefix(PUBLIC_KEY_PREFIX) {
                if !kid.is_empty() && !value.trim().is_empty() {
                    pems.push((kid.to_string(), value));
                }
            }
        }
        if pems.is_empty() {
            return Ok(None);
        }
        let keys = Self::keys_from_pem(pems.iter().map(|(k, v)| (k.as_str(), v.as_str())))?;
        let audience = get("SERVICE_JWT_AUDIENCE").unwrap_or_else(|| DEFAULT_AUDIENCE.into());
        let issuer = get("SERVICE_JWT_ISSUER").unwrap_or_else(|| DEFAULT_ISSUER.into());
        let subjects = get("ROOMS_GRPC_ALLOWED_SUBJECTS")
            .map(|v| {
                v.split(',')
                    .map(|s| s.trim().to_string())
                    .filter(|s| !s.is_empty())
                    .collect()
            })
            .unwrap_or_default();
        Ok(Some(Self::new(keys, audience, vec![issuer], subjects)))
    }

    /// Verifies `token` and requires its `scope` to equal `required_scope`.
    /// Emits the shared JWT verification metric/log for every call.
    pub fn verify(&self, token: &str, required_scope: &str) -> Result<ServiceClaims, JwtRejection> {
        self.verify_any(token, &[required_scope])
    }

    /// Like [`Self::verify`] but accepts any ONE of `allowed_scopes` (the token still carries a
    /// single `scope`; the caller reads it from the returned claims to tell which one). Used by
    /// RPCs whose reach depends on the scope presented, e.g. `ListHands`.
    pub fn verify_any(
        &self,
        token: &str,
        allowed_scopes: &[&str],
    ) -> Result<ServiceClaims, JwtRejection> {
        let started = Instant::now();
        let mut alg = ALG_LABEL_ABSENT;
        let result = self.verify_checked(token, allowed_scopes, &mut alg);
        let outcome = match &result {
            Ok(_) => OUTCOME_OK,
            Err(r) => r.reason,
        };
        report_outcome(
            JwtMetrics::global(),
            VERIFIER_SERVICE_EDDSA,
            started,
            alg,
            outcome,
        );
        result
    }

    fn verify_checked(
        &self,
        token: &str,
        allowed_scopes: &[&str],
        alg: &mut &'static str,
    ) -> Result<ServiceClaims, JwtRejection> {
        let header = inspect_header(token, &[SERVICE_JWT_ALGORITHM], KidPolicy::Require)
            .inspect_err(|r| *alg = r.alg)?;
        *alg = "es256";
        let kid = header.kid.unwrap_or_default();
        let key = self
            .keys
            .get(&kid)
            .ok_or(JwtRejection::new(REASON_UNKNOWN_KID, "es256"))?;

        let mut validation = Validation::new(Algorithm::ES256);
        validation.set_issuer(&self.issuers);
        validation.set_audience(std::slice::from_ref(&self.audience));
        validation.leeway = 0;
        validation.set_required_spec_claims(&["exp", "nbf", "iss", "aud", "sub"]);
        // exp/nbf/iat are checked below against the injectable clock, not system time.
        validation.validate_exp = false;
        validation.validate_nbf = false;
        let data = decode::<Value>(token, key, &validation)
            .map_err(|e| JwtRejection::new(classify(e.kind()), "es256"))?;

        let claims = data
            .claims
            .as_object()
            .ok_or(JwtRejection::new(REASON_INVALID_CLAIM, "es256"))?;
        for name in REQUIRED_CLAIMS {
            if !claims.contains_key(name) {
                return Err(JwtRejection::new(REASON_MISSING_CLAIM, "es256"));
            }
        }
        let text = |name: &str| -> Result<String, JwtRejection> {
            match claims.get(name) {
                Some(Value::String(s)) if !s.is_empty() => Ok(s.clone()),
                _ => Err(JwtRejection::new(REASON_INVALID_CLAIM, "es256")),
            }
        };
        let time = |name: &str| -> Result<i64, JwtRejection> {
            claims
                .get(name)
                .and_then(Value::as_i64)
                .ok_or(JwtRejection::new(REASON_INVALID_CLAIM, "es256"))
        };
        let sub = text("sub")?;
        let scope = text("scope")?;
        text("jti")?;
        let (exp, nbf, iat) = (time("exp")?, time("nbf")?, time("iat")?);
        let now = (self.clock)();
        if now > exp.saturating_add(CLOCK_SKEW_SECONDS) {
            return Err(JwtRejection::new(REASON_EXPIRED, "es256"));
        }
        if nbf > now.saturating_add(CLOCK_SKEW_SECONDS)
            || iat > now.saturating_add(CLOCK_SKEW_SECONDS)
        {
            return Err(JwtRejection::new(REASON_IMMATURE, "es256"));
        }
        if !self.allowed_subjects.is_empty() && !self.allowed_subjects.contains(&sub) {
            return Err(JwtRejection::new(REASON_SCOPE_DENIED, "es256"));
        }
        if !allowed_scopes.contains(&scope.as_str()) {
            return Err(JwtRejection::new(REASON_SCOPE_DENIED, "es256"));
        }
        let community = match claims.get("community") {
            None => None,
            Some(v) => Some(
                v.as_u64()
                    .ok_or(JwtRejection::new(REASON_INVALID_CLAIM, "es256"))?,
            ),
        };
        let tenant = match claims.get("tenant") {
            None => None,
            Some(Value::String(s)) if !s.is_empty() => Some(s.clone()),
            Some(Value::Number(n)) => Some(n.to_string()),
            Some(_) => return Err(JwtRejection::new(REASON_INVALID_CLAIM, "es256")),
        };
        Ok(ServiceClaims {
            sub,
            scope,
            tenant,
            community,
        })
    }
}

/// Maps a `jsonwebtoken` failure to a closed `REASON_*` -- never its text.
fn classify(kind: &jsonwebtoken::errors::ErrorKind) -> &'static str {
    use jsonwebtoken::errors::ErrorKind;
    match kind {
        ErrorKind::ExpiredSignature => REASON_EXPIRED,
        ErrorKind::ImmatureSignature => REASON_IMMATURE,
        ErrorKind::InvalidSignature => REASON_BAD_SIGNATURE,
        ErrorKind::InvalidIssuer => REASON_BAD_ISSUER,
        ErrorKind::InvalidAudience => REASON_BAD_AUDIENCE,
        ErrorKind::MissingRequiredClaim(_) => REASON_MISSING_CLAIM,
        ErrorKind::InvalidClaimFormat(_) | ErrorKind::Json(_) => REASON_INVALID_CLAIM,
        ErrorKind::InvalidToken | ErrorKind::Base64(_) | ErrorKind::Utf8(_) => REASON_MALFORMED,
        _ => REASON_INVALID,
    }
}
