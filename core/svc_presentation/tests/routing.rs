//! Route-specificity regression tests for the merged `http::router`.
//!
//! `POST /overlay/{community}/image/push` (P6's upload route) and
//! `POST /overlay/{community}/caption/push` (the caption ingest route)
//! each use a literal path segment where the generic
//! `/overlay/{community}/{surface}/...` routes use a `{surface}` capture.
//! These tests pin that a literal-segment route never shadows the generic
//! VIEW routes (`.../live`, `.../live/ws`) or the generic PUSH route for a
//! *different* surface -- a regression a single-surface test can't see,
//! because it only fails on the path shape the shadowing route doesn't own
//! -- and that the legacy caption viewer URLs (`/overlay/captions/{key}`,
//! `/ws/captions/{community_id}`) collide with nothing.
//!
//! The one routing bug found in P6 (PR #696) was not shadowing at all: the
//! literal-segment push route was *reachable*, but the PUSH guard demanded a
//! `{surface}` path parameter the route doesn't have and answered every
//! request 400. `image_push_is_still_push_guarded` pins the fix; the signed
//! end-to-end proof is `tests/push_guard.rs`.

use axum::body::Body;
use axum::http::{Request, StatusCode};
use clap::Parser;
use http_body_util::BodyExt;
use overlay_auth::{generate_view_token, hash_token};
use sea_orm::{DatabaseBackend, MockDatabase};
use std::time::Duration;
use tower::ServiceExt;

use svc_presentation::config::{CliConfig, Config, Secret};
use svc_presentation::db::entities::overlay_view_credential::Model as ViewCredentialModel;
use svc_presentation::http::{router, AppState};

fn config() -> Config {
    let cli = CliConfig::try_parse_from(["svc-presentation"]).expect("defaults parse");
    Config {
        cli,
        db_password: Secret::new("db-pass"),
        cache_password: None,
        image_bucket_access_key_id: None,
        image_bucket_secret_access_key: None,
    }
}

/// State whose mock DB answers exactly one VIEW-credential lookup for
/// `community_id`/`token`.
fn state_with_view_credential(community_id: i64, token: &str) -> AppState {
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
    AppState::new(config(), prometheus::Registry::new(), db)
}

fn state_with_no_queries_expected() -> AppState {
    let db = MockDatabase::new(DatabaseBackend::Postgres).into_connection();
    AppState::new(config(), prometheus::Registry::new(), db)
}

