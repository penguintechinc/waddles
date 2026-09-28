//! HTTP layer: axum router wiring for the control-plane surface
//! (`/health`, `/healthz`) and the secondary metrics router. The generic
//! webhook/JWT intake routes (`POST /intake/webhook/{tenant}/{source}`,
//! `POST /intake/events`, spec S10.1) mount here once the connector/intake
//! work lands -- see the `TODO(M5)` seam in `src/lib.rs` and `router`
//! below; nothing here fakes that surface in the meantime.
//!
//! The Twitch EventSub webhook receiver (`eventsub`, `POST /eventsub/
//! twitch/webhook`) is the one exception already mounted here -- it is
//! in-line of traffic per `critical-rules.md` Data Plane and was built in
//! Rust from the start (design doc `docs/superpowers/specs/2026-09-28-
//! connections-credentials-design.md` §8 increment 4), landing ahead of the
//! generic connector/intake work above. A separate `Router` merged in
//! below, not folded into the generic intake surface once that lands --
//! different auth/trust model, different secrets, different rate limits
//! (design doc §4.4).

pub mod eventsub;
pub mod health;

use std::sync::Arc;
use std::time::Instant;

use axum::extract::{Request, State};
use axum::middleware::Next;
use axum::response::Response;
use axum::routing::get;
use axum::Router;
use tower_http::trace::TraceLayer;

use crate::config::Config;
use crate::telemetry::RequestMetrics;

/// Shared state handed to every axum handler via `Router::with_state`.
/// Cheap to clone: everything behind an `Arc`.
#[derive(Clone)]
pub struct AppState {
    pub config: Arc<Config>,
    pub metrics: Arc<prometheus::Registry>,
    pub request_metrics: RequestMetrics,
    pub started_at: Instant,
    /// Twitch EventSub webhook dependencies (`crate::http::eventsub`) --
    /// `None` until `crate::lib::try_build_eventsub_state` succeeds (no
    /// binding keyring/spine config/Valkey connection configured, or the
    /// `waddles.core.rust-data-plane` flag is off); the route still mounts
    /// unconditionally and responds `503` in that case, matching every
    /// other fixed-platform receiver's graceful-degradation contract.
    pub eventsub: Option<Arc<eventsub::EventSubState>>,
}

impl AppState {
    /// Builds the shared application state from a loaded [`Config`] and the
    /// Prometheus [`prometheus::Registry`] created during telemetry init.
    /// Registers this service's base request metrics against `metrics` --
    /// see [`crate::telemetry::register_request_metrics`]. Must be called
    /// exactly once per `metrics` registry (a `prometheus::Registry` panics
    /// on duplicate registration).
    pub fn new(config: Config, metrics: prometheus::Registry) -> Self {
        let request_metrics = crate::telemetry::register_request_metrics(&metrics);
        Self {
            config: Arc::new(config),
            metrics: Arc::new(metrics),
            request_metrics,
            started_at: Instant::now(),
            eventsub: None,
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

/// Builds the control-plane router: `/health` and `/healthz`.
///
/// // TODO(M5): connectors/intake -- blocked on M1 penguin-connectors +
/// // M2 (compiler/SDKs/hub-api hooks). The generic webhook intake
/// // (`POST /intake/webhook/{tenant}/{source}`), the JWT intake
/// // (`POST /intake/events`), and the six platform normalizers/receivers
/// // mount into this router once those crates land -- see spec S4.1,
/// // S10. Nothing here fakes that surface: an unmounted route 404s,
/// // which is the honest state of an unimplemented milestone.
pub fn router(state: AppState) -> Router {
    let base = Router::new()
        .route("/health", get(health::liveness))
        .route("/healthz", get(health::healthz))
        .with_state(state.clone());

    // `eventsub::router` mounts `POST /eventsub/twitch/webhook` -- see
    // `eventsub`'s module doc for why it's a separately-built, merged-in
    // `Router` rather than folded into the routes above.
    base.merge(eventsub::router(state.clone()))
        .layer(axum::middleware::from_fn_with_state(
            state,
            record_http_metrics,
        ))
        .layer(TraceLayer::new_for_http())
}

/// Builds the secondary Prometheus metrics router, bound to its own port
/// (`METRICS_PORT`, default 9090) per `rules/critical-rules.md`
/// Observability -- kept separate from the control-plane router so a
/// scraper never needs auth and never shares a listener with user traffic.
pub fn metrics_router(state: AppState) -> Router {
    Router::new()
        .route("/metrics", get(health::metrics))
        .with_state(state)
}
