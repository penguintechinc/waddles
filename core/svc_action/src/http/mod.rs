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
    /// `true` once the action-stage dispatch loop
    /// (`crate::lib::try_start_dispatch`) is connected and actively
    /// reading -- defaults `true` (nothing to wait for) when
    /// `ACTION_APP_ID` is unset. Backs `GET /readyz` (combined with
    /// `connections` below: readiness is loop-running AND
    /// executor-connected). regression: drain loop exited on NOGROUP
    /// (alpha 2026-10-02)
    pub consumer_loop_ready: Arc<std::sync::atomic::AtomicBool>,
    /// `true` once the multi-tenant changelog consumer
    /// (`crate::changelog_consumer::run`) has completed its initial full
    /// active-set read -- independent of `consumer_loop_ready` (this path
    /// runs unconditionally alongside the legacy dispatch loop, never
    /// mutually exclusive with it, see `try_start_changelog_consumer`'s own
    /// doc). Defaults `false`; `try_start_changelog_consumer` flips it to
    /// `true` immediately if `DB_READER_PASSWORD` is unset (nothing to wait
    /// for), or once `initial_state` succeeds otherwise.
    // regression: watermark id INT2 vs i32 decode killed active-set consumer (alpha 2026-10-02)
    // -- `GET /readyz` previously had no signal at all for this path (only
    // ever checked `ACTION_APP_ID`), so the pod stayed `Ready` with no
    // changelog consumer running after a startup decode failure.
    pub changelog_consumer_ready: Arc<std::sync::atomic::AtomicBool>,
    /// Fix/executor-link-heartbeat: `/health`/`/healthz`/`/readyz` read this
    /// directly so liveness/readiness reflect whether an executor session is
    /// actually live, not just "the HTTP server is answering" -- the alpha
    /// 2026-10-02 incident this exists to catch left every pod
    /// `Running`/`Ready` while silently dead-lettering everything.
    pub connections: Arc<crate::host_api::ConnectionRegistry>,
}

impl AppState {
    /// Builds the shared application state from a loaded [`Config`], the
    /// Prometheus [`prometheus::Registry`] created during telemetry init,
    /// and the host-API [`crate::host_api::ConnectionRegistry`]. Registers
    /// this service's base request metrics against `metrics` -- see
    /// [`crate::telemetry::register_request_metrics`].
    pub fn new(
        config: Config,
        metrics: prometheus::Registry,
        connections: Arc<crate::host_api::ConnectionRegistry>,
    ) -> Self {
        let request_metrics = crate::telemetry::register_request_metrics(&metrics);
        // `changelog_consumer_ready` starts `true` (nothing to wait for)
        // unless `DB_READER_PASSWORD` is actually configured -- matches
        // `try_start_changelog_consumer`'s own early-return branch, which
        // sets it `true` explicitly for the same "not configured" case.
        let changelog_consumer_configured = config.db_reader_password.is_some();
        Self {
            config: Arc::new(config),
            metrics: Arc::new(metrics),
            request_metrics,
            started_at: Instant::now(),
            consumer_loop_ready: Arc::new(std::sync::atomic::AtomicBool::new(true)),
            changelog_consumer_ready: Arc::new(std::sync::atomic::AtomicBool::new(
                !changelog_consumer_configured,
            )),
            connections,
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
        .route("/readyz", get(health::readyz))
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
            discord_bot_token: None,
            db_reader_password: None,
        };
        AppState::new(
            config,
            prometheus::Registry::new(),
            Arc::new(crate::host_api::ConnectionRegistry::new()),
        )
    }

    /// Drives a request through the full `router()` (middleware included)
    /// so `record_http_metrics` and `TraceLayer` are actually exercised,
    /// not just the bare handler functions -- see `http::health`'s own
    /// tests for the handler-level coverage.
    #[tokio::test]
    async fn router_serves_healthz_and_records_metrics() {
        // regression: readiness gated on executor connection deadlocked
        // rollouts (alpha 2026-10-02) -- `/healthz` (the container-level
        // `--healthcheck` probe target) must stay `ok` with zero executor
        // sessions; see `http::health`'s own transition tests for the full
        // before/after coverage. The metrics-recording assertion below
        // holds regardless of status code.
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
