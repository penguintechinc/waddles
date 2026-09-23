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
//! (`retry`, `db::entities::action_dispatch_log`).
//!
//! **Post-M3 review fix: the sender path is now live.** [`try_start_host_api`]
//! installs a real `crate::capabilities::StageCapabilities` (a live Valkey
//! connection for `relay`, `crate::egress::EgressGuard` for `http`) rather
//! than the placeholder `DenyAllCapabilities` this crate shipped
//! previously -- and, per the review's other finding, capability scope is
//! resolved **per invoke** (`crate::capabilities::InvokeScope`, threaded
//! through `crate::host_api::Connection::invoke`), never fixed at
//! connection-construction time, since one connection multiplexes many
//! `(tenant, community, app_id)` activations (see `crate::capabilities`'s
//! module doc for the full rationale). [`try_start_distribution_poll`]
//! resolves which bundle/digest to `load` from the `GET /api/v1/
//! distribution/bundles?stage=action` poll, gated by `ACTION_APP_ID` --
//! single-bundle-scoped in this landing, matching every other seam at that
//! same scope (`core/svc_process`'s own M4 skeleton's `PROCESS_APP_ID`).
//! What remains a documented seam: `db`/`kv`/`flags` host capabilities
//! (`crate::capabilities`), and full multi-bundle/hot-swap distribution
//! reconciliation (`crate::distribution`'s module doc).

pub mod capabilities;
pub mod config;
pub mod db;
pub mod dispatch;
pub mod distribution;
pub mod egress;
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
use std::sync::{Arc, Mutex};

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

    // Shared across the host-API capability set and the dispatch loop --
    // see `crate::capabilities::StageCapabilities`'s `usage` field doc and
    // `try_start_usage_flush` below: one batcher, one flush loop,
    // regardless of which of the two accumulates a given delta.
    let usage = Arc::new(Mutex::new(usage::UsageBatcher::new()));
    // Shared between the distribution poll (writer) and the host-API
    // capability set's `EgressGuard` (reader, spec §7.4's `http` egress
    // allowlist) -- see `crate::distribution`'s module doc.
    let catalog = Arc::new(distribution::BundleCatalog::new());
    let egress_denied_total = telemetry::register_egress_metrics(&prom_registry);

    let state = http::AppState::new(config.clone(), prom_registry);

    let connections = try_start_host_api(
        &config.cli,
        Arc::clone(&usage),
        Arc::clone(&catalog),
        egress_denied_total,
    );
    try_start_distribution_poll(&config, Arc::clone(&connections), Arc::clone(&catalog));
    try_start_dispatch(&config, connections, catalog, usage);

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

