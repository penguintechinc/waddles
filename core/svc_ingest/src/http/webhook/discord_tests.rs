//! Discord interaction route tests: Ed25519 verification, host-side PING/ack,
//! replay, and the 3-second deadline (virtual clock).

use super::*;
use crate::config::{CliConfig, Config};
use crate::connector::transport::Transport;
use crate::connector::webhook::{
    BoxFut, DedupToken, SecretError, WebhookDedup, WebhookIngress, WebhookSecretSource,
    WebhookTransport,
};
use crate::connector::webhook_verify::hex_encode;
use crate::connector_runtime::webhook_boot::{
    activate_route, build_route, WebhookEnv, ENV_PLATFORMS, ENV_ROUTE_KEY_SECRET,
};
use axum::body::Body;
use axum::http::Request;
use clap::Parser;
use connection_resolver::{ConnectionRef, SecretString};
use ring::signature::{Ed25519KeyPair, KeyPair};
use std::collections::HashSet;
use std::sync::Mutex;
use tower::ServiceExt;

const COMMUNITY: i64 = 5;
const CONNECTION: i64 = 11;

struct PubKey {
    hex: String,
    delay: Duration,
}
impl WebhookSecretSource for PubKey {
    fn resolve(&self, _r: ConnectionRef) -> BoxFut<'_, Result<SecretString, SecretError>> {
        Box::pin(async move {
            tokio::time::sleep(self.delay).await;
            Ok(SecretString::new(self.hex.clone()))
        })
    }
}

#[derive(Default)]
struct Seen(Mutex<HashSet<String>>);
impl WebhookDedup for Seen {
    fn first_seen<'a>(&'a self, k: &'a str) -> BoxFut<'a, Result<Option<DedupToken>, ()>> {
        Box::pin(async move {
            Ok(self
                .0
                .lock()
                .map_err(|_| ())?
                .insert(k.to_string())
                .then(|| DedupToken("t".into())))
        })
    }
    fn release<'a>(&'a self, _k: &'a str, _t: &'a DedupToken) -> BoxFut<'a, Result<(), ()>> {
        Box::pin(async { Ok(()) })
    }
}

fn keypair() -> Ed25519KeyPair {
    Ed25519KeyPair::from_seed_unchecked(&[7u8; 32]).expect("seed")
}

async fn setup(delay: Duration) -> (axum::Router, WebhookTransport, String) {
    setup_with(delay, true).await
}

async fn setup_with(delay: Duration, with_store: bool) -> (axum::Router, WebhookTransport, String) {
    let env = WebhookEnv::from_lookup(&|k| match k {
        ENV_PLATFORMS => Some("discord".into()),
        ENV_ROUTE_KEY_SECRET => Some("0123456789abcdef0123456789abcdef".into()),
        _ => None,
    })
    .expect("env");
    let ing = WebhookIngress::new(
        Arc::new(PubKey {
            hex: hex_encode(keypair().public_key().as_ref()),
            delay,
        }),
        Arc::new(Seen::default()),
        None,
    );
    let route = build_route(
        &env,
        "discord",
        ConnectionRef {
            community_id: COMMUNITY,
            connection_id: CONNECTION,
        },
    )
    .expect("route");
    let key = route.route_key.clone();
    let t = activate_route(&ing, &env, route, COMMUNITY).expect("activate");
    let mut st = AppState::new(
        Config::from_cli(CliConfig::parse_from(["svc-ingest"])).expect("cfg"),
        prometheus::Registry::new(),
    );
    st.webhook = Some(Arc::new(ing));
    st.interactions = if with_store {
        Some(store().await)
    } else {
        None
    };
    (router(st), t, key)
}

/// The real Valkey the interaction token store runs on (`VALKEY_URL`).
async fn store() -> crate::interaction_store::InteractionStore {
    let url = std::env::var("VALKEY_URL").expect("VALKEY_URL (a local valkey) is required");
    crate::interaction_store::InteractionStore::new(
        redis::Client::open(url)
            .expect("valkey url")
            .get_multiplexed_async_connection()
            .await
            .expect("valkey"),
    )
}

