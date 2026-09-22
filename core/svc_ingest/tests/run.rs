//! Integration test for `svc_ingest::run_with_shutdown` -- exercises the
//! real bind/serve/telemetry-init wiring end-to-end (the part `run()`
//! can't otherwise be tested through, since `run()` itself reads process
//! argv via `clap::Parser::parse()` and installs real OS signal handlers).
//! Binds both listeners (HTTP, metrics) to an explicit free/ephemeral port
//! and passes already-resolved shutdown futures so the server starts,
//! logs, and stops immediately instead of blocking forever.
//!
//! No env-var lock is needed here: this file contains exactly one test, so
//! there is no cross-test env-var race to serialize against within this
//! process (each `tests/*.rs` file is its own binary/process).

use clap::Parser;

use svc_ingest::config::{CliConfig, Config};

/// Binds a std `TcpListener` to an OS-assigned ephemeral port, reads it
/// back, and immediately releases the socket so `run_with_shutdown` can
/// bind it again moments later.
fn free_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0")
        .expect("bind to ephemeral port")
        .local_addr()
        .expect("read local addr")
        .port()
}

#[tokio::test]
async fn run_with_shutdown_binds_serves_and_stops_cleanly() {
    // SAFETY: single test in this process, before any await point.
    unsafe {
        std::env::remove_var("OTEL_EXPORTER_OTLP_ENDPOINT");
    }

    let http_port = free_port();
    let metrics_port = free_port();

    let cli = CliConfig::try_parse_from([
        "svc-ingest",
        "--http-port",
        &http_port.to_string(),
        "--metrics-port",
        &metrics_port.to_string(),
        "--bind-addr",
        "127.0.0.1",
    ])
    .expect("valid CLI assembly");
    let config = Config::from_cli(cli).expect("defaults require no secrets");

    let result = svc_ingest::run_with_shutdown(config, async {}, async {}).await;
    assert!(
        result.is_ok(),
        "run_with_shutdown should exit cleanly: {result:?}"
    );
}
