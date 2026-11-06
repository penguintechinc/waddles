use super::*;
use crate::connector::ConnectorFrameError;
use crate::connector_runtime::ready::ExecutorStatus;
use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use tokio::sync::Semaphore;

struct Up;
impl ExecutorStatus for Up {
    fn attached(&self) -> bool {
        true
    }
}

fn status() -> Arc<RuntimeStatus> {
    Arc::new(RuntimeStatus::new(&["twitch"], Arc::new(Up)))
}

fn ctx() -> ConnectionCtx {
    ConnectionCtx {
        platform: "twitch".into(),
        source_id: "s".into(),
        connection_id: "c".into(),
        session_token: None,
    }
}

fn empty_hs() -> Handshake {
    Handshake {
        bytes: vec![],
        secret_refs: vec![],
    }
}

fn frame() -> RawFrame {
    RawFrame {
        ctx: ctx(),
        bytes: vec![],
        received_at: "t".into(),
    }
}

/// Invoker answering every call with a scripted result.
struct Scripted(Mutex<VecDeque<Result<(), ConnectorInvokeError>>>);
impl Scripted {
    fn next(&self) -> Result<(), ConnectorInvokeError> {
        self.0.lock().expect("lock").pop_front().expect("scripted")
    }
}
impl ConnectorInvoker for Scripted {
    async fn on_connect(
        &self,
        _p: &str,
        _c: &ConnectionCtx,
    ) -> Result<Handshake, ConnectorInvokeError> {
        self.next().map(|()| empty_hs())
    }
    async fn on_frame(
        &self,
        _p: &str,
        _f: RawFrame,
    ) -> Result<Vec<ConnectorEvent>, ConnectorInvokeError> {
        self.next().map(|()| vec![])
    }
    async fn on_heartbeat_due(
        &self,
        _p: &str,
        _c: &ConnectionCtx,
    ) -> Result<Option<Vec<u8>>, ConnectorInvokeError> {
        self.next().map(|()| None)
    }
    async fn on_disconnect(
        &self,
        _p: &str,
        _c: &ConnectionCtx,
        _r: &str,
    ) -> Result<(), ConnectorInvokeError> {
        self.next()
    }
}

fn pinned(s: &RuntimeStatus) -> bool {
    s.snapshot("twitch").expect("snap").pinned_ok
}

#[tokio::test]
async fn an_answered_call_proves_the_pin_but_unavailable_and_trips_do_not() {
    let script = Scripted(Mutex::new(VecDeque::from([
        Err(ConnectorInvokeError::Unavailable("no executor".into())),
        Err(ConnectorInvokeError::sandbox_trip("trap", "x")),
        Err(ConnectorInvokeError::Guest(ConnectorFrameError::Malformed(
            "bad".into(),
        ))),
    ])));
    let st = status();
    let inv = ObservedInvoker::new(Arc::new(script), st.clone());
    assert!(inv.on_connect("twitch", &ctx()).await.is_err());
    assert!(!pinned(&st), "unavailable is not an answer");
    assert!(inv.on_heartbeat_due("twitch", &ctx()).await.is_err());
    assert!(!pinned(&st), "a sandbox trip is not an answer");
    assert!(inv.on_frame("twitch", frame()).await.is_err());
    assert!(
        pinned(&st),
        "a guest frame error means the executor answered"
    );
}

#[tokio::test]
async fn every_invoker_method_delegates_and_success_pins() {
    let script = Scripted(Mutex::new(VecDeque::from([Ok(()), Ok(()), Ok(()), Ok(())])));
    let st = status();
    let inv = ObservedInvoker::new(Arc::new(script), st.clone());
    inv.on_connect("twitch", &ctx()).await.expect("connect");
    inv.on_disconnect("twitch", &ctx(), "bye")
        .await
        .expect("disconnect");
    inv.on_heartbeat_due("twitch", &ctx()).await.expect("hb");
    inv.on_frame("twitch", frame()).await.expect("frame");
    assert!(pinned(&st));
}

#[tokio::test(start_paused = true)]
async fn the_pacer_serves_waiters_one_gap_apart() {
    let p = Arc::new(Pacer::new(Duration::from_millis(300)));
    let t0 = Instant::now();
    p.wait().await;
    assert!(
        t0.elapsed() < Duration::from_millis(10),
        "first is immediate"
    );
    p.wait().await;
    assert!(t0.elapsed() >= Duration::from_millis(300));
    p.wait().await;
    assert!(t0.elapsed() >= Duration::from_millis(600));
}

/// Transport whose operations succeed or fail on demand.
struct Fake {
    connect_ok: AtomicBool,
    handshake_ok: AtomicBool,
    frames: Mutex<VecDeque<Result<Option<Vec<u8>>, TransportError>>>,
    closed: AtomicBool,
    outcomes: Mutex<Vec<&'static str>>,
}

impl Fake {
    fn new() -> Self {
        Self {
            connect_ok: AtomicBool::new(true),
            handshake_ok: AtomicBool::new(true),
            frames: Mutex::new(VecDeque::new()),
            closed: AtomicBool::new(false),
            outcomes: Mutex::new(vec![]),
        }
    }
}

