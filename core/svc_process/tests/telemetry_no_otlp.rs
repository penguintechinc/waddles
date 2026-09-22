//! Integration test (own process) exercising `telemetry::init` with no OTLP
//! endpoint configured -- the "skip export entirely" branch.
//!
//! Kept out of `src/telemetry.rs`'s own `#[cfg(test)]` module because
//! `tracing_subscriber::registry().init()` installs a process-global
//! default subscriber and panics if called a second time in the same
//! process. Every file under `tests/` is compiled as its own binary, so
//! this file and `tests/telemetry_with_otlp.rs` each get exactly one call
//! to `telemetry::init`.

#[test]
fn init_without_otlp_endpoint_skips_export_and_returns_a_usable_registry() {
    // SAFETY: the only test in this binary/process that touches this var.
    unsafe { std::env::remove_var("OTEL_EXPORTER_OTLP_ENDPOINT") };

    let (mut guard, registry) = svc_process::telemetry::init("svc-process-test-no-otlp");
    tracing::info!("telemetry initialized without an OTLP endpoint");

    let rendered =
        svc_process::telemetry::render_metrics(&registry).expect("empty registry still encodes");
    assert!(rendered.is_empty(), "no metrics registered yet");

    // Shutdown with no tracer/meter provider configured must be a no-op,
    // never a panic or hang.
    guard.shutdown();
}
