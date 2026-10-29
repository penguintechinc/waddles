//! The connector end-to-end proof (issue #840, spec
//! `docs/superpowers/specs/2026-09-28-connector-bundles.md` S5.1 row 1 / S6
//! phase 2), now over the PRODUCTION wire path: a host transport feeds raw
//! frames, svc_ingest's `WireConnectorInvoker` carries each `receiver` export
//! as an `ExportKind::Connector*` `invoke` frame to the REAL bundle executor,
//! which runs a REAL compiled WASM connector, and the normalized event comes
//! out the other side as a D30-minted `StageEnvelope` on the right per-source
//! stream.
//!
//! Nothing between the transport and the appender is mocked: the driver, the
//! pinned registry, the wire invoker, the host-API codec, the executor's
//! `run_connection`/`ConnectorRouter`, wasmtime with fuel/epoch/memory
//! bounds, the per-component `Linker`, the Echo/fault components and
//! `publish_event` are all production code. Only the two ends are synthetic --
//! an in-memory transport (the spec's "synthetic in-memory transport"), a
//! duplex pipe standing in for the mTLS socket, and a recording stand-in for
//! Valkey's `XADD` (an external dependency).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::{Arc, Mutex};
use std::time::Duration;

use bundle_executor::connector::ConnectorLimits;
use connector_e2e::{RigConnector, WireRig};
use penguin_spine::{KeyRing, NoopMetrics, Scope, SpineError, StageEnvelope};
/// Fixed-DEK provider so the sink's identity-encryption path is real but offline.
struct FakeDekProvider;

impl svc_ingest::identity_crypto::DekProvider for FakeDekProvider {
    async fn get_dek(
        &self,
        _tenant_id: &str,
    ) -> Result<
        (svc_ingest::identity_crypto::Dek, u32),
        svc_ingest::identity_crypto::DekUnavailableError,
    > {
        Ok((svc_ingest::identity_crypto::Dek::new([9u8; 32]), 3))
    }
}

/// Decrypts one identity field the sink envelope-encrypted, using the
/// harness's fixed DEK and the exact `(tenant, stream, field, event_id)` AAD.
fn decrypt_field(value: &str, stream: &str, field: &str, event_id: &str) -> String {
    let obj: svc_ingest::identity_crypto::JsonEnvelope =
        serde_json::from_str(value).expect("identity field is a JSON envelope");
    svc_ingest::identity_crypto::decrypt_identity_value(
        &obj,
        &svc_ingest::identity_crypto::Dek::new([9u8; 32]),
        "acme",
        stream,
        field,
        event_id,
    )
    .expect("envelope decrypts under the fixed DEK")
}

/// Test-only identity-encryption counter the sink requires.
fn test_identity_metrics() -> prometheus::IntCounterVec {
    prometheus::IntCounterVec::new(
        prometheus::Opts::new("test_identity_encryption_total", "test-only"),
        &["tenant", "status"],
    )
    .expect("static metric name/labels are valid")
}

use svc_ingest::connector::{
    register_connector_metrics, ConnectionCtx, ConnectorDriver, ConnectorMetrics,
    ConnectorRegistry, ExecutorLink, Handshake, SpineEventSink, Transport, TransportError,
    WireConnectorInvoker,
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
    rig: WireRig,
}

