//! `svc-presentation`: the Waddles overlay/presentation service (P1
//! scaffold).
//!
//! This crate is split into a library (this file) and a thin binary
//! (`src/main.rs`) so integration tests under `tests/` can exercise the
//! router, config loader, and overlay-auth wiring directly instead of
//! spawning a subprocess -- same pattern as `core/svc_streaming`.
//!
//! P1 scope (this chunk): config, telemetry, DB connection, SeaORM
//! entities for this service's three tables, and the `overlay_auth`
//! VIEW/PUSH axum guards mounted on route-less sub-routers. See
//! `src/overlay/router.rs`'s module doc for the extension points P2
//! (render), P3 (live SSE/websocket), and P4 (push) build on top of.

pub mod config;
pub mod db;
pub mod error;
pub mod flags;
pub mod http;
pub mod images;
pub mod overlay;
pub mod telemetry;

use std::net::SocketAddr;

use anyhow::Context as _;
use tokio::signal;

/// Default `tracing`/OTel service name, also the fallback `--healthcheck`
/// target and the resource `service.name` when `OTEL_SERVICE_NAME` is
/// unset.
pub const SERVICE_NAME: &str = "svc-presentation";

/// Runs the service: loads config, bootstraps telemetry, connects to the
/// database, builds the control-plane + metrics routers, and serves
/// everything until SIGINT/SIGTERM is received.
pub async fn run() -> anyhow::Result<()> {
    let config = config::Config::load()?;
    run_with_shutdown(config, shutdown_signal(), shutdown_signal()).await
}

/// Same as [`run`], but takes an already-loaded [`config::Config`] and
/// caller-supplied shutdown futures for each listener instead of installing
/// OS signal handlers -- what makes the bind/serve/telemetry wiring
/// testable (a test passes port `0` for an OS-assigned ephemeral port and
/// an already-resolved future so the server binds, logs, and shuts down
/// immediately instead of blocking forever on a real signal).
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

    let db = db::get_or_connect(&config)
        .await
        .context("connecting to the database")?;

    let state = http::AppState::new(config.clone(), prom_registry, db);

    // Hourly purge of expired caption history (flag-gated inside the task).
    let retention_task = overlay::caption_store::spawn_retention_task(
        state.caption_store.clone(),
        state.captions_flag.clone(),
        overlay::caption_store::RETENTION,
        overlay::caption_store::PURGE_INTERVAL,
    );

    let http_addr = SocketAddr::new(config.cli.bind_addr, config.cli.http_port);
    let metrics_addr = SocketAddr::new(config.cli.bind_addr, config.cli.metrics_port);

    let http_listener = tokio::net::TcpListener::bind(http_addr).await?;
    let metrics_listener = tokio::net::TcpListener::bind(metrics_addr).await?;

    tracing::info!(%http_addr, %metrics_addr, "listening");

    let http_server = axum::serve(http_listener, http::router(state.clone()))
        .with_graceful_shutdown(http_shutdown);
    let metrics_server = axum::serve(metrics_listener, http::metrics_router(state))
        .with_graceful_shutdown(metrics_shutdown);

    let served = tokio::try_join!(
        async { http_server.await.map_err(anyhow::Error::from) },
        async { metrics_server.await.map_err(anyhow::Error::from) },
    );
    retention_task.abort();
    served?;

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

/// `svc-presentation --healthcheck`: GETs `/health` on the locally-bound
/// HTTP port and exits 0/1 accordingly. Reads `MODULE_PORT` the same way
/// [`run`] does, without needing the full secret-bearing [`config::Config`]
/// -- the container `HEALTHCHECK` invokes this directly instead of relying
/// on `curl` being present in the runtime image.
pub async fn run_healthcheck() -> anyhow::Result<()> {
    let port: u16 = std::env::var("MODULE_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(8207);
    let url = format!("http://127.0.0.1:{port}/health");

    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(3))
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
    use tokio::sync::Mutex;

    // MODULE_PORT is process-global; serialize the one test that sets it.
    // `tokio::sync::Mutex` (not `std::sync::Mutex`) -- its guard is `Send`
    // and safe to hold across an `.await`, which this test needs to do;
    // same precedent as `src/db/mod.rs`'s own `ENV_LOCK`.
    static ENV_LOCK: Mutex<()> = Mutex::const_new(());

    /// The success path only -- the two failure branches call
    /// `std::process::exit(1)` directly, which would terminate the whole
    /// test process if exercised in-process; that is an inherent
    /// limitation of a `--healthcheck` subcommand meant to set the
    /// container `HEALTHCHECK`'s exit code, not something a unit test can
    /// safely cover without subprocessing (same class of gap
    /// `rust-svc-process.yml`'s `coverage_ignore_regex: 'main\.rs'`
    /// documents for thin OS-boundary entrypoints elsewhere in this
    /// repo).
    #[tokio::test]
    async fn run_healthcheck_succeeds_against_a_responding_health_endpoint() {
        let _guard = ENV_LOCK.lock().await;

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let app = axum::Router::new().route("/health", axum::routing::get(|| async { "ok" }));
        let server = tokio::spawn(async move { axum::serve(listener, app).await });

        // SAFETY: serialized by ENV_LOCK.
        unsafe { std::env::set_var("MODULE_PORT", port.to_string()) };
        let result = run_healthcheck().await;
        unsafe { std::env::remove_var("MODULE_PORT") };

        assert!(result.is_ok());
        server.abort();
    }
}
