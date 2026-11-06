//! Real-path proof of the Twitch `ConnectorProfile` (PR-F1): the `WsConfig` the
//! profile derives from a hub-api binding drives a REAL `WsTransport` against a
//! local fake IRC server, with the REAL compiled Twitch connector component
//! (`fixtures/twitch_connector.wasm`) running in a real bundle executor behind
//! svc_ingest's production wire invoker. Nothing between the socket and the
//! recorded event is mocked except the fake IRC peer, the scripted identity stage
//! (`scripted_bridge`, a fake identity), and the credential source.
//!
//! Test-only deviation from the derived config (in `derived_config`):
//! `RuntimeEnv` has no loopback switch and the URL is a host constant, so the
//! test rewrites `url` to the fake server and sets `allow_loopback_insecure`
//! AFTER asserting the derived values are the production constants. The
//! credential source is the production `TwitchFieldSecrets` wrapper over a
//! pass-only stand-in for `TransportSecrets` (the real one refuses loopback
//! hosts); the stand-in records every ref it is asked for, proving nick and
//! channel never reach the credential source.
//!
//! `fixtures/twitch_connector.wasm` is a build output of `bundles/rust/twitch`;
//! see `fixtures/README.md`.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::HashSet;
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
use connection_resolver::{BindingSummary, ConnectionKind, ConnectionRef, SecretString};
use connector_e2e::{digest_of, NoSource};
use futures_util::{SinkExt, StreamExt};
use svc_ingest::connector::secrets::{Destination, ResolvedAuth};
use svc_ingest::connector::webhook::{BoxFut, SecretError};
use svc_ingest::connector::{
    register_connector_metrics, ConnectionCtx, ConnectorDriver, ConnectorEvent, ConnectorRegistry,
    EventSink, ExecutorLink, PinnedConnector, SinkError, TransportSecretSource,
    WireConnectorInvoker, WsConfig, WsTransport,
};
use svc_ingest::connector_runtime::profiles::twitch::{
    TwitchFieldSecrets, TwitchProfile, APP_ID, IRC_URL,
};
use svc_ingest::connector_runtime::{ConnectorProfile, RuntimeEnv, TransportPlan};
use tokio::net::TcpListener;
use tokio::sync::{mpsc, oneshot};
use tokio_tungstenite::tungstenite::Message;

const WASM: &[u8] = include_bytes!("fixtures/twitch_connector.wasm");

const TOKEN: &str = "tok_FAKE_0123456789";
const BOT: &str = "waddlebot";
const CHANNEL: &str = "somechannel";
const MAX_FRAME: usize = 65_536;

/// The credential ref (the only one the credential source may be asked for).
const PASS_REF: ConnectionRef = ConnectionRef {
    community_id: 7,
    connection_id: 9,
};

/// Stand-in for the real credential source: answers the pass ref with the
/// token, refuses everything else, and records every ref it was asked for.
#[derive(Clone, Default)]
struct PassOnly(Arc<Mutex<Vec<ConnectionRef>>>);

impl TransportSecretSource for PassOnly {
    fn resolve(
        &self,
        requested: ConnectionRef,
        _dest: Destination,
    ) -> BoxFut<'_, Result<ResolvedAuth, SecretError>> {
        self.0.lock().unwrap().push(requested);
        Box::pin(async move {
            if requested != PASS_REF {
                return Err(SecretError::Missing);
            }
            Ok(ResolvedAuth {
                secret: SecretString::new(TOKEN.to_owned()),
                header: None,
            })
        })
    }
}

/// Records every published event.
#[derive(Clone, Default)]
struct RecordingSink(Arc<Mutex<Vec<ConnectorEvent>>>);

impl EventSink for RecordingSink {
    async fn publish(&self, _ctx: &ConnectionCtx, event: ConnectorEvent) -> Result<(), SinkError> {
        self.0.lock().unwrap().push(event);
        Ok(())
    }
}

