//! Behavioural tests of the connector driver's host-side contract (issue #840
//! B0, spec S2.6 / S3.5): size ceiling before invocation, events-per-frame
//! cap, platform spoof refusal, the three failure classes, reconnect under
//! backoff, and the loud-failure default invoker.
//!
//! The transport/invoker/sink here are scripted fakes ON PURPOSE -- they let
//! each failure class be driven deterministically. The real-path proof (real
//! WASM connector, real executor, real publish) is `core/connector_e2e`.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::VecDeque;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use svc_ingest::connector::{
    register_connector_metrics, ConnectionCtx, ConnectionEnd, ConnectorDriver, ConnectorEvent,
    ConnectorFrameError, ConnectorInvokeError, ConnectorInvoker, ConnectorMetrics,
    ConnectorRegistry, DriverError, EventSink, Handshake, PinnedConnector, RawFrame, RegistryError,
    SinkError, Transport, TransportError, UnwiredInvoker,
};
use tokio::sync::oneshot;

const PLATFORM: &str = "echo";

fn ctx(n: u32) -> ConnectionCtx {
    ConnectionCtx {
        platform: PLATFORM.to_string(),
        source_id: "src-1".to_string(),
        connection_id: format!("conn-{n}"),
        session_token: None,
    }
}

fn event(n: usize) -> ConnectorEvent {
    ConnectorEvent {
        platform: PLATFORM.to_string(),
        event_type: "message".to_string(),
        actor: Some(format!("actor-{n}")),
        payload_json: format!(r#"{{"n":{n}}}"#),
        occurred_at: "2026-10-12T00:00:00.000Z".to_string(),
    }
}

/// Reads exactly the scripted items per connection, then either closes
/// cleanly or (when `then_hang`) parks forever so shutdown can end it.
struct ScriptedTransport {
    platform: String,
    connects: VecDeque<Result<ConnectionCtx, TransportError>>,
    reads: VecDeque<Vec<Result<Option<Vec<u8>>, TransportError>>>,
    current: VecDeque<Result<Option<Vec<u8>>, TransportError>>,
    handshake_fails: bool,
    then_hang: bool,
    handshakes: Arc<Mutex<Vec<Handshake>>>,
    closes: Arc<Mutex<Vec<String>>>,
}

impl ScriptedTransport {
    fn new(connections: Vec<Vec<Result<Option<Vec<u8>>, TransportError>>>) -> Self {
        let connects = (0..connections.len())
            .map(|i| Ok(ctx(i as u32)))
            .collect::<VecDeque<_>>();
        Self {
            platform: PLATFORM.to_string(),
            connects,
            reads: connections.into(),
            current: VecDeque::new(),
            handshake_fails: false,
            then_hang: false,
            handshakes: Arc::default(),
            closes: Arc::default(),
        }
    }
}

impl Transport for ScriptedTransport {
    fn platform(&self) -> &str {
        &self.platform
    }

    async fn connect(&mut self) -> Result<ConnectionCtx, TransportError> {
        let next = self
            .connects
            .pop_front()
            .unwrap_or_else(|| Err(TransportError::Connect("script exhausted".to_string())));
        if next.is_ok() {
            self.current = self.reads.pop_front().unwrap_or_default().into();
        }
        next
    }

    async fn send_handshake(&mut self, handshake: Handshake) -> Result<(), TransportError> {
        self.handshakes.lock().unwrap().push(handshake);
        if self.handshake_fails {
            Err(TransportError::Handshake("refused".to_string()))
        } else {
            Ok(())
        }
    }

    async fn next_frame(&mut self) -> Result<Option<Vec<u8>>, TransportError> {
        match self.current.pop_front() {
            Some(item) => item,
            None if self.then_hang => std::future::pending().await,
            None => Ok(None),
        }
    }

    async fn close(&mut self, reason: &str) {
        self.closes.lock().unwrap().push(reason.to_string());
    }
}

type FrameResult = Result<Vec<ConnectorEvent>, ConnectorInvokeError>;

#[derive(Default)]
struct ScriptedInvoker {
    on_frame_results: Mutex<VecDeque<FrameResult>>,
    on_connect_error: Mutex<Option<ConnectorInvokeError>>,
    frame_calls: Mutex<u32>,
    disconnect_reasons: Mutex<Vec<String>>,
}

impl ScriptedInvoker {
    fn with_frames(results: Vec<FrameResult>) -> Self {
        Self {
            on_frame_results: Mutex::new(results.into()),
            ..Self::default()
        }
    }
}

impl ConnectorInvoker for ScriptedInvoker {
    async fn on_connect(
        &self,
        _platform: &str,
        _ctx: &ConnectionCtx,
    ) -> Result<Handshake, ConnectorInvokeError> {
        if let Some(e) = self.on_connect_error.lock().unwrap().clone() {
            return Err(e);
        }
        Ok(Handshake {
            bytes: b"hello".to_vec(),
            secret_refs: vec![("token".to_string(), "handle-1".to_string())],
        })
    }

    async fn on_frame(&self, _platform: &str, _frame: RawFrame) -> FrameResult {
        *self.frame_calls.lock().unwrap() += 1;
        self.on_frame_results
            .lock()
            .unwrap()
            .pop_front()
            .unwrap_or_else(|| Ok(Vec::new()))
    }

    async fn on_heartbeat_due(
        &self,
        _platform: &str,
        _ctx: &ConnectionCtx,
    ) -> Result<Option<Vec<u8>>, ConnectorInvokeError> {
        Ok(None)
    }

    async fn on_disconnect(
        &self,
        _platform: &str,
        _ctx: &ConnectionCtx,
        reason: &str,
    ) -> Result<(), ConnectorInvokeError> {
        self.disconnect_reasons
            .lock()
            .unwrap()
            .push(reason.to_string());
        Ok(())
    }
}

#[derive(Default)]
struct RecordingSink {
    published: Mutex<Vec<(ConnectionCtx, ConnectorEvent)>>,
    fail_n: Mutex<u32>,
}

impl EventSink for RecordingSink {
    async fn publish(&self, ctx: &ConnectionCtx, event: ConnectorEvent) -> Result<(), SinkError> {
        {
            let mut fail = self.fail_n.lock().unwrap();
            if *fail > 0 {
                *fail -= 1;
                return Err(SinkError::Invalid("injected".to_string()));
            }
        }
        self.published.lock().unwrap().push((ctx.clone(), event));
        Ok(())
    }
}

struct Rig<I: ConnectorInvoker> {
    driver: ConnectorDriver<I, RecordingSink>,
    invoker: Arc<I>,
    sink: Arc<RecordingSink>,
    metrics: ConnectorMetrics,
}

fn registry(max_frame_bytes: usize) -> Arc<ConnectorRegistry> {
    let mut r = ConnectorRegistry::new();
    r.register(PinnedConnector::new(
        PLATFORM,
        "waddles.core.example.echo",
        "sha256:aa",
        max_frame_bytes,
    ))
    .unwrap();
    Arc::new(r)
}

fn rig<I: ConnectorInvoker>(invoker: I, max_frame_bytes: usize) -> Rig<I> {
    let registry_metrics = prometheus::Registry::new();
    let metrics = register_connector_metrics(&registry_metrics).unwrap();
    let invoker = Arc::new(invoker);
    let sink = Arc::new(RecordingSink::default());
    Rig {
        driver: ConnectorDriver::new(
            registry(max_frame_bytes),
            Arc::clone(&invoker),
            Arc::clone(&sink),
            metrics.clone(),
        )
        .with_backoff_cap(Duration::from_millis(5)),
        invoker,
        sink,
        metrics,
    }
}

fn frames(items: &[&[u8]]) -> Vec<Result<Option<Vec<u8>>, TransportError>> {
    items.iter().map(|b| Ok(Some(b.to_vec()))).collect()
}

fn count(m: &ConnectorMetrics, family: &str, a: &str, b: &str) -> u64 {
    let vec = match family {
        "frames" => &m.frames_total,
        "events" => &m.events_total,
        "trap" => &m.trap_total,
        "connects" => &m.connects_total,
        other => panic!("unknown family {other}"),
    };
    vec.with_label_values(&[a, b]).get()
}

fn never_shutdown() -> (oneshot::Sender<()>, oneshot::Receiver<()>) {
    oneshot::channel()
}

#[tokio::test]
async fn a_good_frame_publishes_its_events_with_the_connection_as_context() {
    let r = rig(
        ScriptedInvoker::with_frames(vec![Ok(vec![event(1), event(2)])]),
        4096,
    );
    let mut t = ScriptedTransport::new(vec![frames(&[b"frame"])]);
    let (_keep, mut rx) = never_shutdown();
    let report = r.driver.serve_connection(&mut t, &mut rx).await.unwrap();

    assert_eq!(report.end, ConnectionEnd::RemoteClosed);
    assert!(report.established);
    assert_eq!(report.frames_ok, 1);
    let published = r.sink.published.lock().unwrap();
    assert_eq!(published.len(), 2);
    assert_eq!(published[0].0.connection_id, "conn-0");
    assert_eq!(count(&r.metrics, "frames", PLATFORM, "ok"), 1);
    assert_eq!(count(&r.metrics, "events", PLATFORM, "published"), 2);
    // The handshake crossed to the transport with the secret handle intact
    // (the transport, not the driver, substitutes it at send time).
    let hs = t.handshakes.lock().unwrap();
    assert_eq!(
        hs[0].secret_refs,
        vec![("token".to_string(), "handle-1".to_string())]
    );
    assert_eq!(
        r.metrics.connection_up.with_label_values(&[PLATFORM]).get(),
        0,
        "the up gauge must be back to 0 after the connection ended"
    );
    assert_eq!(
        *r.invoker.disconnect_reasons.lock().unwrap(),
        vec!["remote_closed"]
    );
}

#[tokio::test]
async fn an_oversize_frame_is_dropped_before_any_guest_runs_and_the_connection_continues() {
    let r = rig(ScriptedInvoker::with_frames(vec![Ok(vec![event(1)])]), 8);
    let mut t = ScriptedTransport::new(vec![frames(&[b"way-too-large-frame", b"small"])]);
    let (_keep, mut rx) = never_shutdown();
    let report = r.driver.serve_connection(&mut t, &mut rx).await.unwrap();

    assert_eq!(
        *r.invoker.frame_calls.lock().unwrap(),
        1,
        "the oversize frame must never reach the guest"
    );
    assert_eq!(count(&r.metrics, "frames", PLATFORM, "oversize"), 1);
    assert_eq!(report.frames_ok, 1);
    assert_eq!(r.sink.published.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn a_guest_frame_error_drops_only_that_frame() {
    let r = rig(
        ScriptedInvoker::with_frames(vec![
            Err(ConnectorInvokeError::Guest(ConnectorFrameError::Malformed(
                "x".into(),
            ))),
            Ok(vec![event(7)]),
        ]),
        4096,
    );
    let mut t = ScriptedTransport::new(vec![frames(&[b"bad", b"good"])]);
    let (_keep, mut rx) = never_shutdown();
    let report = r.driver.serve_connection(&mut t, &mut rx).await.unwrap();

    assert_eq!(report.end, ConnectionEnd::RemoteClosed);
    assert_eq!(report.frames_ok, 1);
    assert_eq!(count(&r.metrics, "frames", PLATFORM, "guest_error"), 1);
    assert_eq!(
        count(&r.metrics, "trap", PLATFORM, "guest_error"),
        0,
        "a bad frame is not a trap"
    );
    assert_eq!(r.sink.published.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn events_beyond_the_per_frame_cap_are_truncated_and_counted() {
    let flood: Vec<ConnectorEvent> = (0..100).map(event).collect();
    let r = rig(ScriptedInvoker::with_frames(vec![Ok(flood)]), 4096);
    let mut t = ScriptedTransport::new(vec![frames(&[b"flood"])]);
    let (_keep, mut rx) = never_shutdown();
    r.driver.serve_connection(&mut t, &mut rx).await.unwrap();

    assert_eq!(r.sink.published.lock().unwrap().len(), 64);
    assert_eq!(count(&r.metrics, "events", PLATFORM, "truncated"), 36);
    assert_eq!(count(&r.metrics, "events", PLATFORM, "published"), 64);
}

#[tokio::test]
async fn an_event_claiming_another_platform_is_refused_not_routed() {
    let mut spoof = event(1);
    spoof.platform = "discord".to_string();
    let r = rig(
        ScriptedInvoker::with_frames(vec![Ok(vec![spoof, event(2)])]),
        4096,
    );
    let mut t = ScriptedTransport::new(vec![frames(&[b"f"])]);
    let (_keep, mut rx) = never_shutdown();
    r.driver.serve_connection(&mut t, &mut rx).await.unwrap();

    let published = r.sink.published.lock().unwrap();
    assert_eq!(published.len(), 1);
    assert_eq!(published[0].1.platform, PLATFORM);
    assert_eq!(
        count(&r.metrics, "events", PLATFORM, "platform_mismatch"),
        1
    );
}

#[tokio::test]
async fn a_sink_failure_is_counted_and_the_next_event_still_publishes() {
    let r = rig(
        ScriptedInvoker::with_frames(vec![Ok(vec![event(1), event(2)])]),
        4096,
    );
    *r.sink.fail_n.lock().unwrap() = 1;
    let mut t = ScriptedTransport::new(vec![frames(&[b"f"])]);
    let (_keep, mut rx) = never_shutdown();
    r.driver.serve_connection(&mut t, &mut rx).await.unwrap();

    assert_eq!(count(&r.metrics, "events", PLATFORM, "invalid"), 1);
    assert_eq!(count(&r.metrics, "events", PLATFORM, "published"), 1);
}

#[tokio::test]
async fn a_sandbox_trip_tears_the_connection_down_and_is_counted_by_kind() {
    let r = rig(
        ScriptedInvoker::with_frames(vec![Err(ConnectorInvokeError::SandboxTrip {
            kind: "trap",
            message: "wasm trap".to_string(),
        })]),
        4096,
    );
    let mut t = ScriptedTransport::new(vec![frames(&[b"boom", b"never-read"])]);
    let (_keep, mut rx) = never_shutdown();
    let report = r.driver.serve_connection(&mut t, &mut rx).await.unwrap();

    assert_eq!(report.end, ConnectionEnd::ConnectorFault("trap"));
    assert_eq!(count(&r.metrics, "trap", PLATFORM, "trap"), 1);
    assert_eq!(
        *r.invoker.frame_calls.lock().unwrap(),
        1,
        "the frame after a trap must not be processed on this connection"
    );
    assert_eq!(
        *r.invoker.disconnect_reasons.lock().unwrap(),
        vec!["connector_fault"]
    );
    assert_eq!(*t.closes.lock().unwrap(), vec!["connector_fault"]);
}

#[tokio::test]
async fn run_reconnects_after_a_trip_under_backoff_and_then_delivers() {
    let r = rig(
        ScriptedInvoker::with_frames(vec![
            Err(ConnectorInvokeError::SandboxTrip {
                kind: "fuel_exhausted",
                message: String::new(),
            }),
            Ok(vec![event(9)]),
        ]),
        4096,
    );
    // Connection 1 trips; connection 2 delivers one frame then parks.
    let mut t = ScriptedTransport::new(vec![frames(&[b"spin"]), frames(&[b"ok"])]);
    t.then_hang = true;
    let (tx, rx) = oneshot::channel();
    let sink = Arc::clone(&r.sink);
    let task = tokio::spawn(async move { r.driver.run(t, rx).await });

    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    loop {
        if sink.published.lock().unwrap().len() == 1 {
            break;
        }
        assert!(
            tokio::time::Instant::now() < deadline,
            "the reconnect never delivered"
        );
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    tx.send(()).unwrap();
    task.await.unwrap().unwrap();
    assert_eq!(sink.published.lock().unwrap()[0].0.connection_id, "conn-1");
}

#[tokio::test]
async fn the_unwired_invoker_fails_loudly_and_publishes_nothing() {
    let r = rig(UnwiredInvoker, 4096);
    let mut t = ScriptedTransport::new(vec![frames(&[b"anything"])]);
    let (_keep, mut rx) = never_shutdown();
    let report = r.driver.serve_connection(&mut t, &mut rx).await.unwrap();

    assert_eq!(report.end, ConnectionEnd::ConnectorFault("unavailable"));
    assert!(!report.established);
    assert_eq!(count(&r.metrics, "trap", PLATFORM, "unavailable"), 1);
    assert!(r.sink.published.lock().unwrap().is_empty());
}

#[tokio::test]
async fn a_connector_that_refuses_to_build_a_handshake_is_a_handshake_failure() {
    let invoker = ScriptedInvoker::default();
    *invoker.on_connect_error.lock().unwrap() = Some(ConnectorInvokeError::Guest(
        ConnectorFrameError::Backend("x".into()),
    ));
    let r = rig(invoker, 4096);
    let mut t = ScriptedTransport::new(vec![frames(&[b"f"])]);
    let (_keep, mut rx) = never_shutdown();
    let report = r.driver.serve_connection(&mut t, &mut rx).await.unwrap();
    assert_eq!(report.end, ConnectionEnd::HandshakeFailed);
    assert_eq!(*r.invoker.frame_calls.lock().unwrap(), 0);
}

#[tokio::test]
async fn a_failed_handshake_send_and_a_failed_connect_are_distinct_outcomes() {
    let r = rig(ScriptedInvoker::default(), 4096);
    let mut t = ScriptedTransport::new(vec![frames(&[b"f"])]);
    t.handshake_fails = true;
    let (_keep, mut rx) = never_shutdown();
    assert_eq!(
        r.driver
            .serve_connection(&mut t, &mut rx)
            .await
            .unwrap()
            .end,
        ConnectionEnd::HandshakeFailed
    );

    let mut t = ScriptedTransport::new(vec![]);
    let report = r.driver.serve_connection(&mut t, &mut rx).await.unwrap();
    assert_eq!(report.end, ConnectionEnd::ConnectFailed);
    assert_eq!(count(&r.metrics, "connects", PLATFORM, "failed"), 1);
}

#[tokio::test]
async fn a_transport_read_error_ends_the_connection_as_a_transport_error() {
    let r = rig(ScriptedInvoker::default(), 4096);
    let mut t = ScriptedTransport::new(vec![vec![Err(TransportError::Read("reset".into()))]]);
    let (_keep, mut rx) = never_shutdown();
    let report = r.driver.serve_connection(&mut t, &mut rx).await.unwrap();
    assert_eq!(report.end, ConnectionEnd::TransportError);
}

#[tokio::test]
async fn shutdown_ends_a_parked_connection_cleanly() {
    let r = rig(ScriptedInvoker::default(), 4096);
    let mut t = ScriptedTransport::new(vec![vec![]]);
    t.then_hang = true;
    let (tx, mut rx) = oneshot::channel();
    let driver = Arc::new(r.driver);
    let d = Arc::clone(&driver);
    let task = tokio::spawn(async move {
        let report = d.serve_connection(&mut t, &mut rx).await.unwrap();
        (report, t.closes.lock().unwrap().clone())
    });
    tokio::time::sleep(Duration::from_millis(30)).await;
    tx.send(()).unwrap();
    let (report, closes) = task.await.unwrap();
    assert_eq!(report.end, ConnectionEnd::Shutdown);
    assert_eq!(closes, vec!["shutdown"]);
}

#[tokio::test]
async fn a_transport_with_no_pinned_connector_is_a_fatal_typed_error() {
    let registry_metrics = prometheus::Registry::new();
    let metrics = register_connector_metrics(&registry_metrics).unwrap();
    let driver = ConnectorDriver::new(
        Arc::new(ConnectorRegistry::new()),
        Arc::new(ScriptedInvoker::default()),
        Arc::new(RecordingSink::default()),
        metrics,
    );
    let mut t = ScriptedTransport::new(vec![frames(&[b"f"])]);
    let (_keep, mut rx) = never_shutdown();
    let err = driver.serve_connection(&mut t, &mut rx).await.unwrap_err();
    assert!(
        matches!(err, DriverError::Registry(RegistryError::NotRegistered(_))),
        "{err:?}"
    );
    assert!(
        t.handshakes.lock().unwrap().is_empty(),
        "nothing may be connected or sent for an unregistered platform"
    );
}

#[tokio::test]
async fn a_context_for_a_different_platform_is_a_fatal_typed_error() {
    let r = rig(ScriptedInvoker::default(), 4096);
    let mut t = ScriptedTransport::new(vec![frames(&[b"f"])]);
    let mut wrong = ctx(0);
    wrong.platform = "discord".to_string();
    t.connects = VecDeque::from(vec![Ok(wrong)]);
    let (_keep, mut rx) = never_shutdown();
    let err = r
        .driver
        .serve_connection(&mut t, &mut rx)
        .await
        .unwrap_err();
    assert!(
        matches!(err, DriverError::ContextPlatformMismatch { .. }),
        "{err:?}"
    );
}
