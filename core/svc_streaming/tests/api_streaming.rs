//! Integration tests for the `/communities/{cid}/streaming/*` and
//! `/communities/{cid}/live-channels` control-plane routes -- exercised
//! through `svc_streaming::api::router_for_testing` (a real axum `Router`,
//! not handler functions directly) against an in-memory sqlite SeaORM DB
//! seeded with DDL matching migration 079 plus the minimal `tenants`/
//! `communities`/`community_servers` slice `crate::api::tenancy` reads, and
//! a fake `PipelineEngine`.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use axum::body::Body;
use axum::http::header::AUTHORIZATION;
use axum::http::{Request, StatusCode};
use clap::Parser;
use http_body_util::BodyExt;
use jsonwebtoken::{encode, Algorithm, EncodingKey, Header};
use sea_orm::{ConnectionTrait, Database, DatabaseConnection};
use tower::ServiceExt;

use svc_streaming::api::{router_for_testing, SharedEngine};
use svc_streaming::config::{CliConfig, Config, Secret};
use svc_streaming::http::auth::Claims;
use svc_streaming::http::AppState;
use svc_streaming::pipeline::{
    PipelineEngine, PipelineError, PipelineHandle, PipelineId, PipelineSpec, PipelineState,
    PipelineStatus,
};

const HMAC_SECRET: &str = "test-hmac-secret";
/// Seeded in `tenants`/`communities` below: tenant id 1 ("tenant-abc") owns
/// community 10; community 20 belongs to a different tenant (id 2) and has
/// no matching row in `tenants`, exercising the cross-tenant 403 path.
const OWNED_COMMUNITY: i32 = 10;
const OTHER_TENANT_COMMUNITY: i32 = 20;

async fn seed_db() -> DatabaseConnection {
    let db = Database::connect("sqlite::memory:")
        .await
        .expect("connect in-memory sqlite");
    let schema = r#"
        CREATE TABLE tenants (id INTEGER PRIMARY KEY, slug TEXT NOT NULL UNIQUE);
        CREATE TABLE communities (id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL);
        CREATE TABLE community_servers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            community_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            platform_server_id TEXT NOT NULL,
            platform_server_name TEXT
        );
        CREATE TABLE streaming_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            community_id INTEGER NOT NULL UNIQUE,
            source_url TEXT NOT NULL,
            source_type TEXT NOT NULL DEFAULT 'rtmp',
            enabled INTEGER NOT NULL DEFAULT 1,
            record_enabled INTEGER NOT NULL DEFAULT 0,
            transcode_enabled INTEGER NOT NULL DEFAULT 0,
            transcode_bitrate_kbps INTEGER NOT NULL DEFAULT 4000,
            video_codec TEXT NOT NULL DEFAULT 'h264',
            audio_codec TEXT NOT NULL DEFAULT 'copy'
        );
        CREATE TABLE streaming_targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            config_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            forward_url TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            protocol TEXT NOT NULL DEFAULT 'rtmp',
            video_codec TEXT,
            audio_codec TEXT
        );
        INSERT INTO tenants (id, slug) VALUES (1, 'tenant-abc');
        INSERT INTO communities (id, tenant_id) VALUES (10, 1);
        INSERT INTO communities (id, tenant_id) VALUES (20, 2);
        INSERT INTO community_servers (community_id, platform, platform_server_id, platform_server_name)
            VALUES (10, 'twitch', 'twitch-123', 'Cool Channel');
    "#;
    for stmt in schema.split(';') {
        let stmt = stmt.trim();
        if !stmt.is_empty() {
            db.execute_unprepared(stmt)
                .await
                .unwrap_or_else(|err| panic!("seed statement failed ({stmt:?}): {err}"));
        }
    }
    db
}

/// In-memory fake [`PipelineEngine`] -- `start` records `Running`, `stop`
/// flips to `Stopped`, `status` reports whatever was last recorded (or
/// `NotFound` for an id never started).
#[derive(Clone, Default)]
struct FakeEngine {
    state: Arc<Mutex<HashMap<PipelineId, PipelineStatus>>>,
    /// Every spec `start` was called with, in order -- lets a test assert
    /// on the codecs/outputs the API actually built.
    specs: Arc<Mutex<Vec<PipelineSpec>>>,
}

impl PipelineEngine for FakeEngine {
    async fn start(&self, spec: PipelineSpec) -> Result<PipelineHandle, PipelineError> {
        let status = PipelineStatus {
            id: spec.id,
            state: PipelineState::Running,
            detail: None,
        };
        self.state.lock().unwrap().insert(spec.id, status);
        let id = spec.id;
        self.specs.lock().unwrap().push(spec);
        Ok(PipelineHandle { id })
    }

    async fn stop(&self, id: PipelineId) -> Result<(), PipelineError> {
        if let Some(status) = self.state.lock().unwrap().get_mut(&id) {
            status.state = PipelineState::Stopped;
        }
        Ok(())
    }

