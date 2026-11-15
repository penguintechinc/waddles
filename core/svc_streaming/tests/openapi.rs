//! Integration tests for the two-document OpenAPI split, exercised through
//! the real HTTP endpoints (not just the generated `OpenApi` struct
//! directly) -- the public doc must expose only `/health`, the full doc
//! (behind auth) documents everything.

use axum::body::Body;
use axum::http::header::AUTHORIZATION;
use axum::http::{Request, StatusCode};
use clap::Parser;
use http_body_util::BodyExt;
use tower::ServiceExt;

use svc_streaming::config::{CliConfig, Config, Secret};
use svc_streaming::http::auth::Claims;
use svc_streaming::http::{router, AppState};

mod jwt_common;

fn test_state() -> AppState {
    let cli = CliConfig::try_parse_from(["svc-streaming"]).expect("defaults parse");
    let config = Config {
        cli,
        db_password: Secret::new("db-pass"),
        cache_password: None,
        service_api_key: Secret::new("service-key"),
        jwt_keys: jwt_common::keys(),
    };
    AppState::new(config, prometheus::Registry::new())
}

fn signed_token(state: &AppState) -> String {
    let now = chrono::Utc::now().timestamp();
    let claims = Claims {
        sub: "user-123".into(),
        iss: state.config.cli.jwt_issuer.clone(),
        aud: state.config.cli.jwt_audience.clone(),
        iat: now,
        exp: now + 3600,
        scope: "streaming:read".into(),
        tenant: "tenant-abc".into(),
        teams: vec![],
        roles: vec![],
    };
    jwt_common::sign(&claims)
}

#[tokio::test]
async fn public_openapi_doc_lists_only_health() {
    let app = router(test_state());
    let response = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/openapi/public.json")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);

    let body = response.into_body().collect().await.unwrap().to_bytes();
    let parsed: serde_json::Value = serde_json::from_slice(&body).unwrap();
    let paths = parsed["paths"].as_object().unwrap();
    assert_eq!(paths.keys().collect::<Vec<_>>(), vec!["/health"]);
}

#[tokio::test]
async fn full_openapi_doc_lists_health_and_readyz() {
    let state = test_state();
    let token = signed_token(&state);

    let app = router(state);
    let response = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/openapi.json")
                .header(AUTHORIZATION, format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);

    let body = response.into_body().collect().await.unwrap().to_bytes();
    let parsed: serde_json::Value = serde_json::from_slice(&body).unwrap();
    let mut paths: Vec<&String> = parsed["paths"].as_object().unwrap().keys().collect();
    paths.sort();
    assert_eq!(paths, vec!["/health", "/readyz"]);
}

#[tokio::test]
async fn swagger_ui_requires_auth() {
    let app = router(test_state());
    let response = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/docs/")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn swagger_ui_serves_once_authenticated() {
    let state = test_state();
    let token = signed_token(&state);

    let app = router(state);
    let response = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/docs/")
                .header(AUTHORIZATION, format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
}
