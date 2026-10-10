//! The real code-resolving PUSH guard (`overlay::router::with_push_guard`),
//! in front of the real routes of `http::router`, verifying real signed JWTs
//! against a live (local) JWKS endpoint -- the end-to-end proof that a *valid*
//! credential reaches each PUSH route's handler, that the overlay code in the
//! URL resolves to the community the credential must be scoped to, and that a
//! wrong code / old integer path never reaches a handler.
//!
//! regression: p696-route-shadow -- `tests/image_upload.rs` injects an
//! already-verified `PushCredential` and so bypasses the guard entirely; it
//! could not see that `POST /{overlay_code}/image/push` answered 400
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
        &format!("/{CODE_42}/image/push"),
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
        &format!("/{CODE_42}/image/push"),
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
        &format!("/{CODE_42}/image/push"),
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
        &format!("/{CODE_42}/caption/push"),
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
        &format!("/{CODE_42}/caption/push"),
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
        &format!("/{CODE_42}/caption/push"),
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
        &format!("/{CODE_42}/caption/push"),
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
        &format!("/{CODE_42}/media/push"),
        Some(sign_push_token(42)),
        "application/json",
        br#"{"title":"hello"}"#.to_vec(),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert!(body.contains("published"));
}

// -- overlay-code resolution: the code is the only public handle --

/// regression: gh-overlay-code -- a token minted for community 43 must not push
/// to community 42's code, and nothing may be delivered to either community.
#[tokio::test]
async fn a_token_for_another_community_is_refused_on_this_code_and_delivers_nothing() {
    let state = state_with_real_push_trust(true).await;
    let mut sub_42 = state
        .frame_hub
        .subscribe(42, overlay_schema::Surface::Media);
    let mut sub_43 = state
        .frame_hub
        .subscribe(43, overlay_schema::Surface::Media);
    let (status, _) = send(
        router(state.clone()),
        "POST",
        &format!("/{CODE_42}/media/push"),
        Some(sign_push_token(43)),
        "application/json",
        br#"{"title":"cross-community"}"#.to_vec(),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN);
    for sub in [&mut sub_42, &mut sub_43] {
        assert!(
            tokio::time::timeout(std::time::Duration::from_millis(100), sub.recv())
                .await
                .is_err(),
            "a refused push must deliver nothing"
        );
    }
}

/// The code picks the community: community 43's code with community 43's token
/// reaches the handler and publishes to 43 only.
#[tokio::test]
async fn each_code_publishes_to_its_own_community_only() {
    let state = state_with_real_push_trust(true).await;
    let mut sub_42 = state
        .frame_hub
        .subscribe(42, overlay_schema::Surface::Media);
    let mut sub_43 = state
        .frame_hub
        .subscribe(43, overlay_schema::Surface::Media);
    let (status, body) = send(
        router(state.clone()),
        "POST",
        &format!("/{CODE_43}/media/push"),
        Some(sign_push_token(43)),
        "application/json",
        br#"{"title":"for forty-three"}"#.to_vec(),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert!(
        body.contains(CODE_43),
        "response echoes the code the caller used: {body}"
    );
    assert!(sub_43.recv().await.is_some(), "43 receives its push");
    assert!(
        tokio::time::timeout(std::time::Duration::from_millis(100), sub_42.recv())
            .await
            .is_err(),
        "42 must not see 43's push"
    );
}

/// An unknown code and the old integer path both 404 -- even carrying a
/// perfectly valid token for the community the integer names.
#[tokio::test]
async fn an_unknown_code_and_the_old_integer_path_are_404_even_with_a_valid_token() {
    let state = state_with_real_push_trust(true).await;
    for uri in [
        format!("/{CODE_UNKNOWN}/media/push"),
        "/42/media/push".to_string(),
        "/overlay/42/media/push".to_string(),
        format!("/{CODE_UNKNOWN}/image/push"),
        format!("/{CODE_UNKNOWN}/caption/push"),
    ] {
        let (status, body) = send(
            router(state.clone()),
            "POST",
            &uri,
            Some(sign_push_token(42)),
            "application/json",
            br#"{"title":"x"}"#.to_vec(),
        )
        .await;
        assert_eq!(status, StatusCode::NOT_FOUND, "{uri}: {body}");
    }
}
