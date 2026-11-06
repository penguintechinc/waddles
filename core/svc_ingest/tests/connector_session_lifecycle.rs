//! End-to-end (REAL driver + REAL `WsTransport` + a local Discord-shaped
//! gateway): HELLO announces the heartbeat interval, the host sends its
//! default heartbeat with the live sequence, op 7 reconnects, the next
//! `on-connect` sees the host-rendered session token and the RESUME on the
//! wire carries the captured session id + seq, and a 4004 close parks the
//! source instead of reconnecting forever.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use connection_resolver::{ConnectionRef, SecretString};
use futures_util::{SinkExt, StreamExt};
use svc_ingest::connector::secrets::{Destination, ResolvedAuth};
use svc_ingest::connector::webhook::{BoxFut, SecretError};
use svc_ingest::connector::{
    register_connector_metrics, CaptureRule, CloseCodeRule, ConnectionCtx, ConnectorDriver,
    ConnectorEvent, ConnectorInvokeError, ConnectorInvoker, ConnectorRegistry, EventSink,
    FrameDirective, FrameMatch, Handshake, HeartbeatCapture, LifecycleAction, PinnedConnector,
    RawFrame, RunEnd, Scalar, SessionRules, SinkError, TransportSecretSource, WsConfig,
    WsTransport,
};
use tokio::net::TcpListener;
use tokio::sync::oneshot;
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;
use tokio_tungstenite::tungstenite::protocol::CloseFrame;
use tokio_tungstenite::tungstenite::Message;

const PLATFORM: &str = "gw";
const REF: ConnectionRef = ConnectionRef {
    community_id: 1,
    connection_id: 2,
};

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
                secret: SecretString::new("tok123".into()),
                header: None,
            })
        })
    }
}

/// An overlay stand-in: builds IDENTIFY, or RESUME when the host handed it a
/// session token; never sees the heartbeat sequence (answers `none`).
struct Overlay {
    tokens: Mutex<Vec<Option<String>>>,
}

impl ConnectorInvoker for Overlay {
    async fn on_connect(
        &self,
        _p: &str,
        ctx: &ConnectionCtx,
    ) -> Result<Handshake, ConnectorInvokeError> {
        self.tokens.lock().unwrap().push(ctx.session_token.clone());
        let bytes: &[u8] = if ctx.session_token.is_some() {
            br#"{"op":6,"d":{"token":"{{TOKEN}}","session_id":"{{SESSION_ID}}","seq":{{SEQ}}}}"#
        } else {
            br#"{"op":2,"d":{"token":"{{TOKEN}}"}}"#
        };
        Ok(Handshake {
            bytes: bytes.to_vec(),
            secret_refs: vec![("{{TOKEN}}".into(), "bot".into())],
        })
    }
    async fn on_frame(
        &self,
        _p: &str,
        _f: RawFrame,
    ) -> Result<Vec<ConnectorEvent>, ConnectorInvokeError> {
        Ok(vec![])
    }
    async fn on_heartbeat_due(
        &self,
        _p: &str,
        _c: &ConnectionCtx,
    ) -> Result<Option<Vec<u8>>, ConnectorInvokeError> {
        Ok(None)
    }
    async fn on_disconnect(
        &self,
        _p: &str,
        _c: &ConnectionCtx,
        _r: &str,
    ) -> Result<(), ConnectorInvokeError> {
        Ok(())
    }
}

struct Null;

impl EventSink for Null {
    async fn publish(&self, _c: &ConnectionCtx, _e: ConnectorEvent) -> Result<(), SinkError> {
        Ok(())
    }
}

