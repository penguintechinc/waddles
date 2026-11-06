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
use crate::http::{AppState, RecordReadiness};

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
/// (DB, cache, ffmpeg) look present. `db::mod` does not yet perform a real
/// connectivity check in this scaffold, so every dependency reports
/// `configured` (host/binary present) rather than `connected` until the
/// owning chunk lands real health checks.
#[utoipa::path(
    get,
    path = "/readyz",
    responses((status = 200, description = "Dependency configuration snapshot", body = ReadinessBody))
)]
pub async fn readiness(State(state): State<AppState>) -> Json<ReadinessBody> {
    let cfg = &state.config.cli;
    let record = match &state.record {
        RecordReadiness::Enabled => DependencyStatus {
            name: "record",
            configured: true,
            detail: "s3 recording sink ready".to_string(),
        },
        RecordReadiness::Disabled(reason) => DependencyStatus {
            name: "record",
            configured: false,
            detail: format!("recording disabled: {reason}"),
        },
    };
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
            name: "ffmpeg",
            configured: cfg.ffmpeg_path.exists(),
            detail: cfg.ffmpeg_path.display().to_string(),
        },
        record,
    ];
    // Recording is an optional capability: its absence degrades the pod but
    // must not fail readiness (that would pull live ingest out of service
    // and kill every stream), so it reports "degraded" with HTTP 200.
    let status = if matches!(state.record, RecordReadiness::Enabled) {
        "ok"
    } else {
        "degraded"
    };
    Json(ReadinessBody {
        status,
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
        let cli = CliConfig::parse_from(["svc-streaming"]);
        let config = Config {
            cli,
            db_password: Secret::new("x"),
            cache_password: None,
            service_api_key: Secret::new("x"),
            jwt_hmac_secret: None,
        };
        AppState::new(config, prometheus::Registry::new())
    }

    #[tokio::test]
    async fn liveness_reports_ok() {
        let Json(body) = liveness(State(test_state())).await;
        assert_eq!(body.status, "ok");
    }

    #[tokio::test]
    async fn readiness_reports_four_dependencies() {
        let Json(body) = readiness(State(test_state())).await;
        assert_eq!(body.dependencies.len(), 4);
        let ffmpeg = body
            .dependencies
            .iter()
            .find(|d| d.name == "ffmpeg")
            .unwrap();
        // Reporting `configured: false` (ffmpeg absent on the host/CI
        // runner) must never panic -- the assertion below is on the
        // *shape*, not on ffmpeg actually being installed.
        assert_eq!(ffmpeg.detail, "/usr/bin/ffmpeg");
    }

    #[tokio::test]
    async fn readiness_is_degraded_with_the_reason_when_record_is_disabled() {
        let mut state = test_state();
        state.record = RecordReadiness::Disabled("S3_ENDPOINT is not set".to_string());
        let Json(body) = readiness(State(state)).await;
        assert_eq!(body.status, "degraded");
        let record = body
            .dependencies
            .iter()
            .find(|d| d.name == "record")
            .unwrap();
        assert!(!record.configured);
        assert!(
            record.detail.contains("S3_ENDPOINT is not set"),
            "{}",
            record.detail
        );
    }

    #[tokio::test]
    async fn readiness_is_ok_when_record_is_enabled() {
        let mut state = test_state();
        state.record = RecordReadiness::Enabled;
        let Json(body) = readiness(State(state)).await;
        assert_eq!(body.status, "ok");
        let record = body
            .dependencies
            .iter()
            .find(|d| d.name == "record")
            .unwrap();
        assert!(record.configured);
    }

    #[tokio::test]
    async fn uninitialised_record_state_never_claims_recording_works() {
        let Json(body) = readiness(State(test_state())).await;
        assert_eq!(body.status, "degraded");
    }

    #[tokio::test]
    async fn metrics_renders_base_metrics_without_error() {
        // `AppState::new` registers the `up` gauge (and request
        // counter/histogram) eagerly, so `/metrics` is never an empty body
        // even before the first request is served.
        let body = metrics(State(test_state())).await.expect("must not error");
        assert!(body.contains("svc_streaming_up 1"));
    }
}
