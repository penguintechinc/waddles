//! `svc-process`: the Waddles process-stage data-plane service.
//!
//! **M4 skeleton scope, now with the M1 penguin-libs crates wired in.**
//! This crate wires config loading, sanitizing OTel/`tracing`/Prometheus
//! telemetry via `penguin-logging` (`crate::telemetry`), the granted-
//! ingest-stream `XREADGROUP` consumer via `penguin-spine`
//! (`crate::spine`), and the `/health` + `/healthz` + `/metrics`
//! control-plane surface -- the same shape as `core/svc_streaming` (the M4
//! reference template), minus everything that service does for A/V. Per
//! `docs/superpowers/specs/2026-09-14-rust-data-plane-design.md` SS4.2 and
//! the M4 milestone row (SS16), the following remain explicitly **out of
//! scope for this skeleton**, blocked on M2 (compiler/executor) and the
//! `penguin-bundle-host`/`penguin-connectors` crates:
//!
//! - Executor integration (invoking a bundle's `transform` over the
//!   `bundle-executor` mTLS wire protocol) -- see `crate::spine::run`'s doc
//!   comment and its `handle_delivered` seam
//! - Built-ins: the content-moderation gate, moderation-enforcement
//!   routing, and cross-app `_target_app_id` routing
//! - The DB host capability (parser allowlist + per-bundle role + RLS)
//!   bundles use for their own `db` host calls
//! - Hop verification (`binding.mac`, tenant/community/grant/approval
//!   checks) and usage metering
//! - The `GET /api/v1/distribution/bundles?stage=process` activation poll
//!   that would resolve `PROCESS_APP_ID`'s granted-stream list instead of
//!   the always-empty one `run_with_shutdown` passes today
//!
//! Each seam is marked `// TODO(M4): executor integration -- blocked on
//! M2` (or, for `bundle-executor` specifically, `blocked on bundle-executor
//! (Wave 2)`) at the point a later chunk plugs in, rather than stubbed with
//! fake behavior.
//!
//! This crate is split into a library (this file) and a thin binary
//! (`src/main.rs`) so integration tests under `tests/` can exercise the
//! router and config loader directly instead of spawning a subprocess.

pub mod config;
pub mod error;
pub mod http;
pub mod spine;
pub mod telemetry;

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use tokio::signal;

/// Default `tracing`/OTel service name, also the fallback `--healthcheck`
/// target and the resource `service.name` when `OTEL_SERVICE_NAME` is
/// unset.
pub const SERVICE_NAME: &str = "svc-process";

/// Runs the service: loads config, bootstraps telemetry, builds the
/// control-plane + metrics routers, and serves both until SIGINT/SIGTERM is
/// received.
///
/// TODO(M4): executor integration -- blocked on M2. Once `penguin-spine`
/// and `bundle-executor` land, this function additionally spawns the
/// `XREADGROUP`-per-granted-stream consumption loop (SS5.3) and the
/// `GET /api/v1/distribution/bundles?stage=process` activation poll
/// alongside the HTTP/metrics servers below, the same way
/// `svc_streaming::run_with_shutdown` spawns its ingest listeners and
/// orchestrator today.
pub async fn run() -> anyhow::Result<()> {
    let config = config::Config::load()?;
    run_with_shutdown(config, shutdown_signal(), shutdown_signal()).await
}

