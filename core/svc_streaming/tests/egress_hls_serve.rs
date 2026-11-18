//! Integration tests for the public `hls_router` (issue #287 S7 §2) through
//! the real axum `Router`, not the handler functions directly -- content
//! types, cache-control, path-traversal rejection, and the `/live/
//! {community_id}` listing JSON shape. Content-type/traversal/CORS edge
//! cases not covered here are already exercised by the inline `#[cfg(test)]`
//! module in `src/egress/hls/serve.rs`; this file focuses on end-to-end
//! behavior through the crate's public API only.

use std::sync::Arc;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use time::OffsetDateTime;
use tower::ServiceExt;
use uuid::Uuid;

use svc_streaming::egress::hls::{hls_router, HlsRouterState, RunningPipeline, RunningPipelines};

struct FakeRegistry(Vec<RunningPipeline>);

impl RunningPipelines for FakeRegistry {
    fn list(&self, _community_id: &str) -> Vec<RunningPipeline> {
        self.0.clone()
    }
}

fn temp_data_dir(tag: &str) -> std::path::PathBuf {
    std::env::temp_dir().join(format!(
        "svc-streaming-hls-serve-it-{tag}-{}",
        Uuid::new_v4()
    ))
}

#[tokio::test]
async fn listing_endpoint_returns_registered_pipelines_as_json() {
    let pipeline_id = Uuid::new_v4();
    let started_at = OffsetDateTime::now_utc();
    let state = HlsRouterState::new(
        std::env::temp_dir(),
        Arc::new(FakeRegistry(vec![RunningPipeline {
            id: pipeline_id,
            profile: "1080p60".into(),
            started_at,
        }])),
    );
    let app = hls_router(state);

    let response = app
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
    let pipelines = parsed["pipelines"].as_array().unwrap();
    assert_eq!(pipelines.len(), 1);
    assert_eq!(pipelines[0]["id"], pipeline_id.to_string());
    assert_eq!(pipelines[0]["profile"], "1080p60");
    assert!(pipelines[0]["url"]
        .as_str()
        .unwrap()
        .starts_with("/live/community-1/"));
}

#[tokio::test]
async fn listing_endpoint_empty_registry_returns_empty_array() {
    let state = HlsRouterState::new(std::env::temp_dir(), Arc::new(FakeRegistry(vec![])));
    let app = hls_router(state);

    let response = app
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
    assert_eq!(parsed["pipelines"].as_array().unwrap().len(), 0);
}

#[tokio::test]
async fn serve_404s_for_a_pipeline_that_was_never_started() {
    let state = HlsRouterState::new(temp_data_dir("missing"), Arc::new(FakeRegistry(vec![])));
    let app = hls_router(state);

    let response = app
        .oneshot(
            Request::builder()
                .uri(format!("/live/c1/{}/prof1/master.m3u8", Uuid::new_v4()))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn serve_rejects_a_traversal_attempt_in_the_filename_segment() {
    let state = HlsRouterState::new(temp_data_dir("traversal"), Arc::new(FakeRegistry(vec![])));
    let app = hls_router(state);

    let response = app
        .oneshot(
            Request::builder()
                // `%2E%2E` decodes to a literal `..` inside the `filename`
                // path segment.
                .uri("/live/c1/p1/prof1/%2E%2E")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn serve_returns_correct_content_type_and_cache_control_per_file_kind() {
    let data_dir = temp_data_dir("headers");
    let pipeline_id = Uuid::new_v4();
    let dir = data_dir
        .join("hls")
        .join(pipeline_id.to_string())
        .join("prof1");
    tokio::fs::create_dir_all(&dir).await.unwrap();
    tokio::fs::write(dir.join("master.m3u8"), b"#EXTM3U\n")
        .await
        .unwrap();
    tokio::fs::write(dir.join("segment_00001.m4s"), vec![0u8; 16])
        .await
        .unwrap();
    tokio::fs::write(dir.join("init.mp4"), vec![0u8; 8])
        .await
        .unwrap();

    let cases = [
        ("master.m3u8", "application/vnd.apple.mpegurl", "no-cache"),
        ("segment_00001.m4s", "video/iso.segment", "max-age=60"),
        ("init.mp4", "video/mp4", "max-age=60"),
    ];

    for (filename, content_type, cache_control) in cases {
        let state = HlsRouterState::new(data_dir.clone(), Arc::new(FakeRegistry(vec![])));
        let app = hls_router(state);
        let response = app
            .oneshot(
                Request::builder()
                    .uri(format!("/live/c1/{pipeline_id}/prof1/{filename}"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK, "filename={filename}");
        assert_eq!(
            response
                .headers()
                .get(axum::http::header::CONTENT_TYPE)
                .unwrap(),
            content_type,
            "filename={filename}"
        );
        assert_eq!(
            response
                .headers()
                .get(axum::http::header::CACHE_CONTROL)
                .unwrap(),
            cache_control,
            "filename={filename}"
        );
    }

    tokio::fs::remove_dir_all(&data_dir).await.ok();
}

#[tokio::test]
async fn cors_preflight_allows_get_from_any_origin() {
    let state = HlsRouterState::new(temp_data_dir("cors"), Arc::new(FakeRegistry(vec![])));
    let app = hls_router(state);

    let response = app
        .oneshot(
            Request::builder()
                .uri("/live/community-1")
                .header("Origin", "https://example.com")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(
        response
            .headers()
            .get("access-control-allow-origin")
            .unwrap(),
        "*"
    );
}
