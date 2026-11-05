//! Real-path tests for the three Discord-gateway gaps the host session
//! lifecycle left open: (1) the validated `resume_gateway_url` reconnect
//! target, (2) the heartbeat-ACK timeout (zombie connection -> RESUME), and
//! (3) `handshake_after: first_frame` (HELLO before IDENTIFY/RESUME). The REAL
//! driver + REAL `WsTransport` talk to local WebSocket servers playing the
//! gateway and the resume host; every claim is asserted on the server's wire.

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::result_large_err)]

use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use connection_resolver::{ConnectionRef, SecretString};
use futures_util::{SinkExt, StreamExt};
use svc_ingest::connector::secrets::{Destination, ResolvedAuth};
use svc_ingest::connector::webhook::{BoxFut, SecretError};
use svc_ingest::connector::{
    register_connector_metrics, CaptureRule, CloseCodeRule, ConnectionCtx, ConnectionEnd,
    ConnectorDriver, ConnectorEvent, ConnectorInvokeError, ConnectorInvoker, ConnectorRegistry,
    EventSink, FrameDirective, FrameMatch, FrameRuleMetrics, Handshake, HandshakeAfter,
    HeartbeatAck, HeartbeatCapture, LifecycleAction, LifecycleError, PinnedConnector, RawFrame,
    ReconnectUrlCapture, RunEnd, Scalar, SessionEngine, SessionRules, SinkError, Transport,
    TransportError, TransportSecretSource, WsConfig, WsTransport,
};
use tokio::net::TcpListener;
use tokio::sync::oneshot;
use tokio_tungstenite::tungstenite::handshake::server::{Request, Response};
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;
use tokio_tungstenite::tungstenite::protocol::CloseFrame;
use tokio_tungstenite::tungstenite::Message;

const PLATFORM: &str = "gw";
const REF: ConnectionRef = ConnectionRef {
    community_id: 1,
    connection_id: 2,
};
const IDENTIFY: &str = r#"{"op":2,"d":{"token":"tok123"}}"#;
const RESUME: &str = r#"{"op":6,"d":{"token":"tok123","session_id":"abc123","seq":5}}"#;

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

/// Overlay stand-in: IDENTIFY, or RESUME when the host handed it a session.
struct Overlay;

