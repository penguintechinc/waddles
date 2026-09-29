//! The mTLS transport `crate::wire::run_connection` runs over in
//! production (spec `docs/superpowers/specs/2026-09-14-rust-data-plane-
//! design.md` SS6.6): `rustls` client config built from the configured CA/
//! client-cert/client-key files, dialing `STAGE_HOST_API_ADDR`.
//!
//! **Peer identity pinning (gh security review CRITICAL finding on PR
//! #406, item 1):** the executor's callers are the trusted multi-tenant
//! `svc-process`/`svc-action` -- the scope (`tenant_id`/`community_id`) on
//! every `Load`/`Unload` this executor's registry trusts is only safe to
//! trust because ONLY that one legitimate stage can ever be on the other
//! end of this connection. Standard TLS chain+hostname verification
//! (`build_client_config`'s base behavior, always on) proves "issued by
//! our CA for this DNS name"; [`PinnedIdentityVerifier`] additionally
//! pins WHICH exact service identity the certificate must present (a
//! SPIFFE URI SAN, or a DNS SAN/CN fallback) when `HOST_API_STAGE_IDENTITY`
//! is configured -- closing spec SS6.6's "otherwise pinned by
//! configuration" gap this module's own doc used to flag as a TODO.
//! `CliConfig::validate_host_api_tls` requires this (and a client cert) in
//! production; a connection presenting no server certificate at all
//! (plaintext), no client certificate when one is required, or a
//! certificate for an unlisted identity is refused before a single frame
//! is read -- see this module's tests for all three.

use std::sync::Arc;

use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::client::WebPkiServerVerifier;
use rustls::{
    ClientConfig, DigitallySignedStruct, Error as TlsError, RootCertStore, SignatureScheme,
};
use rustls_pki_types::pem::PemObject;
use rustls_pki_types::{CertificateDer, PrivateKeyDer, ServerName, UnixTime};
use tokio::net::TcpStream;
use tokio_rustls::client::TlsStream;
use tokio_rustls::TlsConnector;

use crate::config::CliConfig;
use crate::error::ExecutorError;

/// Wraps the standard `WebPkiServerVerifier` (full chain + hostname
/// verification, delegated to unchanged) and additionally requires the
/// leaf certificate's Subject Alternative Name (a `URI` SAN, matching a
/// SPIFFE identity like `spiffe://penguintech.io/<env>/svc-process`, or a
/// `DNSName` SAN as a fallback) OR Subject Common Name to exactly equal
/// `expected_identity`. This is the "checks the peer against an allowlist"
/// half of item 1 -- a certificate that is perfectly valid (trusted CA,
/// correct hostname) but was issued for a DIFFERENT service identity is
/// still refused.
#[derive(Debug)]
struct PinnedIdentityVerifier {
    inner: Arc<dyn ServerCertVerifier>,
    expected_identity: String,
}

impl PinnedIdentityVerifier {
    fn new(roots: Arc<RootCertStore>, expected_identity: String) -> Result<Self, ExecutorError> {
        let inner = WebPkiServerVerifier::builder(roots)
            .build()
            .map_err(|e| ExecutorError::Config(format!("invalid TLS verifier config: {e}")))?;
        Ok(Self {
            inner,
            expected_identity,
        })
    }

    /// True if `cert`'s SAN (URI or DNS) or Subject CN exactly equals
    /// `self.expected_identity`. A parse failure is treated as "no match"
    /// (fail closed), never as "skip the check".
    fn certificate_matches_expected_identity(&self, cert: &CertificateDer<'_>) -> bool {
        let Ok((_, parsed)) = x509_parser::parse_x509_certificate(cert.as_ref()) else {
            return false;
        };
        if let Ok(Some(san)) = parsed.subject_alternative_name() {
            for name in &san.value.general_names {
                let candidate = match name {
                    x509_parser::extensions::GeneralName::URI(uri) => Some(*uri),
                    x509_parser::extensions::GeneralName::DNSName(dns) => Some(*dns),
                    _ => None,
                };
                if candidate == Some(self.expected_identity.as_str()) {
                    return true;
                }
            }
        }
        let cn_matches = parsed
            .subject()
            .iter_common_name()
            .filter_map(|cn| cn.as_str().ok())
            .any(|cn| cn == self.expected_identity);
        cn_matches
    }
}