/// Pins `wasm` as the connector for `platform` in a real executor, attaches it
/// to svc_ingest over the host-API wire and runs a driver over an in-memory
/// transport until shutdown.
async fn start(
    platform: &str,
    wasm: &'static [u8],
    tune: impl Fn(&mut ConnectorLimits),
) -> Harness {
    let rig = WireRig::start(
        vec![RigConnector {
            platform: platform.to_string(),
            app_id: format!("waddles.core.example.{platform}"),
            wasm,
        }],
        4096,
        tune,
    )
    .await;

    let appender = RecordingAppender::default();
    let sink = SpineEventSink::new(
        appender.clone(),
        NoopMetrics,
        KeyRing::new(vec![("kid-1".to_string(), vec![5u8; 32])]),
        "kid-1",
        Scope::new("acme", Some("main".to_string())),
        FakeDekProvider,
        test_identity_metrics(),
    );
    let prom = prometheus::Registry::new();
    let metrics = register_connector_metrics(&prom).unwrap();
    let driver = ConnectorDriver::new(
        Arc::clone(&rig.registry),
        Arc::clone(&rig.invoker),
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
        rig,
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

/// THE smoke, over the production wire: transport -> wire invoker -> executor
/// invokes Echo `on-frame` -> normalized event emitted as a minted envelope.
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
        // The sink's real identity-encryption path envelope-encrypts the
        // actor and the handle-bearing `text` before XADD; decrypt with the
        // same fixed DEK to prove the plaintext survived the round trip and
        // that nothing identity-bearing sits plaintext on the stream.
        let actor = env.event.actor.as_deref().expect("actor present");
        assert_ne!(actor, "alice", "actor must be ciphertext on the stream");
        assert_eq!(
            decrypt_field(actor, stream, "actor", &env.event_id),
            "alice"
        );
        let text = env.event.payload["text"]
            .as_str()
            .expect("text is a string");
        assert_ne!(text, "hello world", "text must be ciphertext on the stream");
        assert_eq!(
            decrypt_field(text, stream, "text", &env.event_id),
            "hello world",
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
    assert!(
        h.rig.link.is_attached(),
        "the frame travelled the live wire session"
    );
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
        Arc::new(WireConnectorInvoker::new(
            Arc::new(ExecutorLink::new()),
            Arc::new(ConnectorRegistry::new()),
        )),
        Arc::new(SpineEventSink::new(
            RecordingAppender::default(),
            NoopMetrics,
            KeyRing::new(vec![("kid-1".to_string(), vec![5u8; 32])]),
            "kid-1",
            Scope::new("acme", None),
            FakeDekProvider,
            test_identity_metrics(),
        )),
        register_connector_metrics(&prom).unwrap(),
    );
    let (_keep, rx) = oneshot::channel();
    let err = driver.run(transport, rx).await.unwrap_err();
    assert!(err.to_string().contains("no connector is pinned"), "{err}");
    assert!(handshakes.lock().unwrap().is_empty());
}

/// The wire invoker carries every `receiver` export, not just `on-frame`: the
/// fault connector supplies a non-default heartbeat body, and a connector
/// `frame-error::unsupported` crosses the wire back as a typed guest error.
#[tokio::test]
async fn the_invoker_reaches_heartbeat_and_disconnect_and_maps_guest_frame_errors() {
    use svc_ingest::connector::{
        ConnectorFrameError, ConnectorInvokeError, ConnectorInvoker, RawFrame,
    };

    let rig = faulty_rig("faulty").await;
    let inv = &rig.invoker;
    let ctx = ConnectionCtx {
        platform: "faulty".to_string(),
        source_id: "s".to_string(),
        connection_id: "c".to_string(),
        session_token: Some("resume-1".to_string()),
    };

    let hs = inv.on_connect("faulty", &ctx).await.unwrap();
    assert!(!hs.bytes.is_empty() || hs.secret_refs.is_empty());
    assert_eq!(
        inv.on_heartbeat_due("faulty", &ctx).await.unwrap(),
        Some(b"faulty-ping".to_vec())
    );
    inv.on_disconnect("faulty", &ctx, "test").await.unwrap();

    let err = inv
        .on_frame(
            "faulty",
            RawFrame {
                ctx: ctx.clone(),
                bytes: b"???".to_vec(),
                received_at: "2026-10-12T00:00:00.000Z".to_string(),
            },
        )
        .await
        .unwrap_err();
    assert!(
        matches!(
            err,
            ConnectorInvokeError::Guest(ConnectorFrameError::Unsupported(_))
        ),
        "{err:?}"
    );
}

async fn faulty_rig(platform: &str) -> WireRig {
    WireRig::start(
        vec![RigConnector {
            platform: platform.to_string(),
            app_id: format!("waddles.core.test.{platform}"),
            wasm: FAULTY_WASM,
        }],
        4096,
        |_| {},
    )
    .await
}

/// Frames are binary-safe over the JSON wire: bytes that are not UTF-8 reach
/// the guest intact (the Echo guest rejects them as a typed guest error, not a
/// wire/decoding failure).
#[tokio::test]
async fn non_utf8_frame_bytes_cross_the_wire_intact() {
    use svc_ingest::connector::{ConnectorInvokeError, ConnectorInvoker, RawFrame};

    let rig = faulty_rig("faulty").await;
    let ctx = ConnectionCtx {
        platform: "faulty".to_string(),
        source_id: "s".to_string(),
        connection_id: "c".to_string(),
        session_token: None,
    };
    // `faulty` treats unknown bytes as `frame-error::unsupported`: reaching
    // that proves the guest saw a well-formed frame, whatever its bytes.
    let err = rig
        .invoker
        .on_frame(
            "faulty",
            RawFrame {
                ctx,
                bytes: vec![0xff, 0xfe, 0x00, 0x80],
                received_at: "2026-10-12T00:00:00.000Z".to_string(),
            },
        )
        .await
        .unwrap_err();
    assert!(matches!(err, ConnectorInvokeError::Guest(_)), "{err:?}");
}

/// A pinned connector outlives the host-API connection: the executor wipes its
/// stage registry on disconnect, but connectors are always-pinned, so a
/// reconnecting stage finds them right where it left them.
#[tokio::test]
async fn a_pinned_connector_survives_an_executor_reconnect() {
    use svc_ingest::connector::ConnectorInvoker;

    let mut rig = faulty_rig("faulty").await;
    let ctx = ConnectionCtx {
        platform: "faulty".to_string(),
        source_id: "s".to_string(),
        connection_id: "c".to_string(),
        session_token: None,
    };
    rig.invoker.on_connect("faulty", &ctx).await.unwrap();

    rig.reconnect().await;

    assert_eq!(rig.executor.pinned_connectors().await, 1);
    rig.invoker.on_connect("faulty", &ctx).await.unwrap();
}

/// A dead executor is a typed, loud `Unavailable` -- never a silent empty
/// result -- and recovers on the next attach.
#[tokio::test]
async fn a_severed_executor_fails_loudly_then_recovers_on_reattach() {
    use svc_ingest::connector::{ConnectorInvokeError, ConnectorInvoker};

    let mut rig = faulty_rig("faulty").await;
    let ctx = ConnectionCtx {
        platform: "faulty".to_string(),
        source_id: "s".to_string(),
        connection_id: "c".to_string(),
        session_token: None,
    };
    rig.invoker.on_connect("faulty", &ctx).await.unwrap();

    rig.sever();
    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    while rig.link.is_attached() {
        assert!(
            tokio::time::Instant::now() < deadline,
            "a dead session stayed current"
        );
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    let err = rig.invoker.on_connect("faulty", &ctx).await.unwrap_err();
    assert!(
        matches!(err, ConnectorInvokeError::Unavailable(_)),
        "{err:?}"
    );

    rig.reconnect().await;
    rig.invoker.on_connect("faulty", &ctx).await.unwrap();
}

/// The executor is the authority on what is pinned: a stage naming a digest
/// the executor never pinned is refused `UNKNOWN_BUNDLE` (surfaced as
/// `Unavailable`), never routed to some other connector.
#[tokio::test]
async fn a_digest_the_executor_never_pinned_is_refused_not_rerouted() {
    use svc_ingest::connector::{
        ConnectorInvokeError, ConnectorInvoker, ConnectorRegistry as Registry, PinnedConnector,
    };

    let rig = faulty_rig("faulty").await;
    let mut registry = Registry::new();
    registry
        .register(PinnedConnector::new(
            "faulty",
            "waddles.core.test.faulty",
            "sha256:0000000000000000000000000000000000000000000000000000000000000000",
            4096,
        ))
        .unwrap();
    let stranger = WireConnectorInvoker::new(Arc::clone(&rig.link), Arc::new(registry));
    let ctx = ConnectionCtx {
        platform: "faulty".to_string(),
        source_id: "s".to_string(),
        connection_id: "c".to_string(),
        session_token: None,
    };
    let err = stranger.on_connect("faulty", &ctx).await.unwrap_err();
    assert!(
        matches!(&err, ConnectorInvokeError::Unavailable(m) if m.contains("UnknownBundle")),
        "{err:?}"
    );
}

/// `NoSource` serves no stage components: a fetch fails typed (naming the
/// requested key) rather than returning fabricated bytes.
#[tokio::test]
async fn the_harness_component_source_refuses_every_fetch_typed() {
    use bundle_executor::invoke::ComponentSource;

    let err = connector_e2e::NoSource
        .fetch("core/some-stage.wasm", "core/some-stage.json")
        .await
        .expect_err("NoSource must never serve a component");
    let msg = err.to_string();
    assert!(
        msg.contains("core/some-stage.wasm"),
        "error names the requested key: {msg}"
    );
}

/// `digest_of` yields the `sha256:<hex>` form the executor pins by.
#[test]
fn digest_of_is_the_sha256_prefixed_hex_form() {
    // sha256("") -- the well-known empty-input digest.
    assert_eq!(
        connector_e2e::digest_of(b""),
        "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    );
}
