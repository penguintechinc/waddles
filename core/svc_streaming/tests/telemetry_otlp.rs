//! Integration test for `telemetry::init` with an OTLP endpoint configured
//! -- a separate process/file from `tests/telemetry.rs` because `init`
//! installs a process-global `tracing` subscriber exactly once.
//!
//! The endpoint (`127.0.0.1:1`, a port nothing listens on) is intentionally
//! unreachable: building a gRPC/HTTP exporter is lazy (no connection
//! attempt at construction time), so this exercises the
//! `Some(endpoint)` branch of `telemetry::init` -- tracer + meter provider
//! construction, the OTel `tracing_opentelemetry` layer wiring, and
//! `TelemetryGuard::shutdown` -- without requiring a live collector. A
//! dead/unreachable exporter must never crash the app; this test is the
//! proof.

use std::sync::Mutex;

static ENV_LOCK: Mutex<()> = Mutex::new(());

#[tokio::test]
async fn init_with_unreachable_otlp_endpoint_does_not_crash() {
    let _guard = ENV_LOCK.lock().unwrap();
    // SAFETY: serialized by ENV_LOCK; this is the only test in this binary
    // that touches OTEL_* env vars before calling `init`.
    unsafe {
        std::env::set_var("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1");
        std::env::set_var("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc");
        std::env::set_var("OTEL_SERVICE_NAME", "svc-streaming-test-otlp");
    }

    let (mut guard, registry) = svc_streaming::telemetry::init("svc-streaming-test-otlp-default");
    tracing::info!("telemetry initialized with an unreachable OTLP endpoint");
    let rendered = svc_streaming::telemetry::render_metrics(&registry).unwrap();
    assert!(rendered.is_empty());

    // A dead collector must never slow or break the data plane: hammer the
    // hot-path instruments and span creation against the unreachable
    // endpoint. Exporters run on their own background threads behind bounded
    // queues (excess is dropped, never blocked on), so the burst finishes at
    // in-process speed and no recording call panics or returns an error.
    let stream_metrics = svc_streaming::telemetry::stream::StreamMetrics::shared();
    let burst = 20_000u64;
    let started = std::time::Instant::now();
    for i in 0..burst {
        stream_metrics.record_ingest_chunk(
            svc_streaming::ingest::IngestKind::Rtmp,
            std::time::Duration::from_micros(i % 500),
            1316,
        );
        stream_metrics.record_stage(
            svc_streaming::telemetry::stream::Stage::EngineStart,
            std::time::Duration::from_micros(i),
        );
        let span = tracing::info_span!("dead_exporter_burst", i);
        let _entered = span.enter();
    }
    let elapsed = started.elapsed();
    println!(
        "telemetry: {burst} instrument + span iterations against a dead OTLP endpoint took {elapsed:?}"
    );
    assert!(
        elapsed < std::time::Duration::from_secs(8),
        "a dead OTLP exporter blocked the recording path: {burst} iterations took {elapsed:?}"
    );

    // Must return promptly rather than hanging on the dead endpoint.
    let shutdown_started = std::time::Instant::now();
    guard.shutdown();
    // Tracer flush is capped at 2s and the SDK bounds the meter flush at 5s;
    // anything near the old unbounded behavior fails this.
    let shutdown_elapsed = shutdown_started.elapsed();
    println!("telemetry: shutdown with a dead OTLP endpoint took {shutdown_elapsed:?}");
    assert!(
        shutdown_elapsed < std::time::Duration::from_secs(9),
        "shutdown hung on the dead endpoint: {shutdown_elapsed:?}"
    );

    unsafe {
        std::env::remove_var("OTEL_EXPORTER_OTLP_ENDPOINT");
        std::env::remove_var("OTEL_EXPORTER_OTLP_PROTOCOL");
        std::env::remove_var("OTEL_SERVICE_NAME");
    }
}
