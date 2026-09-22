//! `svc-action`: the Waddles ACTION stage-runner (pipeline terminal stage).
//!
//! This crate is split into a library (this file) and a thin binary
//! (`src/main.rs`) so integration tests under `tests/` can exercise the
//! router and config loader directly instead of spawning a subprocess.
//!
//! Scope of this file (M3, "Rust service on the `svc_streaming` template"
//! per the M3 row of
//! `docs/superpowers/specs/2026-09-14-rust-data-plane-design.md`): health,
//! metrics, OTel wiring, config loading. Everything else is a deliberate
//! seam, not a silent gap -- see the `TODO(M3)` markers below and in
//! `src/http/mod.rs`/`src/db/mod.rs`.

pub mod config;
pub mod db;
pub mod error;
pub mod http;
pub mod telemetry;

// TODO(M3): executor integration -- blocked on M2 (`bundle_executor`,
// `penguin-spine`, `penguin-bundle-host`, all landing in parallel this
// wave). Once those crates exist, this file additionally wires:
//   - `pub mod dispatch` -- `XREADGROUP` each activated bundle's own
//     `:action` stream (single consumer group `{app_id}`), invoke
//     `dispatch` through the executor, classify
//     `transport-error.retryable`, apply retry-with-backoff
//     (`ACTION_MAX_RETRIES`/`ACTION_BASE_BACKOFF_MS`/`ACTION_MAX_BACKOFF_MS`,
//     §4.3), and write the outcome to `action_dispatch_log`
//     (`db::entities::action_dispatch_log`, already declared).
//   - `pub mod senders` -- Rust built-in platform senders (Discord, Slack,
//     YouTube, Kick REST; Twitch via the outbound relay `LPUSH`) reached by
//     bundles through the host API, never holding a platform credential
//     directly in a bundle.
//   - The `:8302` mTLS host-API listener (`HOST_API_PORT`) the
//     `svc-action-executor` deployment dials.
// None of this is stubbed here: a stub that "looks wired" but silently
// no-ops would be worse than an honest absence.

use std::net::SocketAddr;

use anyhow::Context as _;
use tokio::signal;

/// Default `tracing`/OTel service name, also the fallback `--healthcheck`
/// target and the resource `service.name` when `OTEL_SERVICE_NAME` is
/// unset.
pub const SERVICE_NAME: &str = "svc-action";

/// Runs the service: loads config, bootstraps telemetry, builds the
/// control-plane + metrics routers, and serves both until SIGINT/SIGTERM is
/// received.
pub async fn run() -> anyhow::Result<()> {
    let config = config::Config::load()?;
    run_with_shutdown(config, shutdown_signal(), shutdown_signal()).await
}

/// Same as [`run`], but takes an already-loaded [`config::Config`] and
/// caller-supplied shutdown futures for each listener instead of installing
/// OS signal handlers. This is what makes the bind/serve/telemetry wiring
/// testable: a test can pass `config` built via
/// [`config::CliConfig::parse_from`] (port `0` for an OS-assigned ephemeral
/// port) and an already-resolved future so the server binds, logs, and
/// shuts down immediately instead of blocking forever on a real signal.
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

    let http_addr = SocketAddr::new(config.cli.bind_addr, config.cli.http_port);
    let metrics_addr = SocketAddr::new(config.cli.bind_addr, config.cli.metrics_port);

    let http_listener = tokio::net::TcpListener::bind(http_addr)
        .await
        .context("binding the HTTP control-plane listener")?;
    let metrics_listener = tokio::net::TcpListener::bind(metrics_addr)
        .await
        .context("binding the metrics listener")?;

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

/// Probes `GET http://127.0.0.1:{port}/healthz`, returning `Ok(())` on any
/// 2xx response and `Err(<detail>)` otherwise (non-success status or
/// request failure). Split out from [`run_healthcheck`] so tests can
/// exercise every branch -- including the failure ones -- without
/// triggering that function's `std::process::exit(1)`.
async fn healthcheck_probe(port: u16) -> Result<(), String> {
    let url = format!("http://127.0.0.1:{port}/healthz");
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(3))
        .build()
        .map_err(|err| err.to_string())?;
    match client.get(&url).send().await {
        Ok(resp) if resp.status().is_success() => Ok(()),
        Ok(resp) => Err(format!("{url} returned {}", resp.status())),
        Err(err) => Err(format!("{url}: {err}")),
    }
}

