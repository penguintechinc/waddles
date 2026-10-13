//! End-to-end proof that the executor's OTel metrics are EMITTED under real
//! execution: a real compiled WASI 0.2 component
//! (`tests/fixtures/hostile_fixture.wasm`) is loaded, invoked (with real guest
//! host calls crossing the real wire protocol to a scripted stage), driven into
//! a real epoch-deadline trap, and disconnected -- and every number a collector
//! would receive is read back out of an in-memory OTel exporter installed as
//! the PROCESS-GLOBAL meter provider, exactly the way production's
//! `telemetry::init_metrics` installs its own.
//!
//! Own process (every file under `tests/` is its own binary), because the
//! global meter provider is process-wide state that must be installed before
//! the first instrument is built.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::BTreeMap;
use std::sync::Arc;

use bundle_executor::config::CliConfig;
use bundle_executor::heartbeat::Heartbeat;
use bundle_executor::invoke::{ComponentSource, Executor};
use bundle_executor::metrics::{instrument_name, METRIC_NAMES};
use bundle_executor::wire::run_connection;
use opentelemetry_sdk::metrics::data::{AggregatedMetrics, MetricData};
use opentelemetry_sdk::metrics::{InMemoryMetricExporter, PeriodicReader, SdkMeterProvider};
use penguin_bundle_host::wire::{
    read_frame, write_frame, CapabilityKind, ErrorCode, ExportKind, Frame, HelloLimits,
    HelloOkBody, HostResultBody, InvokeBody, LoadBody, LoadLimits, Message, ShutdownBody,
};
use sha2::{Digest, Sha256};

const FIXTURE_WASM: &[u8] = include_bytes!("fixtures/hostile_fixture.wasm");
const APP_ID: &str = "waddles.test.metrics-e2e";

struct FixtureSource;

impl ComponentSource for FixtureSource {
    async fn fetch(
        &self,
        _component_key: &str,
        _sidecar_key: &str,
    ) -> Result<(Vec<u8>, Vec<u8>), bundle_executor::error::ExecutorError> {
        Ok((FIXTURE_WASM.to_vec(), b"{}".to_vec()))
    }
}

fn fixture_digest() -> String {
    let mut hasher = Sha256::new();
    hasher.update(FIXTURE_WASM);
    format!("sha256:{:x}", hasher.finalize())
}

fn test_config() -> CliConfig {
    use clap::Parser;
    let mut cfg = CliConfig::try_parse_from([
        "bundle-executor",
        "--stage-host-api-addr",
        "svc-process:8301",
    ])
    .expect("static test args always parse");
    // This test proves the EPOCH-deadline path (`executor_deadline`). Fuel
    // exhaustion now has its own `fuel_exhausted` label, and a tight busy
    // loop can burn the default 500M-fuel budget before the 300ms deadline on
    // a fast box -- so fuel is made effectively unbounded here to keep the
    // trap deterministic (fuel's own labeling is covered by the
    // `invoke::tests::metrics_emission` unit tests).
    cfg.executor_fuel_limit_transform = u64::MAX;
    cfg.executor_fuel_limit_dispatch = u64::MAX;
    cfg
}

/// One flattened exported data point.
struct Point {
    name: String,
    attrs: BTreeMap<String, String>,
    value: f64,
}

fn flatten(provider: &SdkMeterProvider, exporter: &InMemoryMetricExporter) -> Vec<Point> {
    provider.force_flush().expect("flush");
    let mut out = Vec::new();
    for resource in exporter.get_finished_metrics().expect("readable") {
        for scope in resource.scope_metrics() {
            for metric in scope.metrics() {
                let name = metric.name().to_string();
                let attrs_of = |iter: &mut dyn Iterator<Item = &opentelemetry::KeyValue>| {
                    iter.map(|kv| (kv.key.to_string(), kv.value.to_string()))
                        .collect::<BTreeMap<_, _>>()
                };
                match metric.data() {
                    AggregatedMetrics::F64(MetricData::Histogram(h)) => {
                        for dp in h.data_points() {
                            out.push(Point {
                                name: name.clone(),
                                attrs: attrs_of(&mut dp.attributes()),
                                value: dp.count() as f64,
                            });
                        }
                    }
                    AggregatedMetrics::U64(MetricData::Sum(s)) => {
                        for dp in s.data_points() {
                            out.push(Point {
                                name: name.clone(),
                                attrs: attrs_of(&mut dp.attributes()),
                                value: dp.value() as f64,
                            });
                        }
                    }
                    AggregatedMetrics::I64(MetricData::Sum(s)) => {
                        for dp in s.data_points() {
                            out.push(Point {
                                name: name.clone(),
                                attrs: attrs_of(&mut dp.attributes()),
                                value: dp.value() as f64,
                            });
                        }
                    }
                    AggregatedMetrics::U64(MetricData::Gauge(g)) => {
                        for dp in g.data_points() {
                            out.push(Point {
                                name: name.clone(),
                                attrs: attrs_of(&mut dp.attributes()),
                                value: dp.value() as f64,
                            });
                        }
                    }
                    _ => {}
                }
            }
        }
    }
    out
}

