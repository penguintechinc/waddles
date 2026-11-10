//! Real-path webhook tests (PR-D): a real axum server on a real socket, the
//! real `build_ingress` over a REAL Valkey (`VALKEY_URL`; unset FAILS, never
//! skips), signed Ed25519 Discord interaction fixtures, and a TCP proxy to
//! Valkey that is killed to prove the dedup store fails CLOSED.

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use clap::Parser;
use connection_resolver::{
    BindingPage, ConnectionKind, ConnectionRef, ConnectionResolve, ConnectionSecret, Endpoint,
    ResolveError, ResolvedConnection, SecretString,
};
use ring::signature::{Ed25519KeyPair, KeyPair};
use svc_ingest::config::{CliConfig, Config};
use svc_ingest::connector::transport::Transport;
use svc_ingest::connector::webhook::WebhookTransport;
use svc_ingest::connector::webhook_verify::hex_encode;
use svc_ingest::connector_runtime::webhook_boot::{
    activate_route, build_ingress, build_route, BootError, IngressDeps, WebhookEnv, ENV_PLATFORMS,
    ENV_ROUTE_KEY_SECRET,
};
use svc_ingest::http::{router, AppState};
use tokio::net::{TcpListener, TcpStream};
use tokio::task::JoinHandle;

const COMMUNITY: i64 = 41;

fn valkey_url() -> String {
    std::env::var("VALKEY_URL")
        .expect("VALKEY_URL must be set: webhook tests need a REAL Valkey (no silent skip)")
}

fn keypair() -> Ed25519KeyPair {
    Ed25519KeyPair::from_seed_unchecked(&[11u8; 32]).expect("seed")
}

/// Resolver: every connection belongs to `owner_community` and holds the
/// interaction public key as a bearer secret.
struct Resolver {
    owner_community: i64,
}

impl ConnectionResolve for Resolver {
    async fn resolve(&self, r: ConnectionRef) -> Result<ResolvedConnection, ResolveError> {
        Ok(ResolvedConnection {
            connection_id: r.connection_id,
            community_id: self.owner_community,
            provider: "discord".into(),
            kind: ConnectionKind::PlatformOauth,
            endpoint: Endpoint::default(),
            secret: ConnectionSecret::Bearer {
                token: SecretString::new(hex_encode(keypair().public_key().as_ref())),
            },
            expires_at: None,
            metadata: Default::default(),
        })
    }

    async fn list_bindings(
        &self,
        _p: &str,
        _c: Option<&str>,
        _a: i64,
        _l: u32,
    ) -> Result<BindingPage, ResolveError> {
        Err(ResolveError::Malformed("unused"))
    }
}

fn env() -> WebhookEnv {
    WebhookEnv::from_lookup(&|k| match k {
        ENV_PLATFORMS => Some("discord".into()),
        ENV_ROUTE_KEY_SECRET => Some("real-path-route-key-secret-0123456789".into()),
        _ => None,
    })
    .expect("env")
}

fn fresh_connection_id() -> i64 {
    (uuid::Uuid::new_v4().as_u128() as i64) & i64::MAX
}

struct Rig {
    base: String,
    key: String,
    transport: WebhookTransport,
    registry: prometheus::Registry,
    client: reqwest::Client,
    _server: JoinHandle<()>,
}

/// Boots the whole path: Valkey client at `valkey` -> `build_ingress` ->
/// activate one route -> AppState -> real server.
async fn rig(valkey: &str, owner_community: i64) -> Rig {
    let env = env();
    let registry = prometheus::Registry::new();
    let client = redis::Client::open(valkey).expect("valkey client");
    let ingress = build_ingress(IngressDeps {
        env: &env,
        resolver: Arc::new(Resolver { owner_community }),
        valkey: Some(&client),
        registry: &registry,
    })
    .await
    .expect("build_ingress")
    .expect("ingress enabled");
    let route = build_route(
        &env,
        "discord",
        ConnectionRef {
            community_id: COMMUNITY,
            connection_id: fresh_connection_id(),
        },
    )
    .expect("route");
    let key = route.route_key.clone();
    let transport = activate_route(&ingress, &env, route, COMMUNITY).expect("activate");
    let mut state = AppState::new(
        Config::from_cli(CliConfig::parse_from(["svc-ingest"])).expect("cfg"),
        registry.clone(),
    );
    state.webhook = Some(ingress);
    // The Discord door stores every verified (non-PING) interaction's reply token host-side
    // before acking; without a store it answers 503, so the rig runs the real Valkey-backed one.
    state.interactions = Some(svc_ingest::interaction_store::InteractionStore::new(
        client
            .get_multiplexed_async_connection()
            .await
            .expect("valkey connection for the interaction store"),
    ));
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
    let addr = listener.local_addr().expect("addr");
    let app = router(state).into_make_service_with_connect_info::<SocketAddr>();
    let server = tokio::spawn(async move {
        axum::serve(listener, app).await.expect("serve");
    });
    Rig {
        base: format!("http://{addr}"),
        key,
        transport,
        registry,
        client: reqwest::Client::new(),
        _server: server,
    }
}

