//! HTTP layer: axum router wiring for the control-plane surface (health,
//! readiness, Prometheus metrics) plus P3/P4's live overlay viewer
//! (SSE + websocket) and push routes (see [`overlay`]). Both overlay route
//! groups are mounted already wrapped by
//! `crate::overlay::router::with_view_guard`/`with_push_guard` -- see that
//! module's doc for why each guard is applied per already-populated
//! sub-router rather than once globally.

pub mod health;
pub mod overlay;

use std::sync::Arc;
use std::time::Instant;

use axum::extract::{Request, State};
use axum::middleware::Next;
use axum::response::Response;
use axum::routing::{get, post};
use axum::Router;
use sea_orm::DatabaseConnection;
use tower_http::trace::TraceLayer;

use crate::config::Config;
use crate::overlay::{AppPushTrustSource, PresentationHub, SeaOrmViewCredentialStore};
use crate::telemetry::RequestMetrics;

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
    /// P3's in-process push fan-out -- shared by every `overlay::live_sse`/
    /// `live_ws` subscriber and the `overlay::push` handler's publisher.
    pub hub: Arc<PresentationHub>,
}

impl AppState {
    /// Builds the shared application state from a loaded [`Config`], the
    /// Prometheus [`prometheus::Registry`] created during telemetry init,
    /// and an already-open database connection (see
    /// [`crate::db::get_or_connect`]).
    pub fn new(config: Config, metrics: prometheus::Registry, db: DatabaseConnection) -> Self {
        let request_metrics = crate::telemetry::register_request_metrics(&metrics);
        let hub_metrics = crate::overlay::hub::register_hub_metrics(&metrics);
        let view_store = Arc::new(SeaOrmViewCredentialStore::new(db.clone()));
        let push_trust_source = Arc::new(AppPushTrustSource::from_config(&config));
        let hub = Arc::new(PresentationHub::new(hub_metrics));
        Self {
            config: Arc::new(config),
            metrics: Arc::new(metrics),
            request_metrics,
            started_at: Instant::now(),
            db,
            view_store,
            push_trust_source,
            hub,
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
/// P3/P4's overlay routes, each wrapped by its own `overlay_auth` guard
/// before being merged in.
///
/// REMAINING EXTENSION POINT (P2): the plain full-page surface route
/// (`GET /overlay/{community}/{surface}`, no `/live`/`/push` suffix) still
/// needs to be added the same way -- wrapped with
/// `crate::overlay::router::with_view_guard` and merged in below -- once a
/// render handler exists.
pub fn router(state: AppState) -> Router {
    let overlay_view = crate::overlay::router::with_view_guard(
        Router::new()
            .route(
                "/overlay/{community}/{surface}/live",
                get(overlay::live_sse),
            )
            .route(
                "/overlay/{community}/{surface}/live/ws",
                get(overlay::live_ws),
            ),
        state.view_store.clone(),
    );
    let overlay_push = crate::overlay::router::with_push_guard(
        Router::new().route("/overlay/{community}/{surface}/push", post(overlay::push)),
        state.push_trust_source.clone(),
    );

    let public = Router::new()
        .route("/health", get(health::liveness))
        .route("/readyz", get(health::readiness))
        .merge(overlay_view)
        .merge(overlay_push);

    public
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
