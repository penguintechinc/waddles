//! Public HLS serving surface: `GET /live/{community_id}` (running-pipeline
//! listing) and `GET /live/{community_id}/{pipeline_id}/{profile}/{file}`
//! (playlists/segments/init segment), mounted via [`hls_router`]. No auth --
//! same posture as the overlay surfaces (`rules/security.md` doesn't require
//! it for public playback URLs); path-traversal-safe and CORS-open (`GET`
//! only) per spec (issue #287 S7 §2).

use std::path::PathBuf;
use std::sync::Arc;

use axum::body::Body;
use axum::extract::{Path, State};
use axum::http::header::{CACHE_CONTROL, CONTENT_TYPE};
use axum::http::{Method, StatusCode};
use axum::response::Response;
use axum::routing::get;
use axum::{Json, Router};
use serde::Serialize;
use time::OffsetDateTime;
use tower_http::cors::{Any, CorsLayer};

use crate::egress::hls::output;
use crate::error::ApiError;
use crate::pipeline::model::PipelineId;

/// Minimal read-only view onto a community's currently-running HLS
/// pipelines, needed only by [`hls_router`]'s `GET /live/{community_id}`
/// listing endpoint. Deliberately narrower than `pipeline::model::
/// PipelineEngine` (whose `impl Future` return types aren't dyn-safe, so it
/// can't be stored as `Arc<dyn PipelineEngine>` here) -- once S3's real
/// pipeline supervisor exists, a thin synchronous adapter over its own
/// registry satisfies this trait; tests fake it directly (see this module's
/// own tests and `tests/egress_hls_serve.rs`). File serving itself
/// (`serve_file`) deliberately does *not* consult this trait -- a
/// stopped/never-started pipeline's directory simply doesn't exist on disk,
/// which already 404s, so serving stays a pure filesystem read independent
/// of whatever tracks "running" pipelines.
pub trait RunningPipelines: Send + Sync {
    /// Returns every currently-running HLS pipeline for `community_id`.
    fn list(&self, community_id: &str) -> Vec<RunningPipeline>;
}

/// Always-empty [`RunningPipelines`] -- the default
/// [`crate::http::AppState`] uses before `crate::run_with_shutdown`
/// overrides `hls_router_state` with the orchestrator's live registry.
/// `GET /live/{community_id}` reports no pipelines; direct file serving
/// (`serve_file`) is unaffected, since it never consults this trait (see
/// this module's own doc comment).
#[derive(Debug, Default, Clone, Copy)]
pub struct EmptyRunningPipelines;

impl RunningPipelines for EmptyRunningPipelines {
    fn list(&self, _community_id: &str) -> Vec<RunningPipeline> {
        Vec::new()
    }
}

/// One running pipeline entry, as returned by [`RunningPipelines::list`].
#[derive(Debug, Clone)]
pub struct RunningPipeline {
    pub id: PipelineId,
    pub profile: String,
    pub started_at: OffsetDateTime,
}

/// State for [`hls_router`]: the `STREAM_DATA_DIR` root (segment/playlist
/// files live under `{data_dir}/hls/...`, see `output::hls_root`) and the
/// running-pipeline directory used only by the listing endpoint.
#[derive(Clone)]
pub struct HlsRouterState {
    data_dir: PathBuf,
    registry: Arc<dyn RunningPipelines>,
}

impl HlsRouterState {
    pub fn new(data_dir: PathBuf, registry: Arc<dyn RunningPipelines>) -> Self {
        Self { data_dir, registry }
    }
}

/// Builds the public (unauthenticated) HLS serving router -- `GET`-only,
/// CORS-open (`Access-Control-Allow-Origin: *`). Fully state-erased
/// (`Router` == `Router<()>`) so the caller (`http::router`, S1) can
/// `.merge()` this directly onto its own public router; see this crate's
/// `README.md` Module Ownership for the exact one-line mount this chunk
/// could not make itself (`src/http/mod.rs` is S1-owned).
pub fn hls_router(state: HlsRouterState) -> Router {
    let cors = CorsLayer::new()
        .allow_methods([Method::GET])
        .allow_origin(Any);

    Router::new()
        .route("/live/{community_id}", get(list_pipelines))
        .route(
            "/live/{community_id}/{pipeline_id}/{profile}/{filename}",
            get(serve_file),
        )
        .with_state(state)
        .layer(cors)
}

/// Wire schema for one entry in the `GET /live/{community_id}` listing.
#[derive(Debug, Serialize)]
struct PipelineListingEntry {
    id: PipelineId,
    profile: String,
    url: String,
    #[serde(serialize_with = "crate::timestamp::serialize_z")]
    started_at: OffsetDateTime,
}

