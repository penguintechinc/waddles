//! Integration tests for P6's `POST /overlay/{community}/image/push`
//! handler -- exercised through the real axum `Router`/`Multipart`
//! extractor against fake `ImageStore`/`AssetStore` backends (the two
//! genuinely-external dependencies, SeaweedFS + Postgres), not the handler
//! function called directly. The `overlay_auth::require_push_credential`
//! middleware itself (JWT verification) is already covered end-to-end by
//! `core/overlay_auth`'s and `crate::overlay::router`'s own test suites --
//! these tests inject an already-verified `PushCredential` request
//! extension directly (`RequestBuilder::extension`), the same seam the
//! guard middleware itself populates on success, so what's under test here
//! is the upload handler's own logic, not re-proving JWT verification.

use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use axum::body::Body;
use axum::http::{Request, StatusCode};
use axum::routing::post;
use axum::Router;
use clap::Parser;
use http_body_util::BodyExt;
use overlay_auth::PushCredential;
use sea_orm::{DatabaseBackend, MockDatabase};
use tower::ServiceExt;

use svc_presentation::config::{CliConfig, Config, Secret};
use svc_presentation::flags::{boxed, StaticFlag};
use svc_presentation::http::AppState;
use svc_presentation::images::asset_store::{AssetStore, AssetStoreError, NewImageAsset};
use svc_presentation::images::store::{ImageStore, ImageStoreError};
use svc_presentation::images::upload::upload_image;

#[derive(Default)]
struct FakeImageStore {
    puts: Mutex<Vec<(String, usize, String)>>,
}

#[async_trait]
impl ImageStore for FakeImageStore {
    async fn put(
        &self,
        key: &str,
        bytes: bytes::Bytes,
        content_type: &str,
    ) -> Result<(), ImageStoreError> {
        self.puts
            .lock()
            .unwrap()
            .push((key.to_string(), bytes.len(), content_type.to_string()));
        Ok(())
    }

    async fn signed_get_url(
        &self,
        _key: &str,
        _ttl: std::time::Duration,
    ) -> Result<url::Url, ImageStoreError> {
        unimplemented!("not exercised by the upload-path tests")
    }
}

/// Always fails `put` -- proves `crate::images::upload::upload_image`
/// surfaces a store failure as a clear 500 (and bumps the `error`-labeled
/// metric) rather than panicking or silently reporting success.
struct FailingImageStore;

#[async_trait]
impl ImageStore for FailingImageStore {
    async fn put(
        &self,
        _key: &str,
        _bytes: bytes::Bytes,
        _content_type: &str,
    ) -> Result<(), ImageStoreError> {
        Err(ImageStoreError::Put("connection refused".to_string()))
    }

    async fn signed_get_url(
        &self,
        _key: &str,
        _ttl: std::time::Duration,
    ) -> Result<url::Url, ImageStoreError> {
        unimplemented!("not exercised by the upload-path tests")
    }
}

#[derive(Default)]
struct FakeAssetStore {
    inserted: Mutex<Vec<NewImageAsset>>,
}

#[async_trait]
impl AssetStore for FakeAssetStore {
    async fn insert(&self, asset: NewImageAsset) -> Result<(), AssetStoreError> {
        self.inserted.lock().unwrap().push(asset);
        Ok(())
    }

    async fn find(
        &self,
        _community_id: i64,
        _asset_id: uuid::Uuid,
    ) -> Result<Option<svc_presentation::images::asset_store::ImageAssetRecord>, AssetStoreError>
    {
        unimplemented!("not exercised by the upload-path tests")
    }
}

/// Builds a minimal `multipart/form-data` body: one `file` part (with the
/// given filename/content-type/bytes) -- hand-rolled rather than pulled
/// from a multipart-building crate, matching `core/bundle_executor::
/// bucket`'s own precedent of hand-rolling a narrow protocol surface
/// rather than adding a dependency for one call shape.
fn multipart_body(boundary: &str, filename: &str, content_type: &str, bytes: &[u8]) -> Vec<u8> {
    multipart_body_with_fields(boundary, filename, content_type, bytes, &[])
}

/// Same as [`multipart_body`], plus one plain-text form field per
/// `(name, value)` pair in `extra_fields` -- covers the display-param
/// (`position_x`/`position_y`/`width`/`height`/`duration_ms`) and
/// unrecognized-field parsing branches `multipart_body` alone never
/// exercises.
fn multipart_body_with_fields(
    boundary: &str,
    filename: &str,
    content_type: &str,
    bytes: &[u8],
    extra_fields: &[(&str, &str)],
) -> Vec<u8> {
    let mut body = Vec::new();
    body.extend_from_slice(format!("--{boundary}\r\n").as_bytes());
    body.extend_from_slice(
        format!("Content-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n")
            .as_bytes(),
    );
    body.extend_from_slice(format!("Content-Type: {content_type}\r\n\r\n").as_bytes());
    body.extend_from_slice(bytes);
    body.extend_from_slice(b"\r\n");
    for (name, value) in extra_fields {
        body.extend_from_slice(format!("--{boundary}\r\n").as_bytes());
        body.extend_from_slice(
            format!("Content-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n")
                .as_bytes(),
        );
    }
    body.extend_from_slice(format!("--{boundary}--\r\n").as_bytes());
    body
}

