//! Integration tests for `/health`, `/readyz`, and the secondary
//! `/metrics` router -- exercised through the real axum `Router`, not the
//! handler functions directly.

use axum::body::Body;
use axum::http::{Request, StatusCode};
use clap::Parser;
use http_body_util::BodyExt;
use sea_orm::{DatabaseBackend, MockDatabase};
use tower::ServiceExt;

use svc_presentation::config::{CliConfig, Config, Secret};
use svc_presentation::http::{router, AppState};

fn test_state() -> AppState {
    let cli = CliConfig::try_parse_from(["svc-presentation"]).expect("defaults parse");
    let config = Config {
        cli,
        db_password: Secret::new("db-pass"),
        cache_password: None,
        image_bucket_access_key_id: None,
        image_bucket_secret_access_key: None,
    };
    let db = MockDatabase::new(DatabaseBackend::Postgres).into_connection();
    AppState::new(config, prometheus::Registry::new(), db)
}

#[tokio::test]
async fn health_returns_200_with_ok_status() {
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
}

#[tokio::test]
async fn readyz_returns_200_and_reports_dependency_snapshot() {
    let app = router(test_state());
    let response = app
        .oneshot(
            Request::builder()
                .uri("/readyz")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::OK);
    let body = response.into_body().collect().await.unwrap().to_bytes();
    let parsed: serde_json::Value = serde_json::from_slice(&body).unwrap();
    let dependencies = parsed["dependencies"].as_array().unwrap();
    assert_eq!(dependencies.len(), 3);
    let names: Vec<&str> = dependencies
        .iter()
        .map(|d| d["name"].as_str().unwrap())
        .collect();
    assert_eq!(names, vec!["database", "cache", "push_jwks"]);
}

#[tokio::test]
async fn metrics_router_serves_prometheus_text_exposition() {
    let app = svc_presentation::http::metrics_router(test_state());
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
    assert!(rendered.contains("svc_presentation_up 1"));
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

    let metrics_app = svc_presentation::http::metrics_router(state);
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
    assert!(rendered.contains("svc_presentation_http_requests_total"));
    assert!(rendered.contains("path=\"/health\""));
    assert!(rendered.contains("svc_presentation_http_request_duration_seconds"));
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

/// The plain full-page surface route (`GET /overlay/{community}/{surface}`)
/// is mounted behind the VIEW guard like `/live`: without a `?key=` the guard
/// answers 400 (never a 404 -- the route exists -- and never the page).
#[tokio::test]
async fn overlay_page_route_is_mounted_behind_the_view_guard() {
    let app = router(test_state());
    let response = app
        .oneshot(
            Request::builder()
                .uri("/overlay/42/media")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
}
