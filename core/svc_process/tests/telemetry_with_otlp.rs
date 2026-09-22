//! Integration test (own process) exercising `telemetry::init` with an OTLP
//! endpoint configured -- the exporter-construction branch. See
//! `tests/telemetry_no_otlp.rs` for why this lives in its own file/process
//! rather than `src/telemetry.rs`'s own `#[cfg(test)]` module.

#[tokio::test]
async fn init_with_otlp_endpoint_builds_exporters_and_shuts_down_cleanly() {
    // SAFETY: the only test in this binary/process that touches this var.
    // A syntactically valid, unreachable loopback endpoint -- the
    // grpc-tonic exporter connects lazily on first export, so `.build()`
    // and an empty-buffer `.shutdown()` never need a real collector to be
    // listening.
    unsafe { std::env::set_var("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4317") };

    let (mut guard, _registry) = svc_process::telemetry::init("svc-process-test-with-otlp");
    tracing::info!("telemetry initialized with an OTLP endpoint");

    guard.shutdown();

    unsafe { std::env::remove_var("OTEL_EXPORTER_OTLP_ENDPOINT") };
}
