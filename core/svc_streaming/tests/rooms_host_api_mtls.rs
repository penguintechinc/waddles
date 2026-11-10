//! The host-API listener over a REAL rustls mTLS handshake: a client with a cert
//! signed by the configured CA gets answers; a client with no certificate (or
//! one from another CA) is refused before any request is read.

use std::sync::Arc;

use rustls::pki_types::{CertificateDer, ServerName};
use rustls_pki_types::pem::PemObject;
use svc_streaming::rooms::host_api::{build_server_config, serve, HostApiTls};
use svc_streaming::rooms::RoomService;
use svc_streaming::rtc::config::RtcConfig;
use svc_streaming::rtc::metrics::RtcMetrics;
use svc_streaming::rtc::pc_factory::PeerConnectionFactory;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio_rustls::TlsConnector;

fn pem_file(dir: &std::path::Path, name: &str, pem: &str) -> std::path::PathBuf {
    let p = dir.join(name);
    std::fs::write(&p, pem).unwrap();
    p
}

fn ca(name: &str) -> (rcgen::CertificateParams, rcgen::KeyPair, rcgen::Certificate) {
    let key = rcgen::KeyPair::generate().unwrap();
    let mut params = rcgen::CertificateParams::new(vec![name.to_string()]).unwrap();
    params.is_ca = rcgen::IsCa::Ca(rcgen::BasicConstraints::Unconstrained);
    let cert = params.self_signed(&key).unwrap();
    (params, key, cert)
}

fn client_config(ca_cert_pem: &str, client: Option<(String, String)>) -> rustls::ClientConfig {
    let mut roots = rustls::RootCertStore::empty();
    roots
        .add(CertificateDer::from_pem_slice(ca_cert_pem.as_bytes()).unwrap())
        .unwrap();
    let b = rustls::ClientConfig::builder_with_provider(Arc::new(
        rustls::crypto::ring::default_provider(),
    ))
    .with_safe_default_protocol_versions()
    .unwrap()
    .with_root_certificates(roots);
    match client {
        Some((cert, key)) => b
            .with_client_auth_cert(
                vec![CertificateDer::from_pem_slice(cert.as_bytes()).unwrap()],
                rustls_pki_types::PrivateKeyDer::from_pem_slice(key.as_bytes()).unwrap(),
            )
            .unwrap(),
        None => b.with_no_client_auth(),
    }
}

async fn post(cfg: rustls::ClientConfig, port: u16) -> std::io::Result<String> {
    let tcp = tokio::net::TcpStream::connect(("127.0.0.1", port)).await?;
    let mut tls = TlsConnector::from(Arc::new(cfg))
        .connect(ServerName::try_from("localhost").unwrap(), tcp)
        .await?;
    let body = r#"{"community_id":"7"}"#;
    tls.write_all(
        format!(
            "POST /host/v1/webrtc/room.create HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        )
        .as_bytes(),
    )
    .await?;
    let mut out = Vec::new();
    tls.read_to_end(&mut out).await?;
    Ok(String::from_utf8_lossy(&out).into_owned())
}

#[tokio::test]
async fn host_api_requires_a_client_cert_from_the_configured_ca() {
    let dir = std::env::temp_dir().join(format!("svc-streaming-mtls-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir_all(&dir).unwrap();

    let (ca_params, ca_key, ca_cert) = ca("test-ca");
    let issuer = rcgen::Issuer::from_params(&ca_params, ca_key);
    let server_key = rcgen::KeyPair::generate().unwrap();
    let server_cert = rcgen::CertificateParams::new(vec!["localhost".to_string()])
        .unwrap()
        .signed_by(&server_key, &issuer)
        .unwrap();
    let client_key = rcgen::KeyPair::generate().unwrap();
    let client_cert = rcgen::CertificateParams::new(vec!["stage".to_string()])
        .unwrap()
        .signed_by(&client_key, &issuer)
        .unwrap();
    let (rogue_params, rogue_ca_key, _rogue_cert) = ca("rogue-ca");
    let rogue_issuer = rcgen::Issuer::from_params(&rogue_params, rogue_ca_key);
    let rogue_key = rcgen::KeyPair::generate().unwrap();
    let rogue_cert = rcgen::CertificateParams::new(vec!["rogue".to_string()])
        .unwrap()
        .signed_by(&rogue_key, &rogue_issuer)
        .unwrap();

    let tls = HostApiTls {
        server_cert: pem_file(&dir, "server.pem", &server_cert.pem()),
        server_key: pem_file(&dir, "server.key", &server_key.serialize_pem()),
        client_ca: pem_file(&dir, "ca.pem", &ca_cert.pem()),
        port: 0,
    };
    let server_config = build_server_config(&tls).expect("server config");

    let factory = Arc::new(
        PeerConnectionFactory::new(RtcConfig {
            bind_ip: "127.0.0.1".parse().unwrap(),
            udp_port_range: (41950, 41960),
            nat_1to1_ip: None,
        })
        .unwrap(),
    );
    let metrics = RtcMetrics::register(&prometheus::Registry::new()).unwrap();
    let svc = Arc::new(RoomService::new(factory, metrics, None, "http://x".into()));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let server = tokio::spawn(serve(listener, server_config, svc));

    // Valid client cert: gets a real room back.
    let ok = post(
        client_config(
            &ca_cert.pem(),
            Some((client_cert.pem(), client_key.serialize_pem())),
        ),
        port,
    )
    .await
    .expect("mTLS client is served");
    assert!(ok.starts_with("HTTP/1.1 200"), "{ok}");
    assert!(ok.contains("room_id"), "{ok}");

    // No client cert / cert from a rogue CA: no response ever produced.
    for cfg in [
        client_config(&ca_cert.pem(), None),
        client_config(
            &ca_cert.pem(),
            Some((rogue_cert.pem(), rogue_key.serialize_pem())),
        ),
    ] {
        match post(cfg, port).await {
            Err(_) => {}
            Ok(body) => assert!(
                !body.contains("HTTP/1.1 200"),
                "unauthenticated peer served: {body}"
            ),
        }
    }
    server.abort();
    let _ = std::fs::remove_dir_all(&dir);
}
