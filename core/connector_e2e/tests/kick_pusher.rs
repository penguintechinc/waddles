//! Real-path proof of the Kick `ConnectorProfile`: the `WsConfig` the profile derives from a
//! hub-api binding drives a REAL `WsTransport` against a local fake Pusher server, with the REAL
//! compiled Kick bundle (`fixtures/kick_connector.wasm`) running in a real bundle executor behind
//! svc_ingest's production wire invoker. Nothing between the socket and the recorded event is
//! mocked except the fake Pusher peer and the scripted identity stage (a fake identity).
//!
//! Test-only deviation from the derived config (`derived_config`): the URL is a host constant
//! and `RuntimeEnv` has no loopback switch, so the test rewrites `url` to the fake server and sets
//! `allow_loopback_insecure` AFTER asserting the derived values are the production constants.
//! The credential source is the production `KickFieldSecrets` (Kick's socket carries no
//! credential, so there is no inner source to stand in for).
//!
//! `fixtures/kick_connector.wasm` is a build output of `bundles/rust/kick`; see
//! `fixtures/README.md`.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::{BTreeMap, HashSet};
use std::net::{IpAddr, Ipv4Addr};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use bundle_executor::config::CliConfig;
use bundle_executor::connector::ConnectorLimits;
use bundle_executor::heartbeat::Heartbeat;
use bundle_executor::invoke::Executor;
use bundle_executor::manifest::{VerifiedManifest, WitWorld, PERM_CONNECTOR_PII_READ};
use bundle_executor::parity_support::{pseudonym_uuid, scripted_bridge};
use bundle_executor::wire::run_connection;
use clap::Parser;
use connection_resolver::{BindingSummary, ConnectionKind};
use connector_e2e::{digest_of, NoSource};
use futures_util::{SinkExt, StreamExt};
use svc_ingest::connector::{
    register_connector_metrics, ConnectionCtx, ConnectorDriver, ConnectorEvent, ConnectorRegistry,
    EventSink, ExecutorLink, PinnedConnector, SinkError, WireConnectorInvoker, WsConfig,
    WsTransport,
};
use svc_ingest::connector_runtime::profiles::kick::{
    KickFieldSecrets, KickProfile, APP_ID, PUSHER_URL, SLOT_PREFIX,
};
use svc_ingest::connector_runtime::{ConnectorProfile, RuntimeEnv, TransportPlan};
use tokio::net::TcpListener;
use tokio::sync::{mpsc, oneshot};
use tokio_tungstenite::tungstenite::Message;

const WASM: &[u8] = include_bytes!("fixtures/kick_connector.wasm");
const MAX_FRAME: usize = 65_536;
const CHATROOM: &str = "12345";

fn binding() -> BindingSummary {
    BindingSummary {
        tenant_id: 42,
        secret_version: 1,
        community_id: 7,
        connection_id: 9,
        provider: "kick".into(),
        kind: ConnectionKind::PlatformOauth,
        metadata: BTreeMap::from([
            ("chatroom_id".to_owned(), CHATROOM.to_owned()),
            ("tenant_id".to_owned(), "42".to_owned()),
        ]),
    }
}

/// Plans the binding through the profile, asserts the production constants, then applies the
/// documented test-only rewrites.
fn derived_config(addr: std::net::SocketAddr) -> WsConfig {
    let env = RuntimeEnv {
        environment: "test".into(),
        max_frame_bytes: MAX_FRAME,
        egress_allow: vec![IpAddr::V4(Ipv4Addr::LOCALHOST)],
        heartbeat_enabled: true,
    };
    let mut planned = KickProfile::new(Arc::new(|_c: i64| Some(42)))
        .plan(&binding(), &env)
        .expect("plan");
    assert_eq!(planned.len(), 1, "one binding in, one connection out");
    let TransportPlan::Ws(cfg) = planned.remove(0).plan else {
        panic!("kick plans a Ws transport");
    };
    let mut cfg = *cfg;
    assert_eq!(cfg.url, PUSHER_URL, "production URL is the host constant");
    cfg.url = format!("ws://{addr}/");
    cfg.allow_loopback_insecure = true;
    cfg
}

#[derive(Clone, Default)]
struct RecordingSink(Arc<Mutex<Vec<ConnectorEvent>>>);

