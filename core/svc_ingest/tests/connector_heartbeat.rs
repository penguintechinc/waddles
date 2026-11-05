//! Behavioural tests of the driver's heartbeat scheduling (P6a Task 5, spec
//! S3.5): a dedicated per-connection loop calls `on-heartbeat-due` on the
//! transport-declared interval and sends the body through the transport,
//! never delaying `on-frame`, with at most one heartbeat in flight, loud
//! failure classes, and shutdown safety.
//!
//! Most tests run under tokio's paused clock (deterministic timers on the
//! REAL driver loop); `real_time_*` run on the wall clock to prove the same
//! path with real timers.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use svc_ingest::connector::{
    register_connector_metrics, ConnectionCtx, ConnectionEnd, ConnectorDriver, ConnectorEvent,
    ConnectorFrameError, ConnectorInvokeError, ConnectorInvoker, ConnectorMetrics,
    ConnectorRegistry, DriverError, EventSink, Handshake, PinnedConnector, RawFrame, RunEnd,
    SinkError, Transport, TransportError, MAX_HEARTBEAT_INTERVAL, MIN_HEARTBEAT_INTERVAL,
};
use tokio::sync::oneshot;

const PLATFORM: &str = "hb";

fn ctx() -> ConnectionCtx {
    ConnectionCtx {
        platform: PLATFORM.to_string(),
        source_id: "src-1".to_string(),
        connection_id: "conn-0".to_string(),
        session_token: None,
    }
}

#[derive(Clone, Copy, PartialEq)]
enum SendMode {
    Ok,
    Fail,
    Fatal,
    /// Do not override `send_heartbeat`: exercises the trait's loud default.
    Default,
    /// The write never completes (a stalled socket).
    Hang,
}

struct HbTransport {
    interval: Option<Duration>,
    /// Frames to hand out, one per `frame_gap`, then hang forever.
    frames: VecDeque<Vec<u8>>,
    frame_gap: Duration,
    sent: Arc<Mutex<Vec<Vec<u8>>>>,
    defaults_sent: Arc<AtomicU32>,
    send_mode: SendMode,
    closes: Arc<Mutex<Vec<String>>>,
    connected: Arc<AtomicBool>,
}

impl HbTransport {
    fn new(interval: Option<Duration>) -> Self {
        Self {
            interval,
            frames: VecDeque::new(),
            frame_gap: Duration::ZERO,
            sent: Arc::default(),
            defaults_sent: Arc::default(),
            send_mode: SendMode::Ok,
            closes: Arc::default(),
            connected: Arc::default(),
        }
    }
}

impl Transport for HbTransport {
    fn platform(&self) -> &str {
        PLATFORM
    }

    async fn connect(&mut self) -> Result<ConnectionCtx, TransportError> {
        self.connected.store(true, Ordering::SeqCst);
        Ok(ctx())
    }

    async fn send_handshake(&mut self, _h: Handshake) -> Result<(), TransportError> {
        Ok(())
    }

    async fn next_frame(&mut self) -> Result<Option<Vec<u8>>, TransportError> {
        match self.frames.front() {
            Some(_) => {
                if !self.frame_gap.is_zero() {
                    tokio::time::sleep(self.frame_gap).await;
                }
                Ok(self.frames.pop_front())
            }
            None => std::future::pending().await,
        }
    }

    fn heartbeat_interval(&self) -> Option<Duration> {
        self.interval
    }

    async fn send_heartbeat(&mut self, bytes: Vec<u8>) -> Result<(), TransportError> {
        match self.send_mode {
            SendMode::Ok => {
                self.sent.lock().unwrap().push(bytes);
                Ok(())
            }
            SendMode::Fail => Err(TransportError::Send("boom".to_string())),
            SendMode::Fatal => Err(TransportError::Fatal("401".to_string())),
            SendMode::Hang => std::future::pending().await,
            SendMode::Default => Err(TransportError::Send(
                "transport declares a heartbeat interval but does not implement send_heartbeat"
                    .to_string(),
            )),
        }
    }

    async fn send_default_heartbeat(&mut self) -> Result<(), TransportError> {
        self.defaults_sent.fetch_add(1, Ordering::SeqCst);
        Ok(())
    }

    async fn close(&mut self, reason: &str) {
        self.closes.lock().unwrap().push(reason.to_string());
    }
}

/// A transport that keeps the trait's default `send_heartbeat`.
struct DefaultSendTransport(Duration);

impl Transport for DefaultSendTransport {
    fn platform(&self) -> &str {
        PLATFORM
    }
    async fn connect(&mut self) -> Result<ConnectionCtx, TransportError> {
        Ok(ctx())
    }
    async fn send_handshake(&mut self, _h: Handshake) -> Result<(), TransportError> {
        Ok(())
    }
    async fn next_frame(&mut self) -> Result<Option<Vec<u8>>, TransportError> {
        std::future::pending().await
    }
    fn heartbeat_interval(&self) -> Option<Duration> {
        Some(self.0)
    }
    async fn close(&mut self, _reason: &str) {}
}

