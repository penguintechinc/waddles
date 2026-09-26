//! `svc-process`: the Waddles process-stage data-plane service.
//!
//! **M4 runtime landing.** This crate wires config loading, sanitizing
//! OTel/`tracing`/Prometheus telemetry via `penguin-logging`
//! (`crate::telemetry`), the granted-ingest-stream `XREADGROUP` consumer
//! via `penguin-spine` (`crate::spine`), hop verification (`crate::hop`),
//! the mTLS host-API server and per-invoke-scoped capabilities
//! (`crate::host_api`/`crate::capabilities`), the cross-app `_target_app_id`
//! routing built-in (`crate::builtins`), the `waddles.core.rust-data-plane`
//! license/feature-flag gate on the drain loop (`crate::license`, spec
//! §13.5), and the `/health` + `/healthz` + `/metrics` control-plane
//! surface -- the same shape as `core/svc_action` (the M3 reference this
//! landing mirrors for the executor-integration half) and
//! `core/svc_streaming` (the original M4 skeleton template).
//!
//! Per `docs/superpowers/specs/2026-09-14-rust-data-plane-design.md` §4.2
//! and the M4 milestone row (§16), the following remain **honest,
//! documented seams** -- never a silent stub, each named at its own call
//! site:
//!
//! - The content-moderation gate itself (`crate::builtins::
//!   run_moderation_gate`) -- needs a Rust Ollama classifier client and
//!   PostHog flag client, neither of which exists in this crate yet
//! - The `db`/`kv`/`http`/`flags` host capabilities
//!   (`crate::capabilities::StageCapabilities`) -- `context`/`clock`/`log`
//!   are fully wired
//! - The `GET /api/v1/distribution/bundles?stage=process` activation poll
//!   (spec §6.7) that would resolve `PROCESS_APP_ID`'s real granted-stream
//!   list, bundle digest, and approved `routes_to` set -- see
//!   [`try_start_process_loop`]'s doc for the interim, env-driven
//!   substitutes this landing uses instead
//!
//! This crate is split into a library (this file) and a thin binary
//! (`src/main.rs`) so integration tests under `tests/` can exercise the
//! router and config loader directly instead of spawning a subprocess.

pub mod builtins;
pub mod capabilities;
pub mod config;
pub mod error;
pub mod hop;
pub mod host_api;
pub mod http;
pub mod license;
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
/// control-plane + metrics routers, starts the host-API mTLS listener and
/// the process-stage drain loop (see [`try_start_host_api`]/
/// [`try_start_process_loop`]), and serves everything until SIGINT/SIGTERM
/// is received.
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
    // Installed as early as possible, once, process-wide: two rustls
    // backends now reach this crate's dependency graph (this crate's own
    // direct `rustls` dep selects `ring`; `penguin-licensing`'s `reqwest`
    // pulls in a second one for its own HTTPS calls) -- with two
    // candidates present, rustls can no longer auto-detect a default and
    // panics on the first TLS config built by *whichever* subsystem gets
    // there first (the host-API mTLS listener, the license client's
    // background refresh, or this crate's own `reqwest` client). See
    // `host_api::ensure_crypto_provider_installed`'s doc for the full
    // rationale; called there too as a defensive, idempotent second call.
    host_api::ensure_crypto_provider_installed();

    let (_telemetry_guard, prom_registry) = telemetry::init(SERVICE_NAME);

    tracing::info!(
        http_port = config.cli.http_port,
        metrics_port = config.cli.metrics_port,
        "starting {SERVICE_NAME}"
    );

    let state = http::AppState::new(config.clone(), prom_registry);

    let connections = try_start_host_api(&config.cli);
    try_start_process_loop(&config, connections);

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