impl EventSink for RecordingSink {
    async fn publish(&self, _ctx: &ConnectionCtx, event: ConnectorEvent) -> Result<(), SinkError> {
        self.0.lock().unwrap().push(event);
        Ok(())
    }
}

struct Rig {
    registry: Arc<ConnectorRegistry>,
    invoker: Arc<WireConnectorInvoker>,
    identity_calls: bundle_executor::parity_support::CallLog,
    _conn: tokio::task::JoinHandle<()>,
}

async fn rig() -> Rig {
    let cfg = CliConfig::try_parse_from([
        "bundle-executor",
        "--stage-host-api-addr",
        "svc-ingest:8301",
    ])
    .unwrap();
    let executor = Arc::new(Executor::new(&cfg, NoSource).unwrap());
    let digest = digest_of(WASM);
    let manifest = VerifiedManifest::new(
        APP_ID,
        digest.clone(),
        WitWorld::ConnectorTools,
        HashSet::from([PERM_CONNECTOR_PII_READ.to_string()]),
    );
    let (bridge, identity_calls) = scripted_bridge();
    executor
        .pin_connector(
            &manifest,
            WASM.to_vec(),
            ConnectorLimits::from_config(&cfg),
            Some(bridge),
        )
        .await
        .expect("the real kick connector pins");
    let mut registry = ConnectorRegistry::new();
    registry
        .register(PinnedConnector::new("kick", APP_ID, digest, MAX_FRAME))
        .unwrap();
    let registry = Arc::new(registry);
    let link = Arc::new(ExecutorLink::new());
    let invoker = Arc::new(WireConnectorInvoker::new(
        Arc::clone(&link),
        Arc::clone(&registry),
    ));
    let (stage_io, exec_io) = tokio::io::duplex(4 << 20);
    let hello = executor.hello(false, "runc");
    let conn = tokio::spawn(async move {
        let _ = run_connection(
            exec_io,
            hello,
            executor,
            "connector-e2e",
            Heartbeat::disabled(),
        )
        .await;
    });
    link.attach(stage_io).await.expect("host-api handshake");
    Rig {
        registry,
        invoker,
        identity_calls,
        _conn: conn,
    }
}

/// What the fake Pusher peer observed, in order.
#[derive(Debug, Clone, PartialEq, Eq)]
enum Seen {
    Handshake(String),
    Client(String),
}

fn chat_frame(id: u64, name: &str, text: &str, badges: &[&str]) -> String {
    let data = serde_json::json!({
        "id": format!("m-{id}"), "chatroom_id": 12345, "content": text,
        "created_at": "2026-11-07T00:00:00.000000Z",
        "sender": {"id": id, "username": name,
            "identity": {"badges": badges.iter().map(|b| serde_json::json!({"type": b})).collect::<Vec<_>>()}},
    });
    serde_json::json!({
        "event": "App\\Events\\ChatMessageEvent",
        "channel": format!("chatrooms.{CHATROOM}.v2"),
        "data": data.to_string(),
    })
    .to_string()
}

/// Fake Pusher server: `connection_established` FIRST (the profile waits for it), then reads the
/// subscribe, acks it, plays `frames` and keeps forwarding everything the client sends.
async fn serve(l: TcpListener, frames: Vec<String>, seen: mpsc::UnboundedSender<Seen>) {
    let (tcp, _) = l.accept().await.unwrap();
    let mut ws = tokio_tungstenite::accept_async(tcp).await.unwrap();
    ws.send(Message::text(
        serde_json::json!({"event": "pusher:connection_established",
            "data": serde_json::json!({"socket_id": "1.2", "activity_timeout": 30}).to_string()})
        .to_string(),
    ))
    .await
    .unwrap();
    let Some(Ok(Message::Text(hs))) = ws.next().await else {
        panic!("no subscribe frame");
    };
    seen.send(Seen::Handshake(hs.to_string())).unwrap();
    ws.send(Message::text(
        serde_json::json!({"event": "pusher_internal:subscription_succeeded", "data": "{}",
            "channel": format!("chatrooms.{CHATROOM}.v2")})
        .to_string(),
    ))
    .await
    .unwrap();
    for f in frames {
        ws.send(Message::text(f)).await.unwrap();
    }
    while let Some(Ok(m)) = ws.next().await {
        if let Message::Text(t) = m {
            seen.send(Seen::Client(t.to_string())).unwrap();
        }
    }
}

