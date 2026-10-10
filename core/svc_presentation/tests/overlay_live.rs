//! Integration tests for P3/P4's overlay routes mounted on the real
//! `svc_presentation::http::router` -- proves the `overlay_auth` guard
//! wiring (view guard on `/live`/`/live/ws`, push guard on `/push`) through
//! the actual merged router, not just the guard functions in isolation
//! (already covered by `overlay_auth`'s and `crate::overlay::router`'s own
//! test suites).
//!
//! The websocket route's full connect-and-stream behavior is covered at
//! the unit level in `src/http/overlay.rs` (a real bound TCP listener,
//! `tokio-tungstenite` client) -- `tower::ServiceExt::oneshot` used here
//! can't drive a genuine HTTP Upgrade, only its non-upgraded guard-reject
//! paths.

use std::time::Duration;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use clap::Parser;
use http_body_util::BodyExt;
use overlay_auth::{generate_view_token, hash_token};
use sea_orm::{DatabaseBackend, MockDatabase};
use tower::ServiceExt;

use svc_presentation::config::{CliConfig, Config, Secret};
use svc_presentation::db::entities::overlay_view_credential::Model as ViewCredentialModel;
use svc_presentation::http::{router, AppState};

fn state_with_view_credential(community_id: i64, token: &str) -> AppState {
    let cli = CliConfig::try_parse_from(["svc-presentation"]).expect("defaults parse");
    let config = Config {
        cli,
        db_password: Secret::new("db-pass"),
        cache_password: None,
        image_bucket_access_key_id: None,
        image_bucket_secret_access_key: None,
    };
    let db = MockDatabase::new(DatabaseBackend::Postgres)
        .append_query_results([vec![ViewCredentialModel {
            id: 1,
            community_id,
            key_hash: hash_token(token),
            previous_key_hash: None,
            is_active: true,
            rotated_at: None,
        }]])
        .into_connection();
    AppState::new(config, prometheus::Registry::new(), db)
}

fn state_with_no_queries_expected() -> AppState {
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
async fn live_sse_route_rejects_the_wrong_key_with_403() {
    let token = generate_view_token();
    let state = state_with_view_credential(42, &token);
    let app = router(state);
    let response = app
        .oneshot(
            Request::builder()
                .uri("/overlay/42/media/live?key=wrong-token")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::FORBIDDEN);
}

#[tokio::test]
async fn live_sse_route_rejects_a_missing_key_query_param_with_400() {
    // axum's own `Query` extractor rejects this before `overlay_auth`'s
    // guard logic runs at all -- no DB query is ever issued, matching
    // `overlay_auth::view`'s own documented precedent for this exact case.
    let app = router(state_with_no_queries_expected());
    let response = app
        .oneshot(
            Request::builder()
                .uri("/overlay/42/media/live")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn live_sse_route_with_the_correct_key_streams_the_connected_frame_first() {
    let token = generate_view_token();
    let state = state_with_view_credential(42, &token);
    let app = router(state);
    let response = app
        .oneshot(
            Request::builder()
                .uri(format!("/overlay/42/media/live?key={token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(
        response.headers().get("content-type").unwrap(),
        "text/event-stream"
    );

    let mut body = response.into_body();
    let frame = tokio::time::timeout(Duration::from_secs(2), body.frame())
        .await
        .expect("first SSE frame arrives before the timeout")
        .expect("a frame is present")
        .expect("no body-level error");
    let bytes = frame.into_data().expect("a data frame, not trailers");
    let text = String::from_utf8(bytes.to_vec()).expect("utf8 SSE payload");
    assert!(text.contains("\"community\":\"42\""));
    assert!(text.contains("\"surface\":\"media\""));
}

#[tokio::test]
async fn push_route_rejects_a_missing_bearer_header_with_401() {
    let app = router(state_with_no_queries_expected());
    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/overlay/42/media/push")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn unrelated_unmatched_path_is_still_404_with_overlay_routes_mounted() {
    // Regression guard for the exact class of bug `overlay::router`'s
    // module doc warns about: mounting a guard must never turn an
    // unrelated 404 into something else.
    let app = router(state_with_no_queries_expected());
    let response = app
        .oneshot(
            Request::builder()
                .uri("/totally-unrelated-path")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::NOT_FOUND);
}
