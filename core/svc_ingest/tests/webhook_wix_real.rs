//! Real-path Wix webhook tests: a real axum server on a real socket, the real
//! `build_ingress` over a REAL Valkey (`VALKEY_URL`; unset FAILS, never skips),
//! the route planned by the real `WixProfile`, and RS256 JWT fixtures signed
//! with a per-test RSA key (openssl CLI, see `support/wix_sign.rs`).

#[path = "support/wix_sign.rs"]
mod wix_sign;

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use clap::Parser;
use connection_resolver::{
    BindingPage, BindingSummary, ConnectionKind, ConnectionRef, ConnectionResolve,
    ConnectionSecret, Endpoint, ResolveError, ResolvedConnection, SecretString,
};
use serde_json::json;
use svc_ingest::config::{CliConfig, Config};
use svc_ingest::connector::transport::Transport;
use svc_ingest::connector::webhook::WebhookTransport;
use svc_ingest::connector_runtime::feed::TransportPlan;
use svc_ingest::connector_runtime::plan::{ConnectorProfile, RuntimeEnv};
use svc_ingest::connector_runtime::profiles::wix::{WixProfile, META_INSTANCE_ID};
use svc_ingest::connector_runtime::webhook_boot::{
    activate_plan, build_ingress, IngressDeps, RouteKeyDeriver, WebhookEnv, ENV_PLATFORMS,
    ENV_ROUTE_KEY_SECRET,
};
use svc_ingest::http::{router, AppState};
use tokio::net::TcpListener;
use tokio::task::JoinHandle;

const COMMUNITY: i64 = 41;
const INSTANCE: &str = "0b9e1a52-4c3d-4e6f-8a7b-1234567890ab";
const ROUTE_SECRET: &str = "real-path-route-key-secret-0123456789";

fn valkey_url() -> String {
    std::env::var("VALKEY_URL")
        .expect("VALKEY_URL must be set: webhook tests need a REAL Valkey (no silent skip)")
}

fn now() -> i64 {
    chrono::Utc::now().timestamp()
}

/// Resolver: the connection holds `public_pem` as its bearer secret.
struct Resolver {
    public_pem: String,
}

