//! Real-path tests for the host session lifecycle (`lifecycle.rs` rules wired
//! into `WsTransport` / `PollTransport`): a local WebSocket server plays a
//! Discord-shaped gateway (HELLO -> IDENTIFY -> READY -> dispatch -> op 7 ->
//! RESUME -> op 9 ...) and a local HTTP server plays a YouTube-shaped poll
//! API. Wire bytes are asserted on the server side, never inferred.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use connection_resolver::{ConnectionRef, SecretString};
use futures_util::{SinkExt, StreamExt};
use tokio::net::TcpListener;
use tokio::sync::mpsc::{unbounded_channel, UnboundedReceiver, UnboundedSender};
use tokio_tungstenite::accept_async;
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;
use tokio_tungstenite::tungstenite::protocol::CloseFrame;
use tokio_tungstenite::tungstenite::Message;

use super::lifecycle::*;
use super::poll::{PollConfig, PollTransport};
use super::secrets::{Destination, ResolvedAuth, TransportSecretSource};
use super::transport::{Transport, TransportError};
use super::transport_common::FrameRuleMetrics;
use super::types::Handshake;
use super::webhook::{BoxFut, SecretError};
use super::ws::{WsConfig, WsTransport};

const REF: ConnectionRef = ConnectionRef {
    community_id: 7,
    connection_id: 9,
};

/// Returns a fixed secret and counts resolves.
#[derive(Clone)]
struct Fixed(String, Arc<AtomicUsize>);

impl Fixed {
    fn new(v: &str) -> Self {
        Self(v.to_string(), Arc::default())
    }
}

impl TransportSecretSource for Fixed {
    fn resolve(
        &self,
        _r: ConnectionRef,
        _d: Destination,
    ) -> BoxFut<'_, Result<ResolvedAuth, SecretError>> {
        self.1.fetch_add(1, Ordering::SeqCst);
        let v = self.0.clone();
        Box::pin(async move {
            Ok(ResolvedAuth {
                secret: SecretString::new(v),
                header: None,
            })
        })
    }
}

// ---------------------------------------------------------------- WS ----

