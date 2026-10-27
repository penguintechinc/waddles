//! Behavioural tests of the connector driver's host-side contract (issue #840
//! B0, spec S2.6 / S3.5): size ceiling before invocation, events-per-frame
//! cap, platform spoof refusal, the three failure classes, reconnect under
//! backoff, and the loud-failure production invoker (no executor attached).
//!
//! The transport/invoker/sink here are scripted fakes ON PURPOSE -- they let
//! each failure class be driven deterministically. The real-path proof (real
//! WASM connector, real executor, real publish) is `core/connector_e2e`.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::VecDeque;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use svc_ingest::connector::ExecutorLink;
use svc_ingest::connector::{
    register_connector_metrics, ConnectionCtx, ConnectionEnd, ConnectorDriver, ConnectorEvent,
    ConnectorFrameError, ConnectorInvokeError, ConnectorInvoker, ConnectorMetrics,
    ConnectorRegistry, DriverError, EventLimits, EventSink, FrameDropReason, FrameOutcome,
    Handshake, PinnedConnector, RawFrame, RegistryError, RunEnd, SinkError, Transport,
    TransportError, WireConnectorInvoker,
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
    handshake_fatal: bool,
    outcomes: Arc<Mutex<Vec<FrameOutcome>>>,
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
            handshake_fatal: false,
            outcomes: Arc::default(),
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
        if self.handshake_fatal {
            Err(TransportError::Fatal("401".to_string()))
        } else if self.handshake_fails {
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

    async fn on_frame_outcome(&mut self, outcome: FrameOutcome) {
        self.outcomes.lock().unwrap().push(outcome);
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
                return Err(SinkError::Invalid("injected"));
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

fn registry(max_frame_bytes: usize, limits: EventLimits) -> Arc<ConnectorRegistry> {
    let mut r = ConnectorRegistry::new();
    let mut pinned = PinnedConnector::new(
        PLATFORM,
        "waddles.core.example.echo",
        "sha256:aa",
        max_frame_bytes,
    );
    pinned.event_limits = limits;
    r.register(pinned).unwrap();
    Arc::new(r)
}

fn rig<I: ConnectorInvoker>(invoker: I, max_frame_bytes: usize) -> Rig<I> {
    rig_with_limits(invoker, max_frame_bytes, EventLimits::default())
}

fn rig_with_limits<I: ConnectorInvoker>(
    invoker: I,
    max_frame_bytes: usize,
    limits: EventLimits,
) -> Rig<I> {
    let registry_metrics = prometheus::Registry::new();
    let metrics = register_connector_metrics(&registry_metrics).unwrap();
    let invoker = Arc::new(invoker);
    let sink = Arc::new(RecordingSink::default());
    Rig {
        driver: ConnectorDriver::new(
            registry(max_frame_bytes, limits),
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
async fn the_production_invoker_with_no_executor_fails_loudly_and_publishes_nothing() {
    let r = rig(
        WireConnectorInvoker::new(
            Arc::new(ExecutorLink::new()),
            registry(4096, EventLimits::default()),
        ),
        4096,
    );
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

// ---------------------------------------------------------------------------
// Hardening regressions (PR #844 review): PII-free logs, per-platform
// connection_up, host size caps, WARN sampling, `TransportError::Fatal`
// parking and the `on_frame_outcome` hook.
// ---------------------------------------------------------------------------

use std::sync::atomic::{AtomicU64, Ordering};

use tracing::field::{Field, Visit};
use tracing::span::{Attributes, Id, Record};
use tracing::{Event, Level, Metadata, Subscriber};

/// Every `tracing` event this thread emits, as `(level, "field=value ...")`.
#[derive(Clone, Default)]
struct Captured(Arc<Mutex<Vec<(Level, String)>>>);

/// Per-thread capture routes consulted by the process-global [`Capture`]
/// subscriber.
static ROUTES: std::sync::LazyLock<
    Mutex<std::collections::HashMap<std::thread::ThreadId, Captured>>,
> = std::sync::LazyLock::new(|| Mutex::new(std::collections::HashMap::new()));

/// Process-global subscriber routing each event to the capture registered for
/// the emitting thread (if any). It is installed ONCE as the global default:
/// a scoped `set_default` loses to `tracing`'s process-wide callsite-interest
/// cache whenever another test thread (no subscriber) reaches a callsite
/// first, which made the WARN capture see zero events under `cargo llvm-cov`.
struct Capture {
    next_span: AtomicU64,
}

struct Collect(String);

impl Visit for Collect {
    fn record_debug(&mut self, field: &Field, value: &dyn std::fmt::Debug) {
        self.0.push_str(&format!("{}={:?} ", field.name(), value));
    }
}

impl Subscriber for Capture {
    fn enabled(&self, _: &Metadata<'_>) -> bool {
        true
    }
    fn new_span(&self, _: &Attributes<'_>) -> Id {
        Id::from_u64(self.next_span.fetch_add(1, Ordering::SeqCst) + 1)
    }
    fn record(&self, _: &Id, _: &Record<'_>) {}
    fn record_follows_from(&self, _: &Id, _: &Id) {}
    fn event(&self, event: &Event<'_>) {
        let target = ROUTES
            .lock()
            .unwrap()
            .get(&std::thread::current().id())
            .cloned();
        if let Some(out) = target {
            let mut c = Collect(String::new());
            event.record(&mut c);
            out.0.lock().unwrap().push((*event.metadata().level(), c.0));
        }
    }
    fn enter(&self, _: &Id) {}
    fn exit(&self, _: &Id) {}
}

/// Unregisters the calling thread's capture when the test ends.
struct CaptureGuard(std::thread::ThreadId);

impl Drop for CaptureGuard {
    fn drop(&mut self) {
        ROUTES.lock().unwrap().remove(&self.0);
    }
}

/// Captures every event this test's thread emits. `#[tokio::test]` runs a
/// current-thread runtime, so thread identity == test identity.
fn capture_logs() -> (Captured, CaptureGuard) {
    static INSTALL: std::sync::Once = std::sync::Once::new();
    INSTALL.call_once(|| {
        // Another global default would leave the capture silently empty, so
        // fail loudly instead.
        tracing::subscriber::set_global_default(Capture {
            next_span: AtomicU64::new(0),
        })
        .expect("no other global tracing subscriber in this test binary");
    });
    let id = std::thread::current().id();
    let captured = Captured::default();
    ROUTES.lock().unwrap().insert(id, captured.clone());
    (captured, CaptureGuard(id))
}

fn limits_small() -> EventLimits {
    EventLimits {
        max_payload_bytes: 32,
        max_actor_bytes: 8,
        max_event_type_bytes: 16,
        max_frame_events_bytes: 1024,
    }
}

fn sized_event(payload_len: usize) -> ConnectorEvent {
    let mut e = event(1);
    e.payload_json = format!(r#"{{"t":"{}"}}"#, "x".repeat(payload_len));
    e
}

/// regression: `SandboxTrip.message` (a wasmtime chain), `Unavailable`
/// payloads and guest `actor`/`event_type`/payload text must never reach a log
/// record at ANY level (a host-import error can echo a URL/token).
#[tokio::test]
async fn logs_never_carry_guest_supplied_text_or_error_payloads() {
    const SECRET: &str = "SECRET-TOKEN-hunter2";
    let (captured, _guard) = capture_logs();

    let hostile_events = vec![
        {
            let mut e = event(1);
            e.actor = Some(format!("actor-{SECRET}-{}", "a".repeat(64)));
            e
        },
        {
            let mut e = event(2);
            e.event_type = format!("Bad Type {SECRET}");
            e
        },
        {
            let mut e = event(3);
            e.payload_json = format!(r#"{{"t":"{SECRET}-{}"}}"#, "p".repeat(64));
            e
        },
        {
            let mut e = event(4);
            e.platform = format!("evil-{SECRET}");
            e
        },
        event(5),
    ];
    let invoker = ScriptedInvoker::with_frames(vec![
        Err(ConnectorInvokeError::sandbox_trip(
            "trap",
            format!("host import failed: fetch https://x/?token={SECRET}"),
        )),
        Err(ConnectorInvokeError::Unavailable(format!(
            "pool said {SECRET}"
        ))),
        Err(ConnectorInvokeError::Guest(ConnectorFrameError::Backend(
            SECRET.to_string(),
        ))),
        Ok(hostile_events),
    ]);
    let r = rig_with_limits(invoker, 4096, limits_small());
    *r.sink.fail_n.lock().unwrap() = 1;
    let mut t = ScriptedTransport::new(vec![
        frames(&[b"a"]),
        frames(&[b"b"]),
        frames(&[b"c", b"d"]),
    ]);
    let (_keep, mut rx) = never_shutdown();
    for _ in 0..3 {
        r.driver.serve_connection(&mut t, &mut rx).await.unwrap();
    }

    // on-connect failing with a trip that carries the secret.
    let invoker = ScriptedInvoker::default();
    *invoker.on_connect_error.lock().unwrap() = Some(ConnectorInvokeError::sandbox_trip(
        "trap",
        format!("on-connect chain {SECRET}"),
    ));
    let r2 = rig(invoker, 4096);
    let mut t2 = ScriptedTransport::new(vec![frames(&[b"x"])]);
    r2.driver.serve_connection(&mut t2, &mut rx).await.unwrap();

    let logs = captured.0.lock().unwrap();
    assert!(
        logs.len() >= 8,
        "denominator: expected the driver to have logged, got {} records",
        logs.len()
    );
    assert!(
        logs.iter()
            .any(|(l, m)| *l == Level::WARN && m.contains("kind=")),
        "a class-only WARN must exist"
    );
    for (level, line) in logs.iter() {
        assert!(
            !line.contains(SECRET) && !line.contains("evil-") && !line.contains("Bad Type"),
            "guest text leaked into a {level} log: {line}"
        );
    }
}

/// regression: `connection_up` was `set(1)`/`set(0)` per platform, so one of
/// two connections ending reported 0 while the other was still up.
#[tokio::test]
async fn connection_up_counts_live_connections_per_platform() {
    let r = rig(ScriptedInvoker::default(), 4096);
    let up = r.metrics.connection_up.with_label_values(&[PLATFORM]);
    let driver = Arc::new(r.driver);

    let spawn_conn = |n: u32| {
        let mut t = ScriptedTransport::new(vec![vec![]]);
        t.connects = VecDeque::from(vec![Ok(ctx(n))]);
        t.then_hang = true;
        let (tx, mut rx) = oneshot::channel();
        let d = Arc::clone(&driver);
        let task = tokio::spawn(async move { d.serve_connection(&mut t, &mut rx).await.unwrap() });
        (tx, task)
    };
    let (tx_a, task_a) = spawn_conn(1);
    let (tx_b, task_b) = spawn_conn(2);

    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    while up.get() != 2 {
        assert!(
            tokio::time::Instant::now() < deadline,
            "never reached 2 up, at {}",
            up.get()
        );
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    tx_a.send(()).unwrap();
    task_a.await.unwrap();
    assert_eq!(up.get(), 1, "one disconnect must leave the other counted");
    tx_b.send(()).unwrap();
    task_b.await.unwrap();
    assert_eq!(up.get(), 0);
}

#[tokio::test]
async fn host_size_caps_and_event_type_shape_are_enforced_loudly_per_event() {
    let invoker = ScriptedInvoker::with_frames(vec![Ok(vec![
        event(1),        // valid
        sized_event(64), // payload over 32
        {
            let mut e = event(2);
            e.actor = Some("far-too-long-actor".to_string());
            e
        },
        {
            let mut e = event(3);
            e.event_type = "Bad Type".to_string(); // shape
            e
        },
        {
            let mut e = event(4);
            e.event_type = "a.very.long.event.type".to_string(); // over 16
            e
        },
    ])]);
    let r = rig_with_limits(invoker, 4096, limits_small());
    let mut t = ScriptedTransport::new(vec![frames(&[b"f"])]);
    let (_keep, mut rx) = never_shutdown();
    r.driver.serve_connection(&mut t, &mut rx).await.unwrap();

    assert_eq!(r.sink.published.lock().unwrap().len(), 1);
    assert_eq!(count(&r.metrics, "events", PLATFORM, "published"), 1);
    assert_eq!(count(&r.metrics, "events", PLATFORM, "oversize"), 3);
    assert_eq!(count(&r.metrics, "events", PLATFORM, "invalid"), 1);
    assert_eq!(
        *t.outcomes.lock().unwrap(),
        vec![FrameOutcome::Dropped(FrameDropReason::PublishIncomplete)]
    );
}

#[tokio::test]
async fn the_per_frame_event_bytes_aggregate_is_capped() {
    let limits = EventLimits {
        max_payload_bytes: 100,
        max_actor_bytes: 64,
        max_event_type_bytes: 64,
        max_frame_events_bytes: 150,
    };
    // Each payload is ~80 bytes: the first fits, the 2nd and 3rd push the
    // frame past 150.
    let invoker = ScriptedInvoker::with_frames(vec![Ok(vec![
        sized_event(70),
        sized_event(70),
        sized_event(70),
    ])]);
    let r = rig_with_limits(invoker, 4096, limits);
    let mut t = ScriptedTransport::new(vec![frames(&[b"f"])]);
    let (_keep, mut rx) = never_shutdown();
    r.driver.serve_connection(&mut t, &mut rx).await.unwrap();

    assert_eq!(count(&r.metrics, "events", PLATFORM, "published"), 1);
    assert_eq!(count(&r.metrics, "events", PLATFORM, "oversize"), 2);
}

/// regression: per-frame / per-event WARNs were unbounded. A hostile stream
/// must be fully COUNTED but only SAMPLED in the log.
#[tokio::test]
async fn a_flood_of_oversize_frames_and_failed_publishes_is_counted_but_log_sampled() {
    let (captured, _guard) = capture_logs();

    // 200 oversize frames.
    let r = rig(ScriptedInvoker::default(), 8);
    let flood: Vec<_> = (0..200).map(|_| Ok(Some(vec![b'z'; 32]))).collect();
    let mut t = ScriptedTransport::new(vec![flood]);
    let (_keep, mut rx) = never_shutdown();
    r.driver.serve_connection(&mut t, &mut rx).await.unwrap();
    assert_eq!(count(&r.metrics, "frames", PLATFORM, "oversize"), 200);

    // 10 frames x 64 events, every publish failing.
    let results = (0..10)
        .map(|_| Ok((0..64).map(event).collect::<Vec<_>>()))
        .collect();
    let r = rig(ScriptedInvoker::with_frames(results), 4096);
    *r.sink.fail_n.lock().unwrap() = 640;
    let mut t = ScriptedTransport::new(vec![frames(&[b"f".as_slice(); 10])]);
    r.driver.serve_connection(&mut t, &mut rx).await.unwrap();
    assert_eq!(count(&r.metrics, "events", PLATFORM, "invalid"), 640);

    let logs = captured.0.lock().unwrap();
    let oversize_warns = logs
        .iter()
        .filter(|(l, m)| *l == Level::WARN && m.contains("exceeds the platform ceiling"))
        .count();
    let publish_warns = logs
        .iter()
        .filter(|(l, m)| *l == Level::WARN && m.contains("not published"))
        .count();
    assert!(
        (1..=5).contains(&oversize_warns),
        "200 oversize frames must log 1..=5 WARNs, logged {oversize_warns}"
    );
    assert!(
        (1..=5).contains(&publish_warns),
        "640 failed publishes must log 1..=5 WARNs, logged {publish_warns}"
    );
}

/// regression: a terminal `Fatal` (401/auth-dead) must PARK the source, not
/// backoff-retry it forever.
#[tokio::test]
async fn a_fatal_connect_error_parks_the_source_and_is_never_retried() {
    let r = rig(ScriptedInvoker::default(), 4096);
    let mut t = ScriptedTransport::new(vec![]);
    t.connects = VecDeque::from(vec![
        Err(TransportError::Fatal("401 unauthorized".to_string())),
        Ok(ctx(1)), // must never be reached
    ]);
    let (_tx, rx) = oneshot::channel();
    let end = tokio::time::timeout(Duration::from_secs(5), r.driver.run(t, rx))
        .await
        .expect("a parked source must return, not loop")
        .unwrap();
    assert_eq!(end, RunEnd::Parked);
    assert_eq!(count(&r.metrics, "connects", PLATFORM, "fatal"), 1);
    assert_eq!(count(&r.metrics, "connects", PLATFORM, "ok"), 0);
    assert_eq!(
        r.metrics.parked_total.with_label_values(&[PLATFORM]).get(),
        1
    );
}

#[tokio::test]
async fn a_fatal_error_mid_stream_or_in_the_handshake_parks_without_reconnecting() {
    // Mid-stream: one good frame, then Fatal on read.
    let r = rig(ScriptedInvoker::default(), 4096);
    let mut t = ScriptedTransport::new(vec![vec![
        Ok(Some(b"f".to_vec())),
        Err(TransportError::Fatal("token revoked".to_string())),
    ]]);
    let (_keep, mut rx) = never_shutdown();
    let report = r.driver.serve_connection(&mut t, &mut rx).await.unwrap();
    assert_eq!(report.end, ConnectionEnd::Parked);
    assert!(report.established);
    assert_eq!(report.frames_ok, 1);
    assert_eq!(*t.closes.lock().unwrap(), vec!["parked"]);
    assert_eq!(
        *r.invoker.disconnect_reasons.lock().unwrap(),
        vec!["parked"]
    );

    // Same via `run`: returns Parked and never attempts a second connect
    // (a retry would hit the exhausted script and count a "failed" connect).
    let r = rig(ScriptedInvoker::default(), 4096);
    let t = ScriptedTransport::new(vec![vec![Err(TransportError::Fatal("x".to_string()))]]);
    let (_tx, rx2) = oneshot::channel();
    assert_eq!(r.driver.run(t, rx2).await.unwrap(), RunEnd::Parked);
    assert_eq!(count(&r.metrics, "connects", PLATFORM, "failed"), 0);
    assert_eq!(count(&r.metrics, "connects", PLATFORM, "ok"), 1);

    // Handshake send: Fatal parks.
    let r = rig(ScriptedInvoker::default(), 4096);
    let mut t = ScriptedTransport::new(vec![frames(&[b"f"])]);
    t.handshake_fatal = true;
    let report = r.driver.serve_connection(&mut t, &mut rx).await.unwrap();
    assert_eq!(report.end, ConnectionEnd::Parked);
    assert!(!report.established);
    assert_eq!(*r.invoker.frame_calls.lock().unwrap(), 0);
    assert_eq!(
        r.metrics.parked_total.with_label_values(&[PLATFORM]).get(),
        1
    );
}

#[tokio::test]
async fn a_non_fatal_connect_error_is_retried_not_parked() {
    let r = rig(ScriptedInvoker::with_frames(vec![Ok(vec![event(1)])]), 4096);
    let mut t = ScriptedTransport::new(vec![frames(&[b"ok"])]);
    t.connects = VecDeque::from(vec![
        Err(TransportError::Connect("reset".to_string())),
        Ok(ctx(1)),
    ]);
    t.then_hang = true;
    let (tx, rx) = oneshot::channel();
    let sink = Arc::clone(&r.sink);
    let metrics = r.metrics.clone();
    let task = tokio::spawn(async move { r.driver.run(t, rx).await });
    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    while sink.published.lock().unwrap().len() != 1 {
        assert!(tokio::time::Instant::now() < deadline, "never retried");
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    tx.send(()).unwrap();
    assert_eq!(task.await.unwrap().unwrap(), RunEnd::Shutdown);
    assert_eq!(metrics.parked_total.with_label_values(&[PLATFORM]).get(), 0);
}

#[tokio::test]
async fn every_yielded_frame_is_reported_back_to_the_transport_with_its_outcome() {
    let invoker = ScriptedInvoker::with_frames(vec![
        Ok(vec![]), // f1: nothing lost
        Err(ConnectorInvokeError::Guest(ConnectorFrameError::Malformed(
            "x".into(),
        ))), // f2
        Ok(vec![event(1)]), // f4: sink fails
        Ok(vec![event(2)]), // f5: published
        Err(ConnectorInvokeError::sandbox_trip("trap", "t")), // f6
    ]);
    let r = rig(invoker, 8);
    *r.sink.fail_n.lock().unwrap() = 1;
    // f3 is oversize (never reaches the guest).
    let mut t = ScriptedTransport::new(vec![frames(&[
        b"f1",
        b"f2",
        b"way-too-large-frame",
        b"f4",
        b"f5",
        b"f6",
    ])]);
    let (_keep, mut rx) = never_shutdown();
    r.driver.serve_connection(&mut t, &mut rx).await.unwrap();

    assert_eq!(
        *t.outcomes.lock().unwrap(),
        vec![
            FrameOutcome::Published,
            FrameOutcome::Dropped(FrameDropReason::GuestError),
            FrameOutcome::Dropped(FrameDropReason::Oversize),
            FrameOutcome::Dropped(FrameDropReason::PublishIncomplete),
            FrameOutcome::Published,
            FrameOutcome::Dropped(FrameDropReason::ConnectorFault),
        ],
        "exactly one outcome per yielded frame, in order"
    );
}

/// A stateful poll transport: yields snapshot pages in order and only
/// COMMITS its cursor over a page once the driver says it was published. The
/// first dropped page freezes the cursor so it can be re-yielded.
struct CursorTransport {
    pages: Vec<Vec<u8>>,
    yielded: usize,
    committed: Arc<Mutex<usize>>,
    blocked: bool,
}

impl Transport for CursorTransport {
    fn platform(&self) -> &str {
        PLATFORM
    }
    async fn connect(&mut self) -> Result<ConnectionCtx, TransportError> {
        Ok(ctx(0))
    }
    async fn send_handshake(&mut self, _h: Handshake) -> Result<(), TransportError> {
        Ok(())
    }
    async fn next_frame(&mut self) -> Result<Option<Vec<u8>>, TransportError> {
        let page = self.pages.get(self.yielded).cloned();
        self.yielded += 1;
        Ok(page)
    }
    async fn close(&mut self, _reason: &str) {}
    async fn on_frame_outcome(&mut self, outcome: FrameOutcome) {
        if outcome == FrameOutcome::Published && !self.blocked {
            *self.committed.lock().unwrap() += 1;
        } else {
            self.blocked = true;
        }
    }
}

/// regression: without the hook a poll transport's cursor advanced past a
/// page a guest error / publish failure had dropped, losing it for good.
#[tokio::test]
async fn on_frame_outcome_stops_a_poll_cursor_advancing_past_a_dropped_page() {
    let r = rig(
        ScriptedInvoker::with_frames(vec![
            Ok(vec![event(1)]),
            Err(ConnectorInvokeError::Guest(ConnectorFrameError::Backend(
                "x".into(),
            ))),
            Ok(vec![event(3)]),
        ]),
        4096,
    );
    let committed = Arc::new(Mutex::new(0usize));
    let mut t = CursorTransport {
        pages: vec![b"p1".to_vec(), b"p2".to_vec(), b"p3".to_vec()],
        yielded: 0,
        committed: Arc::clone(&committed),
        blocked: false,
    };
    let (_keep, mut rx) = never_shutdown();
    let report = r.driver.serve_connection(&mut t, &mut rx).await.unwrap();

    assert_eq!(report.frames_ok, 2, "p1 and p3 were processed");
    assert_eq!(r.sink.published.lock().unwrap().len(), 2);
    assert_eq!(
        *committed.lock().unwrap(),
        1,
        "the cursor must stop at the dropped p2, not advance to 3"
    );
}
