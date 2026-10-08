//! HTTP layer: axum router wiring for the control-plane surface (health,
//! readiness, Prometheus metrics) plus P6's PUSH-guarded image-upload
//! route. No view/live overlay routes are mounted yet -- see
//! [`crate::overlay::router`]'s module doc for the `with_view_guard`
//! extension point P2/P3 call from here once those routes exist, and for
//! why P1 did not pre-attach either guard to an empty router (P6 is the
//! first chunk to actually exercise `with_push_guard` against a real
//! route).

pub mod health;

use std::sync::Arc;
use std::time::Instant;

use axum::extract::{DefaultBodyLimit, Request, State};
use axum::middleware::Next;
use axum::response::Response;
use axum::routing::{get, post};
use axum::Router;
use sea_orm::DatabaseConnection;
use tower_http::trace::TraceLayer;

use crate::config::Config;
use crate::images::store::ObjectStoreImageStore;
use crate::images::{AssetStore, ImageStore, SeaOrmImageAssetStore};
use crate::overlay::{AppPushTrustSource, SeaOrmViewCredentialStore};
use crate::telemetry::{ImageMetrics, RequestMetrics};

/// Shared state handed to every axum handler via `Router::with_state`.
/// Cheap to clone: everything behind an `Arc` (or already `Clone`, like
/// `DatabaseConnection`, which wraps an `Arc`-backed pool internally).
#[derive(Clone)]
pub struct AppState {
    pub config: Arc<Config>,
    pub metrics: Arc<prometheus::Registry>,
    pub request_metrics: RequestMetrics,
    pub started_at: Instant,
    pub db: DatabaseConnection,
    /// Concrete `overlay_auth::ViewCredentialStore` this service mounts --
    /// shared with `crate::overlay::router::view_guarded_router` and
    /// (once P2/P3 add real routes) any render handler that also needs a
    /// validated `community_id`.
    pub view_store: Arc<SeaOrmViewCredentialStore>,
    /// Concrete `overlay_auth::PushTrustSource` this service mounts --
    /// shared with `crate::overlay::router::push_guarded_router`.
    pub push_trust_source: Arc<AppPushTrustSource>,
    /// P6/P9 image-upload/render additions.
    ///
    /// `None` when `IMAGE_BUCKET_ACCESS_KEY_ID`/`IMAGE_BUCKET_SECRET_ACCESS_KEY`
    /// are unset -- a deployment that never enables
    /// `crate::flags::IMAGE_UPLOAD_FLAG` need not configure a bucket at
    /// all (`crate::images::store::ObjectStoreImageStore::from_config`'s
    /// own doc). `crate::images::upload::upload_image` returns a clear 500
    /// rather than panicking when this is `None` but the flag is ON.
    pub image_store: Option<Arc<dyn ImageStore>>,
    pub image_asset_store: Arc<dyn AssetStore>,
    pub image_upload_flag: Arc<dyn crate::flags::FeatureFlag>,
    pub image_metrics: ImageMetrics,
}

impl AppState {
    /// Builds the shared application state from a loaded [`Config`], the
    /// Prometheus [`prometheus::Registry`] created during telemetry init,
    /// and an already-open database connection (see
    /// [`crate::db::get_or_connect`]).
    pub fn new(config: Config, metrics: prometheus::Registry, db: DatabaseConnection) -> Self {
        let request_metrics = crate::telemetry::register_request_metrics(&metrics);
        let image_metrics = crate::telemetry::register_image_metrics(&metrics);
        let view_store = Arc::new(SeaOrmViewCredentialStore::new(db.clone()));
        let push_trust_source = Arc::new(AppPushTrustSource::from_config(&config));
        let image_asset_store: Arc<dyn AssetStore> =
            Arc::new(SeaOrmImageAssetStore::new(db.clone()));
        // Best-effort: a deployment that never enables
        // `crate::flags::IMAGE_UPLOAD_FLAG` need not set
        // `IMAGE_BUCKET_ACCESS_KEY_ID`/`IMAGE_BUCKET_SECRET_ACCESS_KEY` at
        // all, so a construction failure here is logged and degrades to
        // `None`, never a process-wide startup failure
        // (`rules/general.md` Red Flags).
        let image_store: Option<Arc<dyn ImageStore>> =
            match ObjectStoreImageStore::from_config(&config) {
                Ok(store) => Some(Arc::new(store)),
                Err(err) => {
                    tracing::warn!(
                        error = %err,
                        "image bucket not configured; image upload/render will fail if enabled"
                    );
                    None
                }
            };
        let license_client = crate::flags::build_license_client();
        let image_upload_flag = crate::flags::image_upload_flag(&license_client);
        Self {
            config: Arc::new(config),
            metrics: Arc::new(metrics),
            request_metrics,
            started_at: Instant::now(),
            db,
            view_store,
            push_trust_source,
            image_store,
            image_asset_store,
            image_upload_flag,
            image_metrics,
        }
    }
}

