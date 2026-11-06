//! The WS driver's `HandshakeGate` hook (connector runtime spec 5.4): awaited
//! after `on-connect` and BEFORE any handshake bytes are sent; a refusal never
//! sends the handshake; `Exhausted{retry_after}` floors the reconnect delay;
//! `Fatal` parks; the default gate changes nothing (Twitch IRC regression).
//!
//! Scripted fakes on purpose: each refusal class is driven deterministically.
//! The real-Valkey proof of the Discord gate is `connector_identify_budget.rs`.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::VecDeque;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use svc_ingest::connector::webhook::BoxFut;
use svc_ingest::connector::{
    register_connector_metrics, ConnectionCtx, ConnectionEnd, ConnectorDriver, ConnectorEvent,
    ConnectorInvokeError, ConnectorInvoker, ConnectorMetrics, ConnectorRegistry, EventSink,
    GateCtx, GateError, GatePermit, Handshake, HandshakeGate, PinnedConnector, RawFrame, RunEnd,
    SinkError, Transport, TransportError,
};
use tokio::sync::oneshot;

const PLATFORM: &str = "gated";

/// Shared ordered event log so tests can assert gate-before-handshake.
type Log = Arc<Mutex<Vec<String>>>;

struct T {
    log: Log,
    token: Option<String>,
    connects: u32,
    reads: u32,
    first_frame_wait: Option<Duration>,
}

impl T {
    fn new(log: &Log, token: Option<&str>) -> Self {
        Self {
            log: Arc::clone(log),
            token: token.map(str::to_string),
            connects: 0,
            reads: 0,
            first_frame_wait: None,
        }
    }
}

impl Transport for T {
    fn platform(&self) -> &str {
        PLATFORM
    }
    async fn connect(&mut self) -> Result<ConnectionCtx, TransportError> {
        self.connects += 1;
        self.log.lock().unwrap().push("connect".into());
        Ok(ConnectionCtx {
            platform: PLATFORM.into(),
            source_id: "src".into(),
            connection_id: format!("c{}", self.connects),
            session_token: self.token.clone(),
        })
    }
    async fn send_handshake(&mut self, _h: Handshake) -> Result<(), TransportError> {
        self.log.lock().unwrap().push("handshake".into());
        Ok(())
    }
    async fn next_frame(&mut self) -> Result<Option<Vec<u8>>, TransportError> {
        self.log.lock().unwrap().push("next_frame".into());
        // The first read (HELLO) yields a frame; every later read is a clean close.
        self.reads += 1;
        Ok((self.reads == 1).then(|| b"hello".to_vec()))
    }
    fn handshake_after_first_frame(&self) -> Option<Duration> {
        self.first_frame_wait
    }
    async fn close(&mut self, reason: &str) {
        self.log.lock().unwrap().push(format!("close:{reason}"));
    }
}

