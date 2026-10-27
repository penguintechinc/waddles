//! `svc-presentation`: the Waddles overlay/presentation service (P1
//! scaffold).
//!
//! This crate is split into a library (this file) and a thin binary
//! (`src/main.rs`) so integration tests under `tests/` can exercise the
//! router, config loader, and overlay-auth wiring directly instead of
//! spawning a subprocess -- same pattern as `core/svc_streaming`.
//!
//! P1 scope: config, telemetry, DB connection, SeaORM entities for this
//! service's tables, and the `overlay_auth` VIEW/PUSH axum guards. P2-P4 and
//! the live pipeline sit on top: a push is rendered ([`overlay::render`]),
//! detokenized and HTML-escaped ([`overlay::detok`]) *before* it is fanned out
//! ([`overlay::hub`]) to the SSE/websocket channels and the browser page
//! ([`http::overlay_page`]) -- see `README.md`'s "Live Overlay Pipeline".

pub mod config;
pub mod db;
pub mod error;
pub mod flags;
pub mod http;
pub mod images;
pub mod overlay;
pub mod telemetry;

use std::net::SocketAddr;
use std::sync::Arc;

use anyhow::Context as _;
use tokio::signal;

/// Default `tracing`/OTel service name, also the fallback `--healthcheck`
/// target and the resource `service.name` when `OTEL_SERVICE_NAME` is
/// unset.
pub const SERVICE_NAME: &str = "svc-presentation";

/// hub-api internal gRPC scope this service requests when bootstrapping its
/// machine JWT (`hub_api/grpc_internal/servicers.py::REQUIRED_SCOPES`) --
/// `ResolveDisplayNames` only; the overlay never mints pseudonyms, resolves
/// handles or fetches stream keys.
const HUB_IDENTITY_RESOLVE_SCOPE: &str = "identity:displayname:read";

/// Builds and connects the `hub_client::HubClient` overlay detokenization
/// resolves display names through, or `Ok(None)` when the operator
/// explicitly switched detokenization off
/// (`PII_DETOKENIZATION_ENABLED=false`).
///
/// **Fail loud, never a silent degraded overlay.** With detokenization
/// enabled (the default), an unset `HUB_API_GRPC_ENDPOINT`/
/// `SERVICE_JWT_TOKEN_ENDPOINT`, or a failed initial connect, returns `Err`
/// so the process exits non-zero instead of starting and showing
/// [`egress_detokenizer::NEUTRAL_LABEL`] for every user. This is
/// startup-only: a transient gRPC failure after a successful connect still
/// degrades per push to the neutral label (`overlay::detok`'s fail-safe-empty
/// contract; `hub_client`'s breaker/retries bound how long), never a process
/// exit.
async fn build_hub_client(
    cli: &config::CliConfig,
) -> anyhow::Result<Option<Arc<hub_client::HubClient>>> {
    if cli.pii_detokenization_enabled_override == Some(false) {
        tracing::warn!(
            "PII_DETOKENIZATION_ENABLED=false: overlay detokenization is DISABLED by the \
             operator; hub_client is not connected, no display name is resolved, and every \
             user reference renders as the neutral label (output is still HTML-escaped and \
             leak-free)"
        );
        return Ok(None);
    }
    if cli.hub_api_grpc_endpoint.is_empty() || cli.service_jwt_token_endpoint.is_empty() {
        anyhow::bail!(
            "overlay detokenization is enabled (the default) but HUB_API_GRPC_ENDPOINT/\
             SERVICE_JWT_TOKEN_ENDPOINT is unset; refusing to start and silently show {:?} \
             for every user -- set both env vars, or set PII_DETOKENIZATION_ENABLED=false \
             for a deployment without a working hub-api connection yet",
            egress_detokenizer::NEUTRAL_LABEL
        );
    }
    match hub_client::HubClient::connect(
        cli.hub_api_grpc_endpoint.clone(),
        cli.service_jwt_token_endpoint.clone(),
        cli.service_jwt_sa_token_path.clone(),
        HUB_IDENTITY_RESOLVE_SCOPE,
        // Empty means "system/webpki roots", which this chart's internal CA is
        // never part of; HUB_API_GRPC_CA_FILE is the real path when deployed.
        (!cli.hub_api_grpc_ca_file.is_empty()).then_some(cli.hub_api_grpc_ca_file.as_str()),
    )
    .await
    {
        Ok(client) => {
            tracing::info!(
                endpoint = %cli.hub_api_grpc_endpoint,
                "hub_client connected; overlay detokenization is live"
            );
            Ok(Some(Arc::new(client)))
        }
        Err(err) => Err(anyhow::anyhow!(
            "overlay detokenization is enabled but connecting to hub-api's internal gRPC \
             endpoint {:?} failed: {err}; refusing to start and silently show the neutral \
             label for every user -- fix the endpoint/credentials, or set \
             PII_DETOKENIZATION_ENABLED=false",
            cli.hub_api_grpc_endpoint
        )),
    }
}

