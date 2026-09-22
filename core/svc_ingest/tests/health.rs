//! Integration tests for `/health`, `/healthz`, and the secondary
//! `/metrics` router -- exercised through the real axum `Router`, not the
//! handler functions directly.

use axum::body::Body;
use axum::http::{Request, StatusCode};
use clap::Parser;
use http_body_util::BodyExt;
use tower::ServiceExt;

use svc_ingest::config::{CliConfig, Config};
use svc_ingest::http::{router, AppState};

fn test_state() -> AppState {
    let cli = CliConfig::try_parse_from(["svc-ingest"]).expect("defaults parse");
    let config = Config::from_cli(cli).expect("defaults require no secrets");
    AppState::new(config, prometheus::Registry::new())
}

#[tokio::test]
async fn health_returns_200_with_ok_status_and_dependency_snapshot() {
    let app = router(test_state());
    let response = app
        .oneshot(
            Request::builder()
                .uri("/health")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::OK);
    let body = response.into_body().collect().await.unwrap().to_bytes();
    let parsed: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(parsed["status"], "ok");
    let dependencies = parsed["dependencies"].as_array().unwrap();
    assert_eq!(dependencies.len(), 1);
    assert_eq!(dependencies[0]["name"], "hub_api");
}

#[tokio::test]
async fn healthz_returns_200_bare_ok() {
    let app = router(test_state());
    let response = app
        .oneshot(
            Request::builder()
                .uri("/healthz")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::OK);
    let body = response.into_body().collect().await.unwrap().to_bytes();
    assert_eq!(&body[..], b"ok");
}

#[tokio::test]
async fn metrics_router_serves_prometheus_text_exposition() {
    let app = svc_ingest::http::metrics_router(test_state());
    let response = app
        .oneshot(
            Request::builder()
                .uri("/metrics")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::OK);
    let body = response.into_body().collect().await.unwrap().to_bytes();
    let rendered = String::from_utf8(body.to_vec()).unwrap();
    // At least one series must be present even with zero requests served --
    // the `up` gauge is registered eagerly in `AppState::new`.
    assert!(rendered.contains("svc_ingest_up 1"));
}

#[tokio::test]
async fn main_router_records_request_metrics() {
    let state = test_state();
    let app = router(state.clone());
    let response = app
        .oneshot(
            Request::builder()
                .uri("/health")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);

    let metrics_app = svc_ingest::http::metrics_router(state);
    let metrics_response = metrics_app
        .oneshot(
            Request::builder()
                .uri("/metrics")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let body = metrics_response
        .into_body()
        .collect()
        .await
        .unwrap()
        .to_bytes();
    let rendered = String::from_utf8(body.to_vec()).unwrap();
    assert!(rendered.contains("svc_ingest_http_requests_total"));
    assert!(rendered.contains("path=\"/health\""));
    assert!(rendered.contains("svc_ingest_http_request_duration_seconds"));
}

#[tokio::test]
async fn unknown_route_on_main_router_is_404() {
    let app = router(test_state());
    let response = app
        .oneshot(
            Request::builder()
                .uri("/does-not-exist")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn intake_routes_are_not_yet_mounted() {
    // TODO(M5): connectors/intake -- blocked on M1 connectors + M2. This
    // test documents the current (honest) 404 state and should be updated
    // once the generic intake routes land, per spec S10.1.
    let app = router(test_state());
    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/intake/events")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::NOT_FOUND);
}