    async fn status(&self, id: PipelineId) -> Result<PipelineStatus, PipelineError> {
        self.state
            .lock()
            .unwrap()
            .get(&id)
            .cloned()
            .ok_or(PipelineError::NotFound(id))
    }
}

fn fake_engine() -> SharedEngine {
    Arc::new(FakeEngine::default())
}

fn test_state() -> AppState {
    let cli = CliConfig::try_parse_from(["svc-streaming"]).expect("defaults parse");
    let config = Config {
        cli,
        db_password: Secret::new("db-pass"),
        cache_password: None,
        service_api_key: Secret::new("service-key"),
        jwt_hmac_secret: Some(Secret::new(HMAC_SECRET)),
    };
    AppState::new(config, prometheus::Registry::new())
}

async fn build_app() -> (axum::Router, DatabaseConnection, SharedEngine) {
    let db = seed_db().await;
    let engine = fake_engine();
    let app = router_for_testing(db.clone(), engine.clone()).with_state(test_state());
    (app, db, engine)
}

fn sign_token(claims: &Claims) -> String {
    encode(
        &Header::new(Algorithm::HS256),
        claims,
        &EncodingKey::from_secret(HMAC_SECRET.as_bytes()),
    )
    .unwrap()
}

fn claims_for_tenant(state: &AppState, tenant: &str) -> Claims {
    let now = chrono::Utc::now().timestamp();
    Claims {
        sub: "user-123".into(),
        iss: state.config.cli.jwt_issuer.clone(),
        aud: state.config.cli.jwt_audience.clone(),
        iat: now,
        exp: now + 3600,
        scope: "streaming:read".into(),
        tenant: tenant.into(),
        teams: vec![],
        roles: vec![],
    }
}

fn bearer_for_owned_tenant() -> String {
    let token = sign_token(&claims_for_tenant(&test_state(), "tenant-abc"));
    format!("Bearer {token}")
}

async fn body_json(response: axum::response::Response) -> serde_json::Value {
    let bytes = response.into_body().collect().await.unwrap().to_bytes();
    serde_json::from_slice(&bytes).unwrap()
}

fn configs_path(cid: i32) -> String {
    format!("/communities/{cid}/streaming/configs")
}

async fn create_config(
    app: &axum::Router,
    cid: i32,
    body: serde_json::Value,
) -> axum::response::Response {
    app.clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(configs_path(cid))
                .header(AUTHORIZATION, bearer_for_owned_tenant())
                .header("content-type", "application/json")
                .body(Body::from(body.to_string()))
                .unwrap(),
        )
        .await
        .unwrap()
}

#[tokio::test]
async fn list_configs_without_auth_is_401() {
    let (app, ..) = build_app().await;
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .uri(configs_path(OWNED_COMMUNITY))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn list_configs_wrong_tenant_is_403() {
    let (app, ..) = build_app().await;
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .uri(configs_path(OTHER_TENANT_COMMUNITY))
                .header(AUTHORIZATION, bearer_for_owned_tenant())
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::FORBIDDEN);
}

#[tokio::test]
async fn list_configs_unknown_community_is_403_not_404() {
    let (app, ..) = build_app().await;
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .uri(configs_path(999_999))
                .header(AUTHORIZATION, bearer_for_owned_tenant())
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    // Must not leak whether the community exists in another tenant.
    assert_eq!(response.status(), StatusCode::FORBIDDEN);
}