/// Wire schema for `GET /live/{community_id}` -- `{pipelines: [...]}`.
#[derive(Debug, Serialize)]
struct PipelineListing {
    pipelines: Vec<PipelineListingEntry>,
}

async fn list_pipelines(
    State(state): State<HlsRouterState>,
    Path(community_id): Path<String>,
) -> Result<Json<PipelineListing>, ApiError> {
    if !output::is_safe_path_segment(&community_id) {
        return Err(ApiError::BadRequest("invalid community_id".into()));
    }

    let pipelines = state
        .registry
        .list(&community_id)
        .into_iter()
        .map(|p| PipelineListingEntry {
            id: p.id,
            // Points at the *media* playlist, not `master.m3u8`: for this
            // MVP's single-profile-per-pipeline output, ffmpeg writes
            // `master.m3u8`'s `#EXT-X-STREAM-INF` line only once it knows
            // its own stream parameters, which (for a single fmp4 HLS
            // output) never happens -- the file stays header-only forever
            // and hls.js has nothing to play. `index.m3u8` is always
            // complete and playable directly. `serve_file` additionally
            // synthesizes a valid master on request (see
            // `synthesize_master_playlist`) so a `master.m3u8` URL built
            // from an older client/cached response still works.
            url: format!(
                "/live/{community_id}/{}/{}/{}",
                p.id,
                p.profile,
                output::MEDIA_PLAYLIST_NAME
            ),
            profile: p.profile,
            started_at: p.started_at,
        })
        .collect();

    Ok(Json(PipelineListing { pipelines }))
}

/// Maps a served filename to its HLS-appropriate `Content-Type` per spec
/// §2. `None` for anything else -- the caller 404s rather than guessing.
fn content_type_for(filename: &str) -> Option<&'static str> {
    if filename.ends_with(".m3u8") {
        Some("application/vnd.apple.mpegurl")
    } else if filename.ends_with(".m4s") {
        Some("video/iso.segment")
    } else if filename.ends_with(".mp4") {
        Some("video/mp4")
    } else {
        None
    }
}

/// `BANDWIDTH` (bits/sec) advertised in a synthesized master playlist's
/// `#EXT-X-STREAM-INF` line. This service's disk layout carries no
/// per-profile bitrate at the file-serving layer -- `serve_file` is a pure
/// filesystem read keyed only by path segments (see this module's doc
/// comment), never consulting the `TranscodeProfile`/`VideoCodec`
/// `bitrate_kbps` config that produced the running pipeline -- so this
/// fallback is always what gets advertised for now; RESOLUTION is omitted
/// entirely for the same reason (never known at this layer).
const DEFAULT_MASTER_BANDWIDTH_BPS: u32 = 2_000_000;

/// If `body` (an on-disk `master.m3u8`, read as bytes) is missing an
/// `#EXT-X-STREAM-INF` line, returns a synthesized replacement referencing
/// [`output::MEDIA_PLAYLIST_NAME`] in the same directory; `None` if `body`
/// already declares a variant (or isn't valid UTF-8 text), in which case
/// the caller serves it verbatim.
///
/// Why this is needed: `ffmpeg -master_pl_name master.m3u8` writes that
/// file once, at HLS-muxer-init time, before the first frame is fully
/// analyzed -- for this MVP's single fmp4 HLS output per pipeline, it is
/// *never rewritten*, so it stays permanently header-only
/// (`#EXTM3U\n#EXT-X-VERSION:7\n`) and no HLS.js player has a variant to
/// select. `index.m3u8`, the media playlist, is complete and playable the
/// entire time. This keeps `master.m3u8` URLs (already-cached client
/// responses, external links) working without waiting on a future
/// multi-profile ladder to make ffmpeg's own master playlist meaningful.
fn maybe_synthesize_master_playlist(body: &[u8]) -> Option<Vec<u8>> {
    let text = std::str::from_utf8(body).ok()?;
    if text
        .lines()
        .any(|line| line.starts_with("#EXT-X-STREAM-INF"))
    {
        return None;
    }
    Some(
        format!(
            "#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-STREAM-INF:BANDWIDTH={DEFAULT_MASTER_BANDWIDTH_BPS}\n{}\n",
            output::MEDIA_PLAYLIST_NAME
        )
        .into_bytes(),
    )
}

