//! Real-path e2e of the connector PUBLISH -> boot-PIN round trip.
//!
//! Driven by `hub_api/tests/test_connector_publish_e2e.py` (`make test-connector-publish`), which
//! starts a pinned SeaweedFS, publishes a real connector through
//! `services/connector_publish_service.py` with a throwaway test signing key, and then runs THIS
//! test (cargo, feature `e2e-main-tier`) once per scenario. Everything here is the production
//! path: the executor's own `BucketComponentSource` (SigV4 GET against SeaweedFS) +
//! `connector_boot::pin_configured` (sidecar signature, digest, wasmtime compile).
//!
//! Inputs (env, set by the driver; absent => FAIL, never a skip): `E2E_BUCKET_ENDPOINT`,
//! `E2E_BUCKET_NAME`, `E2E_BUCKET_ACCESS_KEY_ID`, `E2E_BUCKET_SECRET_ACCESS_KEY`,
//! `E2E_SIGNING_PUBLIC_KEYS` (JSON key_id->b64), `E2E_DISCORD_DIGEST`, `E2E_EXPECT` (`ok`|`fail`),
//! and for `fail` `E2E_EXPECT_ERR_CONTAINS` (a substring of the typed error).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use bundle_executor::bucket::BucketComponentSource;
use bundle_executor::config::CliConfig;
use bundle_executor::connector_boot::pin_configured;
use bundle_executor::invoke::Executor;
use clap::Parser;

fn env(name: &str) -> String {
    std::env::var(name)
        .ok()
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| panic!("{name} is not set: run via `make test-connector-publish`"))
}

#[tokio::test]
async fn published_connector_pins_or_fails_as_the_driver_expects() {
    let cfg = CliConfig::try_parse_from([
        "bundle-executor".to_string(),
        "--stage-host-api-addr".into(),
        "svc-action:8302".into(),
        "--bundle-signing-public-keys".into(),
        env("E2E_SIGNING_PUBLIC_KEYS"),
        "--bundle-bucket-endpoint".into(),
        env("E2E_BUCKET_ENDPOINT"),
        "--bundle-bucket-name".into(),
        env("E2E_BUCKET_NAME"),
        "--bundle-bucket-access-key-id".into(),
        env("E2E_BUCKET_ACCESS_KEY_ID"),
        "--bundle-bucket-secret-access-key".into(),
        env("E2E_BUCKET_SECRET_ACCESS_KEY"),
        "--connector-pin-digest-discord".into(),
        env("E2E_DISCORD_DIGEST"),
    ])
    .unwrap();
    let source = BucketComponentSource::from_cli(&cfg).unwrap();
    let exec = Executor::new(&cfg, source).unwrap();
    let result = pin_configured(&exec, &cfg).await;
    match env("E2E_EXPECT").as_str() {
        "ok" => {
            let pinned = result.unwrap_or_else(|e| panic!("pin must succeed: {e}"));
            assert_eq!(pinned, 1, "denominator: exactly one connector configured");
            assert_eq!(exec.pinned_connectors().await, 1);
        }
        "fail" => {
            let err = result.expect_err("pin must be refused").to_string();
            let want = env("E2E_EXPECT_ERR_CONTAINS");
            assert!(
                err.contains("discord"),
                "error must name the platform: {err}"
            );
            assert!(
                err.to_lowercase().contains(&want.to_lowercase()),
                "want {want:?} in: {err}"
            );
            assert_eq!(
                exec.pinned_connectors().await,
                0,
                "nothing may be pinned on failure"
            );
        }
        other => panic!("E2E_EXPECT must be ok|fail, got {other}"),
    }
}
