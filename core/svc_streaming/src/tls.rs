//! In-cluster TLS for svc-streaming: the control-plane listener and the hub-api client.
//!
//! Server side ([`build_server_config`] + [`serve`]): rustls with the `ring` provider,
//! pinned to a deliberately modest-but-modern profile so the handshake stays cheap --
//! TLS 1.3 preferred (1.2 floor), ECDSA P-256 server certs, `X25519`/`secp256r1` key
//! exchange, AES-128-GCM or ChaCha20-Poly1305 only, stateless session tickets for
//! resumption, ALPN `h2` then `http/1.1`. Stronger protection than this belongs to the
//! CNI layer (e.g. Cilium WireGuard), not a heavier per-connection handshake.
//!
//! Client side ([`internal_client`]): the pooled `reqwest` client for hub-api REST calls,
//! honouring the chart's `INTERNAL_CA_FILE` / `INTERNAL_TLS_REQUIRED` policy (same contract
//! as `service_auth::internal_tls`; svc-streaming does not link `service_auth` because it
//! pins a different `jsonwebtoken` major). Misconfiguration is an error, never a fallback.

use std::future::Future;
use std::path::Path;
use std::sync::Arc;
use std::time::Duration;

use anyhow::{anyhow, bail, Context};
use axum::body::Body;
use axum::Router;
use hyper::body::Incoming;
use hyper::Request;
use hyper_util::rt::{TokioExecutor, TokioIo};
use hyper_util::server::conn::auto::Builder as ConnBuilder;
use hyper_util::service::TowerToHyperService;
use rustls::crypto::ring as ring_provider;
use rustls::crypto::CryptoProvider;
use rustls::ServerConfig;
use rustls_pki_types::pem::PemObject;
use rustls_pki_types::{CertificateDer, PrivateKeyDer};
use tokio::net::TcpListener;
use tokio_rustls::TlsAcceptor;
use tower::ServiceExt;

/// Env var naming the PEM bundle of the internal CA that signed hub-api's certificate.
pub const ENV_INTERNAL_CA_FILE: &str = "INTERNAL_CA_FILE";
/// Inline PEM alternative to [`ENV_INTERNAL_CA_FILE`] (the chart injects it from a Secret key).
pub const ENV_INTERNAL_CA_PEM: &str = "INTERNAL_CA_PEM";
/// Env var that, when truthy, makes plaintext hub-api URLs a hard error.
pub const ENV_INTERNAL_TLS_REQUIRED: &str = "INTERNAL_TLS_REQUIRED";

/// Idle pooled connection lifetime for the hub-api client.
const POOL_IDLE_TIMEOUT: Duration = Duration::from_secs(90);
/// Keep-alive probe interval (TCP and HTTP/2 PING).
const KEEPALIVE: Duration = Duration::from_secs(30);

/// Builds the restricted ring provider: AES-128-GCM / ChaCha20-Poly1305, X25519 + P-256.
fn provider() -> CryptoProvider {
    let mut p = ring_provider::default_provider();
    p.cipher_suites = vec![
        ring_provider::cipher_suite::TLS13_AES_128_GCM_SHA256,
        ring_provider::cipher_suite::TLS13_CHACHA20_POLY1305_SHA256,
        ring_provider::cipher_suite::TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256,
        ring_provider::cipher_suite::TLS_ECDHE_ECDSA_WITH_CHACHA20_POLY1305_SHA256,
    ];
    p.kx_groups = vec![
        ring_provider::kx_group::X25519,
        ring_provider::kx_group::SECP256R1,
    ];
    p
}

/// Loads the PEM cert chain + private key into a rustls [`ServerConfig`].
///
/// Fails loudly on a missing/unreadable/empty file; there is no plaintext fallback.
pub fn build_server_config(cert_file: &Path, key_file: &Path) -> anyhow::Result<Arc<ServerConfig>> {
    let certs: Vec<CertificateDer<'static>> = CertificateDer::pem_file_iter(cert_file)
        .with_context(|| format!("cannot read TLS_CERT_FILE {cert_file:?}"))?
        .collect::<Result<_, _>>()
        .with_context(|| format!("TLS_CERT_FILE {cert_file:?} holds an invalid PEM certificate"))?;
    if certs.is_empty() {
        bail!("TLS_CERT_FILE {cert_file:?} contains no certificate");
    }
    let key = PrivateKeyDer::from_pem_file(key_file)
        .with_context(|| format!("cannot read a private key from TLS_KEY_FILE {key_file:?}"))?;
    let mut config = ServerConfig::builder_with_provider(Arc::new(provider()))
        .with_protocol_versions(&[&rustls::version::TLS13, &rustls::version::TLS12])
        .context("TLS protocol versions rejected by the crypto provider")?
        .with_no_client_auth()
        .with_single_cert(certs, key)
        .context("TLS certificate/key pair rejected")?;
    // Stateless tickets: resumption without server-side session state or a shared cache.
    config.ticketer = ring_provider::Ticketer::new().context("session ticketer unavailable")?;
    config.alpn_protocols = vec![b"h2".to_vec(), b"http/1.1".to_vec()];
    Ok(Arc::new(config))
}