struct Inv;
impl ConnectorInvoker for Inv {
    async fn on_connect(
        &self,
        _p: &str,
        _c: &ConnectionCtx,
    ) -> Result<Handshake, ConnectorInvokeError> {
        Ok(Handshake {
            bytes: b"hs".to_vec(),
            secret_refs: vec![],
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

struct Sink;
impl EventSink for Sink {
    async fn publish(&self, _c: &ConnectionCtx, _e: ConnectorEvent) -> Result<(), SinkError> {
        Ok(())
    }
}

/// A gate scripted per call; logs `gate` (with whether the ctx carried a
/// session token) into the shared log.
struct ScriptedGate {
    log: Log,
    script: Mutex<VecDeque<Result<GatePermit, GateError>>>,
    stamps: Mutex<Vec<Instant>>,
    hang: bool,
}

impl ScriptedGate {
    fn new(log: &Log, script: Vec<Result<GatePermit, GateError>>) -> Arc<Self> {
        Arc::new(Self {
            log: Arc::clone(log),
            script: Mutex::new(script.into()),
            stamps: Mutex::default(),
            hang: false,
        })
    }
}

impl HandshakeGate for ScriptedGate {
    fn acquire<'a>(&'a self, ctx: &'a GateCtx<'a>) -> BoxFut<'a, Result<GatePermit, GateError>> {
        Box::pin(async move {
            self.log
                .lock()
                .unwrap()
                .push(format!("gate:token={}", ctx.session_token.is_some()));
            self.stamps.lock().unwrap().push(Instant::now());
            if self.hang {
                std::future::pending::<()>().await;
            }
            self.script
                .lock()
                .unwrap()
                .pop_front()
                .unwrap_or(Err(GateError::Retryable {
                    reason: "script exhausted",
                }))
        })
    }
}

fn driver(gate: Option<Arc<dyn HandshakeGate>>) -> (ConnectorDriver<Inv, Sink>, ConnectorMetrics) {
    let mut r = ConnectorRegistry::new();
    r.register(PinnedConnector::new(PLATFORM, "id", "sha256:aa", 4096))
        .unwrap();
    let metrics = register_connector_metrics(&prometheus::Registry::new()).unwrap();
    let mut d = ConnectorDriver::new(Arc::new(r), Arc::new(Inv), Arc::new(Sink), metrics.clone())
        .with_backoff_cap(Duration::from_millis(5));
    if let Some(g) = gate {
        d = d.with_handshake_gate(g);
    }
    (d, metrics)
}

fn permit() -> GatePermit {
    GatePermit {
        consumed: true,
        waited: Duration::ZERO,
    }
}

fn pos(log: &Log, what: &str) -> usize {
    let events = log.lock().unwrap().clone();
    events
        .iter()
        .position(|e| e.starts_with(what))
        .unwrap_or_else(|| panic!("{what} not logged: {events:?}"))
}

#[tokio::test]
async fn the_gate_runs_after_connect_and_before_the_handshake_with_the_session_token() {
    let log = Log::default();
    let gate = ScriptedGate::new(&log, vec![Ok(permit())]);
    let (d, _) = driver(Some(gate.clone()));
    let mut t = T::new(&log, Some(r#"{"session_id":"abc"}"#));
    let (_k, mut rx) = oneshot::channel();
    let report = d.serve_connection(&mut t, &mut rx).await.unwrap();

    assert_eq!(report.end, ConnectionEnd::RemoteClosed);
    assert!(report.established);
    assert_eq!(
        gate.stamps.lock().unwrap().len(),
        1,
        "denominator: gate called once"
    );
    assert!(pos(&log, "connect") < pos(&log, "gate:token=true"));
    assert!(pos(&log, "gate:token=true") < pos(&log, "handshake"));
}

#[tokio::test]
async fn the_gate_runs_before_the_first_frame_read_when_handshake_waits_for_hello() {
    let log = Log::default();
    let gate = ScriptedGate::new(&log, vec![Ok(permit())]);
    let (d, _) = driver(Some(gate));
    let mut t = T::new(&log, None);
    t.first_frame_wait = Some(Duration::from_secs(1));
    let (_k, mut rx) = oneshot::channel();
    d.serve_connection(&mut t, &mut rx).await.unwrap();
    assert!(pos(&log, "gate:token=false") < pos(&log, "next_frame"));
    assert!(pos(&log, "next_frame") < pos(&log, "handshake"));
}

#[tokio::test]
async fn a_retryable_refusal_never_sends_the_handshake_and_closes_the_connection() {
    let log = Log::default();
    let gate = ScriptedGate::new(&log, vec![Err(GateError::Retryable { reason: "store" })]);
    let (d, m) = driver(Some(gate));
    let mut t = T::new(&log, None);
    let (_k, mut rx) = oneshot::channel();
    let report = d.serve_connection(&mut t, &mut rx).await.unwrap();

    assert_eq!(report.end, ConnectionEnd::HandshakeFailed);
    assert!(!report.established);
    assert_eq!(report.retry_after, None);
    assert!(!log.lock().unwrap().iter().any(|e| e == "handshake"));
    assert!(log
        .lock()
        .unwrap()
        .iter()
        .any(|e| e == "close:handshake_gate_denied"));
    assert_eq!(
        m.connects_total
            .with_label_values(&[PLATFORM, "gate_denied"])
            .get(),
        1
    );
}

#[tokio::test]
async fn an_exhausted_refusal_floors_the_reconnect_delay_instead_of_hammering() {
    let log = Log::default();
    let retry_after = Duration::from_millis(300);
    let gate = ScriptedGate::new(
        &log,
        vec![Err(GateError::Exhausted { retry_after }), Ok(permit())],
    );
    let (d, _) = driver(Some(gate.clone()));
    let mut t = T::new(&log, None);
    let (_k, mut rx) = oneshot::channel();
    let report = d.serve_connection(&mut t, &mut rx).await.unwrap();
    assert_eq!(report.retry_after, Some(retry_after));

    // run(): the second attempt must not start before retry_after elapsed
    // (the driver's own backoff cap is 5ms, so only the floor can delay it).
    let log2 = Log::default();
    let gate2 = ScriptedGate::new(
        &log2,
        vec![
            Err(GateError::Exhausted { retry_after }),
            Err(GateError::Fatal { reason: "stop" }),
        ],
    );
    let (d2, _) = driver(Some(gate2.clone()));
    let (_k2, rx2) = oneshot::channel();
    let end = tokio::time::timeout(Duration::from_secs(5), d2.run(T::new(&log2, None), rx2))
        .await
        .expect("a Fatal gate refusal must park, not loop")
        .unwrap();
    assert_eq!(end, RunEnd::Parked);
    let stamps = gate2.stamps.lock().unwrap();
    assert_eq!(stamps.len(), 2, "denominator: two gate calls");
    assert!(
        stamps[1] - stamps[0] >= retry_after,
        "reconnect came {:?} after the refusal, floor is {retry_after:?}",
        stamps[1] - stamps[0]
    );
}

#[tokio::test]
async fn a_fatal_refusal_parks_the_source() {
    let log = Log::default();
    let gate = ScriptedGate::new(&log, vec![Err(GateError::Fatal { reason: "cred" })]);
    let (d, m) = driver(Some(gate));
    let mut t = T::new(&log, None);
    let (_k, mut rx) = oneshot::channel();
    let report = d.serve_connection(&mut t, &mut rx).await.unwrap();
    assert_eq!(report.end, ConnectionEnd::Parked);
    assert!(!log.lock().unwrap().iter().any(|e| e == "handshake"));
    assert_eq!(m.parked_total.with_label_values(&[PLATFORM]).get(), 1);
}

#[tokio::test]
async fn shutdown_while_the_gate_waits_ends_cleanly_without_a_handshake() {
    let log = Log::default();
    let mut g = ScriptedGate::new(&log, vec![]);
    Arc::get_mut(&mut g).unwrap().hang = true;
    let (d, _) = driver(Some(g));
    let mut t = T::new(&log, None);
    let (tx, mut rx) = oneshot::channel();
    let h = tokio::spawn(async move {
        let r = d.serve_connection(&mut t, &mut rx).await.unwrap();
        (r, t.log.lock().unwrap().clone())
    });
    tokio::time::sleep(Duration::from_millis(50)).await;
    tx.send(()).unwrap();
    let (report, events) = h.await.unwrap();
    assert_eq!(report.end, ConnectionEnd::Shutdown);
    assert!(events.iter().any(|e| e.starts_with("gate")));
    assert!(!events.iter().any(|e| e == "handshake"));
}

#[tokio::test]
async fn the_default_gate_changes_nothing_for_platforms_without_one() {
    // Twitch-IRC-shaped regression: no gate installed -> connect, handshake,
    // pump exactly as before, no gate metric, no retry floor.
    let log = Log::default();
    let (d, m) = driver(None);
    let mut t = T::new(&log, None);
    let (_k, mut rx) = oneshot::channel();
    let report = d.serve_connection(&mut t, &mut rx).await.unwrap();
    assert_eq!(report.end, ConnectionEnd::RemoteClosed);
    assert!(report.established);
    assert_eq!(report.retry_after, None);
    assert_eq!(
        m.connects_total
            .with_label_values(&[PLATFORM, "gate_denied"])
            .get(),
        0
    );
    let ev = log.lock().unwrap().clone();
    assert_eq!(ev[..2], ["connect", "handshake"], "{ev:?}");
}