#[derive(Clone)]
enum BeatScript {
    /// Return this body every time.
    Body(Vec<u8>),
    /// Connector has no body.
    Empty,
    /// Connector reports a frame-error.
    Guest,
    /// Sandbox trip.
    Trip,
    /// Take this long before answering.
    Slow(Duration),
    /// Hang forever while holding a guard whose drop is recorded.
    HangHolding(Arc<AtomicBool>),
}

/// Sets its flag when dropped: proves a future was cancelled/dropped.
struct DropFlag(Arc<AtomicBool>);

impl Drop for DropFlag {
    fn drop(&mut self) {
        self.0.store(true, Ordering::SeqCst);
    }
}

struct HbInvoker {
    script: BeatScript,
    beats: AtomicU32,
    frames: AtomicU32,
    /// How long `on_frame` takes (a slow guest / sink).
    frame_delay: Duration,
    disconnects: Mutex<Vec<String>>,
    /// Set when `on_disconnect` runs: was the heartbeat guard already dropped?
    beat_guard_dropped_at_disconnect: Mutex<Option<bool>>,
}

impl HbInvoker {
    fn new(script: BeatScript) -> Self {
        Self {
            script,
            beats: AtomicU32::new(0),
            frames: AtomicU32::new(0),
            frame_delay: Duration::ZERO,
            disconnects: Mutex::default(),
            beat_guard_dropped_at_disconnect: Mutex::default(),
        }
    }
}

impl ConnectorInvoker for HbInvoker {
    async fn on_connect(
        &self,
        _p: &str,
        _c: &ConnectionCtx,
    ) -> Result<Handshake, ConnectorInvokeError> {
        Ok(Handshake {
            bytes: b"hi".to_vec(),
            secret_refs: vec![],
        })
    }

    async fn on_frame(
        &self,
        _p: &str,
        _f: RawFrame,
    ) -> Result<Vec<ConnectorEvent>, ConnectorInvokeError> {
        if !self.frame_delay.is_zero() {
            tokio::time::sleep(self.frame_delay).await;
        }
        self.frames.fetch_add(1, Ordering::SeqCst);
        Ok(Vec::new())
    }

    async fn on_heartbeat_due(
        &self,
        _p: &str,
        _c: &ConnectionCtx,
    ) -> Result<Option<Vec<u8>>, ConnectorInvokeError> {
        self.beats.fetch_add(1, Ordering::SeqCst);
        match &self.script {
            BeatScript::Body(b) => Ok(Some(b.clone())),
            BeatScript::Empty => Ok(None),
            BeatScript::Guest => Err(ConnectorInvokeError::Guest(ConnectorFrameError::Backend(
                "no".to_string(),
            ))),
            BeatScript::Trip => Err(ConnectorInvokeError::sandbox_trip("trap", "x")),
            BeatScript::Slow(d) => {
                tokio::time::sleep(*d).await;
                Ok(Some(b"late".to_vec()))
            }
            BeatScript::HangHolding(flag) => {
                let _guard = DropFlag(Arc::clone(flag));
                std::future::pending().await
            }
        }
    }

    async fn on_disconnect(
        &self,
        _p: &str,
        _c: &ConnectionCtx,
        reason: &str,
    ) -> Result<(), ConnectorInvokeError> {
        self.disconnects.lock().unwrap().push(reason.to_string());
        if let BeatScript::HangHolding(flag) = &self.script {
            *self.beat_guard_dropped_at_disconnect.lock().unwrap() =
                Some(flag.load(Ordering::SeqCst));
        }
        Ok(())
    }
}

struct NullSink;

impl EventSink for NullSink {
    async fn publish(&self, _c: &ConnectionCtx, _e: ConnectorEvent) -> Result<(), SinkError> {
        Ok(())
    }
}

fn driver(
    invoker: &Arc<HbInvoker>,
    enabled: bool,
) -> (ConnectorDriver<HbInvoker, NullSink>, ConnectorMetrics) {
    let mut r = ConnectorRegistry::new();
    r.register(PinnedConnector::new(PLATFORM, "id", "sha256:aa", 4096))
        .unwrap();
    let metrics = register_connector_metrics(&prometheus::Registry::new()).unwrap();
    let d = ConnectorDriver::new(
        Arc::new(r),
        Arc::clone(invoker),
        Arc::new(NullSink),
        metrics.clone(),
    )
    .with_heartbeat_enabled(enabled);
    (d, metrics)
}

fn beats(m: &ConnectorMetrics, outcome: &str) -> u64 {
    m.heartbeats_total
        .with_label_values(&[PLATFORM, outcome])
        .get()
}

