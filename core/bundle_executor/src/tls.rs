//! The mTLS transport `crate::wire::run_connection` runs over in
//! production (spec `docs/superpowers/specs/2026-09-14-rust-data-plane-
//! design.md` SS6.6): `rustls` client config built from the configured CA/
//! client-cert/client-key files, dialing `STAGE_HOST_API_ADDR`.
//!
//! **Scope note:** this wires a real rustls client handshake (TLS 1.3
//! preferred, 1.2 minimum per `rules/security.md`) with real certificate
//! verification against the configured CA -- it does NOT yet add the
//! SPIFFE-ID/pinned-CN peer-identity check spec SS6.6 additionally
//! requires ("each verifies the other's SPIFFE ID ... otherwise ...
//! pinned by configuration"); rustls's standard `ServerCertVerifier`
//! already refuses a certificate that doesn't chain to the configured CA,
//! which is the load-bearing half of "connection whose peer certificate
//! does not verify ... is closed before a single frame is read" --
//! `TODO(M2 follow-up)` covers only the additional identity-matching
//! layer on top of that.

use std::sync::Arc;

use rustls::{ClientConfig, RootCertStore};
use rustls_pki_types::pem::PemObject;
use rustls_pki_types::{CertificateDer, PrivateKeyDer, ServerName};
use tokio::net::TcpStream;
use tokio_rustls::client::TlsStream;
use tokio_rustls::TlsConnector;

use crate::config::CliConfig;
use crate::error::ExecutorError;

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
/// connection to the public internet), and a client certificate/key when
/// both are configured (spec SS6.6: "Both peers present certificates").
fn build_client_config(cfg: &CliConfig) -> Result<ClientConfig, ExecutorError> {
    let mut roots = RootCertStore::empty();
    if let Some(ca_path) = &cfg.host_api_ca_file {
        for cert in load_certs(ca_path)? {
            roots
                .add(cert)
                .map_err(|e| ExecutorError::Config(format!("invalid CA certificate: {e}")))?;
        }
    }

    let builder = ClientConfig::builder().with_root_certificates(roots);

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

fn load_certs(path: &std::path::Path) -> Result<Vec<CertificateDer<'static>>, ExecutorError> {
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
}