/// A full interaction body (the shape Discord posts: snowflake id, application id and reply
/// token) so the door's host-side token store accepts it. `n` + the clock make the id unique
/// per run, since the interaction store and the dedup both persist in the real Valkey.
fn interaction(n: u64) -> String {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("clock")
        .as_nanos();
    format!(
        r#"{{"type":2,"id":"{n}{nanos}","application_id":"42","token":"webTOKEN-secret.1","guild_id":"111"}}"#
    )
}

fn now() -> i64 {
    chrono::Utc::now().timestamp()
}

impl Rig {
    async fn post(&self, ts: i64, body: &str, forge: bool) -> (u16, String) {
        let mut msg = ts.to_string().into_bytes();
        msg.extend_from_slice(body.as_bytes());
        let mut sig = keypair().sign(&msg).as_ref().to_vec();
        if forge {
            sig[0] ^= 0xff;
        }
        let r = self
            .client
            .post(format!("{}/intake/webhook/{}", self.base, self.key))
            .header("x-signature-timestamp", ts.to_string())
            .header("x-signature-ed25519", hex_encode(&sig))
            .body(body.to_string())
            .send()
            .await
            .expect("send");
        let status = r.status().as_u16();
        (status, r.text().await.expect("text"))
    }

    async fn delivered(&mut self) -> Option<Vec<u8>> {
        match tokio::time::timeout(Duration::from_millis(300), self.transport.next_frame()).await {
            Ok(Ok(frame)) => frame,
            _ => None,
        }
    }

    fn outcome_total(&self) -> u64 {
        self.registry
            .gather()
            .iter()
            .filter(|f| f.name() == "webhook_requests_total")
            .flat_map(|f| f.get_metric().iter())
            .map(|m| m.get_counter().value() as u64)
            .sum()
    }
}