/// Lets spawned/pending futures run to quiescence under the paused clock.
async fn settle() {
    for _ in 0..20 {
        tokio::task::yield_now().await;
    }
}

#[tokio::test(start_paused = true)]
async fn heartbeat_body_is_sent_each_interval_and_shutdown_ends_cleanly() {
    let inv = Arc::new(HbInvoker::new(BeatScript::Body(b"PING".to_vec())));
    let (d, m) = driver(&inv, true);
    let mut t = HbTransport::new(Some(Duration::from_secs(10)));
    let sent = Arc::clone(&t.sent);
    let (stop, mut rx) = oneshot::channel::<()>();
    let h = tokio::spawn(async move { d.serve_connection(&mut t, &mut rx).await });

    settle().await;
    assert_eq!(
        sent.lock().unwrap().len(),
        0,
        "first beat is one period out"
    );
    tokio::time::advance(Duration::from_secs(10)).await;
    settle().await;
    assert_eq!(sent.lock().unwrap().len(), 1);
    for _ in 0..2 {
        // One tick per step: missed ticks are delayed, never burst.
        tokio::time::advance(Duration::from_secs(10)).await;
        settle().await;
    }
    assert_eq!(sent.lock().unwrap().len(), 3);
    assert_eq!(sent.lock().unwrap()[0], b"PING");
    assert_eq!(beats(&m, "sent"), 3);
    assert_eq!(
        m.heartbeat_duration_seconds
            .with_label_values(&[PLATFORM])
            .get_sample_count(),
        3
    );

    stop.send(()).unwrap();
    let report = h.await.unwrap().unwrap();
    assert_eq!(report.end, ConnectionEnd::Shutdown);
    assert_eq!(inv.disconnects.lock().unwrap().as_slice(), ["shutdown"]);
}

#[tokio::test(start_paused = true)]
async fn transport_without_an_interval_never_heartbeats() {
    let inv = Arc::new(HbInvoker::new(BeatScript::Body(b"PING".to_vec())));
    let (d, _m) = driver(&inv, true);
    let mut t = HbTransport::new(None);
    let (stop, mut rx) = oneshot::channel::<()>();
    let h = tokio::spawn(async move { d.serve_connection(&mut t, &mut rx).await });
    tokio::time::advance(Duration::from_secs(3600)).await;
    settle().await;
    assert_eq!(inv.beats.load(Ordering::SeqCst), 0);
    stop.send(()).unwrap();
    assert_eq!(h.await.unwrap().unwrap().end, ConnectionEnd::Shutdown);
}

#[tokio::test(start_paused = true)]
async fn flag_off_runs_no_heartbeat() {
    let inv = Arc::new(HbInvoker::new(BeatScript::Body(b"PING".to_vec())));
    let (d, m) = driver(&inv, false);
    let mut t = HbTransport::new(Some(Duration::from_secs(1)));
    t.frames = (0..3).map(|_| b"f".to_vec()).collect();
    t.frame_gap = Duration::from_secs(10);
    let connected = Arc::clone(&t.connected);
    let sent = Arc::clone(&t.sent);
    let (stop, mut rx) = oneshot::channel::<()>();
    let h = tokio::spawn(async move { d.serve_connection(&mut t, &mut rx).await });
    for _ in 0..4 {
        tokio::time::advance(Duration::from_secs(10)).await;
        settle().await;
    }
    // The connection really ran past every would-be tick (40 of them).
    assert!(connected.load(Ordering::SeqCst), "connection established");
    assert_eq!(inv.frames.load(Ordering::SeqCst), 3, "frames flowed");
    assert_eq!(inv.beats.load(Ordering::SeqCst), 0);
    assert!(sent.lock().unwrap().is_empty());
    assert_eq!(beats(&m, "sent"), 0);
    stop.send(()).unwrap();
    h.await.unwrap().unwrap();
}

/// regression: the zero-interval refusal ran before the flag check, so a
/// flag-OFF driver refused a connection over a heartbeat it would never run.
#[tokio::test(start_paused = true)]
async fn flag_off_has_no_effect_even_with_a_zero_interval() {
    let inv = Arc::new(HbInvoker::new(BeatScript::Empty));
    let (d, _m) = driver(&inv, false);
    let mut t = HbTransport::new(Some(Duration::ZERO));
    let connected = Arc::clone(&t.connected);
    let (stop, mut rx) = oneshot::channel::<()>();
    let h = tokio::spawn(async move { d.serve_connection(&mut t, &mut rx).await });
    tokio::time::advance(Duration::from_secs(5)).await;
    settle().await;
    assert!(connected.load(Ordering::SeqCst), "flag OFF must not refuse");
    assert_eq!(inv.beats.load(Ordering::SeqCst), 0);
    stop.send(()).unwrap();
    assert_eq!(h.await.unwrap().unwrap().end, ConnectionEnd::Shutdown);
}

