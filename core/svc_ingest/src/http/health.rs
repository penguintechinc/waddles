//! `/health` (rich), `/healthz` (bare, for the Kubernetes probes), and the
//! Prometheus `/metrics` handler mounted on the secondary metrics router --
//! see `docs/superpowers/specs/2026-09-14-rust-data-plane-design.md` S13.4.
//!
//! `/health` proves the process is alive and additionally reports
//! configured dependencies without ever crashing the process on a
//! transient dependency outage; `/healthz` is a bare liveness probe for
//! Kubernetes, deliberately as cheap as possible.

use axum::extract::State;
use axum::Json;
use serde::Serialize;

use crate::error::ApiError;
use crate::http::AppState;

/// Per-dependency configuration snapshot reported on `/health`.
#[derive(Debug, Serialize)]
pub struct DependencyStatus {
    pub name: &'static str,
    pub configured: bool,
    pub detail: String,
}

/// `GET /health` response body.
#[derive(Debug, Serialize)]
pub struct HealthBody {
    pub status: &'static str,
    pub uptime_seconds: u64,
    pub dependencies: Vec<DependencyStatus>,
}

/// `GET /health` -- rich liveness: the process is up and answering HTTP,
/// plus a configuration snapshot of this service's dependencies. This
/// skeleton has exactly one: the hub-api distribution poll target, which
/// nothing calls yet (see the `TODO(M5)` seam in `src/lib.rs`) -- reporting
/// `configured` here is a config check, not a connectivity check, matching
/// `svc_streaming`'s own scaffold-stage readiness handler.
pub async fn liveness(State(state): State<AppState>) -> Json<HealthBody> {
    let dependencies = vec![DependencyStatus {
        name: "hub_api",
        configured: !state.config.cli.hub_api_url.is_empty(),
        detail: state.config.cli.hub_api_url.clone(),
    }];
    Json(HealthBody {
        status: "ok",
        uptime_seconds: state.started_at.elapsed().as_secs(),
        dependencies,
    })
}

/// `GET /healthz` -- bare liveness probe for Kubernetes: no dependency
/// reporting, no JSON body, just `200 ok`. Never checks external
/// dependencies; a slow/unreachable dependency must not fail liveness and
/// trigger a restart loop.
pub async fn healthz() -> &'static str {
    "ok"
}

/// `GET /metrics` (secondary router, `METRICS_PORT`) -- Prometheus text
/// exposition. A registry gather/encode failure returns 500 rather than
/// panicking; a scrape failure must never crash the process.
pub async fn metrics(State(state): State<AppState>) -> Result<String, ApiError> {
    crate::telemetry::render_metrics(&state.metrics).map_err(ApiError::Internal)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{CliConfig, Config};
    use crate::http::AppState;
    use clap::Parser;

    fn test_state() -> AppState {
        let cli = CliConfig::parse_from(["svc-ingest"]);
        let config = Config::from_cli(cli).expect("defaults require no secrets");
        AppState::new(config, prometheus::Registry::new())
    }

    #[tokio::test]
    async fn liveness_reports_ok_and_one_dependency() {
        let Json(body) = liveness(State(test_state())).await;
        assert_eq!(body.status, "ok");
        assert_eq!(body.dependencies.len(), 1);
        assert_eq!(body.dependencies[0].name, "hub_api");
        assert!(body.dependencies[0].configured);
    }

    #[tokio::test]
    async fn healthz_returns_bare_ok() {
        assert_eq!(healthz().await, "ok");
    }

    #[tokio::test]
    async fn metrics_renders_base_metrics_without_error() {
        // `AppState::new` registers the `up` gauge (and request
        // counter/histogram) eagerly, so `/metrics` is never an empty body
        // even before the first request is served.
        let body = metrics(State(test_state())).await.expect("must not error");
        assert!(body.contains("svc_ingest_up 1"));
    }
}