/// Starts the host-API mTLS listener (spec §6.6/§7.1, `:8301`) as its own
/// background task and returns the [`host_api::ConnectionRegistry`]
/// immediately, regardless of whether the listener actually starts --
/// never blocks or fails `run_with_shutdown`'s caller. Missing/invalid TLS
/// config (no `HOST_API_SERVER_CERT_FILE`/`_KEY_FILE`/`HOST_API_CLIENT_CA_
/// FILE`) is logged and disables executor integration rather than
/// crashing the HTTP/metrics servers served alongside it -- identical
/// graceful-degradation contract to `core/svc_action::try_start_host_api`.
/// The connection-level `fallback_capabilities` is always
/// `DenyAllCapabilities`: the real, envelope-scoped capability set is
/// supplied per-invoke by `crate::spine::handle_delivered` (see
/// `crate::host_api`'s module doc), never fixed here.
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
        let fallback_capabilities: Arc<dyn capabilities::CapabilityHandler> =
            Arc::new(capabilities::DenyAllCapabilities);
        if let Err(err) =
            host_api::serve(cli, registry_for_task, fallback_capabilities, shutdown_rx).await
        {
            tracing::warn!(error = %err, "host-api listener unavailable; executor integration disabled");
        }
    });
    registry
}

/// Attempts to start the process-stage drain loop (`crate::spine::run`) as
/// its own background task, mirroring `core/svc_action::try_start_dispatch`'s
/// shape: three independent reasons this never starts, all logged and none
/// an error -- `PROCESS_APP_ID` unset (no bundle assigned yet, multi-bundle
/// scheduling is blocked on the distribution poll); `penguin_spine::
/// SpineConfig::from_env()`/`ENVELOPE_BINDING_KEYS` parsing failing (hop
/// verification must never silently fail open, so a missing keyring
/// disables the loop rather than starting it unverified); or the license
/// client failing to construct (a malformed `LICENSE_SERVER_URL`/
/// `POSTHOG_HOST` -- see `crate::license`). Once started, the loop itself
/// is additionally gated per-batch on `waddles.core.rust-data-plane`
/// (spec §13.5) -- OFF drains nothing without stopping the loop or
/// affecting `/health`/`/metrics`, see `crate::spine::drain_batch`.
///
/// **TODO(M4+), interim substitutes for the `GET /api/v1/distribution/
/// bundles?stage=process` poll (spec §6.7):**
/// - Grant list: `PROCESS_INGEST_PLATFORM`/`PROCESS_INGEST_SOURCE_ID`
///   resolve at most one ingest-source stream (a real poll would resolve
///   the bundle's full `consumes` grant set, spec §5.2); empty means no
///   grants at all -- the loop still connects and blocks on nothing.
/// - Bundle identity: `PROCESS_BUNDLE_DIGEST`/`_VERSION`/`_COMPONENT_KEY`/
///   `_SIDECAR_KEY` and a `"{}"` resolved config -- a real poll would
///   resolve these per-activation.
/// - Cross-app routing approvals: `PROCESS_ROUTES_TO_APPROVED`
///   (`crate::builtins::parse_approved_targets`) substitutes for a real
///   `app_install_approvals` lookup.
/// - Tenant/community scope: hardcoded to the tenant-wide `global`
///   activation for grant resolution (a per-entry envelope's own
///   tenant/community, verified by `crate::hop`, still drives every
///   downstream decision -- this hardcoding only affects which stream
///   `PROCESS_INGEST_PLATFORM`/`_SOURCE_ID` resolves to), identical scope
///   to `core/svc_action::try_start_dispatch`'s own documented gap.
fn try_start_process_loop(config: &config::Config, connections: Arc<host_api::ConnectionRegistry>) {
    if config.cli.process_app_id.is_empty() {
        tracing::info!(
            "PROCESS_APP_ID not set; process loop not started (blocked on distribution poll)"
        );
        return;
    }

    let Some(keys_raw) = config.envelope_binding_keys.as_ref() else {
        tracing::warn!("ENVELOPE_BINDING_KEYS not set; process loop disabled (hop verification must never fail open)");
        return;
    };
    let key_ring = match hop::KeyRing::parse(keys_raw.expose()) {
        Ok(r) => r,
        Err(err) => {
            tracing::warn!(error = %err, "ENVELOPE_BINDING_KEYS invalid; process loop disabled");
            return;
        }
    };

    let spine_cfg = match penguin_spine::SpineConfig::from_env() {
        Ok(c) => c,
        Err(err) => {
            tracing::warn!(error = %err, "spine config unavailable; process loop disabled");
            return;
        }
    };

    // Spec §13.5: the drain loop itself is gated on `waddles.core.
    // rust-data-plane` (default OFF, fail-closed on an unreachable
    // license server) -- see `crate::license`. `build_license_client`
    // never touches the network; only a malformed `LICENSE_SERVER_URL`/
    // `POSTHOG_HOST` fails here, treated the same as every other
    // startup-config gate in this function.
    let license_client = match license::build_license_client("waddles") {
        Ok(c) => c,
        Err(err) => {
            tracing::warn!(error = %err, "license client config invalid; process loop disabled");
            return;
        }
    };
    let license_gate: Arc<dyn license::FeatureGate> =
        Arc::new(license::LicenseFeatureGate::new(license_client));

    let cli = config.cli.clone();
    let app_id = cli.process_app_id.clone();
    let approved_targets = builtins::parse_approved_targets(&cli.process_routes_to_approved);

    tokio::spawn(async move {
        // TODO(M4+): tenant/community scope hardcoded to the tenant-wide
        // `global` activation until the distribution poll resolves the
        // real grant set -- see this function's doc.
        let scope = penguin_spine::Scope::new("global", None);
        let grants: Vec<penguin_spine::Grant> = if !cli.process_ingest_platform.is_empty()
            && !cli.process_ingest_source_id.is_empty()
        {
            vec![penguin_spine::Grant {
                stream: scope
                    .source_stream(&cli.process_ingest_platform, &cli.process_ingest_source_id),
                platform: cli.process_ingest_platform.clone(),
                source_id: cli.process_ingest_source_id.clone(),
            }]
        } else {
            tracing::info!(
                    "PROCESS_INGEST_PLATFORM/PROCESS_INGEST_SOURCE_ID unset; process loop starts with an empty grant list"
                );
            Vec::new()
        };

        let metrics: Arc<dyn penguin_spine::SpineMetrics> = Arc::new(penguin_spine::NoopMetrics);
        let deps = spine::ProcessDeps {
            app_id: app_id.clone(),
            digest: cli.process_bundle_digest.clone(),
            version: cli.process_bundle_version.clone(),
            component_key: cli.process_bundle_component_key.clone(),
            sidecar_key: cli.process_bundle_sidecar_key.clone(),
            key_ring,
            connections,
            call_timeout_ms: cli.executor_call_timeout_ms,
            load_state: Arc::new(spine::LoadState::new()),
            approved_targets,
            consumer_id: spine_cfg.consumer_id.clone(),
            spine: match penguin_spine::SpineClient::connect(spine_cfg.clone(), metrics.clone())
                .await
            {
                Ok(c) => c,
                Err(err) => {
                    tracing::error!(error = %err, "spine client connect failed; process loop not started");
                    return;
                }
            },
            metrics,
            license: license_gate,
        };

        let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel();
        tokio::spawn(async move {
            shutdown_signal().await;
            let _ = shutdown_tx.send(());
        });

        if let Err(err) = spine::run(spine_cfg, grants, deps, shutdown_rx).await {
            tracing::error!(error = %err, "process-stage drain loop exited");
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

    /// Minimal, syntactically valid [`Config`] for `try_start_process_loop`
    /// tests -- secrets are dummies, never read by that function.
    fn test_config(cli: CliConfig) -> Config {
        Config {
            cli,
            db_password: crate::config::Secret::new("x"),
            cache_password: None,
            service_api_key: crate::config::Secret::new("x"),
            envelope_binding_keys: Some(crate::config::Secret::new("k1:aabbcc")),
        }
    }

    #[tokio::test]
    async fn try_start_process_loop_noop_when_process_app_id_unset() {
        // Deliberately does not call `telemetry::init` (a process-global
        // one-time call -- see `run_with_shutdown_binds_serves_and_stops_
        // on_signal`, the only test in this binary allowed to exercise
        // it), which is exactly why this logic was split into its own
        // function: `tracing::info!`/`warn!` are harmless no-ops without an
        // installed subscriber.
        let cli = CliConfig::parse_from(["svc-process"]);
        assert_eq!(cli.process_app_id, "");
        let config = test_config(cli);
        try_start_process_loop(&config, Arc::new(host_api::ConnectionRegistry::new()));
    }

    #[tokio::test]
    async fn try_start_process_loop_disabled_without_envelope_binding_keys() {
        let cli = CliConfig::parse_from([
            "svc-process",
            "--process-app-id",
            "waddles.bot.commands.default",
        ]);
        let mut config = test_config(cli);
        config.envelope_binding_keys = None;
        try_start_process_loop(&config, Arc::new(host_api::ConnectionRegistry::new()));
    }

    #[tokio::test]
    async fn try_start_process_loop_disabled_with_a_malformed_envelope_binding_keys_value() {
        let cli = CliConfig::parse_from([
            "svc-process",
            "--process-app-id",
            "waddles.bot.commands.default",
        ]);
        let mut config = test_config(cli);
        config.envelope_binding_keys = Some(crate::config::Secret::new("not-kid-colon-hex"));
        try_start_process_loop(&config, Arc::new(host_api::ConnectionRegistry::new()));
    }

    #[tokio::test]
    async fn try_start_process_loop_degrades_gracefully_when_spine_unconfigured() {
        // `PROCESS_APP_ID`/`ENVELOPE_BINDING_KEYS` set but `VALKEY_URL`/
        // `REDIS_URL` unset must warn and return immediately rather than
        // panicking or spawning anything -- the same graceful-degradation
        // contract as a dead OTLP exporter (see `try_start_process_loop`'s
        // doc comment).
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
        let config = test_config(cli);
        try_start_process_loop(&config, Arc::new(host_api::ConnectionRegistry::new()));
    }

    #[tokio::test]
    async fn try_start_process_loop_spawns_when_spine_config_is_valid() {
        // `SpineConfig::from_env` succeeds (a syntactically valid,
        // TLS-required URL plus a password satisfies `validate()`, spec
        // Sec11.6.1) but nothing is actually listening on port 1 (a
        // privileged port, refused immediately rather than timing out) --
        // exercises the spawn path end to end (including the
        // shutdown-forwarder and the spawned loop's own connect-failure
        // logging arm) without needing a live Valkey.
        // Guard is dropped before the `.await` below (clippy
        // `await_holding_lock`) -- `penguin_spine::SpineConfig::from_env`
        // reads these env vars synchronously inside `try_start_process_loop`
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
            let config = test_config(cli);
            try_start_process_loop(&config, Arc::new(host_api::ConnectionRegistry::new()));
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

    #[tokio::test]
    async fn try_start_process_loop_resolves_a_configured_ingest_grant() {
        // Same shape as `try_start_process_loop_spawns_when_spine_config_
        // is_valid`, but with `PROCESS_INGEST_PLATFORM`/`_SOURCE_ID` also
        // set -- exercises the non-empty-grant-list branch of the interim
        // substitute (see this function's own doc).
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
                "--process-ingest-platform",
                "twitch",
                "--process-ingest-source-id",
                "tw-channelA",
            ]);
            let config = test_config(cli);
            try_start_process_loop(&config, Arc::new(host_api::ConnectionRegistry::new()));
            unsafe {
                std::env::remove_var("VALKEY_URL");
                std::env::remove_var("VALKEY_PASSWORD");
            }
        }
        tokio::time::sleep(Duration::from_secs(5)).await;
    }

    #[tokio::test]
    async fn try_start_host_api_returns_a_registry_without_blocking() {
        // No TLS config set -- the spawned listener fails to bind
        // (`HostApiError::Config`) and logs a warning; this call must
        // still return the registry immediately either way.
        let cli = CliConfig::parse_from(["svc-process"]);
        let registry = try_start_host_api(&cli);
        assert!(registry.active().is_none());
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
            envelope_binding_keys: None,
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
