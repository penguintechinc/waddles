//! Integration test for `svc_presentation::run_with_shutdown` -- exercises
//! the real bind/serve/telemetry-init/DB-connect wiring end-to-end (the
//! part `run()` can't otherwise be tested through, since `run()` itself
//! reads process argv via `clap::Parser::parse()` and installs real OS
//! signal handlers). Binds the HTTP and metrics listeners to explicit
//! free/ephemeral ports and passes already-resolved shutdown futures so
//! the server starts, logs, connects to a throwaway sqlite DB, and stops
//! immediately instead of blocking forever. Same pattern as
//! `core/svc_streaming/tests/run.rs`.
//!
//! No env-var lock is needed here: this file contains exactly one test, so
//! there is no cross-test env-var race to serialize against within this
//! process (each `tests/*.rs` file is its own binary/process).

use clap::Parser;

use svc_presentation::config::{CliConfig, Config};

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
async fn run_with_shutdown_binds_connects_serves_and_stops_cleanly() {
    let db_path = std::env::temp_dir().join(format!(
        "svc-presentation-run-with-shutdown-test-{}.sqlite",
        std::process::id()
    ));

    // SAFETY: single test in this process, before any await point.
    unsafe {
        std::env::remove_var("OTEL_EXPORTER_OTLP_ENDPOINT");
        std::env::set_var("DB_PASSWORD", "test-db-pass");
        std::env::set_var("DB_TYPE", "sqlite");
    }

    let http_port = free_port();
    let metrics_port = free_port();

    let cli = CliConfig::try_parse_from([
        "svc-presentation",
        "--dev-mode",
        "--http-port",
        &http_port.to_string(),
        "--metrics-port",
        &metrics_port.to_string(),
        "--bind-addr",
        "127.0.0.1",
        "--db-name",
        &db_path.to_string_lossy(),
    ])
    .expect("valid CLI assembly");
    let config = Config::from_cli(cli).expect("required secrets are set");

    let result = svc_presentation::run_with_shutdown(config, async {}, async {}).await;
    assert!(
        result.is_ok(),
        "run_with_shutdown should exit cleanly: {result:?}"
    );

    unsafe {
        std::env::remove_var("DB_PASSWORD");
        std::env::remove_var("DB_TYPE");
    }
    std::fs::remove_file(&db_path).ok();
}
