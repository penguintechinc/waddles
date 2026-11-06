//! Real-path proof of the Discord `ConnectorProfile` (PR-F2): the config the
//! profile DERIVES for a hub-api binding drives the REAL `ConnectorDriver` and
//! `WsTransport` against a local fake gateway, with the REAL compiled Discord
//! connector running in the REAL bundle executor over the host-API wire.
//!
//! Scripted gateway (every claim is asserted on the server's wire):
//! HELLO -> IDENTIFY (token slot substituted, only AFTER HELLO) -> READY with
//! `resume_gateway_url` and the bot's user id -> the profile's READY directive
//! ends the connection with state kept -> RESUME on the CAPTURED url carrying
//! `self_user_id` in the session token -> RESUMED -> the bot's OWN
//! `MESSAGE_CREATE` is dropped by the connector (via `self_user_id`) while a
//! foreign one reaches identity lookup (denied on this wire: no host-call
//! bridge yet) -> missed heartbeat ACK -> RESUME again -> op 7 reconnect ->
//! RESUME -> close 4004 parks.

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::result_large_err)]

use std::collections::{BTreeMap, HashSet};
use std::net::SocketAddr;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use bundle_executor::config::CliConfig;
use bundle_executor::connector::ConnectorLimits;
use bundle_executor::heartbeat::Heartbeat;
use bundle_executor::invoke::Executor;
use bundle_executor::manifest::{VerifiedManifest, WitWorld, PERM_CONNECTOR_PII_READ};
use bundle_executor::wire::run_connection;
use clap::Parser;
use connection_resolver::{BindingSummary, ConnectionKind, ConnectionRef, SecretString};
use connector_e2e::{digest_of, NoSource};
use futures_util::{SinkExt, StreamExt};
use svc_ingest::connector::secrets::{Destination, ResolvedAuth};
use svc_ingest::connector::webhook::{BoxFut, SecretError};
use svc_ingest::connector::{
    register_connector_metrics, ConnectionCtx, ConnectorDriver, ConnectorEvent, ConnectorRegistry,
    EventSink, ExecutorLink, PinnedConnector, RunEnd, SinkError, TransportSecretSource,
    WireConnectorInvoker, WsTransport,
};
use svc_ingest::connector_runtime::profiles::discord::{DiscordProfile, APP_ID};
use svc_ingest::connector_runtime::{ConnectorProfile, RuntimeEnv, TransportPlan};
use tokio::net::TcpListener;
use tokio::sync::oneshot;
use tokio_tungstenite::tungstenite::handshake::server::{Request, Response};
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;
use tokio_tungstenite::tungstenite::protocol::CloseFrame;
use tokio_tungstenite::tungstenite::Message;

const WASM: &[u8] = include_bytes!("../../bundle_executor/tests/fixtures/discord_connector.wasm");
const GUILD: &str = "123456789012345678";
const BOT_ID: &str = "999999999999999999";
const TOKEN: &str = "bot-token-xyz";

#[derive(Clone)]
struct Tok;

impl TransportSecretSource for Tok {
    fn resolve(
        &self,
        _r: ConnectionRef,
        _d: Destination,
    ) -> BoxFut<'_, Result<ResolvedAuth, SecretError>> {
        Box::pin(async {
            Ok(ResolvedAuth {
                secret: SecretString::new(TOKEN.into()),
                header: None,
            })
        })
    }
}

#[derive(Clone, Default)]
struct CountingSink(Arc<Mutex<u64>>);

impl EventSink for CountingSink {
    async fn publish(&self, _c: &ConnectionCtx, _e: ConnectorEvent) -> Result<(), SinkError> {
        *self.0.lock().unwrap() += 1;
        Ok(())
    }
}

type Ws = tokio_tungstenite::WebSocketStream<tokio::net::TcpStream>;

async fn next_text(ws: &mut Ws) -> String {
    loop {
        match tokio::time::timeout(Duration::from_secs(10), ws.next())
            .await
            .expect("client frame within 10s")
        {
            Some(Ok(Message::Text(t))) => return t.to_string(),
            Some(Ok(_)) => continue,
            other => panic!("unexpected client side: {other:?}"),
        }
    }
}

/// Reads until the client closes; returns (texts seen, close code).
async fn drain_to_close(ws: &mut Ws) -> (Vec<String>, Option<u16>) {
    let mut texts = Vec::new();
    loop {
        match tokio::time::timeout(Duration::from_secs(10), ws.next()).await {
            Ok(Some(Ok(Message::Text(t)))) => texts.push(t.to_string()),
            Ok(Some(Ok(Message::Close(f)))) => return (texts, f.map(|f| u16::from(f.code))),
            Ok(Some(Ok(_))) => {}
            _ => return (texts, None),
        }
    }
}

