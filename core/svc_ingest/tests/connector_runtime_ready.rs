//! `/readyz` with an installed connector-runtime status, through the real
//! axum `Router`. Own test binary (own process): `install` is process-global
//! and once-only, so no other test may race it.

use std::sync::Arc;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use clap::Parser;
use http_body_util::BodyExt;
use tower::ServiceExt;

use svc_ingest::config::{CliConfig, Config};
use svc_ingest::connector_runtime::ready::{install, installed, ExecutorStatus, RuntimeStatus};
use svc_ingest::http::{router, AppState};

struct Detached;
impl ExecutorStatus for Detached {
    fn attached(&self) -> bool {
        false
    }
}

fn state() -> AppState {
    let cli = CliConfig::try_parse_from(["svc-ingest"]).expect("defaults parse");
    let config = Config::from_cli(cli).expect("defaults require no secrets");
    AppState::new(config, prometheus::Registry::new())
}

async fn readyz() -> (StatusCode, serde_json::Value) {
    let response = router(state())
        .oneshot(
            Request::builder()
                .uri("/readyz")
                .body(Body::empty())
                .expect("req"),
        )
        .await
        .expect("response");
    let code = response.status();
    let body = response
        .into_body()
        .collect()
        .await
        .expect("body")
        .to_bytes();
    (code, serde_json::from_slice(&body).expect("json"))
}

#[tokio::test]
async fn an_installed_runtime_that_is_not_ready_degrades_readyz_with_per_platform_fields() {
    let (code, body) = readyz().await;
    assert_eq!(code, StatusCode::OK, "no connector installed yet");
    assert!(body.get("connectors").is_none());

    let status = Arc::new(RuntimeStatus::new(
        &["twitch", "youtube"],
        Arc::new(Detached),
    ));
    install(status.clone()).expect("first install");
    assert!(install(status).is_err(), "install is once-only");
    assert!(installed().is_some());

    let (code, body) = readyz().await;
    assert_eq!(code, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(body["status"], "degraded");
    let connectors = body["connectors"].as_array().expect("connectors");
    assert_eq!(connectors.len(), 2, "denominator: both platforms reported");
    for o in connectors {
        assert_eq!(o["enabled"], true);
        assert_eq!(o["executor_attached"], false);
        assert_eq!(o["pinned_ok"], false);
        assert_eq!(o["desired"], 0);
        assert_eq!(o["live"], 0);
        assert_eq!(o["discovery_failed"], false);
        assert_eq!(o["reason"], "executor_detached");
    }
}