/// Serves `app` over TLS on `listener` until `shutdown` resolves.
///
/// One accept loop; each connection is handshaken then served by hyper's auto (h1/h2)
/// builder, so HTTP/2 multiplexing over a single long-lived connection works. Failed
/// handshakes are logged at WARN and drop only that connection. On shutdown the loop stops
/// accepting; in-flight connections end with the process (same documented simplification as
/// the ingest listeners).
pub async fn serve(
    listener: TcpListener,
    config: Arc<ServerConfig>,
    app: Router,
    shutdown: impl Future<Output = ()>,
) -> anyhow::Result<()> {
    let acceptor = TlsAcceptor::from(config);
    tokio::pin!(shutdown);
    loop {
        let (tcp, peer) = tokio::select! {
            _ = &mut shutdown => return Ok(()),
            accepted = listener.accept() => accepted.context("TLS listener accept failed")?,
        };
        let acceptor = acceptor.clone();
        let app = app.clone();
        tokio::spawn(async move {
            let tls = match acceptor.accept(tcp).await {
                Ok(tls) => tls,
                Err(err) => {
                    tracing::warn!(%peer, error = %err, "tls handshake failed");
                    return;
                }
            };
            let service = TowerToHyperService::new(
                app.map_request(|req: Request<Incoming>| req.map(Body::new)),
            );
            if let Err(err) = ConnBuilder::new(TokioExecutor::new())
                .serve_connection_with_upgrades(TokioIo::new(tls), service)
                .await
            {
                tracing::debug!(%peer, error = %err, "tls connection ended with error");
            }
        });
    }
}

/// Builds the pooled hub-api REST client under the chart's internal-TLS policy.
///
/// `INTERNAL_CA_FILE` pins trust to the internal CA (public roots off); with
/// `INTERNAL_TLS_REQUIRED` it is mandatory and the client is HTTPS-only. TLS 1.2 floor,
/// HTTP/2 via ALPN with keep-alive pings so the one pooled connection stays warm.
pub fn internal_client(timeout: Duration) -> anyhow::Result<reqwest::Client> {
    internal_client_from(timeout, |k| std::env::var(k).ok())
}

