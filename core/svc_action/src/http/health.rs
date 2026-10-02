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
use axum::http::StatusCode;
use axum::Json;
use serde::Serialize;

use crate::error::ApiError;
use crate::http::AppState;

/// Readiness probe response body -- `GET /healthz`.
#[derive(Debug, Serialize)]
pub struct HealthzBody {
    pub status: &'static str,
    /// Fix/executor-link-heartbeat: `false` while zero executor sessions
    /// are live -- `status`/HTTP code already reflect this; this field
    /// lets an operator see it without cross-referencing `host_api_
    /// connected_executors`.
    pub executor_connected: bool,
}

/// `GET /healthz` -- readiness for the Kubernetes probe: `false` the
/// instant zero executor sessions are live (no grace period -- Kubernetes
/// should stop routing new work here immediately, well before `/health`
/// considers restarting the pod). Never checks any other dependency.
pub async fn healthz(State(state): State<AppState>) -> (StatusCode, Json<HealthzBody>) {
    let executor_connected = state.connections.active().is_some();
    let status = if executor_connected {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    };
    (
        status,
        Json(HealthzBody {
            status: if executor_connected {
                "ok"
            } else {
                "no_executor"
            },
            executor_connected,
        }),
    )
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
    /// Fix/executor-link-heartbeat: seconds since the last live executor
    /// session, `0` while one is active.
    pub executor_outage_seconds: u64,
}

/// `GET /health` -- rich: process liveness plus a snapshot of configured
/// (not yet connectivity-probed) dependencies, AND (fix/executor-link-
/// heartbeat) whether the host-API executor link has been down for more
/// than `EXECUTOR_GRACE_SECONDS`. Readiness (`/healthz`) fails immediately
/// at zero executor sessions; liveness only after the grace period, so a
/// pod stuck with no executor actually gets restarted by Kubernetes
/// instead of sitting `Running` forever while silently dead-lettering
/// everything -- the alpha 2026-10-02 incident this is fixing. `crate::db`
/// does not perform a real connectivity check in this scaffold, so
/// `database` reports `configured` rather than `connected` until executor
/// integration lands real dependency probes per §12.6; that gap is
/// unrelated to, and must never gate, this liveness check.
pub async fn health(State(state): State<AppState>) -> (StatusCode, Json<HealthBody>) {
    let cfg = &state.config.cli;
    let dependencies = vec![DependencyStatus {
        name: "database",
        configured: !cfg.db_host.is_empty(),
        detail: format!("{}:{}/{}", cfg.db_host, cfg.db_port, cfg.db_name),
    }];
    let outage = state.connections.duration_without_executor();
    let grace = state.config.cli.executor_grace();
    let status = if outage >= grace {
        StatusCode::SERVICE_UNAVAILABLE
    } else {
        StatusCode::OK
    };
    (
        status,
        Json(HealthBody {
            status: if status == StatusCode::OK {
                "ok"
            } else {
                "unhealthy"
            },
            uptime_seconds: state.started_at.elapsed().as_secs(),
            dependencies,
            executor_outage_seconds: outage.as_secs(),
        }),
    )
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
    use std::sync::Arc;

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

    /// regression: executor stuck on terminated svc pod after rollout
    /// (alpha 2026-10-02) -- readiness must be false the instant zero
    /// executor sessions are live.
    #[tokio::test]
    async fn healthz_reports_no_executor_with_zero_sessions() {
        let (status, Json(body)) = healthz(State(test_state())).await;
        assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(body.status, "no_executor");
        assert!(!body.executor_connected);
    }

    /// Readiness flips back to `ok` the moment an executor session is
    /// registered -- the recovery half of the transition.
    #[tokio::test]
    async fn healthz_recovers_once_an_executor_connects() {
        let state = test_state();
        state
            .connections
            .set_active(crate::host_api::test_connection());
        let (status, Json(body)) = healthz(State(state)).await;
        assert_eq!(status, StatusCode::OK);
        assert!(body.executor_connected);
    }

    /// Liveness must still be `ok` immediately after zero executor
    /// sessions begin -- it only fails once `EXECUTOR_GRACE_SECONDS` has
    /// elapsed.
    #[tokio::test]
    async fn health_reports_ok_with_no_executor_within_grace_period() {
        let (status, Json(body)) = health(State(test_state())).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body.status, "ok");
        assert_eq!(body.dependencies.len(), 1);
        let db = body
            .dependencies
            .iter()
            .find(|d| d.name == "database")
            .unwrap();
        assert!(db.configured);
        assert_eq!(body.executor_outage_seconds, 0);
    }

    /// regression: executor stuck on terminated svc pod after rollout
    /// (alpha 2026-10-02) -- liveness must fail once the zero-executor
    /// outage exceeds `EXECUTOR_GRACE_SECONDS`.
    #[tokio::test]
    async fn health_fails_once_executor_outage_exceeds_grace_period() {
        let cli = CliConfig::parse_from(["svc-action", "--executor-grace-seconds", "0"]);
        let config = Config {
            cli,
            db_password: Secret::new("x"),
            envelope_binding_keys: None,
            discord_bot_token: None,
            db_reader_password: None,
        };
        let state = AppState::new(
            config,
            prometheus::Registry::new(),
            Arc::new(crate::host_api::ConnectionRegistry::new()),
        );
        assert!(state.connections.active().is_none());
        tokio::time::sleep(std::time::Duration::from_millis(1100)).await;
        let (status, Json(body)) = health(State(state)).await;
        assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(body.status, "unhealthy");
        assert!(body.executor_outage_seconds >= 1);
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
