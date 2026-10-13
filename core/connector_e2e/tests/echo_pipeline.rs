//! The B0 end-to-end proof (issue #840, spec
//! `docs/superpowers/specs/2026-09-28-connector-bundles.md` S5.1 row 1 / S6
//! phase 2): a host transport feeds raw frames, the REAL bundle executor
//! invokes a REAL compiled WASM connector's `on-frame`, and the normalized
//! event comes out the other side as a D30-minted `StageEnvelope` on the right
//! per-source stream.
//!
//! Nothing between the transport and the appender is mocked: the driver, the
//! pinned registry, wasmtime with fuel/epoch/memory bounds, the per-component
//! `Linker`, the Echo/fault components and `publish_event` are all production
//! code. Only the two ends are synthetic -- an in-memory transport (the spec's
//! "synthetic in-memory transport") and a recording stand-in for Valkey's
//! `XADD` (an external dependency).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::HashSet;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use bundle_executor::config::CliConfig;
use bundle_executor::connector::{ConnectorHost, ConnectorLimits};
use bundle_executor::engine::{build_engine, spawn_epoch_ticker, LinkerCache};
use bundle_executor::manifest::{VerifiedManifest, WitWorld};
use clap::Parser;
use connector_e2e::ExecutorInvoker;
use penguin_spine::{KeyRing, NoopMetrics, Scope, SpineError, StageEnvelope};
use sha2::{Digest, Sha256};
use svc_ingest::connector::{
    register_connector_metrics, ConnectionCtx, ConnectorDriver, ConnectorMetrics,
    ConnectorRegistry, Handshake, PinnedConnector, SpineEventSink, Transport, TransportError,
};
use svc_ingest::publish::EventAppender;
use tokio::sync::{mpsc, oneshot};

const ECHO_WASM: &[u8] = include_bytes!("../../bundle_executor/tests/fixtures/echo_connector.wasm");
const FAULTY_WASM: &[u8] =
    include_bytes!("../../bundle_executor/tests/fixtures/faulty_connector.wasm");

/// Stands in for Valkey's `XADD`: records every `(stream, envelope)`.
#[derive(Default, Clone)]
struct RecordingAppender {
    calls: Arc<Mutex<Vec<(String, StageEnvelope)>>>,
}

impl EventAppender for RecordingAppender {
    async fn append(&self, stream: &str, env: &StageEnvelope) -> Result<String, SpineError> {
        let mut calls = self.calls.lock().unwrap();
        calls.push((stream.to_string(), env.clone()));
        Ok(format!("{}-0", calls.len()))
    }
}

/// The spec's synthetic in-memory transport: frames come from a channel and
/// survive reconnects (like a socket the remote keeps writing to).
struct InMemoryTransport {
    platform: String,
    rx: mpsc::UnboundedReceiver<Vec<u8>>,
    connections: u32,
    handshakes: Arc<Mutex<Vec<Handshake>>>,
}

impl Transport for InMemoryTransport {
    fn platform(&self) -> &str {
        &self.platform
    }

    async fn connect(&mut self) -> Result<ConnectionCtx, TransportError> {
        self.connections += 1;
        Ok(ConnectionCtx {
            platform: self.platform.clone(),
            source_id: "src-1".to_string(),
            connection_id: format!("conn-{}", self.connections),
            session_token: None,
        })
    }

    async fn send_handshake(&mut self, handshake: Handshake) -> Result<(), TransportError> {
        self.handshakes.lock().unwrap().push(handshake);
        Ok(())
    }

    async fn next_frame(&mut self) -> Result<Option<Vec<u8>>, TransportError> {
        Ok(self.rx.recv().await)
    }

    async fn close(&mut self, _reason: &str) {}
}

struct Harness {
    appender: RecordingAppender,
    metrics: ConnectorMetrics,
    handshakes: Arc<Mutex<Vec<Handshake>>>,
    frames: mpsc::UnboundedSender<Vec<u8>>,
    shutdown: oneshot::Sender<()>,
    task: tokio::task::JoinHandle<()>,
    _ticker: tokio::task::JoinHandle<()>,
}

fn digest_of(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("sha256:{:x}", hasher.finalize())
}

