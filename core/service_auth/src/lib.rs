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
    // `nbf` is mandatory (security review MEDIUM finding) -- `validate_nbf`
    // defaults to `false` in jsonwebtoken 10.x, so it must be opted into
    // explicitly; `leeway` above bounds it against issuer/verifier clock
    // skew the same way it already bounds `exp`.
    validation.validate_nbf = true;
    validation.set_required_spec_claims(&["exp", "iat", "nbf", "iss", "aud", "sub"]);

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
            let key = DecodingKey::from_ed_components(&entry.x).map_err(|e| {
                ServiceAuthError::InvalidToken(format!("bad JWKS entry {}: {e}", entry.kid))
            })?;
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
    pub fn new(
        token_endpoint: impl Into<String>,
        sa_token_path: impl Into<String>,
        scope: impl Into<String>,
    ) -> Self {
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
        assert!(matches!(err, ServiceAuthError::InvalidToken(m) if m.contains("malformed header")));
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
}