/// Highest value (cumulative temporality) of the series `documented` whose
/// attributes include every `(k, v)` in `filter`.
fn count(points: &[Point], documented: &str, filter: &[(&str, &str)]) -> f64 {
    points
        .iter()
        .filter(|p| {
            p.name == instrument_name(documented)
                && filter
                    .iter()
                    .all(|(k, v)| p.attrs.get(*k).map(String::as_str) == Some(*v))
        })
        .map(|p| p.value)
        .fold(0.0, f64::max)
}

/// The most recent value of a gauge-style series (explicit zeros included).
fn last(points: &[Point], documented: &str) -> Option<f64> {
    points
        .iter()
        .rfind(|p| p.name == instrument_name(documented))
        .map(|p| p.value)
}

async fn answer_one_host_call<S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin>(io: &mut S) {
    let frame = read_frame(io).await.expect("expected a host-call frame");
    let Message::HostCall(call) = frame.message else {
        panic!("expected host-call, got {:?}", frame.message);
    };
    let result = match (call.capability, call.op.as_str()) {
        (CapabilityKind::Kv, "set") => serde_json::json!({}),
        (CapabilityKind::Kv, "get") => serde_json::json!({ "value": [104, 105] }),
        other => panic!("unexpected host-call {other:?}"),
    };
    write_frame(
        io,
        &Frame::new(
            frame.id,
            Message::HostResult(HostResultBody {
                result: Some(result),
                error: None,
            }),
        ),
    )
    .await
    .expect("write host-result");
}

fn invoke_frame(id: u64, digest: &str, event_type: &str, deadline_ms: u64) -> Frame {
    Frame::new(
        id,
        Message::Invoke(InvokeBody {
            app_id: APP_ID.to_string(),
            digest: digest.to_string(),
            export: ExportKind::Transform,
            payload: serde_json::json!({
                "platform": "test",
                "event_type": event_type,
                "actor": null,
                "payload_json": "{}",
                "occurred_at": "2026-09-22T00:00:00.000Z",
            }),
            deadline_ms,
            trace: None,
        }),
    )
}

/// The stage half: handshake, `load`, a `kv-roundtrip` invoke (two real guest
/// host calls), a `busy-loop` invoke that must hit its epoch deadline, then
/// `shutdown`.
async fn run_fake_stage<S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin>(
    mut io: S,
    digest: String,
) {
    let hello = read_frame(&mut io).await.expect("hello");
    write_frame(
        &mut io,
        &Frame::new(
            hello.id,
            Message::HelloOk(HelloOkBody {
                stage: "svc-process".to_string(),
                protocol_version: 1,
                limits: HelloLimits {
                    call_timeout_ms: 2000,
                    memory_mb: 64,
                    max_concurrent_calls: 32,
                },
            }),
        ),
    )
    .await
    .expect("write hello-ok");

    write_frame(
        &mut io,
        &Frame::new(
            1000,
            Message::Load(LoadBody {
                tenant_id: 1,
                community_id: 0,
                app_id: APP_ID.to_string(),
                version: "1".to_string(),
                digest: digest.clone(),
                component_key: "bundles/metrics-e2e/1/x.wasm".to_string(),
                sidecar_key: "bundles/metrics-e2e/1/x.json".to_string(),
                capabilities: vec![],
                limits: LoadLimits {
                    timeout_ms: 2000,
                    memory_mb: 64,
                },
            }),
        ),
    )
    .await
    .expect("write load");
    let loaded = read_frame(&mut io).await.expect("loaded reply");
    assert!(matches!(loaded.message, Message::Loaded(_)));

    write_frame(&mut io, &invoke_frame(1001, &digest, "kv-roundtrip", 5000))
        .await
        .expect("write invoke");
    answer_one_host_call(&mut io).await; // kv.set
    answer_one_host_call(&mut io).await; // kv.get
    let ok = read_frame(&mut io).await.expect("transform result");
    assert!(matches!(ok.message, Message::Result(_)));

    write_frame(&mut io, &invoke_frame(1002, &digest, "busy-loop", 300))
        .await
        .expect("write busy invoke");
    let trapped = read_frame(&mut io).await.expect("trap reply");
    let Message::Error(body) = trapped.message else {
        panic!("expected an error frame, got {:?}", trapped.message);
    };
    assert_eq!(body.code, ErrorCode::ExecutorDeadline);

    write_frame(
        &mut io,
        &Frame::new(1003, Message::Shutdown(ShutdownBody { grace_ms: 100 })),
    )
    .await
    .expect("write shutdown");
}