/// Loads `wasm` as the pinned connector for `platform` through the production
/// executor path and runs a driver over an in-memory transport until shutdown.
async fn start(
    platform: &str,
    wasm: &'static [u8],
    tune: impl FnOnce(&mut ConnectorLimits),
) -> Harness {
    let cfg = CliConfig::try_parse_from([
        "bundle-executor",
        "--stage-host-api-addr",
        "svc-process:8301",
    ])
    .unwrap();
    let engine = build_engine(&cfg).unwrap();
    let ticker = spawn_epoch_ticker(&engine);
    let app_id = format!("waddles.core.example.{platform}");
    let manifest = VerifiedManifest::new(
        app_id.clone(),
        digest_of(wasm),
        WitWorld::Connector,
        HashSet::new(),
    );
    let mut limits = ConnectorLimits::from_config(&cfg);
    tune(&mut limits);
    let host = ConnectorHost::load(
        &engine,
        &LinkerCache::new(engine.clone()),
        &manifest,
        wasm.to_vec(),
        limits,
        None,
    )
    .await
    .unwrap();

    let mut registry = ConnectorRegistry::new();
    registry
        .register(PinnedConnector::new(
            platform,
            host.app_id(),
            host.digest(),
            4096,
        ))
        .unwrap();

    let appender = RecordingAppender::default();
    let sink = SpineEventSink::new(
        appender.clone(),
        NoopMetrics,
        KeyRing::new(vec![("kid-1".to_string(), vec![5u8; 32])]),
        "kid-1",
        Scope::new("acme", Some("main".to_string())),
    );
    let prom = prometheus::Registry::new();
    let metrics = register_connector_metrics(&prom).unwrap();
    let driver = ConnectorDriver::new(
        Arc::new(registry),
        Arc::new(ExecutorInvoker::new().with_host(platform, host)),
        Arc::new(sink),
        metrics.clone(),
    )
    .with_backoff_cap(Duration::from_millis(5));

    let (frames, rx) = mpsc::unbounded_channel();
    let handshakes = Arc::new(Mutex::new(Vec::new()));
    let transport = InMemoryTransport {
        platform: platform.to_string(),
        rx,
        connections: 0,
        handshakes: Arc::clone(&handshakes),
    };
    let (shutdown, shutdown_rx) = oneshot::channel();
    let task = tokio::spawn(async move {
        driver.run(transport, shutdown_rx).await.unwrap();
    });
    Harness {
        appender,
        metrics,
        handshakes,
        frames,
        shutdown,
        task,
        _ticker: ticker,
    }
}