/// `svc-action --healthcheck`: GETs `/healthz` on the locally-bound HTTP
/// port and exits 0/1 accordingly. Reads `MODULE_PORT` the same way
/// [`run`] does, without needing the full secret-bearing [`config::Config`]
/// -- the container `HEALTHCHECK` invokes this directly instead of relying
/// on `curl` being present in the runtime image.
pub async fn run_healthcheck() -> anyhow::Result<()> {
    let port: u16 = std::env::var("MODULE_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(8202);
    if let Err(detail) = healthcheck_probe(port).await {
        eprintln!("healthcheck failed: {detail}");
        std::process::exit(1);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{CliConfig, Config, Secret};
    use clap::Parser;
    use tokio::sync::Mutex;

    // std::env is process-global; serialize env-mutating tests so parallel
    // `cargo test` threads don't race on the same variables.
    // `tokio::sync::Mutex` (not `std::sync::Mutex`) -- its guard is `Send`
    // and safe to hold across an `.await`, which this test does (see
    // `crate::db`'s tests for the same pattern).
    static ENV_LOCK: Mutex<()> = Mutex::const_new(());

    fn ephemeral_config() -> Config {
        // Port `0` asks the OS for an unused ephemeral port -- avoids test
        // flakiness from a hardcoded port already in use.
        let cli = CliConfig::parse_from(["svc-action", "--http-port", "0", "--metrics-port", "0"]);
        Config {
            cli,
            db_password: Secret::new("test-password"),
        }
    }

    #[tokio::test]
    async fn run_with_shutdown_binds_and_shuts_down_cleanly() {
        let _guard = ENV_LOCK.lock().await;
        // SAFETY: serialized by ENV_LOCK; no other test reads OTEL env.
        unsafe { std::env::remove_var("OTEL_EXPORTER_OTLP_ENDPOINT") };
        let config = ephemeral_config();
        // Shutdown futures resolve immediately, so the servers bind, log,
        // and drain right away instead of blocking on a real OS signal.
        let result =
            run_with_shutdown(config, std::future::ready(()), std::future::ready(())).await;
        assert!(result.is_ok(), "expected clean shutdown, got {result:?}");
    }

    /// Spawns a minimal axum server on an OS-assigned ephemeral port,
    /// serving `/healthz` with the given status, and returns the bound
    /// port. The server task is detached -- it lives only as long as the
    /// test process.
    async fn spawn_healthz_server(status: axum::http::StatusCode) -> u16 {
        let app = axum::Router::new().route(
            "/healthz",
            axum::routing::get(move || async move { status }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("binds an ephemeral port");
        let port = listener.local_addr().expect("has a local addr").port();
        tokio::spawn(async move {
            axum::serve(listener, app).await.ok();
        });
        port
    }

    #[tokio::test]
    async fn healthcheck_probe_succeeds_against_a_live_healthz_endpoint() {
        let port = spawn_healthz_server(axum::http::StatusCode::OK).await;
        assert!(healthcheck_probe(port).await.is_ok());
    }

    #[tokio::test]
    async fn healthcheck_probe_fails_on_non_success_status() {
        let port = spawn_healthz_server(axum::http::StatusCode::INTERNAL_SERVER_ERROR).await;
        let err = healthcheck_probe(port).await.unwrap_err();
        assert!(err.contains("500"));
    }

    #[tokio::test]
    async fn healthcheck_probe_fails_when_nothing_is_listening() {
        // Bind then immediately drop -- guarantees an ephemeral port with
        // nothing listening on it.
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("binds an ephemeral port");
        let port = listener.local_addr().expect("has a local addr").port();
        drop(listener);
        assert!(healthcheck_probe(port).await.is_err());
    }

    #[tokio::test]
    async fn run_healthcheck_succeeds_when_module_port_env_points_at_a_live_server() {
        let _guard = ENV_LOCK.lock().await;
        let port = spawn_healthz_server(axum::http::StatusCode::OK).await;
        // SAFETY: serialized by ENV_LOCK.
        unsafe { std::env::set_var("MODULE_PORT", port.to_string()) };
        let result = run_healthcheck().await;
        unsafe { std::env::remove_var("MODULE_PORT") };
        assert!(result.is_ok(), "expected Ok, got {result:?}");
    }
}