fn rules() -> SessionRules {
    let op = |n| ("/op".to_string(), Scalar::Int(n));
    SessionRules {
        captures: vec![
            CaptureRule {
                when: FrameMatch::Json(vec![op(0), ("/t".into(), Scalar::Str("READY".into()))]),
                pointer: "/d/session_id".into(),
                name: "SESSION_ID".into(),
            },
            CaptureRule {
                when: FrameMatch::Json(vec![op(0)]),
                pointer: "/s".into(),
                name: "SEQ".into(),
            },
        ],
        heartbeat: Some(HeartbeatCapture {
            when: FrameMatch::Json(vec![op(10)]),
            pointer: "/d/heartbeat_interval".into(),
        }),
        directives: vec![FrameDirective {
            when: FrameMatch::Json(vec![op(7)]),
            action: LifecycleAction::Reconnect,
        }],
        close_codes: vec![CloseCodeRule {
            from: 4004,
            to: 4004,
            action: LifecycleAction::Park,
        }],
        session_token_template: Some(r#"{"session_id":"{{SESSION_ID}}","seq":{{SEQ}}}"#.into()),
        default_heartbeat: Some(r#"{"op":1,"d":{{SEQ|null}}}"#.into()),
        host_owned_heartbeat: true,
        ..SessionRules::default()
    }
}

fn cfg(addr: SocketAddr) -> WsConfig {
    WsConfig {
        platform: PLATFORM.into(),
        url: format!("ws://{addr}/gw"),
        source_key: "k".into(),
        handles: HashMap::from([("bot".to_string(), REF)]),
        resume_token: None,
        connect_timeout: Duration::from_secs(2),
        idle_timeout: Duration::from_secs(10),
        max_lifetime: Duration::from_secs(60),
        egress_allow: vec!["127.0.0.1".parse().unwrap()],
        max_frame_bytes: 4096,
        dedup_window: 0,
        allow_loopback_insecure: true,
        slot_prefixes: HashMap::from([("bot".to_string(), vec![r#""token":""#.to_string()])]),
        auto_replies: vec![],
        self_echo: None,
        heartbeat_interval: None,
        session: rules(),
    }
}

async fn next_text(ws: &mut tokio_tungstenite::WebSocketStream<tokio::net::TcpStream>) -> String {
    loop {
        match tokio::time::timeout(Duration::from_secs(5), ws.next())
            .await
            .expect("client frame within 5s")
        {
            Some(Ok(Message::Text(t))) => return t.to_string(),
            Some(Ok(_)) => continue,
            other => panic!("unexpected client side: {other:?}"),
        }
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn hello_heartbeat_resume_and_park_end_to_end() {
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    let wire = Arc::new(Mutex::new(Vec::<String>::new()));
    let w = Arc::clone(&wire);
    tokio::spawn(async move {
        // Connection 1: HELLO(150 ms) -> IDENTIFY -> READY -> s=5 -> beat -> op 7.
        let (tcp, _) = l.accept().await.unwrap();
        let mut ws = tokio_tungstenite::accept_async(tcp).await.unwrap();
        ws.send(Message::text(r#"{"op":10,"d":{"heartbeat_interval":150}}"#))
            .await
            .unwrap();
        let line = next_text(&mut ws).await;
        w.lock().unwrap().push(line); // IDENTIFY
        ws.send(Message::text(
            r#"{"op":0,"t":"READY","s":1,"d":{"session_id":"abc123"}}"#,
        ))
        .await
        .unwrap();
        ws.send(Message::text(
            r#"{"op":0,"t":"MESSAGE_CREATE","s":5,"d":{}}"#,
        ))
        .await
        .unwrap();
        let line = next_text(&mut ws).await;
        w.lock().unwrap().push(line); // first heartbeat
        ws.send(Message::text(r#"{"op":7,"d":null}"#))
            .await
            .unwrap();
        // Connection 2: RESUME, then a fatal close.
        let (tcp, _) = l.accept().await.unwrap();
        let mut ws2 = tokio_tungstenite::accept_async(tcp).await.unwrap();
        let line = next_text(&mut ws2).await;
        w.lock().unwrap().push(line); // RESUME
        let _ = ws2
            .close(Some(CloseFrame {
                code: CloseCode::from(4004),
                reason: "auth".into(),
            }))
            .await;
        let _ = tokio::time::timeout(Duration::from_secs(1), ws2.next()).await;
    });

    let mut r = ConnectorRegistry::new();
    r.register(PinnedConnector::new(PLATFORM, "id", "sha256:aa", 4096))
        .unwrap();
    let metrics = register_connector_metrics(&prometheus::Registry::new()).unwrap();
    let overlay = Arc::new(Overlay {
        tokens: Mutex::default(),
    });
    let driver = ConnectorDriver::new(Arc::new(r), Arc::clone(&overlay), Arc::new(Null), metrics)
        .with_heartbeat_enabled(true)
        .with_backoff_cap(Duration::from_millis(20));
    let t = WsTransport::new(cfg(addr), Tok).unwrap();
    let (_stop, rx) = oneshot::channel::<()>();
    let end = tokio::time::timeout(Duration::from_secs(15), driver.run(t, rx))
        .await
        .expect("driver finished")
        .unwrap();

    assert_eq!(end, RunEnd::Parked, "4004 parks, no reconnect loop");
    let wire = wire.lock().unwrap().clone();
    assert_eq!(wire[0], r#"{"op":2,"d":{"token":"tok123"}}"#);
    assert_eq!(
        wire[1], r#"{"op":1,"d":5}"#,
        "host heartbeat with the live seq"
    );
    assert_eq!(
        wire[2],
        r#"{"op":6,"d":{"token":"tok123","session_id":"abc123","seq":5}}"#
    );
    let tokens = overlay.tokens.lock().unwrap().clone();
    assert_eq!(tokens[0], None);
    assert_eq!(
        tokens[1].as_deref(),
        Some(r#"{"session_id":"abc123","seq":5}"#)
    );
}
