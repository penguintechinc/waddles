//! Integration tests for the JWT auth middleware gating
//! `/api/v1/openapi.json`, the Swagger UI, and the nested `/api/v1`
//! control-plane router -- 401/403 rejection paths plus the happy path,
//! exercised through the real axum `Router`.

use axum::body::Body;
use axum::http::header::AUTHORIZATION;
use axum::http::{Request, StatusCode};
use clap::Parser;
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

fn sign_token(claims: &Claims) -> String {
    jwt_common::sign(claims)
}

fn valid_claims(state: &AppState) -> Claims {
    let now = chrono::Utc::now().timestamp();
    Claims {
        sub: "user-123".into(),
        iss: state.config.cli.jwt_issuer.clone(),
        aud: state.config.cli.jwt_audience.clone(),
        iat: now,
        exp: now + 3600,
        scope: "streaming:read".into(),
        tenant: "tenant-abc".into(),
        teams: vec![],
        roles: vec![],
    }
}

#[tokio::test]
async fn openapi_full_spec_requires_auth() {
    let app = router(test_state());
    let response = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/openapi.json")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn openapi_full_spec_rejects_token_missing_tenant() {
    let state = test_state();
    let mut claims = valid_claims(&state);
    claims.tenant = String::new();
    let token = sign_token(&claims);

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
    assert_eq!(response.status(), StatusCode::FORBIDDEN);
}

#[tokio::test]
async fn openapi_full_spec_accepts_valid_token() {
    let state = test_state();
    let token = sign_token(&valid_claims(&state));

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
}

#[tokio::test]
async fn openapi_public_spec_needs_no_auth() {
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
}

#[tokio::test]
async fn nested_api_v1_placeholder_requires_auth() {
    let app = router(test_state());
    let response = app
        .oneshot(
            Request::builder()
                .uri("/api/v1")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn nested_api_v1_placeholder_returns_501_once_authenticated() {
    let state = test_state();
    let token = sign_token(&valid_claims(&state));

    let app = router(state);
    let response = app
        .oneshot(
            Request::builder()
                .uri("/api/v1")
                .header(AUTHORIZATION, format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::NOT_IMPLEMENTED);
}

#[tokio::test]
async fn malformed_authorization_header_is_401() {
    let app = router(test_state());
    let response = app
        .oneshot(
            Request::builder()
                .uri("/api/v1/openapi.json")
                .header(AUTHORIZATION, "Basic dXNlcjpwYXNz")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn health_is_reachable_without_auth_even_when_api_v1_is_gated() {
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
}
