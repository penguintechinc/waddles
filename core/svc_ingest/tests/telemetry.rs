//! Integration test (own process) exercising `telemetry::init` with no OTLP
//! endpoint configured -- the "skip export entirely" branch. Kept out of
//! `src/telemetry.rs`'s own `#[cfg(test)]` module because
//! `tracing_subscriber::registry().init()` (via `penguin_logging::init`)
//! installs a process-global default subscriber and panics if called a
//! second time in the same process. Every file under `tests/` is compiled
//! as its own binary, so this file and `tests/telemetry_otlp.rs` each get
//! exactly one call to `telemetry::init`.

#[test]
fn init_without_otlp_endpoint_uses_tracing_only() {
    // SAFETY: the only test in this binary/process that touches this var.
    unsafe { std::env::remove_var("OTEL_EXPORTER_OTLP_ENDPOINT") };

    let (mut guard, registry) = svc_ingest::telemetry::init("svc-ingest-test-no-otlp");
    tracing::info!("telemetry initialized without an OTLP endpoint");

    // `penguin_logging::init` always attaches a Prometheus reader to the
    // OTel meter provider it builds -- that reader unconditionally emits
    // one `target_info` resource-metadata gauge, so the registry is never
    // truly empty even with zero application metrics recorded. This
    // replaces the pre-`penguin-logging` assertion (a bare
    // `prometheus::Registry::new()` really was empty); asserting
    // `target_info` carries this service's name is a stronger check of the
    // same underlying property this test always cared about -- telemetry
    // initialized without an OTLP endpoint still produces a usable,
    // correctly-labeled `/metrics` surface.
    let rendered = svc_ingest::telemetry::render_metrics(&registry).expect("registry must encode");
    assert!(
        rendered.contains("target_info"),
        "expected the OTel resource target_info gauge, got: {rendered:?}"
    );
    assert!(
        rendered.contains("service_name=\"svc-ingest-test-no-otlp\""),
        "target_info must carry the service name passed to init, got: {rendered:?}"
    );

    // Shutdown with no tracer/logger provider configured must be a no-op,
    // never a panic or hang.
    guard.shutdown();
}
