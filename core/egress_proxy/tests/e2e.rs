//! End-to-end tests against the crate's public API: `proxy::validate` for
//! the auth/assertion/SSRF decision pipeline (no network I/O), and a real
//! bound server for the CONNECT tunnel test.

use std::collections::HashMap;
use std::io;
use std::net::{IpAddr, SocketAddr};
use std::sync::Mutex;
use std::time::Duration;

use async_trait::async_trait;
use egress_proxy::assertion::EgressAssertion;
use egress_proxy::config::Config;
use egress_proxy::dns::Resolver;
use egress_proxy::ip_policy::DestinationCategory;
use egress_proxy::limits::TenantLimiter;
use egress_proxy::metrics::Metrics;
use egress_proxy::proxy::{self, ProxyError, ProxyState, ValidationDeps};
use jsonwebtoken::{Algorithm, DecodingKey, EncodingKey, Header};
use service_auth::{ServiceClaims, TrustBundle};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};

// PKCS8/SPKI-DER-encoded Ed25519 test keypair for the machine JWT
// (identical fixture to core/service_auth's own test module -- test-only,
// never used outside this file). Simulates hub-api's machine-JWT signer.
const MACHINE_PRIV_DER: &[u8] = &[
    48, 46, 2, 1, 0, 48, 5, 6, 3, 43, 101, 112, 4, 34, 4, 32, 1, 204, 5, 142, 35, 153, 231, 38,
    150, 122, 1, 218, 34, 237, 70, 125, 233, 62, 126, 103, 151, 16, 11, 238, 95, 122, 209, 74, 183,
    9, 171, 161,
];
const MACHINE_PUB_RAW: &[u8] = &[
    169, 90, 255, 23, 51, 151, 156, 147, 56, 247, 214, 168, 76, 160, 67, 99, 211, 238, 208, 5, 69,
    236, 245, 115, 4, 81, 1, 42, 23, 107, 4, 187,
];

// A second, distinct Ed25519 test keypair for the allowlist-assertion
// signer (generated once with `openssl genpkey -algorithm ed25519` /
// `openssl pkey -pubout`, test-only) -- deliberately a different key from
// the machine-JWT signer above, since production uses two separate
// signers (see `assertion` module doc).
const ASSERTION_PRIV_DER: &[u8] = &[
    48, 46, 2, 1, 0, 48, 5, 6, 3, 43, 101, 112, 4, 34, 4, 32, 77, 65, 83, 87, 2, 13, 121, 231, 125,
    130, 252, 109, 198, 133, 79, 69, 66, 86, 80, 167, 34, 213, 224, 124, 197, 5, 224, 50, 184, 86,
    125, 220,
];
const ASSERTION_PUB_RAW: &[u8] = &[
    189, 125, 13, 22, 108, 145, 95, 109, 95, 172, 1, 244, 132, 99, 244, 47, 253, 151, 89, 153, 142,
    44, 125, 48, 4, 254, 117, 34, 146, 239, 144, 185,
];

struct StaticTrustBundle(Mutex<HashMap<String, DecodingKey>>);

#[async_trait]
impl TrustBundle for StaticTrustBundle {
    async fn public_key(&self, kid: &str) -> Option<DecodingKey> {
        self.0.lock().unwrap().get(kid).cloned()
    }
}

fn machine_keypair() -> (EncodingKey, DecodingKey) {
    (
        EncodingKey::from_ed_der(MACHINE_PRIV_DER),
        DecodingKey::from_ed_der(MACHINE_PUB_RAW),
    )
}

fn assertion_keypair() -> (EncodingKey, DecodingKey) {
    (
        EncodingKey::from_ed_der(ASSERTION_PRIV_DER),
        DecodingKey::from_ed_der(ASSERTION_PUB_RAW),
    )
}

fn now_secs() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs()
}

fn make_machine_jwt(enc: &EncodingKey, aud: &str, sub: &str, scope: &str) -> String {
    let now = now_secs();
    let claims = ServiceClaims {
        iss: "hub-api".into(),
        aud: aud.into(),
        sub: sub.into(),
        scope: scope.into(),
        iat: now,
        nbf: now,
        exp: now + 300,
        jti: "test-jti".into(),
    };
    let mut header = Header::new(Algorithm::EdDSA);
    header.kid = Some("k1".into());
    jsonwebtoken::encode(&header, &claims, enc).unwrap()
}

fn make_assertion_jwt(enc: &EncodingKey, assertion: &EgressAssertion) -> String {
    let header = Header::new(Algorithm::EdDSA);
    jsonwebtoken::encode(&header, assertion, enc).unwrap()
}

fn assertion(category: DestinationCategory, destination: &str) -> EgressAssertion {
    let now = now_secs();
    EgressAssertion {
        tenant: "tenant-1".into(),
        community: "community-1".into(),
        app: "app-1".into(),
        category,
        destination: destination.to_string(),
        iat: now,
        exp: now + 30,
    }
}

