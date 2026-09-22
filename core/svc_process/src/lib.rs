//! `svc-process`: the Waddles process-stage data-plane service.
//!
//! **M4 skeleton scope only.** This crate currently wires config loading,
//! OTel/`tracing`/Prometheus telemetry, and the `/health` + `/healthz` +
//! `/metrics` control-plane surface -- the same shape as
//! `core/svc_streaming` (the M4 reference template), minus everything that
//! service does for A/V. Per
//! `docs/superpowers/specs/2026-09-14-rust-data-plane-design.md` SS4.2 and
//! the M4 milestone row (SS16), the following are explicitly **out of
//! scope for this skeleton** and blocked on M2 (compiler/executor) and the
//! M1 `penguin-spine`/`penguin-bundle-host`/`penguin-connectors` crates
//! landing in parallel:
//!
//! - Executor integration (invoking a bundle's `transform` over the
//!   `bundle-executor` mTLS wire protocol)
//! - Built-ins: the content-moderation gate, moderation-enforcement
//!   routing, and cross-app `_target_app_id` routing
//! - The DB host capability (parser allowlist + per-bundle role + RLS)
//!   bundles use for their own `db` host calls
//! - Hop verification (`binding.mac`, tenant/community/grant/approval
//!   checks) and usage metering
//!
//! Each seam is marked `// TODO(M4): executor integration -- blocked on
//! M2` at the point a later chunk plugs in, rather than stubbed with fake
//! behavior.
//!
//! This crate is split into a library (this file) and a thin binary
//! (`src/main.rs`) so integration tests under `tests/` can exercise the
//! router and config loader directly instead of spawning a subprocess.

pub mod config;
pub mod error;
pub mod http;
pub mod telemetry;

use std::net::SocketAddr;
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

    // TODO(M4): executor integration -- blocked on M2. This is where the
    // granted-stream consumption loop and the distribution-bundles poll
    // would be `tokio::spawn`ed, reading `config.cli.hub_api_url` /
    // `poll_interval_s` / `cache_host` / `cache_port` -- all already
    // present on `config::CliConfig` so this function only grows, it does
    // not need re-plumbing.

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