#[tokio::test]
async fn a_zero_interval_is_a_loud_driver_error() {
    let inv = Arc::new(HbInvoker::new(BeatScript::Empty));
    let (d, _m) = driver(&inv, true);
    let mut t = HbTransport::new(Some(Duration::ZERO));
    let (_keep, mut rx) = oneshot::channel::<()>();
    let err = d.serve_connection(&mut t, &mut rx).await.unwrap_err();
    assert!(matches!(
        err,
        DriverError::HeartbeatIntervalOutOfRange { .. }
    ));
    assert!(
        !t.connected.load(Ordering::SeqCst),
        "refused before connecting"
    );
}

#[tokio::test(start_paused = true)]
async fn a_slow_heartbeat_does_not_starve_on_frame() {
    // The connector takes an hour to answer a heartbeat; frames still flow
    // promptly meanwhile.
    let inv = Arc::new(HbInvoker::new(BeatScript::Slow(Duration::from_secs(3600))));
    let (d, _m) = driver(&inv, true);
    let mut t = HbTransport::new(Some(Duration::from_secs(1)));
    t.frames = (0..50).map(|_| b"f".to_vec()).collect();
    t.frame_gap = Duration::from_secs(1);
    let (stop, mut rx) = oneshot::channel::<()>();
    let h = tokio::spawn(async move { d.serve_connection(&mut t, &mut rx).await });
    tokio::time::advance(Duration::from_secs(1)).await; // beat 1 starts, hangs
    for _ in 0..40 {
        tokio::time::advance(Duration::from_secs(1)).await;
        settle().await;
    }
    assert_eq!(
        inv.beats.load(Ordering::SeqCst),
        1,
        "a hung heartbeat blocks further beats (bounded: one in flight)"
    );
    assert!(
        inv.frames.load(Ordering::SeqCst) >= 40,
        "frames kept flowing while a heartbeat was in flight: {}",
        inv.frames.load(Ordering::SeqCst)
    );
    stop.send(()).unwrap();
    assert_eq!(h.await.unwrap().unwrap().end, ConnectionEnd::Shutdown);
}

#[tokio::test(start_paused = true)]
async fn shutdown_during_an_in_flight_heartbeat_is_prompt() {
    let inv = Arc::new(HbInvoker::new(BeatScript::Slow(Duration::from_secs(3600))));
    let (d, _m) = driver(&inv, true);
    let mut t = HbTransport::new(Some(Duration::from_secs(1)));
    let (stop, mut rx) = oneshot::channel::<()>();
    let h = tokio::spawn(async move { d.serve_connection(&mut t, &mut rx).await });
    settle().await;
    tokio::time::advance(Duration::from_secs(2)).await;
    settle().await;
    assert_eq!(inv.beats.load(Ordering::SeqCst), 1);
    stop.send(()).unwrap();
    let report = tokio::time::timeout(Duration::from_secs(1), h)
        .await
        .expect("shutdown must not wait on the in-flight heartbeat")
        .unwrap()
        .unwrap();
    assert_eq!(report.end, ConnectionEnd::Shutdown);
}

#[tokio::test(start_paused = true)]
async fn empty_and_guest_declined_beats_keep_the_connection_up() {
    for (script, outcome) in [(BeatScript::Guest, "guest_error")] {
        let inv = Arc::new(HbInvoker::new(script));
        let (d, m) = driver(&inv, true);
        let mut t = HbTransport::new(Some(Duration::from_secs(5)));
        let sent = Arc::clone(&t.sent);
        let (stop, mut rx) = oneshot::channel::<()>();
        let h = tokio::spawn(async move { d.serve_connection(&mut t, &mut rx).await });
        settle().await;
        for _ in 0..2 {
            tokio::time::advance(Duration::from_secs(5)).await;
            settle().await;
        }
        assert_eq!(beats(&m, outcome), 2, "{outcome}");
        assert!(sent.lock().unwrap().is_empty());
        stop.send(()).unwrap();
        assert_eq!(h.await.unwrap().unwrap().end, ConnectionEnd::Shutdown);
    }
}

#[tokio::test(start_paused = true)]
async fn a_heartbeat_sandbox_trip_tears_the_connection_down() {
    let inv = Arc::new(HbInvoker::new(BeatScript::Trip));
    let (d, m) = driver(&inv, true);
    let mut t = HbTransport::new(Some(Duration::from_secs(5)));
    let closes = Arc::clone(&t.closes);
    let (_keep, mut rx) = oneshot::channel::<()>();
    let h = tokio::spawn(async move { d.serve_connection(&mut t, &mut rx).await });
    tokio::time::advance(Duration::from_secs(5)).await;
    let report = h.await.unwrap().unwrap();
    assert_eq!(report.end, ConnectionEnd::ConnectorFault("trap"));
    assert!(report.established);
    assert_eq!(beats(&m, "trap"), 1);
    assert_eq!(m.trap_total.with_label_values(&[PLATFORM, "trap"]).get(), 1);
    assert_eq!(
        inv.disconnects.lock().unwrap().as_slice(),
        ["connector_fault"]
    );
    assert_eq!(closes.lock().unwrap().as_slice(), ["connector_fault"]);
}