fn test_config(allowed_ports: Vec<u16>) -> Config {
    Config {
        listen_port: 0,
        metrics_port: 0,
        allowed_ports,
        allowlist_signing_key_path: String::new(),
        deny_cidrs: vec![],
        deny_cluster_cidrs: vec![
            "10.244.0.0/16".parse().unwrap(),
            "10.96.0.0/12".parse().unwrap(),
        ],
        machine_jwt_jwks_url: String::new(),
        machine_jwt_audience: "egress-proxy".into(),
        machine_jwt_trusted_issuers: vec!["hub-api".into()],
        machine_jwt_required_scope: "egress:connect".into(),
        allowed_caller_services: vec![
            "svc-ingest".into(),
            "svc-process".into(),
            "svc-action".into(),
        ],
        per_tenant_max_connections: 50,
        per_tenant_bandwidth_bytes_per_sec: 100_000_000,
        connect_timeout: Duration::from_secs(5),
        assertion_max_ttl: Duration::from_secs(60),
    }
}

fn headers_with(auth: Option<&str>, assertion_token: Option<&str>) -> http::HeaderMap {
    let mut headers = http::HeaderMap::new();
    if let Some(auth) = auth {
        headers.insert(
            http::header::AUTHORIZATION,
            format!("Bearer {auth}").parse().unwrap(),
        );
    }
    if let Some(token) = assertion_token {
        headers.insert("x-waddles-egress-assertion", token.parse().unwrap());
    }
    headers
}

