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
pub mod webhook;

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
    /// Generic registered-webhook ingress (`crate::http::webhook`, P6a);
    /// `None` until the route registry is wired -- the route then answers
    /// `503` (fail loud), never an unverified accept.
    pub webhook: Option<Arc<crate::connector::webhook::WebhookIngress>>,
    /// Per-receiver spine-connect readiness, shared with `crate::lib`'s
    /// `try_start_twitch_irc`/`try_start_discord`/`try_start_twitch_outbound`
    /// -- backs `GET /readyz` (`crate::http::health::readyz`). Regression:
    /// one-shot valkey probe disabled discord receiver while the pod stayed
    /// Ready forever (alpha 2026-10-02).
    pub receiver_readiness: Arc<crate::ReceiverReadiness>,
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
            webhook: None,
            receiver_readiness: Arc::new(crate::ReceiverReadiness::new()),
        }
    }
}

/// Bounded metric/trace label for a request path: fixed routes pass through,
/// parameterized routes collapse to their template (so a webhook route key or
/// callback key never becomes a label), everything else is `unmatched`.
pub(crate) fn metric_path(path: &str) -> String {
    const FIXED: [&str; 4] = ["/health", "/healthz", "/readyz", "/eventsub/twitch/webhook"];
    if FIXED.contains(&path) {
        path.to_string()
    } else if path.starts_with("/intake/webhook/") {
        "/intake/webhook/{route_key}".to_string()
    } else if path.starts_with("/eventsub/twitch/webhook/") {
        "/eventsub/twitch/webhook/{callback_key}".to_string()
    } else {
        "unmatched".to_string()
    }
}

/// Records every request into [`AppState::request_metrics`]: a labeled
/// counter and a latency histogram. Applied to the whole control-plane
/// router so `/metrics` always has data once the service has served at
/// least one request (the `up` gauge covers the window before that).
async fn record_http_metrics(State(state): State<AppState>, req: Request, next: Next) -> Response {
    let method = req.method().to_string();
    let path = metric_path(req.uri().path());
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
/// The generic registered-webhook intake (`POST /intake/webhook/{route_key}`,
/// [`webhook`]) mounts here.
///
/// // TODO(M5): connectors/intake -- blocked on M1 penguin-connectors +
/// // M2 (compiler/SDKs/hub-api hooks). The JWT intake
/// // (`POST /intake/events`), and the six platform normalizers/receivers
/// // mount into this router once those crates land -- see spec S4.1,
/// // S10. Nothing here fakes that surface: an unmounted route 404s,
/// // which is the honest state of an unimplemented milestone.
pub fn router(state: AppState) -> Router {
    let base = Router::new()
        .route("/health", get(health::liveness))
        .route("/healthz", get(health::healthz))
        .route("/readyz", get(health::readyz))
        .with_state(state.clone());

    // `eventsub::router` mounts `POST /eventsub/twitch/webhook` -- see
    // `eventsub`'s module doc for why it's a separately-built, merged-in
    // `Router` rather than folded into the routes above.
    base.merge(eventsub::router(state.clone()))
        .merge(webhook::router(state.clone()))
        .layer(axum::middleware::from_fn_with_state(
            state,
            record_http_metrics,
        ))
        // Span carries the bounded template, never the raw URI (route keys).
        .layer(
            TraceLayer::new_for_http().make_span_with(|req: &Request<axum::body::Body>| {
                tracing::info_span!(
                    "http_request",
                    method = %req.method(),
                    path = %metric_path(req.uri().path()),
                )
            }),
        )
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

#[cfg(test)]
mod metric_path_tests {
    use super::metric_path;

    #[test]
    fn label_is_the_template_never_the_raw_path() {
        assert_eq!(
            metric_path("/intake/webhook/super-secret-route-key"),
            "/intake/webhook/{route_key}"
        );
        assert_eq!(
            metric_path("/eventsub/twitch/webhook/cb123"),
            "/eventsub/twitch/webhook/{callback_key}"
        );
        assert_eq!(metric_path("/healthz"), "/healthz");
        assert_eq!(metric_path("/random/attacker/path-1"), "unmatched");
    }
}