/// Records every request into [`AppState::request_metrics`]: a labeled
/// counter and a latency histogram. Applied to the whole control-plane
/// router so `/metrics` always has data once the service has served at
/// least one request (the `up` gauge covers the window before that).
async fn record_http_metrics(State(state): State<AppState>, req: Request, next: Next) -> Response {
    let method = req.method().to_string();
    let path = req.uri().path().to_string();
    let start = Instant::now();
    let response = next.run(req).await;
    let status = response.status().as_u16().to_string();
    state
        .request_metrics
        .http_requests_total
        .with_label_values(&[&method, &path, &status])
        .inc();
    state
        .request_metrics
        .http_request_duration_seconds
        .with_label_values(&[&method, &path])
        .observe(start.elapsed().as_secs_f64());
    response
}

/// Builds the control-plane router: public health/readiness (no auth), plus
/// P6's PUSH-guarded image-upload route
/// (`POST /overlay/{community}/image/push`).
///
/// EXTENSION POINT (P2/P3): once a real view/live route exists, wrap it
/// with `crate::overlay::router::with_view_guard`
/// (passing `state.view_store.clone()`) and `.merge(...)` the result in
/// here too, e.g.:
/// ```ignore
/// let overlay_view = crate::overlay::router::with_view_guard(
///     Router::new().route("/overlay/{community}/{surface}", get(render::surface)),
///     state.view_store.clone(),
/// );
/// public.merge(overlay_view)
/// ```
pub fn router(state: AppState) -> Router {
    let public = Router::new()
        .route("/health", get(health::liveness))
        .route("/readyz", get(health::readiness));

    // The literal `image` segment plays the generic PUSH route's
    // `{surface}` role -- `overlay_auth::push_scope` is keyed on
    // `community_id` alone, so this reuses `with_push_guard` unmodified
    // (`crate::images::upload`'s own module doc).
    let image_upload = crate::overlay::router::with_push_guard(
        Router::new().route(
            "/overlay/{community}/image/push",
            post(crate::images::upload::upload_image),
        ),
        state.push_trust_source.clone(),
    )
    // Bounds the raw request body independently of the post-decode
    // `IMAGE_MAX_BYTES` check in `crate::images::upload` -- defense in
    // depth against an oversized multipart body being buffered at all.
    // +64KiB headroom for multipart boundaries/field overhead.
    .layer(DefaultBodyLimit::max(
        state.config.cli.image_max_bytes as usize + 64 * 1024,
    ));

    public
        .merge(image_upload)
        .with_state(state.clone())
        .layer(axum::middleware::from_fn_with_state(
            state.clone(),
            record_http_metrics,
        ))
        .layer(TraceLayer::new_for_http())
}

/// Builds the secondary Prometheus metrics router, bound to its own port
/// (`METRICS_PORT`, default 9090) per `rules/critical-rules.md`
/// Observability -- kept separate from the control-plane router so a
/// scraper never needs auth and never shares a listener with user
/// traffic.
pub fn metrics_router(state: AppState) -> Router {
    Router::new()
        .route("/metrics", get(health::metrics))
        .with_state(state)
}
