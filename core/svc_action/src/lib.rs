//! `svc-action`: the Waddles ACTION stage-runner (pipeline terminal stage).
//!
//! This crate is split into a library (this file) and a thin binary
//! (`src/main.rs`) so integration tests under `tests/` can exercise the
//! router and config loader directly instead of spawning a subprocess.
//!
//! M3 ("Executor integration", spec §16 M3 row) landed the full stage side
//! of the bundle-executor wire protocol (`host_api`), hop verification
//! (`hop`), usage metering (`usage`), retry/audit (`retry`,
//! `db::entities::action_dispatch_log`), and one built-in sender end to
//! end (`senders`::Twitch via `capabilities`::relay). What remains a
//! documented seam -- never a silent stub -- is named at each call site
//! below: the `GET /api/v1/distribution/bundles?stage=action` poll (which
//! bundle/digest/grants to run) is blocked on the same M2 hub-api work
//! `core/svc_process`'s own M4 skeleton left as `TODO(M4)`.

pub mod capabilities;
pub mod config;
pub mod db;
pub mod dispatch;
pub mod error;
pub mod hop;
pub mod host_api;
pub mod http;
pub mod retry;
pub mod senders;
pub mod telemetry;
pub mod usage;
pub mod wiring;

use std::net::SocketAddr;
use std::sync::Arc;

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

    let connections = try_start_host_api(&config.cli);
    try_start_dispatch(&config, connections);

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

/// Starts the host-API mTLS listener as its own background task and
/// returns the [`host_api::ConnectionRegistry`] immediately, regardless of
/// whether the listener actually starts -- never blocks or fails
/// `run_with_shutdown`'s caller. Missing/invalid TLS config (no
/// `HOST_API_SERVER_CERT_FILE`/`_KEY_FILE`/`HOST_API_CLIENT_CA_FILE`) is
/// logged and disables executor integration rather than crashing the
/// HTTP/metrics servers served alongside it -- the same graceful-
/// degradation contract `core/svc_process`'s `try_start_spine_drain`
/// applies to its own optional dependency.
fn try_start_host_api(cli: &config::CliConfig) -> Arc<host_api::ConnectionRegistry> {
    let registry = Arc::new(host_api::ConnectionRegistry::new());
    let cli = cli.clone();
    let registry_for_task = Arc::clone(&registry);
    tokio::spawn(async move {
        let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel();
        tokio::spawn(async move {
            shutdown_signal().await;
            let _ = shutdown_tx.send(());
        });
        // TODO(M3+): the `relay`/`clock`/`context`/`log` capabilities need
        // a live Valkey connection (`crate::capabilities::StageCapabilities`)
        // and per-activation (tenant, community, app_id) scope neither of
        // which the host-api listener alone has -- until the distribution
        // poll (see the module doc) resolves that scope, every host-call
        // this listener receives is answered by `DenyAllCapabilities`
        // (a real, correct "not configured yet" denial, never a fabricated
        // success).
        let capabilities: Arc<dyn capabilities::CapabilityHandler> =
            Arc::new(capabilities::DenyAllCapabilities);
        if let Err(err) = host_api::serve(cli, registry_for_task, capabilities, shutdown_rx).await {
            tracing::warn!(error = %err, "host-api listener unavailable; executor integration disabled");
        }
    });
    registry
}

/// Starts the action-stage dispatch loop (`crate::dispatch::run`) as its
/// own background task, mirroring `core/svc_process`'s
/// `try_start_spine_drain` exactly: two independent reasons this never
/// starts, both logged and neither an error -- `ACTION_APP_ID` unset (no
/// bundle assigned yet, multi-bundle scheduling is blocked on the
/// distribution poll), or `penguin_spine::SpineConfig::from_env()`/
/// `ENVELOPE_BINDING_KEYS` parsing failing (missing/invalid required
/// config -- hop verification must never silently fail open, so a missing
/// keyring disables the loop rather than starting it unverified).
fn try_start_dispatch(config: &config::Config, connections: Arc<host_api::ConnectionRegistry>) {
    if config.cli.action_app_id.is_empty() {
        tracing::info!(
            "ACTION_APP_ID not set; dispatch loop not started (blocked on distribution poll)"
        );
        return;
    }

    let Some(keys_raw) = config.envelope_binding_keys.as_ref() else {
        tracing::warn!("ENVELOPE_BINDING_KEYS not set; dispatch loop disabled (hop verification must never fail open)");
        return;
    };
    let key_ring = match hop::KeyRing::parse(keys_raw.expose()) {
        Ok(r) => r,
        Err(err) => {
            tracing::warn!(error = %err, "ENVELOPE_BINDING_KEYS invalid; dispatch loop disabled");
            return;
        }
    };

    let spine_cfg = match penguin_spine::SpineConfig::from_env() {
        Ok(c) => c,
        Err(err) => {
            tracing::warn!(error = %err, "spine config unavailable; action-stage dispatch disabled");
            return;
        }
    };

    let app_id = config.cli.action_app_id.clone();
    let config = config.clone();
    tokio::spawn(async move {
        let db = match db::connect(&config).await {
            Ok(db) => db,
            Err(err) => {
                tracing::error!(error = %err, "db connection failed; dispatch loop not started");
                return;
            }
        };
        // TODO(M3+): tenant/community scope is hardcoded to the
        // tenant-wide `global` activation until the distribution poll
        // (module doc) resolves the real set of (tenant, community,
        // app_id) activations this pod should drain -- a single-tenant
        // deployment (today's only shipped topology) is unaffected.
        let scope = penguin_spine::Scope::new("global", None);
        let stream_key = scope.action_stream(&app_id);
        let grant = penguin_spine::Grant {
            stream: stream_key.clone(),
            platform: "internal".to_string(),
            source_id: app_id.clone(),
        };
        let metrics: Arc<dyn penguin_spine::SpineMetrics> = Arc::new(penguin_spine::NoopMetrics);
        let spine = match penguin_spine::SpineClient::connect(spine_cfg.clone(), metrics.clone())
            .await
        {
            Ok(c) => c,
            Err(err) => {
                tracing::error!(error = %err, "spine client connect failed; dispatch loop not started");
                return;
            }
        };
        let deps = dispatch::DispatchDeps {
            app_id: app_id.clone(),
            digest: String::new(),
            config_json: "{}".to_string(),
            key_ring,
            connections,
            retry_policy: dispatch::RetryPolicy {
                max_retries: config.cli.action_max_retries,
                base_backoff_ms: config.cli.action_base_backoff_ms,
                max_backoff_ms: config.cli.action_max_backoff_ms,
                call_timeout_ms: config.cli.executor_call_timeout_ms,
            },
            jitter: retry::Jitter::from_entropy(),
            audit: wiring::DbAuditSink::new(db.clone()),
            tenants: wiring::DbTenantResolver::new(db),
            usage: Arc::new(std::sync::Mutex::new(usage::UsageBatcher::new())),
            spine,
            metrics,
        };

        let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel();
        tokio::spawn(async move {
            shutdown_signal().await;
            let _ = shutdown_tx.send(());
        });

        if let Err(err) = dispatch::run(spine_cfg, vec![grant], stream_key, deps, shutdown_rx).await
        {
            tracing::error!(error = %err, "action-stage dispatch loop exited");
        }
    });
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
            envelope_binding_keys: None,
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
