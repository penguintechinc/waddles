//! HTTP layer: axum router wiring for the control-plane surface (health,
//! readiness, Prometheus metrics). No overlay routes are mounted in P1 --
//! see [`crate::overlay::router`]'s module doc for the
//! `with_view_guard`/`with_push_guard` extension points P2-P4 call from
//! here once the real routes exist, and for why P1 does not pre-attach
//! either guard to an empty router.

pub mod health;

use std::sync::Arc;
use std::time::Instant;

use axum::extract::{Request, State};
use axum::middleware::Next;
use axum::response::Response;
use axum::routing::get;
use axum::Router;
use sea_orm::DatabaseConnection;
use tower_http::trace::TraceLayer;

use crate::config::Config;
use crate::overlay::{AppPushTrustSource, SeaOrmViewCredentialStore};
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
}

impl AppState {
    /// Builds the shared application state from a loaded [`Config`], the
    /// Prometheus [`prometheus::Registry`] created during telemetry init,
    /// and an already-open database connection (see
    /// [`crate::db::get_or_connect`]).
    pub fn new(config: Config, metrics: prometheus::Registry, db: DatabaseConnection) -> Self {
        let request_metrics = crate::telemetry::register_request_metrics(&metrics);
        let view_store = Arc::new(SeaOrmViewCredentialStore::new(db.clone()));
        let push_trust_source = Arc::new(AppPushTrustSource::from_config(&config));
        Self {
            config: Arc::new(config),
            metrics: Arc::new(metrics),
            request_metrics,
            started_at: Instant::now(),
            db,
            view_store,
            push_trust_source,
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

/// Builds the control-plane router: public health/readiness (no auth).
///
/// EXTENSION POINT (P2/P3/P4): once a real overlay route exists, wrap it
/// with `crate::overlay::router::with_view_guard`/`with_push_guard`
/// (passing `state.view_store.clone()`/`state.push_trust_source.clone()`)
/// and `.merge(...)` the result into `public` below, e.g.:
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