async fn serve_file(
    State(state): State<HlsRouterState>,
    Path((community_id, pipeline_id, profile, filename)): Path<(String, String, String, String)>,
) -> Result<Response, ApiError> {
    // `community_id` is not part of the disk layout (see `output::
    // HlsOutputTarget`'s doc comment) -- validated here for shape/
    // traversal safety only, never looked up: a stopped/never-started
    // pipeline's directory simply doesn't exist, which 404s below on its
    // own.
    for segment in [&community_id, &pipeline_id, &profile, &filename] {
        if !output::is_safe_path_segment(segment) {
            return Err(not_found());
        }
    }

    let content_type = content_type_for(&filename).ok_or_else(not_found)?;

    let dir = output::hls_root(&state.data_dir)
        .join(&pipeline_id)
        .join(&profile);
    let path = dir.join(&filename);
    let resolved = resolve_safe_path(&dir, &path).await?;

    let mut bytes = tokio::fs::read(&resolved).await.map_err(|_| not_found())?;

    if filename == output::MASTER_PLAYLIST_NAME {
        if let Some(synthesized) = maybe_synthesize_master_playlist(&bytes) {
            bytes = synthesized;
        }
    }

    let cache_control = if filename.ends_with(".m3u8") {
        "no-cache"
    } else {
        "max-age=60"
    };

    Response::builder()
        .status(StatusCode::OK)
        .header(CONTENT_TYPE, content_type)
        .header(CACHE_CONTROL, cache_control)
        .body(Body::from(bytes))
        .map_err(|err| ApiError::Internal(err.into()))
}

fn not_found() -> ApiError {
    ApiError::NotFound("not found".into())
}