/// Multi-threaded: the epoch ticker is a runtime task and the busy guest owns
/// a worker thread while it spins.
#[tokio::test(flavor = "multi_thread", worker_threads = 3)]
async fn real_load_invoke_trap_and_disconnect_emit_the_documented_metrics() {
    // Install the in-memory provider as the global one BEFORE the first
    // instrument exists (`Executor::new` builds them from the global meter).
    let exporter = InMemoryMetricExporter::default();
    let provider = SdkMeterProvider::builder()
        .with_reader(PeriodicReader::builder(exporter.clone()).build())
        .build();
    opentelemetry::global::set_meter_provider(provider.clone());

    let (executor_io, stage_io) = tokio::io::duplex(256 * 1024);
    let executor = Arc::new(Executor::new(&test_config(), FixtureSource).expect("executor builds"));
    let digest = fixture_digest();
    let stage = tokio::spawn(run_fake_stage(stage_io, digest));
    let hello = executor.hello(false, "runc");
    let executor_task = tokio::spawn(async move {
        run_connection(
            executor_io,
            hello,
            Arc::clone(&executor),
            "test-peer",
            Heartbeat::disabled(),
        )
        .await
    });
    stage.await.expect("stage task");
    executor_task
        .await
        .expect("executor task")
        .expect("connection ran to a clean shutdown");

    let p = flatten(&provider, &exporter);

    // Load: every phase of the one cold compile, and the outcome counter.
    for phase in ["fetch", "verify", "precompile", "total"] {
        assert_eq!(
            count(
                &p,
                "waddles_executor_load_seconds",
                &[("app_id", APP_ID), ("phase", phase), ("result", "ok")]
            ),
            1.0,
            "load phase {phase}"
        );
    }
    assert_eq!(
        count(&p, "waddles_executor_loads_total", &[("result", "ok")]),
        1.0
    );

    // Invoke: one ok, one deadline trap; instantiate + guest for BOTH (the
    // trapped guest ran, so its time is recorded too).
    assert_eq!(
        count(
            &p,
            "waddles_executor_invoke_seconds",
            &[
                ("app_id", APP_ID),
                ("export", "transform"),
                ("result", "ok")
            ]
        ),
        1.0
    );
    assert_eq!(
        count(
            &p,
            "waddles_executor_invoke_seconds",
            &[("app_id", APP_ID), ("result", "executor_deadline")]
        ),
        1.0
    );
    assert_eq!(
        count(
            &p,
            "waddles_executor_instantiate_seconds",
            &[("app_id", APP_ID)]
        ),
        2.0
    );
    assert_eq!(
        count(
            &p,
            "waddles_executor_guest_seconds",
            &[("app_id", APP_ID), ("export", "transform")]
        ),
        2.0
    );
    assert_eq!(
        count(
            &p,
            "waddles_executor_sandbox_trip_total",
            &[("app_id", APP_ID), ("limit", "timeout")]
        ),
        1.0
    );

    // Host calls: the guest's kv.set and kv.get really crossed the wire.
    for op in ["set", "get"] {
        assert_eq!(
            count(
                &p,
                "waddles_executor_host_call_seconds",
                &[("capability", "kv"), ("op", op), ("result", "ok")]
            ),
            1.0,
            "kv.{op} host call"
        );
    }

    // Gauges settle to explicit zeros: no invocation in flight, and the
    // registry was wiped on disconnect.
    assert_eq!(last(&p, "waddles_executor_invocations_active"), Some(0.0));
    assert_eq!(last(&p, "waddles_executor_bundles_loaded"), Some(0.0));

    // Nothing the guest or the stage controls became a label value.
    for point in &p {
        for (key, value) in &point.attrs {
            assert!(
                !value.contains("kv-roundtrip") && !value.contains("busy-loop"),
                "{} leaked guest-controlled text into {key}={value}",
                point.name
            );
        }
    }

    // Denominator: how many of the documented series this run exercised.
    let exported: std::collections::BTreeSet<&str> = p.iter().map(|x| x.name.as_str()).collect();
    let exercised = METRIC_NAMES
        .iter()
        .filter(|name| exported.contains(instrument_name(name)))
        .count();
    assert!(
        exercised >= 9,
        "only {exercised} of {} documented metrics were exercised end to end: {exported:?}",
        METRIC_NAMES.len()
    );
}
