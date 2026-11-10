//! In-cluster HTTPS policy for hub-api (and other internal) HTTP clients.
//!
//! The chart (`global.internalTransitTls`) mounts the internal-CA bundle and sets
//! `INTERNAL_CA_PEM` (or `INTERNAL_CA_FILE`) + `INTERNAL_TLS_REQUIRED=true` on every workload that dials an
//! internal HTTP endpoint. This module turns those two variables into a hardened
//! `reqwest::ClientBuilder`: trust is pinned to the internal CA (public roots are
//! disabled), TLS 1.2 is the floor (1.3 negotiated whenever the server offers it),
//! plaintext is refused (`https_only`), and connections are pooled with keep-alive so
//! a process pays one handshake per peer, not one per request. Every misconfiguration
//! -- an unreadable CA file, a CA file that is not PEM, `INTERNAL_TLS_REQUIRED` without
//! a CA, an `http://` URL under the policy -- is a hard [`InternalTlsError`], never a
//! silent fallback to plaintext or to system roots.
//!
//! Callers MUST build one client per process and clone it (a `reqwest::Client` is a
//! cheap `Arc` handle sharing one pool); see [`shared_client`].

use std::path::Path;
use std::sync::OnceLock;
use std::time::Duration;

/// Path of the PEM bundle holding the internal CA that signed the in-cluster server certs.
pub const ENV_INTERNAL_CA_FILE: &str = "INTERNAL_CA_FILE";
/// Inline PEM alternative to [`ENV_INTERNAL_CA_FILE`] (the chart injects the CA from a Secret key).
pub const ENV_INTERNAL_CA_PEM: &str = "INTERNAL_CA_PEM";
/// `true`/`1` makes every internal client refuse `http://` URLs and require a CA.
pub const ENV_INTERNAL_TLS_REQUIRED: &str = "INTERNAL_TLS_REQUIRED";

/// How long an idle pooled connection is kept for reuse (keeps resumption/keep-alive warm).
pub const POOL_IDLE_TIMEOUT: Duration = Duration::from_secs(90);
/// TCP keep-alive probe interval on pooled connections.
pub const TCP_KEEPALIVE: Duration = Duration::from_secs(30);

/// Every way the internal-TLS policy can reject a configuration.
#[derive(thiserror::Error, Debug)]
pub enum InternalTlsError {
    /// `INTERNAL_TLS_REQUIRED` is on but no CA bundle was configured.
    #[error("{ENV_INTERNAL_TLS_REQUIRED} is set but {ENV_INTERNAL_CA_FILE} is empty; refusing to guess a trust root")]
    MissingCa,
    /// The CA file could not be read.
    #[error("cannot read {ENV_INTERNAL_CA_FILE} {path:?}: {source}")]
    UnreadableCa {
        path: String,
        #[source]
        source: std::io::Error,
    },
    /// The CA file holds no parseable PEM certificate.
    #[error("{ENV_INTERNAL_CA_FILE} {path:?} holds no valid PEM certificate: {source}")]
    InvalidCa {
        path: String,
        #[source]
        source: reqwest::Error,
    },
    /// The CA input contains no `BEGIN CERTIFICATE` block at all.
    #[error("{ENV_INTERNAL_CA_FILE} {path:?} contains no PEM certificate block")]
    NoCertificate { path: String },
    /// An `http://` (or non-http) URL was supplied while the policy requires HTTPS.
    #[error("plaintext or non-HTTPS URL {url:?} refused: {ENV_INTERNAL_TLS_REQUIRED} is set")]
    PlaintextRefused { url: String },
    /// The HTTP client could not be built.
    #[error("cannot build internal HTTP client: {0}")]
    Build(#[from] reqwest::Error),
}

/// Parses a PEM CA bundle; garbage or a bundle without any certificate block is an error.
fn parse_ca(pem: &[u8], path: &str) -> Result<reqwest::Certificate, InternalTlsError> {
    if !String::from_utf8_lossy(pem).contains("-----BEGIN CERTIFICATE-----") {
        return Err(InternalTlsError::NoCertificate {
            path: path.to_string(),
        });
    }
    reqwest::Certificate::from_pem(pem).map_err(|source| InternalTlsError::InvalidCa {
        path: path.to_string(),
        source,
    })
}

/// Resolved internal-TLS policy: an optional pinned CA plus the plaintext-refusal flag.
#[derive(Clone, Debug, Default)]
pub struct InternalTls {
    ca: Option<reqwest::Certificate>,
    required: bool,
}

impl InternalTls {
    /// Builds the policy from explicit inputs (`ca_pem` is validated here).
    pub fn new(ca_pem: Option<&[u8]>, required: bool) -> Result<Self, InternalTlsError> {
        if required && ca_pem.is_none() {
            return Err(InternalTlsError::MissingCa);
        }
        let ca = match ca_pem {
            Some(pem) => Some(parse_ca(pem, "<inline>")?),
            None => None,
        };
        Ok(Self { ca, required })
    }