impl Transport for Fake {
    fn platform(&self) -> &str {
        "twitch"
    }
    async fn connect(&mut self) -> Result<ConnectionCtx, TransportError> {
        if self.connect_ok.load(Ordering::SeqCst) {
            Ok(ctx())
        } else {
            Err(TransportError::Connect("down".into()))
        }
    }
    async fn send_handshake(&mut self, _h: Handshake) -> Result<(), TransportError> {
        if self.handshake_ok.load(Ordering::SeqCst) {
            Ok(())
        } else {
            Err(TransportError::Handshake("no".into()))
        }
    }
    async fn next_frame(&mut self) -> Result<Option<Vec<u8>>, TransportError> {
        self.frames
            .lock()
            .expect("lock")
            .pop_front()
            .expect("scripted")
    }
    fn heartbeat_interval(&self) -> Option<Duration> {
        Some(Duration::from_secs(7))
    }
    async fn send_heartbeat(&mut self, _b: Vec<u8>) -> Result<(), TransportError> {
        Ok(())
    }
    async fn send_default_heartbeat(&mut self) -> Result<(), TransportError> {
        Ok(())
    }
    fn handshake_after_first_frame(&self) -> Option<Duration> {
        Some(Duration::from_secs(3))
    }
    fn first_heartbeat_jitter(&self) -> bool {
        true
    }
    async fn close(&mut self, _r: &str) {
        self.closed.store(true, Ordering::SeqCst);
    }
    async fn on_frame_outcome(&mut self, _o: crate::connector::FrameOutcome) {
        self.outcomes.lock().expect("lock").push("seen");
    }
}

fn wrap(
    t: Fake,
    st: &Arc<RuntimeStatus>,
    sem: &Arc<Semaphore>,
) -> (ObservedTransport<Fake>, IntGauge) {
    let gauge = IntGauge::new("g", "g").expect("gauge");
    let permit = sem.clone().try_acquire_owned().expect("permit");
    (
        ObservedTransport::new(t, st.clone(), gauge.clone(), "twitch", None, Some(permit)),
        gauge,
    )
}

#[tokio::test]
async fn live_follows_handshake_and_frame_failures_and_the_permit_is_released() {
    let st = status();
    let sem = Arc::new(Semaphore::new(1));
    let fake = Fake::new();
    fake.frames
        .lock()
        .expect("lock")
        .extend([Ok(Some(vec![1])), Err(TransportError::Read("reset".into()))]);
    let (mut t, gauge) = wrap(fake, &st, &sem);
    assert_eq!(sem.available_permits(), 0);
    t.connect().await.expect("connect");
    assert_eq!(
        sem.available_permits(),
        0,
        "held until the handshake outcome"
    );
    t.send_handshake(empty_hs()).await.expect("hs");
    assert_eq!(sem.available_permits(), 1, "released after the handshake");
    assert_eq!(gauge.get(), 1);
    assert_eq!(st.snapshot("twitch").expect("snap").live, 1);
    assert!(t.next_frame().await.expect("frame").is_some());
    assert_eq!(gauge.get(), 1, "a good frame keeps it live");
    assert!(t.next_frame().await.is_err());
    assert_eq!(gauge.get(), 0, "a read error is not live");
    assert_eq!(st.snapshot("twitch").expect("snap").live, 0);
}

#[tokio::test]
async fn a_failed_connect_or_handshake_frees_the_permit_and_never_goes_live() {
    let st = status();
    let sem = Arc::new(Semaphore::new(2));
    let a = Fake::new();
    a.connect_ok.store(false, Ordering::SeqCst);
    let (mut ta, ga) = wrap(a, &st, &sem);
    assert!(ta.connect().await.is_err());
    assert_eq!(sem.available_permits(), 2);
    let b = Fake::new();
    b.handshake_ok.store(false, Ordering::SeqCst);
    let (mut tb, gb) = wrap(b, &st, &sem);
    tb.connect().await.expect("connect");
    assert!(tb.send_handshake(empty_hs()).await.is_err());
    assert_eq!(sem.available_permits(), 2);
    assert_eq!((ga.get(), gb.get()), (0, 0));
}

#[tokio::test]
async fn close_and_drop_clear_live_and_every_hook_delegates() {
    let st = status();
    let sem = Arc::new(Semaphore::new(1));
    let (mut t, gauge) = wrap(Fake::new(), &st, &sem);
    t.send_handshake(empty_hs()).await.expect("hs");
    assert_eq!(t.platform(), "twitch");
    assert_eq!(t.heartbeat_interval(), Some(Duration::from_secs(7)));
    assert_eq!(
        t.handshake_after_first_frame(),
        Some(Duration::from_secs(3))
    );
    assert!(t.first_heartbeat_jitter());
    t.send_heartbeat(vec![1]).await.expect("hb");
    t.send_default_heartbeat().await.expect("dhb");
    t.on_frame_outcome(crate::connector::FrameOutcome::Published)
        .await;
    assert_eq!(t.inner.outcomes.lock().expect("lock").len(), 1);
    t.close("bye").await;
    assert!(t.inner.closed.load(Ordering::SeqCst));
    assert_eq!(gauge.get(), 0);
    // Dropping a live transport (a cancelled driver) also clears it.
    let (mut t2, g2) = wrap(Fake::new(), &st, &Arc::new(Semaphore::new(1)));
    t2.send_handshake(empty_hs()).await.expect("hs");
    assert_eq!(g2.get(), 1);
    drop(t2);
    assert_eq!(g2.get(), 0);
    assert_eq!(st.snapshot("twitch").expect("snap").live, 0);
}
