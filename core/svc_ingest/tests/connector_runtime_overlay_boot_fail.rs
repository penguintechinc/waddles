//! An overlay ON with a missing prerequisite is a loud, named startup error --
//! never a silent idle service (spec 5.3). One test fn: it mutates process env.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clap::Parser;
use svc_ingest::config::{CliConfig, Config};

fn config() -> Config {
    let cli = CliConfig::try_parse_from(["svc-ingest", "--bind-addr", "127.0.0.1"]).unwrap();
    Config::from_cli(cli).unwrap()
}

#[tokio::test]
async fn overlay_on_without_its_prerequisites_does_not_start() {
    // SAFETY: the only test in this process, before any other thread reads env.
    unsafe {
        std::env::remove_var("OTEL_EXPORTER_OTLP_ENDPOINT");
        std::env::set_var("OVERLAY_TWITCH_ENABLED", "true");
    }
    let err = svc_ingest::run_with_shutdown(config(), async {}, async {})
        .await
        .expect_err("overlay ON without its prerequisites must not start");
    let msg = err.to_string();
    assert!(
        msg.contains("INGEST_DYNAMIC_CONNECTIONS"),
        "the error names the missing item: {msg}"
    );
    assert!(svc_ingest::connector_runtime::ready::installed().is_none());
}
