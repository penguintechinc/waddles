//! Health/readiness/`metrics` handlers.
//!
//! Endpoint shape follows §13.4 of
//! `docs/superpowers/specs/2026-09-14-rust-data-plane-design.md` (the
//! shared surface all four Rust data-plane services converge on via the
//! future `penguin-logging` crate, §4.9): `/health` is rich (process
//! liveness plus a dependency-configuration snapshot), `/healthz` is the
//! bare-`ok` Kubernetes liveness/readiness probe target, and `/metrics`
//! (mounted on the secondary metrics router) is Prometheus text
//! exposition. This differs slightly from `core/svc_streaming`'s current
//! bespoke `/health` (bare) + `/readyz` (rich) split -- that crate predates
//! this spec's health-endpoint unification and adopts the same shape in
//! M6.
//!
//! Never checks external dependencies for `/healthz`: a slow DB must not
//! fail liveness and trigger a restart loop -- see
//! `rules/critical-rules.md` Observability.

use axum::extract::State;
use axum::Json;
use serde::Serialize;

use crate::error::ApiError;
use crate::http::AppState;

/// Bare liveness probe response body -- `GET /healthz`.
#[derive(Debug, Serialize)]
pub struct HealthzBody {
    pub status: &'static str,
}

/// `GET /healthz` -- bare liveness for the Kubernetes probe: the process is
/// up and answering HTTP. Never checks external dependencies.
pub async fn healthz() -> Json<HealthzBody> {
    Json(HealthzBody { status: "ok" })
}

/// Per-dependency configuration status reported by the rich `/health`
/// endpoint.
#[derive(Debug, Serialize)]
pub struct DependencyStatus {
    pub name: &'static str,
    pub configured: bool,
    pub detail: String,
}

/// Rich health response body -- `GET /health`.
#[derive(Debug, Serialize)]
pub struct HealthBody {
    pub status: &'static str,
    pub uptime_seconds: u64,
    pub dependencies: Vec<DependencyStatus>,
}

/// `GET /health` -- rich: process liveness plus a snapshot of configured
/// (not yet connectivity-probed) dependencies. `crate::db` does not
/// perform a real connectivity check in this scaffold, so `database`
/// reports `configured` (host/account present) rather than `connected`
/// until executor integration (TODO(M3), blocked on M2) lands real
/// dependency probes per §12.6.
pub async fn health(State(state): State<AppState>) -> Json<HealthBody> {
    let cfg = &state.config.cli;
    let dependencies = vec![DependencyStatus {
        name: "database",
        configured: !cfg.db_host.is_empty(),
        detail: format!("{}:{}/{}", cfg.db_host, cfg.db_port, cfg.db_name),
    }];
    Json(HealthBody {
        status: "ok",
        uptime_seconds: state.started_at.elapsed().as_secs(),
        dependencies,
    })
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
    use crate::config::{CliConfig, Config, Secret};
    use clap::Parser;

    fn test_state() -> AppState {
        let cli = CliConfig::parse_from(["svc-action"]);
        let config = Config {
            cli,
            db_password: Secret::new("x"),
            envelope_binding_keys: None,
            secret_key: Secret::new("x"),
            discord_bot_token: None,
        };
        AppState::new(config, prometheus::Registry::new())
    }

    #[tokio::test]
    async fn healthz_reports_ok() {
        let Json(body) = healthz().await;
        assert_eq!(body.status, "ok");
    }

    #[tokio::test]
    async fn health_reports_one_dependency() {
        let Json(body) = health(State(test_state())).await;
        assert_eq!(body.status, "ok");
        assert_eq!(body.dependencies.len(), 1);
        let db = body
            .dependencies
            .iter()
            .find(|d| d.name == "database")
            .unwrap();
        assert!(db.configured);
    }

    #[tokio::test]
    async fn metrics_renders_base_metrics_without_error() {
        // `AppState::new` registers the `up` gauge (and request
        // counter/histogram) eagerly, so `/metrics` is never an empty body
        // even before the first request is served.
        let body = metrics(State(test_state())).await.expect("must not error");
        assert!(body.contains("svc_action_up 1"));
    }
}