#[tokio::test(start_paused = true)]
async fn send_failures_are_classified_loudly() {
    for (mode, end, outcome) in [
        (SendMode::Fail, ConnectionEnd::TransportError, "send_failed"),
        (SendMode::Fatal, ConnectionEnd::Parked, "send_failed"),
        (
            SendMode::Default,
            ConnectionEnd::TransportError,
            "send_failed",
        ),
    ] {
        let inv = Arc::new(HbInvoker::new(BeatScript::Body(b"P".to_vec())));
        let (d, m) = driver(&inv, true);
        let mut t = HbTransport::new(Some(Duration::from_secs(5)));
        t.send_mode = mode;
        let (_keep, mut rx) = oneshot::channel::<()>();
        let h = tokio::spawn(async move { d.serve_connection(&mut t, &mut rx).await });
        tokio::time::advance(Duration::from_secs(5)).await;
        let report = h.await.unwrap().unwrap();
        assert_eq!(report.end, end);
        assert_eq!(beats(&m, outcome), 1);
    }
}

#[tokio::test(start_paused = true)]
async fn the_trait_default_send_heartbeat_fails_loud() {
    let inv = Arc::new(HbInvoker::new(BeatScript::Body(b"P".to_vec())));
    let (d, m) = driver(&inv, true);
    let mut t = DefaultSendTransport(Duration::from_secs(5));
    let (_keep, mut rx) = oneshot::channel::<()>();
    let h = tokio::spawn(async move { d.serve_connection(&mut t, &mut rx).await });
    tokio::time::advance(Duration::from_secs(5)).await;
    let report = h.await.unwrap().unwrap();
    assert_eq!(report.end, ConnectionEnd::TransportError);
    assert_eq!(beats(&m, "send_failed"), 1);
}

#[tokio::test(start_paused = true)]
async fn run_stops_on_shutdown_with_heartbeat_active() {
    let inv = Arc::new(HbInvoker::new(BeatScript::Body(b"P".to_vec())));
    let (d, _m) = driver(&inv, true);
    let t = HbTransport::new(Some(Duration::from_secs(1)));
    let (stop, rx) = oneshot::channel::<()>();
    let h = tokio::spawn(async move { d.run(t, rx).await });
    tokio::time::advance(Duration::from_secs(3)).await;
    settle().await;
    stop.send(()).unwrap();
    assert_eq!(h.await.unwrap().unwrap(), RunEnd::Shutdown);
}

#[test]
fn transport_error_send_kind_is_stable() {
    let e = TransportError::Send("x".to_string());
    assert_eq!(e.kind(), "send");
    assert!(!e.is_fatal());
}

/// Real wall-clock timers (no paused clock): the same driver path ticks and
/// sends, interleaved with real frames.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn real_time_heartbeats_interleave_with_frames() {
    let inv = Arc::new(HbInvoker::new(BeatScript::Body(b"PING".to_vec())));
    let (d, m) = driver(&inv, true);
    let mut t = HbTransport::new(Some(MIN_HEARTBEAT_INTERVAL));
    t.frames = (0..10).map(|_| b"f".to_vec()).collect();
    t.frame_gap = Duration::from_millis(10);
    let sent = Arc::clone(&t.sent);
    let (stop, mut rx) = oneshot::channel::<()>();
    let h = tokio::spawn(async move { d.serve_connection(&mut t, &mut rx).await });
    tokio::time::sleep(Duration::from_millis(700)).await;
    stop.send(()).unwrap();
    let report = h.await.unwrap().unwrap();
    assert_eq!(report.end, ConnectionEnd::Shutdown);
    let n = sent.lock().unwrap().len();
    assert!(n >= 3, "expected >=3 real-time heartbeats, got {n}");
    assert_eq!(report.frames_ok, 10, "all frames processed alongside beats");
    assert_eq!(beats(&m, "sent"), n as u64);
}

