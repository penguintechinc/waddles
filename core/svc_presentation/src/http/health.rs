//! Liveness (`/health`) and readiness (`/readyz`) probes, plus the
//! Prometheus `/metrics` handler mounted on the secondary metrics router.
//!
//! Liveness only proves the process is alive and serving HTTP; readiness
//! additionally reports configured dependencies without ever crashing the
//! process on a transient dependency outage -- see
//! `rules/critical-rules.md` Observability.

use axum::extract::State;
use axum::Json;
use serde::Serialize;
use utoipa::ToSchema;

use crate::error::ApiError;
use crate::http::AppState;

/// Liveness probe response body.
#[derive(Debug, Serialize, ToSchema)]
pub struct LivenessBody {
    pub status: &'static str,
    pub uptime_seconds: u64,
}

/// `GET /health` -- liveness only: the process is up and answering HTTP.
/// Never checks external dependencies; a slow DB must not fail liveness
/// and trigger a restart loop.
#[utoipa::path(
    get,
    path = "/health",
    responses((status = 200, description = "Process is alive", body = LivenessBody))
)]
pub async fn liveness(State(state): State<AppState>) -> Json<LivenessBody> {
    Json(LivenessBody {
        status: "ok",
        uptime_seconds: state.started_at.elapsed().as_secs(),
    })
}

/// Per-dependency readiness status.
#[derive(Debug, Serialize, ToSchema)]
pub struct DependencyStatus {
    pub name: &'static str,
    pub configured: bool,
    pub detail: String,
}

/// Readiness probe response body.
#[derive(Debug, Serialize, ToSchema)]
pub struct ReadinessBody {
    pub status: &'static str,
    pub dependencies: Vec<DependencyStatus>,
}

/// `GET /readyz` -- readiness: reports whether configured dependencies
/// (DB, cache, hub-api JWKS endpoint for PUSH verification) look present.
/// No real connectivity probe against any of them in this scaffold -- only
/// "is a host/URL configured", matching `core/svc_streaming/src/http/
/// health.rs`'s own documented P1-era simplification.
#[utoipa::path(
    get,
    path = "/readyz",
    responses((status = 200, description = "Dependency configuration snapshot", body = ReadinessBody))
)]
pub async fn readiness(State(state): State<AppState>) -> Json<ReadinessBody> {
    let cfg = &state.config.cli;
    let dependencies = vec![
        DependencyStatus {
            name: "database",
            configured: !cfg.db_host.is_empty(),
            detail: format!("{}:{}/{}", cfg.db_host, cfg.db_port, cfg.db_name),
        },
        DependencyStatus {
            name: "cache",
            configured: !cfg.cache_host.is_empty(),
            detail: format!("{}:{}", cfg.cache_host, cfg.cache_port),
        },
        DependencyStatus {
            name: "push_jwks",
            configured: !cfg.push_jwks_url.is_empty(),
            detail: cfg.push_jwks_url.clone(),
        },
    ];
    Json(ReadinessBody {
        status: "ok",
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
    use sea_orm::{DatabaseBackend, MockDatabase};

    fn test_state() -> AppState {
        let cli = CliConfig::parse_from(["svc-presentation"]);
        let config = Config {
            cli,
            db_password: Secret::new("x"),
            cache_password: None,
        };
        let db = MockDatabase::new(DatabaseBackend::Postgres).into_connection();
        AppState::new(config, prometheus::Registry::new(), db)
    }

    #[tokio::test]
    async fn liveness_reports_ok() {
        let Json(body) = liveness(State(test_state())).await;
        assert_eq!(body.status, "ok");
    }

    #[tokio::test]
    async fn readiness_reports_three_dependencies() {
        let Json(body) = readiness(State(test_state())).await;
        assert_eq!(body.status, "ok");
        assert_eq!(body.dependencies.len(), 3);
        let names: Vec<&str> = body.dependencies.iter().map(|d| d.name).collect();
        assert_eq!(names, vec!["database", "cache", "push_jwks"]);
    }

    #[tokio::test]
    async fn metrics_renders_base_metrics_without_error() {
        // `AppState::new` registers the `up` gauge (and request
        // counter/histogram) eagerly, so `/metrics` is never an empty body
        // even before the first request is served.
        let body = metrics(State(test_state())).await.expect("must not error");
        assert!(body.contains("svc_presentation_up 1"));
    }
}
