//! Integration tests for `/internal/streaming/pipelines` and
//! `/internal/streaming/ingest-auth` -- `X-Service-Key` gated, exercised
//! directly through `svc_streaming::api::router_for_testing` (bypassing
//! `svc_streaming::http::router`'s JWT `route_layer`, which currently also
//! wraps this subtree -- see `src/api/internal.rs`'s module doc for that
//! known upstream gap; this test file proves this chunk's own `ServiceKey`
//! gate is correct in isolation).

use axum::body::Body;
use axum::http::{Request, StatusCode};
use clap::Parser;
use sea_orm::{ConnectionTrait, Database, DatabaseConnection};
use tower::ServiceExt;

use svc_streaming::api::{router_for_testing, SharedEngine};
use svc_streaming::config::{CliConfig, Config, Secret};
use svc_streaming::http::auth::SERVICE_KEY_HEADER;
use svc_streaming::http::AppState;
use svc_streaming::pipeline::{
    PipelineEngine, PipelineError, PipelineHandle, PipelineId, PipelineSpec, PipelineState,
    PipelineStatus,
};

const SERVICE_KEY: &str = "test-service-key";

async fn seed_db() -> DatabaseConnection {
    let db = Database::connect("sqlite::memory:")
        .await
        .expect("connect in-memory sqlite");
    let schema = r#"
        CREATE TABLE streaming_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            community_id INTEGER NOT NULL UNIQUE,
            source_url TEXT NOT NULL,
            stream_key_hash TEXT,
            source_type TEXT NOT NULL DEFAULT 'rtmp',
            enabled INTEGER NOT NULL DEFAULT 1,
            record_enabled INTEGER NOT NULL DEFAULT 0,
            transcode_enabled INTEGER NOT NULL DEFAULT 0,
            transcode_bitrate_kbps INTEGER NOT NULL DEFAULT 4000,
            video_codec TEXT NOT NULL DEFAULT 'h264',
            audio_codec TEXT NOT NULL DEFAULT 'copy'
        );
        INSERT INTO streaming_configs (id, community_id, source_url, enabled)
            VALUES (1, 10, 'demo-stream-key-a', 1);
        INSERT INTO streaming_configs (id, community_id, source_url, enabled)
            VALUES (2, 20, 'demo-stream-key-disabled', 0);
    "#;
    for stmt in schema.split(';') {
        let stmt = stmt.trim();
        if !stmt.is_empty() {
            db.execute_unprepared(stmt)
                .await
                .unwrap_or_else(|err| panic!("seed statement failed ({stmt:?}): {err}"));
        }
    }
    svc_streaming::stream_key::backfill_hashes_from_source_url_for_tests(&db).await;
    db
}

/// Reports config 1 (community 10) as running once `start` has been
/// called against it; everything else is `NotFound`.
#[derive(Clone, Default)]
struct FakeEngine {
    running_id: std::sync::Arc<std::sync::Mutex<Option<PipelineId>>>,
}

impl PipelineEngine for FakeEngine {
    async fn start(&self, spec: PipelineSpec) -> Result<PipelineHandle, PipelineError> {
        *self.running_id.lock().unwrap() = Some(spec.id);
        Ok(PipelineHandle { id: spec.id })
    }

    async fn stop(&self, _id: PipelineId) -> Result<(), PipelineError> {
        Ok(())
    }

    async fn status(&self, id: PipelineId) -> Result<PipelineStatus, PipelineError> {
        if *self.running_id.lock().unwrap() == Some(id) {
            Ok(PipelineStatus {
                id,
                state: PipelineState::Running,
                detail: None,
            })
        } else {
            Err(PipelineError::NotFound(id))
        }
    }
}

fn test_state() -> AppState {
    let cli = CliConfig::try_parse_from(["svc-streaming"]).expect("defaults parse");
    let config = Config {
        cli,
        db_password: Secret::new("db-pass"),
        cache_password: None,
        service_api_key: Secret::new(SERVICE_KEY),
        jwt_hmac_secret: None,
    };
    AppState::new(config, prometheus::Registry::new())
}

async fn build_app() -> (axum::Router, SharedEngine) {
    let db = seed_db().await;
    let engine: SharedEngine = std::sync::Arc::new(FakeEngine::default());
    let app = router_for_testing(db, engine.clone()).with_state(test_state());
    (app, engine)
}

