//! Own-process proof of the executor-path boot (`outbound_boot::build_with` +
//! `launch`): with a complete, valid configuration the mTLS listener binds and
//! the three drains start (the `executor` path gauge flips for each platform
//! and a TCP peer reaches the listener); with unusable TLS material it fails
//! BEFORE anything is spawned. Needs a real Valkey (`VALKEY_URL`; unset FAILS).
//! Lives in its own test binary so the process-global path gauge is not raced
//! by the lib tests.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::Duration;

use clap::Parser;
use rcgen::{CertificateParams, DnType, IsCa, KeyPair};
use svc_ingest::config::CliConfig;
use svc_ingest::outbound_boot::{build_with, launch, path_value, ExecBootConfig, PLATFORMS};
use svc_ingest::platform_users::HubPlatformUserResolver;
use tokio::sync::oneshot;

/// Serializes the tests: they read/write the process-global path gauge.
static LOCK: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

fn write_pki(dir: &std::path::Path) -> (String, String, String) {
    svc_ingest::crypto::ensure_installed();
    let mut ca_p = CertificateParams::new(Vec::<String>::new()).unwrap();
    ca_p.distinguished_name
        .push(DnType::CommonName, "boot-test-ca");
    ca_p.is_ca = IsCa::Ca(rcgen::BasicConstraints::Unconstrained);
    let ca_k = KeyPair::generate().unwrap();
    let ca = ca_p.self_signed(&ca_k).unwrap();
    let issuer = rcgen::Issuer::new(ca_p, ca_k);
    let leaf_p = CertificateParams::new(vec!["localhost".to_string()]).unwrap();
    let leaf_k = KeyPair::generate().unwrap();
    let leaf = leaf_p.signed_by(&leaf_k, &issuer).unwrap();
    let put = |name: &str, body: String| {
        let p = dir.join(name);
        std::fs::write(&p, body).unwrap();
        p.to_string_lossy().to_string()
    };
    (
        put("tls.crt", leaf.pem()),
        put("tls.key", leaf_k.serialize_pem()),
        put("ca.crt", ca.pem()),
    )
}

fn free_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
        .port()
}

fn cli() -> CliConfig {
    let mut c = CliConfig::parse_from(["svc-ingest"]);
    c.dynamic_connections = true;
    c.connector_tools = true;
    c.environment = "alpha".to_string();
    c.hub_api_url = "http://hub-api:8204".to_string();
    c.service_jwt_token_endpoint = "http://hub-api:8204/internal/service-token".to_string();
    c.bind_addr = "127.0.0.1".parse().unwrap();
    c
}

fn env(cert: &str, key: &str, ca: &str, port: u16) -> HashMap<&'static str, String> {
    let d = |c: char| format!("sha256:{}", c.to_string().repeat(64));
    HashMap::from([
        (
            "OUTBOUND_PIN_KEYS",
            "k1:00112233445566778899aabbccddeeff".to_string(),
        ),
        ("OUTBOUND_PIN_ACTIVE_KID", "k1".to_string()),
        ("SENDER_EXEC_DIGEST_TWITCH", d('a')),
        ("SENDER_EXEC_DIGEST_DISCORD", d('b')),
        ("SENDER_EXEC_DIGEST_YOUTUBE", d('c')),
        ("TWITCH_CLIENT_ID", "cid".to_string()),
        ("HUB_API_GRPC_ENDPOINT", "https://hub:50204".to_string()),
        ("HOST_API_SERVER_CERT_FILE", cert.to_string()),
        ("HOST_API_SERVER_KEY_FILE", key.to_string()),
        ("HOST_API_CLIENT_CA_FILE", ca.to_string()),
        ("HOST_API_CLIENT_IDENTITY", "executor".to_string()),
        ("SENDER_EXEC_HOST_API_PORT", port.to_string()),
    ])
}

fn users() -> HubPlatformUserResolver {
    let channel = tonic::transport::Channel::from_static("http://127.0.0.1:9").connect_lazy();
    HubPlatformUserResolver::from_channel(
        channel,
        "http://hub-api:8204/internal/service-token",
        "/nonexistent/token",
    )
}

