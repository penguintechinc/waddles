//! Real-path test for `svc_process::media::build_media_wiring`: every missing dependency
//! leaves the `media` capability UNWIRED (loud log, never a stub), and a complete
//! configuration wires it with the flag gate defaulting OFF. Uses a real Valkey connection
//! (the job store's backing store); no provider, bucket or hub-api traffic is generated.
#![allow(clippy::unwrap_used, clippy::expect_used)]

#[path = "../../test_support/tests/docker_container.rs"]
mod docker_container;

use std::sync::Arc;
use std::time::Duration;

use clap::Parser;
use docker_container::start_container;
use svc_process::config::CliConfig;
use svc_process::media::{build_media_wiring, MediaWiring, DEFAULT_IMAGE_CONCURRENCY};

const KEY_ID: &str = "MEDIA_BUCKET_ACCESS_KEY_ID";
const KEY_SECRET: &str = "MEDIA_BUCKET_SECRET_ACCESS_KEY";

fn license_client() -> Arc<penguin_licensing::LicenseClient> {
    let cfg = penguin_licensing::LicenseConfig::new("svc-process-media-wiring-test").unwrap();
    penguin_licensing::LicenseClient::new(cfg).unwrap()
}

/// Walks every early-return of `build_media_wiring` and then the fully-wired success path.
/// One test fn on purpose: it mutates process-global env vars.
#[tokio::test]
async fn build_media_wiring_fails_closed_per_missing_dependency_and_wires_when_complete() {
    let valkey = start_container(
        "valkey/valkey:8-alpine",
        6379,
        &[],
        &["valkey-cli", "ping"],
        Duration::from_secs(120),
    )
    .await;
    let conn_for = || async {
        redis::Client::open(format!("redis://{}:{}/", valkey.host(), valkey.port()))
            .unwrap()
            .get_multiplexed_async_connection()
            .await
            .unwrap()
    };
    let lc = license_client();

    // SAFETY: this is the only test in this binary, so nothing else reads the env concurrently.
    unsafe {
        std::env::remove_var(KEY_ID);
        std::env::remove_var(KEY_SECRET);
    }

    // 1. No machine-JWT endpoint: hub-api tier + BYOK cannot be authenticated.
    let cli = CliConfig::parse_from(["svc-process"]);
    assert!(build_media_wiring(&cli, &lc, conn_for().await).is_none());

    // 2. JWT endpoint set but bucket credentials absent.
    let cli = CliConfig::parse_from([
        "svc-process",
        "--service-jwt-token-endpoint",
        "http://127.0.0.1:1",
    ]);
    assert!(build_media_wiring(&cli, &lc, conn_for().await).is_none());

    // 3. Credentials present but hub-api gRPC endpoint unset.
    unsafe {
        std::env::set_var(KEY_ID, "test-access-key");
        std::env::set_var(KEY_SECRET, "test-secret-key");
    }
    assert!(build_media_wiring(&cli, &lc, conn_for().await).is_none());

    // 4. hub-api https endpoint whose CA bundle cannot be read: channel build fails closed.
    let cli_bad_ca = CliConfig::parse_from([
        "svc-process",
        "--service-jwt-token-endpoint",
        "http://127.0.0.1:1",
        "--hub-api-grpc-endpoint",
        "https://hub.invalid:50204",
        "--hub-api-grpc-ca-file",
        "/nonexistent/svc-process-test-ca.pem",
    ]);
    assert!(build_media_wiring(&cli_bad_ca, &lc, conn_for().await).is_none());

    // 5. Fully configured: wired, flag OFF by default, image concurrency at the default cap.
    let cli_ok = CliConfig::parse_from([
        "svc-process",
        "--service-jwt-token-endpoint",
        "http://127.0.0.1:1",
        "--hub-api-grpc-endpoint",
        "http://127.0.0.1:1",
    ]);
    let wiring: MediaWiring = build_media_wiring(&cli_ok, &lc, conn_for().await)
        .expect("a complete configuration wires the media capability");
    assert_eq!(
        wiring.concurrency.available_permits(),
        DEFAULT_IMAGE_CONCURRENCY
    );
    assert!(
        !wiring.flag.enabled().await,
        "waddles.harness-media-generation must default OFF"
    );

    unsafe {
        std::env::remove_var(KEY_ID);
        std::env::remove_var(KEY_SECRET);
    }
}