struct Run {
    events: Arc<Mutex<Vec<ConnectorEvent>>>,
    seen: mpsc::UnboundedReceiver<Seen>,
    shutdown: oneshot::Sender<()>,
    task: tokio::task::JoinHandle<()>,
    rig: Rig,
}

async fn start(frames: Vec<String>, heartbeat: Option<Duration>) -> Run {
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    let (tx, seen) = mpsc::unbounded_channel();
    tokio::spawn(serve(l, frames, tx));
    let mut cfg = derived_config(addr);
    if let Some(h) = heartbeat {
        cfg.heartbeat_interval = Some(h);
    }
    let rig = rig().await;
    let secrets = KickFieldSecrets::from_config(&cfg).expect("field secrets");
    let transport = WsTransport::new(cfg, secrets).expect("derived config is valid");
    let sink = RecordingSink::default();
    let events = Arc::clone(&sink.0);
    let metrics = register_connector_metrics(&prometheus::Registry::new()).unwrap();
    let driver = ConnectorDriver::new(
        Arc::clone(&rig.registry),
        Arc::clone(&rig.invoker),
        Arc::new(sink),
        metrics,
    )
    .with_backoff_cap(Duration::from_millis(5))
    .with_heartbeat_enabled(true);
    let (shutdown, rx) = oneshot::channel();
    let task = tokio::spawn(async move {
        driver.run(transport, rx).await.unwrap();
    });
    Run {
        events,
        seen,
        shutdown,
        task,
        rig,
    }
}

async fn next_seen(r: &mut Run) -> Seen {
    tokio::time::timeout(Duration::from_secs(10), r.seen.recv())
        .await
        .expect("fake Pusher peer saw nothing in time")
        .expect("peer channel open")
}