/// Canonicalizes `path` and verifies it is still contained in `dir` and is
/// not a symlink -- defense in depth beyond `output::is_safe_path_segment`
/// (e.g. a segment file replaced with a symlink after being written).
/// Every failure mode 404s (never a raw I/O error), so a missing file and a
/// rejected traversal/symlink attempt are indistinguishable to the caller.
async fn resolve_safe_path(
    dir: &std::path::Path,
    path: &std::path::Path,
) -> Result<PathBuf, ApiError> {
    let metadata = tokio::fs::symlink_metadata(path)
        .await
        .map_err(|_| not_found())?;
    if metadata.file_type().is_symlink() {
        return Err(not_found());
    }
    let canonical_dir = tokio::fs::canonicalize(dir)
        .await
        .map_err(|_| not_found())?;
    let canonical_path = tokio::fs::canonicalize(path)
        .await
        .map_err(|_| not_found())?;
    if !canonical_path.starts_with(&canonical_dir) {
        return Err(not_found());
    }
    Ok(canonical_path)
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body as AxumBody;
    use axum::http::Request;
    use http_body_util::BodyExt;
    use tower::ServiceExt;
    use uuid::Uuid;

    struct FakeRegistry(Vec<RunningPipeline>);
    impl RunningPipelines for FakeRegistry {
        fn list(&self, _community_id: &str) -> Vec<RunningPipeline> {
            self.0.clone()
        }
    }

    fn state_with(pipelines: Vec<RunningPipeline>) -> HlsRouterState {
        HlsRouterState::new(std::env::temp_dir(), Arc::new(FakeRegistry(pipelines)))
    }

    #[test]
    fn content_type_maps_known_extensions() {
        assert_eq!(
            content_type_for("master.m3u8"),
            Some("application/vnd.apple.mpegurl")
        );
        assert_eq!(
            content_type_for("index.m3u8"),
            Some("application/vnd.apple.mpegurl")
        );
        assert_eq!(
            content_type_for("segment_00001.m4s"),
            Some("video/iso.segment")
        );
        assert_eq!(content_type_for("init.mp4"), Some("video/mp4"));
        assert_eq!(content_type_for("evil.exe"), None);
    }

    #[tokio::test]
    async fn listing_returns_pipelines_as_json() {
        let pipeline_id = Uuid::new_v4();
        let started_at = OffsetDateTime::now_utc();
        let app = hls_router(state_with(vec![RunningPipeline {
            id: pipeline_id,
            profile: "1080p60".into(),
            started_at,
        }]));

        let response = app
            .oneshot(
                Request::builder()
                    .uri("/live/community-1")
                    .body(AxumBody::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let body = response.into_body().collect().await.unwrap().to_bytes();
        let parsed: serde_json::Value = serde_json::from_slice(&body).unwrap();
        let pipelines = parsed["pipelines"].as_array().unwrap();
        assert_eq!(pipelines.len(), 1);
        assert_eq!(pipelines[0]["profile"], "1080p60");
        assert!(pipelines[0]["url"]
            .as_str()
            .unwrap()
            .ends_with(&format!("/{pipeline_id}/1080p60/index.m3u8")));
    }

    #[tokio::test]
    async fn listing_entry_has_exactly_the_documented_field_set() {
        // Output-validation regression guard (`rules/security.md` Output
        // Validation): `core/svc_presentation/blueprints/live_stream.py`'s
        // `_normalize_pipelines` reads exactly `id`/`profile`/`url`/
        // `started_at` off each entry and silently drops anything it
        // doesn't recognize -- an extra or renamed field here degrades
        // playback there without either side raising an error, so pin the
        // exact key set rather than only spot-checking a few of them (as
        // `listing_returns_pipelines_as_json` above does).
        let pipeline_id = Uuid::new_v4();
        let app = hls_router(state_with(vec![RunningPipeline {
            id: pipeline_id,
            profile: "1080p60".into(),
            started_at: OffsetDateTime::now_utc(),
        }]));

        let response = app
            .oneshot(
                Request::builder()
                    .uri("/live/community-1")
                    .body(AxumBody::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let body = response.into_body().collect().await.unwrap().to_bytes();
        let parsed: serde_json::Value = serde_json::from_slice(&body).unwrap();

        let top_level_keys: std::collections::BTreeSet<&str> = parsed
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect();
        assert_eq!(
            top_level_keys,
            std::collections::BTreeSet::from(["pipelines"])
        );

        let entry = parsed["pipelines"][0].as_object().unwrap();
        let entry_keys: std::collections::BTreeSet<&str> =
            entry.keys().map(String::as_str).collect();
        assert_eq!(
            entry_keys,
            std::collections::BTreeSet::from(["id", "profile", "url", "started_at"])
        );
        assert_eq!(entry["id"], pipeline_id.to_string());
        assert_eq!(entry["profile"], "1080p60");
        assert_eq!(
            entry["url"],
            format!("/live/community-1/{pipeline_id}/1080p60/index.m3u8")
        );
        assert!(entry["started_at"].as_str().is_some());
    }

    #[tokio::test]
    async fn listing_rejects_unsafe_community_id() {
        let app = hls_router(state_with(vec![]));
        let response = app
            .oneshot(
                Request::builder()
                    // `%00` decodes to a NUL byte within one path segment --
                    // unambiguously unsafe without depending on whether the
                    // HTTP stack normalizes literal `..` dot-segments.
                    .uri("/live/a%00b")
                    .body(AxumBody::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    }

    #[tokio::test]
    async fn serve_traversal_attempt_in_filename_is_rejected() {
        let app = hls_router(state_with(vec![]));
        let response = app
            .oneshot(
                Request::builder()
                    // `%2E%2E` decodes to `..` within the `filename` segment.
                    .uri("/live/c1/p1/prof1/%2E%2E")
                    .body(AxumBody::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn serve_unknown_extension_is_rejected() {
        let app = hls_router(state_with(vec![]));
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/live/c1/p1/prof1/evil.exe")
                    .body(AxumBody::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn serve_missing_pipeline_directory_is_404() {
        let app = hls_router(state_with(vec![]));
        let response = app
            .oneshot(
                Request::builder()
                    .uri(format!("/live/c1/{}/prof1/master.m3u8", Uuid::new_v4()))
                    .body(AxumBody::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::NOT_FOUND);
    }

    /// A `master.m3u8` that already declares a variant (`#EXT-X-STREAM-INF`
    /// present) -- e.g. a future multi-profile ladder ffmpeg rewrote after
    /// analyzing its inputs -- must be served exactly as-is, not
    /// resynthesized.
    #[tokio::test]
    async fn serve_complete_master_playlist_is_passed_through_verbatim() {
        let data_dir =
            std::env::temp_dir().join(format!("svc-streaming-hls-serve-{}", Uuid::new_v4()));
        let pipeline_id = Uuid::new_v4();
        let dir = output::hls_root(&data_dir)
            .join(pipeline_id.to_string())
            .join("prof1");
        tokio::fs::create_dir_all(&dir).await.unwrap();
        let complete_master =
            b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=4500000\nindex.m3u8\n".to_vec();
        tokio::fs::write(dir.join("master.m3u8"), &complete_master)
            .await
            .unwrap();

        let app = hls_router(HlsRouterState::new(
            data_dir.clone(),
            Arc::new(FakeRegistry(vec![])),
        ));
        let response = app
            .oneshot(
                Request::builder()
                    .uri(format!("/live/c1/{pipeline_id}/prof1/master.m3u8"))
                    .body(AxumBody::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(
            response.headers().get(CONTENT_TYPE).unwrap(),
            "application/vnd.apple.mpegurl"
        );
        assert_eq!(response.headers().get(CACHE_CONTROL).unwrap(), "no-cache");
        let body = response.into_body().collect().await.unwrap().to_bytes();
        assert_eq!(&body[..], &complete_master[..]);

        tokio::fs::remove_dir_all(&data_dir).await.ok();
    }

    /// A `master.m3u8` ffmpeg wrote header-only (no `#EXT-X-STREAM-INF`) --
    /// this module's doc comment on `maybe_synthesize_master_playlist`
    /// explains why this is the steady state for a single-profile pipeline
    /// -- must be resynthesized into a playable master referencing the
    /// media playlist, not served as the unplayable two-line stub.
    #[tokio::test]
    async fn serve_header_only_master_playlist_is_synthesized() {
        let data_dir =
            std::env::temp_dir().join(format!("svc-streaming-hls-serve-{}", Uuid::new_v4()));
        let pipeline_id = Uuid::new_v4();
        let dir = output::hls_root(&data_dir)
            .join(pipeline_id.to_string())
            .join("prof1");
        tokio::fs::create_dir_all(&dir).await.unwrap();
        tokio::fs::write(dir.join("master.m3u8"), b"#EXTM3U\n#EXT-X-VERSION:7\n")
            .await
            .unwrap();

        let app = hls_router(HlsRouterState::new(
            data_dir.clone(),
            Arc::new(FakeRegistry(vec![])),
        ));
        let response = app
            .oneshot(
                Request::builder()
                    .uri(format!("/live/c1/{pipeline_id}/prof1/master.m3u8"))
                    .body(AxumBody::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(
            response.headers().get(CONTENT_TYPE).unwrap(),
            "application/vnd.apple.mpegurl"
        );
        assert_eq!(response.headers().get(CACHE_CONTROL).unwrap(), "no-cache");
        let body = response.into_body().collect().await.unwrap().to_bytes();
        let text = std::str::from_utf8(&body).unwrap();
        assert!(text.starts_with("#EXTM3U\n"));
        assert!(text.contains("#EXT-X-STREAM-INF:BANDWIDTH=2000000"));
        assert!(text.trim_end().ends_with("index.m3u8"));

        tokio::fs::remove_dir_all(&data_dir).await.ok();
    }

    #[test]
    fn maybe_synthesize_master_playlist_leaves_variant_playlists_untouched() {
        let complete = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000000\nindex.m3u8\n";
        assert!(maybe_synthesize_master_playlist(complete).is_none());
    }

    #[test]
    fn maybe_synthesize_master_playlist_replaces_header_only_files() {
        let header_only = b"#EXTM3U\n#EXT-X-VERSION:7\n";
        let synthesized = maybe_synthesize_master_playlist(header_only)
            .expect("header-only master must be synthesized");
        let text = String::from_utf8(synthesized).unwrap();
        assert!(text.contains("#EXT-X-STREAM-INF:BANDWIDTH=2000000"));
        assert!(text.trim_end().ends_with("index.m3u8"));
    }

    #[test]
    fn maybe_synthesize_master_playlist_ignores_non_utf8_bytes() {
        let invalid = [0xFFu8, 0xFE, 0x00];
        assert!(maybe_synthesize_master_playlist(&invalid).is_none());
    }

    #[tokio::test]
    async fn serve_segment_has_max_age_cache_control() {
        let data_dir =
            std::env::temp_dir().join(format!("svc-streaming-hls-serve-{}", Uuid::new_v4()));
        let pipeline_id = Uuid::new_v4();
        let dir = output::hls_root(&data_dir)
            .join(pipeline_id.to_string())
            .join("prof1");
        tokio::fs::create_dir_all(&dir).await.unwrap();
        tokio::fs::write(dir.join("segment_00001.m4s"), b"\x00\x01")
            .await
            .unwrap();

        let app = hls_router(HlsRouterState::new(
            data_dir.clone(),
            Arc::new(FakeRegistry(vec![])),
        ));
        let response = app
            .oneshot(
                Request::builder()
                    .uri(format!("/live/c1/{pipeline_id}/prof1/segment_00001.m4s"))
                    .body(AxumBody::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(
            response.headers().get(CONTENT_TYPE).unwrap(),
            "video/iso.segment"
        );
        assert_eq!(response.headers().get(CACHE_CONTROL).unwrap(), "max-age=60");

        tokio::fs::remove_dir_all(&data_dir).await.ok();
    }

    #[tokio::test]
    async fn cors_allows_get_from_any_origin() {
        let app = hls_router(state_with(vec![]));
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/live/community-1")
                    .header("Origin", "https://example.com")
                    .body(AxumBody::empty())
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
}