impl ConnectionResolve for Resolver {
    async fn resolve(&self, r: ConnectionRef) -> Result<ResolvedConnection, ResolveError> {
        Ok(ResolvedConnection {
            connection_id: r.connection_id,
            community_id: COMMUNITY,
            provider: "wix".into(),
            kind: ConnectionKind::PlatformOauth,
            endpoint: Endpoint::default(),
            secret: ConnectionSecret::Bearer {
                token: SecretString::new(self.public_pem.clone()),
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

struct Rig {
    base: String,
    key: String,
    transport: WebhookTransport,
    signer: wix_sign::WixKey,
    client: reqwest::Client,
    _server: JoinHandle<()>,
}

/// `resolver_key`: the public key the resolver serves (the signer's own for
/// the happy path, a different pair's for the wrong-key case).
async fn rig(resolver_key: Option<wix_sign::WixKey>) -> Rig {
    let signer = wix_sign::generate();
    let served = resolver_key.map_or_else(|| signer.public_pem.clone(), |k| k.public_pem);
    let env = WebhookEnv::from_lookup(&|k| match k {
        ENV_PLATFORMS => Some("wix".into()),
        ENV_ROUTE_KEY_SECRET => Some(ROUTE_SECRET.into()),
        _ => None,
    })
    .expect("env");
    let registry = prometheus::Registry::new();
    let client = redis::Client::open(valkey_url()).expect("valkey client");
    let ingress = build_ingress(IngressDeps {
        env: &env,
        resolver: Arc::new(Resolver { public_pem: served }),
        valkey: Some(&client),
        registry: &registry,
    })
    .await
    .expect("build_ingress")
    .expect("ingress enabled");
    let connection_id = (uuid::Uuid::new_v4().as_u128() as i64) & i64::MAX | 1;
    let binding = BindingSummary {
        community_id: COMMUNITY,
        connection_id,
        tenant_id: 1,
        secret_version: 1,
        provider: "wix".into(),
        kind: ConnectionKind::PlatformOauth,
        metadata: [(META_INSTANCE_ID.to_string(), INSTANCE.to_string())].into(),
    };
    let profile = WixProfile::new(Some(
        RouteKeyDeriver::new(SecretString::new(ROUTE_SECRET.into())).expect("deriver"),
    ));
    let rt = RuntimeEnv {
        environment: "alpha".into(),
        max_frame_bytes: 65_536,
        egress_allow: vec![],
        heartbeat_enabled: false,
    };
    let mut planned = profile.plan(&binding, &rt).expect("plan");
    let TransportPlan::Webhook(plan) = planned.remove(0).plan else {
        panic!("wix plans a webhook");
    };
    let key = plan.route.route_key.clone();
    let transport = activate_plan(&ingress, &env, *plan).expect("activate");
    let mut state = AppState::new(
        Config::from_cli(CliConfig::parse_from(["svc-ingest"])).expect("cfg"),
        registry,
    );
    state.webhook = Some(ingress);
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
        signer,
        client: reqwest::Client::new(),
        _server: server,
    }
}

impl Rig {
    fn event(&self, id: &str) -> String {
        wix_sign::domain_event(id, "createdEvent", &json!({"entity": "{}"}))
    }

    fn jwt(&self, header: &serde_json::Value, instance: &str, id: &str, iat: i64) -> String {
        wix_sign::jwt(
            &self.signer,
            header,
            instance,
            "wix.members.v1.member_created",
            &self.event(id),
            Some(iat),
            Some(iat + 60),
        )
    }

    async fn post(&self, body: Vec<u8>) -> u16 {
        self.client
            .post(format!("{}/intake/webhook/{}", self.base, self.key))
            .body(body)
            .send()
            .await
            .expect("send")
            .status()
            .as_u16()
    }

    async fn delivered(&mut self) -> Option<Vec<u8>> {
        match tokio::time::timeout(Duration::from_millis(300), self.transport.next_frame()).await {
            Ok(Ok(frame)) => frame,
            _ => None,
        }
    }
}

fn unique() -> String {
    format!("evt-{}", uuid::Uuid::new_v4())
}

#[tokio::test]
async fn valid_jwt_reaches_the_transport_as_the_exact_verified_body() {
    let mut rig = rig(None).await;
    let t = rig.jwt(&wix_sign::rs256(), INSTANCE, &unique(), now());
    assert_eq!(rig.post(t.clone().into_bytes()).await, 200);
    assert_eq!(rig.delivered().await.expect("frame"), t.as_bytes());
}

#[tokio::test]
async fn replay_of_the_same_event_id_is_deduped_even_when_resigned() {
    let mut rig = rig(None).await;
    let id = unique();
    let a = rig.jwt(&wix_sign::rs256(), INSTANCE, &id, now());
    let b = rig.jwt(&wix_sign::rs256(), INSTANCE, &id, now() + 1);
    assert_ne!(a, b);
    assert_eq!(rig.post(a.clone().into_bytes()).await, 200);
    assert_eq!(rig.post(a.into_bytes()).await, 200);
    assert_eq!(rig.post(b.into_bytes()).await, 200);
    assert!(rig.delivered().await.is_some(), "first delivery");
    assert!(
        rig.delivered().await.is_none(),
        "replays are not redelivered"
    );
}

#[tokio::test]
async fn forged_wrong_key_wrong_instance_and_stale_are_403() {
    let mut rig = rig(None).await;
    // Tampered signature segment.
    let good = rig.jwt(&wix_sign::rs256(), INSTANCE, &unique(), now());
    let mut parts: Vec<&str> = good.split('.').collect();
    let sig = format!("{}A", &parts[2][..parts[2].len() - 1]);
    parts[2] = &sig;
    assert_eq!(rig.post(parts.join(".").into_bytes()).await, 403);
    // alg=none / HS256 / lowercase confusion headers (signature is irrelevant).
    for alg in ["none", "HS256", "rs256"] {
        let t = rig.jwt(&json!({"alg": alg}), INSTANCE, &unique(), now());
        assert_eq!(rig.post(t.into_bytes()).await, 403, "{alg}");
    }
    // Signed for another Wix instance.
    let t = rig.jwt(
        &wix_sign::rs256(),
        "someone-elses-instance",
        &unique(),
        now(),
    );
    assert_eq!(rig.post(t.into_bytes()).await, 403);
    // Expired / far-future freshness.
    for iat in [now() - 3600, now() + 3600] {
        let t = rig.jwt(&wix_sign::rs256(), INSTANCE, &unique(), iat);
        assert_eq!(rig.post(t.into_bytes()).await, 403);
    }
    // Garbage and empty bodies.
    assert_eq!(rig.post(b"not-a-jwt".to_vec()).await, 403);
    assert_eq!(rig.post(Vec::new()).await, 403);
    assert!(
        rig.delivered().await.is_none(),
        "nothing forged is delivered"
    );
}

#[tokio::test]
async fn token_signed_by_a_different_key_than_the_connections_is_403() {
    let mut rig = rig(Some(wix_sign::generate())).await;
    let t = rig.jwt(&wix_sign::rs256(), INSTANCE, &unique(), now());
    assert_eq!(rig.post(t.into_bytes()).await, 403);
    assert!(rig.delivered().await.is_none());
}

#[tokio::test]
async fn oversized_body_is_413_and_unknown_route_is_404() {
    let rig = rig(None).await;
    assert_eq!(rig.post(vec![b'a'; 1024 * 1024 + 1]).await, 413);
    let s = rig
        .client
        .post(format!("{}/intake/webhook/{}", rig.base, "x".repeat(43)))
        .body("a.b.c")
        .send()
        .await
        .expect("send")
        .status()
        .as_u16();
    assert_eq!(s, 404);
}