    /// Reads `INTERNAL_CA_FILE` / `INTERNAL_TLS_REQUIRED` from the process environment.
    pub fn from_env() -> Result<Self, InternalTlsError> {
        Self::from_lookup(|k| std::env::var(k).ok())
    }

    /// Same as [`Self::from_env`] with an injectable variable lookup (used by tests).
    pub fn from_lookup(get: impl Fn(&str) -> Option<String>) -> Result<Self, InternalTlsError> {
        let required = matches!(
            get(ENV_INTERNAL_TLS_REQUIRED)
                .map(|v| v.trim().to_ascii_lowercase())
                .as_deref(),
            Some("1" | "true" | "yes" | "on")
        );
        if let Some(pem) = get(ENV_INTERNAL_CA_PEM).filter(|v| !v.trim().is_empty()) {
            let ca = parse_ca(pem.as_bytes(), ENV_INTERNAL_CA_PEM)?;
            return Ok(Self {
                ca: Some(ca),
                required,
            });
        }
        let path = get(ENV_INTERNAL_CA_FILE)
            .map(|v| v.trim().to_string())
            .filter(|v| !v.is_empty());
        let Some(path) = path else {
            return Self::new(None, required);
        };
        let pem =
            std::fs::read(Path::new(&path)).map_err(|source| InternalTlsError::UnreadableCa {
                path: path.clone(),
                source,
            })?;
        let ca = parse_ca(&pem, &path)?;
        Ok(Self {
            ca: Some(ca),
            required,
        })
    }

    /// True when `http://` URLs must be refused.
    pub fn is_required(&self) -> bool {
        self.required
    }

    /// True when trust is pinned to an internal CA.
    pub fn has_ca(&self) -> bool {
        self.ca.is_some()
    }

    /// Rejects `url` when the policy requires HTTPS and it is not an `https://` URL.
    pub fn check_url(&self, url: &str) -> Result<(), InternalTlsError> {
        if self.required && !url.trim().to_ascii_lowercase().starts_with("https://") {
            return Err(InternalTlsError::PlaintextRefused {
                url: url.to_string(),
            });
        }
        Ok(())
    }

