//! Route-specificity regression tests for the merged `http::router`.
//!
//! Overlay routes are addressed by the community's unguessable overlay code:
//! `/{overlay_code}/{surface}[/...]`. `POST /{overlay_code}/image/push` (P6's
//! upload route) and `POST /{overlay_code}/caption/push` (the caption ingest
//! route) each use a literal path segment where the generic
//! `/{overlay_code}/{surface}/...` routes use a `{surface}` capture.
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
//!
//! The first path segment is a plain capture (axum cannot regex-constrain it),
//! so the second half of this file proves the constraint the guards enforce:
//! only exactly 16 lowercase hex characters naming a real community ever
//! reaches a handler. `/health`, `/readyz` and every other root keep their own
//! routes; an old integer path, a wrong code, or a reserved word under the
//! capture is a `404` that touches no database.

use axum::body::Body;
use axum::http::{Request, StatusCode};
use clap::Parser;
use http_body_util::BodyExt;
use overlay_auth::{generate_view_token, hash_token};
use sea_orm::{DatabaseBackend, MockDatabase};
use std::time::Duration;
use tower::ServiceExt;

mod common;

use common::{test_overlay_codes, CODE_42, CODE_UNKNOWN};
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
        .with_overlay_codes(test_overlay_codes())
}

fn state_with_no_queries_expected() -> AppState {
    let db = MockDatabase::new(DatabaseBackend::Postgres).into_connection();
    AppState::new(config(), prometheus::Registry::new(), db)
        .with_overlay_codes(test_overlay_codes())
}

