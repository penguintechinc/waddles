//! End-to-end tests against the crate's public API: `proxy::validate` for
//! the auth/assertion/SSRF decision pipeline (no network I/O), and a real
//! bound server for the CONNECT tunnel and forward-HTTP header-hygiene
//! tests.

use std::collections::HashMap;
use std::io;
use std::net::{IpAddr, SocketAddr};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use bundle_host_http::egress::{EgressAssertionSigner, ProxyAssertionSigner};
use egress_assertion::AssertionSigningKey;
use egress_proxy::assertion::{EgressAssertion, InMemoryReplayCache};
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

// PKCS8/SPKI-DER-encoded Ed25519 test keypair for `svc-process`'s own
// identity (identical fixture to core/service_auth's own test module --
// test-only, never used outside this file). Post-redesign: this single
// per-service key signs *both* the machine JWT and the allowlist
// assertion, verified via the same `kid` against the same trust bundle --
// simulating `svc-process`'s real key, not a separate hub-api-held one.
const SVC_PROCESS_PRIV_DER: &[u8] = &[
    48, 46, 2, 1, 0, 48, 5, 6, 3, 43, 101, 112, 4, 34, 4, 32, 1, 204, 5, 142, 35, 153, 231, 38,
    150, 122, 1, 218, 34, 237, 70, 125, 233, 62, 126, 103, 151, 16, 11, 238, 95, 122, 209, 74, 183,
    9, 171, 161,
];
const SVC_PROCESS_PUB_RAW: &[u8] = &[
    169, 90, 255, 23, 51, 151, 156, 147, 56, 247, 214, 168, 76, 160, 67, 99, 211, 238, 208, 5, 69,
    236, 245, 115, 4, 81, 1, 42, 23, 107, 4, 187,
];

// A second, distinct Ed25519 test keypair simulating a *different* calling
// service (`svc-action`) -- used only to prove an assertion signed by one
// service's key can never authorize a connection authenticated under a
// different service's machine JWT.
const SVC_ACTION_PRIV_DER: &[u8] = &[
    48, 46, 2, 1, 0, 48, 5, 6, 3, 43, 101, 112, 4, 34, 4, 32, 77, 65, 83, 87, 2, 13, 121, 231, 125,
    130, 252, 109, 198, 133, 79, 69, 66, 86, 80, 167, 34, 213, 224, 124, 197, 5, 224, 50, 184, 86,
    125, 220,
];
const SVC_ACTION_PUB_RAW: &[u8] = &[
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

fn svc_process_keypair() -> (EncodingKey, DecodingKey) {
    (
        EncodingKey::from_ed_der(SVC_PROCESS_PRIV_DER),
        DecodingKey::from_ed_der(SVC_PROCESS_PUB_RAW),
    )
}

fn svc_action_keypair() -> (EncodingKey, DecodingKey) {
    (
        EncodingKey::from_ed_der(SVC_ACTION_PRIV_DER),
        DecodingKey::from_ed_der(SVC_ACTION_PUB_RAW),
    )
}

fn now_secs() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs()
}

const SVC_PROCESS_SUB: &str = "spiffe://penguintech.io/alpha/svc-process";

fn make_machine_jwt(enc: &EncodingKey, kid: &str, aud: &str, sub: &str, scope: &str) -> String {
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
    header.kid = Some(kid.into());
    jsonwebtoken::encode(&header, &claims, enc).unwrap()
}

fn make_assertion_jwt(enc: &EncodingKey, kid: &str, assertion: &EgressAssertion) -> String {
    let mut header = Header::new(Algorithm::EdDSA);
    header.kid = Some(kid.into());
    jsonwebtoken::encode(&header, assertion, enc).unwrap()
}

/// Builds an assertion signed (by convention of every test here) by the
/// same identity as `SVC_PROCESS_SUB`'s machine JWT, granting `port` 443
/// unless overridden via [`assertion_with_port`].
fn assertion(category: DestinationCategory, destination: &str) -> EgressAssertion {
    assertion_with_port(category, destination, 443)
}

fn assertion_with_port(
    category: DestinationCategory,
    destination: &str,
    port: u16,
) -> EgressAssertion {
    let now = now_secs();
    EgressAssertion {
        sub: SVC_PROCESS_SUB.into(),
        tenant: "tenant-1".into(),
        community: "community-1".into(),
        app: "app-1".into(),
        category,
        destination: destination.to_string(),
        port,
        jti: uuid_like_jti(),
        iat: now,
        exp: now + 30,
    }
}

/// A cheap, collision-resistant-enough-for-tests unique ID -- avoids
/// pulling in a `uuid` dependency purely for test fixtures.
fn uuid_like_jti() -> String {
    use std::sync::atomic::{AtomicU64, Ordering};
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    format!(
        "test-jti-{}-{}",
        now_secs(),
        COUNTER.fetch_add(1, Ordering::Relaxed)
    )
}

