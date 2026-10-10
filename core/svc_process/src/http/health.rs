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
use axum::http::StatusCode;
use axum::Json;
use serde::Serialize;

use crate::error::ApiError;
use crate::http::AppState;

/// Liveness probe response body.
#[derive(Debug, Serialize)]
pub struct LivenessBody {
    pub status: &'static str,
    pub uptime_seconds: u64,
    /// Informational only -- does NOT affect `status`/HTTP code. Seconds
    /// since the last live executor session, `0` while one is active. See
    /// the periodic zero-executor ERROR log (`crate::host_api::
    /// run_zero_executor_watchdog`) and `host_api_connected_executors` for
    /// the operator-facing signal.
    pub executor_outage_seconds: u64,
}

/// `GET /health` -- liveness: the process is up, answering HTTP, and not
/// deadlocked. Deliberately excludes executor presence -- regression:
/// readiness gated on executor connection deadlocked rollouts (alpha
/// 2026-10-02): a prior revision also failed *liveness* after
/// `EXECUTOR_GRACE_SECONDS` with zero executors, which combined with the
/// ClusterIP-routes-only-to-Ready-pods behavior to both stall the rollout
/// AND restart-loop the stuck pod. Client (executor) presence must never
/// restart this server -- zero executors or a network partition on the
/// executor side must not cause a restart storm here. A slow/absent DB or
/// cache must still never fail liveness either.
pub async fn liveness(State(state): State<AppState>) -> (StatusCode, Json<LivenessBody>) {
    let outage = state.connections.duration_without_executor();
    (
        StatusCode::OK,
        Json(LivenessBody {
            status: "ok",
            uptime_seconds: state.started_at.elapsed().as_secs(),
            executor_outage_seconds: outage.as_secs(),
        }),
    )
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
    /// Informational only -- does NOT affect `status`/HTTP code. See the
    /// `host_api_connected_executors` gauge and the periodic zero-executor
    /// ERROR log for the operator-facing signal; readiness itself must
    /// reflect only this service's own internal health.
    pub executor_connected: bool,
}

