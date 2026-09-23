//! HTTP layer: axum router wiring for the control-plane surface
//! (`/health`, `/healthz`) and the secondary Prometheus `/metrics` router.
//!
//! This is deliberately the entire HTTP surface of the M4 skeleton --
//! `docs/superpowers/specs/2026-09-14-rust-data-plane-design.md` SS4.2's
//! `GET /api/v1/distribution/bundles?stage=process` poll and the
//! capability-scoped host API on `:8301` (mTLS, executor-facing) are
//! `// TODO(M4)`, blocked on M2's compiler/executor and `penguin-spine`
//! landing in parallel. See `crate::lib` for the seam markers.

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

/// Builds the control-plane router: `/health` (liveness) and `/healthz`
/// (readiness), both unauthenticated -- health probes never require a
/// credential. Bound to `MODULE_PORT` (default `:8201`).
pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/health", get(health::liveness))
        .route("/healthz", get(health::readiness))
        .with_state(state.clone())
        .layer(axum::middleware::from_fn_with_state(
            state,
            record_http_metrics,
        ))
        .layer(TraceLayer::new_for_http())
}

/// Builds the secondary Prometheus metrics router, bound to its own port
/// (`METRICS_PORT`, default `:9090`) per `rules/critical-rules.md`
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
    use axum::body::to_bytes;
    use axum::http::StatusCode;
    use clap::Parser;
    use tower::ServiceExt;

    fn test_state() -> AppState {
        let cli = CliConfig::parse_from(["svc-process"]);
        let config = Config {
            cli,
            db_password: Secret::new("x"),
            cache_password: None,
            service_api_key: Secret::new("x"),
            envelope_binding_keys: None,
        };
        AppState::new(config, prometheus::Registry::new())
    }

    #[tokio::test]
    async fn health_route_returns_200() {
        let app = router(test_state());
        let resp = app
            .oneshot(
                axum::http::Request::builder()
                    .uri("/health")
                    .body(axum::body::Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn healthz_route_returns_200() {
        let app = router(test_state());
        let resp = app
            .oneshot(
                axum::http::Request::builder()
                    .uri("/healthz")
                    .body(axum::body::Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn metrics_router_serves_metrics_on_its_own_router() {
        let app = metrics_router(test_state());
        let resp = app
            .oneshot(
                axum::http::Request::builder()
                    .uri("/metrics")
                    .body(axum::body::Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        assert!(String::from_utf8_lossy(&body).contains("svc_process_up 1"));
    }

    #[tokio::test]
    async fn control_plane_router_records_request_metrics() {
        let state = test_state();
        let app = router(state.clone());
        let _ = app
            .oneshot(
                axum::http::Request::builder()
                    .uri("/health")
                    .body(axum::body::Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let rendered = crate::telemetry::render_metrics(&state.metrics).unwrap();
        assert!(rendered.contains("svc_process_http_requests_total"));
    }
}