/// regression: p696-route-shadow -- the literal `image` segment of
/// `POST /{overlay_code}/image/push` must not shadow
/// `GET /{overlay_code}/image/live`: that path still reaches the VIEW
/// guard and then `live_sse`, streaming the Connected frame first.
#[tokio::test]
async fn image_live_sse_still_reaches_the_live_handler() {
    let token = generate_view_token();
    let app = router(state_with_view_credential(42, &token));
    let response = app
        .oneshot(
            Request::builder()
                .uri(format!("/{CODE_42}/image/live?key={token}"))
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
                .uri(format!("/{CODE_42}/image/live?key=wrong-token"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::FORBIDDEN);
}

/// `GET /{overlay_code}/image/live/ws` is likewise still routed to the
/// VIEW-guarded websocket group (403 on a wrong key, never 404).
#[tokio::test]
async fn image_live_ws_is_still_view_guarded() {
    let token = generate_view_token();
    let app = router(state_with_view_credential(42, &token));
    let response = app
        .oneshot(
            Request::builder()
                .uri(format!("/{CODE_42}/image/live/ws?key=wrong-token"))
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
                .uri(format!("/{CODE_42}/image/push"))
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
                    .uri(format!("/{CODE_42}/{surface}/push"))
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

/// `GET /{overlay_code}/caption/live` is the generic VIEW route for the
/// caption surface -- the literal `caption` segment of the ingest route must
/// not shadow it, and the Connected frame names the caption surface.
#[tokio::test]
async fn caption_live_sse_reaches_the_generic_live_handler() {
    let token = generate_view_token();
    let app = router(state_with_view_credential(42, &token));
    let response = app
        .oneshot(
            Request::builder()
                .uri(format!("/{CODE_42}/caption/live?key={token}"))
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
                    .uri(format!("/{CODE_42}/caption/{suffix}?key=wrong-token"))
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
                .uri(format!("/{CODE_42}/caption/push"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
}

/// The legacy page URL is its own route, not swallowed by the generic
/// `/{overlay_code}/{surface}/...` pattern: with a wrong key it reaches
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

/// A community's `/{overlay_code}/caption` is the generic overlay-page route
/// (VIEW-guarded), never the legacy caption page: with a valid key it reaches
/// the page handler, which answers 404 because `caption` has its own page
/// (`/overlay/captions/{key}`) -- not a 200 caption page.
#[tokio::test]
async fn a_numeric_community_never_matches_the_legacy_caption_page_route() {
    let token = generate_view_token();
    let app = router(state_with_view_credential(42, &token));
    let response = app
        .oneshot(
            Request::builder()
                .uri(format!("/{CODE_42}/caption?key={token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::NOT_FOUND);
}

/// The overlay page route and the literal caption page route coexist:
/// `/{overlay_code}/chat` reaches the page, `/overlay/captions/{key}` still
/// reaches the caption handler.
#[tokio::test]
async fn the_overlay_page_route_does_not_shadow_the_caption_page_route() {
    let token = generate_view_token();
    let app = router(state_with_view_credential(42, &token));
    let response = app
        .oneshot(
            Request::builder()
                .uri(format!("/{CODE_42}/chat?key={token}"))
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

// -- the overlay-code constraint -----------------------------------------

async fn status_of(app: axum::Router, method: &str, uri: &str) -> StatusCode {
    app.oneshot(
        Request::builder()
            .method(method)
            .uri(uri)
            .body(Body::empty())
            .unwrap(),
    )
    .await
    .unwrap()
    .status()
}

/// `/health` and `/readyz` keep their own routes: the root-level
/// `/{overlay_code}/...` capture never clobbers them.
#[tokio::test]
async fn health_and_readyz_are_not_clobbered_by_the_overlay_code_route() {
    let app = router(state_with_no_queries_expected());
    let response = app
        .clone()
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
    assert!(
        String::from_utf8_lossy(&body).contains("ok"),
        "/health must answer its own liveness body"
    );

    // `/readyz` answers its own dependency snapshot, not a guard rejection.
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
    assert!(parsed["dependencies"].is_array(), "{parsed}");
}

/// The `[0-9a-f]{16}` constraint: the overlay-code capture can never serve a
/// reserved root word, whatever follows it, and never queries the database
/// (the mock DB has nothing queued, so any query would surface as a 500).
#[tokio::test]
async fn a_reserved_root_word_under_the_code_capture_is_a_404_not_a_handler() {
    for root in ["health", "readyz", "overlay", "ws", "metrics", "api"] {
        for (method, path) in [
            ("GET", format!("/{root}/chat?key=k")),
            ("GET", format!("/{root}/chat/live?key=k")),
            ("GET", format!("/{root}/chat/live/ws?key=k")),
            ("POST", format!("/{root}/chat/push")),
            ("POST", format!("/{root}/caption/push")),
            ("POST", format!("/{root}/image/push")),
        ] {
            let status = status_of(router(state_with_no_queries_expected()), method, &path).await;
            assert_eq!(status, StatusCode::NOT_FOUND, "{method} {path}");
        }
    }
}

/// An old-scheme integer URL no longer resolves anywhere: the integer is not a
/// code, and the old `/overlay/{id}/...` prefix has no route.
#[tokio::test]
async fn the_old_integer_overlay_urls_are_404() {
    for (method, path) in [
        ("GET", "/42/chat?key=k"),
        ("GET", "/42/chat/live?key=k"),
        ("POST", "/42/chat/push"),
        ("GET", "/overlay/42/chat?key=k"),
        ("GET", "/overlay/42/chat/live?key=k"),
        ("GET", "/overlay/42/chat/live/ws?key=k"),
        ("POST", "/overlay/42/chat/push"),
        ("POST", "/overlay/42/image/push"),
        ("POST", "/overlay/42/caption/push"),
    ] {
        let status = status_of(router(state_with_no_queries_expected()), method, path).await;
        assert_eq!(status, StatusCode::NOT_FOUND, "{method} {path}");
    }
}

/// Malformed shapes of the right length are not codes either: uppercase,
/// non-hex, too short, too long, a 16-digit zero-padded integer.
#[tokio::test]
async fn near_miss_codes_are_404() {
    for code in [
        "A1B2C3D4E5F60718",  // uppercase
        "a1b2c3d4e5f6071g",  // non-hex
        "a1b2c3d4e5f6071",   // 15
        "a1b2c3d4e5f607180", // 17
        "0000000000000042",  // a zero-padded integer id is not a code
        "a1b2c3d4e5f6%2f18", // encoded slash
    ] {
        let status = status_of(
            router(state_with_no_queries_expected()),
            "GET",
            &format!("/{code}/chat?key=k"),
        )
        .await;
        assert_eq!(status, StatusCode::NOT_FOUND, "{code}");
    }
}

/// A well-formed code that names no community is a 404 on every route kind,
/// including with a key and a bearer present -- the lookup comes first.
#[tokio::test]
async fn an_unknown_well_formed_code_is_404_on_every_route_kind() {
    for (method, path) in [
        ("GET", format!("/{CODE_UNKNOWN}/chat?key=k")),
        ("GET", format!("/{CODE_UNKNOWN}/chat/live?key=k")),
        ("GET", format!("/{CODE_UNKNOWN}/chat/live/ws?key=k")),
        ("POST", format!("/{CODE_UNKNOWN}/chat/push")),
        ("POST", format!("/{CODE_UNKNOWN}/caption/push")),
        ("POST", format!("/{CODE_UNKNOWN}/image/push")),
    ] {
        let status = status_of(router(state_with_no_queries_expected()), method, &path).await;
        assert_eq!(status, StatusCode::NOT_FOUND, "{method} {path}");
    }
}

/// A known code serves its page behind the VIEW key; a wrong key on the same
/// code is the credential check's 403 (not a 404: the code did resolve).
/// Cross-community key scoping is proven against a store that actually filters
/// by community in `overlay::router`'s unit tests (the mock DB here answers the
/// same row for any lookup, so it cannot show it).
#[tokio::test]
async fn a_known_code_serves_its_page_behind_the_view_key() {
    let token = generate_view_token();
    let app = router(state_with_view_credential(42, &token));
    assert_eq!(
        status_of(app, "GET", &format!("/{CODE_42}/chat?key={token}")).await,
        StatusCode::OK
    );
    let app = router(state_with_view_credential(42, &token));
    assert_eq!(
        status_of(app, "GET", &format!("/{CODE_42}/chat?key=not-the-key")).await,
        StatusCode::FORBIDDEN
    );
}

/// Without `?key=` a known code is a 400 (the VIEW key stays mandatory:
/// the unguessable code is defense in depth beside it, not instead of it).
#[tokio::test]
async fn the_view_key_is_still_mandatory_beside_the_code() {
    let app = router(state_with_no_queries_expected());
    for suffix in ["chat", "chat/live", "chat/live/ws"] {
        assert_eq!(
            status_of(app.clone(), "GET", &format!("/{CODE_42}/{suffix}")).await,
            StatusCode::BAD_REQUEST,
            "{suffix}"
        );
    }
}
