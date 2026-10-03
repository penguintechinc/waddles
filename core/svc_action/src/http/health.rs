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

/// `GET /healthz` response body -- informational only, never gates the
/// container-level `svc-action --healthcheck` probe status.
#[derive(Debug, Serialize)]
pub struct HealthzBody {
    pub status: &'static str,
    /// Informational: whether an executor session is currently live.
    /// Surfaced for operator visibility only -- see
    /// `host_api_connected_executors` for the authoritative gauge.
    /// regression: readiness gated on executor connection deadlocked
    /// rollouts (alpha 2026-10-02) -- a bundle-executor dials this service
    /// through its ClusterIP Service, which only routes to Ready pods, so
    /// gating readiness/liveness on executor presence created a deadlock
    /// during rollouts (new pod never Ready -> never gets an executor).
    pub executor_connected: bool,
}

/// `GET /healthz` -- the `svc-action --healthcheck` CLI subcommand's own
/// probe target (Docker-level healthcheck, not the Kubernetes
/// readinessProbe, which is `/readyz`): always `200 OK` once the process is
/// up and serving HTTP, regardless of executor state. Client (executor)
/// presence must never gate this service's own health -- zero executors or
/// a network partition must not drive a restart storm.
pub async fn healthz(State(state): State<AppState>) -> (StatusCode, Json<HealthzBody>) {
    let executor_connected = state.connections.active().is_some();
    (
        StatusCode::OK,
        Json(HealthzBody {
            status: "ok",
            executor_connected,
        }),
    )
}

/// `GET /readyz` response body.
#[derive(Debug, Serialize)]
pub struct ReadyBody {
    pub status: &'static str,
    pub configured: bool,
    pub running: bool,
    /// Informational only -- does NOT affect `status`/HTTP code. See the
    /// `host_api_connected_executors` gauge and the periodic zero-executor
    /// ERROR log for the operator-facing signal; readiness itself must
    /// reflect only this service's own internal health.
    pub executor_connected: bool,
}