#[tokio::test]
async fn ingest_auth_without_service_key_is_401() {
    let (app, _engine) = build_app().await;
    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/internal/streaming/ingest-auth")
                .header("content-type", "application/json")
                .body(Body::from(
                    serde_json::json!({"kind": "rtmp", "key": "demo-stream-key-a"}).to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn ingest_auth_wrong_service_key_is_401() {
    let (app, _engine) = build_app().await;
    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/internal/streaming/ingest-auth")
                .header(SERVICE_KEY_HEADER, "not-the-real-key")
                .header("content-type", "application/json")
                .body(Body::from(
                    serde_json::json!({"kind": "rtmp", "key": "demo-stream-key-a"}).to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn ingest_auth_allows_known_enabled_key() {
    let (app, _engine) = build_app().await;
    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/internal/streaming/ingest-auth")
                .header(SERVICE_KEY_HEADER, SERVICE_KEY)
                .header("content-type", "application/json")
                .body(Body::from(
                    serde_json::json!({"kind": "rtmp", "key": "demo-stream-key-a"}).to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = response.into_body();
    let bytes = http_body_util::BodyExt::collect(bytes)
        .await
        .unwrap()
        .to_bytes();
    let body: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(body["data"]["allowed"], true);
    assert_eq!(body["data"]["community_id"], 10);
    assert_eq!(body["data"]["config_id"], 1);
}

#[tokio::test]
async fn ingest_auth_denies_unknown_key() {
    let (app, _engine) = build_app().await;
    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/internal/streaming/ingest-auth")
                .header(SERVICE_KEY_HEADER, SERVICE_KEY)
                .header("content-type", "application/json")
                .body(Body::from(
                    serde_json::json!({"kind": "rtmp", "key": "no-such-key"}).to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = http_body_util::BodyExt::collect(response.into_body())
        .await
        .unwrap()
        .to_bytes();
    let body: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(body["data"]["allowed"], false);
    assert!(body["data"]["community_id"].is_null());
}

#[tokio::test]
async fn ingest_auth_denies_disabled_config() {
    let (app, _engine) = build_app().await;
    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/internal/streaming/ingest-auth")
                .header(SERVICE_KEY_HEADER, SERVICE_KEY)
                .header("content-type", "application/json")
                .body(Body::from(
                    serde_json::json!({"kind": "rtmp", "key": "demo-stream-key-disabled"})
                        .to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    let bytes = http_body_util::BodyExt::collect(response.into_body())
        .await
        .unwrap()
        .to_bytes();
    let body: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(body["data"]["allowed"], false);
}

#[tokio::test]
async fn list_pipelines_without_service_key_is_401() {
    let (app, _engine) = build_app().await;
    let response = app
        .oneshot(
            Request::builder()
                .uri("/internal/streaming/pipelines")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn list_pipelines_reports_only_running_configs() {
    let (app, engine) = build_app().await;
    // Config 1 (community 10) is "started" directly against the engine,
    // bypassing the JWT-gated `/start` route -- this test is scoped to
    // `list_pipelines`'s own filtering logic, not the full lifecycle.
    // Mirrors `crate::api::common::pipeline_id_for_config` (private to
    // `src/api`, not part of this crate's public surface): a stable
    // `PipelineId` derived from `streaming_configs.id`.
    let id = uuid::Uuid::from_u128(1u128);
    engine
        .start(svc_streaming::pipeline::PipelineSpec {
            id,
            tenant: "tenant-abc".into(),
            community_id: "10".into(),
            inputs: vec![],
            profiles: vec![],
            outputs: vec![],
        })
        .await
        .unwrap();

    let response = app
        .oneshot(
            Request::builder()
                .uri("/internal/streaming/pipelines")
                .header(SERVICE_KEY_HEADER, SERVICE_KEY)
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = http_body_util::BodyExt::collect(response.into_body())
        .await
        .unwrap()
        .to_bytes();
    let body: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    let running = body["data"].as_array().unwrap();
    assert_eq!(running.len(), 1);
    assert_eq!(running[0]["config_id"], 1);
    assert_eq!(running[0]["community_id"], 10);
    assert_eq!(running[0]["status"]["state"], "running");
}

async fn ingest_auth_allowed(app: &axum::Router, key: &str) -> bool {
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/internal/streaming/ingest-auth")
                .header(SERVICE_KEY_HEADER, SERVICE_KEY)
                .header("content-type", "application/json")
                .body(Body::from(
                    serde_json::json!({"kind": "rtmp", "key": key}).to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = http_body_util::BodyExt::collect(response.into_body())
        .await
        .unwrap()
        .to_bytes();
    let body: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    body["data"]["allowed"].as_bool().unwrap()
}

#[tokio::test]
async fn ingest_auth_refuses_empty_and_short_keys_even_when_source_url_is_empty() {
    // Config created the way hub-api / the REST API creates it: source_url = '' (and here a
    // matching empty-string "hash" row would be the legacy hazard). An empty key must never match.
    let db = seed_db().await;
    db.execute_unprepared(
        "INSERT INTO streaming_configs (id, community_id, source_url, enabled) VALUES (3, 30, '', 1)",
    )
    .await
    .unwrap();
    let engine: SharedEngine = std::sync::Arc::new(FakeEngine::default());
    let app = router_for_testing(db, engine).with_state(test_state());
    assert!(!ingest_auth_allowed(&app, "").await);
    assert!(!ingest_auth_allowed(&app, "short").await);
    assert!(!ingest_auth_allowed(&app, "0123456789abcde").await); // 15 chars: one below the minimum
}

#[tokio::test]
async fn ingest_auth_no_longer_matches_plaintext_source_url() {
    // The fixture's plaintext key sits in source_url; only its hash column authorizes.
    let db = seed_db().await;
    db.execute_unprepared("UPDATE streaming_configs SET stream_key_hash = NULL WHERE id = 1")
        .await
        .unwrap();
    let engine: SharedEngine = std::sync::Arc::new(FakeEngine::default());
    let app = router_for_testing(db, engine).with_state(test_state());
    assert!(!ingest_auth_allowed(&app, "demo-stream-key-a").await);
}

#[tokio::test]
async fn ingest_auth_valid_key_accepted_and_old_key_refused_after_rotate() {
    let db = seed_db().await;
    let engine: SharedEngine = std::sync::Arc::new(FakeEngine::default());
    let app = router_for_testing(db.clone(), engine).with_state(test_state());
    let old = "demo-stream-key-a";
    let new = "rotated-key-0123456789";
    assert!(ingest_auth_allowed(&app, old).await);

    // What hub-api's rotate does: overwrite the hash column (hex sha256 of the new key).
    db.execute_unprepared(&format!(
        "UPDATE streaming_configs SET stream_key_hash = '{}' WHERE id = 1",
        svc_streaming::stream_key::hash(new)
    ))
    .await
    .unwrap();
    assert!(!ingest_auth_allowed(&app, old).await);
    assert!(ingest_auth_allowed(&app, new).await);
}
