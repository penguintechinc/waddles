//! `svc-ingest`: the Waddles chat/event data-plane ingest service (Rust
//! rewrite, M5 skeleton).
//!
//! This crate is split into a library (this file) and a thin binary
//! (`src/main.rs`) so integration tests under `tests/` can exercise the
//! router and config loader directly instead of spawning a subprocess --
//! same split as `core/svc_streaming`.
//!
//! **Scope of this skeleton.** Per the M5 milestone row in
//! `docs/superpowers/specs/2026-09-14-rust-data-plane-design.md` S16, this
//! chunk builds only the `svc_streaming`-template subset: `/health`,
//! `/healthz`, `/metrics`, OTel wiring, config, the Dockerfile, and CI.
//! The fixed platform normalizers, the five `penguin-connectors` crates,
//! the outbound relay drain, the generic/JWT intake surfaces, and
//! workstream minting are **out of scope** here -- they depend on the M1
//! `penguin-connectors` crates and M2 (compiler/SDKs/hub-api hooks), both
//! building in parallel. Every seam where that work lands is marked:
//!
//! ```text
//! // TODO(M5): connectors/intake -- blocked on M1 connectors + M2
//! ```
//!
//! See `src/http/mod.rs::router` for the one seam this skeleton has today.
//! Per spec S4.1, this service has **no database** -- no `sea-orm`
//! dependency, unlike `svc_process`/`svc_action`.

pub mod config;
pub mod error;
pub mod http;
pub mod telemetry;

use std::net::SocketAddr;

use tokio::signal;

/// Default `tracing`/OTel service name, also the fallback `--healthcheck`
/// target and the resource `service.name` when `OTEL_SERVICE_NAME` is
/// unset.
pub const SERVICE_NAME: &str = "svc-ingest";

/// Runs the service: loads config, bootstraps telemetry, builds the
/// control-plane + metrics routers, and serves both until SIGINT/SIGTERM
/// is received.
pub async fn run() -> anyhow::Result<()> {
    let config = config::Config::load()?;
    run_with_shutdown(config, shutdown_signal(), shutdown_signal()).await
}

/// Same as [`run`], but takes an already-loaded [`config::Config`] and
/// caller-supplied shutdown futures for each listener instead of installing
/// OS signal handlers -- this is what makes the bind/serve/telemetry wiring
/// testable (see `tests/run.rs`): a test can pass `config` built via
/// [`config::CliConfig::parse_from`] (an explicit free port) and an
/// already-resolved future so the server binds, logs, and shuts down
/// immediately instead of blocking forever on a real signal.
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

    // TODO(M5): connectors/intake -- blocked on M1 connectors + M2. This is
    // where the platform receivers (Twitch IRC/EventSub, Discord gateway,
    // Slack Socket Mode, YouTube poll, Kick Pusher), the outbound relay
    // drain, and the generic/JWT intake routes attach once
    // `penguin-connectors` and `penguin-spine` land -- see spec S4.1, S10.
    // This skeleton spawns no background tasks: it serves health and
    // metrics only, which is the honest state of an unimplemented
    // milestone rather than a faked one.

    let state = http::AppState::new(config.clone(), prom_registry);

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

/// Reads `MODULE_PORT` from the environment, defaulting to this service's
/// standard port -- shared by [`run`] (via [`config::Config::load`]'s
/// `clap` parsing) and [`run_healthcheck`], which deliberately avoids
/// pulling in the full [`config::Config`] (and its `clap::Parser::parse()`
/// call against real process argv) just to read one port.
fn healthcheck_port() -> u16 {
    std::env::var("MODULE_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(8200)
}

/// Builds the loopback `/healthz` URL [`run_healthcheck`] probes.
fn healthcheck_url(port: u16) -> String {
    format!("http://127.0.0.1:{port}/healthz")
}

/// Probes `url` once and classifies the outcome. Split out from
/// [`run_healthcheck`] so the classification logic (success / bad status /
/// transport error) is unit-testable without the `std::process::exit(1)`
/// side effect a failing container healthcheck must have -- that side
/// effect isn't itself testable (it would tear down the test process), so
/// it stays in the thin wrapper below, not here.
async fn healthcheck_probe(client: &reqwest::Client, url: &str) -> Result<(), String> {
    match client.get(url).send().await {
        Ok(resp) if resp.status().is_success() => Ok(()),
        Ok(resp) => Err(format!("{url} returned {}", resp.status())),
        Err(err) => Err(err.to_string()),
    }
}

/// `svc-ingest --healthcheck`: GETs `/healthz` on the locally-bound HTTP
/// port and exits 0/1 accordingly. Reads `MODULE_PORT` the same way
/// [`run`] does. The container `HEALTHCHECK` invokes this directly instead
/// of relying on `curl` being present in the runtime image.
pub async fn run_healthcheck() -> anyhow::Result<()> {
    let url = healthcheck_url(healthcheck_port());
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(3))
        .build()?;

    match healthcheck_probe(&client, &url).await {
        Ok(()) => Ok(()),
        Err(reason) => {
            eprintln!("healthcheck failed: {reason}");
            std::process::exit(1);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::routing::get;
    use axum::Router;
    use std::sync::Mutex;

    // std::env is process-global; serialize env-mutating tests so parallel
    // `cargo test` threads don't race on the same variable.
    static ENV_LOCK: Mutex<()> = Mutex::new(());

    #[test]
    fn healthcheck_port_defaults_when_unset() {
        let _guard = ENV_LOCK.lock().unwrap();
        // SAFETY: serialized by ENV_LOCK.
        unsafe { std::env::remove_var("MODULE_PORT") };
        assert_eq!(healthcheck_port(), 8200);
    }

    #[test]
    fn healthcheck_port_reads_env_override() {
        let _guard = ENV_LOCK.lock().unwrap();
        // SAFETY: serialized by ENV_LOCK.
        unsafe { std::env::set_var("MODULE_PORT", "9999") };
        assert_eq!(healthcheck_port(), 9999);
        unsafe { std::env::remove_var("MODULE_PORT") };
    }

    #[test]
    fn healthcheck_url_formats_loopback_address() {
        assert_eq!(healthcheck_url(8200), "http://127.0.0.1:8200/healthz");
    }

    async fn spawn_fixed_status_server(status: axum::http::StatusCode) -> std::net::SocketAddr {
        let router = Router::new().route("/healthz", get(move || async move { status }));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            axum::serve(listener, router).await.ok();
        });
        addr
    }

    #[tokio::test]
    async fn healthcheck_probe_succeeds_on_200() {
        let addr = spawn_fixed_status_server(axum::http::StatusCode::OK).await;
        let client = reqwest::Client::new();
        let url = healthcheck_url(addr.port());
        assert!(healthcheck_probe(&client, &url).await.is_ok());
    }

    #[tokio::test]
    async fn healthcheck_probe_fails_on_non_2xx_status() {
        let addr = spawn_fixed_status_server(axum::http::StatusCode::SERVICE_UNAVAILABLE).await;
        let client = reqwest::Client::new();
        let url = healthcheck_url(addr.port());
        let err = healthcheck_probe(&client, &url).await.unwrap_err();
        assert!(err.contains("503"));
    }

    #[tokio::test]
    async fn healthcheck_probe_fails_on_connection_error() {
        // Port 0 is never a live listener to connect to.
        let client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_millis(200))
            .build()
            .unwrap();
        let url = "http://127.0.0.1:1/healthz";
        assert!(healthcheck_probe(&client, url).await.is_err());
    }
}