/// regression: a `send_heartbeat` that never completes froze the pump (and so
/// every inbound frame) forever. The send now has a deadline; on expiry the
/// connection is torn down loudly so the backoff loop reconnects.
#[tokio::test(start_paused = true)]
async fn a_stalled_heartbeat_send_hits_a_deadline_and_tears_the_connection_down() {
    let inv = Arc::new(HbInvoker::new(BeatScript::Body(b"PING".to_vec())));
    let (d, m) = driver(&inv, true);
    let mut t = HbTransport::new(Some(Duration::from_secs(1)));
    t.send_mode = SendMode::Hang;
    let closes = Arc::clone(&t.closes);
    let (_keep, mut rx) = oneshot::channel::<()>();
    let h = tokio::spawn(async move { d.serve_connection(&mut t, &mut rx).await });
    for _ in 0..60 {
        tokio::time::advance(Duration::from_secs(1)).await;
        settle().await;
    }
    assert!(
        h.is_finished(),
        "a stalled send must not hang the pump forever"
    );
    let report = h.await.unwrap().unwrap();
    assert_eq!(report.end, ConnectionEnd::TransportError);
    assert_eq!(beats(&m, "send_timeout"), 1);
    assert_eq!(closes.lock().unwrap().as_slice(), ["transport_error"]);
}

/// A frame already in flight keeps making progress while a heartbeat send is
/// stalled (the send and the frame work are polled together).
#[tokio::test(start_paused = true)]
async fn an_in_flight_frame_completes_while_a_heartbeat_send_is_stalled() {
    let mut inv = HbInvoker::new(BeatScript::Body(b"PING".to_vec()));
    inv.frame_delay = Duration::from_secs(3);
    let inv = Arc::new(inv);
    let (d, _m) = driver(&inv, true);
    let mut t = HbTransport::new(Some(Duration::from_secs(1)));
    t.send_mode = SendMode::Hang;
    t.frames = VecDeque::from([b"f".to_vec()]);
    let (_keep, mut rx) = oneshot::channel::<()>();
    let h = tokio::spawn(async move { d.serve_connection(&mut t, &mut rx).await });
    settle().await;
    // Frame work starts at t=0 and takes 3s; the beat at t=1 stalls its send
    // (5s deadline, not yet hit at t=4).
    for _ in 0..4 {
        tokio::time::advance(Duration::from_secs(1)).await;
        settle().await;
    }
    assert_eq!(
        inv.frames.load(Ordering::SeqCst),
        1,
        "the in-flight frame finished while the send was stalled"
    );
    h.abort();
}

/// regression: a slow `on-frame`/publish used to delay a due heartbeat (the
/// send only ran between frames), so upstream could drop a healthy
/// connection. The beat is now sent while frame work is still in flight.
#[tokio::test(start_paused = true)]
async fn a_slow_frame_does_not_delay_a_due_heartbeat() {
    let mut inv = HbInvoker::new(BeatScript::Body(b"PING".to_vec()));
    inv.frame_delay = Duration::from_secs(3600);
    let inv = Arc::new(inv);
    let (d, _m) = driver(&inv, true);
    let mut t = HbTransport::new(Some(Duration::from_secs(10)));
    t.frames = VecDeque::from([b"f".to_vec()]);
    let sent = Arc::clone(&t.sent);
    let (stop, mut rx) = oneshot::channel::<()>();
    let h = tokio::spawn(async move { d.serve_connection(&mut t, &mut rx).await });
    settle().await;
    for _ in 0..3 {
        tokio::time::advance(Duration::from_secs(10)).await;
        settle().await;
    }
    assert_eq!(
        sent.lock().unwrap().len(),
        3,
        "heartbeats went out on schedule while on_frame was stuck"
    );
    stop.send(()).unwrap();
    assert_eq!(h.await.unwrap().unwrap().end, ConnectionEnd::Shutdown);
}

/// regression: the pinned heartbeat future outlived the select and was only
/// dropped at the end of `serve_connection`, i.e. AFTER `on-disconnect` ran,
/// so an in-flight `on-heartbeat-due` still held its resources then.
#[tokio::test(start_paused = true)]
async fn an_in_flight_heartbeat_is_cancelled_before_on_disconnect() {
    let dropped = Arc::new(AtomicBool::new(false));
    let inv = Arc::new(HbInvoker::new(BeatScript::HangHolding(Arc::clone(
        &dropped,
    ))));
    let (d, _m) = driver(&inv, true);
    let mut t = HbTransport::new(Some(Duration::from_secs(1)));
    let (stop, mut rx) = oneshot::channel::<()>();
    let h = tokio::spawn(async move { d.serve_connection(&mut t, &mut rx).await });
    settle().await;
    tokio::time::advance(Duration::from_secs(2)).await;
    settle().await;
    assert_eq!(inv.beats.load(Ordering::SeqCst), 1);
    stop.send(()).unwrap();
    assert_eq!(h.await.unwrap().unwrap().end, ConnectionEnd::Shutdown);
    assert_eq!(
        *inv.beat_guard_dropped_at_disconnect.lock().unwrap(),
        Some(true),
        "in-flight on-heartbeat-due must be dropped before on-disconnect"
    );
}