impl ConnectorInvoker for Overlay {
    async fn on_connect(
        &self,
        _p: &str,
        ctx: &ConnectionCtx,
    ) -> Result<Handshake, ConnectorInvokeError> {
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

fn op(n: i64) -> (String, Scalar) {
    ("/op".to_string(), Scalar::Int(n))
}

/// The Discord host config (also the docs example), with a loopback allow-list
/// standing in for `*.discord.gg`.
fn rules(allowed: &[&str], max_unacked: u8, after: HandshakeAfter) -> SessionRules {
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
        reconnect_url: Some(ReconnectUrlCapture {
            when: FrameMatch::Json(vec![op(0), ("/t".into(), Scalar::Str("READY".into()))]),
            pointer: "/d/resume_gateway_url".into(),
            allowed_hosts: allowed.iter().map(|s| (*s).to_string()).collect(),
            query: "v=10&encoding=json".into(),
        }),
        heartbeat_ack: Some(HeartbeatAck {
            when: FrameMatch::Json(vec![op(11)]),
            max_unacked,
        }),
        handshake_after: after,
        jitter_first_heartbeat: false,
    }
}

const FIRST_FRAME: HandshakeAfter = HandshakeAfter::FirstFrame {
    timeout: Duration::from_secs(2),
};

fn cfg(addr: SocketAddr, session: SessionRules) -> WsConfig {
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
        slot_prefixes: vec![r#""token":""#.into()],
        auto_replies: vec![],
        self_echo: None,
        heartbeat_interval: None,
        session,
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

/// Reads until the client's Close frame; returns (texts seen, close code).
async fn drain_to_close(
    ws: &mut tokio_tungstenite::WebSocketStream<tokio::net::TcpStream>,
) -> (Vec<String>, Option<u16>) {
    let mut texts = Vec::new();
    loop {
        match tokio::time::timeout(Duration::from_secs(5), ws.next()).await {
            Ok(Some(Ok(Message::Text(t)))) => texts.push(t.to_string()),
            Ok(Some(Ok(Message::Close(f)))) => return (texts, f.map(|f| u16::from(f.code))),
            Ok(Some(Ok(_))) => {}
            _ => return (texts, None),
        }
    }
}

fn driver(metrics: &prometheus::Registry) -> ConnectorDriver<Overlay, Null> {
    let mut r = ConnectorRegistry::new();
    r.register(PinnedConnector::new(PLATFORM, "id", "sha256:aa", 4096))
        .unwrap();
    let m = register_connector_metrics(metrics).unwrap();
    ConnectorDriver::new(Arc::new(r), Arc::new(Overlay), Arc::new(Null), m)
        .with_heartbeat_enabled(true)
        .with_backoff_cap(Duration::from_millis(20))
}

#[derive(Default)]
struct Wire {
    /// Client texts, in arrival order, tagged with the server that saw them.
    lines: Vec<String>,
    silent_before_hello: bool,
    close_code: Option<u16>,
    resume_uri: Option<String>,
}

fn count_total(reg: &prometheus::Registry, name: &str, label: (&str, &str)) -> u64 {
    reg.gather()
        .iter()
        .filter(|f| f.name() == name)
        .flat_map(|f| f.get_metric())
        .filter(|m| {
            m.get_label()
                .iter()
                .any(|l| l.name() == label.0 && l.value() == label.1)
        })
        .map(|m| m.get_counter().value() as u64)
        .sum()
}

/// HELLO -> (host waits) -> IDENTIFY -> READY(resume_gateway_url) -> dispatch
/// -> heartbeat 1 never ACKed -> heartbeat 2 due = zombie -> close != 1000 ->
/// RESUME dialled on the RESUME host (not the original gateway), with the
/// configured query, session id and seq on the wire.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn missed_ack_reconnects_to_the_resume_url_and_resumes() {
    let gw = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let resume_host = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let gw_addr = gw.local_addr().unwrap();
    let resume_addr = resume_host.local_addr().unwrap();
    let wire = Arc::new(Mutex::new(Wire::default()));

    let w = Arc::clone(&wire);
    tokio::spawn(async move {
        let (tcp, _) = gw.accept().await.unwrap();
        let mut ws = tokio_tungstenite::accept_async(tcp).await.unwrap();
        // The client must NOT speak before HELLO.
        let early = tokio::time::timeout(Duration::from_millis(300), ws.next()).await;
        w.lock().unwrap().silent_before_hello = early.is_err();
        ws.send(Message::text(r#"{"op":10,"d":{"heartbeat_interval":200}}"#))
            .await
            .unwrap();
        let identify = next_text(&mut ws).await;
        w.lock().unwrap().lines.push(identify);
        ws.send(Message::text(format!(
            r#"{{"op":0,"t":"READY","s":1,"d":{{"session_id":"abc123","resume_gateway_url":"ws://{resume_addr}"}}}}"#
        )))
        .await
        .unwrap();
        ws.send(Message::text(
            r#"{"op":0,"t":"MESSAGE_CREATE","s":5,"d":{}}"#,
        ))
        .await
        .unwrap();
        // Never ACK: heartbeat 1 arrives, heartbeat 2 is replaced by a close.
        let (texts, code) = drain_to_close(&mut ws).await;
        let mut g = w.lock().unwrap();
        g.lines.extend(texts);
        g.close_code = code;
    });
    let w = Arc::clone(&wire);
    tokio::spawn(async move {
        let (tcp, _) = resume_host.accept().await.unwrap();
        let uri = Arc::new(Mutex::new(String::new()));
        let u = Arc::clone(&uri);
        let mut ws =
            tokio_tungstenite::accept_hdr_async(tcp, move |r: &Request, resp: Response| {
                *u.lock().unwrap() = r.uri().to_string();
                Ok(resp)
            })
            .await
            .unwrap();
        w.lock().unwrap().resume_uri = Some(uri.lock().unwrap().clone());
        ws.send(Message::text(r#"{"op":10,"d":{"heartbeat_interval":200}}"#))
            .await
            .unwrap();
        let resume = next_text(&mut ws).await;
        w.lock().unwrap().lines.push(format!("RESUMEHOST:{resume}"));
        let _ = ws
            .close(Some(CloseFrame {
                code: CloseCode::from(4004),
                reason: "auth".into(),
            }))
            .await;
        let _ = tokio::time::timeout(Duration::from_secs(1), ws.next()).await;
    });

    let reg = prometheus::Registry::new();
    let frm_reg = prometheus::Registry::new();
    let frm = FrameRuleMetrics::register(&frm_reg).unwrap();
    let d = driver(&reg);
    let t = WsTransport::new(cfg(gw_addr, rules(&["127.0.0.1"], 1, FIRST_FRAME)), Tok)
        .unwrap()
        .with_metrics(frm);
    let (_stop, rx) = oneshot::channel::<()>();
    let end = tokio::time::timeout(Duration::from_secs(20), d.run(t, rx))
        .await
        .expect("driver finished")
        .unwrap();
    assert_eq!(end, RunEnd::Parked, "4004 on the resume host parks");

    let w = wire.lock().unwrap();
    assert!(w.silent_before_hello, "handshake must wait for HELLO");
    assert_eq!(w.lines[0], IDENTIFY);
    assert_eq!(w.lines[1], r#"{"op":1,"d":5}"#, "exactly one heartbeat");
    assert_eq!(
        w.lines.len(),
        3,
        "the second heartbeat is NOT sent: {:?}",
        w.lines
    );
    assert_eq!(
        w.close_code,
        Some(4000),
        "dead connection closed with a non-1000 code (keeps the session resumable)"
    );
    assert_eq!(
        w.resume_uri.as_deref(),
        Some("/?v=10&encoding=json"),
        "RESUME dials the resume host with the configured query"
    );
    assert_eq!(w.lines[2], format!("RESUMEHOST:{RESUME}"));
    drop(w);

    assert_eq!(
        count_total(
            &frm_reg,
            "svc_ingest_transport_heartbeat_ack_timeout_total",
            ("platform", PLATFORM)
        ),
        1
    );
    assert_eq!(
        count_total(
            &frm_reg,
            "svc_ingest_transport_frame_rules_total",
            ("outcome", "used")
        ),
        1
    );
}

/// Control: with ACKs flowing the zombie rule never fires, however many beats.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn acknowledged_heartbeats_never_trip_the_timeout() {
    let gw = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = gw.local_addr().unwrap();
    let beats = Arc::new(Mutex::new(0usize));
    let b = Arc::clone(&beats);
    tokio::spawn(async move {
        let (tcp, _) = gw.accept().await.unwrap();
        let mut ws = tokio_tungstenite::accept_async(tcp).await.unwrap();
        ws.send(Message::text(r#"{"op":10,"d":{"heartbeat_interval":120}}"#))
            .await
            .unwrap();
        let _identify = next_text(&mut ws).await;
        // ACK five beats, then end with a fatal close.
        for _ in 0..5 {
            let beat = next_text(&mut ws).await;
            assert!(beat.starts_with(r#"{"op":1"#));
            *b.lock().unwrap() += 1;
            ws.send(Message::text(r#"{"op":11}"#)).await.unwrap();
        }
        let _ = ws
            .close(Some(CloseFrame {
                code: CloseCode::from(4004),
                reason: "x".into(),
            }))
            .await;
        let _ = tokio::time::timeout(Duration::from_secs(1), ws.next()).await;
    });
    let frm_reg = prometheus::Registry::new();
    let frm = FrameRuleMetrics::register(&frm_reg).unwrap();
    let d = driver(&prometheus::Registry::new());
    let t = WsTransport::new(cfg(addr, rules(&["127.0.0.1"], 1, FIRST_FRAME)), Tok)
        .unwrap()
        .with_metrics(frm);
    let (_stop, rx) = oneshot::channel::<()>();
    let end = tokio::time::timeout(Duration::from_secs(20), d.run(t, rx))
        .await
        .expect("driver finished")
        .unwrap();
    assert_eq!(end, RunEnd::Parked);
    assert_eq!(*beats.lock().unwrap(), 5);
    assert_eq!(
        count_total(
            &frm_reg,
            "svc_ingest_transport_heartbeat_ack_timeout_total",
            ("platform", PLATFORM)
        ),
        0
    );
}

/// Without `handshake_after`, IDENTIFY goes out BEFORE HELLO is read (the
/// unchanged default every existing transport keeps).
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn default_handshake_is_still_sent_before_the_first_frame() {
    let gw = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = gw.local_addr().unwrap();
    let first = Arc::new(Mutex::new(None::<String>));
    let f = Arc::clone(&first);
    tokio::spawn(async move {
        let (tcp, _) = gw.accept().await.unwrap();
        let mut ws = tokio_tungstenite::accept_async(tcp).await.unwrap();
        // Server stays silent: the client's handshake must arrive unprompted.
        let line = next_text(&mut ws).await;
        *f.lock().unwrap() = Some(line);
        let _ = ws
            .close(Some(CloseFrame {
                code: CloseCode::from(4004),
                reason: "x".into(),
            }))
            .await;
        let _ = tokio::time::timeout(Duration::from_secs(1), ws.next()).await;
    });
    let d = driver(&prometheus::Registry::new());
    let t = WsTransport::new(
        cfg(addr, rules(&["127.0.0.1"], 1, HandshakeAfter::Connect)),
        Tok,
    )
    .unwrap();
    assert_eq!(t.handshake_after_first_frame(), None);
    let (_stop, rx) = oneshot::channel::<()>();
    let end = tokio::time::timeout(Duration::from_secs(20), d.run(t, rx))
        .await
        .expect("driver finished")
        .unwrap();
    assert_eq!(end, RunEnd::Parked);
    assert_eq!(first.lock().unwrap().as_deref(), Some(IDENTIFY));
}

/// No first frame within the bounded wait: loud failure, NOTHING sent (no
/// IDENTIFY, no secret on the wire), counted.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn first_frame_timeout_fails_loudly_and_sends_nothing() {
    let gw = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = gw.local_addr().unwrap();
    let seen = Arc::new(Mutex::new(Vec::<String>::new()));
    let s = Arc::clone(&seen);
    tokio::spawn(async move {
        let (tcp, _) = gw.accept().await.unwrap();
        let mut ws = tokio_tungstenite::accept_async(tcp).await.unwrap();
        // Silent server; record anything the client writes until it closes.
        let (texts, _) = drain_to_close(&mut ws).await;
        s.lock().unwrap().extend(texts);
    });
    let reg = prometheus::Registry::new();
    let d = driver(&reg);
    let after = HandshakeAfter::FirstFrame {
        timeout: Duration::from_millis(300),
    };
    let mut t = WsTransport::new(cfg(addr, rules(&["127.0.0.1"], 1, after)), Tok).unwrap();
    let (_stop, mut rx) = oneshot::channel::<()>();
    let report = tokio::time::timeout(Duration::from_secs(10), d.serve_connection(&mut t, &mut rx))
        .await
        .expect("serve_connection finished")
        .unwrap();
    assert_eq!(report.end, ConnectionEnd::HandshakeFailed);
    assert!(!report.established);
    tokio::time::sleep(Duration::from_millis(200)).await;
    assert!(
        seen.lock().unwrap().is_empty(),
        "no handshake bytes before HELLO: {:?}",
        seen.lock().unwrap()
    );
    assert_eq!(
        count_total(
            &reg,
            "svc_ingest_connector_connects_total",
            ("outcome", "first_frame_timeout")
        ),
        1
    );
}

fn ready_with_url(url: &str) -> String {
    serde_json::json!({"op":0,"t":"READY","s":1,"d":{"session_id":"abc123","resume_gateway_url":url}})
        .to_string()
}

/// Serves one scripted connection that sends `frames`, then holds.
async fn serve_frames(frames: Vec<String>) -> SocketAddr {
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    tokio::spawn(async move {
        let (tcp, _) = l.accept().await.unwrap();
        let mut ws = tokio_tungstenite::accept_async(tcp).await.unwrap();
        for f in frames {
            ws.send(Message::text(f)).await.unwrap();
        }
        let _ = tokio::time::timeout(Duration::from_secs(2), ws.next()).await;
    });
    addr
}

/// Hostile resume URLs inside a READY frame are refused (the frame fails, the
/// transport errors, NOTHING from that frame is stored -- not even the
/// session id), and the next connect is a fresh identify on the CONFIGURED
/// gateway. The decoy host is never dialled.
#[tokio::test]
async fn hostile_resume_urls_are_refused_and_never_dialled() {
    let decoy = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let decoy_addr = decoy.local_addr().unwrap();
    let hostile = [
        format!("ws://evil.example:{}", decoy_addr.port()), // plain ws, not loopback
        "wss://evil.example".to_string(),                   // not allow-listed
        "wss://user@127.0.0.1".to_string(),                 // userinfo
        "wss://127.0.0.1@evil.example".to_string(),         // userinfo trick
        "wss://127.0.0.1\r\nHost: evil".to_string(),        // CRLF
        "wss://127.0.0.1:8443".to_string(),                 // explicit port
        "wss://127.0.0.1/steal".to_string(),                // path
        "http://127.0.0.1".to_string(),                     // scheme
        "wss://10.0.0.5".to_string(),                       // private IP, not allow-listed
    ];
    for url in hostile {
        let addr = serve_frames(vec![ready_with_url(&url)]).await;
        let frm_reg = prometheus::Registry::new();
        let frm = FrameRuleMetrics::register(&frm_reg).unwrap();
        // The allow-list holds exactly the loopback literal.
        let mut t = WsTransport::new(
            cfg(addr, rules(&["127.0.0.1"], 1, HandshakeAfter::Connect)),
            Tok,
        )
        .unwrap()
        .with_metrics(frm);
        t.connect().await.unwrap();
        let err = t.next_frame().await.unwrap_err();
        assert!(matches!(err, TransportError::Read(_)), "{url:?}: {err:?}");
        assert!(
            !err.to_string().contains("evil"),
            "error leaked the URL: {err}"
        );
        assert_eq!(
            count_total(
                &frm_reg,
                "svc_ingest_transport_frame_rules_total",
                ("rule", "reconnect_url")
            ),
            1,
            "{url:?}"
        );
        // Nothing is stored from the rejected READY (not even its session
        // id): the same frame through a bare engine leaves it empty.
        let mut e =
            SessionEngine::new_with(rules(&["127.0.0.1"], 1, HandshakeAfter::Connect), true)
                .unwrap();
        assert_eq!(
            e.observe(ready_with_url(&url).as_bytes()),
            Err(LifecycleError::BadReconnectUrl),
            "{url:?}"
        );
        assert_eq!(e.held(), 0, "{url:?}");
        assert_eq!(e.reconnect_target(), None, "{url:?}");
    }
    assert!(
        tokio::time::timeout(Duration::from_millis(200), decoy.accept())
            .await
            .is_err(),
        "the decoy host must never be dialled"
    );
}

/// An allow-listed but SSRF-forbidden host (private IP) is accepted by the
/// syntax/allow-list stage and refused by the egress guard at connect time;
/// the session is dropped so the following connect is a fresh identify on the
/// configured URL (loud + counted, never silent).
#[tokio::test]
async fn private_ip_resume_host_is_refused_by_the_egress_guard() {
    // Two connections on the configured gateway: READY, then a quiet one.
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    let second = Arc::new(Mutex::new(false));
    let sec = Arc::clone(&second);
    tokio::spawn(async move {
        let (tcp, _) = l.accept().await.unwrap();
        let mut ws = tokio_tungstenite::accept_async(tcp).await.unwrap();
        ws.send(Message::text(ready_with_url("wss://10.0.0.5")))
            .await
            .unwrap();
        let _ = tokio::time::timeout(Duration::from_millis(500), ws.next()).await;
        let (tcp, _) = l.accept().await.unwrap();
        let mut ws = tokio_tungstenite::accept_async(tcp).await.unwrap();
        *sec.lock().unwrap() = true;
        let _ = tokio::time::timeout(Duration::from_secs(1), ws.next()).await;
    });
    let frm_reg = prometheus::Registry::new();
    let frm = FrameRuleMetrics::register(&frm_reg).unwrap();
    // 10.0.0.5 is allow-listed on purpose; egress_allow is loopback only.
    let mut t = WsTransport::new(
        cfg(
            addr,
            rules(&["127.0.0.1", "10.0.0.5"], 1, HandshakeAfter::Connect),
        ),
        Tok,
    )
    .unwrap()
    .with_metrics(frm);
    t.connect().await.unwrap();
    // READY is accepted (shape + allow-list) and forwarded.
    assert!(t.next_frame().await.unwrap().is_some());
    let err = t.connect().await.unwrap_err();
    assert!(matches!(err, TransportError::Connect(_)), "{err:?}");
    assert_eq!(
        count_total(
            &frm_reg,
            "svc_ingest_transport_frame_rules_total",
            ("outcome", "refused_cleared")
        ),
        1
    );
    // The session was dropped: the next connect is a fresh identify on the
    // configured gateway (not another doomed attempt at the refused host).
    let ctx = t.connect().await.unwrap();
    assert_eq!(ctx.session_token, None);
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert!(*second.lock().unwrap(), "second connect hit the gateway");
}

/// The engine drops the URL itself on `clear()`, independent of the
/// session-token guard (a config without a token template has no other gate).
#[test]
fn clear_drops_the_reconnect_url_even_without_a_token_template() {
    let mut r = rules(&["127.0.0.1"], 1, HandshakeAfter::Connect);
    r.session_token_template = None;
    let mut e = SessionEngine::new_with(r, true).unwrap();
    assert_eq!(e.reconnect_target(), None);
    let obs = e
        .observe(ready_with_url("ws://127.0.0.1:9").as_bytes())
        .unwrap();
    assert!(obs.reconnect_url);
    assert_eq!(
        e.reconnect_target(),
        Some("ws://127.0.0.1:9/?v=10&encoding=json")
    );
    e.clear();
    assert_eq!(e.reconnect_target(), None);
}

/// Fresh identify clears the reconnect URL: op 9 d=false (ReconnectFresh)
/// drops it with the session, so the next connect dials the CONFIGURED URL.
#[tokio::test]
async fn fresh_identify_clears_the_resume_url() {
    let decoy = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let decoy_addr = decoy.local_addr().unwrap();
    let mut r = rules(&["127.0.0.1"], 1, HandshakeAfter::Connect);
    r.directives.push(FrameDirective {
        when: FrameMatch::Json(vec![op(9), ("/d".into(), Scalar::Bool(false))]),
        action: LifecycleAction::ReconnectFresh,
    });
    let gw = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let gw_addr = gw.local_addr().unwrap();
    let got = Arc::new(Mutex::new(Vec::<String>::new()));
    let g = Arc::clone(&got);
    tokio::spawn(async move {
        let (tcp, _) = gw.accept().await.unwrap();
        let mut ws = tokio_tungstenite::accept_async(tcp).await.unwrap();
        ws.send(Message::text(ready_with_url(&format!("ws://{decoy_addr}"))))
            .await
            .unwrap();
        ws.send(Message::text(r#"{"op":9,"d":false}"#))
            .await
            .unwrap();
        let _ = tokio::time::timeout(Duration::from_secs(1), ws.next()).await;
        // Second connection: must arrive on the configured gateway.
        let (tcp, _) = gw.accept().await.unwrap();
        let mut ws = tokio_tungstenite::accept_async(tcp).await.unwrap();
        g.lock()
            .unwrap()
            .push("second-connection-on-gateway".into());
        let _ = tokio::time::timeout(Duration::from_secs(1), ws.next()).await;
    });
    let mut t = WsTransport::new(cfg(gw_addr, r), Tok).unwrap();
    t.connect().await.unwrap();
    assert!(t.next_frame().await.unwrap().is_some()); // READY
    assert_eq!(t.next_frame().await.unwrap(), None); // op 9 false consumed
    let ctx = t.connect().await.unwrap();
    assert_eq!(ctx.session_token, None, "fresh");
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert_eq!(got.lock().unwrap().len(), 1);
    assert!(
        tokio::time::timeout(Duration::from_millis(100), decoy.accept())
            .await
            .is_err(),
        "stale resume url must not be dialled after a fresh identify"
    );
}

#[test]
fn gateway_rule_misconfig_is_refused_at_construction() {
    let t =
        |r: SessionRules| WsTransport::new(cfg("127.0.0.1:1".parse().unwrap(), r), Tok).map(|_| ());
    let ok = rules(&["127.0.0.1"], 1, FIRST_FRAME);
    assert!(t(ok.clone()).is_ok());

    let mut r = ok.clone();
    r.reconnect_url.as_mut().unwrap().allowed_hosts.clear();
    assert!(t(r).is_err(), "no allow-list");
    let mut r = ok.clone();
    r.reconnect_url.as_mut().unwrap().allowed_hosts = vec!["*.gg".into()];
    assert!(t(r).is_err(), "TLD wildcard");
    let mut r = ok.clone();
    r.heartbeat_ack.as_mut().unwrap().max_unacked = 0;
    assert!(t(r).is_err());
    let mut r = ok.clone();
    r.heartbeat_ack.as_mut().unwrap().max_unacked = 99;
    assert!(t(r).is_err(), "unbounded tolerance");
    let mut r = ok;
    r.handshake_after = HandshakeAfter::FirstFrame {
        timeout: Duration::from_secs(3600),
    };
    assert!(t(r).is_err(), "unbounded first-frame wait");
}

/// A close before the first frame: a plain close fails the handshake phase, a
/// fatal close code (4004) parks the source -- and no handshake is sent.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn close_before_the_first_frame_fails_or_parks_without_a_handshake() {
    for (code, expected) in [
        (1011u16, ConnectionEnd::HandshakeFailed),
        (4004, ConnectionEnd::Parked),
    ] {
        let gw = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = gw.local_addr().unwrap();
        let seen = Arc::new(Mutex::new(Vec::<String>::new()));
        let s = Arc::clone(&seen);
        tokio::spawn(async move {
            let (tcp, _) = gw.accept().await.unwrap();
            let mut ws = tokio_tungstenite::accept_async(tcp).await.unwrap();
            let _ = ws
                .close(Some(CloseFrame {
                    code: CloseCode::from(code),
                    reason: "x".into(),
                }))
                .await;
            let (texts, _) = drain_to_close(&mut ws).await;
            s.lock().unwrap().extend(texts);
        });
        let d = driver(&prometheus::Registry::new());
        let mut t =
            WsTransport::new(cfg(addr, rules(&["127.0.0.1"], 1, FIRST_FRAME)), Tok).unwrap();
        let (_stop, mut rx) = oneshot::channel::<()>();
        let report = d.serve_connection(&mut t, &mut rx).await.unwrap();
        assert_eq!(report.end, expected, "code {code}");
        assert!(!report.established);
        assert!(seen.lock().unwrap().is_empty(), "no handshake on {code}");
    }
}