/// A unique snowflake-shaped id per call.
fn uid(seed: u64) -> String {
    let n = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("clock")
        .as_nanos();
    format!("{seed}{n}")
}

/// A full interaction body (the shape Discord posts), with a reply token.
fn ix(ty: u64, id: &str) -> String {
    format!(
        r#"{{"type":{ty},"id":"{id}","application_id":"42","token":"webTOKEN-secret.1","guild_id":"111"}}"#
    )
}

fn signed(key: &str, ts: i64, body: &str, forge: bool) -> Request<Body> {
    let mut msg = ts.to_string().into_bytes();
    msg.extend_from_slice(body.as_bytes());
    let mut sig = keypair().sign(&msg).as_ref().to_vec();
    if forge {
        sig[0] ^= 0xff;
    }
    Request::builder()
        .method("POST")
        .uri(format!("/intake/webhook/{key}"))
        .header("x-signature-timestamp", ts.to_string())
        .header("x-signature-ed25519", hex_encode(&sig))
        .body(Body::from(body.to_string()))
        .expect("req")
}

async fn json(r: Response) -> serde_json::Value {
    let b = axum::body::to_bytes(r.into_body(), 4096)
        .await
        .expect("body");
    serde_json::from_slice(&b).expect("json")
}

async fn nothing_delivered(t: &mut WebhookTransport) -> bool {
    tokio::time::timeout(Duration::from_millis(50), t.next_frame())
        .await
        .is_err()
}

fn now() -> i64 {
    chrono::Utc::now().timestamp()
}

#[tokio::test]
async fn ping_is_answered_host_side_and_delivered_once() {
    let (app, mut t, key) = setup(Duration::ZERO).await;
    let body = r#"{"type":1,"id":"1"}"#;
    let r = app.oneshot(signed(&key, now(), body, false)).await.unwrap();
    assert_eq!(r.status(), StatusCode::OK);
    assert_eq!(json(r).await, serde_json::json!({"type": 1}));
    assert_eq!(t.next_frame().await.unwrap().unwrap(), body.as_bytes());
}

#[tokio::test]
async fn commands_get_a_deferred_ack_and_reach_the_transport() {
    let (app, mut t, key) = setup(Duration::ZERO).await;
    for (ty, want) in [(2, 5), (3, 6), (5, 6)] {
        let id = uid(ty);
        let body = ix(ty, &id);
        let r = app
            .clone()
            .oneshot(signed(&key, now(), &body, false))
            .await
            .unwrap();
        assert_eq!(r.status(), StatusCode::OK);
        assert_eq!(json(r).await["type"], want);
        assert_eq!(t.next_frame().await.unwrap().unwrap(), body.as_bytes());
        // The reply token is stored host-side, keyed by the interaction id.
        let rec = store().await.get(&id).await.unwrap().expect("stored");
        assert_eq!(rec.token, "webTOKEN-secret.1");
        assert_eq!(rec.guild_id.as_deref(), Some("111"));
    }
    let r = app
        .oneshot(signed(&key, now(), &ix(4, &uid(4)), false))
        .await
        .unwrap();
    assert_eq!(json(r).await["data"]["choices"], serde_json::json!([]));
}

