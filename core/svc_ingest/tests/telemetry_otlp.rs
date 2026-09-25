//! Integration test (own process) exercising `telemetry::init` with an OTLP
//! endpoint configured -- the exporter-construction branch. See
//! `tests/telemetry.rs` for why this lives in its own file/process rather
//! than `src/telemetry.rs`'s own `#[cfg(test)]` module.

#[tokio::test]
async fn init_with_unreachable_otlp_endpoint_does_not_crash() {
    // SAFETY: the only test in this binary/process that touches this var.
    // A syntactically valid, unreachable loopback endpoint -- the
    // grpc-tonic exporter connects lazily on first export, so `.build()`
    // and an empty-buffer `.shutdown()` never need a real collector to be
    // listening.
    unsafe {
        std::env::set_var("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1");
        std::env::set_var("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc");
    }

    let (mut guard, _registry) = svc_ingest::telemetry::init("svc-ingest-test-otlp-default");
    tracing::info!("telemetry initialized with an unreachable OTLP endpoint");

    // Must return promptly rather than hanging on the dead endpoint.
    guard.shutdown();

    unsafe {
        std::env::remove_var("OTEL_EXPORTER_OTLP_ENDPOINT");
        std::env::remove_var("OTEL_EXPORTER_OTLP_PROTOCOL");
    }
}