fn test_state_with(
    image_store: Option<Arc<dyn ImageStore>>,
    asset_store: Arc<dyn AssetStore>,
    flag_enabled: bool,
) -> AppState {
    let cli = CliConfig::try_parse_from(["svc-presentation"]).expect("defaults parse");
    let config = Config {
        cli,
        db_password: Secret::new("db-pass"),
        cache_password: None,
        image_bucket_access_key_id: None,
        image_bucket_secret_access_key: None,
    };
    let db = MockDatabase::new(DatabaseBackend::Postgres).into_connection();
    let mut state = AppState::new(config, prometheus::Registry::new(), db);
    state.image_store = image_store;
    state.image_asset_store = asset_store;
    state.image_upload_flag = boxed(StaticFlag(flag_enabled));
    state
}

fn push_credential(community_id: i64) -> PushCredential {
    PushCredential {
        claims: service_auth::ServiceClaims {
            iss: "hub-api".into(),
            aud: "waddlebot-internal".into(),
            sub: "spiffe://penguintech.io/alpha/svc-action".into(),
            scope: overlay_auth::push_scope(community_id),
            iat: 0,
            nbf: 0,
            exp: u64::MAX,
            jti: "test-jti".into(),
        },
        community_id,
    }
}

fn upload_router(state: AppState) -> Router {
    Router::new()
        .route("/overlay/{community}/image/push", post(upload_image))
        .with_state(state)
}

/// Minimal PNG signature plus filler -- passes the magic-byte sniff.
const PNG_BYTES: &[u8] = b"\x89PNG\r\n\x1a\nIHDR-filler-bytes";

#[tokio::test]
async fn spoofed_content_type_with_html_payload_is_rejected_with_400() {
    let image_store: Arc<dyn ImageStore> = Arc::new(FakeImageStore::default());
    let asset_store = Arc::new(FakeAssetStore::default());
    let state = test_state_with(Some(image_store), asset_store.clone(), true);
    let app = upload_router(state);
    let boundary = "testboundary";
    let body = multipart_body(
        boundary,
        "evil.png",
        "image/png",
        b"<html><script>alert(1)</script></html>",
    );
    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/overlay/42/image/push")
                .header(
                    "content-type",
                    format!("multipart/form-data; boundary={boundary}"),
                )
                .extension(push_credential(42))
                .body(Body::from(body))
                .expect("request"),
        )
        .await
        .expect("response");
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    assert!(asset_store.inserted.lock().unwrap().is_empty());
}

#[tokio::test]
async fn happy_path_upload_stores_the_object_and_the_metadata_row() {
    let image_store: Arc<dyn ImageStore> = Arc::new(FakeImageStore::default());
    let asset_store = Arc::new(FakeAssetStore::default());
    let state = test_state_with(Some(image_store.clone()), asset_store.clone(), true);
    let app = upload_router(state);

    let boundary = "testboundary";
    let body = multipart_body(boundary, "test.png", "image/png", PNG_BYTES);

    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/overlay/42/image/push")
                .header(
                    "content-type",
                    format!("multipart/form-data; boundary={boundary}"),
                )
                .extension(push_credential(42))
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::CREATED);
    let bytes = response.into_body().collect().await.unwrap().to_bytes();
    let parsed: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(parsed["content_type"], "image/png");

    let inserted = asset_store.inserted.lock().unwrap();
    assert_eq!(inserted.len(), 1);
    assert_eq!(inserted[0].community_id, 42);
    assert!(inserted[0].object_key.starts_with("overlay-images/42/"));
    assert!(inserted[0].object_key.ends_with(".png"));
}

#[tokio::test]
async fn non_image_content_type_is_rejected_with_400() {
    let image_store: Arc<dyn ImageStore> = Arc::new(FakeImageStore::default());
    let asset_store = Arc::new(FakeAssetStore::default());
    let state = test_state_with(Some(image_store), asset_store.clone(), true);
    let app = upload_router(state);

    let boundary = "testboundary";
    let body = multipart_body(
        boundary,
        "evil.html",
        "text/html",
        b"<script>evil()</script>",
    );

    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/overlay/42/image/push")
                .header(
                    "content-type",
                    format!("multipart/form-data; boundary={boundary}"),
                )
                .extension(push_credential(42))
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    assert!(asset_store.inserted.lock().unwrap().is_empty());
}