enum Step {
    Send(&'static str),
    Recv,
    Close(u16),
}

/// One scripted connection per entry; `Recv` forwards the client's next text
/// frame to `got`; after its script the server holds the socket until the
/// client drops it.
async fn serve(l: TcpListener, conns: Vec<Vec<Step>>, got: UnboundedSender<String>) {
    for steps in conns {
        let (tcp, _) = l.accept().await.unwrap();
        let mut ws = accept_async(tcp).await.unwrap();
        for st in steps {
            match st {
                Step::Send(t) => ws.send(Message::text(t)).await.unwrap(),
                Step::Recv => {
                    let m = tokio::time::timeout(Duration::from_secs(3), ws.next()).await;
                    if let Ok(Some(Ok(Message::Text(t)))) = m {
                        got.send(t.to_string()).unwrap();
                    }
                }
                Step::Close(code) => {
                    let _ = ws
                        .close(Some(CloseFrame {
                            code: CloseCode::from(code),
                            reason: "bye".into(),
                        }))
                        .await;
                }
            }
        }
        let _ = tokio::time::timeout(Duration::from_millis(500), ws.next()).await;
    }
}

const HELLO: &str = r#"{"op":10,"d":{"heartbeat_interval":1500}}"#;
const READY: &str = r#"{"op":0,"t":"READY","s":1,"d":{"session_id":"abc123"}}"#;
const DISPATCH5: &str = r#"{"op":0,"t":"MESSAGE_CREATE","s":5,"d":{"content":"hi"}}"#;
const OP7: &str = r#"{"op":7,"d":null}"#;
const OP9_FALSE: &str = r#"{"op":9,"d":false}"#;
const OP9_TRUE: &str = r#"{"op":9,"d":true}"#;

fn op(n: i64) -> (String, Scalar) {
    ("/op".into(), Scalar::Int(n))
}

/// The host config an Discord connector would ship (also the docs example).
fn discord_rules() -> SessionRules {
    let close = |from, to, action| CloseCodeRule { from, to, action };
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
        directives: vec![
            FrameDirective {
                when: FrameMatch::Json(vec![op(7)]),
                action: LifecycleAction::Reconnect,
            },
            FrameDirective {
                when: FrameMatch::Json(vec![op(9), ("/d".into(), Scalar::Bool(true))]),
                action: LifecycleAction::Reconnect,
            },
            FrameDirective {
                when: FrameMatch::Json(vec![op(9), ("/d".into(), Scalar::Bool(false))]),
                action: LifecycleAction::ReconnectFresh,
            },
        ],
        close_codes: vec![
            close(4004, 4004, LifecycleAction::Park),
            close(4010, 4014, LifecycleAction::Park),
            close(4007, 4007, LifecycleAction::ReconnectFresh),
            close(4009, 4009, LifecycleAction::ReconnectFresh),
            close(4000, 4003, LifecycleAction::Reconnect),
            close(4005, 4005, LifecycleAction::Reconnect),
            close(4008, 4008, LifecycleAction::Reconnect),
        ],
        session_token_template: Some(r#"{"session_id":"{{SESSION_ID}}","seq":{{SEQ}}}"#.into()),
        default_heartbeat: Some(r#"{"op":1,"d":{{SEQ|null}}}"#.into()),
        host_owned_heartbeat: true,
        ..SessionRules::default()
    }
}

fn ws_cfg(addr: SocketAddr, session: SessionRules) -> WsConfig {
    WsConfig {
        platform: "testplat".into(),
        url: format!("ws://{addr}/gw"),
        source_key: "k".into(),
        handles: HashMap::from([("h1".to_string(), REF)]),
        resume_token: None,
        connect_timeout: Duration::from_secs(2),
        idle_timeout: Duration::from_secs(5),
        max_lifetime: Duration::from_secs(60),
        egress_allow: vec!["127.0.0.1".parse().unwrap()],
        max_frame_bytes: 1024,
        dedup_window: 0,
        allow_loopback_insecure: true,
        slot_prefixes: HashMap::from([(
            "h1".to_string(),
            vec![
                r#"{"op":2,"d":{"token":""#.to_string(),
                r#"{"op":6,"d":{"token":""#.to_string(),
            ],
        )]),
        auto_replies: vec![],
        self_echo: None,
        heartbeat_interval: None,
        session,
    }
}

fn identify() -> Handshake {
    Handshake {
        bytes: br#"{"op":2,"d":{"token":"{{TOKEN}}"}}"#.to_vec(),
        secret_refs: vec![("{{TOKEN}}".into(), "h1".into())],
    }
}

fn resume() -> Handshake {
    Handshake {
        bytes: br#"{"op":6,"d":{"token":"{{TOKEN}}","session_id":"{{SESSION_ID}}","seq":{{SEQ}}}}"#
            .to_vec(),
        secret_refs: vec![("{{TOKEN}}".into(), "h1".into())],
    }
}

async fn start(conns: Vec<Vec<Step>>) -> (SocketAddr, UnboundedReceiver<String>) {
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    let (tx, rx) = unbounded_channel();
    tokio::spawn(serve(l, conns, tx));
    (addr, rx)
}

fn metrics() -> (FrameRuleMetrics, prometheus::Registry) {
    let r = prometheus::Registry::new();
    (FrameRuleMetrics::register(&r).unwrap(), r)
}

fn count(reg: &prometheus::Registry, rule: &str, outcome: &str) -> u64 {
    reg.gather()
        .iter()
        .flat_map(|f| f.get_metric())
        .filter(|m| {
            let l: HashMap<_, _> = m
                .get_label()
                .iter()
                .map(|p| (p.name().to_string(), p.value().to_string()))
                .collect();
            l.get("rule").map(String::as_str) == Some(rule)
                && l.get("outcome").map(String::as_str) == Some(outcome)
        })
        .map(|m| m.get_counter().value() as u64)
        .sum()
}

/// HELLO -> IDENTIFY -> READY -> dispatch -> op 7 -> RESUME(session_id, seq on
/// the wire) -> op 9 d=false -> fresh IDENTIFY with no session state.
#[tokio::test]
async fn discord_session_flows_through_resume_and_fresh_identify() {
    let (addr, mut rx) = start(vec![
        vec![
            Step::Send(HELLO),
            Step::Recv,
            Step::Send(READY),
            Step::Send(DISPATCH5),
            Step::Send(OP7),
        ],
        vec![Step::Recv, Step::Send(HELLO), Step::Send(OP9_FALSE)],
        vec![],
        vec![Step::Recv],
    ])
    .await;
    let (m, reg) = metrics();
    let secrets = Fixed::new("tok123");
    let resolved = secrets.1.clone();
    let mut t = WsTransport::new(ws_cfg(addr, discord_rules()), secrets)
        .unwrap()
        .with_metrics(m);

    // Connection 1: fresh, no session token.
    assert_eq!(t.connect().await.unwrap().session_token, None);
    t.send_handshake(identify()).await.unwrap();
    assert_eq!(
        rx.recv().await.unwrap(),
        r#"{"op":2,"d":{"token":"tok123"}}"#
    );
    assert_eq!(t.heartbeat_interval(), None, "no interval before HELLO");
    assert_eq!(t.next_frame().await.unwrap().unwrap(), HELLO.as_bytes());
    assert_eq!(t.heartbeat_interval(), Some(Duration::from_millis(1500)));
    assert_eq!(t.next_frame().await.unwrap().unwrap(), READY.as_bytes());
    assert_eq!(t.next_frame().await.unwrap().unwrap(), DISPATCH5.as_bytes());
    // op 7 is consumed and ends the connection (driver reconnects).
    assert_eq!(t.next_frame().await.unwrap(), None);

    // Connection 2: RESUME carries the captured session id + seq.
    let ctx = t.connect().await.unwrap();
    assert_eq!(
        ctx.session_token.as_deref(),
        Some(r#"{"session_id":"abc123","seq":5}"#),
        "host feeds the captured session back via the opaque session token"
    );
    assert_eq!(t.heartbeat_interval(), None, "interval is per connection");
    t.send_handshake(resume()).await.unwrap();
    assert_eq!(
        rx.recv().await.unwrap(),
        r#"{"op":6,"d":{"token":"tok123","session_id":"abc123","seq":5}}"#
    );
    assert_eq!(t.next_frame().await.unwrap().unwrap(), HELLO.as_bytes());
    // op 9 d=false: drop session state, reconnect.
    assert_eq!(t.next_frame().await.unwrap(), None);

    // Connection 3: state gone -> no token, and a RESUME template fails loud
    // BEFORE any byte (or secret) leaves.
    let resolves = resolved.load(Ordering::SeqCst);
    assert_eq!(t.connect().await.unwrap().session_token, None);
    assert!(matches!(
        t.send_handshake(resume()).await.unwrap_err(),
        TransportError::Handshake(_)
    ));
    assert_eq!(
        resolved.load(Ordering::SeqCst),
        resolves,
        "secret never resolved"
    );
    assert!(
        rx.try_recv().is_err(),
        "nothing sent on the failed handshake"
    );

    // Connection 4: fresh IDENTIFY, no session id anywhere on the wire.
    t.connect().await.unwrap();
    t.send_handshake(identify()).await.unwrap();
    let wire = rx.recv().await.unwrap();
    assert_eq!(wire, r#"{"op":2,"d":{"token":"tok123"}}"#);
    assert!(!wire.contains("abc123"));

    assert_eq!(count(&reg, "directive", "reconnect"), 1);
    assert_eq!(count(&reg, "directive", "reconnect_fresh"), 1);
    assert_eq!(count(&reg, "heartbeat_interval", "applied"), 2);
}

/// op 9 d=true keeps the session (RESUME next); op 7 likewise.
#[tokio::test]
async fn op9_resumable_keeps_session_state() {
    let (addr, _rx) = start(vec![vec![
        Step::Send(READY),
        Step::Send(DISPATCH5),
        Step::Send(OP9_TRUE),
    ]])
    .await;
    let mut t = WsTransport::new(ws_cfg(addr, discord_rules()), Fixed::new("tok123")).unwrap();
    t.connect().await.unwrap();
    t.next_frame().await.unwrap().unwrap();
    t.next_frame().await.unwrap().unwrap();
    assert_eq!(t.next_frame().await.unwrap(), None);
    let st = t.session_token_for_test();
    assert_eq!(st.as_deref(), Some(r#"{"session_id":"abc123","seq":5}"#));
}

/// 4004 / 4010-4014 park (Fatal); 4000 reconnects keeping state; 4007 drops
/// state; an unclassified code is the old plain close.
#[tokio::test]
async fn close_codes_park_reconnect_or_go_fresh() {
    let (addr, _rx) = start(vec![
        vec![Step::Send(READY), Step::Close(4004)],
        vec![Step::Send(READY), Step::Close(4000)],
        vec![Step::Close(4007)],
        vec![Step::Close(1000)],
        vec![Step::Close(4012)],
    ])
    .await;
    let (m, reg) = metrics();
    let mut t = WsTransport::new(ws_cfg(addr, discord_rules()), Fixed::new("tok123"))
        .unwrap()
        .with_metrics(m);

    t.connect().await.unwrap();
    t.next_frame().await.unwrap().unwrap();
    let e = t.next_frame().await.unwrap_err();
    assert!(e.is_fatal(), "4004 parks: {e}");

    t.connect().await.unwrap();
    t.next_frame().await.unwrap().unwrap(); // READY (seq 1 captured too)
    assert_eq!(t.next_frame().await.unwrap(), None, "4000 reconnects");
    assert!(t.session_token_for_test().is_some(), "4000 keeps state");

    t.connect().await.unwrap();
    assert_eq!(t.next_frame().await.unwrap(), None, "4007 reconnects");
    assert_eq!(t.session_token_for_test(), None, "4007 drops state");

    t.connect().await.unwrap();
    assert_eq!(t.next_frame().await.unwrap(), None, "1000 plain close");

    t.connect().await.unwrap();
    assert!(t.next_frame().await.unwrap_err().is_fatal(), "4012 parks");

    assert_eq!(count(&reg, "close_code", "park"), 2);
    assert_eq!(count(&reg, "close_code", "reconnect"), 1);
    assert_eq!(count(&reg, "close_code", "reconnect_fresh"), 1);
    assert_eq!(count(&reg, "close_code", "unclassified"), 1);
}

/// regression: a hostile (CR/LF, quote, oversize, wrong-type) captured value
/// is REJECTED -- the frame fails, nothing is stored, nothing is reflected.
#[tokio::test]
async fn hostile_captures_are_rejected_not_sanitized() {
    for bad in [
        r#"{"op":0,"t":"READY","s":1,"d":{"session_id":"ab\r\nINJECT"}}"#,
        r#"{"op":0,"t":"READY","s":1,"d":{"session_id":"a\"b"}}"#,
        r#"{"op":0,"t":"READY","s":1,"d":{"session_id":"{{TOKEN}}"}}"#,
        r#"{"op":0,"t":"READY","s":1,"d":{"session_id":""}}"#,
        r#"{"op":0,"t":"READY","s":1,"d":{"session_id":{"x":1}}}"#,
        r#"{"op":0,"t":"MESSAGE_CREATE","s":-4,"d":{}}"#,
        r#"{"op":0,"t":"MESSAGE_CREATE","s":1.5,"d":{}}"#,
    ] {
        let (addr, _rx) = start(vec![vec![Step::Send(bad)]]).await;
        let (m, reg) = metrics();
        let mut t = WsTransport::new(ws_cfg(addr, discord_rules()), Fixed::new("tok123"))
            .unwrap()
            .with_metrics(m);
        t.connect().await.unwrap();
        let e = t.next_frame().await.unwrap_err();
        assert!(matches!(e, TransportError::Read(_)), "{bad}: {e:?}");
        assert!(!e.to_string().contains("INJECT") && !e.to_string().contains("TOKEN"));
        assert_eq!(t.session_token_for_test(), None, "nothing stored");
        assert_eq!(count(&reg, "session_capture", "rejected"), 1);
    }
    // And an over-long value.
    let long = format!(
        r#"{{"op":0,"t":"READY","s":1,"d":{{"session_id":"{}"}}}}"#,
        "a".repeat(MAX_CAPTURE_LEN + 1)
    );
    let long: &'static str = Box::leak(long.into_boxed_str());
    let (addr, _rx) = start(vec![vec![Step::Send(long)]]).await;
    let mut cfg = ws_cfg(addr, discord_rules());
    cfg.max_frame_bytes = 4096;
    let mut t = WsTransport::new(cfg, Fixed::new("tok123")).unwrap();
    t.connect().await.unwrap();
    assert!(t.next_frame().await.is_err());
}

/// A dynamic interval outside the driver bounds is rejected.
#[tokio::test]
async fn out_of_range_heartbeat_interval_is_rejected() {
    for bad in [
        r#"{"op":10,"d":{"heartbeat_interval":50}}"#,
        r#"{"op":10,"d":{"heartbeat_interval":3600001}}"#,
        r#"{"op":10,"d":{"heartbeat_interval":"fast"}}"#,
        r#"{"op":10,"d":{"heartbeat_interval":-1}}"#,
    ] {
        let (addr, _rx) = start(vec![vec![Step::Send(bad)]]).await;
        let (m, reg) = metrics();
        let mut t = WsTransport::new(ws_cfg(addr, discord_rules()), Fixed::new("tok123"))
            .unwrap()
            .with_metrics(m);
        t.connect().await.unwrap();
        assert!(t.next_frame().await.is_err(), "{bad}");
        assert_eq!(t.heartbeat_interval(), None);
        assert_eq!(count(&reg, "heartbeat_interval", "rejected"), 1);
    }
}

/// The host-built default heartbeat carries the LIVE sequence (`null` before
/// any), and a guest-supplied body is replaced when host-owned.
#[tokio::test]
async fn default_heartbeat_carries_live_seq_and_replaces_guest_body() {
    let (addr, mut rx) = start(vec![vec![
        Step::Send(HELLO),
        Step::Recv,
        Step::Send(DISPATCH5),
        Step::Recv,
    ]])
    .await;
    let mut t = WsTransport::new(ws_cfg(addr, discord_rules()), Fixed::new("tok123")).unwrap();
    t.connect().await.unwrap();
    t.next_frame().await.unwrap().unwrap(); // HELLO -> interval
    t.send_default_heartbeat().await.unwrap();
    assert_eq!(rx.recv().await.unwrap(), r#"{"op":1,"d":null}"#);
    t.next_frame().await.unwrap().unwrap(); // seq 5
    t.send_heartbeat(br#"{"op":1,"d":1}"#.to_vec())
        .await
        .unwrap();
    assert_eq!(rx.recv().await.unwrap(), r#"{"op":1,"d":5}"#);
}

/// IRC-shaped: a `RECONNECT` command line reconnects; a chat line that merely
/// contains the word does not (attacker-controlled text).
#[tokio::test]
async fn irc_reconnect_command_is_matched_by_command_token_only() {
    let rules = SessionRules {
        directives: vec![FrameDirective {
            when: FrameMatch::IrcCommand("RECONNECT".into()),
            action: LifecycleAction::Reconnect,
        }],
        ..Default::default()
    };
    let (addr, _rx) = start(vec![vec![
        Step::Send(":nick!u@h PRIVMSG #c :RECONNECT"),
        Step::Send("@t=1 :tmi.twitch.tv RECONNECT\r\n"),
    ]])
    .await;
    let mut t = WsTransport::new(ws_cfg(addr, rules), Fixed::new("tok123")).unwrap();
    t.connect().await.unwrap();
    assert!(
        t.next_frame().await.unwrap().is_some(),
        "chat text forwarded"
    );
    assert_eq!(t.next_frame().await.unwrap(), None, "real command acts");
}

#[test]
fn ws_misconfig_is_refused_at_construction() {
    let addr: SocketAddr = "127.0.0.1:1".parse().unwrap();
    let mk = |f: &dyn Fn(&mut SessionRules)| {
        let mut r = discord_rules();
        f(&mut r);
        WsTransport::new(ws_cfg(addr, r), Fixed::new("x")).err()
    };
    assert!(mk(&|_| {}).is_none(), "the reference config is valid");
    assert!(mk(&|r| r.captures[0].name = "lower".into()).is_some());
    assert!(mk(&|r| r.captures[0].pointer = "no-slash".into()).is_some());
    assert!(mk(&|r| r.captures = vec![r.captures[0].clone(); MAX_CAPTURES + 1]).is_some());
    assert!(mk(&|r| {
        r.captures = (0..=MAX_STATE_VALUES)
            .map(|i| CaptureRule {
                when: FrameMatch::Json(vec![op(0)]),
                pointer: "/s".into(),
                name: format!("N{i}"),
            })
            .collect();
        r.session_token_template = None;
        r.default_heartbeat = None;
        r.host_owned_heartbeat = false;
    })
    .is_some());
    assert!(mk(&|r| r.close_codes[0].from = 999).is_some());
    assert!(mk(&|r| r.close_codes[0].to = 4010).is_some(), "overlap");
    assert!(
        mk(&|r| r.close_codes[0].to = 3000).is_some(),
        "inverted range"
    );
    assert!(mk(&|r| r.close_codes[0].action = LifecycleAction::Park).is_none());
    assert!(mk(&|r| r.session_token_template = Some("{{NOPE}}".into())).is_some());
    assert!(mk(&|r| r.default_heartbeat = Some("x\r\n{{SEQ}}".into())).is_some());
    assert!(mk(&|r| r.default_heartbeat = Some("{{SEQ|bad value}}".into())).is_some());
    assert!(
        mk(&|r| r.default_heartbeat = None).is_some(),
        "host-owned needs a template"
    );
    assert!(mk(&|r| r.directives[0].when = FrameMatch::Json(vec![])).is_some());
    assert!(mk(&|r| r.directives[0].when = FrameMatch::IrcCommand("a b".into())).is_some());
    assert!(mk(&|r| r.directives = vec![r.directives[0].clone(); MAX_DIRECTIVES + 1]).is_some());
    assert!(mk(&|r| {
        r.heartbeat.as_mut().unwrap().pointer = "x".into();
    })
    .is_some());
    // Template + static resume token is ambiguous.
    let mut c = ws_cfg(addr, discord_rules());
    c.resume_token = Some("static".into());
    assert!(WsTransport::new(c, Fixed::new("x")).is_err());
}

#[test]
fn engine_state_is_redacted_in_debug_and_bounded() {
    let mut e = SessionEngine::new(discord_rules()).unwrap();
    e.observe(READY.as_bytes()).unwrap();
    assert_eq!(e.held(), 2);
    let dbg = format!("{e:?}");
    assert!(!dbg.contains("abc123"), "Debug must not print state: {dbg}");
    e.clear();
    assert_eq!(e.held(), 0);
}

// ------------------------------------------------------------- poll ----

type Reply = (u16, Vec<(&'static str, &'static str)>, String);

#[derive(Clone, Default)]
struct Api {
    replies: Arc<Mutex<Vec<Reply>>>,
    seen: Arc<Mutex<Vec<(std::time::Instant, String)>>>,
}

async fn api_handler(
    axum::extract::State(s): axum::extract::State<Api>,
    uri: axum::http::Uri,
) -> axum::response::Response {
    use axum::response::IntoResponse;
    s.seen.lock().unwrap().push((
        std::time::Instant::now(),
        uri.query().unwrap_or("").to_string(),
    ));
    let next = {
        let mut r = s.replies.lock().unwrap();
        if r.len() > 1 {
            r.remove(0)
        } else {
            r[0].clone()
        }
    };
    let mut resp = (axum::http::StatusCode::from_u16(next.0).unwrap(), next.2).into_response();
    for (k, v) in next.1 {
        resp.headers_mut().insert(k, v.parse().unwrap());
    }
    resp
}

async fn api(replies: Vec<Reply>) -> (SocketAddr, Api) {
    let s = Api {
        replies: Arc::new(Mutex::new(replies)),
        ..Default::default()
    };
    let app = axum::Router::new()
        .route("/poll", axum::routing::get(api_handler))
        .with_state(s.clone());
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(l, app).await.unwrap() });
    (addr, s)
}

fn ok(body: &str) -> Reply {
    (200, vec![], body.to_string())
}

fn poll_cfg(addr: SocketAddr, lifecycle: PollLifecycle) -> PollConfig {
    PollConfig {
        platform: "testplat".into(),
        url: format!("http://{addr}/poll"),
        source_key: "k".into(),
        handles: HashMap::from([("h1".to_string(), REF)]),
        auth: None,
        cursor_param: Some("pageToken".into()),
        cursor_pointer: Some("/nextPageToken".into()),
        initial_cursor: None,
        interval: Duration::from_millis(5),
        request_timeout: Duration::from_secs(2),
        max_body_bytes: 4096,
        max_drop_attempts: 3,
        egress_allow: vec!["127.0.0.1".parse().unwrap()],
        allow_loopback_insecure: true,
        lifecycle,
        request: Default::default(),
        rediscover: None,
    }
}

fn yt_error(reason: &str) -> String {
    format!(r#"{{"error":{{"code":403,"errors":[{{"reason":"{reason}"}}]}}}}"#)
}

fn yt_lifecycle(base_ms: u64, max_ms: u64) -> PollLifecycle {
    let rule = |reasons: &[&str], action| HttpErrorRule {
        status: 403,
        reason_pointer: "/error/errors/0/reason".into(),
        reasons: reasons.iter().map(|s| (*s).to_string()).collect(),
        action,
    };
    PollLifecycle {
        interval_hint: Some(IntervalHint {
            pointer: "/pollingIntervalMillis".into(),
            min: Duration::from_millis(50),
            max: Duration::from_millis(400),
        }),
        error_rules: vec![
            rule(
                &["quotaExceeded", "rateLimitExceeded"],
                HttpAction::Backoff {
                    base: Duration::from_millis(base_ms),
                    max: Duration::from_millis(max_ms),
                    honor_retry_after: true,
                },
            ),
            rule(&["forbidden", "insufficientPermissions"], HttpAction::Park),
        ],
        query_secret: None,
    }
}

#[tokio::test]
async fn poll_interval_is_taken_from_the_response_within_bounds() {
    let (addr, srv) = api(vec![
        ok(r#"{"items":[],"pollingIntervalMillis":300,"nextPageToken":"a"}"#),
        ok(r#"{"items":[],"pollingIntervalMillis":999999,"nextPageToken":"b"}"#),
        ok(r#"{"items":[],"pollingIntervalMillis":1,"nextPageToken":"c"}"#),
        ok(r#"{"items":[],"nextPageToken":"d"}"#),
    ])
    .await;
    let (m, reg) = metrics();
    let mut t = PollTransport::new(poll_cfg(addr, yt_lifecycle(10, 100)), Fixed::new("x"))
        .unwrap()
        .with_metrics(m);
    t.connect().await.unwrap();
    for (i, want) in [300u64, 400, 50, 5].into_iter().enumerate() {
        t.next_frame().await.unwrap().unwrap();
        t.on_frame_outcome(crate::connector::FrameOutcome::Published)
            .await;
        assert_eq!(
            t.interval(),
            Duration::from_millis(want),
            "step {i}: applied / clamped-max / clamped-min / absent -> configured"
        );
    }
    assert_eq!(count(&reg, "poll_interval", "applied"), 1);
    assert_eq!(count(&reg, "poll_interval", "clamped"), 2);
    assert_eq!(count(&reg, "poll_interval", "ignored"), 1);
    // The 300 ms hint really spaced the next request on the wire.
    let seen = srv.seen.lock().unwrap().clone();
    assert!(seen[1].0 - seen[0].0 >= Duration::from_millis(280));
}

#[tokio::test]
async fn quota_403_backs_off_and_resumes_forbidden_parks() {
    let (addr, srv) = api(vec![
        (403, vec![], yt_error("quotaExceeded")),
        ok(r#"{"items":[1],"nextPageToken":"a"}"#),
        (403, vec![], yt_error("forbidden")),
    ])
    .await;
    let (m, reg) = metrics();
    let mut t = PollTransport::new(poll_cfg(addr, yt_lifecycle(200, 1000)), Fixed::new("x"))
        .unwrap()
        .with_metrics(m);
    t.connect().await.unwrap();
    // quotaExceeded: NOT fatal -- next_frame waits and then yields the 200.
    let body = t.next_frame().await.unwrap().unwrap();
    assert!(body.starts_with(br#"{"items":[1]"#));
    {
        let seen = srv.seen.lock().unwrap();
        assert_eq!(seen.len(), 2);
        assert!(
            seen[1].0 - seen[0].0 >= Duration::from_millis(190),
            "waited out the back-off: {:?}",
            seen[1].0 - seen[0].0
        );
    }
    t.on_frame_outcome(crate::connector::FrameOutcome::Published)
        .await;
    // forbidden: Fatal park.
    let e = t.next_frame().await.unwrap_err();
    assert!(e.is_fatal(), "{e}");
    assert_eq!(count(&reg, "poll_http", "backoff"), 1);
    assert_eq!(count(&reg, "poll_http", "park"), 1);
}

#[tokio::test]
async fn unclassified_403_stays_fatal_and_retry_after_is_honored_but_capped() {
    // Unknown reason / unparsable body: the old fail-loud behaviour.
    for body in [
        yt_error("liveChatEnded"),
        "not json".to_string(),
        String::new(),
    ] {
        let (addr, _s) = api(vec![(403, vec![], body)]).await;
        let (m, reg) = metrics();
        let mut t = PollTransport::new(poll_cfg(addr, yt_lifecycle(10, 100)), Fixed::new("x"))
            .unwrap()
            .with_metrics(m);
        assert!(t.next_frame().await.unwrap_err().is_fatal());
        assert_eq!(count(&reg, "poll_http", "unclassified"), 1);
    }
    // Retry-After: 30 is capped at the rule max (300 ms here).
    let (addr, srv) = api(vec![
        (
            403,
            vec![("retry-after", "30")],
            yt_error("rateLimitExceeded"),
        ),
        ok(r#"{"items":[1]}"#),
    ])
    .await;
    let mut t = PollTransport::new(poll_cfg(addr, yt_lifecycle(10, 300)), Fixed::new("x")).unwrap();
    let started = std::time::Instant::now();
    t.next_frame().await.unwrap().unwrap();
    assert!(started.elapsed() < Duration::from_secs(3), "capped");
    let seen = srv.seen.lock().unwrap().clone();
    assert!(seen[1].0 - seen[0].0 >= Duration::from_millis(290));
}

/// The API key lands percent-encoded in exactly one declared query param and
/// is absent from every error string and every tracing event.
#[tokio::test]
async fn api_key_query_slot_is_encoded_and_never_leaks() {
    const KEY: &str = "AIza Sy&b=c+/?#é%\"";
    let (addr, srv) = api(vec![
        ok(r#"{"items":[],"nextPageToken":"p1"}"#),
        (500, vec![], "boom".into()),
        (403, vec![], yt_error("quotaExceeded")),
        (403, vec![], yt_error("forbidden")),
    ])
    .await;
    let mut lc = yt_lifecycle(10, 100);
    lc.query_secret = Some(QuerySecret {
        param: "key".into(),
        handle: "h1".into(),
    });
    let logs = Logs::default();
    let _g = tracing::subscriber::set_default(logs.clone());
    let mut t = PollTransport::new(poll_cfg(addr, lc.clone()), Fixed::new(KEY)).unwrap();
    t.connect().await.unwrap();
    t.next_frame().await.unwrap().unwrap();
    t.on_frame_outcome(crate::connector::FrameOutcome::Published)
        .await;
    let mut errs = vec![t.next_frame().await.unwrap_err().to_string()];
    errs.push(t.next_frame().await.unwrap_err().to_string());

    // Wire: key strictly percent-encoded, decodes back, no injected params.
    let q = srv.seen.lock().unwrap()[0].1.clone();
    assert!(
        q.contains("key=AIza+Sy%26b%3Dc%2B%2F%3F%23%C3%A9%25%22"),
        "{q}"
    );
    let pairs: Vec<(String, String)> = reqwest::Url::parse(&format!("http://x/?{q}"))
        .unwrap()
        .query_pairs()
        .map(|(k, v)| (k.into_owned(), v.into_owned()))
        .collect();
    assert_eq!(pairs, vec![("key".into(), KEY.into())]);
    let q2 = srv.seen.lock().unwrap()[1].1.clone();
    assert!(q2.starts_with("pageToken=p1&key="), "cursor + key: {q2}");

    // Connect refused + timeout error paths (the URL carries the key).
    let dead: SocketAddr = {
        let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        l.local_addr().unwrap()
    };
    let mut t2 = PollTransport::new(poll_cfg(dead, lc.clone()), Fixed::new(KEY)).unwrap();
    errs.push(t2.next_frame().await.unwrap_err().to_string());
    for e in &errs {
        for needle in ["AIza", "Sy&b", "Sy%26b", "Sy+b", "key=", "%C3%A9"] {
            assert!(!e.contains(needle), "secret leaked in error {e:?}");
        }
    }
    let all = logs.joined();
    // (Callsite interest is per-process and racy across parallel tests, so
    // the capture may be partial; what it holds must still be leak-free.)
    for needle in ["AIza", "Sy&b", "Sy%26b", "%C3%A9", "key="] {
        assert!(!all.contains(needle), "secret leaked in logs: {all}");
    }
}

#[tokio::test]
async fn query_secret_misconfig_is_refused() {
    let addr: SocketAddr = "127.0.0.1:1".parse().unwrap();
    let mk = |param: &str, handle: &str, url_extra: &str| {
        let lc = PollLifecycle {
            query_secret: Some(QuerySecret {
                param: param.into(),
                handle: handle.into(),
            }),
            ..Default::default()
        };
        let mut c = poll_cfg(addr, lc);
        c.url = format!("{}{url_extra}", c.url);
        PollTransport::new(c, Fixed::new("x")).err()
    };
    assert!(mk("key", "h1", "").is_none());
    assert!(
        mk("key", "nope", "").is_some(),
        "handle must be in the manifest"
    );
    assert!(mk("pageToken", "h1", "").is_some(), "no cursor clash");
    assert!(mk("key", "h1", "?key=pinned").is_some(), "no URL clash");
    assert!(mk("k e", "h1", "").is_some(), "charset");
    assert!(mk("", "h1", "").is_some());
    assert!(mk(&"a".repeat(33), "h1", "").is_some());
}

#[test]
fn http_and_interval_config_misconfig_is_refused() {
    let url = reqwest::Url::parse("https://x/").unwrap();
    let h: HashMap<String, ConnectionRef> = HashMap::new();
    let bad = |f: &dyn Fn(&mut PollLifecycle)| {
        let mut l = yt_lifecycle(10, 100);
        f(&mut l);
        l.validate(&h, None, &url).is_err()
    };
    assert!(!bad(&|_| {}));
    assert!(bad(
        &|l| l.interval_hint.as_mut().unwrap().max = Duration::from_millis(1)
    ));
    assert!(bad(
        &|l| l.interval_hint.as_mut().unwrap().min = Duration::ZERO
    ));
    assert!(bad(
        &|l| l.interval_hint.as_mut().unwrap().max = MAX_BACKOFF + Duration::from_secs(1)
    ));
    assert!(bad(&|l| l.error_rules[0].status = 200));
    assert!(bad(&|l| l.error_rules[0].reasons = vec![]));
    assert!(bad(&|l| l.error_rules[0].reasons = vec!["a b".into()]));
    assert!(bad(&|l| l.error_rules[0].reason_pointer = "x".into()));
    assert!(bad(
        &|l| l.error_rules = vec![l.error_rules[0].clone(); MAX_HTTP_RULES + 1]
    ));
    assert!(
        bad(&|l| l.error_rules[1].reasons = vec!["quotaExceeded".into()]),
        "overlap"
    );
    assert!(bad(&|l| {
        l.error_rules[0].action = HttpAction::Backoff {
            base: Duration::from_secs(10),
            max: Duration::from_secs(1),
            honor_retry_after: false,
        }
    }));
    assert!(bad(&|l| {
        l.error_rules[0].action = HttpAction::Backoff {
            base: Duration::from_secs(1),
            max: MAX_BACKOFF + Duration::from_secs(1),
            honor_retry_after: false,
        }
    }));
}

#[test]
fn backoff_doubles_is_capped_and_retry_after_wins_up_to_the_cap() {
    let (b, m) = (Duration::from_secs(1), Duration::from_secs(10));
    let w = |s, ra| backoff_wait(b, m, s, ra);
    assert_eq!(w(0, None), Duration::from_secs(1));
    assert_eq!(w(1, None), Duration::from_secs(2));
    assert_eq!(w(2, None), Duration::from_secs(4));
    assert_eq!(w(9, None), m, "capped");
    assert_eq!(w(u32::MAX, None), m, "no shift overflow");
    assert_eq!(w(0, Some(Duration::from_secs(7))), Duration::from_secs(7));
    assert_eq!(w(0, Some(Duration::from_secs(700))), m, "capped");
}

#[test]
fn template_rendering_rules() {
    let mut e = SessionEngine::new(discord_rules()).unwrap();
    // No state: token absent, handshake with a required slot refused, default used.
    assert_eq!(e.render_session_token(), None);
    assert_eq!(e.render_handshake(b"{{SESSION_ID}}"), None);
    assert_eq!(e.render_handshake(b"{{SEQ|null}}").unwrap(), b"null");
    // Undeclared (secret) slots and malformed braces pass through untouched.
    assert_eq!(
        e.render_handshake(b"{{DISCORD_BOT_TOKEN}} {{ {{x}} {{SEQ")
            .unwrap(),
        b"{{DISCORD_BOT_TOKEN}} {{ {{x}} {{SEQ"
    );
    e.observe(READY.as_bytes()).unwrap();
    assert_eq!(
        e.render_handshake(b"a{{{SEQ}}b{{SESSION_ID}}").unwrap(),
        b"a{1b"
            .iter()
            .chain(b"abc123")
            .copied()
            .collect::<Vec<u8>>()
    );
    // Single pass: a value is never re-scanned for slots.
    assert_eq!(e.render_default_heartbeat().unwrap(), br#"{"op":1,"d":1}"#);
}

#[test]
fn json_match_and_capture_edge_cases() {
    let mut e = SessionEngine::new(discord_rules()).unwrap();
    // Non-JSON / binary / non-object frames are untouched, never an error.
    for f in [&b"PING :x"[..], &[0xff, 0xfe][..], b"[1,2]", b"{bad json"] {
        assert_eq!(e.observe(f).unwrap(), Observation::default());
    }
    // Absent / null captures are skipped (heartbeat ACK `s:null`).
    let o = e.observe(br#"{"op":0,"s":null,"d":{}}"#).unwrap();
    assert_eq!(o.captured, 0);
    // `op` as a string does not match an Int condition.
    let o = e.observe(br#"{"op":"7"}"#).unwrap();
    assert_eq!(o.action, None);
    // A failed frame stores nothing from its earlier valid captures.
    let bad = br#"{"op":0,"t":"READY","s":9,"d":{"session_id":"a b"}}"#;
    assert!(e.observe(bad).is_err());
    assert_eq!(e.held(), 0);
    assert_eq!(e.classify_close(4004), Some(LifecycleAction::Park));
    assert_eq!(e.classify_close(4006), None);
    assert!(!e.is_inert());
    assert!(SessionEngine::new(SessionRules::default())
        .unwrap()
        .is_inert());
}

// --------------------------------------------------- log capture ----

/// Minimal subscriber that records every event's formatted fields.
#[derive(Clone, Default)]
struct Logs(Arc<Mutex<Vec<String>>>);

impl Logs {
    fn joined(&self) -> String {
        self.0.lock().unwrap().join("\n")
    }
}

struct Rec(String);

impl tracing::field::Visit for Rec {
    fn record_debug(&mut self, f: &tracing::field::Field, v: &dyn std::fmt::Debug) {
        self.0.push_str(&format!("{}={v:?} ", f.name()));
    }
}

impl tracing::Subscriber for Logs {
    fn enabled(&self, _: &tracing::Metadata<'_>) -> bool {
        true
    }
    fn new_span(&self, _: &tracing::span::Attributes<'_>) -> tracing::span::Id {
        tracing::span::Id::from_u64(1)
    }
    fn record(&self, _: &tracing::span::Id, _: &tracing::span::Record<'_>) {}
    fn record_follows_from(&self, _: &tracing::span::Id, _: &tracing::span::Id) {}
    fn event(&self, e: &tracing::Event<'_>) {
        let mut r = Rec(String::new());
        e.record(&mut r);
        self.0.lock().unwrap().push(r.0);
    }
    fn enter(&self, _: &tracing::span::Id) {}
    fn exit(&self, _: &tracing::span::Id) {}
}

// ------------------------------------------------------ rediscovery ----

use super::poll::PollAuth;
use super::rediscover::{Rediscover, RediscoverConfig, RediscoverError, RediscoverOutcome};

/// Records what the transport handed it and answers a scripted result.
#[derive(Debug)]
struct Hook {
    result: Result<RediscoverOutcome, RediscoverError>,
    bodies: Mutex<Vec<Vec<u8>>>,
    outcome: Mutex<Option<RediscoverOutcome>>,
}

impl Hook {
    fn new(result: Result<RediscoverOutcome, RediscoverError>) -> Arc<Self> {
        Arc::new(Self {
            result,
            bodies: Mutex::default(),
            outcome: Mutex::default(),
        })
    }
}

impl Rediscover for Hook {
    fn apply(&self, body: &[u8]) -> Result<RediscoverOutcome, RediscoverError> {
        self.bodies.lock().unwrap().push(body.to_vec());
        if let Ok(o) = self.result {
            *self.outcome.lock().unwrap() = Some(o);
        }
        self.result
    }

    fn take_outcome(&self) -> Option<RediscoverOutcome> {
        self.outcome.lock().unwrap().take()
    }
}

/// `/poll` answers 403 liveChatEnded; `/discover` answers the scripted reply.
/// Records `(path, authorization)` per request.
type RouteSeen = Vec<(String, Option<String>)>;

#[derive(Clone)]
struct TwoRoute {
    discover: Reply,
    seen: Arc<Mutex<RouteSeen>>,
}

async fn two_route(scripted: Reply) -> (SocketAddr, TwoRoute) {
    let s = TwoRoute {
        discover: scripted,
        seen: Arc::default(),
    };
    async fn poll(
        axum::extract::State(s): axum::extract::State<TwoRoute>,
        h: axum::http::HeaderMap,
    ) -> axum::response::Response {
        use axum::response::IntoResponse;
        s.seen.lock().unwrap().push((
            "/poll".into(),
            h.get("authorization").map(|v| v.to_str().unwrap().into()),
        ));
        (axum::http::StatusCode::FORBIDDEN, yt_error("liveChatEnded")).into_response()
    }
    async fn discover(
        axum::extract::State(s): axum::extract::State<TwoRoute>,
        h: axum::http::HeaderMap,
    ) -> axum::response::Response {
        use axum::response::IntoResponse;
        s.seen.lock().unwrap().push((
            "/discover".into(),
            h.get("authorization").map(|v| v.to_str().unwrap().into()),
        ));
        (
            axum::http::StatusCode::from_u16(s.discover.0).unwrap(),
            s.discover.2.clone(),
        )
            .into_response()
    }
    let app = axum::Router::new()
        .route("/poll", axum::routing::get(poll))
        .route("/discover", axum::routing::get(discover))
        .with_state(s.clone());
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(l, app).await.unwrap() });
    (addr, s)
}

fn rd_lifecycle() -> PollLifecycle {
    PollLifecycle {
        interval_hint: None,
        error_rules: vec![HttpErrorRule {
            status: 403,
            reason_pointer: "/error/errors/0/reason".into(),
            reasons: vec!["liveChatEnded".into()],
            action: HttpAction::Rediscover,
        }],
        query_secret: None,
    }
}

fn rd_cfg(addr: SocketAddr, hook: Arc<Hook>) -> PollConfig {
    let mut c = poll_cfg(addr, rd_lifecycle());
    c.auth = Some(PollAuth {
        header: "Authorization".into(),
        prefix: "Bearer ".into(),
        handle: "h1".into(),
    });
    c.rediscover = Some(RediscoverConfig {
        url: format!("http://{addr}/discover?x=1"),
        hook,
    });
    c
}

#[tokio::test]
async fn live_chat_ended_rediscovers_with_the_connections_own_credential_then_ends_the_run() {
    for (outcome, label) in [
        (RediscoverOutcome::Rediscovered, "rediscovered"),
        (RediscoverOutcome::WaitingForLive, "waiting_for_live"),
    ] {
        let (addr, srv) = two_route(ok(r#"{"items":[{"id":"b"}]}"#)).await;
        let hook = Hook::new(Ok(outcome));
        let (m, reg) = metrics();
        let mut t = PollTransport::new(rd_cfg(addr, hook.clone()), Fixed::new("tok"))
            .unwrap()
            .with_metrics(m);
        t.connect().await.unwrap();
        let e = t.next_frame().await.unwrap_err();
        assert!(
            e.is_fatal(),
            "the run ends so the runtime can retarget: {e}"
        );
        assert_eq!(hook.take_outcome(), Some(outcome));
        assert_eq!(
            hook.bodies.lock().unwrap().as_slice(),
            &[br#"{"items":[{"id":"b"}]}"#.to_vec()],
            "the hook got the discovery response verbatim"
        );
        let seen = srv.seen.lock().unwrap().clone();
        assert_eq!(
            seen,
            vec![
                ("/poll".to_owned(), Some("Bearer tok".to_owned())),
                ("/discover".to_owned(), Some("Bearer tok".to_owned())),
            ],
            "discovery used the same bearer credential, once"
        );
        assert_eq!(count(&reg, "poll_http", "rediscover"), 1);
        assert_eq!(count(&reg, "poll_rediscover", label), 1);
    }
}

#[tokio::test]
async fn rediscovery_failures_are_classified_not_swallowed() {
    // (discover reply, hook result, fatal?, metric outcome)
    let cases: Vec<(
        Reply,
        Result<RediscoverOutcome, RediscoverError>,
        bool,
        &str,
    )> = vec![
        (
            (401, vec![], String::new()),
            Ok(RediscoverOutcome::Rediscovered),
            true,
            "rejected",
        ),
        (
            (403, vec![], yt_error("forbidden")),
            Ok(RediscoverOutcome::Rediscovered),
            true,
            "rejected",
        ),
        (
            (500, vec![], String::new()),
            Ok(RediscoverOutcome::Rediscovered),
            false,
            "http_error",
        ),
        (
            ok("not json"),
            Err(RediscoverError::Malformed),
            false,
            "malformed",
        ),
        (ok("{}"), Err(RediscoverError::Refused), true, "refused"),
    ];
    for (reply, result, fatal, label) in cases {
        let (addr, _s) = two_route(reply).await;
        let hook = Hook::new(result);
        let (m, reg) = metrics();
        let mut t = PollTransport::new(rd_cfg(addr, hook.clone()), Fixed::new("tok"))
            .unwrap()
            .with_metrics(m);
        let e = t.next_frame().await.unwrap_err();
        assert_eq!(e.is_fatal(), fatal, "{label}: {e}");
        assert_eq!(count(&reg, "poll_rediscover", label), 1, "{label}");
        if label != "refused" {
            assert_eq!(hook.take_outcome(), None, "{label}: no outcome recorded");
        }
    }
}

#[tokio::test]
async fn rediscovery_config_is_validated_at_construction() {
    let (addr, _s) = two_route(ok("{}")).await;
    // A rediscover rule with no rediscovery config.
    let mut c = poll_cfg(addr, rd_lifecycle());
    c.rediscover = None;
    assert!(PollTransport::new(c, Fixed::new("x")).is_err());
    // A different port = a different credential destination.
    let mut c = rd_cfg(addr, Hook::new(Ok(RediscoverOutcome::WaitingForLive)));
    let other = c.rediscover.as_mut().unwrap();
    other.url = format!("http://127.0.0.1:{}/discover", addr.port().wrapping_add(1));
    assert!(PollTransport::new(c, Fixed::new("x")).is_err());
    // A different host.
    let mut c = rd_cfg(addr, Hook::new(Ok(RediscoverOutcome::WaitingForLive)));
    c.rediscover.as_mut().unwrap().url = format!("http://localhost:{}/discover", addr.port());
    assert!(PollTransport::new(c, Fixed::new("x")).is_err());
    // Plain http without the loopback test allowance.
    let mut c = rd_cfg(addr, Hook::new(Ok(RediscoverOutcome::WaitingForLive)));
    c.allow_loopback_insecure = false;
    assert!(PollTransport::new(c, Fixed::new("x")).is_err());
    // A hook without any rule that triggers it is allowed (inert).
    let mut c = rd_cfg(addr, Hook::new(Ok(RediscoverOutcome::WaitingForLive)));
    c.lifecycle = PollLifecycle::default();
    assert!(PollTransport::new(c, Fixed::new("x")).is_ok());
    // Debug never prints the URL.
    let dbg = format!(
        "{:?}",
        rd_cfg(addr, Hook::new(Ok(RediscoverOutcome::WaitingForLive))).rediscover
    );
    assert!(!dbg.contains("/discover"), "{dbg}");
    assert_eq!(RediscoverOutcome::Rediscovered.label(), "rediscovered");
}
