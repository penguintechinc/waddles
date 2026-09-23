//! `svc-action`: the Waddles ACTION stage-runner (pipeline terminal stage).
//!
//! This crate is split into a library (this file) and a thin binary
//! (`src/main.rs`) so integration tests under `tests/` can exercise the
//! router and config loader directly instead of spawning a subprocess.
//!
//! M3 ("Executor integration", spec §16 M3 row) landed the full stage side
//! of the bundle-executor wire protocol (`host_api`), hop verification
//! (`hop`), usage metering (`usage`, periodically flushed to
//! `waddles:usage` -- see [`try_start_dispatch`]), and retry/audit
//! (`retry`, `db::entities::action_dispatch_log`). What remains a
//! documented seam -- never a silent stub -- is named at each call site
//! below: the `GET /api/v1/distribution/bundles?stage=action` poll (which
//! bundle/digest/grants to run) is blocked on the same M2 hub-api work
//! `core/svc_process`'s own M4 skeleton left as `TODO(M4)`.
//!
//! **Correction (post-M3 review): Twitch sending is not yet functional
//! end to end.** `crate::capabilities::StageCapabilities` fully implements
//! the `relay` host capability's Valkey `LPUSH` (byte-exact port of
//! `waddle_transports.transports.irc_relay`), but [`try_start_host_api`]
//! below always installs `DenyAllCapabilities` as the live connection's
//! handler -- the same tenant/community-scoping gap the distribution poll
//! above is blocked on -- so a bundle's `relay` host call is denied in
//! every build shipped so far, not routed to a live Twitch send.
//! `crate::senders`'s `Platform`/`sender_status`/`is_retryable`/
//! `twitch_relay_args` are correspondingly unreferenced outside their own
//! module's tests today; they document the intended shape for the caller
//! that will issue/classify a relay send once `StageCapabilities` is
//! actually wired to a live connection, not code currently exercised by
//! `dispatch`.

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
        let usage = Arc::new(std::sync::Mutex::new(usage::UsageBatcher::new()));
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
            usage: Arc::clone(&usage),
            // spec §5.11/D30 (mirrors `penguin_spine::client::claim_stale`'s
            // own convention): a `DlqError.consumer_id` names the *pod*
            // handling the entry, not the entry's own stream id.
            consumer_id: spine_cfg.consumer_id.clone(),
            spine,
            metrics,
        };

        try_start_usage_flush(
            config.cli.metering_flush_interval_s,
            usage,
            spine_cfg.clone(),
        );

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

/// Starts the usage-metering flush loop (spec §5.12/D31) as its own
/// background task, independent of the dispatch drain loop above: `deps`
/// (moved into [`dispatch::run`]) and this task share the same
/// `Arc<Mutex<UsageBatcher>>`, so deltas `dispatch::handle_delivered` and
/// `crate::capabilities::StageCapabilities` accumulate here get drained on
/// a `METERING_FLUSH_INTERVAL_S` timer regardless of dispatch throughput.
/// A failed initial Valkey connection is logged and this task exits
/// without retrying -- usage metering is best-effort accounting (spec
/// §5.12: "no charging, quota or enforcement wired to it"), never a reason
/// to crash or block the dispatch loop it instruments; deltas simply keep
/// accumulating in memory (bounded by the number of distinct
/// `(tenant, community, workstream, app_id)` keys seen) until a future
/// successful flush drains them, or the pod restarts.
fn try_start_usage_flush(
    flush_interval_s: u64,
    usage: Arc<std::sync::Mutex<usage::UsageBatcher>>,
    spine_cfg: penguin_spine::SpineConfig,
) {
    let (shutdown_tx, mut shutdown_rx) = tokio::sync::oneshot::channel();
    tokio::spawn(async move {
        shutdown_signal().await;
        let _ = shutdown_tx.send(());
    });

    tokio::spawn(async move {
        let sink = match usage::connect_sink(&spine_cfg).await {
            Ok(sink) => sink,
            Err(err) => {
                tracing::warn!(
                    error = %err,
                    "usage-metering valkey connection failed; deltas will accumulate in memory but never flush"
                );
                return;
            }
        };
        let mut interval =
            tokio::time::interval(std::time::Duration::from_secs(flush_interval_s.max(1)));
        interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        loop {
            tokio::select! {
                _ = interval.tick() => {
                    // Swap the shared batcher for a fresh, empty one while
                    // holding the lock only for that synchronous swap, then
                    // flush the *owned* taken-out batcher after dropping
                    // the guard -- a `std::sync::MutexGuard` held across
                    // `.await` makes the enclosing future `!Send`, which
                    // `tokio::spawn` (this task's own caller) requires.
                    let mut batcher = std::mem::take(
                        &mut *usage.lock().unwrap_or_else(|e| e.into_inner()),
                    );
                    let flushed = batcher.flush(&sink).await;
                    if flushed > 0 {
                        tracing::debug!(flushed, "usage deltas flushed to waddles:usage");
                    }
                }
                _ = &mut shutdown_rx => {
                    let mut batcher = std::mem::take(
                        &mut *usage.lock().unwrap_or_else(|e| e.into_inner()),
                    );
                    let flushed = batcher.flush(&sink).await;
                    tracing::info!(flushed, "usage-metering flush loop shutting down, final flush complete");
                    return;
                }
            }
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

    /// A syntactically valid [`penguin_spine::SpineConfig`] that never
    /// actually connects -- identical fixture to `crate::usage::tests`'
    /// own `unreachable_spine_config` (same pinned `penguin-spine` rev,
    /// same rationale: port `1` refuses the TCP connection immediately).
    fn unreachable_spine_config() -> penguin_spine::SpineConfig {
        penguin_spine::SpineConfig {
            valkey_url: "redis://127.0.0.1:1/".to_string(),
            valkey_username: None,
            valkey_password: None,
            valkey_ca_file: std::path::PathBuf::from("/nonexistent-ca.crt"),
            security_transport_tls: false,
            security_transport_auth: false,
            consumer_id: "test-consumer".to_string(),
            stream_maxlen: 100,
            read_count: 1,
            block_ms: 1_000,
            claim_idle_ms: 30_000,
            claim_interval_ms: 15_000,
            stats_interval_ms: 10_000,
            pel_alert: 5_000,
            dlq_maxlen: 100,
            max_deliveries: 5,
            drain_socket_timeout_s: 65,
            relay_block_timeout_s: 30,
        }
    }

    /// Regression coverage for the CRITICAL usage-metering finding: proves
    /// [`try_start_usage_flush`] doesn't panic and returns control to its
    /// caller immediately (fire-and-forget `tokio::spawn`) when the initial
    /// Valkey connection fails -- the graceful-degradation path every other
    /// optional dependency in this module also takes (never a reason to
    /// crash or block the dispatch loop it instruments).
    #[tokio::test]
    async fn try_start_usage_flush_does_not_panic_when_valkey_is_unreachable() {
        let usage = Arc::new(std::sync::Mutex::new(usage::UsageBatcher::new()));
        try_start_usage_flush(1, usage, unreachable_spine_config());
        // Fire-and-forget: give the spawned task a moment to attempt (and
        // fail) its connection before the test process exits and drops it.
        tokio::time::sleep(std::time::Duration::from_millis(200)).await;
    }
}