#[tokio::test]
async fn valid_signature_reaches_the_transport_and_ping_is_answered() {
    let mut rig = rig(&valkey_url(), COMMUNITY).await;
    let (s, b) = rig.post(now(), r#"{"type":1}"#, false).await;
    assert_eq!((s, b.as_str()), (200, r#"{"type":1}"#));
    let cmd = &interaction(1);
    let (s, b) = rig.post(now(), cmd, false).await;
    assert_eq!((s, b.as_str()), (200, r#"{"type":5}"#));
    assert_eq!(rig.delivered().await.expect("ping frame"), br#"{"type":1}"#);
    assert_eq!(rig.delivered().await.expect("cmd frame"), cmd.as_bytes());
    assert!(rig.outcome_total() >= 2, "denominator must be non-zero");
}

#[tokio::test]
async fn bad_signature_is_403_and_nothing_is_delivered() {
    let mut rig = rig(&valkey_url(), COMMUNITY).await;
    let (s, _) = rig.post(now(), r#"{"type":2,"id":"x"}"#, true).await;
    assert_eq!(s, 403);
    assert!(rig.delivered().await.is_none());
    assert!(rig.outcome_total() >= 1);
}

#[tokio::test]
async fn stale_timestamp_is_refused() {
    let mut rig = rig(&valkey_url(), COMMUNITY).await;
    let (s, _) = rig
        .post(now() - 3600, r#"{"type":2,"id":"s"}"#, false)
        .await;
    assert_eq!(s, 403);
    assert!(rig.delivered().await.is_none());
}

#[tokio::test]
async fn replay_is_deduped_in_valkey() {
    let mut rig = rig(&valkey_url(), COMMUNITY).await;
    let ts = now();
    let body = &interaction(2);
    assert_eq!(rig.post(ts, body, false).await.0, 200);
    assert_eq!(rig.post(ts, body, false).await.0, 200);
    assert_eq!(rig.delivered().await.expect("first"), body.as_bytes());
    assert!(
        rig.delivered().await.is_none(),
        "replay must not re-deliver"
    );
}

#[tokio::test]
async fn wrong_community_secret_is_refused() {
    // The resolver answers for a DIFFERENT community than the route's
    // secret_ref: the ingress refuses the key (403), nothing delivered.
    let mut rig = rig(&valkey_url(), COMMUNITY + 1).await;
    let (s, _) = rig.post(now(), r#"{"type":2,"id":"w"}"#, false).await;
    assert_eq!(s, 403);
    assert!(rig.delivered().await.is_none());
}

#[tokio::test]
async fn wrong_community_registration_is_refused_at_activation() {
    let env = env();
    let client = redis::Client::open(valkey_url()).expect("client");
    let reg = prometheus::Registry::new();
    let ingress = build_ingress(IngressDeps {
        env: &env,
        resolver: Arc::new(Resolver {
            owner_community: COMMUNITY,
        }),
        valkey: Some(&client),
        registry: &reg,
    })
    .await
    .expect("boot")
    .expect("enabled");
    let route = build_route(
        &env,
        "discord",
        ConnectionRef {
            community_id: COMMUNITY,
            connection_id: fresh_connection_id(),
        },
    )
    .expect("route");
    let err = activate_route(&ingress, &env, route, COMMUNITY + 1).err();
    assert!(matches!(err, Some(BootError::Registry(_))), "{err:?}");
}

#[tokio::test]
async fn pre_auth_failure_budget_blocks_a_flooding_source() {
    let mut rig = rig(&valkey_url(), COMMUNITY).await;
    let mut last = 0;
    for _ in 0..25 {
        last = rig.post(now(), r#"{"type":2,"id":"f"}"#, true).await.0;
    }
    assert_eq!(last, 429, "source over its failure budget is throttled");
    // The throttled source cannot get a valid delivery through either.
    assert_eq!(rig.post(now(), r#"{"type":1}"#, false).await.0, 429);
    assert!(rig.delivered().await.is_none());
}

#[tokio::test]
async fn unknown_route_key_is_404() {
    let rig = rig(&valkey_url(), COMMUNITY).await;
    let r = rig
        .client
        .post(format!("{}/intake/webhook/{}", rig.base, "x".repeat(43)))
        .body("{}")
        .send()
        .await
        .expect("send");
    assert_eq!(r.status().as_u16(), 404);
}

#[tokio::test]
async fn oversize_body_is_413() {
    let rig = rig(&valkey_url(), COMMUNITY).await;
    let big = format!(r#"{{"type":2,"pad":"{}"}}"#, "a".repeat(1024 * 1024));
    let (s, _) = rig.post(now(), &big, false).await;
    assert_eq!(s, 413);
}

/// Forwards TCP to Valkey until `kill`; killing drops every live socket.
struct Proxy {
    addr: SocketAddr,
    accept: JoinHandle<()>,
    conns: Arc<std::sync::Mutex<Vec<JoinHandle<()>>>>,
}

impl Proxy {
    async fn start(target: String) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
        let addr = listener.local_addr().expect("addr");
        let conns: Arc<std::sync::Mutex<Vec<JoinHandle<()>>>> = Arc::default();
        let c2 = Arc::clone(&conns);
        let accept = tokio::spawn(async move {
            while let Ok((mut inbound, _)) = listener.accept().await {
                let target = target.clone();
                let h = tokio::spawn(async move {
                    if let Ok(mut out) = TcpStream::connect(target).await {
                        let _ = tokio::io::copy_bidirectional(&mut inbound, &mut out).await;
                    }
                });
                c2.lock().expect("lock").push(h);
            }
        });
        Self {
            addr,
            accept,
            conns,
        }
    }

    fn kill(&self) {
        self.accept.abort();
        for h in self.conns.lock().expect("lock").drain(..) {
            h.abort();
        }
    }
}

#[tokio::test]
async fn valkey_outage_fails_closed_and_delivers_nothing() {
    let url = valkey_url();
    let parsed = redis::parse_redis_url(&url).expect("VALKEY_URL parse");
    let target = format!(
        "{}:{}",
        parsed.host_str().expect("host"),
        parsed.port().unwrap_or(6379)
    );
    let proxy = Proxy::start(target).await;
    let mut rig = rig(&format!("redis://{}", proxy.addr), COMMUNITY).await;

    // Healthy through the proxy first (so the later failure is the outage).
    assert_eq!(rig.post(now(), &interaction(3), false).await.0, 200);
    assert!(rig.delivered().await.is_some());

    proxy.kill();
    let (s, _) = rig.post(now(), &interaction(4), false).await;
    assert_eq!(s, 503, "dedup outage must not accept");
    assert!(
        rig.delivered().await.is_none(),
        "nothing delivered on outage"
    );
}

#[tokio::test]
async fn build_ingress_refuses_a_dead_valkey_loudly() {
    let env = env();
    let client = redis::Client::open("redis://127.0.0.1:1/").expect("client");
    let reg = prometheus::Registry::new();
    let r = build_ingress(IngressDeps {
        env: &env,
        resolver: Arc::new(Resolver {
            owner_community: COMMUNITY,
        }),
        valkey: Some(&client),
        registry: &reg,
    })
    .await;
    assert!(matches!(r, Err(BootError::ValkeyConnect(_))));
}