#[tokio::test]
async fn oversized_upload_is_rejected_with_400() {
    let image_store: Arc<dyn ImageStore> = Arc::new(FakeImageStore::default());
    let asset_store = Arc::new(FakeAssetStore::default());
    let mut state = test_state_with(Some(image_store), asset_store.clone(), true);
    state.config = Arc::new(Config {
        cli: {
            let mut cli = CliConfig::try_parse_from(["svc-presentation"]).expect("defaults parse");
            cli.image_max_bytes = 4;
            cli
        },
        db_password: Secret::new("db-pass"),
        cache_password: None,
        image_bucket_access_key_id: None,
        image_bucket_secret_access_key: None,
    });
    let app = upload_router(state);

    let boundary = "testboundary";
    let body = multipart_body(boundary, "test.png", "image/png", PNG_BYTES);

    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/overlay/42/image/push")
                .header(
                    "content-type",
                    format!("multipart/form-data; boundary={boundary}"),
                )
                .extension(push_credential(42))
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn flag_disabled_rejects_with_403_before_touching_any_store() {
    let image_store: Arc<dyn ImageStore> = Arc::new(FakeImageStore::default());
    let asset_store = Arc::new(FakeAssetStore::default());
    let state = test_state_with(Some(image_store), asset_store.clone(), false);
    let app = upload_router(state);

    let boundary = "testboundary";
    let body = multipart_body(boundary, "test.png", "image/png", PNG_BYTES);

    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/overlay/42/image/push")
                .header(
                    "content-type",
                    format!("multipart/form-data; boundary={boundary}"),
                )
                .extension(push_credential(42))
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::FORBIDDEN);
    assert!(asset_store.inserted.lock().unwrap().is_empty());
}

#[tokio::test]
async fn missing_image_store_surfaces_a_clear_500_not_a_panic() {
    let asset_store = Arc::new(FakeAssetStore::default());
    let state = test_state_with(None, asset_store.clone(), true);
    let app = upload_router(state);

    let boundary = "testboundary";
    let body = multipart_body(boundary, "test.png", "image/png", PNG_BYTES);

    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/overlay/42/image/push")
                .header(
                    "content-type",
                    format!("multipart/form-data; boundary={boundary}"),
                )
                .extension(push_credential(42))
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::INTERNAL_SERVER_ERROR);
}

/// Covers the display-param (`position_x`/`position_y`/`width`/`height`/
/// `duration_ms`) multipart fields plus one unrecognized field, which a
/// bare `multipart_body` call never exercises.
#[tokio::test]
async fn display_param_fields_are_parsed_and_stored_and_unknown_fields_are_ignored() {
    let image_store: Arc<dyn ImageStore> = Arc::new(FakeImageStore::default());
    let asset_store = Arc::new(FakeAssetStore::default());
    let state = test_state_with(Some(image_store), asset_store.clone(), true);
    let app = upload_router(state);

    let boundary = "testboundary";
    let body = multipart_body_with_fields(
        boundary,
        "test.png",
        "image/png",
        PNG_BYTES,
        &[
            ("position_x", "10"),
            ("position_y", "20"),
            ("width", "200"),
            ("height", "100"),
            ("duration_ms", "5000"),
            ("some_future_field", "ignored"),
        ],
    );

    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/overlay/42/image/push")
                .header(
                    "content-type",
                    format!("multipart/form-data; boundary={boundary}"),
                )
                .extension(push_credential(42))
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::CREATED);
    let inserted = asset_store.inserted.lock().unwrap();
    assert_eq!(inserted.len(), 1);
    assert_eq!(inserted[0].position_x, Some(10));
    assert_eq!(inserted[0].position_y, Some(20));
    assert_eq!(inserted[0].width, Some(200));
    assert_eq!(inserted[0].height, Some(100));
    assert_eq!(inserted[0].duration_ms, Some(5000));
}

/// A bucket PUT failure surfaces as a clear 500, never a panic, and the
/// asset row is never inserted for a partially-failed upload.
#[tokio::test]
async fn image_store_put_failure_surfaces_a_clear_500() {
    let image_store: Arc<dyn ImageStore> = Arc::new(FailingImageStore);
    let asset_store = Arc::new(FakeAssetStore::default());
    let state = test_state_with(Some(image_store), asset_store.clone(), true);
    let app = upload_router(state);

    let boundary = "testboundary";
    let body = multipart_body(boundary, "test.png", "image/png", PNG_BYTES);

    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/overlay/42/image/push")
                .header(
                    "content-type",
                    format!("multipart/form-data; boundary={boundary}"),
                )
                .extension(push_credential(42))
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::INTERNAL_SERVER_ERROR);
    assert!(asset_store.inserted.lock().unwrap().is_empty());
}
