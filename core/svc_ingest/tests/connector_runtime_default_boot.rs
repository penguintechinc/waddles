//! The connector runtime boot is strictly opt-in: with `CONNECTOR_RUNTIME_ENABLED`
//! unset and no `CONNECTOR_<P>_ENABLED` baseline, `run_with_shutdown` starts
//! NOTHING new (no runtime, no `connector_runtime_*` series, no connector in
//! `/readyz`, webhook ingress stays 503). The connector-ON failure is in
//! `connector_runtime_boot_fail.rs` (telemetry init is once per process).
//!
//! One test fn: it mutates process env, so it must be the only test in this binary.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clap::Parser;
use svc_ingest::config::{CliConfig, Config};

fn free_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
        .port()
}

fn config(http: u16, metrics: u16) -> Config {
    let cli = CliConfig::try_parse_from([
        "svc-ingest",
        "--http-port",
        &http.to_string(),
        "--metrics-port",
        &metrics.to_string(),
        "--bind-addr",
        "127.0.0.1",
    ])
    .unwrap();
    Config::from_cli(cli).unwrap()
}

#[tokio::test]
async fn default_boot_starts_nothing_new() {
    for name in [
        "CONNECTOR_TWITCH_ENABLED",
        "CONNECTOR_DISCORD_ENABLED",
        "CONNECTOR_YOUTUBE_ENABLED",
        "CONNECTOR_RUNTIME_ENABLED",
        "CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
    ] {
        // SAFETY: the only test in this process, before any other thread reads env.
        unsafe { std::env::remove_var(name) };
    }
    // The data plane refuses to start without a tenant-DEK source (#1252); the hard-gated
    // dev/alpha KEK stands in for hub-api's key broker (read by `Config::from_cli`).
    // SAFETY: as above -- single test, before any other thread reads env.
    unsafe {
        std::env::set_var("WADDLES_ENV", "local");
        std::env::set_var("INGEST_DEV_KEK", "k".repeat(48));
    }

    // ---- default: serve, probe, stop ----
    let (http, metrics) = (free_port(), free_port());
    let (stop_tx, stop_rx) = tokio::sync::oneshot::channel::<()>();
    let (mstop_tx, mstop_rx) = tokio::sync::oneshot::channel::<()>();
    let server = tokio::spawn(svc_ingest::run_with_shutdown(
        config(http, metrics),
        async move {
            let _ = stop_rx.await;
        },
        async move {
            let _ = mstop_rx.await;
        },
    ));
    let client = reqwest::Client::new();
    let mut body = None;
    for _ in 0..100 {
        if let Ok(r) = client
            .get(format!("http://127.0.0.1:{http}/readyz"))
            .send()
            .await
        {
            body = Some(r.text().await.unwrap());
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    let readyz: serde_json::Value = serde_json::from_str(&body.expect("service came up")).unwrap();
    assert!(
        readyz
            .get("connectors")
            .is_none_or(|o| o.as_array().is_some_and(Vec::is_empty)),
        "no connector platform reported by default: {readyz}"
    );
    assert_eq!(readyz["connector"].as_array().map(Vec::len), Some(0));
    assert!(
        svc_ingest::connector_runtime::ready::installed().is_none(),
        "no runtime status installed by default"
    );
    let metrics_text = client
        .get(format!("http://127.0.0.1:{metrics}/metrics"))
        .send()
        .await
        .unwrap()
        .text()
        .await
        .unwrap();
    assert!(!metrics_text.is_empty(), "the metrics endpoint answered");
    for series in [
        "connector_runtime_",
        "connector_host_call",
        "svc_ingest_connector_frames_total",
    ] {
        assert!(
            !metrics_text.contains(series),
            "default boot must not register {series}"
        );
    }
    let hook = client
        .post(format!("http://127.0.0.1:{http}/intake/webhook/abcdefgh"))
        .body("{}")
        .send()
        .await
        .unwrap();
    assert_eq!(
        hook.status().as_u16(),
        503,
        "webhook ingress stays fail-loud 503"
    );
    stop_tx.send(()).unwrap();
    mstop_tx.send(()).unwrap();
    server.await.unwrap().expect("clean stop");
}
