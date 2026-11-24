//! Control-plane REST API surface (`/api/v1/*`).
//!
//! Every handler in this module extracts its own
//! [`crate::http::auth::AuthenticatedClaims`] or
//! [`crate::http::auth::ServiceKey`] directly (rather than relying solely
//! on a `route_layer`) so the router built here is self-contained and
//! testable in isolation via [`router_for_testing`] -- see that function's
//! doc comment. [`router`] and [`internal_router`] are the two production
//! entrypoints [`crate::http::router`] mounts: `router` (community/tenant
//! routes) sits behind the JWT `route_layer`; `internal_router`
//! (`/internal/streaming/*`) does not -- every handler in `internal` already
//! extracts [`crate::http::auth::ServiceKey`] itself, and wrapping it in the
//! JWT layer too (as a single combined nest previously did) defeated the
//! point of a pure service-to-service credential. See `internal`'s module
//! doc for the historical note on that gap, now fixed at the S12
//! integration layer (`src/http/mod.rs`).
//!
//! A `DatabaseConnection` reaches every handler via [`extract::DbConn`]
//! (an `Extension` override if [`router_for_testing`] layered one in,
//! otherwise a lazy [`crate::db::get_or_connect`] singleton) and a
//! [`engine::SharedEngine`] via plain `axum::Extension`, supplied by
//! [`router`]/[`internal_router`]'s caller (the real
//! [`crate::pipeline::FfmpegSupervisor`] in production, a fake in tests).

mod common;
mod configs;
mod dto;
mod engine;
mod extract;
mod internal;
mod lifecycle;
mod live_channels;
mod response;
mod targets;
mod tenancy;

use axum::routing::{delete, get, post};
use axum::{Extension, Router};
use sea_orm::DatabaseConnection;
use utoipa::OpenApi;

use crate::error::ApiError;
use crate::http::AppState;

pub(crate) use common::pipeline_id_for_config;
pub use engine::{default_engine, SharedEngine};

/// Every route this chunk owns, annotated for OpenAPI generation. Not yet
/// merged into `crate::http::openapi::FullApiDoc` -- `src/http/openapi.rs`
/// is owned by a different chunk; wiring `crate::api::openapi()` into that
/// struct's `#[openapi(paths(...))]` list is a follow-up, not done here.
#[derive(OpenApi)]
#[openapi(
    paths(
        configs::list_configs,
        configs::create_config,
        configs::get_config,
        configs::update_config,
        configs::delete_config,
        targets::list_targets,
        targets::add_target,
        targets::remove_target,
        lifecycle::start,
        lifecycle::stop,
        lifecycle::status,
        live_channels::get_live_channels,
        internal::list_pipelines,
        internal::ingest_auth,
    ),
    components(schemas(
        dto::StreamingConfigDto,
        dto::CreateConfigRequest,
        dto::UpdateConfigRequest,
        dto::SecretRefDto,
        dto::StreamingTargetDto,
        dto::AddTargetRequest,
        dto::PipelineStatusDto,
        live_channels::ChannelStatusDto,
        internal::RunningPipelineDto,
        internal::IngestKind,
        internal::IngestAuthRequest,
        internal::IngestAuthResponse,
    )),
    tags(
        (name = "streaming", description = "svc-streaming control-plane API"),
        (name = "streaming-internal", description = "Service-to-service routes, X-Service-Key gated"),
    )
)]
struct ApiDoc;

/// Full OpenAPI document for every route this chunk owns.
pub fn openapi() -> utoipa::openapi::OpenApi {
    ApiDoc::openapi()
}

async fn placeholder() -> Result<(), ApiError> {
    Err(ApiError::Unimplemented(
        "control-plane API routes are not yet implemented".into(),
    ))
}

/// JWT-gated community/tenant routes -- everything except
/// `/internal/streaming/*`. Mounted behind the `require_auth` `route_layer`
/// in production ([`router`], nested by [`crate::http::router`]).
fn community_routes() -> Router<AppState> {
    Router::new()
        // Kept for `tests/auth.rs`'s `nested_api_v1_placeholder_*` tests:
        // `GET /api/v1` (this router's bare root) must keep returning 501
        // once authenticated. Every real route below lives at a non-root
        // path.
        .route("/", get(placeholder))
        .route(
            "/communities/{community_id}/streaming/configs",
            get(configs::list_configs).post(configs::create_config),
        )
        .route(
            "/communities/{community_id}/streaming/configs/{config_id}",
            get(configs::get_config)
                .put(configs::update_config)
                .delete(configs::delete_config),
        )
        .route(
            "/communities/{community_id}/streaming/configs/{config_id}/targets",
            get(targets::list_targets).post(targets::add_target),
        )
        .route(
            "/communities/{community_id}/streaming/targets/{target_id}",
            delete(targets::remove_target),
        )
        .route(
            "/communities/{community_id}/streaming/configs/{config_id}/start",
            post(lifecycle::start),
        )
        .route(
            "/communities/{community_id}/streaming/configs/{config_id}/stop",
            post(lifecycle::stop),
        )
        .route(
            "/communities/{community_id}/streaming/configs/{config_id}/status",
            get(lifecycle::status),
        )
        .route(
            "/communities/{community_id}/live-channels",
            get(live_channels::get_live_channels),
        )
}