/// regression: `Ok(None)` from `on-heartbeat-due` was treated as "no
/// heartbeat", but the WIT contract says `none` = the host sends its
/// platform-default heartbeat unchanged.
#[tokio::test(start_paused = true)]
async fn a_none_body_sends_the_platform_default_heartbeat() {
    let inv = Arc::new(HbInvoker::new(BeatScript::Empty));
    let (d, m) = driver(&inv, true);
    let mut t = HbTransport::new(Some(Duration::from_secs(5)));
    let sent = Arc::clone(&t.sent);
    let defaults = Arc::clone(&t.defaults_sent);
    let (stop, mut rx) = oneshot::channel::<()>();
    let h = tokio::spawn(async move { d.serve_connection(&mut t, &mut rx).await });
    settle().await;
    for _ in 0..2 {
        tokio::time::advance(Duration::from_secs(5)).await;
        settle().await;
    }
    assert_eq!(defaults.load(Ordering::SeqCst), 2);
    assert!(sent.lock().unwrap().is_empty(), "no connector body to send");
    assert_eq!(beats(&m, "default_sent"), 2);
    stop.send(()).unwrap();
    assert_eq!(h.await.unwrap().unwrap().end, ConnectionEnd::Shutdown);
}

/// A transport that declares an interval but has no platform-default
/// heartbeat fails loud on a `none` body (never a silent no-op).
#[tokio::test(start_paused = true)]
async fn a_none_body_on_a_transport_without_a_default_heartbeat_fails_loud() {
    let inv = Arc::new(HbInvoker::new(BeatScript::Empty));
    let (d, m) = driver(&inv, true);
    let mut t = DefaultSendTransport(Duration::from_secs(5));
    let (_keep, mut rx) = oneshot::channel::<()>();
    let h = tokio::spawn(async move { d.serve_connection(&mut t, &mut rx).await });
    tokio::time::advance(Duration::from_secs(5)).await;
    let report = h.await.unwrap().unwrap();
    assert_eq!(report.end, ConnectionEnd::TransportError);
    assert_eq!(beats(&m, "send_failed"), 1);
}

/// regression: no floor/ceiling let a 1ns interval busy-loop the connector.
#[tokio::test]
async fn out_of_range_intervals_are_refused_before_connecting() {
    for bad in [
        Duration::from_nanos(1),
        MIN_HEARTBEAT_INTERVAL - Duration::from_nanos(1),
        MAX_HEARTBEAT_INTERVAL + Duration::from_nanos(1),
        Duration::MAX,
    ] {
        let inv = Arc::new(HbInvoker::new(BeatScript::Empty));
        let (d, _m) = driver(&inv, true);
        let mut t = HbTransport::new(Some(bad));
        let (_keep, mut rx) = oneshot::channel::<()>();
        let err = d.serve_connection(&mut t, &mut rx).await.unwrap_err();
        assert!(
            matches!(err, DriverError::HeartbeatIntervalOutOfRange { interval, .. } if interval == bad),
            "{bad:?}"
        );
        assert!(!t.connected.load(Ordering::SeqCst), "{bad:?}");
    }
}

/// The inclusive bounds themselves are accepted.
#[tokio::test(start_paused = true)]
async fn the_interval_bounds_are_inclusive() {
    for ok in [MIN_HEARTBEAT_INTERVAL, MAX_HEARTBEAT_INTERVAL] {
        let inv = Arc::new(HbInvoker::new(BeatScript::Empty));
        let (d, _m) = driver(&inv, true);
        let mut t = HbTransport::new(Some(ok));
        let (stop, mut rx) = oneshot::channel::<()>();
        let h = tokio::spawn(async move { d.serve_connection(&mut t, &mut rx).await });
        settle().await;
        stop.send(()).unwrap();
        assert_eq!(
            h.await.unwrap().unwrap().end,
            ConnectionEnd::Shutdown,
            "{ok:?}"
        );
    }
}

/// A transport whose heartbeat interval is learned from the wire: each frame
/// may carry a new interval (the host `heartbeat` capture, e.g. a HELLO).
struct DynTransport {
    interval: Option<Duration>,
    /// `(arrival offset from creation, interval it announces)`; arrival is an
    /// absolute deadline so the read is cancel-safe (the pump drops and
    /// re-creates `next_frame` whenever a beat is due).
    frames: VecDeque<(Duration, Option<Duration>)>,
    base: tokio::time::Instant,
    sent: Arc<Mutex<Vec<Vec<u8>>>>,
}

impl DynTransport {
    fn new(frames: Vec<(Duration, Option<Duration>)>) -> Self {
        Self {
            interval: None,
            frames: frames.into(),
            base: tokio::time::Instant::now(),
            sent: Arc::default(),
        }
    }
}