fn hello() -> Message {
    Message::text(r#"{"op":10,"d":{"heartbeat_interval":200}}"#)
}

#[derive(Default)]
struct Wire {
    silent_before_hello: bool,
    identify: Option<String>,
    resume_uris: Vec<String>,
    /// RESUME texts in arrival order, one per resume-host connection.
    resumes: Vec<String>,
    heartbeats_before_zombie_close: usize,
    zombie_close_code: Option<u16>,
}

/// Executor + wire link with the REAL Discord connector pinned (PII-read granted:
/// the connector imports `identity`).
async fn real_invoker() -> Arc<WireConnectorInvoker> {
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
    executor
        .pin_connector(
            &manifest,
            WASM.to_vec(),
            ConnectorLimits::from_config(&cfg),
            None,
        )
        .await
        .unwrap();
    let mut registry = ConnectorRegistry::new();
    registry
        .register(PinnedConnector::new("discord", APP_ID, digest, 65_536))
        .unwrap();
    let registry = Arc::new(registry);
    let link = Arc::new(ExecutorLink::new());
    let (stage_io, exec_io) = tokio::io::duplex(4 << 20);
    let hello = executor.hello(false, "runc");
    let exec = Arc::clone(&executor);
    tokio::spawn(async move {
        let _ = run_connection(exec_io, hello, exec, "profile-e2e", Heartbeat::disabled()).await;
    });
    link.attach(stage_io).await.unwrap();
    // Keep the executor alive for the whole test.
    std::mem::forget(executor);
    Arc::new(WireConnectorInvoker::new(link, registry))
}

fn count(reg: &prometheus::Registry, name: &str, outcome: &str) -> u64 {
    reg.gather()
        .iter()
        .filter(|f| f.name() == name)
        .flat_map(|f| f.get_metric())
        .filter(|m| {
            m.get_label()
                .iter()
                .any(|l| l.name() == "outcome" && l.value() == outcome)
        })
        .map(|m| m.get_counter().value() as u64)
        .sum()
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn discord_profile_drives_the_real_connector_through_the_whole_gateway_lifecycle() {
    let gw = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let resume = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let gw_addr: SocketAddr = gw.local_addr().unwrap();
    let resume_addr: SocketAddr = resume.local_addr().unwrap();
    let wire = Arc::new(Mutex::new(Wire::default()));

    // Original gateway: HELLO -> IDENTIFY -> READY, then the client ends the
    // connection itself (the profile's READY directive).
    let w = Arc::clone(&wire);
    tokio::spawn(async move {
        let (tcp, _) = gw.accept().await.unwrap();
        let mut ws = tokio_tungstenite::accept_async(tcp).await.unwrap();
        let early = tokio::time::timeout(Duration::from_millis(300), ws.next()).await;
        w.lock().unwrap().silent_before_hello = early.is_err();
        ws.send(hello()).await.unwrap();
        let identify = next_text(&mut ws).await;
        w.lock().unwrap().identify = Some(identify);
        ws.send(Message::text(format!(
            r#"{{"op":0,"t":"READY","s":1,"d":{{"session_id":"sess1","resume_gateway_url":"ws://{resume_addr}","user":{{"id":"{BOT_ID}"}}}}}}"#
        )))
        .await
        .unwrap();
        // The bot's own message right behind READY: this connection has no
        // session token (it started with a fresh IDENTIFY), so it must never
        // be read -- the READY directive ends the connection first.
        ws.send(Message::text(format!(
            r#"{{"op":0,"t":"MESSAGE_CREATE","s":2,"d":{{"id":"551","channel_id":"222","guild_id":"{GUILD}","content":"hi","author":{{"id":"{BOT_ID}","username":"u","bot":false}},"type":0}}}}"#
        )))
        .await
        .unwrap();
        let _ = drain_to_close(&mut ws).await;
    });

    // Resume host: three consecutive connections, scripted by index.
    let w = Arc::clone(&wire);
    tokio::spawn(async move {
        for n in 0..3u8 {
            let (tcp, _) = resume.accept().await.unwrap();
            let uri = Arc::new(Mutex::new(String::new()));
            let u = Arc::clone(&uri);
            let mut ws =
                tokio_tungstenite::accept_hdr_async(tcp, move |r: &Request, resp: Response| {
                    *u.lock().unwrap() = r.uri().to_string();
                    Ok(resp)
                })
                .await
                .unwrap();
            w.lock()
                .unwrap()
                .resume_uris
                .push(uri.lock().unwrap().clone());
            ws.send(hello()).await.unwrap();
            let resume_text = next_text(&mut ws).await;
            w.lock().unwrap().resumes.push(resume_text);
            match n {
                0 => {
                    ws.send(Message::text(r#"{"op":0,"t":"RESUMED","s":2,"d":{}}"#))
                        .await
                        .unwrap();
                    // The bot's own message, then a foreign one.
                    for (s, author) in [(3, BOT_ID), (4, "444444444444444444")] {
                        ws.send(Message::text(format!(
                            r#"{{"op":0,"t":"MESSAGE_CREATE","s":{s},"d":{{"id":"55{s}","channel_id":"222","guild_id":"{GUILD}","content":"hi","author":{{"id":"{author}","username":"u","bot":false}},"type":0}}}}"#
                        )))
                        .await
                        .unwrap();
                    }
                    // Never ACK: heartbeat 1 is sent, heartbeat 2 becomes a close.
                    let (texts, code) = drain_to_close(&mut ws).await;
                    let mut g = w.lock().unwrap();
                    g.heartbeats_before_zombie_close = texts.len();
                    g.zombie_close_code = code;
                }
                1 => {
                    ws.send(Message::text(r#"{"op":7,"d":null}"#))
                        .await
                        .unwrap();
                    let _ = drain_to_close(&mut ws).await;
                }
                _ => {
                    let _ = ws
                        .close(Some(CloseFrame {
                            code: CloseCode::from(4004),
                            reason: "auth".into(),
                        }))
                        .await;
                    let _ = tokio::time::timeout(Duration::from_secs(1), ws.next()).await;
                }
            }
        }
    });

    // The config under test: derived by the profile, then pointed at loopback
    // exactly as the production allow-list/test switches allow (url, loopback
    // switch and the resume allow-list are the ONLY overrides).
    let profile = DiscordProfile::new().with_budget_ready(true);
    let env = RuntimeEnv {
        environment: "test".into(),
        max_frame_bytes: 65_536,
        egress_allow: vec!["127.0.0.1".parse().unwrap()],
        heartbeat_enabled: true,
    };
    let binding = BindingSummary {
        tenant_id: 1,
        secret_version: 1,
        community_id: 7,
        connection_id: 11,
        provider: "discord".into(),
        kind: ConnectionKind::PlatformOauth,
        metadata: BTreeMap::from([("guild_id".to_string(), GUILD.to_string())]),
    };
    let desired = profile.plan(&binding, &env).unwrap();
    assert_eq!(desired.len(), 1);
    let TransportPlan::Ws(mut cfg) = desired.into_iter().next().unwrap().plan else {
        panic!("discord plans a Ws transport");
    };
    cfg.url = format!("ws://{gw_addr}/gw");
    cfg.allow_loopback_insecure = true;
    cfg.session.reconnect_url.as_mut().unwrap().allowed_hosts = vec!["127.0.0.1".into()];

    let reg = prometheus::Registry::new();
    let metrics = register_connector_metrics(&reg).unwrap();
    let sink = CountingSink::default();
    let invoker = real_invoker().await;
    let mut registry = ConnectorRegistry::new();
    registry
        .register(PinnedConnector::new(
            "discord",
            APP_ID,
            digest_of(WASM),
            65_536,
        ))
        .unwrap();
    let driver = ConnectorDriver::new(Arc::new(registry), invoker, Arc::new(sink.clone()), metrics)
        .with_heartbeat_enabled(true)
        .with_backoff_cap(Duration::from_millis(50));
    let transport = WsTransport::new(*cfg, Tok).unwrap();
    let (_stop, rx) = oneshot::channel::<()>();
    let end = tokio::time::timeout(Duration::from_secs(60), driver.run(transport, rx))
        .await
        .expect("driver finished")
        .unwrap();
    assert_eq!(end, RunEnd::Parked, "4004 parks the source");

    let w = wire.lock().unwrap();
    assert!(w.silent_before_hello, "nothing sent before HELLO");
    let identify: serde_json::Value = serde_json::from_str(w.identify.as_ref().unwrap()).unwrap();
    assert_eq!(identify["op"], 2);
    assert_eq!(
        identify["d"]["token"], TOKEN,
        "slot substituted by the host"
    );
    assert_eq!(identify["d"]["intents"], 33_281);
    assert!(identify["d"].get("shard").is_none(), "single shard");
    assert!(!w.identify.as_ref().unwrap().contains("{{"));

    assert_eq!(
        w.resume_uris.len(),
        3,
        "READY, missed ACK and op 7 each reconnected"
    );
    for u in &w.resume_uris {
        assert!(
            u.contains("v=10&encoding=json"),
            "captured url + host query: {u}"
        );
    }
    let resumes: Vec<serde_json::Value> = w
        .resumes
        .iter()
        .map(|t| serde_json::from_str(t).unwrap())
        .collect();
    for r in &resumes {
        assert_eq!(r["op"], 6, "RESUME, never a second IDENTIFY");
        assert_eq!(r["d"]["token"], TOKEN);
        assert_eq!(r["d"]["session_id"], "sess1");
    }
    assert_eq!(resumes[0]["d"]["seq"], 1, "resume right after READY");
    assert_eq!(resumes[1]["d"]["seq"], 4, "sequence tracked by the host");
    assert_eq!(resumes[2]["d"]["seq"], 4);
    assert_eq!(
        w.heartbeats_before_zombie_close, 1,
        "exactly one unacked beat"
    );
    assert_eq!(
        w.zombie_close_code,
        Some(4000),
        "zombie close keeps the session resumable"
    );

    // Self-message filter: of the two MESSAGE_CREATEs only the foreign one
    // reached identity lookup (denied on this wire -> guest_error). If
    // `self_user_id` were not captured and rendered, the bot's own message
    // would have failed too (2).
    assert_eq!(
        count(&reg, "svc_ingest_connector_frames_total", "guest_error"),
        1
    );
    assert!(count(&reg, "svc_ingest_connector_frames_total", "ok") >= 2);
    assert_eq!(*sink.0.lock().unwrap(), 0, "no event reached the sink");
}