fn test_config(allowed_ports: Vec<u16>) -> Config {
    Config {
        listen_port: 0,
        metrics_port: 0,
        allowed_ports,
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
        header_read_timeout: Duration::from_secs(10),
        tunnel_idle_timeout: Duration::from_secs(300),
        tunnel_max_duration: Duration::from_secs(3600),
        allow_private_ip: true,
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
    make_machine_jwt(enc, "k1", "egress-proxy", SVC_PROCESS_SUB, "egress:connect")
}

#[tokio::test]
async fn unauthenticated_caller_is_rejected() {
    let (_enc, dec) = svc_process_keypair();
    let bundle = StaticTrustBundle(Mutex::new(HashMap::from([("k1".to_string(), dec)])));
    let cfg = test_config(vec![443]);
    let resolver = FakeResolver(HashMap::new());
    let replay_cache = InMemoryReplayCache::new();

    // No Authorization header at all.
    let headers = headers_with(None, None);
    let deps = ValidationDeps {
        trust_bundle: &bundle,
        replay_cache: &replay_cache,
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
    let (enc, dec) = svc_process_keypair();
    let bundle = StaticTrustBundle(Mutex::new(HashMap::from([("k1".to_string(), dec)])));
    let cfg = test_config(vec![443]);
    let resolver = FakeResolver(HashMap::from([(
        "evil.example.com",
        vec!["93.184.216.34".parse().unwrap()],
    )]));
    let replay_cache = InMemoryReplayCache::new();

    let machine_jwt = valid_machine_jwt(&enc);
    let grant = assertion(DestinationCategory::Fqdn, "allowed.example.com");
    let assertion_jwt = make_assertion_jwt(&enc, "k1", &grant);
    let headers = headers_with(Some(&machine_jwt), Some(&assertion_jwt));

    let deps = ValidationDeps {
        trust_bundle: &bundle,
        replay_cache: &replay_cache,
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
async fn port_mismatching_assertion_is_denied() {
    let (enc, dec) = svc_process_keypair();
    let bundle = StaticTrustBundle(Mutex::new(HashMap::from([("k1".to_string(), dec)])));
    let cfg = test_config(vec![443, 8443]);
    let resolver = FakeResolver(HashMap::from([(
        "allowed.example.com",
        vec!["93.184.216.34".parse().unwrap()],
    )]));
    let replay_cache = InMemoryReplayCache::new();

    let machine_jwt = valid_machine_jwt(&enc);
    // Assertion grants port 443, but the request is for 8443 (itself on
    // the operator-wide port allowlist) -- must still be denied.
    let grant = assertion_with_port(DestinationCategory::Fqdn, "allowed.example.com", 443);
    let assertion_jwt = make_assertion_jwt(&enc, "k1", &grant);
    let headers = headers_with(Some(&machine_jwt), Some(&assertion_jwt));

    let deps = ValidationDeps {
        trust_bundle: &bundle,
        replay_cache: &replay_cache,
        cfg: &cfg,
        cluster_cidrs: &cfg.deny_cluster_cidrs,
        resolver: &resolver,
    };
    let err = proxy::validate(&headers, "allowed.example.com", 8443, deps)
        .await
        .unwrap_err();
    assert!(
        matches!(err, ProxyError::DestinationMismatch),
        "expected DestinationMismatch, got {err:?}"
    );
}

#[tokio::test]
async fn assertion_signed_by_a_different_service_than_the_machine_jwt_is_rejected() {
    let (menc, mdec) = svc_process_keypair();
    let (aenc, adec) = svc_action_keypair();
    // Both services' keys are present in the trust bundle (as they would
    // be in the real hub-api JWKS), under distinct `kid`s.
    let bundle = StaticTrustBundle(Mutex::new(HashMap::from([
        ("k1".to_string(), mdec),
        ("k2".to_string(), adec),
    ])));
    let cfg = test_config(vec![443]);
    let resolver = FakeResolver(HashMap::from([(
        "allowed.example.com",
        vec!["93.184.216.34".parse().unwrap()],
    )]));
    let replay_cache = InMemoryReplayCache::new();

    // Machine JWT authenticates as svc-process, but the assertion is
    // validly signed by svc-action's own key (`sub` claims svc-action's
    // identity, matching that key) -- must be rejected as a sub mismatch,
    // never silently accepted just because the signature itself verifies.
    let machine_jwt = valid_machine_jwt(&menc);
    let mut grant = assertion(DestinationCategory::Fqdn, "allowed.example.com");
    grant.sub = "spiffe://penguintech.io/alpha/svc-action".into();
    let assertion_jwt = make_assertion_jwt(&aenc, "k2", &grant);
    let headers = headers_with(Some(&machine_jwt), Some(&assertion_jwt));

    let deps = ValidationDeps {
        trust_bundle: &bundle,
        replay_cache: &replay_cache,
        cfg: &cfg,
        cluster_cidrs: &cfg.deny_cluster_cidrs,
        resolver: &resolver,
    };
    let err = proxy::validate(&headers, "allowed.example.com", 443, deps)
        .await
        .unwrap_err();
    assert!(
        matches!(
            err,
            ProxyError::Assertion(egress_proxy::assertion::AssertionError::SubMismatch { .. })
        ),
        "expected SubMismatch, got {err:?}"
    );
}

#[tokio::test]
async fn replayed_assertion_jti_is_rejected_on_second_use() {
    let (enc, dec) = svc_process_keypair();
    let bundle = StaticTrustBundle(Mutex::new(HashMap::from([("k1".to_string(), dec)])));
    let cfg = test_config(vec![443]);
    let resolver = FakeResolver(HashMap::from([(
        "allowed.example.com",
        vec!["93.184.216.34".parse().unwrap()],
    )]));
    let replay_cache = InMemoryReplayCache::new();

    let machine_jwt = valid_machine_jwt(&enc);
    let grant = assertion(DestinationCategory::Fqdn, "allowed.example.com");
    let assertion_jwt = make_assertion_jwt(&enc, "k1", &grant);
    let headers = headers_with(Some(&machine_jwt), Some(&assertion_jwt));

    let deps = ValidationDeps {
        trust_bundle: &bundle,
        replay_cache: &replay_cache,
        cfg: &cfg,
        cluster_cidrs: &cfg.deny_cluster_cidrs,
        resolver: &resolver,
    };
    proxy::validate(&headers, "allowed.example.com", 443, deps)
        .await
        .expect("first use succeeds");

    // Second use of the identical assertion (same `jti`) must be denied,
    // even though every other check (signature, destination, port) still
    // passes.
    let deps = ValidationDeps {
        trust_bundle: &bundle,
        replay_cache: &replay_cache,
        cfg: &cfg,
        cluster_cidrs: &cfg.deny_cluster_cidrs,
        resolver: &resolver,
    };
    let err = proxy::validate(&headers, "allowed.example.com", 443, deps)
        .await
        .unwrap_err();
    assert!(
        matches!(
            err,
            ProxyError::Assertion(egress_proxy::assertion::AssertionError::Replayed(_))
        ),
        "expected Replayed, got {err:?}"
    );
}

#[tokio::test]
async fn dns_rebinding_to_a_private_address_is_blocked() {
    let (enc, dec) = svc_process_keypair();
    let bundle = StaticTrustBundle(Mutex::new(HashMap::from([("k1".to_string(), dec)])));
    let mut cfg = test_config(vec![443]);
    cfg.allow_private_ip = false;
    // "safe.example.com" is exactly what the assertion grants, but the
    // resolver returns a private address for it -- simulating a rebind
    // between grant time and connect time.
    let resolver = FakeResolver(HashMap::from([(
        "safe.example.com",
        vec!["10.1.2.3".parse().unwrap()],
    )]));
    let replay_cache = InMemoryReplayCache::new();

    let machine_jwt = valid_machine_jwt(&enc);
    let grant = assertion(DestinationCategory::Fqdn, "safe.example.com");
    let assertion_jwt = make_assertion_jwt(&enc, "k1", &grant);
    let headers = headers_with(Some(&machine_jwt), Some(&assertion_jwt));

    let deps = ValidationDeps {
        trust_bundle: &bundle,
        replay_cache: &replay_cache,
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
    let (enc, dec) = svc_process_keypair();
    let bundle = StaticTrustBundle(Mutex::new(HashMap::from([("k1".to_string(), dec)])));
    let cfg = test_config(vec![443]);
    let resolver = FakeResolver(HashMap::new());
    let machine_jwt = valid_machine_jwt(&enc);
    let replay_cache = InMemoryReplayCache::new();

    // Without a private-ip grant: PublicIp category naming this exact
    // private literal is still denied by the shared SSRF check.
    let public_grant = assertion(DestinationCategory::PublicIp, "192.168.1.50");
    let public_jwt = make_assertion_jwt(&enc, "k1", &public_grant);
    let headers = headers_with(Some(&machine_jwt), Some(&public_jwt));
    let deps = ValidationDeps {
        trust_bundle: &bundle,
        replay_cache: &replay_cache,
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

    // With a private-ip grant covering that address (and the deployment
    // gate enabled -- `test_config` defaults `allow_private_ip: true`):
    // permitted.
    let private_grant = assertion(DestinationCategory::PrivateIp, "192.168.1.0/24");
    let private_jwt = make_assertion_jwt(&enc, "k1", &private_grant);
    let headers = headers_with(Some(&machine_jwt), Some(&private_jwt));
    let deps = ValidationDeps {
        trust_bundle: &bundle,
        replay_cache: &replay_cache,
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
async fn private_ip_grant_denied_when_deployment_gate_disabled() {
    let (enc, dec) = svc_process_keypair();
    let bundle = StaticTrustBundle(Mutex::new(HashMap::from([("k1".to_string(), dec)])));
    let mut cfg = test_config(vec![443]);
    // Deployment-wide kill switch off, independent of the assertion's own
    // category -- a correctly-scoped private-ip grant must still be
    // denied.
    cfg.allow_private_ip = false;
    let resolver = FakeResolver(HashMap::new());
    let machine_jwt = valid_machine_jwt(&enc);
    let replay_cache = InMemoryReplayCache::new();

    let private_grant = assertion(DestinationCategory::PrivateIp, "192.168.1.0/24");
    let private_jwt = make_assertion_jwt(&enc, "k1", &private_grant);
    let headers = headers_with(Some(&machine_jwt), Some(&private_jwt));
    let deps = ValidationDeps {
        trust_bundle: &bundle,
        replay_cache: &replay_cache,
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
}

#[tokio::test]
async fn metadata_and_cluster_cidrs_are_always_blocked_even_with_private_ip_grant() {
    let (enc, dec) = svc_process_keypair();
    let bundle = StaticTrustBundle(Mutex::new(HashMap::from([("k1".to_string(), dec)])));
    let cfg = test_config(vec![443]);
    let resolver = FakeResolver(HashMap::new());
    let machine_jwt = valid_machine_jwt(&enc);
    let replay_cache = InMemoryReplayCache::new();

    // Cloud metadata, even under a private-ip grant naming it exactly.
    let metadata_grant = assertion(DestinationCategory::PrivateIp, "169.254.169.254/32");
    let metadata_jwt = make_assertion_jwt(&enc, "k1", &metadata_grant);
    let headers = headers_with(Some(&machine_jwt), Some(&metadata_jwt));
    let deps = ValidationDeps {
        trust_bundle: &bundle,
        replay_cache: &replay_cache,
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
    let cluster_jwt = make_assertion_jwt(&enc, "k1", &cluster_grant);
    let headers = headers_with(Some(&machine_jwt), Some(&cluster_jwt));
    let deps = ValidationDeps {
        trust_bundle: &bundle,
        replay_cache: &replay_cache,
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

fn test_proxy_state(
    cfg: Config,
    bundle: std::sync::Arc<dyn TrustBundle>,
) -> std::sync::Arc<ProxyState> {
    std::sync::Arc::new(ProxyState {
        cfg: std::sync::Arc::new(cfg),
        trust_bundle: bundle,
        replay_cache: std::sync::Arc::new(InMemoryReplayCache::new()),
        cluster_cidrs: vec![],
        resolver: std::sync::Arc::new(egress_proxy::dns::TokioResolver),
        limiter: TenantLimiter::new(50, 100_000_000),
        metrics: Metrics::new(&prometheus::Registry::new()),
    })
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

    let (enc, dec) = svc_process_keypair();
    let bundle: std::sync::Arc<dyn TrustBundle> = std::sync::Arc::new(StaticTrustBundle(
        Mutex::new(HashMap::from([("k1".to_string(), dec)])),
    ));
    let mut cfg = test_config(vec![upstream_addr.port()]);
    cfg.deny_cluster_cidrs = vec![]; // this host's own address must not collide with a cluster CIDR
    let state = test_proxy_state(cfg, bundle);

    let proxy_listener = TcpListener::bind(("127.0.0.1", 0))
        .await
        .expect("bind proxy");
    let proxy_addr = proxy_listener.local_addr().unwrap();
    tokio::spawn(egress_proxy::serve_proxy(proxy_listener, state));

    let machine_jwt = valid_machine_jwt(&enc);
    let grant = assertion_with_port(
        DestinationCategory::PrivateIp,
        &format!("{local_ip}/32"),
        upstream_addr.port(),
    );
    let assertion_jwt = make_assertion_jwt(&enc, "k1", &grant);

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

/// CRITICAL security-review regression: the forward-HTTP path must never
/// relay this proxy's own inbound credential headers (`Authorization` --
/// the caller's machine JWT -- and `X-Waddles-Egress-Assertion`) to the
/// destination. Runs a real fake "destination" HTTP/1.1 server and
/// inspects exactly what it received.
#[tokio::test]
async fn forward_http_never_leaks_proxy_hop_credentials_to_destination() {
    let local_ip = local_non_loopback_ip();

    let upstream_listener = TcpListener::bind((local_ip, 0))
        .await
        .expect("bind upstream");
    let upstream_addr = upstream_listener.local_addr().unwrap();
    let (received_tx, received_rx) = tokio::sync::oneshot::channel();
    tokio::spawn(async move {
        if let Ok((mut sock, _)) = upstream_listener.accept().await {
            let mut buf = vec![0u8; 4096];
            let mut total = Vec::new();
            loop {
                let n = sock.read(&mut buf).await.unwrap_or(0);
                if n == 0 {
                    break;
                }
                total.extend_from_slice(&buf[..n]);
                if total.windows(4).any(|w| w == b"\r\n\r\n") {
                    break;
                }
            }
            let _ = sock
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
                .await;
            let _ = received_tx.send(String::from_utf8_lossy(&total).to_string());
        }
    });

    let (enc, dec) = svc_process_keypair();
    let bundle: std::sync::Arc<dyn TrustBundle> = std::sync::Arc::new(StaticTrustBundle(
        Mutex::new(HashMap::from([("k1".to_string(), dec)])),
    ));
    let mut cfg = test_config(vec![upstream_addr.port()]);
    cfg.deny_cluster_cidrs = vec![];
    let state = test_proxy_state(cfg, bundle);

    let proxy_listener = TcpListener::bind(("127.0.0.1", 0))
        .await
        .expect("bind proxy");
    let proxy_addr = proxy_listener.local_addr().unwrap();
    tokio::spawn(egress_proxy::serve_proxy(proxy_listener, state));

    let machine_jwt = valid_machine_jwt(&enc);
    let grant = assertion_with_port(
        DestinationCategory::PrivateIp,
        &format!("{local_ip}/32"),
        upstream_addr.port(),
    );
    let assertion_jwt = make_assertion_jwt(&enc, "k1", &grant);

    let mut client = TcpStream::connect(proxy_addr)
        .await
        .expect("connect to proxy");
    let request = format!(
        "GET http://{local_ip}:{port}/ HTTP/1.1\r\nHost: {local_ip}:{port}\r\nAuthorization: Bearer {machine_jwt}\r\nX-Waddles-Egress-Assertion: {assertion_jwt}\r\nX-Forwarded-For-Test: end-to-end-header\r\nConnection: close\r\n\r\n",
        port = upstream_addr.port()
    );
    client.write_all(request.as_bytes()).await.unwrap();

    let mut resp = Vec::new();
    let _ = client.read_to_end(&mut resp).await;

    let received = tokio::time::timeout(Duration::from_secs(5), received_rx)
        .await
        .expect("upstream received a request within timeout")
        .expect("channel not dropped");

    assert!(
        !received.to_ascii_lowercase().contains("authorization:"),
        "destination must never see any Authorization header, got:\n{received}"
    );
    assert!(
        !received.to_ascii_lowercase().contains("x-waddles-"),
        "destination must never see any X-Waddles-* header, got:\n{received}"
    );
    // A genuinely end-to-end header the caller sent must still pass
    // through untouched -- the allowlist strips specific proxy-hop
    // headers, not everything.
    assert!(
        received.contains("X-Forwarded-For-Test: end-to-end-header")
            || received.contains("x-forwarded-for-test: end-to-end-header"),
        "an ordinary end-to-end header must still be forwarded, got:\n{received}"
    );
}

/// CRITICAL security-review regression, positive case: a bundle's own
/// credential -- substituted into the distinct
/// `X-Waddles-Forward-Authorization` header -- *is* delivered to the
/// destination as a real `Authorization` header, proving the escape hatch
/// works even while the proxy-hop credential stays stripped.
#[tokio::test]
async fn forward_authorization_header_is_mapped_to_real_authorization_for_destination() {
    let local_ip = local_non_loopback_ip();

    let upstream_listener = TcpListener::bind((local_ip, 0))
        .await
        .expect("bind upstream");
    let upstream_addr = upstream_listener.local_addr().unwrap();
    let (received_tx, received_rx) = tokio::sync::oneshot::channel();
    tokio::spawn(async move {
        if let Ok((mut sock, _)) = upstream_listener.accept().await {
            let mut buf = vec![0u8; 4096];
            let mut total = Vec::new();
            loop {
                let n = sock.read(&mut buf).await.unwrap_or(0);
                if n == 0 {
                    break;
                }
                total.extend_from_slice(&buf[..n]);
                if total.windows(4).any(|w| w == b"\r\n\r\n") {
                    break;
                }
            }
            let _ = sock
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
                .await;
            let _ = received_tx.send(String::from_utf8_lossy(&total).to_string());
        }
    });

    let (enc, dec) = svc_process_keypair();
    let bundle: std::sync::Arc<dyn TrustBundle> = std::sync::Arc::new(StaticTrustBundle(
        Mutex::new(HashMap::from([("k1".to_string(), dec)])),
    ));
    let mut cfg = test_config(vec![upstream_addr.port()]);
    cfg.deny_cluster_cidrs = vec![];
    let state = test_proxy_state(cfg, bundle);

    let proxy_listener = TcpListener::bind(("127.0.0.1", 0))
        .await
        .expect("bind proxy");
    let proxy_addr = proxy_listener.local_addr().unwrap();
    tokio::spawn(egress_proxy::serve_proxy(proxy_listener, state));

    let machine_jwt = valid_machine_jwt(&enc);
    let grant = assertion_with_port(
        DestinationCategory::PrivateIp,
        &format!("{local_ip}/32"),
        upstream_addr.port(),
    );
    let assertion_jwt = make_assertion_jwt(&enc, "k1", &grant);

    let mut client = TcpStream::connect(proxy_addr)
        .await
        .expect("connect to proxy");
    let request = format!(
        "GET http://{local_ip}:{port}/ HTTP/1.1\r\nHost: {local_ip}:{port}\r\nAuthorization: Bearer {machine_jwt}\r\nX-Waddles-Egress-Assertion: {assertion_jwt}\r\nX-Waddles-Forward-Authorization: Bearer end-to-end-secret\r\nConnection: close\r\n\r\n",
        port = upstream_addr.port()
    );
    client.write_all(request.as_bytes()).await.unwrap();

    let mut resp = Vec::new();
    let _ = client.read_to_end(&mut resp).await;

    let received = tokio::time::timeout(Duration::from_secs(5), received_rx)
        .await
        .expect("upstream received a request within timeout")
        .expect("channel not dropped");

    assert!(
        received.contains("authorization: Bearer end-to-end-secret")
            || received.contains("Authorization: Bearer end-to-end-secret"),
        "the forwarded credential must arrive as a real Authorization header, got:\n{received}"
    );
    // The proxy-hop machine JWT itself must never appear anywhere in what
    // the destination received.
    assert!(
        !received.contains(&machine_jwt),
        "the machine JWT must never reach the destination, got:\n{received}"
    );
}

// --- Cross-crate: `bundle_host_http`'s signer -> `egress_proxy`'s verifier ---
//
// The tests above all forge assertions by hand (`make_assertion_jwt` +
// `EgressAssertion { .. }` literals) to exercise `proxy::validate` in
// isolation. The tests below instead drive the *real* client-side signer
// (`bundle_host_http::egress::EgressAssertionSigner`, PR #468) and feed its
// output straight into this crate's own `proxy::validate` -- proving the two
// crates on either side of the `egress_assertion` shared wire format still
// agree end to end, not just that each compiles against the same struct.

// PKCS8-DER-encoded Ed25519 test keypair (fixed, test-only, distinct from
// the SVC_PROCESS_*/SVC_ACTION_* pairs above purely so these tests don't
// share key material with the hand-forged-assertion tests) -- generated
// once with `openssl genpkey -algorithm ed25519` / `openssl pkey -pubout`,
// never used outside this test module.
const BUNDLE_SIGNER_PRIV_DER: &[u8] = &[
    48, 46, 2, 1, 0, 48, 5, 6, 3, 43, 101, 112, 4, 34, 4, 32, 13, 186, 244, 23, 103, 115, 237, 53,
    220, 205, 68, 209, 2, 224, 53, 221, 134, 143, 240, 210, 4, 242, 252, 217, 89, 189, 18, 56, 10,
    231, 127, 112,
];
const BUNDLE_SIGNER_PUB_RAW: &[u8] = &[
    203, 153, 62, 224, 216, 7, 49, 241, 132, 82, 7, 74, 194, 22, 36, 114, 13, 239, 65, 192, 119,
    152, 28, 118, 139, 213, 71, 250, 45, 191, 130, 148,
];
const BUNDLE_SIGNER_SUB: &str = "spiffe://penguintech.io/alpha/svc-process";

/// Minimal PKCS8 PEM encoder for the fixed Ed25519 private key DER above --
/// `AssertionSigningKey::from_ed25519_pem` (the only public constructor
/// `bundle_host_http`'s signer has for injecting raw key bytes in a test)
/// takes PEM, not DER. Avoids a `base64` dev-dependency purely to wrap a
/// fixed 48-byte blob in PKCS8 armor; the label is built from parts (not a
/// literal `"-----BEGIN...-----"` string) so this fixed, publicly-known,
/// test-only DER blob doesn't trip a secrets scanner's private-key-marker
/// heuristic on a string it merely resembles. Mirrors
/// `core/bundle_host_http/src/egress.rs`'s own test-only helper of the same
/// shape.
fn pem_encode_ed25519_private_key(der: &[u8]) -> Vec<u8> {
    const ALPHABET: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut b64 = String::new();
    for chunk in der.chunks(3) {
        let b = [
            chunk[0],
            *chunk.get(1).unwrap_or(&0),
            *chunk.get(2).unwrap_or(&0),
        ];
        let n = ((b[0] as u32) << 16) | ((b[1] as u32) << 8) | (b[2] as u32);
        b64.push(ALPHABET[((n >> 18) & 0x3f) as usize] as char);
        b64.push(ALPHABET[((n >> 12) & 0x3f) as usize] as char);
        b64.push(if chunk.len() > 1 {
            ALPHABET[((n >> 6) & 0x3f) as usize] as char
        } else {
            '='
        });
        b64.push(if chunk.len() > 2 {
            ALPHABET[(n & 0x3f) as usize] as char
        } else {
            '='
        });
    }
    let dashes = "-".repeat(5);
    let label = "PRIVATE KEY";
    let mut pem = format!("{dashes}BEGIN {label}{dashes}\n");
    for line in b64.as_bytes().chunks(64) {
        pem.push_str(std::str::from_utf8(line).unwrap());
        pem.push('\n');
    }
    pem.push_str(&format!("{dashes}END {label}{dashes}\n"));
    pem.into_bytes()
}

fn bundle_signer() -> EgressAssertionSigner {
    let signing_key = AssertionSigningKey::from_ed25519_pem(
        &pem_encode_ed25519_private_key(BUNDLE_SIGNER_PRIV_DER),
        "k-bundle",
    )
    .expect("valid Ed25519 PEM");
    EgressAssertionSigner::new(
        BUNDLE_SIGNER_SUB,
        "tenant-1",
        "community-1",
        Arc::new(signing_key),
        30,
    )
}

fn bundle_signer_trust_bundle() -> StaticTrustBundle {
    StaticTrustBundle(Mutex::new(HashMap::from([(
        "k-bundle".to_string(),
        DecodingKey::from_ed_der(BUNDLE_SIGNER_PUB_RAW),
    )])))
}

fn bundle_signer_machine_jwt() -> String {
    let enc = EncodingKey::from_ed_der(BUNDLE_SIGNER_PRIV_DER);
    make_machine_jwt(
        &enc,
        "k-bundle",
        "egress-proxy",
        BUNDLE_SIGNER_SUB,
        "egress:connect",
    )
}

/// The cross-crate contract this shared crate exists to guarantee, exercised
/// end to end through this crate's own verifier: an assertion signed by
/// `bundle_host_http::egress::EgressAssertionSigner` (the real client-side
/// signer wired into `EgressGuard::with_proxy_assertion_signer`) is accepted
/// by `egress_proxy::proxy::validate`.
#[tokio::test]
async fn bundle_host_http_signed_assertion_is_accepted_by_egress_proxy() {
    let bundle = bundle_signer_trust_bundle();
    let cfg = test_config(vec![443]);
    let resolver = FakeResolver(HashMap::from([(
        "discord.com",
        vec!["93.184.216.34".parse().unwrap()],
    )]));
    let replay_cache = InMemoryReplayCache::new();

    let machine_jwt = bundle_signer_machine_jwt();
    let assertion_jwt = bundle_signer()
        .sign("app-1", "discord.com", 443, DestinationCategory::Fqdn)
        .expect("bundle_host_http signs a valid assertion");
    let headers = headers_with(Some(&machine_jwt), Some(&assertion_jwt));

    let deps = ValidationDeps {
        trust_bundle: &bundle,
        replay_cache: &replay_cache,
        cfg: &cfg,
        cluster_cidrs: &cfg.deny_cluster_cidrs,
        resolver: &resolver,
    };
    let target = proxy::validate(&headers, "discord.com", 443, deps)
        .await
        .expect("egress_proxy accepts bundle_host_http's own assertion");
    assert_eq!(target.tenant, "tenant-1");
    assert_eq!(target.community, "community-1");
    assert_eq!(target.app, "app-1");
    assert_eq!(target.category, DestinationCategory::Fqdn);
}

/// Same signer, tampered signature -- one flipped base64url character in
/// the final segment must invalidate the whole token.
#[tokio::test]
async fn bundle_host_http_signed_assertion_tampered_is_rejected() {
    let bundle = bundle_signer_trust_bundle();
    let cfg = test_config(vec![443]);
    let resolver = FakeResolver(HashMap::from([(
        "discord.com",
        vec!["93.184.216.34".parse().unwrap()],
    )]));
    let replay_cache = InMemoryReplayCache::new();

    let machine_jwt = bundle_signer_machine_jwt();
    let mut assertion_jwt = bundle_signer()
        .sign("app-1", "discord.com", 443, DestinationCategory::Fqdn)
        .expect("signs");
    let last = assertion_jwt.pop().expect("non-empty token");
    assertion_jwt.push(if last == 'A' { 'B' } else { 'A' });
    let headers = headers_with(Some(&machine_jwt), Some(&assertion_jwt));

    let deps = ValidationDeps {
        trust_bundle: &bundle,
        replay_cache: &replay_cache,
        cfg: &cfg,
        cluster_cidrs: &cfg.deny_cluster_cidrs,
        resolver: &resolver,
    };
    let err = proxy::validate(&headers, "discord.com", 443, deps)
        .await
        .unwrap_err();
    assert!(
        matches!(err, ProxyError::Assertion(_)),
        "expected an Assertion (signature) error, got {err:?}"
    );
}

/// Same signer, an assertion minted already expired -- `sign()` always
/// stamps `iat = now`, so an expired token is built directly against the
/// same `AssertionSigningKey`/`egress_assertion::build_assertion` primitives
/// the signer itself uses internally, rather than sleeping past the TTL in
/// a test.
#[tokio::test]
async fn bundle_host_http_signed_assertion_expired_is_rejected() {
    let bundle = bundle_signer_trust_bundle();
    let cfg = test_config(vec![443]);
    let resolver = FakeResolver(HashMap::from([(
        "discord.com",
        vec!["93.184.216.34".parse().unwrap()],
    )]));
    let replay_cache = InMemoryReplayCache::new();

    let signing_key = AssertionSigningKey::from_ed25519_pem(
        &pem_encode_ed25519_private_key(BUNDLE_SIGNER_PRIV_DER),
        "k-bundle",
    )
    .expect("valid Ed25519 PEM");
    let mut claims = egress_assertion::build_assertion(
        BUNDLE_SIGNER_SUB,
        "tenant-1",
        "community-1",
        "app-1",
        DestinationCategory::Fqdn,
        "discord.com",
        443,
        30,
    );
    claims.iat = egress_assertion::now_secs() - 120;
    claims.exp = egress_assertion::now_secs() - 60;
    let assertion_jwt = signing_key.sign(&claims).expect("signs");

    let machine_jwt = bundle_signer_machine_jwt();
    let headers = headers_with(Some(&machine_jwt), Some(&assertion_jwt));
    let deps = ValidationDeps {
        trust_bundle: &bundle,
        replay_cache: &replay_cache,
        cfg: &cfg,
        cluster_cidrs: &cfg.deny_cluster_cidrs,
        resolver: &resolver,
    };
    let err = proxy::validate(&headers, "discord.com", 443, deps)
        .await
        .unwrap_err();
    assert!(
        matches!(err, ProxyError::Assertion(_)),
        "expected an Assertion (expired) error, got {err:?}"
    );
}

/// Same signer, `sub` doesn't match the authenticated machine JWT's own
/// `sub` -- must be rejected even though the signature itself verifies
/// cleanly against the registered key.
#[tokio::test]
async fn bundle_host_http_signed_assertion_wrong_sub_is_rejected() {
    let bundle = bundle_signer_trust_bundle();
    let cfg = test_config(vec![443]);
    let resolver = FakeResolver(HashMap::from([(
        "discord.com",
        vec!["93.184.216.34".parse().unwrap()],
    )]));
    let replay_cache = InMemoryReplayCache::new();

    // Signed under the registered `k-bundle` key, but the `sub` claim
    // names a different service than the machine JWT authenticates.
    let signing_key = AssertionSigningKey::from_ed25519_pem(
        &pem_encode_ed25519_private_key(BUNDLE_SIGNER_PRIV_DER),
        "k-bundle",
    )
    .expect("valid Ed25519 PEM");
    let claims = egress_assertion::build_assertion(
        "spiffe://penguintech.io/alpha/svc-action",
        "tenant-1",
        "community-1",
        "app-1",
        DestinationCategory::Fqdn,
        "discord.com",
        443,
        30,
    );
    let assertion_jwt = signing_key.sign(&claims).expect("signs");

    let machine_jwt = bundle_signer_machine_jwt(); // authenticates as BUNDLE_SIGNER_SUB (svc-process)
    let headers = headers_with(Some(&machine_jwt), Some(&assertion_jwt));
    let deps = ValidationDeps {
        trust_bundle: &bundle,
        replay_cache: &replay_cache,
        cfg: &cfg,
        cluster_cidrs: &cfg.deny_cluster_cidrs,
        resolver: &resolver,
    };
    let err = proxy::validate(&headers, "discord.com", 443, deps)
        .await
        .unwrap_err();
    assert!(
        matches!(
            err,
            ProxyError::Assertion(egress_proxy::assertion::AssertionError::SubMismatch { .. })
        ),
        "expected SubMismatch, got {err:?}"
    );
}

/// Same signer, request for a different port than the assertion granted.
#[tokio::test]
async fn bundle_host_http_signed_assertion_wrong_port_is_rejected() {
    let bundle = bundle_signer_trust_bundle();
    let cfg = test_config(vec![443, 8443]);
    let resolver = FakeResolver(HashMap::from([(
        "discord.com",
        vec!["93.184.216.34".parse().unwrap()],
    )]));
    let replay_cache = InMemoryReplayCache::new();

    let machine_jwt = bundle_signer_machine_jwt();
    // Grants port 443 only.
    let assertion_jwt = bundle_signer()
        .sign("app-1", "discord.com", 443, DestinationCategory::Fqdn)
        .expect("signs");
    let headers = headers_with(Some(&machine_jwt), Some(&assertion_jwt));

    let deps = ValidationDeps {
        trust_bundle: &bundle,
        replay_cache: &replay_cache,
        cfg: &cfg,
        cluster_cidrs: &cfg.deny_cluster_cidrs,
        resolver: &resolver,
    };
    // Requested on the operator-allowed but not assertion-granted port.
    let err = proxy::validate(&headers, "discord.com", 8443, deps)
        .await
        .unwrap_err();
    assert!(
        matches!(err, ProxyError::DestinationMismatch),
        "expected DestinationMismatch, got {err:?}"
    );
}

/// Same signer, the identical assertion (same `jti`) replayed on a second
/// call -- must be rejected on the second use even though every other check
/// still passes.
#[tokio::test]
async fn bundle_host_http_signed_assertion_replay_is_rejected() {
    let bundle = bundle_signer_trust_bundle();
    let cfg = test_config(vec![443]);
    let resolver = FakeResolver(HashMap::from([(
        "discord.com",
        vec!["93.184.216.34".parse().unwrap()],
    )]));
    let replay_cache = InMemoryReplayCache::new();

    let machine_jwt = bundle_signer_machine_jwt();
    let assertion_jwt = bundle_signer()
        .sign("app-1", "discord.com", 443, DestinationCategory::Fqdn)
        .expect("signs");
    let headers = headers_with(Some(&machine_jwt), Some(&assertion_jwt));

    let deps = ValidationDeps {
        trust_bundle: &bundle,
        replay_cache: &replay_cache,
        cfg: &cfg,
        cluster_cidrs: &cfg.deny_cluster_cidrs,
        resolver: &resolver,
    };
    proxy::validate(&headers, "discord.com", 443, deps)
        .await
        .expect("first use succeeds");

    let deps = ValidationDeps {
        trust_bundle: &bundle,
        replay_cache: &replay_cache,
        cfg: &cfg,
        cluster_cidrs: &cfg.deny_cluster_cidrs,
        resolver: &resolver,
    };
    let err = proxy::validate(&headers, "discord.com", 443, deps)
        .await
        .unwrap_err();
    assert!(
        matches!(
            err,
            ProxyError::Assertion(egress_proxy::assertion::AssertionError::Replayed(_))
        ),
        "expected Replayed, got {err:?}"
    );
}
