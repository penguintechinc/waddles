//! The real `overlay_auth::require_push_credential` guard, in front of the
//! real routes of `http::router`, verifying real signed JWTs against a live
//! (local) JWKS endpoint -- the end-to-end proof that a *valid* credential
//! reaches each PUSH route's handler.
//!
//! regression: p696-route-shadow -- `tests/image_upload.rs` injects an
//! already-verified `PushCredential` and so bypasses the guard entirely; it
//! could not see that `POST /overlay/{community}/image/push` answered 400
//! ("Invalid URL: missing field `surface`") to every request, valid
//! credential or not, because the guard extracted a `{surface}` path
//! parameter the literal-`image` route doesn't have. These tests drive the
//! guard for real.

mod common;

use axum::body::Body;
use axum::http::{header, Request, StatusCode};
use http_body_util::BodyExt;
use tower::ServiceExt;

use common::*;
use svc_presentation::http::router;

async fn send(
    app: axum::Router,
    method: &str,
    uri: &str,
    bearer: Option<String>,
    content_type: &str,
    body: Vec<u8>,
) -> (StatusCode, String) {
    let mut request = Request::builder()
        .method(method)
        .uri(uri)
        .header(header::CONTENT_TYPE, content_type);
    if let Some(token) = bearer {
        request = request.header(header::AUTHORIZATION, format!("Bearer {token}"));
    }
    let response = app
        .oneshot(request.body(Body::from(body)).unwrap())
        .await
        .unwrap();
    let status = response.status();
    let bytes = response.into_body().collect().await.unwrap().to_bytes();
    (status, String::from_utf8_lossy(&bytes).into_owned())
}

/// A valid credential gets PAST the guard on the image-upload route: the
/// image-upload flag is forced OFF so the *handler's own* JSON 403 ("not
/// enabled") is the observable proof the handler ran -- distinct from the
/// guard's plain-text 403/401/400 rejections.
#[tokio::test]
async fn image_upload_route_accepts_a_valid_credential_and_reaches_its_handler() {
    let mut state = state_with_real_push_trust(true).await;
    state.image_upload_flag =
        svc_presentation::flags::boxed(svc_presentation::flags::StaticFlag(false));
    let (status, body) = send(
        router(state),
        "POST",
        "/overlay/42/image/push",
        Some(sign_push_token(42)),
        "multipart/form-data; boundary=x",
        b"--x--\r\n".to_vec(),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN);
    assert!(
        body.contains("image upload is not enabled"),
        "the request must reach upload_image, got: {body}"
    );
}

#[tokio::test]
async fn image_upload_route_rejects_a_credential_for_another_community() {
    let state = state_with_real_push_trust(true).await;
    let (status, body) = send(
        router(state),
        "POST",
        "/overlay/42/image/push",
        Some(sign_push_token(99)),
        "multipart/form-data; boundary=x",
        b"--x--\r\n".to_vec(),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN);
    assert!(!body.contains("not enabled"), "guard must stop it: {body}");
}

#[tokio::test]
async fn image_upload_route_without_a_bearer_is_401_not_400() {
    let state = state_with_real_push_trust(true).await;
    let (status, _) = send(
        router(state),
        "POST",
        "/overlay/42/image/push",
        None,
        "multipart/form-data; boundary=x",
        b"--x--\r\n".to_vec(),
    )
    .await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn caption_ingest_route_accepts_a_valid_credential_and_publishes() {
    let store = arc_store(FakeCaptionStore::default());
    let mut state = state_with_real_push_trust(true).await;
    state.caption_store = store.clone();
    let (status, body) = send(
        router(state),
        "POST",
        "/overlay/42/caption/push",
        Some(sign_push_token(42)),
        "application/json",
        serde_json::to_vec(&caption_push("signed caption")).unwrap(),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(store.inserted.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn caption_ingest_route_rejects_a_credential_for_another_community() {
    let store = arc_store(FakeCaptionStore::default());
    let mut state = state_with_real_push_trust(true).await;
    state.caption_store = store.clone();
    let (status, _) = send(
        router(state),
        "POST",
        "/overlay/42/caption/push",
        Some(sign_push_token(99)),
        "application/json",
        serde_json::to_vec(&caption_push("wrong tenant")).unwrap(),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN);
    assert!(store.inserted.lock().unwrap().is_empty());
}

#[tokio::test]
async fn caption_ingest_route_without_a_bearer_is_401() {
    let state = state_with_real_push_trust(true).await;
    let (status, _) = send(
        router(state),
        "POST",
        "/overlay/42/caption/push",
        None,
        "application/json",
        serde_json::to_vec(&caption_push("anonymous")).unwrap(),
    )
    .await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn caption_ingest_route_bounds_the_request_body() {
    let store = arc_store(FakeCaptionStore::default());
    let mut state = state_with_real_push_trust(true).await;
    state.caption_store = store.clone();
    // 128 KiB of JSON: far past any legitimate caption (and past the 64 KiB cap).
    let oversized = format!(r#"{{"text":"{}"}}"#, "x".repeat(128 * 1024));
    let (status, _) = send(
        router(state),
        "POST",
        "/overlay/42/caption/push",
        Some(sign_push_token(42)),
        "application/json",
        oversized.into_bytes(),
    )
    .await;
    assert_eq!(status, StatusCode::PAYLOAD_TOO_LARGE);
    assert!(store.inserted.lock().unwrap().is_empty());
}

/// The generic PUSH route (every surface without its own literal route)
/// still takes a valid credential and publishes.
#[tokio::test]
async fn generic_push_route_still_accepts_a_valid_credential() {
    let state = state_with_real_push_trust(true).await;
    let (status, body) = send(
        router(state),
        "POST",
        "/overlay/42/media/push",
        Some(sign_push_token(42)),
        "application/json",
        br#"{"title":"hello"}"#.to_vec(),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert!(body.contains("published"));
}
