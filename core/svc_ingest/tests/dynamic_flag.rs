//! Flag-ON startup test for `waddles.ingest-dynamic-connections`: drives the
//! real `run_with_shutdown` with the flag turned ON through its alpha ENV
//! baseline (`FLAG_WADDLES_INGEST_DYNAMIC_CONNECTIONS=true`) and proves the
//! dynamic path is the one that runs -- and that a flag-ON deployment missing
//! its hub-api credentials plumbing **fails loudly** at startup instead of
//! silently falling back to the static `TWITCH_IRC_*` receiver.
//!
//! The flag-OFF side (the static path unchanged, nothing dynamic started) is
//! `tests/run.rs`, which runs the same startup with the flag unset.
//!
//! One test per file: `telemetry::init` may be called once per process, and
//! this test mutates process env (each `tests/*.rs` is its own binary).

use clap::Parser;

use svc_ingest::config::{CliConfig, Config};

fn free_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0")
        .expect("bind to ephemeral port")
        .local_addr()
        .expect("read local addr")
        .port()
}

#[tokio::test]
async fn flag_on_without_hub_credentials_fails_startup_loudly() {
    // SAFETY: single test in this process, before any await point.
    unsafe {
        std::env::remove_var("OTEL_EXPORTER_OTLP_ENDPOINT");
        std::env::remove_var("SERVICE_JWT_TOKEN_ENDPOINT");
        std::env::set_var("FLAG_WADDLES_INGEST_DYNAMIC_CONNECTIONS", "true");
    }

    let cli = CliConfig::try_parse_from([
        "svc-ingest",
        "--http-port",
        &free_port().to_string(),
        "--metrics-port",
        &free_port().to_string(),
        "--bind-addr",
        "127.0.0.1",
    ])
    .expect("valid CLI assembly");
    let config = Config::from_cli(cli).expect("defaults require no secrets");

    let err = svc_ingest::run_with_shutdown(config, async {}, async {})
        .await
        .expect_err("flag ON with no SERVICE_JWT_TOKEN_ENDPOINT must refuse to start");
    let msg = err.to_string();
    assert!(msg.contains("waddles.ingest-dynamic-connections"), "{msg}");
    assert!(msg.contains("SERVICE_JWT_TOKEN_ENDPOINT"), "{msg}");
    assert!(msg.contains("no silent fallback"), "{msg}");
}
