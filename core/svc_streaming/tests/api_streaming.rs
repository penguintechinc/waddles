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
}

impl PipelineEngine for FakeEngine {
    async fn start(&self, spec: PipelineSpec) -> Result<PipelineHandle, PipelineError> {
        let status = PipelineStatus {
            id: spec.id,
            state: PipelineState::Running,
            detail: None,
        };
        self.state.lock().unwrap().insert(spec.id, status);
        Ok(PipelineHandle { id: spec.id })
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