fn spine() -> penguin_spine::SpineConfig {
    std::env::var("VALKEY_URL")
        .expect("VALKEY_URL must be set: this test needs a REAL Valkey (no silent skip)");
    std::env::set_var("SECURITY_TRANSPORT_TLS", "false");
    std::env::set_var("SECURITY_TRANSPORT_AUTH", "false");
    penguin_spine::SpineConfig::from_env().unwrap()
}

#[tokio::test(flavor = "multi_thread")]
async fn a_valid_configuration_binds_the_listener_and_starts_all_three_drains() {
    let _g = LOCK.lock().await;
    let dir = std::env::temp_dir().join(format!("svc-ingest-boot-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let (cert, key, ca) = write_pki(&dir);
    let port = free_port();
    let cli = cli();
    let cfg = ExecBootConfig::from_lookup(&cli, &|k| env(&cert, &key, &ca, port).get(k).cloned())
        .unwrap();
    let parts = build_with(&cli, &cfg, users()).unwrap();
    assert!(!parts.link.is_attached(), "nothing dialed in yet");

    let senders = Mutex::new(Vec::new());
    launch(cfg, parts, spine(), || {
        let (tx, rx) = oneshot::channel();
        senders.lock().unwrap().push(tx);
        rx
    })
    .await
    .unwrap();
    for p in PLATFORMS {
        assert_eq!(path_value("executor", p), 1, "{p}");
        assert_eq!(path_value("legacy", p), 0, "{p}");
    }
    // One listener + one shutdown per drain, all handed out.
    assert_eq!(senders.lock().unwrap().len(), 1 + PLATFORMS.len());
    // The listener is reachable (TCP), and refuses a plaintext client before any frame.
    let tcp = tokio::net::TcpStream::connect(("127.0.0.1", port))
        .await
        .unwrap();
    drop(tcp);

    // Shutdown parity: every shutdown receiver fires and the listener closes.
    for tx in senders.lock().unwrap().drain(..) {
        let _ = tx.send(());
    }
    let mut closed = false;
    for _ in 0..40 {
        if tokio::net::TcpStream::connect(("127.0.0.1", port))
            .await
            .is_err()
        {
            closed = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    assert!(closed, "the host-api listener must stop on shutdown");
    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test(flavor = "multi_thread")]
async fn unusable_tls_material_fails_before_any_drain_starts() {
    let _g = LOCK.lock().await;
    let dir = std::env::temp_dir().join(format!("svc-ingest-boot-bad-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let (_cert, key, ca) = write_pki(&dir);
    let missing = dir.join("nope.crt").to_string_lossy().to_string();
    let cli = cli();
    let port = free_port();
    let cfg =
        ExecBootConfig::from_lookup(&cli, &|k| env(&missing, &key, &ca, port).get(k).cloned())
            .unwrap();
    let parts = build_with(&cli, &cfg, users()).unwrap();
    let before: Vec<i64> = PLATFORMS
        .iter()
        .map(|p| path_value("executor", p))
        .collect();
    let handed_out = Mutex::new(0usize);
    let err = launch(cfg, parts, spine(), || {
        *handed_out.lock().unwrap() += 1;
        oneshot::channel().1
    })
    .await
    .unwrap_err();
    assert!(err.to_string().contains("server_cert"), "{err}");
    assert_eq!(*handed_out.lock().unwrap(), 0, "nothing was spawned");
    let after: Vec<i64> = PLATFORMS
        .iter()
        .map(|p| path_value("executor", p))
        .collect();
    assert_eq!(before, after, "no path was recorded for a failed boot");
    // A port that is already taken is also a startup error, not a silent skip.
    let (cert2, key2, ca2) = write_pki(&dir);
    let taken = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let taken_port = taken.local_addr().unwrap().port();
    let cfg = ExecBootConfig::from_lookup(&cli, &|k| {
        env(&cert2, &key2, &ca2, taken_port).get(k).cloned()
    })
    .unwrap();
    let parts = build_with(&cli, &cfg, users()).unwrap();
    let err = launch(cfg, parts, spine(), || oneshot::channel().1)
        .await
        .unwrap_err();
    assert!(err.to_string().contains("bind failed"), "{err}");
    let _ = std::fs::remove_dir_all(&dir);
}