/// regression: p696-route-shadow -- the literal `image` segment of
/// `POST /overlay/{community}/image/push` must not shadow
/// `GET /overlay/{community}/image/live`: that path still reaches the VIEW
/// guard and then `live_sse`, streaming the Connected frame first.
#[tokio::test]
async fn image_live_sse_still_reaches_the_live_handler() {
    let token = generate_view_token();
    let app = router(state_with_view_credential(42, &token));
    let response = app
        .oneshot(
            Request::builder()
                .uri(format!("/overlay/42/image/live?key={token}"))
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
    let text = String::from_utf8(frame.into_data().expect("data frame").to_vec()).unwrap();
    assert!(text.contains("\"surface\":\"image\""), "got: {text}");
}

/// The same path with a wrong key must be rejected by the VIEW guard (403),
/// not 404/405 -- 403 proves the request was routed to the VIEW-guarded
/// group rather than falling into the image-upload route's path space.
#[tokio::test]
async fn image_live_sse_is_still_view_guarded() {
    let token = generate_view_token();
    let app = router(state_with_view_credential(42, &token));
    let response = app
        .oneshot(
            Request::builder()
                .uri("/overlay/42/image/live?key=wrong-token")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::FORBIDDEN);
}

/// `GET /overlay/{community}/image/live/ws` is likewise still routed to the
/// VIEW-guarded websocket group (403 on a wrong key, never 404).
#[tokio::test]
async fn image_live_ws_is_still_view_guarded() {
    let token = generate_view_token();
    let app = router(state_with_view_credential(42, &token));
    let response = app
        .oneshot(
            Request::builder()
                .uri("/overlay/42/image/live/ws?key=wrong-token")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::FORBIDDEN);
}

/// The image-upload route itself is still reachable and PUSH-guarded.
#[tokio::test]
async fn image_push_is_still_push_guarded() {
    let app = router(state_with_no_queries_expected());
    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/overlay/42/image/push")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let status = response.status();
    let body = response.into_body().collect().await.unwrap().to_bytes();
    assert_eq!(
        status,
        StatusCode::UNAUTHORIZED,
        "body: {}",
        String::from_utf8_lossy(&body)
    );
}

/// The generic PUSH route for a different surface is unaffected by the
/// literal-segment routes.
#[tokio::test]
async fn generic_push_for_other_surfaces_is_still_push_guarded() {
    for surface in ["media", "chat", "full_screen"] {
        let app = router(state_with_no_queries_expected());
        let response = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri(format!("/overlay/42/{surface}/push"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(
            response.status(),
            StatusCode::UNAUTHORIZED,
            "surface {surface}"
        );
    }
}

/// `GET /overlay/{community}/caption/live` is the generic VIEW route for the
/// caption surface -- the literal `caption` segment of the ingest route must
/// not shadow it, and the Connected frame names the caption surface.
#[tokio::test]
async fn caption_live_sse_reaches_the_generic_live_handler() {
    let token = generate_view_token();
    let app = router(state_with_view_credential(42, &token));
    let response = app
        .oneshot(
            Request::builder()
                .uri(format!("/overlay/42/caption/live?key={token}"))
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
    let text = String::from_utf8(frame.into_data().expect("data frame").to_vec()).unwrap();
    assert!(text.contains("\"surface\":\"caption\""), "got: {text}");
}

#[tokio::test]
async fn caption_live_routes_are_still_view_guarded() {
    for suffix in ["live", "live/ws"] {
        let token = generate_view_token();
        let app = router(state_with_view_credential(42, &token));
        let response = app
            .oneshot(
                Request::builder()
                    .uri(format!("/overlay/42/caption/{suffix}?key=wrong-token"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::FORBIDDEN, "{suffix}");
    }
}

/// The caption ingest route is reachable and PUSH-guarded: 401 without a
/// bearer -- not 400 (the image-route guard bug) and not 404.
#[tokio::test]
async fn caption_push_is_push_guarded_and_reachable() {
    let app = router(state_with_no_queries_expected());
    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/overlay/42/caption/push")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
}

/// The legacy page URL is its own route, not swallowed by a generic
/// `/overlay/{community}/{surface}/...` pattern: with a wrong key it reaches
/// the page handler's credential check (403), and with no `community_id` its
/// own 400 -- not a 404.
#[tokio::test]
async fn legacy_caption_page_url_routes_to_the_page_handler() {
    let token = generate_view_token();
    let app = router(state_with_view_credential(42, &token));
    let response = app
        .oneshot(
            Request::builder()
                .uri("/overlay/captions/not-the-key?community_id=42")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::FORBIDDEN);

    let app = router(state_with_no_queries_expected());
    let response = app
        .oneshot(
            Request::builder()
                .uri("/overlay/captions/some-key")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
}

/// A numeric community's `/overlay/42/caption` is the generic overlay-page
/// route (VIEW-guarded), never the legacy caption page: with a valid key it
/// reaches the page handler, which answers 404 because `caption` has its own
/// page (`/overlay/captions/{key}`) -- not a 200 caption page.
#[tokio::test]
async fn a_numeric_community_never_matches_the_legacy_caption_page_route() {
    let token = generate_view_token();
    let app = router(state_with_view_credential(42, &token));
    let response = app
        .oneshot(
            Request::builder()
                .uri(format!("/overlay/42/caption?key={token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::NOT_FOUND);
}

/// The overlay page route and the literal caption page route coexist:
/// `/overlay/42/chat` reaches the page, `/overlay/captions/{key}` still
/// reaches the caption handler.
#[tokio::test]
async fn the_overlay_page_route_does_not_shadow_the_caption_page_route() {
    let token = generate_view_token();
    let app = router(state_with_view_credential(42, &token));
    let response = app
        .oneshot(
            Request::builder()
                .uri(format!("/overlay/42/chat?key={token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(
        response.headers().get("content-type").unwrap(),
        "text/html; charset=utf-8"
    );
    // And the literal caption URL is unchanged (wrong key => its own 403).
    let app = router(state_with_view_credential(42, &token));
    let response = app
        .oneshot(
            Request::builder()
                .uri("/overlay/captions/not-the-key?community_id=42")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::FORBIDDEN);
}

/// The legacy websocket URL routes to the caption websocket handshake (401
/// without a key), not a 404 and not the generic live websocket.
#[tokio::test]
async fn legacy_caption_websocket_url_routes_to_the_caption_handshake() {
    let app = router(state_with_no_queries_expected());
    let response = app
        .oneshot(
            Request::builder()
                .uri("/ws/captions/42")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
}