/// `GET /healthz` -- Kubernetes readiness: `false` only while the legacy
/// single-consumer drain loop (`crate::lib::try_start_process_loop`) is
/// configured (`PROCESS_APP_ID` set) but not yet (re)connected and reading
/// -- this service's own internal health, nothing more. Executor presence
/// is deliberately excluded (`executor_connected` is reported for operator
/// visibility only, never gates `status`/the HTTP code) -- regression:
/// readiness gated on executor connection deadlocked rollouts (alpha
/// 2026-10-02): bundle-executors dial this service through its ClusterIP
/// Service, which only routes to Ready pods, so a new pod gated on
/// "executor connected" could never become Ready (no executor would ever
/// dial it) and the rollout stalled forever. Also reports whether
/// configured dependencies (Postgres, Valkey) look present. Neither DB/
/// cache is actually dialed here -- SeaORM/spine connection wiring is
/// `// TODO(M4)`, blocked on M2's executor/compiler and `penguin-spine`
/// landing in parallel.
pub async fn readiness(
    State(state): State<AppState>,
) -> (axum::http::StatusCode, Json<ReadinessBody>) {
    let cfg = &state.config.cli;
    // regression: watermark id INT2 vs i32 decode killed active-set consumer (alpha 2026-10-02)
    // -- the multi-tenant changelog-consumer path doesn't set
    // `PROCESS_APP_ID`, so `consumer_loop_configured` must also consider
    // `multi_tenant_consumer_configured` or this check never gates on it.
    let consumer_loop_configured = !cfg.process_app_id.is_empty()
        || state
            .multi_tenant_consumer_configured
            .load(std::sync::atomic::Ordering::Relaxed);
    let consumer_loop_running = state
        .consumer_loop_ready
        .load(std::sync::atomic::Ordering::Relaxed);
    let executor_connected = state.connections.active().is_some();
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
        DependencyStatus {
            name: "consumer_loop",
            configured: consumer_loop_configured,
            detail: if !consumer_loop_configured {
                "not configured".to_string()
            } else if consumer_loop_running {
                "running".to_string()
            } else {
                "not running".to_string()
            },
        },
    ];
    let ready = !consumer_loop_configured || consumer_loop_running;
    let code = if ready {
        axum::http::StatusCode::OK
    } else {
        axum::http::StatusCode::SERVICE_UNAVAILABLE
    };
    let status = if ready { "ok" } else { "degraded" };
    (
        code,
        Json(ReadinessBody {
            status,
            dependencies,
            executor_connected,
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
        let cli = CliConfig::parse_from(["svc-process"]);
        let config = Config {
            cli,
            db_password: Secret::new("x"),
            cache_password: None,
            service_api_key: Secret::new("x"),
            envelope_binding_keys: None,
            db_reader_password: None,
            bundle_db_password: None,
            bundle_reputation_password: None,
            bundle_economy_password: None,
        };
        AppState::new(
            config,
            prometheus::Registry::new(),
            Arc::new(crate::host_api::ConnectionRegistry::new()),
        )
    }

    /// Liveness must be `ok` immediately after zero executor sessions
    /// begin.
    #[tokio::test]
    async fn liveness_reports_ok_with_no_executor() {
        let (status, Json(body)) = liveness(State(test_state())).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body.status, "ok");
        assert_eq!(body.executor_outage_seconds, 0);
    }

    /// regression: readiness gated on executor connection deadlocked
    /// rollouts (alpha 2026-10-02) -- liveness must stay `ok` indefinitely
    /// with zero executor sessions, however long the outage. Client
    /// (executor) presence must never restart this server.
    #[tokio::test]
    async fn liveness_stays_ok_past_the_old_executor_grace_period() {
        let cli = CliConfig::parse_from(["svc-process", "--executor-grace-seconds", "0"]);
        // `executor_grace()` floors at 1s even when the CLI value is `0`;
        // sleep past that floor to prove liveness still ignores it.
        let config = Config {
            cli,
            db_password: Secret::new("x"),
            cache_password: None,
            service_api_key: Secret::new("x"),
            envelope_binding_keys: None,
            db_reader_password: None,
            bundle_db_password: None,
            bundle_reputation_password: None,
            bundle_economy_password: None,
        };
        let state = AppState::new(
            config,
            prometheus::Registry::new(),
            Arc::new(crate::host_api::ConnectionRegistry::new()),
        );
        // Establish `zero_since` now, then wait past the 1s floor.
        assert!(state.connections.active().is_none());
        tokio::time::sleep(std::time::Duration::from_millis(1100)).await;
        let (status, Json(body)) = liveness(State(state)).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body.status, "ok");
        assert!(body.executor_outage_seconds >= 1);
    }

    /// regression: readiness gated on executor connection deadlocked
    /// rollouts (alpha 2026-10-02) -- readiness must stay `ok` with zero
    /// executor sessions; a bundle-executor dials this service through its
    /// ClusterIP Service, which only routes to Ready pods, so gating
    /// readiness on executor presence meant a freshly-rolled pod could
    /// never become Ready (no executor would ever reach it) and the
    /// rollout stalled forever.
    #[tokio::test]
    async fn readiness_is_ok_with_no_executor_once_the_loop_is_unconfigured() {
        let (status, Json(body)) = readiness(State(test_state())).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body.status, "ok");
        assert!(!body.executor_connected);
        assert_eq!(body.dependencies.len(), 4);
        let db = body
            .dependencies
            .iter()
            .find(|d| d.name == "database")
            .unwrap();
        assert!(db.configured);
        assert_eq!(db.detail, "localhost:5432/waddlebot");
    }

    /// Builds an `AppState` with `PROCESS_APP_ID` set to `app_id` (empty =
    /// unconfigured) and `consumer_loop_ready` forced to `running`, for
    /// `readiness`'s own consumer-loop transition tests below.
    fn readiness_state(app_id: &str, running: bool) -> AppState {
        let mut cli = CliConfig::parse_from(["svc-process"]);
        cli.process_app_id = app_id.to_string();
        let config = Config {
            cli,
            db_password: Secret::new("x"),
            cache_password: None,
            service_api_key: Secret::new("x"),
            envelope_binding_keys: None,
            db_reader_password: None,
            bundle_db_password: None,
            bundle_reputation_password: None,
            bundle_economy_password: None,
        };
        let state = AppState::new(
            config,
            prometheus::Registry::new(),
            Arc::new(crate::host_api::ConnectionRegistry::new()),
        );
        state
            .consumer_loop_ready
            .store(running, std::sync::atomic::Ordering::Relaxed);
        state
    }

    // regression: drain loop exited on NOGROUP while the pod stayed Ready
    // forever (alpha 2026-10-02). `PROCESS_APP_ID` unset is "nothing to
    // wait for" on the consumer-loop side.
    #[tokio::test]
    async fn readiness_is_ok_when_process_app_id_is_unset() {
        let state = readiness_state("", true);
        let (code, Json(body)) = readiness(State(state)).await;
        assert_eq!(code, axum::http::StatusCode::OK);
        let consumer_loop = body
            .dependencies
            .iter()
            .find(|d| d.name == "consumer_loop")
            .unwrap();
        assert!(!consumer_loop.configured);
    }

    // regression: drain loop exited on NOGROUP while the pod stayed Ready
    // forever (alpha 2026-10-02)
    #[tokio::test]
    async fn readiness_is_503_when_the_consumer_loop_is_configured_but_not_running() {
        let state = readiness_state("waddles.core.example.ping", false);
        state
            .connections
            .set_active(crate::host_api::test_connection());

        let (code, Json(body)) = readiness(State(state)).await;
        assert_eq!(code, axum::http::StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(body.status, "degraded");
        let consumer_loop = body
            .dependencies
            .iter()
            .find(|d| d.name == "consumer_loop")
            .unwrap();
        assert!(consumer_loop.configured);
        assert_eq!(consumer_loop.detail, "not running");
    }

    // regression: drain loop exited on NOGROUP while the pod stayed Ready
    // forever (alpha 2026-10-02)
    #[tokio::test]
    async fn readiness_is_ok_once_the_configured_consumer_loop_is_running_and_executor_connected() {
        let state = readiness_state("waddles.core.example.ping", true);
        state
            .connections
            .set_active(crate::host_api::test_connection());

        let (code, Json(body)) = readiness(State(state)).await;
        assert_eq!(code, axum::http::StatusCode::OK);
        assert_eq!(body.status, "ok");
    }

    /// regression: readiness gated on executor connection deadlocked
    /// rollouts (alpha 2026-10-02) -- readiness must stay `ok` even once
    /// the consumer loop itself is running, with zero executor sessions
    /// live; `executor_connected` is still reported, informationally.
    #[tokio::test]
    async fn readiness_is_ok_when_consumer_loop_running_but_no_executor() {
        let state = readiness_state("waddles.core.example.ping", true);
        let (code, Json(body)) = readiness(State(state)).await;
        assert_eq!(code, axum::http::StatusCode::OK);
        assert_eq!(body.status, "ok");
        assert!(!body.executor_connected);
    }

    /// `executor_connected` tracks executor state in both directions
    /// without ever affecting the readiness HTTP code.
    #[tokio::test]
    async fn readiness_reports_executor_connected_without_gating_status() {
        let state = test_state();
        let (status, Json(body)) = readiness(State(state.clone())).await;
        assert_eq!(status, StatusCode::OK);
        assert!(!body.executor_connected);

        state
            .connections
            .set_active(crate::host_api::test_connection());

        let (status, Json(body)) = readiness(State(state)).await;
        assert_eq!(status, StatusCode::OK);
        assert!(body.executor_connected);
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
