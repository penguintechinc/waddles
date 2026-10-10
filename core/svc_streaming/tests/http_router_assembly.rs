//! S12 integration test: exercises `svc_streaming::http::router` -- the
//! fully assembled app, not `crate::api::router_for_testing` -- hitting one
//! route from every mount: public health, the JWT-gated community `/api/v1`
//! surface, the `ServiceKey`-gated `/api/v1/internal/*` surface (proving it
//! is *not* also wrapped by the JWT layer -- the gap `crate::api::internal`'s
//! module doc used to flag), the public HLS serving router, and the WHIP/
//! WHEP WebRTC signaling routers.
//!
//! Seeds `svc_streaming::db`'s process-global lazy connection singleton
//! with an in-memory sqlite DB *before* any request is made (this test
//! file is its own process/binary, so the singleton starts empty) --
//! `crate::api::internal_router`'s handlers extract `DbConn` before
//! `ServiceKey`, so a real DB connection has to succeed for the
//! auth-layering assertion below to be meaningful rather than a false
//! positive from an earlier `ApiError::Internal` short-circuit.

use axum::body::Body;
use axum::http::header::{AUTHORIZATION, CONTENT_TYPE};
use axum::http::{Request, StatusCode};
use clap::Parser;
use http_body_util::BodyExt;
use jsonwebtoken::{encode, Algorithm, EncodingKey, Header};
use sea_orm::ConnectionTrait;
use tower::ServiceExt;

use svc_streaming::config::{CliConfig, Config, Secret};
use svc_streaming::http::auth::{Claims, SERVICE_KEY_HEADER};
use svc_streaming::http::{router, AppState};

const HMAC_SECRET: &str = "test-hmac-secret";
const SERVICE_KEY: &str = "test-service-key";

async fn seed_db_singleton(config: &Config) {
    // SAFETY: single test in this process, before any await point that
    // could race another thread's env access -- this file has exactly one
    // `#[tokio::test]`.
    unsafe { std::env::set_var("DB_TYPE", "sqlite") };
    let db = svc_streaming::db::get_or_connect(config)
        .await
        .expect("lazy sqlite connection establishes");
    db.execute_unprepared(
        r#"
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
        "#,
    )
    .await
    .expect("seed schema");
}

fn test_config() -> Config {
    let cli = CliConfig::try_parse_from([
        "svc-streaming",
        "--db-name",
        &std::env::temp_dir()
            .join(format!(
                "svc-streaming-http-router-assembly-{}.sqlite",
                std::process::id()
            ))
            .to_string_lossy(),
    ])
    .expect("defaults parse");
    Config {
        cli,
        db_password: Secret::new("db-pass"),
        cache_password: None,
        service_api_key: Secret::new(SERVICE_KEY),
        jwt_hmac_secret: Some(Secret::new(HMAC_SECRET)),
    }
}

fn sign_token(claims: &Claims) -> String {
    encode(
        &Header::new(Algorithm::HS256),
        claims,
        &EncodingKey::from_secret(HMAC_SECRET.as_bytes()),
    )
    .unwrap()
}

fn valid_claims(config: &Config) -> Claims {
    let now = chrono::Utc::now().timestamp();
    Claims {
        sub: "user-123".into(),
        iss: config.cli.jwt_issuer.clone(),
        aud: config.cli.jwt_audience.clone(),
        iat: now,
        exp: now + 3600,
        scope: "streaming:read".into(),
        tenant: "tenant-abc".into(),
        teams: vec![],
        roles: vec![],
    }
}

#[tokio::test]
async fn every_mount_is_reachable_through_the_assembled_router() {
    let config = test_config();
    seed_db_singleton(&config).await;
    let token = sign_token(&valid_claims(&config));

    let state = AppState::new(config, prometheus::Registry::new());
    let app = router(state);

    // 1. Public mount: /health, no auth.
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
    assert_eq!(response.status(), StatusCode::OK, "/health must be public");

    // 2. Community mount: JWT-gated /api/v1/* (crate::api::router). The
    // bare placeholder route returns 501 once authenticated -- proves the
    // community router is nested and its JWT route_layer accepts a valid
    // token.
    let response = app
        .clone()
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

    // 3. Internal mount: ServiceKey-gated /api/v1/internal/*
    // (crate::api::internal_router), reached with ONLY X-Service-Key and
    // *no* Authorization header at all -- this is the regression check for
    // the fix: before S12, this whole subtree was also wrapped in the JWT
    // route_layer, so a request with no bearer token 401'd here regardless
    // of a correct service key. A 200 (not 401) proves the two routers were
    // actually split into separate nests.
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/internal/streaming/ingest-auth")
                .header(SERVICE_KEY_HEADER, SERVICE_KEY)
                .header(CONTENT_TYPE, "application/json")
                .body(Body::from(
                    serde_json::json!({"kind": "rtmp", "key": "no-such-key"}).to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(
        response.status(),
        StatusCode::OK,
        "internal route must be reachable with only X-Service-Key, no JWT"
    );
    let body = response.into_body().collect().await.unwrap().to_bytes();
    let parsed: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(parsed["data"]["allowed"], false);

    // 4. HLS mount: public GET /live/{community_id} (crate::egress::hls).
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/live/community-1")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let body = response.into_body().collect().await.unwrap().to_bytes();
    let parsed: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(
        parsed["pipelines"].as_array().unwrap().len(),
        0,
        "default AppState has no registered pipelines"
    );

    // 5. WHIP mount: POST /whip/{token} (crate::ingest::whip). No real
    // authorizer backend is reachable in this test, so the handler reaches
    // its own `ApiError::Internal` -- the point is that it is *reached at
    // all* (any well-formed JSON error envelope, not axum's routeless 404).
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/whip/some-token")
                .header(CONTENT_TYPE, "application/sdp")
                .body(Body::from(
                    "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n",
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_ne!(
        response.status(),
        StatusCode::NOT_FOUND,
        "/whip/{{token}} must be mounted"
    );
    let body = response.into_body().collect().await.unwrap().to_bytes();
    let parsed: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert!(
        parsed.get("error").is_some(),
        "expected the ApiError JSON envelope, got: {parsed}"
    );

    // 6. WHEP mount: POST /whep/{community_id}/{pipeline_id}
    // (crate::egress::whep) -- deterministic 404 (no source registered),
    // via the ApiError envelope (not axum's routeless 404).
    let pipeline_id = uuid::Uuid::new_v4();
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!("/whep/community-1/{pipeline_id}"))
                .header(CONTENT_TYPE, "application/sdp")
                .body(Body::from("v=0\r\n"))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::NOT_FOUND);
    let body = response.into_body().collect().await.unwrap().to_bytes();
    let parsed: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(parsed["error"], "not_found");
}