#[tokio::test]
async fn create_then_get_config_round_trips() {
    let (app, ..) = build_app().await;
    let created = create_config(
        &app,
        OWNED_COMMUNITY,
        serde_json::json!({"source_url": "rtmp://ingest.example/live"}),
    )
    .await;
    assert_eq!(created.status(), StatusCode::CREATED);
    let body = body_json(created).await;
    assert_eq!(body["status"], "success");
    assert_eq!(body["data"]["source_type"], "rtmp");
    assert_eq!(body["data"]["transcode_bitrate_kbps"], 4000);
    // A client that never heard of codec selection gets today's behaviour.
    assert_eq!(body["data"]["video_codec"], "h264");
    assert_eq!(body["data"]["audio_codec"], "copy");
    let config_id = body["data"]["id"].as_i64().unwrap();

    let fetched = app
        .clone()
        .oneshot(
            Request::builder()
                .uri(format!("{}/{config_id}", configs_path(OWNED_COMMUNITY)))
                .header(AUTHORIZATION, bearer_for_owned_tenant())
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(fetched.status(), StatusCode::OK);
    let body = body_json(fetched).await;
    assert_eq!(body["data"]["source_url"], "rtmp://ingest.example/live");
}

#[tokio::test]
async fn create_config_twice_is_bad_request() {
    let (app, ..) = build_app().await;
    let first = create_config(
        &app,
        OWNED_COMMUNITY,
        serde_json::json!({"source_url": "rtmp://a"}),
    )
    .await;
    assert_eq!(first.status(), StatusCode::CREATED);
    let second = create_config(
        &app,
        OWNED_COMMUNITY,
        serde_json::json!({"source_url": "rtmp://b"}),
    )
    .await;
    assert_eq!(second.status(), StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn create_config_invalid_source_type_is_bad_request() {
    let (app, ..) = build_app().await;
    let response = create_config(
        &app,
        OWNED_COMMUNITY,
        serde_json::json!({"source_url": "rtmp://a", "source_type": "webrtc"}),
    )
    .await;
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn create_config_invalid_bitrate_is_bad_request() {
    let (app, ..) = build_app().await;
    let response = create_config(
        &app,
        OWNED_COMMUNITY,
        serde_json::json!({"source_url": "rtmp://a", "transcode_bitrate_kbps": 0}),
    )
    .await;
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn update_config_invalid_bitrate_is_bad_request() {
    let (app, ..) = build_app().await;
    let config_id = create_config_and_id(&app).await;
    let response = app
        .oneshot(
            Request::builder()
                .method("PUT")
                .uri(format!("{}/{config_id}", configs_path(OWNED_COMMUNITY)))
                .header(AUTHORIZATION, bearer_for_owned_tenant())
                .header("content-type", "application/json")
                .body(Body::from(
                    serde_json::json!({"transcode_bitrate_kbps": -1}).to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn update_config_invalid_source_type_is_bad_request() {
    let (app, ..) = build_app().await;
    let config_id = create_config_and_id(&app).await;
    let response = app
        .oneshot(
            Request::builder()
                .method("PUT")
                .uri(format!("{}/{config_id}", configs_path(OWNED_COMMUNITY)))
                .header(AUTHORIZATION, bearer_for_owned_tenant())
                .header("content-type", "application/json")
                .body(Body::from(
                    serde_json::json!({"source_type": "webrtc"}).to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn update_config_partial_fields() {
    let (app, ..) = build_app().await;
    let created = create_config(
        &app,
        OWNED_COMMUNITY,
        serde_json::json!({"source_url": "rtmp://a"}),
    )
    .await;
    let body = body_json(created).await;
    let config_id = body["data"]["id"].as_i64().unwrap();

    let updated = app
        .clone()
        .oneshot(
            Request::builder()
                .method("PUT")
                .uri(format!("{}/{config_id}", configs_path(OWNED_COMMUNITY)))
                .header(AUTHORIZATION, bearer_for_owned_tenant())
                .header("content-type", "application/json")
                .body(Body::from(
                    serde_json::json!({"enabled": false, "transcode_bitrate_kbps": 6000})
                        .to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(updated.status(), StatusCode::OK);
    let body = body_json(updated).await;
    assert_eq!(body["data"]["enabled"], false);
    assert_eq!(body["data"]["transcode_bitrate_kbps"], 6000);
    // Untouched field survives the partial update.
    assert_eq!(body["data"]["source_url"], "rtmp://a");
}

#[tokio::test]
async fn delete_config_then_get_is_not_found() {
    let (app, ..) = build_app().await;
    let created = create_config(
        &app,
        OWNED_COMMUNITY,
        serde_json::json!({"source_url": "rtmp://a"}),
    )
    .await;
    let body = body_json(created).await;
    let config_id = body["data"]["id"].as_i64().unwrap();

    let deleted = app
        .clone()
        .oneshot(
            Request::builder()
                .method("DELETE")
                .uri(format!("{}/{config_id}", configs_path(OWNED_COMMUNITY)))
                .header(AUTHORIZATION, bearer_for_owned_tenant())
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(deleted.status(), StatusCode::OK);

    let refetched = app
        .clone()
        .oneshot(
            Request::builder()
                .uri(format!("{}/{config_id}", configs_path(OWNED_COMMUNITY)))
                .header(AUTHORIZATION, bearer_for_owned_tenant())
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(refetched.status(), StatusCode::NOT_FOUND);
}

async fn create_config_and_id(app: &axum::Router) -> i64 {
    let created = create_config(
        app,
        OWNED_COMMUNITY,
        serde_json::json!({"source_url": "rtmp://a"}),
    )
    .await;
    let body = body_json(created).await;
    body["data"]["id"].as_i64().unwrap()
}

#[tokio::test]
async fn add_target_with_env_secret_ref_succeeds_and_never_returns_a_raw_url() {
    let (app, ..) = build_app().await;
    let config_id = create_config_and_id(&app).await;

    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!(
                    "{}/{config_id}/targets",
                    configs_path(OWNED_COMMUNITY)
                ))
                .header(AUTHORIZATION, bearer_for_owned_tenant())
                .header("content-type", "application/json")
                .body(Body::from(
                    serde_json::json!({
                        "platform": "twitch",
                        "url_secret_ref": {"source": "env", "var": "TWITCH_RELAY_URL"}
                    })
                    .to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::CREATED);
    let body = body_json(response).await;
    assert_eq!(body["data"]["url_secret_ref"]["source"], "env");
    assert_eq!(body["data"]["url_secret_ref"]["var"], "TWITCH_RELAY_URL");

    let listed = app
        .clone()
        .oneshot(
            Request::builder()
                .uri(format!(
                    "{}/{config_id}/targets",
                    configs_path(OWNED_COMMUNITY)
                ))
                .header(AUTHORIZATION, bearer_for_owned_tenant())
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let body = body_json(listed).await;
    let targets = body["data"].as_array().unwrap();
    assert_eq!(targets.len(), 1);
    // The response never carries a `forward_url`/raw-URL field.
    assert!(targets[0].get("forward_url").is_none());
}

#[tokio::test]
async fn add_target_rejects_inline_forward_url_field() {
    let (app, ..) = build_app().await;
    let config_id = create_config_and_id(&app).await;

    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!(
                    "{}/{config_id}/targets",
                    configs_path(OWNED_COMMUNITY)
                ))
                .header(AUTHORIZATION, bearer_for_owned_tenant())
                .header("content-type", "application/json")
                .body(Body::from(
                    serde_json::json!({
                        "platform": "custom",
                        "url_secret_ref": {"source": "env", "var": "X"},
                        "forward_url": "rtmp://evil.example/stream?key=secret"
                    })
                    .to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn add_target_rejects_bare_url_string_as_secret_ref() {
    let (app, ..) = build_app().await;
    let config_id = create_config_and_id(&app).await;

    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!(
                    "{}/{config_id}/targets",
                    configs_path(OWNED_COMMUNITY)
                ))
                .header(AUTHORIZATION, bearer_for_owned_tenant())
                .header("content-type", "application/json")
                .body(Body::from(
                    serde_json::json!({
                        "platform": "custom",
                        "url_secret_ref": "rtmp://evil.example/stream?key=secret"
                    })
                    .to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn add_target_invalid_platform_is_bad_request() {
    let (app, ..) = build_app().await;
    let config_id = create_config_and_id(&app).await;

    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!(
                    "{}/{config_id}/targets",
                    configs_path(OWNED_COMMUNITY)
                ))
                .header(AUTHORIZATION, bearer_for_owned_tenant())
                .header("content-type", "application/json")
                .body(Body::from(
                    serde_json::json!({
                        "platform": "myspace",
                        "url_secret_ref": {"source": "env", "var": "X"}
                    })
                    .to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn remove_target_then_list_is_empty() {
    let (app, ..) = build_app().await;
    let config_id = create_config_and_id(&app).await;

    let added = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!(
                    "{}/{config_id}/targets",
                    configs_path(OWNED_COMMUNITY)
                ))
                .header(AUTHORIZATION, bearer_for_owned_tenant())
                .header("content-type", "application/json")
                .body(Body::from(
                    serde_json::json!({
                        "platform": "custom",
                        "url_secret_ref": {"source": "file", "path": "/var/secrets/x"}
                    })
                    .to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    let body = body_json(added).await;
    let target_id = body["data"]["id"].as_i64().unwrap();

    let removed = app
        .clone()
        .oneshot(
            Request::builder()
                .method("DELETE")
                .uri(format!(
                    "/communities/{OWNED_COMMUNITY}/streaming/targets/{target_id}"
                ))
                .header(AUTHORIZATION, bearer_for_owned_tenant())
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(removed.status(), StatusCode::OK);

    let listed = app
        .clone()
        .oneshot(
            Request::builder()
                .uri(format!(
                    "{}/{config_id}/targets",
                    configs_path(OWNED_COMMUNITY)
                ))
                .header(AUTHORIZATION, bearer_for_owned_tenant())
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let body = body_json(listed).await;
    assert_eq!(body["data"].as_array().unwrap().len(), 0);
}

#[tokio::test]
async fn start_then_status_reports_running() {
    let (app, ..) = build_app().await;
    let config_id = create_config_and_id(&app).await;

    let started = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!(
                    "{}/{config_id}/start",
                    configs_path(OWNED_COMMUNITY)
                ))
                .header(AUTHORIZATION, bearer_for_owned_tenant())
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(started.status(), StatusCode::OK);
    let body = body_json(started).await;
    assert_eq!(body["data"]["state"], "running");

    let status = app
        .clone()
        .oneshot(
            Request::builder()
                .uri(format!(
                    "{}/{config_id}/status",
                    configs_path(OWNED_COMMUNITY)
                ))
                .header(AUTHORIZATION, bearer_for_owned_tenant())
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(status.status(), StatusCode::OK);
    let body = body_json(status).await;
    assert_eq!(body["data"]["state"], "running");
}

#[tokio::test]
async fn stop_reports_stopped() {
    let (app, ..) = build_app().await;
    let config_id = create_config_and_id(&app).await;

    app.clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!(
                    "{}/{config_id}/start",
                    configs_path(OWNED_COMMUNITY)
                ))
                .header(AUTHORIZATION, bearer_for_owned_tenant())
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    let stopped = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!(
                    "{}/{config_id}/stop",
                    configs_path(OWNED_COMMUNITY)
                ))
                .header(AUTHORIZATION, bearer_for_owned_tenant())
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(stopped.status(), StatusCode::OK);
    let body = body_json(stopped).await;
    assert_eq!(body["data"]["state"], "stopped");
}

#[tokio::test]
async fn status_before_start_is_not_found() {
    let (app, ..) = build_app().await;
    let config_id = create_config_and_id(&app).await;

    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .uri(format!(
                    "{}/{config_id}/status",
                    configs_path(OWNED_COMMUNITY)
                ))
                .header(AUTHORIZATION, bearer_for_owned_tenant())
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn live_channels_lists_seeded_community_server() {
    let (app, ..) = build_app().await;
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .uri(format!("/communities/{OWNED_COMMUNITY}/live-channels"))
                .header(AUTHORIZATION, bearer_for_owned_tenant())
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let body = body_json(response).await;
    let channels = body["data"].as_array().unwrap();
    assert_eq!(channels.len(), 1);
    assert_eq!(channels[0]["platform"], "twitch");
    assert_eq!(channels[0]["channel_name"], "Cool Channel");
    assert!(channels[0]["live"].is_null());
}

#[tokio::test]
async fn start_with_transcode_and_record_enabled_and_a_target_succeeds() {
    let (app, ..) = build_app().await;
    let created = create_config(
        &app,
        OWNED_COMMUNITY,
        serde_json::json!({
            "source_url": "rtmp://ingest.example/live",
            "record_enabled": true,
            "transcode_enabled": true,
            "transcode_bitrate_kbps": 8000
        }),
    )
    .await;
    let body = body_json(created).await;
    let config_id = body["data"]["id"].as_i64().unwrap();

    let added = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!(
                    "{}/{config_id}/targets",
                    configs_path(OWNED_COMMUNITY)
                ))
                .header(AUTHORIZATION, bearer_for_owned_tenant())
                .header("content-type", "application/json")
                .body(Body::from(
                    serde_json::json!({
                        "platform": "twitch",
                        "url_secret_ref": {"source": "env", "var": "RELAY_URL"}
                    })
                    .to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(added.status(), StatusCode::CREATED);

    let started = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!(
                    "{}/{config_id}/start",
                    configs_path(OWNED_COMMUNITY)
                ))
                .header(AUTHORIZATION, bearer_for_owned_tenant())
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(started.status(), StatusCode::OK);
    let body = body_json(started).await;
    assert_eq!(body["data"]["state"], "running");
}

// ---------------------------------------------------------------------
// Codec selection (H.265 / AV1), protocol, and loud container rejection
// ---------------------------------------------------------------------

const FLV_MESSAGE: &str =
    "FLV/RTMP cannot carry HEVC/AV1 (needs enhanced-RTMP); use HLS/SRT or H.264";

async fn send_json(
    app: &axum::Router,
    method: &str,
    uri: String,
    body: serde_json::Value,
) -> axum::response::Response {
    app.clone()
        .oneshot(
            Request::builder()
                .method(method)
                .uri(uri)
                .header(AUTHORIZATION, bearer_for_owned_tenant())
                .header("content-type", "application/json")
                .body(Body::from(body.to_string()))
                .unwrap(),
        )
        .await
        .unwrap()
}

/// Creates a config with the given extra JSON fields and returns its id.
async fn config_with(app: &axum::Router, extra: serde_json::Value) -> i64 {
    let mut body = serde_json::json!({"source_url": "rtmp://ingest.example/live"});
    body.as_object_mut()
        .unwrap()
        .extend(extra.as_object().unwrap().clone());
    let created = create_config(app, OWNED_COMMUNITY, body).await;
    assert_eq!(created.status(), StatusCode::CREATED);
    body_json(created).await["data"]["id"].as_i64().unwrap()
}

async fn add_target(
    app: &axum::Router,
    config_id: i64,
    extra: serde_json::Value,
) -> axum::response::Response {
    let mut body = serde_json::json!({
        "platform": "custom",
        "url_secret_ref": {"source": "env", "var": "RELAY_URL"}
    });
    body.as_object_mut()
        .unwrap()
        .extend(extra.as_object().unwrap().clone());
    send_json(
        app,
        "POST",
        format!("{}/{config_id}/targets", configs_path(OWNED_COMMUNITY)),
        body,
    )
    .await
}

async fn error_message(response: axum::response::Response) -> String {
    body_json(response).await["message"]
        .as_str()
        .expect("error body carries a message")
        .to_string()
}

#[tokio::test]
async fn create_config_accepts_h265_with_transcoding_and_canonicalizes_the_name() {
    let (app, ..) = build_app().await;
    let created = create_config(
        &app,
        OWNED_COMMUNITY,
        serde_json::json!({
            "source_url": "rtmp://ingest.example/live",
            "transcode_enabled": true,
            "video_codec": "HEVC",
            "audio_codec": "Opus"
        }),
    )
    .await;
    assert_eq!(created.status(), StatusCode::CREATED);
    let body = body_json(created).await;
    assert_eq!(body["data"]["video_codec"], "h265");
    assert_eq!(body["data"]["audio_codec"], "opus");
}

#[tokio::test]
async fn create_config_with_a_codec_but_no_transcoding_is_rejected() {
    let (app, ..) = build_app().await;
    for extra in [
        serde_json::json!({"video_codec": "av1"}),
        serde_json::json!({"audio_codec": "aac"}),
    ] {
        let mut body = serde_json::json!({"source_url": "rtmp://x"});
        body.as_object_mut()
            .unwrap()
            .extend(extra.as_object().unwrap().clone());
        let response = create_config(&app, OWNED_COMMUNITY, body).await;
        assert_eq!(response.status(), StatusCode::BAD_REQUEST);
        assert!(error_message(response).await.contains("transcode_enabled"));
    }
}

#[tokio::test]
async fn create_config_with_an_unknown_codec_is_rejected_not_defaulted() {
    let (app, ..) = build_app().await;
    for extra in [
        serde_json::json!({"transcode_enabled": true, "video_codec": "vp9"}),
        serde_json::json!({"transcode_enabled": true, "audio_codec": "flac"}),
    ] {
        let mut body = serde_json::json!({"source_url": "rtmp://x"});
        body.as_object_mut()
            .unwrap()
            .extend(extra.as_object().unwrap().clone());
        let response = create_config(&app, OWNED_COMMUNITY, body).await;
        assert_eq!(response.status(), StatusCode::BAD_REQUEST);
        assert!(error_message(response).await.contains("expected one of"));
    }
}

#[tokio::test]
async fn update_config_switches_codec_and_canonicalizes() {
    let (app, ..) = build_app().await;
    let config_id = config_with(&app, serde_json::json!({"transcode_enabled": true})).await;
    let response = send_json(
        &app,
        "PUT",
        format!("{}/{config_id}", configs_path(OWNED_COMMUNITY)),
        serde_json::json!({"video_codec": "AV1", "audio_codec": "opus"}),
    )
    .await;
    assert_eq!(response.status(), StatusCode::OK);
    let body = body_json(response).await;
    assert_eq!(body["data"]["video_codec"], "av1");
    assert_eq!(body["data"]["audio_codec"], "opus");
}

#[tokio::test]
async fn update_config_cannot_turn_transcoding_off_while_a_codec_is_selected() {
    let (app, ..) = build_app().await;
    let config_id = config_with(
        &app,
        serde_json::json!({"transcode_enabled": true, "video_codec": "h265"}),
    )
    .await;
    let response = send_json(
        &app,
        "PUT",
        format!("{}/{config_id}", configs_path(OWNED_COMMUNITY)),
        serde_json::json!({"transcode_enabled": false}),
    )
    .await;
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    // Turning it off together with resetting the codec is fine.
    let ok = send_json(
        &app,
        "PUT",
        format!("{}/{config_id}", configs_path(OWNED_COMMUNITY)),
        serde_json::json!({"transcode_enabled": false, "video_codec": "h264"}),
    )
    .await;
    assert_eq!(ok.status(), StatusCode::OK);
}

#[tokio::test]
async fn update_config_to_av1_is_refused_while_an_srt_target_would_inherit_it() {
    let (app, ..) = build_app().await;
    let config_id = config_with(
        &app,
        serde_json::json!({"transcode_enabled": true, "video_codec": "h265"}),
    )
    .await;
    let added = add_target(&app, config_id, serde_json::json!({"protocol": "srt"})).await;
    assert_eq!(added.status(), StatusCode::CREATED);

    let response = send_json(
        &app,
        "PUT",
        format!("{}/{config_id}", configs_path(OWNED_COMMUNITY)),
        serde_json::json!({"video_codec": "av1"}),
    )
    .await;
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    let message = error_message(response).await;
    assert!(
        message.contains("MPEG-TS/SRT cannot carry AV1"),
        "{message}"
    );
    assert!(
        message.contains("streaming_target"),
        "names the row: {message}"
    );
}

#[tokio::test]
async fn add_target_defaults_to_rtmp_with_no_codec_override() {
    let (app, ..) = build_app().await;
    let config_id = create_config_and_id(&app).await;
    let response = add_target(&app, config_id, serde_json::json!({})).await;
    assert_eq!(response.status(), StatusCode::CREATED);
    let body = body_json(response).await;
    assert_eq!(body["data"]["protocol"], "rtmp");
    assert!(body["data"]["video_codec"].is_null());
    assert!(body["data"]["audio_codec"].is_null());
}

#[tokio::test]
async fn add_rtmp_target_requesting_hevc_or_av1_is_rejected_loudly() {
    let (app, ..) = build_app().await;
    let config_id = config_with(&app, serde_json::json!({"transcode_enabled": true})).await;
    for codec in ["h265", "HEVC", "av1"] {
        let response = add_target(&app, config_id, serde_json::json!({"video_codec": codec})).await;
        assert_eq!(response.status(), StatusCode::BAD_REQUEST, "{codec}");
        let message = error_message(response).await;
        assert!(message.contains(FLV_MESSAGE), "{codec}: {message}");
        assert!(
            !message.contains("streaming_target"),
            "a not-yet-created row has no id to report: {message}"
        );
    }
    // Nothing was stored by the rejected attempts.
    let listed = app
        .clone()
        .oneshot(
            Request::builder()
                .uri(format!(
                    "{}/{config_id}/targets",
                    configs_path(OWNED_COMMUNITY)
                ))
                .header(AUTHORIZATION, bearer_for_owned_tenant())
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert!(body_json(listed).await["data"]
        .as_array()
        .unwrap()
        .is_empty());
}

#[tokio::test]
async fn add_srt_target_may_carry_hevc_but_not_av1() {
    let (app, ..) = build_app().await;
    let config_id = config_with(&app, serde_json::json!({"transcode_enabled": true})).await;

    let ok = add_target(
        &app,
        config_id,
        serde_json::json!({"protocol": "srt", "video_codec": "h265", "audio_codec": "opus"}),
    )
    .await;
    assert_eq!(ok.status(), StatusCode::CREATED);
    let body = body_json(ok).await;
    assert_eq!(body["data"]["protocol"], "srt");
    assert_eq!(body["data"]["video_codec"], "h265");
    assert_eq!(body["data"]["audio_codec"], "opus");

    let bad = add_target(
        &app,
        config_id,
        serde_json::json!({"protocol": "srt", "video_codec": "av1"}),
    )
    .await;
    assert_eq!(bad.status(), StatusCode::BAD_REQUEST);
    assert!(error_message(bad)
        .await
        .contains("MPEG-TS/SRT cannot carry AV1"));
}

#[tokio::test]
async fn add_rtmp_target_with_opus_audio_is_rejected() {
    let (app, ..) = build_app().await;
    let config_id = config_with(&app, serde_json::json!({"transcode_enabled": true})).await;
    let response = add_target(&app, config_id, serde_json::json!({"audio_codec": "opus"})).await;
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    assert!(error_message(response).await.contains("cannot carry Opus"));
}

#[tokio::test]
async fn add_target_codec_override_without_transcoding_is_rejected() {
    let (app, ..) = build_app().await;
    let config_id = create_config_and_id(&app).await;
    let response = add_target(
        &app,
        config_id,
        serde_json::json!({"protocol": "srt", "video_codec": "h265"}),
    )
    .await;
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    assert!(error_message(response).await.contains("transcode_enabled"));
}

#[tokio::test]
async fn add_target_with_an_unknown_protocol_or_codec_is_rejected() {
    let (app, ..) = build_app().await;
    let config_id = config_with(&app, serde_json::json!({"transcode_enabled": true})).await;
    for extra in [
        serde_json::json!({"protocol": "whip"}),
        serde_json::json!({"video_codec": "vp9"}),
        serde_json::json!({"audio_codec": "flac"}),
    ] {
        let response = add_target(&app, config_id, extra).await;
        assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    }
}

/// Spawns a mock hub-api whose token ledger always admits the debit.
async fn spawn_admitting_ledger() -> String {
    let app = axum::Router::new().route(
        "/api/v1/marketplace/communities/{community_id}/tokens/debit",
        axum::routing::post(|| async {
            axum::Json(serde_json::json!({"success": true, "balance_after": 55}))
        }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    format!("http://{addr}")
}

/// An app whose engine records specs and whose hub-api (token ledger) is
/// `hub_api_url`.
async fn build_app_capturing(hub_api_url: &str) -> (axum::Router, DatabaseConnection, FakeEngine) {
    let db = seed_db().await;
    let engine = FakeEngine::default();
    let cli = CliConfig::try_parse_from(["svc-streaming", "--hub-api-url", hub_api_url])
        .expect("cli parses");
    let state = AppState::new(
        Config {
            cli,
            db_password: Secret::new("db-pass"),
            cache_password: None,
            service_api_key: Secret::new("service-key"),
            jwt_hmac_secret: Some(Secret::new(HMAC_SECRET)),
        },
        prometheus::Registry::new(),
    );
    let shared: SharedEngine = Arc::new(engine.clone());
    let app = router_for_testing(db.clone(), shared).with_state(state);
    (app, db, engine)
}

async fn start_config(app: &axum::Router, config_id: i64) -> axum::response::Response {
    app.clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!(
                    "{}/{config_id}/start",
                    configs_path(OWNED_COMMUNITY)
                ))
                .header(AUTHORIZATION, bearer_for_owned_tenant())
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap()
}

#[tokio::test]
async fn start_with_an_admitted_h265_config_builds_a_codec_ladder_spec() {
    let ledger = spawn_admitting_ledger().await;
    let (app, _db, engine) = build_app_capturing(&ledger).await;
    let config_id = config_with(
        &app,
        serde_json::json!({
            "transcode_enabled": true,
            "video_codec": "h265",
            "audio_codec": "aac",
            "record_enabled": true
        }),
    )
    .await;
    // RTMP inherits nothing video-wise (-> H.264); SRT inherits H.265.
    assert_eq!(
        add_target(&app, config_id, serde_json::json!({}))
            .await
            .status(),
        StatusCode::CREATED
    );
    assert_eq!(
        add_target(&app, config_id, serde_json::json!({"protocol": "srt"}))
            .await
            .status(),
        StatusCode::CREATED
    );

    let started = start_config(&app, config_id).await;
    assert_eq!(started.status(), StatusCode::OK);

    let specs = engine.specs.lock().unwrap();
    let spec = specs.last().expect("engine started");
    assert_eq!(spec.profiles.len(), 2, "default H.265 + an H.264 rung");
    assert_eq!(spec.profiles[0].name, "default");
    assert!(matches!(
        spec.profiles[0].video,
        svc_streaming::pipeline::VideoCodec::H265 { .. }
    ));
    assert!(matches!(
        spec.profiles[0].audio,
        svc_streaming::pipeline::AudioCodec::Aac { .. }
    ));
    assert_eq!(spec.profiles[1].name, "h264-aac");
    assert!(matches!(
        spec.profiles[1].video,
        svc_streaming::pipeline::VideoCodec::H264 { .. }
    ));

    let kinds: Vec<(&str, Option<&str>)> = spec
        .outputs
        .iter()
        .map(|o| match o {
            svc_streaming::pipeline::OutputSpec::RtmpPush { profile, .. } => {
                ("rtmp", profile.as_deref())
            }
            svc_streaming::pipeline::OutputSpec::SrtPush { profile, .. } => {
                ("srt", profile.as_deref())
            }
            svc_streaming::pipeline::OutputSpec::Record { .. } => ("record", None),
            other => panic!("unexpected output {other:?}"),
        })
        .collect();
    assert_eq!(
        kinds,
        vec![
            ("rtmp", Some("h264-aac")),
            ("srt", Some("default")),
            ("record", None)
        ]
    );
}

#[tokio::test]
async fn start_with_an_unreachable_ledger_falls_back_to_passthrough_for_any_codec() {
    // Port 9 (discard) refuses connections: admission is "unavailable".
    let (app, _db, engine) = build_app_capturing("http://127.0.0.1:9").await;
    let config_id = config_with(
        &app,
        serde_json::json!({"transcode_enabled": true, "video_codec": "av1"}),
    )
    .await;
    assert_eq!(start_config(&app, config_id).await.status(), StatusCode::OK);
    let specs = engine.specs.lock().unwrap();
    let spec = specs.last().expect("engine started");
    assert!(matches!(
        spec.profiles[0].video,
        svc_streaming::pipeline::VideoCodec::Copy
    ));
}

#[tokio::test]
async fn start_refuses_a_stored_rtmp_target_that_requests_hevc_with_a_400() {
    let ledger = spawn_admitting_ledger().await;
    let (app, db, engine) = build_app_capturing(&ledger).await;
    let config_id = config_with(&app, serde_json::json!({"transcode_enabled": true})).await;
    // A row that bypassed the API (manual edit / older writer).
    db.execute_unprepared(&format!(
        "INSERT INTO streaming_targets (config_id, platform, forward_url, enabled, protocol, video_codec) \
         VALUES ({config_id}, 'custom', '{{\"source\":\"env\",\"var\":\"X\"}}', 1, 'rtmp', 'h265')"
    ))
    .await
    .expect("insert");

    let response = start_config(&app, config_id).await;
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    assert!(error_message(response).await.contains(FLV_MESSAGE));
    assert!(
        engine.specs.lock().unwrap().is_empty(),
        "the engine must never see an unmuxable spec"
    );
}