/// Applies the startup detokenization decision to `state`: a connected
/// hub client, or the explicit operator-disabled mode.
async fn with_detokenization(
    state: http::AppState,
    cli: &config::CliConfig,
) -> anyhow::Result<http::AppState> {
    Ok(match build_hub_client(cli).await? {
        Some(client) => state.with_hub_client(client),
        None => state.with_detokenization_disabled(),
    })
}

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
    // Fails the process (non-zero exit) when detokenization is on but hub-api
    // is not configured/reachable -- see `build_hub_client`.
    let state = with_detokenization(state, &config.cli).await?;

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

    fn cli(args: &[&str]) -> config::CliConfig {
        use clap::Parser;
        let mut argv = vec!["svc-presentation"];
        argv.extend_from_slice(args);
        config::CliConfig::parse_from(argv)
    }

    fn test_state() -> http::AppState {
        use sea_orm::{DatabaseBackend, MockDatabase};
        let config = config::Config {
            cli: cli(&[]),
            db_password: config::Secret::new("x"),
            cache_password: None,
            image_bucket_access_key_id: None,
            image_bucket_secret_access_key: None,
        };
        let db = MockDatabase::new(DatabaseBackend::Postgres).into_connection();
        http::AppState::new(config, prometheus::Registry::new(), db)
    }

    /// Fail-loud regression: detokenization on (the default) with no hub-api
    /// endpoint configured must be a startup `Err`, never a service that
    /// silently renders "Unknown User" for everyone.
    #[tokio::test]
    async fn build_hub_client_fails_loud_when_enabled_and_endpoint_unset() {
        let err = match build_hub_client(&cli(&[])).await {
            Ok(_) => panic!("enabled detokenization with no endpoint must fail loud"),
            Err(err) => err,
        };
        assert!(err.to_string().contains("HUB_API_GRPC_ENDPOINT"), "{err}");
        // Either one missing is enough.
        let err = match build_hub_client(&cli(&["--hub-api-grpc-endpoint", "http://x:1"])).await {
            Ok(_) => panic!("a missing token endpoint must fail loud"),
            Err(err) => err,
        };
        assert!(
            err.to_string().contains("SERVICE_JWT_TOKEN_ENDPOINT"),
            "{err}"
        );
    }

    #[tokio::test]
    async fn build_hub_client_fails_loud_when_the_endpoint_is_unreachable() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        drop(listener); // nothing listening: a prompt connection-refused
        let err = match build_hub_client(&cli(&[
            "--hub-api-grpc-endpoint",
            &format!("http://{addr}"),
            "--service-jwt-token-endpoint",
            &format!("http://{addr}/internal/service-token"),
        ]))
        .await
        {
            Ok(_) => panic!("an unreachable hub-api must fail loud at startup"),
            Err(err) => err,
        };
        assert!(err.to_string().contains("hub-api"), "{err}");
    }

    #[tokio::test]
    async fn build_hub_client_is_none_only_for_the_explicit_operator_override() {
        let client = build_hub_client(&cli(&["--pii-detokenization-enabled-override", "false"]))
            .await
            .expect("the explicit override never fails");
        assert!(client.is_none());
        // `true` is NOT an override: it keeps the fail-loud default.
        assert!(
            build_hub_client(&cli(&["--pii-detokenization-enabled-override", "true"]))
                .await
                .is_err()
        );
    }

    #[tokio::test]
    async fn with_detokenization_connects_a_client_or_disables_explicitly() {
        // Disabled by the operator.
        let state = with_detokenization(
            test_state(),
            &cli(&["--pii-detokenization-enabled-override", "false"]),
        )
        .await
        .unwrap();
        assert!(state.detokenizer.is_disabled());

        // Connected (a listening peer is all `connect` needs).
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            while let Ok((sock, _)) = listener.accept().await {
                std::mem::forget(sock);
            }
        });
        let state = with_detokenization(
            test_state(),
            &cli(&[
                "--hub-api-grpc-endpoint",
                &format!("http://{addr}"),
                "--service-jwt-token-endpoint",
                &format!("http://{addr}/internal/service-token"),
            ]),
        )
        .await
        .unwrap();
        assert!(!state.detokenizer.is_disabled());

        // A bare AppState holds the loud "unconfigured" detokenizer.
        assert!(!test_state().detokenizer.is_disabled());

        // Unset => the whole startup step errors.
        assert!(with_detokenization(test_state(), &cli(&[])).await.is_err());
    }

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
