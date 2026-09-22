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
        std::env::set_var("OTEL_SERVICE_NAME", "svc-ingest-test-otlp");
    }

    let (mut guard, registry) = svc_ingest::telemetry::init("svc-ingest-test-otlp-default");
    tracing::info!("telemetry initialized with an unreachable OTLP endpoint");
    let rendered = svc_ingest::telemetry::render_metrics(&registry).unwrap();
    assert!(rendered.is_empty());
    // Must return promptly rather than hanging on the dead endpoint.
    guard.shutdown();

    unsafe {
        std::env::remove_var("OTEL_EXPORTER_OTLP_ENDPOINT");
        std::env::remove_var("OTEL_EXPORTER_OTLP_PROTOCOL");
        std::env::remove_var("OTEL_SERVICE_NAME");
    }
}