#[tokio::test]
async fn unsupported_interaction_type_is_a_loud_400() {
    let (app, _t, key) = setup(Duration::ZERO).await;
    let r = app
        .oneshot(signed(&key, now(), r#"{"type":99}"#, false))
        .await
        .unwrap();
    assert_eq!(r.status(), StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn forged_stale_and_malformed_are_refused_and_nothing_delivered() {
    let (app, mut t, key) = setup(Duration::ZERO).await;
    let ping = r#"{"type":1}"#;
    let forged = app
        .clone()
        .oneshot(signed(&key, now(), ping, true))
        .await
        .unwrap();
    assert_eq!(forged.status(), StatusCode::FORBIDDEN);
    assert_eq!(json(forged).await["status"], "forbidden");
    let stale = app
        .clone()
        .oneshot(signed(&key, now() - 3600, ping, false))
        .await
        .unwrap();
    assert_eq!(stale.status(), StatusCode::FORBIDDEN);
    // Body not a JSON object (ts|body boundary pinned): refused.
    let r = app
        .clone()
        .oneshot(signed(&key, now(), "7", false))
        .await
        .unwrap();
    assert_eq!(r.status(), StatusCode::FORBIDDEN);
    let unknown = app
        .clone()
        .oneshot(signed("no-such-route-key", now(), ping, false))
        .await
        .unwrap();
    assert_eq!(unknown.status(), StatusCode::NOT_FOUND);
    assert!(nothing_delivered(&mut t).await);
    drop(app);
}

#[tokio::test]
async fn replay_is_acked_but_delivered_once() {
    let (app, mut t, key) = setup(Duration::ZERO).await;
    let ts = now();
    let body = ix(2, &uid(9));
    for _ in 0..2 {
        let r = app
            .clone()
            .oneshot(signed(&key, ts, &body, false))
            .await
            .unwrap();
        assert_eq!(r.status(), StatusCode::OK);
    }
    assert_eq!(t.next_frame().await.unwrap().unwrap(), body.as_bytes());
    assert!(nothing_delivered(&mut t).await);
    drop(app);
}

/// The 3 s Discord deadline: a secret backend stalling for 10 s must yield a
/// 503 inside the deadline (virtual clock), never a hang.
#[tokio::test(start_paused = true)]
async fn slow_backend_answers_503_within_the_discord_deadline() {
    let (app, _t, key) = setup_with(Duration::from_secs(10), false).await;
    let start = tokio::time::Instant::now();
    let r = app
        .oneshot(signed(&key, now(), r#"{"type":1}"#, false))
        .await
        .unwrap();
    let took = start.elapsed();
    assert_eq!(r.status(), StatusCode::SERVICE_UNAVAILABLE);
    assert!(took >= DISCORD_ACK_DEADLINE, "answered early: {took:?}");
    assert!(
        took < Duration::from_secs(3),
        "missed the 3s deadline: {took:?}"
    );
}

#[tokio::test(start_paused = true)]
async fn fast_ping_acks_well_inside_the_deadline() {
    let (app, _t, key) = setup_with(Duration::from_millis(100), false).await;
    let start = tokio::time::Instant::now();
    let r = app
        .oneshot(signed(&key, now(), r#"{"type":1}"#, false))
        .await
        .unwrap();
    assert_eq!(r.status(), StatusCode::OK);
    assert!(start.elapsed() < Duration::from_secs(1));
}

#[test]
fn ack_mapping_rejects_garbage() {
    assert!(discord_ack(b"not json").is_none());
    assert!(discord_ack(br#"{"x":1}"#).is_none());
    assert!(discord_ack(br#"{"type":"1"}"#).is_none());
}

#[tokio::test]
async fn an_interaction_that_cannot_be_stored_is_503_not_acked() {
    // No store configured: the delivery is verified but a reply could never be sent.
    let (app, _t, key) = setup_with(Duration::ZERO, false).await;
    let r = app
        .oneshot(signed(&key, now(), &ix(2, &uid(8)), false))
        .await
        .unwrap();
    assert_eq!(r.status(), StatusCode::SERVICE_UNAVAILABLE);
}

#[tokio::test]
async fn a_verified_interaction_missing_its_token_is_503() {
    let (app, _t, key) = setup(Duration::ZERO).await;
    let r = app
        .oneshot(signed(&key, now(), r#"{"type":2,"id":"123"}"#, false))
        .await
        .unwrap();
    assert_eq!(r.status(), StatusCode::SERVICE_UNAVAILABLE);
}