async fn wait_events(r: &Run, n: usize) {
    for _ in 0..200 {
        if r.events.lock().unwrap().len() >= n {
            return;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!(
        "expected {n} events, saw {}",
        r.events.lock().unwrap().len()
    );
}

async fn stop(r: Run) {
    let _ = r.shutdown.send(());
    tokio::time::timeout(Duration::from_secs(10), r.task)
        .await
        .expect("driver stops")
        .unwrap();
    drop(r.rig);
}

fn is_uuid_shaped(s: &str) -> bool {
    let p: Vec<&str> = s.split('-').collect();
    p.iter().map(|x| x.len()).collect::<Vec<_>>() == [8, 4, 4, 4, 12]
        && s.bytes().all(|b| b == b'-' || b.is_ascii_hexdigit())
}

/// The bundle's handshake prefix and the host's pinned slot prefix must be the same bytes.
#[test]
fn bundle_handshake_prefix_equals_the_host_pinned_prefix() {
    let wasm = String::from_utf8_lossy(WASM);
    assert!(
        wasm.contains(SLOT_PREFIX),
        "the compiled bundle does not carry the host-pinned slot prefix"
    );
}

/// The whole inbound path: waits for `connection_established`, exact subscribe bytes on the
/// wire (slot substituted by the host), foreign events dropped, chat events with UUID actors.
#[tokio::test(flavor = "multi_thread")]
async fn derived_config_drives_subscribe_and_normalized_chat_events() {
    let frames = vec![
        serde_json::json!({"event": "App\\Events\\SubscriptionEvent", "data": "{}"}).to_string(),
        chat_frame(555, "Viewer1", "hello chat", &["moderator"]),
        serde_json::json!({"event": "pusher:ping", "data": "{}"}).to_string(),
        chat_frame(777, "Viewer2", "second", &[]),
    ];
    let mut run = start(frames, None).await;

    let Seen::Handshake(hs) = next_seen(&mut run).await else {
        panic!("first client frame must be the subscribe");
    };
    assert_eq!(
        hs,
        format!(
            r#"{{"event":"pusher:subscribe","data":{{"auth":"","channel":"chatrooms.{CHATROOM}.v2"}}}}"#
        )
    );

    wait_events(&run, 2).await;
    tokio::time::sleep(Duration::from_millis(300)).await;
    let events = run.events.lock().unwrap().clone();
    assert_eq!(events.len(), 2, "protocol + foreign frames yield no events");
    for (e, (uid, text, is_mod)) in events
        .iter()
        .zip([("555", "hello chat", true), ("777", "second", false)])
    {
        assert_eq!(e.platform, "kick");
        assert_eq!(e.event_type, "message");
        let actor = e.actor.as_deref().expect("actor");
        assert!(is_uuid_shaped(actor), "{actor}");
        assert_eq!(actor, pseudonym_uuid("kick", uid));
        assert!(!actor.contains("iewer"), "no raw handle in the actor");
        let p: serde_json::Value = serde_json::from_str(&e.payload_json).unwrap();
        assert_eq!(p["text"], text);
        assert_eq!(p["is_mod"], is_mod);
        assert_eq!(p["room_id"], CHATROOM);
    }
    assert!(
        run.rig
            .identity_calls
            .lock()
            .unwrap()
            .iter()
            .any(|op| op == "identity.lookup"),
        "the real identity path was exercised"
    );
    stop(run).await;
}

/// The profile's heartbeat reaches the guest: a client `pusher:ping` goes out.
#[tokio::test(flavor = "multi_thread")]
async fn heartbeat_sends_a_pusher_ping() {
    let mut run = start(vec![], Some(Duration::from_millis(150))).await;
    let Seen::Handshake(_) = next_seen(&mut run).await else {
        panic!("subscribe first");
    };
    let Seen::Client(ping) = next_seen(&mut run).await else {
        panic!("client ping expected");
    };
    let v: serde_json::Value = serde_json::from_str(&ping).unwrap();
    assert_eq!(v["event"], "pusher:ping", "{ping}");
    stop(run).await;
}

/// A host-built webhook envelope (what `kick_webhook::Prepared::envelope` emits after the
/// signature verified) reaches the same real bundle through the same invoker: follow, gifts and
/// live status become events with UUID actors, giftee identities are never published, and an
/// unhandled type yields nothing. (Delivered over the fake socket because the bundle routes on
/// frame shape; the HTTP route itself is the webhook seam's job.)
#[tokio::test(flavor = "multi_thread")]
async fn verified_webhook_envelopes_normalize_through_the_real_bundle() {
    let user = |id: u64, name: &str| {
        serde_json::json!({"is_anonymous": false, "user_id": id, "username": name,
            "channel_slug": name.to_lowercase(), "identity": null})
    };
    let env = |ty: &str, body: serde_json::Value| {
        serde_json::json!({"type": ty, "version": 1, "message_id": "01H",
            "timestamp": "2026-11-07T00:00:00Z", "body": body})
        .to_string()
    };
    let frames = vec![
        env(
            "channel.followed",
            serde_json::json!({"broadcaster": user(1, "Caster"), "follower": user(2, "Fan")}),
        ),
        env(
            "moderation.banned",
            serde_json::json!({"broadcaster": user(1, "Caster")}),
        ),
        env(
            "channel.subscription.gifts",
            serde_json::json!({"broadcaster": user(1, "Caster"), "gifter": user(7, "Santa"),
                "giftees": [user(8, "GifteeA"), user(9, "GifteeB")]}),
        ),
        env(
            "livestream.status.updated",
            serde_json::json!({"broadcaster": user(1, "Caster"), "is_live": true,
                "started_at": "2026-11-07T00:00:00Z"}),
        ),
    ];
    let mut run = start(frames, None).await;
    let Seen::Handshake(_) = next_seen(&mut run).await else {
        panic!("subscribe first");
    };
    wait_events(&run, 3).await;
    tokio::time::sleep(Duration::from_millis(300)).await;
    let events = run.events.lock().unwrap().clone();
    assert_eq!(events.len(), 3, "the banned frame is ignored");
    let kinds: Vec<&str> = events.iter().map(|e| e.event_type.as_str()).collect();
    assert_eq!(
        kinds,
        [
            "channel.follow",
            "channel.subscription.gift",
            "stream.online"
        ]
    );
    assert_eq!(
        events[0].actor.as_deref(),
        Some(pseudonym_uuid("kick", "2").as_str())
    );
    assert_eq!(
        events[1].actor.as_deref(),
        Some(pseudonym_uuid("kick", "7").as_str())
    );
    assert_eq!(events[2].actor, None, "a stream event has no actor");
    let gift: serde_json::Value = serde_json::from_str(&events[1].payload_json).unwrap();
    assert_eq!(gift["metadata"]["total"], 2);
    assert!(
        !events[1].payload_json.contains("Giftee"),
        "giftee identities never published"
    );
    stop(run).await;
}