/// [`internal_client`] with an injectable env lookup (tests).
pub fn internal_client_from(
    timeout: Duration,
    get: impl Fn(&str) -> Option<String>,
) -> anyhow::Result<reqwest::Client> {
    let required = matches!(
        get(ENV_INTERNAL_TLS_REQUIRED)
            .map(|v| v.trim().to_ascii_lowercase())
            .as_deref(),
        Some("1" | "true" | "yes" | "on")
    );
    let ca_pem = get(ENV_INTERNAL_CA_PEM).filter(|v| !v.trim().is_empty());
    let ca_path = get(ENV_INTERNAL_CA_FILE)
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty());
    let mut builder = reqwest::Client::builder()
        .timeout(timeout)
        .min_tls_version(reqwest::tls::Version::TLS_1_2)
        .pool_idle_timeout(POOL_IDLE_TIMEOUT)
        .tcp_keepalive(KEEPALIVE)
        .http2_keep_alive_interval(KEEPALIVE)
        .http2_keep_alive_while_idle(true);
    let ca_source: Option<(String, Vec<u8>)> = match (ca_pem, ca_path) {
        (Some(pem), _) => Some((ENV_INTERNAL_CA_PEM.to_string(), pem.into_bytes())),
        (None, Some(path)) => {
            let pem = std::fs::read(&path)
                .with_context(|| format!("cannot read {ENV_INTERNAL_CA_FILE} {path:?}"))?;
            Some((format!("{ENV_INTERNAL_CA_FILE} {path:?}"), pem))
        }
        (None, None) => None,
    };
    match ca_source {
        Some((label, pem)) => {
            if !String::from_utf8_lossy(&pem).contains("-----BEGIN CERTIFICATE-----") {
                bail!("{label} contains no PEM certificate block");
            }
            let ca = reqwest::Certificate::from_pem(&pem)
                .with_context(|| format!("{label} is not valid PEM"))?;
            builder = builder
                .tls_built_in_root_certs(false)
                .add_root_certificate(ca);
        }
        None if required => {
            return Err(anyhow!(
                "{ENV_INTERNAL_TLS_REQUIRED} is set but neither {ENV_INTERNAL_CA_PEM} nor {ENV_INTERNAL_CA_FILE} is set"
            ))
        }
        None => {}
    }
    if required {
        builder = builder.https_only(true);
    }
    builder.build().context("cannot build internal HTTP client")
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::routing::get;
    use std::process::Command;

    /// Generates (ca.pem, server.crt, server.key): ECDSA P-256 / SHA-256, SAN=localhost.
    fn make_pki(
        dir: &Path,
        tag: &str,
    ) -> (std::path::PathBuf, std::path::PathBuf, std::path::PathBuf) {
        let run = |args: &[&str]| {
            let out = Command::new("openssl")
                .args(args)
                .current_dir(dir)
                .output()
                .expect("openssl must be installed to run the TLS real-path test");
            assert!(
                out.status.success(),
                "openssl {args:?}: {}",
                String::from_utf8_lossy(&out.stderr)
            );
        };
        let ca_key = format!("{tag}-ca.key");
        let ca_crt = format!("{tag}-ca.crt");
        let key = format!("{tag}.key");
        let csr = format!("{tag}.csr");
        let crt = format!("{tag}.crt");
        let ext = format!("{tag}.ext");
        run(&[
            "ecparam",
            "-name",
            "prime256v1",
            "-genkey",
            "-noout",
            "-out",
            &ca_key,
        ]);
        run(&[
            "req",
            "-x509",
            "-new",
            "-key",
            &ca_key,
            "-sha256",
            "-days",
            "1",
            "-subj",
            "/CN=test-ca",
            "-out",
            &ca_crt,
        ]);
        run(&[
            "ecparam",
            "-name",
            "prime256v1",
            "-genkey",
            "-noout",
            "-out",
            &format!("{tag}.sec1.key"),
        ]);
        run(&[
            "pkcs8",
            "-topk8",
            "-nocrypt",
            "-in",
            &format!("{tag}.sec1.key"),
            "-out",
            &key,
        ]);
        run(&[
            "req",
            "-new",
            "-key",
            &key,
            "-subj",
            "/CN=localhost",
            "-out",
            &csr,
        ]);
        std::fs::write(dir.join(&ext), "subjectAltName=DNS:localhost\n").unwrap();
        run(&[
            "x509",
            "-req",
            "-in",
            &csr,
            "-CA",
            &ca_crt,
            "-CAkey",
            &ca_key,
            "-CAcreateserial",
            "-sha256",
            "-days",
            "1",
            "-extfile",
            &ext,
            "-out",
            &crt,
        ]);
        (dir.join(ca_crt), dir.join(crt), dir.join(key))
    }

    async fn start(cert: &Path, key: &Path) -> (u16, tokio::sync::oneshot::Sender<()>) {
        let cfg = build_server_config(cert, key).expect("server config");
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let (tx, rx) = tokio::sync::oneshot::channel::<()>();
        let app = Router::new().route("/health", get(|| async { "ok" }));
        tokio::spawn(serve(listener, cfg, app, async move {
            let _ = rx.await;
        }));
        (port, tx)
    }

    fn client_with_ca(ca: &Path) -> reqwest::Client {
        internal_client_from(Duration::from_secs(5), |k| match k {
            ENV_INTERNAL_CA_FILE => Some(ca.display().to_string()),
            ENV_INTERNAL_TLS_REQUIRED => Some("true".into()),
            _ => None,
        })
        .expect("client")
    }

    #[tokio::test]
    async fn real_tls_handshake_verifies_ca_and_negotiates_h2() {
        let dir = std::env::temp_dir().join(format!("svcs-tls-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let (ca, crt, key) = make_pki(&dir, "good");
        let (other_ca, _, _) = make_pki(&dir, "other");
        let (port, stop) = start(&crt, &key).await;
        let url = format!("https://localhost:{port}/health");

        let ok = client_with_ca(&ca)
            .get(&url)
            .send()
            .await
            .expect("trusted CA handshake");
        assert_eq!(
            ok.version(),
            reqwest::Version::HTTP_2,
            "ALPN must select h2"
        );
        assert_eq!(ok.text().await.unwrap(), "ok");

        // Wrong CA: the handshake MUST fail (loud), not fall back.
        let err = client_with_ca(&other_ca)
            .get(&url)
            .send()
            .await
            .unwrap_err();
        assert!(
            err.is_connect(),
            "expected a connect/handshake error, got {err}"
        );

        // Plaintext against the TLS port is refused by the HTTPS-only client.
        let plain = client_with_ca(&ca)
            .get(format!("http://localhost:{port}/health"))
            .send()
            .await;
        assert!(plain.is_err(), "https_only client must refuse http://");

        let _ = stop.send(());
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn server_config_errors_are_loud() {
        assert!(build_server_config(Path::new("/nope.crt"), Path::new("/nope.key")).is_err());
        let dir = std::env::temp_dir().join(format!("svcs-tls-bad-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let empty = dir.join("empty.pem");
        std::fs::write(&empty, "").unwrap();
        assert!(build_server_config(&empty, &empty).is_err());
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn client_policy_errors_are_loud() {
        let none = |_: &str| None;
        assert!(internal_client_from(Duration::from_secs(1), none).is_ok());
        let req_no_ca = |k: &str| (k == ENV_INTERNAL_TLS_REQUIRED).then(|| "true".to_string());
        assert!(internal_client_from(Duration::from_secs(1), req_no_ca).is_err());
        let bad_path =
            |k: &str| (k == ENV_INTERNAL_CA_FILE).then(|| "/nonexistent/ca.crt".to_string());
        assert!(internal_client_from(Duration::from_secs(1), bad_path).is_err());
    }
}