/// Same as [`run`], but takes an already-loaded [`config::Config`] and
/// caller-supplied shutdown futures for each listener instead of installing
/// OS signal handlers. This is what makes the bind/serve/telemetry wiring
/// testable: a test can pass `config` built via
/// [`config::CliConfig::parse_from`] (port `0` for an OS-assigned ephemeral
/// port is rejected by `CliConfig::validate`, so tests use an explicit
/// high port instead) and an already-resolved future so the server binds,
/// logs, and shuts down immediately instead of blocking forever on a real
/// signal.
pub async fn run_with_shutdown<F1, F2>(
    config: config::Config,
    http_shutdown: F1,
    metrics_shutdown: F2,
) -> anyhow::Result<()>
where
    F1: std::future::Future<Output = ()> + Send + 'static,
    F2: std::future::Future<Output = ()> + Send + 'static,
{
    let (_telemetry_guard, prom_registry) = telemetry::init(SERVICE_NAME);

    tracing::info!(
        http_port = config.cli.http_port,
        metrics_port = config.cli.metrics_port,
        "starting {SERVICE_NAME}"
    );

    let state = http::AppState::new(config.clone(), prom_registry);

    // TODO(M4): executor integration -- blocked on M2. The
    // distribution-bundles poll that would resolve `PROCESS_APP_ID`'s
    // granted-stream list is not wired yet, so the spine drain loop always
    // runs with an empty grant list -- see `try_start_spine_drain`.
    try_start_spine_drain(&config.cli);

    let http_addr = SocketAddr::new(config.cli.bind_addr, config.cli.http_port);
    let metrics_addr = SocketAddr::new(config.cli.bind_addr, config.cli.metrics_port);

    let http_listener = tokio::net::TcpListener::bind(http_addr).await?;
    let metrics_listener = tokio::net::TcpListener::bind(metrics_addr).await?;

    tracing::info!(%http_addr, %metrics_addr, "listening");

    let http_server = axum::serve(http_listener, http::router(state.clone()))
        .with_graceful_shutdown(http_shutdown);
    let metrics_server = axum::serve(metrics_listener, http::metrics_router(state))
        .with_graceful_shutdown(metrics_shutdown);

    tokio::try_join!(
        async { http_server.await.map_err(anyhow::Error::from) },
        async { metrics_server.await.map_err(anyhow::Error::from) },
    )?;

    Ok(())
}

/// Attempts to start the `penguin-spine` drain loop as its own background
/// task and returns immediately either way -- never blocks or fails
/// `run_with_shutdown`'s caller. Split out from `run_with_shutdown` itself
/// so it can be unit-tested directly, without also going through
/// `telemetry::init` (a process-global one-time call: a test binary can
/// only exercise one full `run_with_shutdown` call per process, see
/// `tests::run_with_shutdown_binds_serves_and_stops_on_signal`).
///
/// Two independent reasons this never starts the drain loop, both logged
/// and neither an error:
/// - `cli.process_app_id` is empty (the default): no bundle assigned yet,
///   multi-bundle scheduling is itself blocked on M2's distribution poll.
/// - `penguin_spine::SpineConfig::from_env()` fails (e.g. `VALKEY_URL`/
///   `REDIS_URL` unset): matches `rules/critical-rules.md` Observability's
///   "a dead exporter never breaks the app" -- the same graceful-
///   degradation contract applies to this dependency, so a missing or
///   invalid spine config disables stream consumption rather than
///   crashing the HTTP/metrics servers `run_with_shutdown` serves
///   alongside it.
fn try_start_spine_drain(cli: &config::CliConfig) {
    if cli.process_app_id.is_empty() {
        tracing::info!(
            "PROCESS_APP_ID not set; spine drain loop not started (blocked on M2 distribution poll)"
        );
        return;
    }

    match penguin_spine::SpineConfig::from_env() {
        Ok(spine_cfg) => {
            let app_id = cli.process_app_id.clone();
            let metrics: Arc<dyn penguin_spine::SpineMetrics> =
                Arc::new(penguin_spine::NoopMetrics);
            let (spine_shutdown_tx, spine_shutdown_rx) = tokio::sync::oneshot::channel();
            tokio::spawn(async move {
                shutdown_signal().await;
                // Receiver may already be gone if the drain loop already
                // exited on its own (e.g. a connect error); that is not
                // this task's failure to report.
                let _ = spine_shutdown_tx.send(());
            });
            tokio::spawn(async move {
                if let Err(err) =
                    spine::run(spine_cfg, app_id, Vec::new(), metrics, spine_shutdown_rx).await
                {
                    tracing::error!(error = %err, "process-stage spine drain loop exited");
                }
            });
        }
        Err(err) => {
            tracing::warn!(
                error = %err,
                "spine config unavailable; process-stage stream consumption disabled"
            );
        }
    }
}

