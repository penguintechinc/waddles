//! Integration tests for `telemetry::init` -- run as a separate process
//! (standard for a `tests/*.rs` file) since it installs a process-global
//! `tracing` subscriber exactly once, which would conflict with the
//! `--lib` unit test binary's own test runs if colocated there.

use std::sync::Mutex;

// std::env is process-global; serialize env-mutating tests within this
// binary (there's only one process per `tests/*.rs` file, but
// `#[tokio::test]` still runs tests concurrently within it).
static ENV_LOCK: Mutex<()> = Mutex::new(());

#[tokio::test]
async fn init_without_otlp_endpoint_uses_tracing_only() {
    let _guard = ENV_LOCK.lock().unwrap();
    // SAFETY: serialized by ENV_LOCK; this is the only test in this binary
    // that touches OTEL_* env vars before calling `init` (a process-global,
    // one-time operation).
    unsafe { std::env::remove_var("OTEL_EXPORTER_OTLP_ENDPOINT") };

    let (mut guard, registry) = svc_ingest::telemetry::init("svc-ingest-test-no-otlp");
    tracing::info!("telemetry initialized without an OTLP endpoint");
    let rendered = svc_ingest::telemetry::render_metrics(&registry).unwrap();
    assert!(rendered.is_empty());
    guard.shutdown();
}