impl ServerCertVerifier for PinnedIdentityVerifier {
    fn verify_server_cert(
        &self,
        end_entity: &CertificateDer<'_>,
        intermediates: &[CertificateDer<'_>],
        server_name: &ServerName<'_>,
        ocsp_response: &[u8],
        now: UnixTime,
    ) -> Result<ServerCertVerified, TlsError> {
        // Base verification first (trusted CA, not expired, hostname
        // match) -- identity pinning is an ADDITIONAL restriction on top
        // of a certificate that has already passed the standard checks,
        // never a replacement for them.
        self.inner.verify_server_cert(
            end_entity,
            intermediates,
            server_name,
            ocsp_response,
            now,
        )?;
        if self.certificate_matches_expected_identity(end_entity) {
            Ok(ServerCertVerified::assertion())
        } else {
            Err(TlsError::General(format!(
                "peer certificate identity is not in the allowlist (expected {:?})",
                self.expected_identity
            )))
        }
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, TlsError> {
        self.inner.verify_tls12_signature(message, cert, dss)
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, TlsError> {
        self.inner.verify_tls13_signature(message, cert, dss)
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.inner.supported_verify_schemes()
    }
}

/// Dials `cfg.stage_host_api_addr` and completes a TLS 1.2+ handshake
/// (mutual if client cert/key are configured), returning a stream
/// `crate::wire::run_connection` can frame directly.
pub async fn dial_stage(cfg: &CliConfig) -> Result<TlsStream<TcpStream>, ExecutorError> {
    let tcp = TcpStream::connect(&cfg.stage_host_api_addr).await?;
    let tls_config = build_client_config(cfg)?;
    let connector = TlsConnector::from(Arc::new(tls_config));

    let server_name = server_name_from_addr(&cfg.stage_host_api_addr)?;
    let stream = connector
        .connect(server_name, tcp)
        .await
        .map_err(ExecutorError::Io)?;
    Ok(stream)
}

fn server_name_from_addr(addr: &str) -> Result<ServerName<'static>, ExecutorError> {
    let host = addr.split(':').next().unwrap_or(addr).to_string();
    ServerName::try_from(host.clone()).map_err(|e| {
        ExecutorError::Config(format!("invalid STAGE_HOST_API_ADDR host {host:?}: {e}"))
    })
}

/// Builds the rustls `ClientConfig`: the configured CA as the sole root
/// (never the system trust store -- this is a closed mTLS pair, not a
/// connection to the public internet), a client certificate/key when both
/// are configured (spec SS6.6: "Both peers present certificates"), and --
/// when `HOST_API_STAGE_IDENTITY` is configured -- [`PinnedIdentityVerifier`]
/// in place of the standard verifier (see this module's own doc for why).
/// `CliConfig::validate_host_api_tls` requires all three (CA, client cert,
/// stage identity) in production; this function itself stays permissive
/// (falls back to base verification / no client auth when unset) so the
/// narrower unit tests in this module and `crate::wire` that don't
/// exercise the full production posture keep working unchanged.
fn build_client_config(cfg: &CliConfig) -> Result<ClientConfig, ExecutorError> {
    let mut roots = RootCertStore::empty();
    if let Some(ca_path) = &cfg.host_api_ca_file {
        for cert in load_certs(ca_path)? {
            roots
                .add(cert)
                .map_err(|e| ExecutorError::Config(format!("invalid CA certificate: {e}")))?;
        }
    }
    let roots = Arc::new(roots);

    let builder = match &cfg.host_api_stage_identity {
        Some(expected_identity) if !expected_identity.is_empty() => {
            let verifier = PinnedIdentityVerifier::new(roots, expected_identity.clone())?;
            ClientConfig::builder()
                .dangerous()
                .with_custom_certificate_verifier(Arc::new(verifier))
        }
        _ => ClientConfig::builder().with_root_certificates(roots),
    };

    let config = match (
        &cfg.host_api_client_cert_file,
        &cfg.host_api_client_key_file,
    ) {
        (Some(cert_path), Some(key_path)) => {
            let certs = load_certs(cert_path)?;
            let key = load_private_key(key_path)?;
            builder
                .with_client_auth_cert(certs, key)
                .map_err(|e| ExecutorError::Config(format!("invalid client cert/key: {e}")))?
        }
        (None, None) => builder.with_no_client_auth(),
        _ => {
            return Err(ExecutorError::Config(
                "HOST_API_CLIENT_CERT_FILE and HOST_API_CLIENT_KEY_FILE must be set together"
                    .to_string(),
            ))
        }
    };
    Ok(config)
}

/// `pub(crate)`: also used by `crate::bucket`'s optional `https://` bucket
/// TLS config (same PEM-CA-file loading, different `ClientConfig`).
pub(crate) fn load_certs(
    path: &std::path::Path,
) -> Result<Vec<CertificateDer<'static>>, ExecutorError> {
    CertificateDer::pem_file_iter(path)
        .map_err(|e| ExecutorError::Config(format!("failed to read certs from {path:?}: {e}")))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| ExecutorError::Config(format!("invalid PEM certificate in {path:?}: {e}")))
}

