//! Liveness (`/health`) and readiness (`/healthz`) probes, plus the
//! Prometheus `/metrics` handler mounted on the secondary metrics router.
//! Endpoint names/ports match `docs/superpowers/specs/2026-09-14-rust-data-
//! plane-design.md` SS4.2: `/health`, `/healthz`, `/metrics` on `:8201` /
//! `:9090`.
//!
//! Liveness only proves the process is alive and serving HTTP; readiness
//! additionally reports configured dependencies without ever crashing the
//! process on a transient dependency outage -- see
//! `rules/critical-rules.md` Observability. Same reporting pattern as
//! `core/svc_streaming/src/http/health.rs` (the M4 reference template):
//! dependencies report `configured` (host/port present), not `connected`,
//! until a later chunk wires a real connectivity check
//! (`// TODO(M4)` in `crate::lib`).

use axum::extract::State;
use axum::Json;
use serde::Serialize;

use crate::error::ApiError;
use crate::http::AppState;

/// Liveness probe response body.
#[derive(Debug, Serialize)]
pub struct LivenessBody {
    pub status: &'static str,
    pub uptime_seconds: u64,
}

/// `GET /health` -- liveness only: the process is up and answering HTTP.
/// Never checks external dependencies; a slow DB must not fail liveness
/// and trigger a restart loop.
pub async fn liveness(State(state): State<AppState>) -> Json<LivenessBody> {
    Json(LivenessBody {
        status: "ok",
        uptime_seconds: state.started_at.elapsed().as_secs(),
    })
}

/// Per-dependency readiness status.
#[derive(Debug, Serialize)]
pub struct DependencyStatus {
    pub name: &'static str,
    pub configured: bool,
    pub detail: String,
}

/// Readiness probe response body.
#[derive(Debug, Serialize)]
pub struct ReadinessBody {
    pub status: &'static str,
    pub dependencies: Vec<DependencyStatus>,
}

/// `GET /healthz` -- readiness: reports whether configured dependencies
/// (Postgres, Valkey) look present. Neither is actually dialed here --
/// SeaORM/spine connection wiring is `// TODO(M4)`, blocked on M2's
/// executor/compiler and `penguin-spine` landing in parallel.
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
            name: "hub_api",
            configured: !cfg.hub_api_url.is_empty(),
            detail: cfg.hub_api_url.clone(),
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
    async fn liveness_reports_ok() {
        let Json(body) = liveness(State(test_state())).await;
        assert_eq!(body.status, "ok");
    }

    #[tokio::test]
    async fn readiness_reports_three_dependencies() {
        let Json(body) = readiness(State(test_state())).await;
        assert_eq!(body.status, "ok");
        assert_eq!(body.dependencies.len(), 3);
        let db = body
            .dependencies
            .iter()
            .find(|d| d.name == "database")
            .unwrap();
        assert!(db.configured);
        assert_eq!(db.detail, "localhost:5432/waddlebot");
    }

    #[tokio::test]
    async fn metrics_renders_base_metrics_without_error() {
        // `AppState::new` registers the `up` gauge (and request
        // counter/histogram) eagerly, so `/metrics` is never an empty body
        // even before the first request is served.
        let body = metrics(State(test_state())).await.expect("must not error");
        assert!(body.contains("svc_process_up 1"));
    }
}