/// `GET /readyz` -- Kubernetes readiness: `503` only while the action-stage
/// dispatch loop (`crate::lib::try_start_dispatch`) is configured
/// (`ACTION_APP_ID` set) but not yet (re)connected and reading -- this
/// service's own internal health, nothing more. Executor presence is
/// deliberately excluded (`executor_connected` is reported for operator
/// visibility only, never gates `status`/the HTTP code) -- regression:
/// readiness gated on executor connection deadlocked rollouts (alpha
/// 2026-10-02): bundle-executors dial this service through its ClusterIP
/// Service, which only routes to Ready pods, so a new pod gated on
/// "executor connected" could never become Ready (no executor would ever
/// dial it) and the rollout stalled forever. Zero executors still
/// dead-letters work for redelivery (`crate::dispatch::handle_delivered`)
/// -- it degrades, it does not drop traffic or block deploys.
pub async fn readyz(State(state): State<AppState>) -> (axum::http::StatusCode, Json<ReadyBody>) {
    let configured = !state.config.cli.action_app_id.is_empty();
    let running = state
        .consumer_loop_ready
        .load(std::sync::atomic::Ordering::Relaxed);
    let executor_connected = state.connections.active().is_some();
    // regression: watermark id INT2 vs i32 decode killed active-set consumer (alpha 2026-10-02)
    // -- the multi-tenant changelog consumer runs unconditionally
    // alongside the legacy dispatch loop (never mutually exclusive), so it
    // gates readiness independently via its own flag.
    let changelog_consumer_ready = state
        .changelog_consumer_ready
        .load(std::sync::atomic::Ordering::Relaxed);
    let ok = (!configured || running) && changelog_consumer_ready;
    let code = if ok {
        axum::http::StatusCode::OK
    } else {
        axum::http::StatusCode::SERVICE_UNAVAILABLE
    };
    (
        code,
        Json(ReadyBody {
            status: if ok { "ok" } else { "degraded" },
            configured,
            running,
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
/// executor side must not cause a restart storm here. `crate::db` does not
/// perform a real connectivity check in this scaffold, so `database`
/// reports `configured` rather than `connected`; that gap is unrelated to,
/// and must never gate, this liveness check.
pub async fn health(State(state): State<AppState>) -> (StatusCode, Json<HealthBody>) {
    let cfg = &state.config.cli;
    let dependencies = vec![DependencyStatus {
        name: "database",
        configured: !cfg.db_host.is_empty(),
        detail: format!("{}:{}/{}", cfg.db_host, cfg.db_port, cfg.db_name),
    }];
    let outage = state.connections.duration_without_executor();
    (
        StatusCode::OK,
        Json(HealthBody {
            status: "ok",
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

    /// Builds an `AppState` with `ACTION_APP_ID` set to `app_id` (empty =
    /// unconfigured) and the dispatch loop's `consumer_loop_ready` forced to
    /// `running`, for `readyz`'s own transition tests below.
    fn readyz_state(app_id: &str, running: bool) -> AppState {
        let mut cli = CliConfig::parse_from(["svc-action"]);
        cli.action_app_id = app_id.to_string();
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
        state
            .consumer_loop_ready
            .store(running, std::sync::atomic::Ordering::Relaxed);
        state
    }

    // regression: readiness gated on executor connection deadlocked
    // rollouts (alpha 2026-10-02). `ACTION_APP_ID` unset is "nothing to
    // wait for" -- readiness is `ok` regardless of executor presence, which
    // is reported informationally only.
    #[tokio::test]
    async fn readyz_is_ok_when_action_app_id_is_unset_even_with_no_executor() {
        let (code, Json(body)) = readyz(State(readyz_state("", true))).await;
        assert_eq!(code, axum::http::StatusCode::OK);
        assert!(!body.configured);
        assert!(!body.executor_connected);
    }

    // regression: drain loop exited on NOGROUP while the pod stayed Ready
    // forever (alpha 2026-10-02)
    #[tokio::test]
    async fn readyz_is_503_when_configured_but_not_running() {
        let state = readyz_state("waddles.core.example.ping", false);
        state
            .connections
            .set_active(crate::host_api::test_connection());
        let (code, Json(body)) = readyz(State(state)).await;
        assert_eq!(code, axum::http::StatusCode::SERVICE_UNAVAILABLE);
        assert!(body.configured);
        assert!(!body.running);
    }

    /// regression: readiness gated on executor connection deadlocked
    /// rollouts (alpha 2026-10-02) -- readiness must stay `ok` with zero
    /// executor sessions once the dispatch loop itself is running; a
    /// bundle-executor dials this service through its ClusterIP Service,
    /// which only routes to Ready pods, so gating readiness on executor
    /// presence meant a freshly-rolled pod could never become Ready (no
    /// executor would ever reach it) and the rollout stalled forever.
    #[tokio::test]
    async fn readyz_is_ok_when_running_with_no_executor() {
        let state = readyz_state("waddles.core.example.ping", true);
        let (code, Json(body)) = readyz(State(state)).await;
        assert_eq!(code, axum::http::StatusCode::OK);
        assert!(body.running);
        assert!(!body.executor_connected);
    }

    // regression: drain loop exited on NOGROUP while the pod stayed Ready
    // forever (alpha 2026-10-02)
    #[tokio::test]
    async fn readyz_is_ok_once_the_configured_loop_is_running_and_executor_connected() {
        let state = readyz_state("waddles.core.example.ping", true);
        state
            .connections
            .set_active(crate::host_api::test_connection());
        let (code, Json(body)) = readyz(State(state)).await;
        assert_eq!(code, axum::http::StatusCode::OK);
        assert!(body.executor_connected);
    }

    /// regression: readiness gated on executor connection deadlocked
    /// rollouts (alpha 2026-10-02) -- `/healthz` (the container-level
    /// `--healthcheck` probe target) must stay `ok` with zero executor
    /// sessions; it reports `executor_connected` informationally only.
    #[tokio::test]
    async fn healthz_is_ok_with_zero_executor_sessions() {
        let (status, Json(body)) = healthz(State(test_state())).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body.status, "ok");
        assert!(!body.executor_connected);
    }

    #[tokio::test]
    async fn healthz_reports_executor_connected_once_one_connects() {
        let state = test_state();
        state
            .connections
            .set_active(crate::host_api::test_connection());
        let (status, Json(body)) = healthz(State(state)).await;
        assert_eq!(status, StatusCode::OK);
        assert!(body.executor_connected);
    }

    #[tokio::test]
    async fn health_reports_ok_with_no_executor() {
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

    /// regression: readiness gated on executor connection deadlocked
    /// rollouts (alpha 2026-10-02) -- liveness must stay `ok` indefinitely
    /// with zero executor sessions, however long the outage. Client
    /// (executor) presence must never restart this server -- zero
    /// executors or a network partition must not cause a restart storm.
    #[tokio::test]
    async fn health_stays_ok_past_the_old_executor_grace_period() {
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
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body.status, "ok");
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