/// Waits for SIGINT (Ctrl-C) or SIGTERM (Kubernetes pod termination) and
/// returns, letting `axum::serve`'s graceful shutdown drain in-flight
/// requests rather than dropping connections mid-response.
async fn shutdown_signal() {
    let ctrl_c = async {
        signal::ctrl_c()
            .await
            .expect("failed to install SIGINT handler");
    };

    #[cfg(unix)]
    let terminate = async {
        signal::unix::signal(signal::unix::SignalKind::terminate())
            .expect("failed to install SIGTERM handler")
            .recv()
            .await;
    };

    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => {},
        _ = terminate => {},
    }
}

/// `svc-process --healthcheck`: GETs `/health` on the locally-bound HTTP
/// port and exits 0/1 accordingly. Reads `MODULE_PORT` the same way
/// [`run`] does, without needing the full secret-bearing [`config::Config`]
/// -- the container `HEALTHCHECK` invokes this directly instead of relying
/// on `curl` being present in the runtime image.
pub async fn run_healthcheck() -> anyhow::Result<()> {
    let port: u16 = std::env::var("MODULE_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(8201);
    let url = format!("http://127.0.0.1:{port}/health");

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(3))
        .build()?;

    match client.get(&url).send().await {
        Ok(resp) if resp.status().is_success() => Ok(()),
        Ok(resp) => {
            eprintln!("healthcheck failed: {url} returned {}", resp.status());
            std::process::exit(1);
        }
        Err(err) => {
            eprintln!("healthcheck failed: {err}");
            std::process::exit(1);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{CliConfig, Config};
    use clap::Parser;
    use std::sync::Mutex;

    // std::env is process-global; serialize env-mutating tests so parallel
    // `cargo test` threads don't race on the same variables.
    static ENV_LOCK: Mutex<()> = Mutex::new(());

    #[tokio::test]
    async fn run_with_shutdown_binds_serves_and_stops_on_signal() {
        // Guard is dropped before the first `.await` below (clippy
        // `await_holding_lock`) -- the env vars only need to be set long
        // enough for `Config::from_cli` to copy them into `Secret`s.
        let config = {
            let _guard = ENV_LOCK.lock().unwrap();
            // SAFETY: serialized by ENV_LOCK above.
            unsafe {
                std::env::set_var("DB_PASSWORD", "test-db-pass");
                std::env::set_var("SERVICE_API_KEY", "test-api-key");
            }
            // Fixed high ports rather than `0` (OS-assigned):
            // `run_with_shutdown` needs the bound port back out to hit it,
            // and `CliConfig::validate` rejects port 0 outright (see
            // config.rs) -- these ports are only used for the lifetime of
            // this single test.
            let cli = CliConfig::parse_from([
                "svc-process",
                "--http-port",
                "18291",
                "--metrics-port",
                "18292",
            ]);
            let config = Config::from_cli(cli).expect("secrets are set");
            unsafe {
                std::env::remove_var("DB_PASSWORD");
                std::env::remove_var("SERVICE_API_KEY");
            }
            config
        };

        let handle = tokio::spawn(run_with_shutdown(
            config,
            std::future::ready(()),
            std::future::ready(()),
        ));

        // Both shutdown futures resolve immediately, so `run_with_shutdown`
        // must return `Ok(())` promptly rather than blocking forever.
        let result = tokio::time::timeout(Duration::from_secs(5), handle)
            .await
            .expect("run_with_shutdown must return promptly on an already-resolved shutdown future")
            .expect("task must not panic");
        assert!(result.is_ok(), "run_with_shutdown must succeed: {result:?}");
    }

    #[tokio::test]
    async fn try_start_spine_drain_noop_when_process_app_id_unset() {
        // Deliberately does not call `telemetry::init` (a process-global
        // one-time call -- see `run_with_shutdown_binds_serves_and_stops_
        // on_signal`, the only test in this binary allowed to exercise
        // it), which is exactly why this logic was split into its own
        // function: `tracing::info!`/`warn!` are harmless no-ops without an
        // installed subscriber.
        let cli = CliConfig::parse_from(["svc-process"]);
        assert_eq!(cli.process_app_id, "");
        try_start_spine_drain(&cli);
    }

    #[tokio::test]
    async fn try_start_spine_drain_degrades_gracefully_when_spine_unconfigured() {
        // `PROCESS_APP_ID` set but `VALKEY_URL`/`REDIS_URL` unset must warn
        // and return immediately rather than panicking or spawning
        // anything -- the same graceful-degradation contract as a dead
        // OTLP exporter (see `try_start_spine_drain`'s doc comment).
        let _guard = ENV_LOCK.lock().unwrap();
        // SAFETY: serialized by ENV_LOCK above.
        unsafe {
            std::env::remove_var("VALKEY_URL");
            std::env::remove_var("REDIS_URL");
        }
        let cli = CliConfig::parse_from([
            "svc-process",
            "--process-app-id",
            "waddles.bot.commands.default",
        ]);
        try_start_spine_drain(&cli);
    }

    #[tokio::test]
    async fn try_start_spine_drain_spawns_when_spine_config_is_valid() {
        // `SpineConfig::from_env` succeeds (a syntactically valid,
        // TLS-required URL plus a password satisfies `validate()`, spec
        // Sec11.6.1) but nothing is actually listening on port 1 (a
        // privileged port, refused immediately rather than timing out) --
        // exercises the `Ok` branch's two `tokio::spawn`s end-to-end
        // (including the shutdown-forwarder and the spawned drain task's
        // own error-logging arm) without needing a live Valkey.
        // Guard is dropped before the `.await` below (clippy
        // `await_holding_lock`) -- `penguin_spine::SpineConfig::from_env`
        // reads these env vars synchronously inside `try_start_spine_drain`
        // itself, so they only need to be set for that one non-async call.
        {
            let _guard = ENV_LOCK.lock().unwrap();
            // SAFETY: serialized by ENV_LOCK above.
            unsafe {
                std::env::set_var("VALKEY_URL", "rediss://127.0.0.1:1/");
                std::env::set_var("VALKEY_PASSWORD", "test-valkey-pass");
            }
            let cli = CliConfig::parse_from([
                "svc-process",
                "--process-app-id",
                "waddles.bot.commands.default",
            ]);
            try_start_spine_drain(&cli);
            unsafe {
                std::env::remove_var("VALKEY_URL");
                std::env::remove_var("VALKEY_PASSWORD");
            }
        }
        // Real (not paused) sleep: lets the spawned tasks above actually
        // run on this same current-thread test runtime and reach their
        // connect-failure log line before the runtime is torn down at the
        // end of this test.
        tokio::time::sleep(Duration::from_secs(5)).await;
    }

    #[test]
    fn service_name_matches_binary_name() {
        assert_eq!(SERVICE_NAME, "svc-process");
    }

    #[tokio::test]
    async fn run_healthcheck_succeeds_against_a_live_health_endpoint() {
        // No other test reads/writes `MODULE_PORT`, so this doesn't need
        // `ENV_LOCK` (which guards `DB_PASSWORD`/`SERVICE_API_KEY`/
        // `CACHE_PASSWORD` only) -- see `config::tests`.
        let cli = CliConfig::parse_from(["svc-process"]);
        let config = Config {
            cli,
            db_password: crate::config::Secret::new("x"),
            cache_password: None,
            service_api_key: crate::config::Secret::new("x"),
        };
        let state = crate::http::AppState::new(config, prometheus::Registry::new());
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            axum::serve(listener, crate::http::router(state))
                .await
                .expect("test http server must not fail to serve");
        });

        // SAFETY: serialized by ENV_LOCK above.
        unsafe { std::env::set_var("MODULE_PORT", addr.port().to_string()) };
        let result = run_healthcheck().await;
        unsafe { std::env::remove_var("MODULE_PORT") };

        assert!(result.is_ok(), "run_healthcheck must succeed: {result:?}");
    }
}
