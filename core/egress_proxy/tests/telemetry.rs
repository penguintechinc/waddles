//! Integration test (own process) exercising `telemetry::init` with no
//! collector present -- the required "OTLP export skipped, everything
//! else keeps working" branch. Kept out of `src/telemetry.rs`'s own
//! `#[cfg(test)]` module because `penguin_logging::init` installs a
//! process-global default `tracing` subscriber and panics if called a
//! second time in the same process; every file under `tests/` is its own
//! binary, so this gets exactly one call to `telemetry::init`. Mirrors
//! `core/svc_ingest/tests/telemetry.rs` verbatim.

#[test]
fn init_without_otlp_endpoint_uses_tracing_only() {
    // SAFETY: the only test in this binary/process that touches this var.
    unsafe { std::env::remove_var("OTEL_EXPORTER_OTLP_ENDPOINT") };

    let (mut guard, registry) = egress_proxy::telemetry::init("egress-proxy-test-no-otlp");
    tracing::info!("telemetry initialized without an OTLP endpoint");

    // `penguin_logging::init` always attaches a Prometheus reader to the
    // OTel meter provider it builds -- that reader unconditionally emits
    // one `target_info` resource-metadata gauge, so the registry is never
    // truly empty even with zero application metrics recorded.
    let rendered =
        egress_proxy::telemetry::render_metrics(&registry).expect("registry must encode");
    assert!(
        rendered.contains("target_info"),
        "expected the OTel resource target_info gauge, got: {rendered:?}"
    );
    assert!(
        rendered.contains("service_name=\"egress-proxy-test-no-otlp\""),
        "target_info must carry the service name passed to init, got: {rendered:?}"
    );

    // Shutdown with no tracer/logger provider configured must be a no-op,
    // never a panic or hang -- proving a dead/absent collector never
    // breaks the service, at startup or at shutdown.
    guard.shutdown();
}