/// A resolver whose answers are pre-programmed per hostname -- used to
/// simulate DNS rebinding (an FQDN that legitimately resolves to a
/// private/internal address after the fact) without any real DNS lookup.
struct FakeResolver(HashMap<&'static str, Vec<IpAddr>>);

#[async_trait]
impl Resolver for FakeResolver {
    async fn resolve(&self, host: &str, port: u16) -> io::Result<Vec<SocketAddr>> {
        Ok(self
            .0
            .get(host)
            .cloned()
            .unwrap_or_default()
            .into_iter()
            .map(|ip| SocketAddr::new(ip, port))
            .collect())
    }
}

fn valid_machine_jwt(enc: &EncodingKey) -> String {
    make_machine_jwt(
        enc,
        "egress-proxy",
        "spiffe://penguintech.io/alpha/svc-process",
        "egress:connect",
    )
}

#[tokio::test]
async fn unauthenticated_caller_is_rejected() {
    let (_menc, mdec) = machine_keypair();
    let (_aenc, adec) = assertion_keypair();
    let bundle = StaticTrustBundle(Mutex::new(HashMap::from([("k1".to_string(), mdec)])));
    let cfg = test_config(vec![443]);
    let resolver = FakeResolver(HashMap::new());

    // No Authorization header at all.
    let headers = headers_with(None, None);
    let deps = ValidationDeps {
        trust_bundle: &bundle,
        assertion_key: &adec,
        cfg: &cfg,
        cluster_cidrs: &cfg.deny_cluster_cidrs,
        resolver: &resolver,
    };
    let err = proxy::validate(&headers, "example.com", 443, deps)
        .await
        .unwrap_err();
    assert!(
        matches!(err, ProxyError::Auth(_)),
        "expected Auth error, got {err:?}"
    );
}

#[tokio::test]
async fn destination_mismatching_assertion_is_denied() {
    let (menc, mdec) = machine_keypair();
    let (aenc, adec) = assertion_keypair();
    let bundle = StaticTrustBundle(Mutex::new(HashMap::from([("k1".to_string(), mdec)])));
    let cfg = test_config(vec![443]);
    let resolver = FakeResolver(HashMap::from([(
        "evil.example.com",
        vec!["93.184.216.34".parse().unwrap()],
    )]));

    let machine_jwt = valid_machine_jwt(&menc);
    let grant = assertion(DestinationCategory::Fqdn, "allowed.example.com");
    let assertion_jwt = make_assertion_jwt(&aenc, &grant);
    let headers = headers_with(Some(&machine_jwt), Some(&assertion_jwt));

    let deps = ValidationDeps {
        trust_bundle: &bundle,
        assertion_key: &adec,
        cfg: &cfg,
        cluster_cidrs: &cfg.deny_cluster_cidrs,
        resolver: &resolver,
    };
    // Requested host differs from what the assertion actually grants.
    let err = proxy::validate(&headers, "evil.example.com", 443, deps)
        .await
        .unwrap_err();
    assert!(
        matches!(err, ProxyError::DestinationMismatch),
        "expected DestinationMismatch, got {err:?}"
    );
}

#[tokio::test]
async fn dns_rebinding_to_a_private_address_is_blocked() {
    let (menc, mdec) = machine_keypair();
    let (aenc, adec) = assertion_keypair();
    let bundle = StaticTrustBundle(Mutex::new(HashMap::from([("k1".to_string(), mdec)])));
    let cfg = test_config(vec![443]);
    // "safe.example.com" is exactly what the assertion grants, but the
    // resolver returns a private address for it -- simulating a rebind
    // between grant time and connect time.
    let resolver = FakeResolver(HashMap::from([(
        "safe.example.com",
        vec!["10.1.2.3".parse().unwrap()],
    )]));

    let machine_jwt = valid_machine_jwt(&menc);
    let grant = assertion(DestinationCategory::Fqdn, "safe.example.com");
    let assertion_jwt = make_assertion_jwt(&aenc, &grant);
    let headers = headers_with(Some(&machine_jwt), Some(&assertion_jwt));

    let deps = ValidationDeps {
        trust_bundle: &bundle,
        assertion_key: &adec,
        cfg: &cfg,
        cluster_cidrs: &cfg.deny_cluster_cidrs,
        resolver: &resolver,
    };
    let err = proxy::validate(&headers, "safe.example.com", 443, deps)
        .await
        .unwrap_err();
    assert!(
        matches!(err, ProxyError::Denied("private")),
        "expected Denied(\"private\"), got {err:?}"
    );
}

#[tokio::test]
async fn private_ip_blocked_without_private_ip_assertion_allowed_with_one() {
    let (menc, mdec) = machine_keypair();
    let (aenc, adec) = assertion_keypair();
    let bundle = StaticTrustBundle(Mutex::new(HashMap::from([("k1".to_string(), mdec)])));
    let cfg = test_config(vec![443]);
    let resolver = FakeResolver(HashMap::new());
    let machine_jwt = valid_machine_jwt(&menc);

    // Without a private-ip grant: PublicIp category naming this exact
    // private literal is still denied by the shared SSRF check.
    let public_grant = assertion(DestinationCategory::PublicIp, "192.168.1.50");
    let public_jwt = make_assertion_jwt(&aenc, &public_grant);
    let headers = headers_with(Some(&machine_jwt), Some(&public_jwt));
    let deps = ValidationDeps {
        trust_bundle: &bundle,
        assertion_key: &adec,
        cfg: &cfg,
        cluster_cidrs: &cfg.deny_cluster_cidrs,
        resolver: &resolver,
    };
    let err = proxy::validate(&headers, "192.168.1.50", 443, deps)
        .await
        .unwrap_err();
    assert!(
        matches!(err, ProxyError::Denied("private")),
        "expected Denied(\"private\"), got {err:?}"
    );

    // With a private-ip grant covering that address: permitted.
    let private_grant = assertion(DestinationCategory::PrivateIp, "192.168.1.0/24");
    let private_jwt = make_assertion_jwt(&aenc, &private_grant);
    let headers = headers_with(Some(&machine_jwt), Some(&private_jwt));
    let deps = ValidationDeps {
        trust_bundle: &bundle,
        assertion_key: &adec,
        cfg: &cfg,
        cluster_cidrs: &cfg.deny_cluster_cidrs,
        resolver: &resolver,
    };
    let target = proxy::validate(&headers, "192.168.1.50", 443, deps)
        .await
        .expect("private-ip grant permits it");
    assert_eq!(
        target.addr,
        "192.168.1.50:443".parse::<SocketAddr>().unwrap()
    );
}

#[tokio::test]
async fn metadata_and_cluster_cidrs_are_always_blocked_even_with_private_ip_grant() {
    let (menc, mdec) = machine_keypair();
    let (aenc, adec) = assertion_keypair();
    let bundle = StaticTrustBundle(Mutex::new(HashMap::from([("k1".to_string(), mdec)])));
    let cfg = test_config(vec![443]);
    let resolver = FakeResolver(HashMap::new());
    let machine_jwt = valid_machine_jwt(&menc);

    // Cloud metadata, even under a private-ip grant naming it exactly.
    let metadata_grant = assertion(DestinationCategory::PrivateIp, "169.254.169.254/32");
    let metadata_jwt = make_assertion_jwt(&aenc, &metadata_grant);
    let headers = headers_with(Some(&machine_jwt), Some(&metadata_jwt));
    let deps = ValidationDeps {
        trust_bundle: &bundle,
        assertion_key: &adec,
        cfg: &cfg,
        cluster_cidrs: &cfg.deny_cluster_cidrs,
        resolver: &resolver,
    };
    let err = proxy::validate(&headers, "169.254.169.254", 443, deps)
        .await
        .unwrap_err();
    assert!(
        matches!(err, ProxyError::Denied("cloud_metadata")),
        "expected cloud_metadata deny, got {err:?}"
    );

    // The configured cluster pod CIDR, even under a private-ip grant.
    let cluster_grant = assertion(DestinationCategory::PrivateIp, "10.244.1.5/32");
    let cluster_jwt = make_assertion_jwt(&aenc, &cluster_grant);
    let headers = headers_with(Some(&machine_jwt), Some(&cluster_jwt));
    let deps = ValidationDeps {
        trust_bundle: &bundle,
        assertion_key: &adec,
        cfg: &cfg,
        cluster_cidrs: &cfg.deny_cluster_cidrs,
        resolver: &resolver,
    };
    let err = proxy::validate(&headers, "10.244.1.5", 443, deps)
        .await
        .unwrap_err();
    assert!(
        matches!(err, ProxyError::Denied("cluster_cidr")),
        "expected cluster_cidr deny, got {err:?}"
    );
}

/// Finds a real, non-loopback local IPv4 address to bind the fake
/// upstream server on -- CONNECT tunnels must dial a non-loopback address
/// (loopback is unconditionally denied by design), so the target for this
/// test is granted as a `private-ip` destination on whatever address this
/// host actually routes through.
fn local_non_loopback_ip() -> IpAddr {
    let socket = std::net::UdpSocket::bind("0.0.0.0:0").expect("bind ephemeral udp socket");
    socket
        .connect("8.8.8.8:80")
        .expect("route lookup (no packet sent)");
    socket.local_addr().expect("local addr").ip()
}

#[tokio::test]
async fn connect_tunnel_relays_bytes_end_to_end() {
    let local_ip = local_non_loopback_ip();

    // Fake upstream: echoes back whatever it reads.
    let upstream_listener = TcpListener::bind((local_ip, 0))
        .await
        .expect("bind upstream");
    let upstream_addr = upstream_listener.local_addr().unwrap();
    tokio::spawn(async move {
        if let Ok((mut sock, _)) = upstream_listener.accept().await {
            let mut buf = [0u8; 1024];
            if let Ok(n) = sock.read(&mut buf).await {
                let _ = sock.write_all(&buf[..n]).await;
            }
        }
    });

    let (menc, mdec) = machine_keypair();
    let (aenc, adec) = assertion_keypair();
    let bundle: std::sync::Arc<dyn TrustBundle> = std::sync::Arc::new(StaticTrustBundle(
        Mutex::new(HashMap::from([("k1".to_string(), mdec)])),
    ));
    let mut cfg = test_config(vec![upstream_addr.port()]);
    cfg.deny_cluster_cidrs = vec![]; // this host's own address must not collide with a cluster CIDR
    let state = std::sync::Arc::new(ProxyState {
        cfg: std::sync::Arc::new(cfg),
        trust_bundle: bundle,
        assertion_key: adec,
        cluster_cidrs: vec![],
        resolver: std::sync::Arc::new(egress_proxy::dns::TokioResolver),
        limiter: TenantLimiter::new(50, 100_000_000),
        metrics: Metrics::new(),
    });

    let proxy_listener = TcpListener::bind(("127.0.0.1", 0))
        .await
        .expect("bind proxy");
    let proxy_addr = proxy_listener.local_addr().unwrap();
    tokio::spawn(egress_proxy::serve_proxy(proxy_listener, state));

    let machine_jwt = valid_machine_jwt(&menc);
    let grant = assertion(DestinationCategory::PrivateIp, &format!("{local_ip}/32"));
    let assertion_jwt = make_assertion_jwt(&aenc, &grant);

    let mut client = TcpStream::connect(proxy_addr)
        .await
        .expect("connect to proxy");
    let connect_req = format!(
        "CONNECT {local_ip}:{port} HTTP/1.1\r\nHost: {local_ip}:{port}\r\nAuthorization: Bearer {machine_jwt}\r\nX-Waddles-Egress-Assertion: {assertion_jwt}\r\n\r\n",
        port = upstream_addr.port()
    );
    client.write_all(connect_req.as_bytes()).await.unwrap();

    // Read the CONNECT response headers.
    let mut resp = Vec::new();
    let mut buf = [0u8; 256];
    loop {
        let n = client.read(&mut buf).await.unwrap();
        resp.extend_from_slice(&buf[..n]);
        if resp.windows(4).any(|w| w == b"\r\n\r\n") {
            break;
        }
    }
    let resp_str = String::from_utf8_lossy(&resp);
    assert!(
        resp_str.starts_with("HTTP/1.1 200"),
        "expected 200 for CONNECT, got: {resp_str}"
    );

    // Now the connection is a raw tunnel -- send a payload and expect the
    // echo server's reply to come straight back through it.
    client.write_all(b"hello through the tunnel").await.unwrap();
    let mut echoed = [0u8; 64];
    let n = client.read(&mut echoed).await.unwrap();
    assert_eq!(&echoed[..n], b"hello through the tunnel");
}