impl Transport for DynTransport {
    fn platform(&self) -> &str {
        PLATFORM
    }
    async fn connect(&mut self) -> Result<ConnectionCtx, TransportError> {
        Ok(ctx())
    }
    async fn send_handshake(&mut self, _h: Handshake) -> Result<(), TransportError> {
        Ok(())
    }
    async fn next_frame(&mut self) -> Result<Option<Vec<u8>>, TransportError> {
        match self.frames.front() {
            Some((at, _)) => {
                tokio::time::sleep_until(self.base + *at).await;
                let (_, announced) = self.frames.pop_front().unwrap();
                if announced.is_some() {
                    self.interval = announced;
                }
                Ok(Some(b"f".to_vec()))
            }
            None => std::future::pending().await,
        }
    }
    fn heartbeat_interval(&self) -> Option<Duration> {
        self.interval
    }
    async fn send_heartbeat(&mut self, bytes: Vec<u8>) -> Result<(), TransportError> {
        self.sent.lock().unwrap().push(bytes);
        Ok(())
    }
    async fn close(&mut self, _reason: &str) {}
}

/// A frame-announced interval starts the schedule one period after the
/// announcement (no heartbeat before it), and a later announcement
/// re-schedules it.
#[tokio::test(start_paused = true)]
async fn a_frame_announced_interval_starts_and_reschedules_the_heartbeat() {
    let inv = Arc::new(HbInvoker::new(BeatScript::Body(b"PING".to_vec())));
    let (d, _m) = driver(&inv, true);
    let mut t = DynTransport::new(vec![
        (Duration::from_secs(1), Some(Duration::from_secs(10))),
        (Duration::from_secs(36), Some(Duration::from_secs(2))),
    ]);
    let sent = Arc::clone(&t.sent);
    let (stop, mut rx) = oneshot::channel::<()>();
    let h = tokio::spawn(async move { d.serve_connection(&mut t, &mut rx).await });
    settle().await;
    tokio::time::advance(Duration::from_secs(1)).await; // t=1: HELLO-like frame
    settle().await;
    tokio::time::advance(Duration::from_secs(4)).await;
    settle().await;
    assert_eq!(
        sent.lock().unwrap().len(),
        0,
        "interval known at t=1, beat at t=11"
    );
    tokio::time::advance(Duration::from_secs(6)).await; // t=11
    settle().await;
    assert_eq!(sent.lock().unwrap().len(), 1);
    for _ in 0..2 {
        tokio::time::advance(Duration::from_secs(10)).await; // t=21, 31
        settle().await;
    }
    assert_eq!(sent.lock().unwrap().len(), 3);
    // t=36: the second frame re-announces 2 s -> beats every 2 s from t=36.
    tokio::time::advance(Duration::from_secs(5)).await;
    settle().await;
    let before = sent.lock().unwrap().len();
    for _ in 0..3 {
        tokio::time::advance(Duration::from_secs(2)).await;
        settle().await;
    }
    assert_eq!(
        sent.lock().unwrap().len(),
        before + 3,
        "2 s cadence after the change"
    );
    stop.send(()).unwrap();
    assert_eq!(h.await.unwrap().unwrap().end, ConnectionEnd::Shutdown);
}

/// An interval announced outside the driver bounds ends the connection
/// (loud) instead of busy-looping or never beating.
#[tokio::test(start_paused = true)]
async fn an_out_of_range_announced_interval_tears_the_connection_down() {
    for bad in [
        Duration::from_millis(10),
        MAX_HEARTBEAT_INTERVAL + Duration::from_secs(1),
    ] {
        let inv = Arc::new(HbInvoker::new(BeatScript::Body(b"PING".to_vec())));
        let (d, _m) = driver(&inv, true);
        let mut t = DynTransport::new(vec![(Duration::from_secs(1), Some(bad))]);
        let (_stop, mut rx) = oneshot::channel::<()>();
        let h = tokio::spawn(async move { d.serve_connection(&mut t, &mut rx).await });
        tokio::time::advance(Duration::from_secs(2)).await;
        settle().await;
        assert_eq!(h.await.unwrap().unwrap().end, ConnectionEnd::TransportError);
        assert_eq!(inv.beats.load(Ordering::SeqCst), 0);
    }
}

/// Flag OFF: an announced interval is ignored like a static one.
#[tokio::test(start_paused = true)]
async fn flag_off_ignores_an_announced_interval() {
    let inv = Arc::new(HbInvoker::new(BeatScript::Body(b"PING".to_vec())));
    let (d, _m) = driver(&inv, false);
    let mut t = DynTransport::new(vec![(Duration::from_secs(1), Some(Duration::from_secs(1)))]);
    let sent = Arc::clone(&t.sent);
    let (stop, mut rx) = oneshot::channel::<()>();
    let h = tokio::spawn(async move { d.serve_connection(&mut t, &mut rx).await });
    for _ in 0..5 {
        tokio::time::advance(Duration::from_secs(2)).await;
        settle().await;
    }
    assert!(sent.lock().unwrap().is_empty());
    stop.send(()).unwrap();
    h.await.unwrap().unwrap();
}