fn binding() -> BindingSummary {
    BindingSummary {
        tenant_id: 42,
        secret_version: 1,
        community_id: 7,
        connection_id: 9,
        provider: "twitch".into(),
        kind: ConnectionKind::PlatformOauth,
        metadata: [
            ("channel", "#SomeChannel"),
            ("bot_login", "WaddleBot"),
            ("tenant_id", "42"),
        ]
        .into_iter()
        .map(|(k, v)| (k.to_owned(), v.to_owned()))
        .collect(),
    }
}

/// Plans the binding through the profile, asserts the production constants,
/// then applies the documented test-only rewrites.
fn derived_config(addr: std::net::SocketAddr) -> WsConfig {
    let env = RuntimeEnv {
        environment: "test".into(),
        max_frame_bytes: MAX_FRAME,
        egress_allow: vec![IpAddr::V4(Ipv4Addr::LOCALHOST)],
        heartbeat_enabled: true,
    };
    let mut planned = TwitchProfile::new(Arc::new(|_c: i64| Some(42)))
        .plan(&binding(), &env)
        .expect("plan");
    assert_eq!(planned.len(), 1, "one binding in, one connection out");
    let TransportPlan::Ws(cfg) = planned.remove(0).plan else {
        panic!("twitch plans a Ws transport");
    };
    let mut cfg = *cfg;
    assert_eq!(cfg.url, IRC_URL, "production URL is the host constant");
    cfg.url = format!("ws://{addr}/");
    cfg.allow_loopback_insecure = true;
    assert_eq!(cfg.handles["pass"], PASS_REF);
    cfg
}

/// A pinned real executor + wire link with the scripted fake-identity stage.
struct Rig {
    registry: Arc<ConnectorRegistry>,
    invoker: Arc<WireConnectorInvoker>,
    identity_calls: svc_ingest_calls::Log,
    _conn: tokio::task::JoinHandle<()>,
}

