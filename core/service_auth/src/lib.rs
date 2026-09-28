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

use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use jsonwebtoken::{Algorithm, DecodingKey, Validation};
use serde::Deserialize;
use tokio::sync::RwLock;
use tracing::{debug, warn};

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

/// Verify a machine JWT's signature, `iss`, `aud`, `exp` (with clock skew)
/// and `scope` against `trust_bundle`. Returns [`ServiceAuthError::UnknownKeyId`]
/// for an unrecognized `kid` and [`ServiceAuthError::InvalidToken`] for
/// every other validation failure -- callers should treat both
/// identically (401/403), never surface which check failed.
pub async fn verify(
    token: &str,
    trust_bundle: &dyn TrustBundle,
    expected_audience: &str,
    trusted_issuers: &[&str],
    required_scope: &str,
) -> Result<ServiceClaims, ServiceAuthError> {
    let header = jsonwebtoken::decode_header(token)
        .map_err(|e| ServiceAuthError::InvalidToken(format!("malformed header: {e}")))?;
    let kid = header.kid.clone();
    let Some(kid_value) = kid.as_deref() else {
        return Err(ServiceAuthError::UnknownKeyId(None));
    };
    let Some(key) = trust_bundle.public_key(kid_value).await else {
        return Err(ServiceAuthError::UnknownKeyId(kid));
    };

    let mut validation = Validation::new(Algorithm::EdDSA);
    validation.set_audience(&[expected_audience]);
    validation.set_issuer(trusted_issuers);
    validation.leeway = CLOCK_SKEW_SECONDS;
    validation.set_required_spec_claims(&["exp", "iat", "iss", "aud", "sub"]);

    let data = jsonwebtoken::decode::<ServiceClaims>(token, &key, &validation)
        .map_err(|e| ServiceAuthError::InvalidToken(e.to_string()))?;

    if data.claims.scope != required_scope {
        return Err(ServiceAuthError::InvalidToken(format!(
            "scope {:?} != required {required_scope:?}",
            data.claims.scope
        )));
    }
    Ok(data.claims)
}

/// [`TrustBundle`] backed by hub-api's own JWKS endpoint, refreshed on a
/// cache miss (new `kid` seen, e.g. mid-rotation).
pub struct JwksTrustBundle {
    jwks_url: String,
    http: reqwest::Client,
    cache: RwLock<HashMap<String, DecodingKey>>,
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
    pub fn new(jwks_url: impl Into<String>) -> Self {
        Self {
            jwks_url: jwks_url.into(),
            http: reqwest::Client::new(),
            cache: RwLock::new(HashMap::new()),
        }
    }

    async fn refresh(&self) -> Result<(), ServiceAuthError> {
        let jwks: Jwks = self.http.get(&self.jwks_url).send().await?.json().await?;
        let mut cache = self.cache.write().await;
        cache.clear();
        for entry in jwks.keys {
            let key = DecodingKey::from_ed_components(&entry.x)
                .map_err(|e| ServiceAuthError::InvalidToken(format!("bad JWKS entry {}: {e}", entry.kid)))?;
            cache.insert(entry.kid, key);
        }
        debug!(count = cache.len(), "service_auth.jwks_refreshed");
        Ok(())
    }
}

#[async_trait::async_trait]
impl TrustBundle for JwksTrustBundle {
    async fn public_key(&self, kid: &str) -> Option<DecodingKey> {
        if let Some(key) = self.cache.read().await.get(kid) {
            return Some(key.clone());
        }
        // Cache miss -- refresh once (covers key rotation) before giving up.
        if let Err(err) = self.refresh().await {
            warn!(error = %err, "service_auth.jwks_refresh_failed");
            return None;
        }
        self.cache.read().await.get(kid).cloned()
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
    pub fn new(token_endpoint: impl Into<String>, sa_token_path: impl Into<String>, scope: impl Into<String>) -> Self {
        Self {
            token_endpoint: token_endpoint.into(),
            sa_token_path: sa_token_path.into(),
            scope: scope.into(),
            http: reqwest::Client::new(),
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
        *self.cached.write().await = Some(CachedToken { token: token.clone(), expires_at });
        Ok(token)
    }
}

fn now_secs() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or(Duration::ZERO).as_secs()
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
    const KEY_A_PRIV_DER: &[u8] = &[
        48, 46, 2, 1, 0, 48, 5, 6, 3, 43, 101, 112, 4, 34, 4, 32, 1, 204, 5, 142, 35, 153, 231, 38, 150, 122, 1, 218,
        34, 237, 70, 125, 233, 62, 126, 103, 151, 16, 11, 238, 95, 122, 209, 74, 183, 9, 171, 161,
    ];
    // Raw 32-byte Ed25519 public key (the last 32 bytes of the SPKI-DER
    // `openssl pkey -pubout` produced) -- `DecodingKey::from_ed_der` in
    // jsonwebtoken's rust_crypto backend reads only the raw key bytes, not
    // the full ASN.1 SPKI wrapper.
    const KEY_A_PUB_RAW: &[u8] = &[
        169, 90, 255, 23, 51, 151, 156, 147, 56, 247, 214, 168, 76, 160, 67, 99, 211, 238, 208, 5, 69, 236, 245, 115,
        4, 81, 1, 42, 23, 107, 4, 187,
    ];
    const KEY_B_PRIV_DER: &[u8] = &[
        48, 46, 2, 1, 0, 48, 5, 6, 3, 43, 101, 112, 4, 34, 4, 32, 145, 56, 92, 35, 32, 192, 103, 161, 66, 249, 233, 0,
        174, 22, 45, 100, 136, 104, 59, 129, 251, 81, 20, 214, 221, 250, 219, 227, 139, 109, 70, 185,
    ];

    fn ed25519_keypair() -> (EncodingKey, DecodingKey) {
        (EncodingKey::from_ed_der(KEY_A_PRIV_DER), DecodingKey::from_ed_der(KEY_A_PUB_RAW))
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
        let claims = verify(&token, &bundle, "waddlebot-internal", &["hub-api"], "identity:ephemeral:mint")
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
        let err = verify(&token, &bundle, "waddlebot-internal", &["hub-api"], "identity:ephemeral:mint")
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
        let err = verify(&token, &bundle, "waddlebot-internal", &["hub-api"], "identity:ephemeral:mint")
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
        let err = verify(&token, &bundle, "waddlebot-internal", &["hub-api"], "identity:ephemeral:mint")
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
        let err = verify(&token, &bundle, "waddlebot-internal", &["hub-api"], "some:other:scope")
            .await
            .unwrap_err();
        assert!(matches!(err, ServiceAuthError::InvalidToken(_)));
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
        let err = verify(&forged, &bundle, "waddlebot-internal", &["hub-api"], "identity:ephemeral:mint")
            .await
            .unwrap_err();
        assert!(matches!(err, ServiceAuthError::InvalidToken(_)));
    }
}