/// `ServiceKey`-gated service-to-service routes -- `/internal/streaming/*`.
/// Mounted **without** the JWT `route_layer` in production ([`internal_router`]):
/// every handler here extracts [`crate::http::auth::ServiceKey`] itself, so
/// a valid `X-Service-Key` is necessary and sufficient.
fn internal_routes() -> Router<AppState> {
    Router::new()
        .route(
            "/internal/streaming/pipelines",
            get(internal::list_pipelines),
        )
        .route(
            "/internal/streaming/ingest-auth",
            post(internal::ingest_auth),
        )
}

/// Every route this chunk owns, with no DB/engine layering applied yet --
/// used only by [`router_for_testing`], which serves both route groups from
/// one flat, unauthenticated-at-the-router-level surface (each handler
/// still enforces its own JWT/service-key credential) so existing
/// integration tests keep exercising both groups through a single `Router`.
fn routes() -> Router<AppState> {
    community_routes().merge(internal_routes())
}

/// Production community/tenant router -- nested at `/api/v1` by
/// [`crate::http::router`] behind its JWT `route_layer`. DB access is lazy
/// per-request (see [`extract::DbConn`]); `engine` is the process's real
/// [`crate::pipeline::FfmpegSupervisor`] (or [`default_engine`] before it is
/// wired up).
pub fn router(engine: SharedEngine) -> Router<AppState> {
    community_routes().layer(Extension(engine))
}

/// Production internal (service-to-service) router -- nested at `/api/v1`
/// by [`crate::http::router`] **outside** its JWT `route_layer`; see this
/// module's doc comment.
pub fn internal_router(engine: SharedEngine) -> Router<AppState> {
    internal_routes().layer(Extension(engine))
}

/// Test entrypoint: builds the same combined route set as [`routes`] but
/// with an explicit `DatabaseConnection` (e.g. an in-memory sqlite pool
/// seeded by the test) and [`SharedEngine`] (a fake), instead of the
/// production lazy Postgres singleton and real engine. Auth behavior is
/// identical to production -- every handler extracts its own JWT/service-key
/// credential regardless of which router constructor built it.
pub fn router_for_testing(db: DatabaseConnection, engine: SharedEngine) -> Router<AppState> {
    routes().layer(Extension(db)).layer(Extension(engine))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn openapi_document_lists_every_route_this_chunk_owns() {
        let doc = openapi();
        let mut paths: Vec<&String> = doc.paths.paths.keys().collect();
        paths.sort();
        assert_eq!(
            paths,
            vec![
                "/communities/{community_id}/live-channels",
                "/communities/{community_id}/streaming/configs",
                "/communities/{community_id}/streaming/configs/{config_id}",
                "/communities/{community_id}/streaming/configs/{config_id}/start",
                "/communities/{community_id}/streaming/configs/{config_id}/status",
                "/communities/{community_id}/streaming/configs/{config_id}/stop",
                "/communities/{community_id}/streaming/configs/{config_id}/targets",
                "/communities/{community_id}/streaming/targets/{target_id}",
                "/internal/streaming/ingest-auth",
                "/internal/streaming/pipelines",
            ]
        );
    }

    #[test]
    fn production_routers_build_without_panicking() {
        // Just proves `router`/`internal_router` (the entrypoints
        // `crate::http::router` nests) construct successfully -- serving a
        // request through them would attempt a real DB connection via the
        // lazy `DbConn` extractor, which is exercised instead through
        // `router_for_testing` in `tests/api_*.rs`.
        let _: Router<AppState> = router(default_engine());
        let _: Router<AppState> = internal_router(default_engine());
    }

    #[tokio::test]
    async fn community_router_excludes_internal_routes() {
        // Regression guard for the S12 integration fix: `/internal/*` must
        // never ride the JWT-gated community router, or it would be
        // double-gated (JWT *and* ServiceKey) again -- see this module's
        // doc comment. A path this router never registered 404s, distinct
        // from the 401/403 a mis-gated route would return.
        use axum::body::Body;
        use axum::http::{Request, StatusCode};
        use clap::Parser as _;
        use tower::ServiceExt;

        let cli = crate::config::CliConfig::parse_from(["svc-streaming"]);
        let config = crate::config::Config {
            cli,
            db_password: crate::config::Secret::new("x"),
            cache_password: None,
            service_api_key: crate::config::Secret::new("x"),
            jwt_keys: crate::jwt_keys::JwtKeys::None,
        };
        let state = AppState::new(config, prometheus::Registry::new());
        let app = router(default_engine()).with_state(state);

        let response = app
            .oneshot(
                Request::builder()
                    .uri("/internal/streaming/pipelines")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::NOT_FOUND);
    }
}