mod svc_ingest_calls {
    pub type Log = bundle_executor::parity_support::CallLog;
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
        .expect("the real twitch connector pins");
    let mut registry = ConnectorRegistry::new();
    registry
        .register(PinnedConnector::new("twitch", APP_ID, digest, MAX_FRAME))
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

/// What the fake IRC peer observed, in order.
#[derive(Debug, Clone, PartialEq, Eq)]
enum Seen {
    Handshake(String),
    Client(String),
}

/// Fake IRC server: reads the handshake, optionally runs a server-PING
/// exchange, then plays `lines` and keeps forwarding everything the client sends.
async fn serve(
    l: TcpListener,
    server_ping: bool,
    lines: Vec<String>,
    seen: mpsc::UnboundedSender<Seen>,
) {
    let (tcp, _) = l.accept().await.unwrap();
    let mut ws = tokio_tungstenite::accept_async(tcp).await.unwrap();
    let Some(Ok(Message::Text(hs))) = ws.next().await else {
        panic!("no handshake frame");
    };
    seen.send(Seen::Handshake(hs.to_string())).unwrap();
    if server_ping {
        ws.send(Message::text("PING :tmi.twitch.tv\r\n"))
            .await
            .unwrap();
        loop {
            let Some(Ok(Message::Text(t))) = ws.next().await else {
                return;
            };
            seen.send(Seen::Client(t.to_string())).unwrap();
            if t.starts_with("PONG") {
                break;
            }
        }
    }
    for l in lines {
        ws.send(Message::text(l)).await.unwrap();
    }
    while let Some(Ok(m)) = ws.next().await {
        if let Message::Text(t) = m {
            seen.send(Seen::Client(t.to_string())).unwrap();
        }
    }
}

struct Run {
    asked: PassOnly,
    events: Arc<Mutex<Vec<ConnectorEvent>>>,
    seen: mpsc::UnboundedReceiver<Seen>,
    shutdown: oneshot::Sender<()>,
    task: tokio::task::JoinHandle<()>,
    rig: Rig,
}

async fn start(server_ping: bool, lines: Vec<String>, heartbeat: Option<Duration>) -> Run {
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    let (tx, seen) = mpsc::unbounded_channel();
    tokio::spawn(serve(l, server_ping, lines, tx));
    let mut cfg = derived_config(addr);
    if let Some(h) = heartbeat {
        cfg.heartbeat_interval = Some(h);
    }
    let rig = rig().await;
    let asked = PassOnly::default();
    let secrets = TwitchFieldSecrets::from_config(asked.clone(), &cfg).expect("field secrets");
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
        asked,
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
        .expect("fake IRC peer saw nothing in time")
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

const VIEWER_LINE: &str = "@display-name=Viewer1;id=m-1;room-id=555;user-id=12345 :viewer1!viewer1@viewer1.tmi.twitch.tv PRIVMSG #somechannel :hello chat\r\n";
const OWN_LINE: &str = "@display-name=WaddleBot;id=m-2;room-id=555;user-id=777 :waddlebot!waddlebot@waddlebot.tmi.twitch.tv PRIVMSG #somechannel :my own reply\r\n";
const LATER_LINE: &str = "@display-name=Viewer2;id=m-3;room-id=555;user-id=999 :viewer2!viewer2@viewer2.tmi.twitch.tv PRIVMSG #somechannel :second\r\n";

/// The whole inbound path: exact handshake bytes, host-answered PING, a
/// normalized event with a UUID actor, and the own-message echo dropped.
#[tokio::test(flavor = "multi_thread")]
async fn derived_config_drives_handshake_pong_event_and_echo_drop() {
    let mut run = start(
        true,
        vec![OWN_LINE.into(), VIEWER_LINE.into(), LATER_LINE.into()],
        None,
    )
    .await;

    // Handshake bytes on the wire, exactly: slots substituted by the host.
    let Seen::Handshake(hs) = next_seen(&mut run).await else {
        panic!("first thing the peer sees must be the handshake");
    };
    assert_eq!(
        hs,
        format!(
            "CAP REQ :twitch.tv/tags\r\nPASS oauth:{TOKEN}\r\nNICK {BOT}\r\nJOIN #{CHANNEL}\r\n"
        )
    );

    // The HOST answers the server PING (the guest cannot).
    assert_eq!(
        next_seen(&mut run).await,
        Seen::Client("PONG :tmi.twitch.tv\r\n".into())
    );

    // Viewer1 and Viewer2 become events; the bot's own echo (sent FIRST) is
    // dropped, so exactly two events arrive, in order.
    wait_events(&run, 2).await;
    tokio::time::sleep(Duration::from_millis(300)).await;
    let events = run.events.lock().unwrap().clone();
    assert_eq!(events.len(), 2, "own echo dropped, nothing else lost");
    for (e, (uid, text)) in events
        .iter()
        .zip([("12345", "hello chat"), ("999", "second")])
    {
        assert_eq!(e.platform, "twitch");
        assert_eq!(e.event_type, "message");
        let actor = e.actor.as_deref().expect("actor");
        assert!(is_uuid_shaped(actor), "{actor}");
        assert_eq!(actor, pseudonym_uuid("twitch", uid));
        assert!(!actor.contains("viewer"), "no raw handle in the actor");
        let p: serde_json::Value = serde_json::from_str(&e.payload_json).unwrap();
        assert_eq!(p["text"], text);
        assert_eq!(p["channel_name"], CHANNEL);
    }
    assert_eq!(
        *run.asked.0.lock().unwrap(),
        vec![PASS_REF],
        "only the pass handle ever reached the credential source"
    );
    assert!(
        !events
            .iter()
            .any(|e| e.payload_json.contains("my own reply")),
        "the bot's own message must not be ingested"
    );
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

/// The profile's heartbeat reaches the guest: the client PING goes out.
#[tokio::test(flavor = "multi_thread")]
async fn heartbeat_sends_the_client_ping() {
    let mut run = start(false, vec![], Some(Duration::from_millis(150))).await;
    let Seen::Handshake(_) = next_seen(&mut run).await else {
        panic!("handshake first");
    };
    let Seen::Client(ping) = next_seen(&mut run).await else {
        panic!("client ping expected");
    };
    assert!(ping.starts_with("PING"), "{ping:?}");
    stop(run).await;
}