fn load_private_key(path: &std::path::Path) -> Result<PrivateKeyDer<'static>, ExecutorError> {
    PrivateKeyDer::from_pem_file(path).map_err(|e| {
        ExecutorError::Config(format!("failed to read private key from {path:?}: {e}"))
    })
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use std::io::Write;

    fn test_config() -> CliConfig {
        use clap::Parser;
        CliConfig::try_parse_from([
            "bundle-executor",
            "--stage-host-api-addr",
            "svc-process:8301",
        ])
        .expect("static test args always parse")
    }

    #[test]
    fn server_name_extracts_host_without_port() -> Result<(), ExecutorError> {
        let name = server_name_from_addr("svc-process:8301")?;
        assert!(matches!(name, ServerName::DnsName(_)));
        Ok(())
    }

    #[test]
    fn server_name_rejects_an_unparseable_host() {
        // An IPv6-literal-looking-but-invalid host with no brackets is
        // neither a valid DNS name nor a valid IP address.
        assert!(matches!(
            server_name_from_addr(":::not-a-host:8301"),
            Err(ExecutorError::Config(_))
        ));
    }

    #[test]
    fn client_config_with_no_certs_allows_no_client_auth() -> Result<(), ExecutorError> {
        build_client_config(&test_config())?;
        Ok(())
    }

    #[test]
    fn client_cert_without_key_is_rejected() {
        let mut cfg = test_config();
        cfg.host_api_client_cert_file = Some(std::path::PathBuf::from("/nonexistent/cert.pem"));
        assert!(matches!(
            build_client_config(&cfg),
            Err(ExecutorError::Config(_))
        ));
    }

    #[test]
    fn key_without_cert_is_rejected() {
        let mut cfg = test_config();
        cfg.host_api_client_key_file = Some(std::path::PathBuf::from("/nonexistent/key.pem"));
        assert!(matches!(
            build_client_config(&cfg),
            Err(ExecutorError::Config(_))
        ));
    }

    /// A throwaway self-signed CA + a client leaf cert/key it issued, held
    /// as PEM text -- generated fresh per test run (`rcgen`, pure Rust, no
    /// external `openssl` CLI dependency) rather than a committed fixture,
    /// so no private key material -- test-only or otherwise -- ever lands
    /// in source control for gitleaks/trufflehog to flag.
    struct TestPki {
        ca_pem: String,
        client_cert_pem: String,
        client_key_pem: String,
    }

    fn generate_test_pki() -> TestPki {
        let ca_key = rcgen::KeyPair::generate().expect("generate CA key");
        let ca_params = rcgen::CertificateParams::new(vec!["bundle-executor-test-ca".to_string()])
            .expect("build CA params");
        let ca_cert = ca_params.self_signed(&ca_key).expect("self-sign CA cert");
        let issuer = rcgen::Issuer::from_params(&ca_params, ca_key);

        let client_key = rcgen::KeyPair::generate().expect("generate client key");
        let client_params =
            rcgen::CertificateParams::new(vec!["bundle-executor-test-client".to_string()])
                .expect("build client params");
        let client_cert = client_params
            .signed_by(&client_key, &issuer)
            .expect("sign client cert with test CA");

        TestPki {
            ca_pem: ca_cert.pem(),
            client_cert_pem: client_cert.pem(),
            client_key_pem: client_key.serialize_pem(),
        }
    }

    fn write_temp_pem(contents: &str, label: &str) -> std::path::PathBuf {
        let path = std::env::temp_dir().join(format!(
            "bundle-executor-test-{}-{}-{label}.pem",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0)
        ));
        let mut f = std::fs::File::create(&path).expect("create temp pem file");
        f.write_all(contents.as_bytes())
            .expect("write temp pem file");
        path
    }

    #[test]
    fn load_certs_and_private_key_parse_a_real_generated_pem() -> Result<(), ExecutorError> {
        let pki = generate_test_pki();
        let cert_path = write_temp_pem(&pki.client_cert_pem, "cert");
        let key_path = write_temp_pem(&pki.client_key_pem, "key");

        let certs = load_certs(&cert_path)?;
        assert_eq!(certs.len(), 1);
        load_private_key(&key_path)?;

        let _ = std::fs::remove_file(&cert_path);
        let _ = std::fs::remove_file(&key_path);
        Ok(())
    }

    #[test]
    fn build_client_config_accepts_a_real_ca_and_client_cert_pair() -> Result<(), ExecutorError> {
        let pki = generate_test_pki();
        let ca_path = write_temp_pem(&pki.ca_pem, "ca");
        let cert_path = write_temp_pem(&pki.client_cert_pem, "cert");
        let key_path = write_temp_pem(&pki.client_key_pem, "key");

        let mut cfg = test_config();
        cfg.host_api_ca_file = Some(ca_path.clone());
        cfg.host_api_client_cert_file = Some(cert_path.clone());
        cfg.host_api_client_key_file = Some(key_path.clone());
        build_client_config(&cfg)?;

        let _ = std::fs::remove_file(&ca_path);
        let _ = std::fs::remove_file(&cert_path);
        let _ = std::fs::remove_file(&key_path);
        Ok(())
    }

    /// Exercises `dial_stage`'s real TCP connect + rustls client handshake
    /// against a local `tokio-rustls` TLS server presenting the same
    /// generated cert, proving `dial_stage`'s success path end to end --
    /// not just `build_client_config`'s config-building half.
    #[tokio::test]
    async fn dial_stage_completes_a_real_tls_handshake() -> Result<(), Box<dyn std::error::Error>> {
        // One CA signs both the server's leaf cert (presented to the
        // client below) and, in principle, a client cert -- only the
        // server side is needed here since `dial_stage`'s own config
        // (via `test_config()`) sets no client cert (server-auth-only
        // TLS, same as `client_config_with_no_certs_allows_no_client_auth`).
        let ca_key = rcgen::KeyPair::generate()?;
        let ca_params = rcgen::CertificateParams::new(vec!["bundle-executor-test-ca".to_string()])?;
        let ca_cert = ca_params.self_signed(&ca_key)?;
        let issuer = rcgen::Issuer::from_params(&ca_params, ca_key);

        let server_key = rcgen::KeyPair::generate()?;
        let server_params = rcgen::CertificateParams::new(vec!["127.0.0.1".to_string()])?;
        let server_cert = server_params.signed_by(&server_key, &issuer)?;

        let ca_path = write_temp_pem(&ca_cert.pem(), "ca");
        let server_cert_path = write_temp_pem(&server_cert.pem(), "servercert");
        let server_key_path = write_temp_pem(&server_key.serialize_pem(), "serverkey");

        let server_certs = load_certs(&server_cert_path)?;
        let server_key = load_private_key(&server_key_path)?;
        let server_config = rustls::ServerConfig::builder()
            .with_no_client_auth()
            .with_single_cert(server_certs, server_key)?;
        let acceptor = tokio_rustls::TlsAcceptor::from(Arc::new(server_config));

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let addr = listener.local_addr()?;

        let server_task = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.expect("accept");
            let _tls = acceptor.accept(tcp).await.expect("server tls handshake");
        });

        let mut cfg = test_config();
        cfg.stage_host_api_addr = addr.to_string();
        cfg.host_api_ca_file = Some(ca_path.clone());

        dial_stage(&cfg).await?;
        server_task.await?;

        let _ = std::fs::remove_file(&ca_path);
        let _ = std::fs::remove_file(&server_cert_path);
        let _ = std::fs::remove_file(&server_key_path);
        Ok(())
    }

    /// A throwaway CA + server leaf cert carrying a SPIFFE `URI` SAN,
    /// generated fresh per test (same no-committed-key-material rationale
    /// as [`generate_test_pki`]).
    struct ServerPkiWithSan {
        ca_pem: String,
        server_cert_pem: String,
        server_key_pem: String,
    }

    fn generate_server_pki_with_uri_san(uri_san: &str) -> ServerPkiWithSan {
        let ca_key = rcgen::KeyPair::generate().expect("generate CA key");
        let ca_params = rcgen::CertificateParams::new(vec!["bundle-executor-test-ca".to_string()])
            .expect("build CA params");
        let ca_cert = ca_params.self_signed(&ca_key).expect("self-sign CA cert");
        let issuer = rcgen::Issuer::from_params(&ca_params, ca_key);

        let server_key = rcgen::KeyPair::generate().expect("generate server key");
        let mut server_params =
            rcgen::CertificateParams::new(vec!["127.0.0.1".to_string()]).expect("server params");
        server_params.subject_alt_names.push(rcgen::SanType::URI(
            uri_san.try_into().expect("valid IA5String URI SAN"),
        ));
        let server_cert = server_params
            .signed_by(&server_key, &issuer)
            .expect("sign server cert with test CA");

        ServerPkiWithSan {
            ca_pem: ca_cert.pem(),
            server_cert_pem: server_cert.pem(),
            server_key_pem: server_key.serialize_pem(),
        }
    }

    /// Spins up a real local `tokio-rustls` TLS server presenting `pki`'s
    /// server cert (server-auth only, no client-cert requirement -- this
    /// module's own item-1 fix pins the SERVER's identity from the
    /// CLIENT/executor side; the executor presenting its own client cert
    /// is exercised separately by `build_client_config_accepts_a_real_ca_
    /// and_client_cert_pair`), returning the bound address and the
    /// spawned accept task.
    fn spawn_tls_stage_server(
        pki: &ServerPkiWithSan,
    ) -> (
        std::net::SocketAddr,
        tokio::task::JoinHandle<Result<(), std::io::Error>>,
    ) {
        let cert_path = write_temp_pem(&pki.server_cert_pem, "servercert");
        let key_path = write_temp_pem(&pki.server_key_pem, "serverkey");
        let server_certs = load_certs(&cert_path).expect("load server certs");
        let server_key = load_private_key(&key_path).expect("load server key");
        let server_config = rustls::ServerConfig::builder()
            .with_no_client_auth()
            .with_single_cert(server_certs, server_key)
            .expect("build server TLS config");
        let acceptor = tokio_rustls::TlsAcceptor::from(Arc::new(server_config));

        let std_listener =
            std::net::TcpListener::bind("127.0.0.1:0").expect("bind local test listener");
        std_listener
            .set_nonblocking(true)
            .expect("set listener non-blocking");
        let addr = std_listener.local_addr().expect("local addr");
        let listener = tokio::net::TcpListener::from_std(std_listener).expect("tokio listener");

        let task = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await?;
            // A rejected handshake (the client refuses the identity-pinned
            // cert) is expected to error here in the rejection tests below
            // -- deliberately not `.expect()`'d, just returned, so this
            // task's own `Result` lets the test assert on it without a
            // panic.
            match acceptor.accept(tcp).await {
                Ok(_tls) => Ok(()),
                Err(e) => Err(e),
            }
        });
        let _ = std::fs::remove_file(&cert_path);
        let _ = std::fs::remove_file(&key_path);
        (addr, task)
    }

    /// **The primary regression test for item 1's "unknown peer" case:** a
    /// server certificate that is otherwise perfectly valid (trusted CA,
    /// correct hostname) but carries a DIFFERENT identity than
    /// `HOST_API_STAGE_IDENTITY` must be refused -- `dial_stage` returns an
    /// error, never a usable connection.
    #[tokio::test]
    async fn dial_stage_rejects_a_server_certificate_with_an_unexpected_identity(
    ) -> Result<(), Box<dyn std::error::Error>> {
        let pki =
            generate_server_pki_with_uri_san("spiffe://penguintech.io/test/some-other-service");
        let ca_path = write_temp_pem(&pki.ca_pem, "ca");
        let (addr, server_task) = spawn_tls_stage_server(&pki);

        let mut cfg = test_config();
        cfg.stage_host_api_addr = addr.to_string();
        cfg.host_api_ca_file = Some(ca_path.clone());
        cfg.host_api_stage_identity = Some("spiffe://penguintech.io/test/svc-process".to_string());

        let result = dial_stage(&cfg).await;
        assert!(
            result.is_err(),
            "a certificate for an unlisted identity must be refused, got {result:?}"
        );
        // The server side observes the handshake fail too (the client
        // aborted after rejecting the identity) -- never silently accepted.
        assert!(server_task.await?.is_err());

        let _ = std::fs::remove_file(&ca_path);
        Ok(())
    }

    /// The positive-path counterpart: a server certificate carrying EXACTLY
    /// the configured `HOST_API_STAGE_IDENTITY` (as a SPIFFE `URI` SAN) is
    /// accepted.
    #[tokio::test]
    async fn dial_stage_accepts_a_server_certificate_with_the_expected_identity(
    ) -> Result<(), Box<dyn std::error::Error>> {
        let expected = "spiffe://penguintech.io/test/svc-process";
        let pki = generate_server_pki_with_uri_san(expected);
        let ca_path = write_temp_pem(&pki.ca_pem, "ca");
        let (addr, server_task) = spawn_tls_stage_server(&pki);

        let mut cfg = test_config();
        cfg.stage_host_api_addr = addr.to_string();
        cfg.host_api_ca_file = Some(ca_path.clone());
        cfg.host_api_stage_identity = Some(expected.to_string());

        dial_stage(&cfg).await?;
        server_task.await??;

        let _ = std::fs::remove_file(&ca_path);
        Ok(())
    }

    /// **The "plaintext" case:** an attacker (or misconfiguration) offering
    /// a bare TCP listener that never speaks TLS at all must fail the
    /// handshake -- `dial_stage` can never fall back to an unencrypted,
    /// unauthenticated connection.
    #[tokio::test]
    async fn dial_stage_fails_against_a_plaintext_listener(
    ) -> Result<(), Box<dyn std::error::Error>> {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let addr = listener.local_addr()?;
        let server_task = tokio::spawn(async move {
            // Accept the raw TCP connection and immediately drop it --
            // never performs a TLS handshake (plaintext peer).
            let _ = listener.accept().await;
        });

        let mut cfg = test_config();
        cfg.stage_host_api_addr = addr.to_string();

        let result = dial_stage(&cfg).await;
        assert!(
            result.is_err(),
            "a plaintext peer must fail the TLS handshake, never be accepted as a connection"
        );
        server_task.await?;
        Ok(())
    }

    #[test]
    fn validate_host_api_tls_requires_ca_client_cert_key_and_stage_identity_together() {
        let base = test_config();
        assert!(
            base.validate_host_api_tls().is_err(),
            "a config with none of the TLS material set must fail closed"
        );

        let mut missing_identity = base.clone();
        missing_identity.host_api_ca_file = Some(std::path::PathBuf::from("/tmp/ca.pem"));
        missing_identity.host_api_client_cert_file =
            Some(std::path::PathBuf::from("/tmp/cert.pem"));
        missing_identity.host_api_client_key_file = Some(std::path::PathBuf::from("/tmp/key.pem"));
        assert!(
            missing_identity.validate_host_api_tls().is_err(),
            "CA + client cert/key alone, with no pinned stage identity, must still fail closed"
        );

        let mut complete = missing_identity.clone();
        complete.host_api_stage_identity =
            Some("spiffe://penguintech.io/beta/svc-process".to_string());
        assert!(
            complete.validate_host_api_tls().is_ok(),
            "CA + client cert/key + stage identity together must pass"
        );
    }

    #[test]
    fn validate_host_api_tls_rejects_an_empty_stage_identity_string() {
        let mut cfg = test_config();
        cfg.host_api_ca_file = Some(std::path::PathBuf::from("/tmp/ca.pem"));
        cfg.host_api_client_cert_file = Some(std::path::PathBuf::from("/tmp/cert.pem"));
        cfg.host_api_client_key_file = Some(std::path::PathBuf::from("/tmp/key.pem"));
        cfg.host_api_stage_identity = Some(String::new());
        assert!(cfg.validate_host_api_tls().is_err());
    }
}