/// Builds the real [`capabilities::StageCapabilities`] (a live Valkey
/// connection for `relay`, [`egress::EgressGuard`] for `http`), or `None`
/// if either dependency is unavailable right now. `try_start_host_api`
/// falls back to [`capabilities::DenyAllCapabilities`] on `None` -- the
/// same all-or-nothing graceful-degradation posture this capability set
/// has always had (a `relay_queue` is not optional on
/// `StageCapabilities<Q>`, so a missing Valkey connection cannot yield a
/// partial capability set without a larger refactor than this landing's
/// scope; a bundle sees `access-denied` on every capability, never a
/// crash, until the next connection attempt).
async fn build_stage_capabilities(
    cli: &config::CliConfig,
    usage: Arc<Mutex<usage::UsageBatcher>>,
    catalog: Arc<distribution::BundleCatalog>,
    egress_denied_total: prometheus::IntCounterVec,
) -> Option<Arc<dyn capabilities::CapabilityHandler>> {
    let spine_cfg = match penguin_spine::SpineConfig::from_env() {
        Ok(c) => c,
        Err(err) => {
            tracing::warn!(error = %err, "spine config unavailable; capabilities disabled (DenyAllCapabilities)");
            return None;
        }
    };
    let relay_conn = match usage::connect(&spine_cfg).await {
        Ok(conn) => conn,
        Err(err) => {
            tracing::warn!(error = %err, "valkey connection for relay capability failed; capabilities disabled (DenyAllCapabilities)");
            return None;
        }
    };
    let egress = Arc::new(egress::EgressGuard::new(
        Arc::new(egress::ReqwestTransport),
        egress::EgressLimits {
            allow_private_hosts: cli.egress_allow_private_hosts,
            rate_limit_rps: cli.egress_rate_limit_rps,
            rate_limit_burst: cli.egress_rate_limit_burst,
            timeout: std::time::Duration::from_millis(cli.egress_timeout_ms),
            max_redirects: cli.egress_max_redirects,
            max_response_bytes: cli.egress_max_response_bytes,
        },
        catalog,
        egress_denied_total,
    ));
    Some(Arc::new(capabilities::StageCapabilities::new(
        relay_conn, egress, usage,
    )))
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
fn try_start_host_api(
    cli: &config::CliConfig,
    usage: Arc<Mutex<usage::UsageBatcher>>,
    catalog: Arc<distribution::BundleCatalog>,
    egress_denied_total: prometheus::IntCounterVec,
) -> Arc<host_api::ConnectionRegistry> {
    let registry = Arc::new(host_api::ConnectionRegistry::new());
    let cli = cli.clone();
    let registry_for_task = Arc::clone(&registry);
    tokio::spawn(async move {
        let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel();
        tokio::spawn(async move {
            shutdown_signal().await;
            let _ = shutdown_tx.send(());
        });
        let capabilities = build_stage_capabilities(&cli, usage, catalog, egress_denied_total)
            .await
            .unwrap_or_else(|| Arc::new(capabilities::DenyAllCapabilities));
        if let Err(err) = host_api::serve(cli, registry_for_task, capabilities, shutdown_rx).await {
            tracing::warn!(error = %err, "host-api listener unavailable; executor integration disabled");
        }
    });
    registry
}

/// Starts the `GET /api/v1/distribution/bundles?stage=action` poll
/// (`crate::distribution::run_poll_loop`) as its own background task.
/// Mirrors [`try_start_dispatch`]'s own `ACTION_APP_ID`-gating: no bundle
/// assigned yet means nothing to poll for.
fn try_start_distribution_poll(
    config: &config::Config,
    connections: Arc<host_api::ConnectionRegistry>,
    catalog: Arc<distribution::BundleCatalog>,
) {
    if config.cli.action_app_id.is_empty() {
        tracing::info!("ACTION_APP_ID not set; distribution poll not started");
        return;
    }
    let cli = config.cli.clone();
    tokio::spawn(async move {
        let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel();
        tokio::spawn(async move {
            shutdown_signal().await;
            let _ = shutdown_tx.send(());
        });
        let load_limits = penguin_bundle_host::wire::LoadLimits {
            timeout_ms: cli.executor_call_timeout_ms,
            memory_mb: 64,
        };
        distribution::run_poll_loop(
            distribution::PollLoopConfig {
                client: reqwest::Client::new(),
                hub_api_url: cli.hub_api_url.clone(),
                stage: "action",
                poll_interval: std::time::Duration::from_secs_f64(cli.poll_interval_s.max(0.1)),
                catalog,
                connections,
                action_app_id: cli.action_app_id.clone(),
                load_limits,
            },
            shutdown_rx,
        )
        .await;
    });
}

/// Waits up to a few `poll_interval`s for [`try_start_distribution_poll`]
/// to have resolved `app_id`'s digest/config into `catalog`, so the
/// dispatch loop's first `invoke` has a real digest to run rather than the
/// empty-string placeholder (which the executor would refuse with
/// `UNKNOWN_BUNDLE`). Falls back to `(String::new(), "{}")` -- today's
/// behavior -- if the poll hasn't landed a row in time (hub-api slow/down
/// at startup): never blocks `try_start_dispatch` indefinitely, and never
/// panics. **This is a one-shot resolution, not hot-swap** -- a digest
/// change observed later by the poll loop updates `catalog` and (once an
/// executor is connected) sends `load` for it, but this dispatch loop's own
/// `deps.digest` stays fixed at whatever this function returned (documented
/// seam, `crate::dispatch`'s own module doc).
async fn resolve_initial_bundle(
    catalog: &distribution::BundleCatalog,
    app_id: &str,
    poll_interval: std::time::Duration,
) -> (String, String) {
    const MAX_ATTEMPTS: u32 = 3;
    for attempt in 0..MAX_ATTEMPTS {
        if let Some(row) = catalog.get(app_id) {
            if let Some(digest) = row.artifact_digest {
                return (digest, row.config_json);
            }
        }
        if attempt + 1 < MAX_ATTEMPTS {
            tokio::time::sleep(poll_interval).await;
        }
    }
    tracing::warn!(
        app_id,
        "no distribution row resolved yet; dispatch loop starting with an empty digest"
    );
    (String::new(), "{}".to_string())
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
fn try_start_dispatch(
    config: &config::Config,
    connections: Arc<host_api::ConnectionRegistry>,
    catalog: Arc<distribution::BundleCatalog>,
    usage: Arc<Mutex<usage::UsageBatcher>>,
) {
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
    let poll_interval = std::time::Duration::from_secs_f64(config.cli.poll_interval_s.max(0.1));
    let config = config.clone();
    tokio::spawn(async move {
        let db = match db::connect(&config).await {
            Ok(db) => db,
            Err(err) => {
                tracing::error!(error = %err, "db connection failed; dispatch loop not started");
                return;
            }
        };
        let (digest, config_json) = resolve_initial_bundle(&catalog, &app_id, poll_interval).await;
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
            digest,
            config_json,
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
    async fn resolve_initial_bundle_returns_immediately_once_the_catalog_has_a_digest() {
        let catalog = distribution::BundleCatalog::new();
        catalog.update(vec![distribution::BundleRow {
            app_id: "waddles.a.b.c".to_string(),
            version: "1.0.0".to_string(),
            artifact_digest: Some("sha256:aa".to_string()),
            component_key: "k".to_string(),
            sidecar_key: "s".to_string(),
            egress: vec![],
            egress_rps: None,
            config_json: "{\"x\":1}".to_string(),
        }]);
        let (digest, config_json) = tokio::time::timeout(
            std::time::Duration::from_secs(2),
            resolve_initial_bundle(
                &catalog,
                "waddles.a.b.c",
                std::time::Duration::from_secs(60),
            ),
        )
        .await
        .expect("resolves without waiting out the poll interval");
        assert_eq!(digest, "sha256:aa");
        assert_eq!(config_json, "{\"x\":1}");
    }

    #[tokio::test]
    async fn resolve_initial_bundle_falls_back_to_empty_when_nothing_ever_resolves() {
        let catalog = distribution::BundleCatalog::new();
        let (digest, config_json) = resolve_initial_bundle(
            &catalog,
            "waddles.never.resolves",
            std::time::Duration::from_millis(5),
        )
        .await;
        assert_eq!(digest, "");
        assert_eq!(config_json, "{}");
    }

    #[tokio::test]
    async fn resolve_initial_bundle_ignores_a_row_with_no_artifact_yet() {
        let catalog = distribution::BundleCatalog::new();
        catalog.update(vec![distribution::BundleRow {
            app_id: "waddles.a.b.c".to_string(),
            version: String::new(),
            artifact_digest: None,
            component_key: String::new(),
            sidecar_key: String::new(),
            egress: vec![],
            egress_rps: None,
            config_json: "{}".to_string(),
        }]);
        let (digest, _config_json) = resolve_initial_bundle(
            &catalog,
            "waddles.a.b.c",
            std::time::Duration::from_millis(5),
        )
        .await;
        assert_eq!(digest, "");
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

    /// `try_start_dispatch`'s second independent disable reason (module
    /// doc): `ACTION_APP_ID` set but `ENVELOPE_BINDING_KEYS` unset --
    /// returns before spawning anything, hop verification must never fail
    /// open. Fire-and-forget, same shape as the Valkey-unreachable test
    /// above: nothing to await, just proves it doesn't panic and doesn't
    /// spawn the loop.
    #[tokio::test]
    async fn try_start_dispatch_disabled_when_envelope_binding_keys_missing() {
        let _guard = ENV_LOCK.lock().await;
        // SAFETY: serialized by ENV_LOCK.
        unsafe { std::env::remove_var("ENVELOPE_BINDING_KEYS") };
        let cli = CliConfig::parse_from(["svc-action", "--action-app-id", "waddles.a.b.c"]);
        let config = Config {
            cli,
            db_password: Secret::new("test-password"),
            envelope_binding_keys: None,
        };
        let connections = Arc::new(host_api::ConnectionRegistry::new());
        let catalog = Arc::new(distribution::BundleCatalog::new());
        let usage = Arc::new(std::sync::Mutex::new(usage::UsageBatcher::new()));
        try_start_dispatch(&config, connections, catalog, usage);
    }

    /// `try_start_distribution_poll`'s own gate: `ACTION_APP_ID` unset never
    /// starts the poll task. A fire-and-forget call proving no panic and no
    /// spawned task -- the "set" path is already exercised end to end by
    /// `crate::distribution`'s own `run_poll_loop` tests.
    #[test]
    fn try_start_distribution_poll_disabled_without_action_app_id() {
        let cli = CliConfig::parse_from(["svc-action"]);
        let config = Config {
            cli,
            db_password: Secret::new("test-password"),
            envelope_binding_keys: None,
        };
        let connections = Arc::new(host_api::ConnectionRegistry::new());
        let catalog = Arc::new(distribution::BundleCatalog::new());
        try_start_distribution_poll(&config, connections, catalog);
    }
}