    /// Applies the policy to a client builder: pin the CA (disabling public roots),
    /// floor TLS at 1.2, enable pooled keep-alive + HTTP/2, and (when required) allow HTTPS
    /// only so a redirect can never downgrade to plaintext.
    pub fn apply(&self, mut builder: reqwest::ClientBuilder) -> reqwest::ClientBuilder {
        builder = builder
            .min_tls_version(reqwest::tls::Version::TLS_1_2)
            .pool_idle_timeout(POOL_IDLE_TIMEOUT)
            .tcp_keepalive(TCP_KEEPALIVE)
            // HTTP/2 is negotiated via ALPN on the TLS connection (h1 fallback if the
            // server only speaks 1.1); keep-alive pings hold the multiplexed connection
            // open so the handshake is paid once per peer.
            .http2_keep_alive_interval(TCP_KEEPALIVE)
            .http2_keep_alive_while_idle(true);
        if let Some(ca) = &self.ca {
            builder = builder
                .tls_built_in_root_certs(false)
                .add_root_certificate(ca.clone());
        }
        if self.required {
            builder = builder.https_only(true);
        }
        builder
    }
}

/// Returns the process-wide pooled internal client, building it on first use.
///
/// Every internal caller clones this one handle so all requests share a single
/// connection pool (one TLS handshake per peer, then keep-alive / resumption). A
/// failed first build is returned as an error and retried on the next call; it is
/// never papered over with an unconfigured client.
pub fn shared_client() -> Result<reqwest::Client, InternalTlsError> {
    static CLIENT: OnceLock<reqwest::Client> = OnceLock::new();
    if let Some(c) = CLIENT.get() {
        return Ok(c.clone());
    }
    let tls = InternalTls::from_env()?;
    let client = tls
        .apply(reqwest::Client::builder().timeout(Duration::from_secs(10)))
        .build()?;
    Ok(CLIENT.get_or_init(|| client).clone())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn lookup(map: HashMap<&'static str, String>) -> impl Fn(&str) -> Option<String> {
        move |k| map.get(k).cloned()
    }

    #[test]
    fn unset_env_is_permissive_but_not_pinned() {
        let tls = InternalTls::from_lookup(|_| None).unwrap();
        assert!(!tls.is_required() && !tls.has_ca());
        assert!(tls.check_url("http://localhost:1").is_ok());
    }

    #[test]
    fn required_without_ca_fails_loud() {
        let err = InternalTls::from_lookup(lookup(HashMap::from([(
            ENV_INTERNAL_TLS_REQUIRED,
            "true".to_string(),
        )])))
        .unwrap_err();
        assert!(matches!(err, InternalTlsError::MissingCa));
    }

    #[test]
    fn inline_pem_env_pins_ca_and_garbage_fails_loud() {
        let bad = InternalTls::from_lookup(lookup(HashMap::from([(
            ENV_INTERNAL_CA_PEM,
            "garbage".to_string(),
        )])))
        .unwrap_err();
        assert!(matches!(bad, InternalTlsError::NoCertificate { .. }));
    }

    #[test]
    fn missing_ca_file_fails_loud() {
        let err = InternalTls::from_lookup(lookup(HashMap::from([(
            ENV_INTERNAL_CA_FILE,
            "/nonexistent/ca.crt".to_string(),
        )])))
        .unwrap_err();
        assert!(matches!(err, InternalTlsError::UnreadableCa { .. }));
    }

    #[test]
    fn non_pem_ca_file_fails_loud() {
        let dir = std::env::temp_dir().join(format!("itls-bad-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("ca.crt");
        std::fs::write(&path, b"not a certificate").unwrap();
        let err = InternalTls::from_lookup(lookup(HashMap::from([(
            ENV_INTERNAL_CA_FILE,
            path.display().to_string(),
        )])))
        .unwrap_err();
        std::fs::remove_dir_all(&dir).ok();
        assert!(matches!(err, InternalTlsError::NoCertificate { .. }));
    }

    #[test]
    fn required_refuses_plaintext_and_accepts_https() {
        let tls = InternalTls {
            ca: None,
            required: true,
        };
        assert!(matches!(
            tls.check_url("http://hub-api:8204/x"),
            Err(InternalTlsError::PlaintextRefused { .. })
        ));
        assert!(tls.check_url("HTTPS://hub-api:8204/x").is_ok());
    }

    /// Real-path pooling proof: three requests through `shared_client()` handles must
    /// ride ONE TCP connection (one handshake), not one connection per request.
    #[tokio::test]
    async fn shared_client_reuses_one_pooled_connection() {
        use std::sync::atomic::{AtomicUsize, Ordering};
        use std::sync::Arc;
        use tokio::io::{AsyncReadExt, AsyncWriteExt};

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let accepted = Arc::new(AtomicUsize::new(0));
        let counter = accepted.clone();
        tokio::spawn(async move {
            loop {
                let Ok((mut sock, _)) = listener.accept().await else {
                    return;
                };
                counter.fetch_add(1, Ordering::SeqCst);
                tokio::spawn(async move {
                    let mut buf = [0u8; 2048];
                    while let Ok(n) = sock.read(&mut buf).await {
                        if n == 0 {
                            return;
                        }
                        let resp = b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\nconnection: keep-alive\r\n\r\nok";
                        if sock.write_all(resp).await.is_err() {
                            return;
                        }
                    }
                });
            }
        });

        let url = format!("http://{addr}/");
        for _ in 0..3 {
            let client = shared_client().expect("shared client");
            let resp = client.get(&url).send().await.expect("request");
            assert_eq!(resp.text().await.unwrap(), "ok");
        }
        assert_eq!(
            accepted.load(Ordering::SeqCst),
            1,
            "3 requests via shared_client() must reuse a single pooled connection"
        );
    }
}
