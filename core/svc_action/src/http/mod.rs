//! HTTP layer: axum router wiring for the control-plane surface. This
//! skeleton exposes only `/health` and `/healthz` on the primary router and
//! `/metrics` on the secondary metrics router -- see `src/http/health.rs`.
//!
//! TODO(M3): executor integration -- blocked on M2. The dispatch-facing
//! `/api/v1/*` surface (if any is needed beyond the Valkey spine drain
//! loop) and the JWT/service-key auth middleware `core/svc_streaming`'s
//! `src/http/auth.rs` implements attach here once `penguin-spine` and the
//! bundle-executor wire protocol land.

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
}

impl AppState {
    /// Builds the shared application state from a loaded [`Config`] and the
    /// Prometheus [`prometheus::Registry`] created during telemetry init.
    /// Registers this service's base request metrics against `metrics` --
    /// see [`crate::telemetry::register_request_metrics`].
    pub fn new(config: Config, metrics: prometheus::Registry) -> Self {
        let request_metrics = crate::telemetry::register_request_metrics(&metrics);
        Self {
            config: Arc::new(config),
            metrics: Arc::new(metrics),
            request_metrics,
            started_at: Instant::now(),
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

/// Builds the control-plane router: `/health` (rich) and `/healthz`
/// (bare-ok, Kubernetes probe target). Unauthenticated -- no request body
/// this service returns today carries anything beyond configuration
/// snapshot/liveness state.
pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/health", get(health::health))
        .route("/healthz", get(health::healthz))
        .with_state(state.clone())
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{CliConfig, Config, Secret};
    use axum::body::Body;
    use axum::http::{Request as HttpRequest, StatusCode};
    use clap::Parser;
    use tower::ServiceExt as _;

    fn test_state() -> AppState {
        let cli = CliConfig::parse_from(["svc-action"]);
        let config = Config {
            cli,
            db_password: Secret::new("x"),
            envelope_binding_keys: None,
        };
        AppState::new(config, prometheus::Registry::new())
    }

    /// Drives a request through the full `router()` (middleware included)
    /// so `record_http_metrics` and `TraceLayer` are actually exercised,
    /// not just the bare handler functions -- see `http::health`'s own
    /// tests for the handler-level coverage.
    #[tokio::test]
    async fn router_serves_healthz_and_records_metrics() {
        let state = test_state();
        let response = router(state.clone())
            .oneshot(
                HttpRequest::builder()
                    .uri("/healthz")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        // The middleware recorded exactly this request into the shared
        // Prometheus registry.
        let rendered = crate::telemetry::render_metrics(&state.metrics).unwrap();
        assert!(rendered.contains("svc_action_http_requests_total"));
    }

    #[tokio::test]
    async fn router_serves_health() {
        let state = test_state();
        let response = router(state)
            .oneshot(
                HttpRequest::builder()
                    .uri("/health")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn router_returns_404_for_unknown_routes() {
        let state = test_state();
        let response = router(state)
            .oneshot(
                HttpRequest::builder()
                    .uri("/does-not-exist")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn metrics_router_serves_prometheus_text() {
        let state = test_state();
        let response = metrics_router(state)
            .oneshot(
                HttpRequest::builder()
                    .uri("/metrics")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
    }
}