impl Harness {
    async fn wait_for_envelopes(&self, n: usize) {
        let deadline = tokio::time::Instant::now() + Duration::from_secs(20);
        loop {
            if self.appender.calls.lock().unwrap().len() >= n {
                return;
            }
            assert!(
                tokio::time::Instant::now() < deadline,
                "timed out waiting for {n} envelopes, have {}",
                self.appender.calls.lock().unwrap().len()
            );
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    }

    async fn stop(self) {
        self.shutdown.send(()).unwrap();
        self.task.await.unwrap();
    }
}

fn count(m: &ConnectorMetrics, family: &str, platform: &str, outcome: &str) -> u64 {
    let vec = match family {
        "frames" => &m.frames_total,
        "events" => &m.events_total,
        "trap" => &m.trap_total,
        other => panic!("unknown family {other}"),
    };
    vec.with_label_values(&[platform, outcome]).get()
}

/// THE B0 smoke: transport -> executor invokes Echo `on-frame` -> normalized
/// event emitted as a minted envelope.
#[tokio::test]
async fn echo_frame_flows_transport_to_executor_to_published_event() {
    let h = start("echo", ECHO_WASM, |_| {}).await;
    h.frames
        .send(br#"{"source":"echo","text":"  hello world  ","actor":"alice"}"#.to_vec())
        .unwrap();
    h.wait_for_envelopes(1).await;

    {
        let calls = h.appender.calls.lock().unwrap();
        let (stream, env) = &calls[0];
        assert_eq!(stream, "waddles:t:acme:c:main:src:echo:src-1:events");
        assert_eq!(env.event.platform, "echo");
        assert_eq!(env.event.event_type, "message");
        assert_eq!(env.event.actor.as_deref(), Some("alice"));
        assert_eq!(
            env.event.payload["text"], "hello world",
            "Echo trims the text"
        );
        assert_eq!(env.session_id.as_deref(), Some("conn-1"));
        assert_eq!(env.binding.kid, "kid-1");
        assert_eq!(
            env.binding.mac.len(),
            64,
            "ingest mints the D30 binding MAC"
        );
    }

    // `on-connect` ran in the guest and its handshake bytes reached the
    // transport, secret handles (none for Echo) intact.
    let handshakes = h.handshakes.lock().unwrap().clone();
    assert_eq!(handshakes.len(), 1);
    assert_eq!(handshakes[0].bytes, b"echo-hello");

    assert_eq!(count(&h.metrics, "frames", "echo", "ok"), 1);
    assert_eq!(count(&h.metrics, "events", "echo", "published"), 1);
    h.stop().await;
}

#[tokio::test]
async fn a_malformed_echo_frame_is_dropped_and_the_connection_keeps_serving() {
    let h = start("echo", ECHO_WASM, |_| {}).await;
    h.frames.send(b"this is not json".to_vec()).unwrap();
    h.frames
        .send(br#"{"source":"echo","text":"still alive"}"#.to_vec())
        .unwrap();
    h.wait_for_envelopes(1).await;

    assert_eq!(count(&h.metrics, "frames", "echo", "guest_error"), 1);
    assert_eq!(count(&h.metrics, "trap", "echo", "guest_error"), 0);
    assert_eq!(
        h.handshakes.lock().unwrap().len(),
        1,
        "no reconnect for a bad frame"
    );
    h.stop().await;
}

#[tokio::test]
async fn a_trapping_connector_is_contained_and_the_host_reconnects_and_keeps_serving() {
    let h = start("faulty", FAULTY_WASM, |_| {}).await;
    h.frames.send(b"trap".to_vec()).unwrap();
    h.frames.send(b"ok".to_vec()).unwrap();
    h.wait_for_envelopes(1).await;

    assert_eq!(count(&h.metrics, "trap", "faulty", "trap"), 1);
    assert_eq!(
        h.handshakes.lock().unwrap().len(),
        2,
        "the trap tore the connection down; the driver reconnected under backoff"
    );
    // The event came from the SECOND connection: the trapped store was
    // destroyed and nothing leaked into the next call.
    let session = h.appender.calls.lock().unwrap()[0].1.session_id.clone();
    assert_eq!(session.as_deref(), Some("conn-2"));
    h.stop().await;
}

#[tokio::test]
async fn a_runaway_connector_is_stopped_by_fuel_and_counted_as_a_trip() {
    let h = start("faulty", FAULTY_WASM, |l| {
        l.fuel = 2_000_000;
        l.deadline_ms = 30_000;
    })
    .await;
    h.frames.send(b"spin".to_vec()).unwrap();
    h.frames.send(b"ok".to_vec()).unwrap();
    h.wait_for_envelopes(1).await;

    assert_eq!(count(&h.metrics, "trap", "faulty", "fuel_exhausted"), 1);
    h.stop().await;
}

#[tokio::test]
async fn a_flooding_connector_is_capped_at_the_per_frame_event_limit() {
    let h = start("faulty", FAULTY_WASM, |_| {}).await;
    h.frames.send(b"flood:100".to_vec()).unwrap();
    h.wait_for_envelopes(64).await;
    // Give any (incorrect) overflow publish a moment to land before asserting.
    tokio::time::sleep(Duration::from_millis(100)).await;

    assert_eq!(h.appender.calls.lock().unwrap().len(), 64);
    assert_eq!(count(&h.metrics, "events", "faulty", "truncated"), 36);
    h.stop().await;
}

#[tokio::test]
async fn an_unregistered_platform_never_connects_and_fails_loudly() {
    // A transport whose platform has no pinned connector: the driver refuses
    // before connecting -- there is no native fallback to fall into silently.
    let (_tx, rx) = mpsc::unbounded_channel();
    let handshakes = Arc::new(Mutex::new(Vec::new()));
    let transport = InMemoryTransport {
        platform: "slack".to_string(),
        rx,
        connections: 0,
        handshakes: Arc::clone(&handshakes),
    };
    let prom = prometheus::Registry::new();
    let driver = ConnectorDriver::new(
        Arc::new(ConnectorRegistry::new()),
        Arc::new(ExecutorInvoker::new()),
        Arc::new(SpineEventSink::new(
            RecordingAppender::default(),
            NoopMetrics,
            KeyRing::new(vec![("kid-1".to_string(), vec![5u8; 32])]),
            "kid-1",
            Scope::new("acme", None),
        )),
        register_connector_metrics(&prom).unwrap(),
    );
    let (_keep, rx) = oneshot::channel();
    let err = driver.run(transport, rx).await.unwrap_err();
    assert!(err.to_string().contains("no connector is pinned"), "{err}");
    assert!(handshakes.lock().unwrap().is_empty());
}
