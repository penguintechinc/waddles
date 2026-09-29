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
//! module doc for the full rationale). What remains a documented seam:
//! `db`/`kv`/`flags` host capabilities (`crate::capabilities`), and full
//! multi-bundle/hot-swap distribution reconciliation
//! (`crate::distribution`'s module doc).
//!
//! **Bundle-selection sources (dataplane scale design rev 4, multi-tenant,
//! 2026-09-28).** Two sources run side by side, neither exclusive of the
//! other:
//!
//! - **Multi-tenant, change-log-driven active-bundle loader**
//!   (`crate::changelog_consumer`, `core/bundle_active_set`,
//!   [`try_start_changelog_consumer`]) -- hub-api is the sole writer, this
//!   stage reads ACTIVE, APPROVED bundle config from a READ-ONLY Postgres
//!   and hot-swaps in/out with no pod restart, discovering every
//!   `(tenant_id, community_id)` scope in the database itself (no
//!   operator-configured tenant scope -- see `config::CliConfig`'s doc:
//!   `BUNDLE_SCOPE_TENANT_ID`/`BUNDLE_SCOPE_COMMUNITY_ID` are retired).
//!   Active whenever `DB_READER_PASSWORD` is configured and the
//!   `waddles.core.disable-db-bundle-config`/`waddles.core.
//!   disable-multi-tenant-watermark` kill-switches are not raw-ON.
//! - **Legacy `ACTION_BUNDLE_*` env override** (`try_start_env_bundle_loader`,
//!   `config::CliConfig::action_bundle_digest`'s doc) -- sends `load` for a
//!   statically configured bundle directly over the host-API connection,
//!   independent of any external service. Runs unconditionally alongside
//!   the DB-driven loader above; the two are gated independently.
//!
//! The now-retired `GET /api/v1/distribution/bundles?stage=action` poll
//! (spec §6.7) that used to be a third source has been removed -- superseded
//! by the DB-driven loader; see `crate::distribution`'s module doc for what
//! that leaves as a documented seam.

pub mod bundle_loader;
pub mod capabilities;
pub mod changelog_consumer;
pub mod config;
pub(crate) mod crypto;
pub mod db;
pub mod dispatch;
pub mod distribution;
pub mod egress;
pub mod error;
pub mod flags;
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
    // Shared between `bundle_loader`'s DB-driven poll (writer -- every
    // tick's `ActiveBundleRow::declared_capabilities`, `bundle_loader`'s
    // own doc) and the host-API capability set's `kv` capability (reader,
    // `bundle_host_kv::authorize::authorize_kv`) -- same "one snapshot,
    // shared Arc, one writer, one reader" pattern as `catalog` above. The
    // legacy `ACTION_BUNDLE_*` env path never writes to this snapshot at
    // all (`try_start_env_bundle_loader` has no active-set row to derive
    // capabilities from), so `kv` denies by default under that path --
    // `bundle_host_kv::authorize`'s own module doc.
    let kv_capabilities = Arc::new(bundle_host_kv::CapabilitySnapshot::new());
    let egress_denied_total = telemetry::register_egress_metrics(&prom_registry);

    // Spec §13.5's two-gate check for this service's flags
    // (`flags::RUST_DATA_PLANE_FLAG`, `flags::BUNDLE_EGRESS_FLAG`), both
    // `min_tier: free` so `flag_enabled` alone gates them (see
    // `build_license_client`'s doc for the fail-closed-to-OFF fallback
    // when even the client itself can't be built).
    let license = build_license_client();
    if let Some(client) = &license {
        let initial_refresh = Arc::clone(client);
        tokio::spawn(async move {
            if let Err(err) = initial_refresh.refresh().await {
                tracing::warn!(
                    error = %err,
                    "initial license/flag refresh failed; both spec §13.5 flags serve their \
                     default (OFF) until the next scheduled refresh succeeds"
                );
            }
        });
        // Runs for the process lifetime (crate doc: "drop it to let the
        // loop run for the process lifetime") -- the same fire-and-forget
        // posture every other background task in this module takes.
        // `drop`, not `let _ =` (clippy::let_underscore_future): the
        // `JoinHandle` itself implements `Future`, and this is a
        // deliberate detach, not an accidental one.
        drop(client.spawn_refresh());
    }

    // Registered before `prom_registry` is moved into `AppState::new`
    // below (`register_bundle_loader_excluded_metrics` only borrows it) --
    // ops-visibility fix (security review): the DB-driven bundle loader's
    // excluded-row counter.
    let bundle_loader_excluded_metric =
        telemetry::register_bundle_loader_excluded_metrics(&prom_registry);
    let changelog_consumer_metrics = telemetry::register_changelog_consumer_metrics(&prom_registry);

    let state = http::AppState::new(config.clone(), prom_registry);

    let connections = try_start_host_api(
        &config.cli,
        config.discord_bot_token.clone(),
        Arc::clone(&usage),
        catalog,
        Arc::clone(&kv_capabilities),
        egress_denied_total,
        license.clone(),
    );
    // Both bundle-selection sources run unconditionally, gated
    // independently (this module's top doc, dataplane scale design rev 4):
    // the legacy `ACTION_BUNDLE_*` env override never gates on the
    // multi-tenant DB path's own state. `resolve_db_path_active`/
    // `try_start_db_bundle_loader` (single-tenant, mutual-exclusion) are
    // retired -- superseded by `try_start_changelog_consumer`'s multi-tenant
    // discovery of every `(tenant_id, community_id)` scope in the database.
    try_start_env_bundle_loader(&config.cli, Arc::clone(&connections));
    try_start_changelog_consumer(
        &config,
        Arc::clone(&connections),
        license.clone(),
        bundle_loader_excluded_metric,
        Arc::clone(&kv_capabilities),
        changelog_consumer_metrics,
    );
    try_start_dispatch(&config, connections, usage, license);

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

/// Product identifier `penguin_licensing::LicenseConfig` validates against
/// and the flag-key prefix it resolves under (spec §13.5: "Flag keys
/// follow `{product}.{feature}`") -- both flags this service checks are
/// `waddles.core.*`.
const LICENSE_PRODUCT: &str = "waddles";

/// PenguinTech/Waddles-owned bypass suffix -- the sole license/flag
/// bypass lever, hardcoded in source (never an env var, CLI flag, or Helm
/// value -- `rules/critical-rules.md` Feature Flags & License Tiers:
/// "bypass is domain-based ONLY, never env var/CLI arg/config flag". A
/// prior revision of this file read `LICENSE_DEPLOYMENT_DOMAIN` from the
/// environment, which let anyone with Helm-values/env access fabricate
/// an arbitrary bypass domain string with no real DNS control; that was
/// reverted).
///
/// `waddles.app` is not one of the pinned `penguin_licensing` crate's
/// *default* bypass suffixes (`penguintech.cloud`/`penguincloud.io` --
/// see `LicenseConfig::DEFAULT_BYPASS_DOMAINS`), so it is explicitly
/// registered via [`penguin_licensing::LicenseConfig::with_bypass_domain`]
/// below -- exactly the mechanism that crate's own module doc describes:
/// "Product `.app` domains are added in code with `LicenseConfig::
/// with_bypass_domain`" (`packages/rust-licensing/src/config.rs`).
/// `domain_bypassed()`'s own match rule (`domain == suffix ||
/// domain.ends_with(".{suffix}")`) already does suffix/subdomain
/// matching, so registering the bare apex here makes every
/// `*.waddles.app` deployment domain bypass, not just this exact literal
/// (`waddles_app_bypass_domain_matches_any_subdomain` below proves it).
const BYPASS_DOMAIN: &str = "waddles.app";

/// This service's own deployment domain -- a `*.waddles.app` subdomain,
/// hardcoded in source (see [`BYPASS_DOMAIN`]'s doc for why it can never
/// be an env var/config value). Distinct per service (`svc-ingest.
/// waddles.app`/`svc-process.waddles.app`/`svc-action.waddles.app`) so
/// each binary's own bypass is traceable to the service that claimed it,
/// though all three resolve bypass true against the same registered
/// [`BYPASS_DOMAIN`] suffix.
const DEPLOYMENT_DOMAIN: &str = "svc-action.waddles.app";

/// Builds the shared `penguin_licensing::LicenseClient` this service's two
/// spec §13.5 flags resolve against, from the standard `LICENSE_KEY`/
/// `LICENSE_SERVER_URL`/`POSTHOG_HOST`/`POSTHOG_KEY` environment
/// variables plus the hardcoded [`DEPLOYMENT_DOMAIN`]/[`BYPASS_DOMAIN`]
/// bypass.
/// `None` only if even the no-network-required default `LicenseConfig`
/// fails to build (a hardcoded, always-valid literal URL parse -- not
/// reachable in practice, handled rather than unwrapped): callers use
/// [`flag_or_closed`] to fall back to [`flags::StaticFlag`]`(false)` for
/// both flags in that case, the same fail-closed-to-OFF posture spec
/// §13.5 already specifies for a never-seen flag.
fn build_license_client() -> Option<Arc<penguin_licensing::LicenseClient>> {
    let cfg = match penguin_licensing::LicenseConfig::from_env(LICENSE_PRODUCT) {
        Ok(cfg) => cfg,
        Err(err) => {
            tracing::error!(
                error = %err,
                "LICENSE_SERVER_URL/POSTHOG_HOST invalid; falling back to defaults \
                 (both spec §13.5 flags default OFF until a valid config is set)"
            );
            match penguin_licensing::LicenseConfig::new(LICENSE_PRODUCT) {
                Ok(cfg) => cfg,
                Err(err) => {
                    tracing::error!(
                        error = %err,
                        "LicenseConfig::new failed unexpectedly; license/flag gating disabled"
                    );
                    return None;
                }
            }
        }
    };
    let cfg = cfg
        .with_bypass_domain(BYPASS_DOMAIN)
        .with_deployment_domain(DEPLOYMENT_DOMAIN);
    match penguin_licensing::LicenseClient::new(cfg) {
        Ok(client) => Some(client),
        Err(err) => {
            tracing::error!(error = %err, "LicenseClient::new failed; license/flag gating disabled");
            None
        }
    }
}

/// Wraps `license` into a live [`flags::LicenseFlag`] for `key` when a
/// client is available, or [`flags::StaticFlag`]`(false)` otherwise -- the
/// single place every call site applies the fail-closed-to-OFF fallback
/// identically (see [`build_license_client`]'s doc for when `None`
/// happens).
fn flag_or_closed(
    license: &Option<Arc<penguin_licensing::LicenseClient>>,
    key: &'static str,
) -> Arc<dyn flags::FeatureFlag> {
    match license {
        Some(client) => flags::boxed(flags::LicenseFlag::new(Arc::clone(client), key)),
        None => flags::boxed(flags::StaticFlag(false)),
    }
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
    discord_bot_token: Option<config::Secret>,
    usage: Arc<Mutex<usage::UsageBatcher>>,
    catalog: Arc<distribution::BundleCatalog>,
    kv_capabilities: Arc<bundle_host_kv::CapabilitySnapshot>,
    egress_denied_total: prometheus::IntCounterVec,
    license: Option<Arc<penguin_licensing::LicenseClient>>,
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
        flag_or_closed(&license, flags::BUNDLE_EGRESS_FLAG),
    ));
    // `kv` reuses this same direct Valkey connection (cloned -- a cheap
    // handle clone over one shared TCP connection, not a second socket)
    // rather than opening a dedicated one: `relay_conn` already IS the
    // "second, direct redis connection" `usage.rs`'s module doc describes,
    // and `kv`'s isolation/quota model needs nothing about the connection
    // itself that `relay`/usage don't already require (`crate::capabilities`'
    // `StageCapabilities::with_kv`'s doc).
    let mut kv_conn = relay_conn.clone();
    // Low-severity fix, security review of PR #425: `count_key` has no
    // TTL, so an `allkeys-*` `maxmemory-policy` can evict it under memory
    // pressure, silently resetting the kv quota -- checked once here,
    // never on the per-op hot path (`bundle_host_kv::policy`'s doc).
    let policy_check = bundle_host_kv::policy::check_maxmemory_policy(&mut kv_conn).await;
    bundle_host_kv::policy::log_and_record(&policy_check);
    let caps = capabilities::StageCapabilities::<_, redis::aio::MultiplexedConnection>::new(
        relay_conn, egress, usage,
    )
    .with_kv(kv_conn, kv_capabilities);
    // Discord relay send (spec: relay providers, `discord`) -- graceful
    // degradation, not a startup requirement: a deployment that never sets
    // `DISCORD_BOT_TOKEN` simply never enables this provider, and a bundle
    // calling `relay.send("discord", ...)` sees `relay_unavailable` rather
    // than this process failing to start (`config::Config::
    // discord_bot_token`'s doc).
    let caps = match discord_bot_token {
        Some(token) => caps.with_discord(Arc::new(egress::ReqwestTransport), token),
        None => {
            tracing::warn!(
                "DISCORD_BOT_TOKEN not set; discord relay provider disabled \
                 (relay_unavailable on every discord relay.send)"
            );
            caps
        }
    };
    Some(Arc::new(caps))
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
    discord_bot_token: Option<config::Secret>,
    usage: Arc<Mutex<usage::UsageBatcher>>,
    catalog: Arc<distribution::BundleCatalog>,
    kv_capabilities: Arc<bundle_host_kv::CapabilitySnapshot>,
    egress_denied_total: prometheus::IntCounterVec,
    license: Option<Arc<penguin_licensing::LicenseClient>>,
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
        let capabilities = build_stage_capabilities(
            &cli,
            discord_bot_token,
            usage,
            catalog,
            kv_capabilities,
            egress_denied_total,
            license,
        )
        .await
        .unwrap_or_else(|| Arc::new(capabilities::DenyAllCapabilities));
        if let Err(err) = host_api::serve(cli, registry_for_task, capabilities, shutdown_rx).await {
            tracing::warn!(error = %err, "host-api listener unavailable; executor integration disabled");
        }
    });
    registry
}

/// Resolves the digest/config JSON the dispatch loop's `deps.digest` starts
/// with. Now that the distribution poll (this crate's former primary
/// source, retired 2026-09-27) is gone, the `ACTION_BUNDLE_*` env override
/// (`config::CliConfig::action_bundle_digest`) is the only source -- an
/// empty value means no bundle is configured yet, and the dispatch loop
/// starts with an empty digest (never blocks, never panics; the executor
/// would refuse an `invoke` against it with `UNKNOWN_BUNDLE` until a real
/// digest is configured). **This is a one-shot resolution, not hot-swap**:
/// this dispatch loop's own `deps.digest` stays fixed at whatever this
/// function returned for the life of the pod (documented seam,
/// `crate::dispatch`'s own module doc).
fn resolve_initial_bundle(env_bundle_digest: &str) -> (String, String) {
    if !env_bundle_digest.is_empty() {
        tracing::info!(
            digest = env_bundle_digest,
            "dispatch loop starting with the ACTION_BUNDLE_DIGEST env override"
        );
        return (env_bundle_digest.to_string(), "{}".to_string());
    }
    tracing::warn!("ACTION_BUNDLE_DIGEST not set; dispatch loop starting with an empty digest");
    (String::new(), "{}".to_string())
}

/// Sends `load` for a statically-configured bundle (`ACTION_BUNDLE_*` env
/// vars, `config::CliConfig::action_bundle_digest`'s doc) directly over the
/// host-API connection -- the legacy bundle-selection path, runs
/// unconditionally alongside [`try_start_changelog_consumer`] (this
/// module's top doc). A no-op (never spawns a task) when
/// `ACTION_BUNDLE_DIGEST` is unset.
fn try_start_env_bundle_loader(
    cli: &config::CliConfig,
    connections: Arc<host_api::ConnectionRegistry>,
) {
    if cli.action_bundle_digest.is_empty() {
        tracing::info!(
            "ACTION_BUNDLE_DIGEST not set; env bundle-override loader not started (no bundle \
             configured for this pod)"
        );
        return;
    }
    let app_id = cli.action_app_id.clone();
    let version = cli.action_bundle_version.clone();
    let digest = cli.action_bundle_digest.clone();
    let component_key = cli.action_bundle_component_key.clone();
    let sidecar_key = cli.action_bundle_sidecar_key.clone();
    let call_timeout_ms = cli.executor_call_timeout_ms;
    tokio::spawn(async move {
        let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel();
        tokio::spawn(async move {
            shutdown_signal().await;
            let _ = shutdown_tx.send(());
        });
        env_bundle_loader_loop(
            connections,
            app_id,
            version,
            digest,
            component_key,
            sidecar_key,
            call_timeout_ms,
            shutdown_rx,
        )
        .await;
    });
}

/// [`try_start_env_bundle_loader`]'s actual retry loop, split out so it is
/// directly testable against a fake in-memory executor connection instead
/// of requiring a real OS signal to ever terminate (mirrors `crate::
/// dispatch::drain_loop`'s split from `crate::dispatch::run`). Polls
/// `connections` every 500ms; once a connection is active and this exact
/// `Arc` hasn't already been successfully loaded (tracked by `Arc::ptr_eq`,
/// same identity convention as `core/svc_process`'s `LoadState` --
/// `ConnectionRegistry::set_active` constructs a fresh `Connection` per
/// TCP accept, so a reconnect is always a different allocation), sends
/// `load` via [`dispatch::ensure_loaded`]. A failed `load` (no connection
/// yet, or the executor rejected it) is logged at WARN/DEBUG and retried on
/// the next tick rather than propagating -- this loader has no caller to
/// report a terminal failure to, and retry is always the right response to
/// a still-starting executor.
#[allow(clippy::too_many_arguments)]
async fn env_bundle_loader_loop(
    connections: Arc<host_api::ConnectionRegistry>,
    app_id: String,
    version: String,
    digest: String,
    component_key: String,
    sidecar_key: String,
    call_timeout_ms: u64,
    mut shutdown: tokio::sync::oneshot::Receiver<()>,
) {
    let mut interval = tokio::time::interval(std::time::Duration::from_millis(500));
    interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    let mut loaded_on: Option<Arc<host_api::Connection>> = None;
    loop {
        tokio::select! {
            _ = &mut shutdown => return,
            _ = interval.tick() => {
                let Some(connection) = connections.active() else {
                    tracing::debug!(app_id = %app_id, "env bundle-override loader: no executor connection yet");
                    continue;
                };
                if loaded_on
                    .as_ref()
                    .is_some_and(|c| Arc::ptr_eq(c, &connection))
                {
                    continue;
                }
                // `(0, 0)`: see `distribution.rs`'s identical env/catalog-
                // interim-path sentinel doc -- this `ACTION_BUNDLE_*` env
                // override has no real tenant row to resolve either.
                match dispatch::ensure_loaded(
                    &connection,
                    0,
                    0,
                    &app_id,
                    &version,
                    &digest,
                    &component_key,
                    &sidecar_key,
                    penguin_bundle_host::wire::LoadLimits {
                        timeout_ms: call_timeout_ms,
                        memory_mb: 64,
                    },
                )
                .await
                {
                    Ok(_) => {
                        tracing::info!(app_id = %app_id, digest = %digest, "ACTION_BUNDLE_* env override: bundle loaded onto executor");
                        loaded_on = Some(connection);
                    }
                    Err(err) => {
                        tracing::warn!(app_id = %app_id, digest = %digest, error = %err, "ACTION_BUNDLE_* env override: bundle load failed, will retry");
                    }
                }
            }
        }
    }
}

/// Attempts to start the multi-tenant, change-log-driven active-bundle
/// loader (`crate::changelog_consumer`, dataplane scale design rev 4,
/// §7/§8 step 2). One reason this never starts, logged and not an error --
/// `DB_READER_PASSWORD` unset (the RO account hasn't been provisioned yet
/// in this environment). Either way, the existing `ACTION_APP_ID`/
/// `ACTION_BUNDLE_*` env selection remains the sole other source (the
/// former `crate::distribution` catalog poll was retired 2026-09-27); this
/// loader only supplements it once actually configured, and is
/// additionally gated per-tick on BOTH `waddles.core.disable-db-bundle-config`
/// and `waddles.core.disable-multi-tenant-watermark` (each already the
/// negated "is this path enabled" answer, enabled by default, combined via
/// `flags::AllFlags`) inside `changelog_consumer::run` regardless of
/// whether this function's own startup gate passes. Reuses the
/// already-built, already-refreshing `license` client (`run_with_shutdown`'s
/// own `build_license_client` call) rather than constructing a second one.
///
/// **`BUNDLE_SCOPE_TENANT_ID`/`BUNDLE_SCOPE_COMMUNITY_ID` REMOVED**
/// (dataplane scale design, user requirement: "every svc_process/
/// svc_action pod serves ALL tenants") -- this loader now discovers and
/// serves every `(tenant_id, community_id)` scope in the database itself.
fn try_start_changelog_consumer(
    config: &config::Config,
    connections: Arc<host_api::ConnectionRegistry>,
    license: Option<Arc<penguin_licensing::LicenseClient>>,
    excluded_metric: prometheus::IntCounterVec,
    kv_capabilities: Arc<bundle_host_kv::CapabilitySnapshot>,
    changelog_consumer_metrics: telemetry::ChangelogConsumerMetrics,
) {
    let Some(password) = config.db_reader_password.as_ref() else {
        tracing::info!(
            "DB_READER_PASSWORD not set; multi-tenant changelog consumer not started (env selection remains authoritative)"
        );
        return;
    };

    // `flags::db_bundle_config_flag`/`multi_tenant_watermark_flag` (not the
    // generic `flag_or_closed` + `NegatedFlag` composition) -- this crate's
    // own hardcoded license-bypass domain (`build_license_client` above)
    // makes `flag_enabled` read `true` for ANY key, so a bare negation
    // would report the multi-tenant path permanently DISABLED for every
    // deployment of this service; see `flags::DisableDbBundleConfigFlag`'s
    // doc.
    let flag: Arc<dyn flags::FeatureFlag> = Arc::new(flags::AllFlags(vec![
        flags::db_bundle_config_flag(&license),
        flags::multi_tenant_watermark_flag(&license),
    ]));
    let reader_cfg = bundle_active_set::ReaderConfig {
        host: config.cli.db_reader_host.clone(),
        port: config.cli.db_reader_port,
        name: config.cli.db_reader_name.clone(),
        user: config.cli.db_reader_user.clone(),
    };
    let password = password.expose().to_string();
    let poll_interval = config.cli.bundle_config_poll_interval();
    let full_reconcile_interval = config.cli.full_reconcile_interval();
    let call_timeout_ms = config.cli.executor_call_timeout_ms;

    tokio::spawn(async move {
        let db = match bundle_active_set::reader::connect(&reader_cfg, &password).await {
            Ok(db) => db,
            Err(err) => {
                tracing::error!(error = %err, "db-reader connection failed; multi-tenant changelog consumer not started");
                return;
            }
        };
        let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel();
        tokio::spawn(async move {
            shutdown_signal().await;
            let _ = shutdown_tx.send(());
        });
        changelog_consumer::run(
            db,
            poll_interval,
            full_reconcile_interval,
            call_timeout_ms,
            flag,
            connections,
            excluded_metric,
            changelog_consumer_metrics,
            kv_capabilities,
            shutdown_rx,
        )
        .await;
    });
}

/// Starts the action-stage dispatch loop (`crate::dispatch::run`) as its
/// own background task, mirroring `core/svc_process`'s
/// `try_start_spine_drain` exactly: two independent reasons this never
/// starts, both logged and neither an error -- `ACTION_APP_ID` unset (no
/// bundle assigned yet), or `penguin_spine::SpineConfig::from_env()`/
/// `ENVELOPE_BINDING_KEYS` parsing failing (missing/invalid required
/// config -- hop verification must never silently fail open, so a missing
/// keyring disables the loop rather than starting it unverified). Runs
/// unconditionally alongside whichever bundle-selection path
/// `run_with_shutdown` chose (this module's top doc) -- this is the
/// consumer loop, not a bundle-selection path itself.
fn try_start_dispatch(
    config: &config::Config,
    connections: Arc<host_api::ConnectionRegistry>,
    usage: Arc<Mutex<usage::UsageBatcher>>,
    license: Option<Arc<penguin_licensing::LicenseClient>>,
) {
    if config.cli.action_app_id.is_empty() {
        tracing::info!("ACTION_APP_ID not set; dispatch loop not started (no bundle assigned)");
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
        let (digest, config_json) = resolve_initial_bundle(&config.cli.action_bundle_digest);
        // TODO(M3+): tenant/community scope is hardcoded to the
        // tenant-wide `global` activation until multi-bundle scheduling
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

        let rust_data_plane = flag_or_closed(&license, flags::RUST_DATA_PLANE_FLAG);
        if let Err(err) = dispatch::run(
            spine_cfg,
            vec![grant],
            stream_key,
            deps,
            rust_data_plane,
            shutdown_rx,
        )
        .await
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
            discord_bot_token: None,
            db_reader_password: None,
        }
    }

    #[tokio::test]
    async fn flag_or_closed_fails_closed_to_off_when_no_license_client_is_available() {
        let flag = flag_or_closed(&None, flags::RUST_DATA_PLANE_FLAG);
        assert!(!flag.enabled().await);
    }

    #[tokio::test]
    async fn flag_or_closed_wraps_a_real_client_as_a_license_flag() {
        let cfg = penguin_licensing::LicenseConfig::new(LICENSE_PRODUCT)
            .expect("default LicenseConfig::new never fails");
        let client = penguin_licensing::LicenseClient::new(cfg)
            .expect("LicenseClient::new with a valid default config never fails");
        let flag = flag_or_closed(&Some(client), flags::RUST_DATA_PLANE_FLAG);
        // A cold client (never refreshed) has an empty snapshot -- proves
        // this reaches the real `LicenseFlag` wrapper, not some other
        // default, since a cold real client also fails closed to OFF.
        assert!(!flag.enabled().await);
    }

    #[test]
    fn build_license_client_succeeds_with_no_license_env_vars_set() {
        // `LicenseConfig::from_env`'s defaults (no `LICENSE_KEY`/
        // `LICENSE_SERVER_URL`/`POSTHOG_HOST`/`POSTHOG_KEY` set) still
        // validate -- the community-tier, no-flags-configured shape every
        // deployment starts from before an operator sets a real key.
        assert!(build_license_client().is_some());
    }

    #[tokio::test]
    async fn build_license_client_hardcoded_domain_bypasses_flag_checks() {
        // Proves the net effect the hardcoded DEPLOYMENT_DOMAIN bypass is
        // meant to have: `build_license_client`'s own output resolves
        // every flag ON, with zero network access and zero env/config
        // spoofability (`rules/critical-rules.md` Feature Flags & License
        // Tiers: bypass is domain-based ONLY -- satisfied entirely in
        // source here).
        let client = build_license_client().expect("valid defaults");
        assert!(client.bypass_active());
        let flag = flag_or_closed(&Some(Arc::clone(&client)), flags::RUST_DATA_PLANE_FLAG);
        assert!(flag.enabled().await);
    }

    #[test]
    fn waddles_app_bypass_domain_matches_any_subdomain() {
        // `LicenseConfig::domain_bypassed()`'s own match rule is
        // `domain == suffix || domain.ends_with(".{suffix}")` (`packages/
        // rust-licensing/src/config.rs`) -- proves registering the bare
        // `BYPASS_DOMAIN` apex via `with_bypass_domain` makes both the
        // apex itself AND any `*.waddles.app` subdomain (this service's
        // own `DEPLOYMENT_DOMAIN` included) resolve bypass true, not just
        // one exact literal. No env access -- no lock needed.
        let apex = penguin_licensing::LicenseConfig::new(LICENSE_PRODUCT)
            .expect("valid defaults")
            .with_bypass_domain(BYPASS_DOMAIN)
            .with_deployment_domain(BYPASS_DOMAIN);
        assert!(apex.domain_bypassed());

        let subdomain = penguin_licensing::LicenseConfig::new(LICENSE_PRODUCT)
            .expect("valid defaults")
            .with_bypass_domain(BYPASS_DOMAIN)
            .with_deployment_domain(DEPLOYMENT_DOMAIN);
        assert!(subdomain.domain_bypassed());

        let arbitrary_subdomain = penguin_licensing::LicenseConfig::new(LICENSE_PRODUCT)
            .expect("valid defaults")
            .with_bypass_domain(BYPASS_DOMAIN)
            .with_deployment_domain("anything.waddles.app");
        assert!(arbitrary_subdomain.domain_bypassed());
    }

    #[test]
    fn a_non_bypass_domain_does_not_resolve_bypass() {
        // Two negative cases: a domain sharing no suffix with
        // `BYPASS_DOMAIN` at all, and the adversarial near-miss
        // `evil-waddles.app` -- which contains the substring
        // `waddles.app` but does NOT end with the required `.waddles.app`
        // dot-boundary, so a naive substring check would wrongly bypass
        // it while the real suffix check correctly rejects it.
        let unrelated = penguin_licensing::LicenseConfig::new(LICENSE_PRODUCT)
            .expect("valid defaults")
            .with_bypass_domain(BYPASS_DOMAIN)
            .with_deployment_domain("example.com");
        assert!(!unrelated.domain_bypassed());

        let near_miss = penguin_licensing::LicenseConfig::new(LICENSE_PRODUCT)
            .expect("valid defaults")
            .with_bypass_domain(BYPASS_DOMAIN)
            .with_deployment_domain("evil-waddles.app");
        assert!(!near_miss.domain_bypassed());
    }

    #[test]
    fn resolve_initial_bundle_returns_the_env_override_digest_when_set() {
        let (digest, config_json) = resolve_initial_bundle("sha256:aa");
        assert_eq!(digest, "sha256:aa");
        assert_eq!(config_json, "{}");
    }

    #[test]
    fn resolve_initial_bundle_returns_empty_when_env_override_unset() {
        let (digest, config_json) = resolve_initial_bundle("");
        assert_eq!(digest, "");
        assert_eq!(config_json, "{}");
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
            discord_bot_token: None,
            db_reader_password: None,
        };
        let connections = Arc::new(host_api::ConnectionRegistry::new());
        let usage = Arc::new(std::sync::Mutex::new(usage::UsageBatcher::new()));
        try_start_dispatch(&config, connections, usage, None);
    }

    /// `try_start_env_bundle_loader`'s own gate: `ACTION_BUNDLE_DIGEST`
    /// unset never starts the loader task -- a fire-and-forget call proving
    /// no panic and no spawned task.
    #[test]
    fn try_start_env_bundle_loader_disabled_without_digest() {
        let cli = CliConfig::parse_from(["svc-action"]);
        let connections = Arc::new(host_api::ConnectionRegistry::new());
        try_start_env_bundle_loader(&cli, connections);
    }

    /// Security review fix regression test (carried forward): `db_reader_
    /// password: None` (the value `config::Config::from_cli` now produces
    /// for both a genuinely unset `DB_READER_PASSWORD` and Helm's
    /// always-rendered-but-empty default) must take
    /// `try_start_changelog_consumer`'s documented no-op branch rather than
    /// attempting a DB connection -- fire-and-forget, same shape as
    /// `try_start_env_bundle_loader_disabled_without_digest` above.
    #[tokio::test]
    async fn try_start_changelog_consumer_noop_when_db_reader_password_unset() {
        let cli = CliConfig::parse_from(["svc-action"]);
        let config = Config {
            cli,
            db_password: Secret::new("test-password"),
            envelope_binding_keys: None,
            discord_bot_token: None,
            db_reader_password: None,
        };
        let connections = Arc::new(host_api::ConnectionRegistry::new());
        try_start_changelog_consumer(
            &config,
            connections,
            None,
            test_excluded_metric(),
            Arc::new(bundle_host_kv::CapabilitySnapshot::new()),
            test_changelog_consumer_metrics(),
        );
    }

    /// Removal regression (dataplane scale design, multi-tenant): the
    /// retired `BUNDLE_SCOPE_TENANT_ID`/`--bundle-scope-tenant-id` flag must
    /// no longer be a recognized CLI arg.
    #[test]
    fn bundle_scope_tenant_id_flag_removed_from_svc_action() {
        let result = CliConfig::try_parse_from(["svc-action", "--bundle-scope-tenant-id", "0"]);
        assert!(result.is_err());
    }

    /// A standalone, unregistered `IntCounterVec` for
    /// `try_start_changelog_consumer` tests -- see `bundle_loader::tests::
    /// test_metric`'s identical rationale (no `Registry` needed for
    /// `.inc()` to work correctly).
    fn test_excluded_metric() -> prometheus::IntCounterVec {
        prometheus::IntCounterVec::new(
            prometheus::Opts::new("test_bundle_active_set_excluded_total", "test"),
            &["app_id", "reason"],
        )
        .expect("valid metric definition")
    }

    /// A standalone, unregistered [`telemetry::ChangelogConsumerMetrics`] --
    /// same rationale as [`test_excluded_metric`].
    fn test_changelog_consumer_metrics() -> telemetry::ChangelogConsumerMetrics {
        telemetry::register_changelog_consumer_metrics(&prometheus::Registry::new())
    }

    /// The core of this PR's fix: once a host-API connection is active,
    /// [`env_bundle_loader_loop`] sends `load` for the `ACTION_BUNDLE_*`
    /// env-configured bundle over it -- entirely independent of the
    /// distribution catalog/hub-api (none is constructed in this test) --
    /// so the action stage can invoke without ever having polled a live
    /// hub-api. Drives a fake executor over an in-memory duplex, matching
    /// `crate::dispatch`'s own `ensure_loaded_sends_load_and_returns_the_
    /// loaded_reply` test harness shape.
    #[tokio::test]
    async fn env_bundle_loader_loop_loads_the_env_configured_bundle_once_connected() {
        use penguin_bundle_host::wire::{
            read_frame, write_frame, Frame, HelloBody, HelloOkBody, LoadBody, LoadedBody, Message,
            SandboxInfo,
        };

        let (stage_io, mut executor_io) = tokio::io::duplex(64 * 1024);
        let received_load: Arc<tokio::sync::Mutex<Option<LoadBody>>> =
            Arc::new(tokio::sync::Mutex::new(None));
        let received_load_writer = Arc::clone(&received_load);
        tokio::spawn(async move {
            write_frame(
                &mut executor_io,
                &Frame::new(
                    1,
                    Message::Hello(HelloBody {
                        protocol_version: 1,
                        executor_version: "0.1.0".to_string(),
                        wasmtime_version: "test".to_string(),
                        wasmtime_abi: "test".to_string(),
                        collector: "drc".to_string(),
                        sandbox: SandboxInfo {
                            runtime: "runc".to_string(),
                            verified: false,
                        },
                    }),
                ),
            )
            .await
            .unwrap();
            let hello_ok = read_frame(&mut executor_io).await.unwrap();
            assert!(matches!(hello_ok.message, Message::HelloOk(_)));

            let load = read_frame(&mut executor_io).await.unwrap();
            let load_body = match load.message {
                Message::Load(b) => b,
                other => panic!("expected load, got {other:?}"),
            };
            *received_load_writer.lock().await = Some(load_body.clone());
            write_frame(
                &mut executor_io,
                &Frame::new(
                    load.id,
                    Message::Loaded(LoadedBody {
                        app_id: load_body.app_id,
                        digest: load_body.digest,
                        precompile_ms: 1,
                        exports: vec!["dispatch".to_string()],
                    }),
                ),
            )
            .await
            .unwrap();
        });

        let (connection, read_loop) = host_api::run_connection(
            stage_io,
            HelloOkBody {
                stage: "svc-action".to_string(),
                protocol_version: 1,
                limits: penguin_bundle_host::wire::HelloLimits {
                    call_timeout_ms: 2000,
                    memory_mb: 64,
                    max_concurrent_calls: 32,
                },
            },
            false,
            Arc::new(capabilities::DenyAllCapabilities),
        )
        .await
        .expect("handshake succeeds");
        tokio::spawn(read_loop);

        let connections = Arc::new(host_api::ConnectionRegistry::new());
        connections.set_active(connection);

        let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel();
        let loader = tokio::spawn(env_bundle_loader_loop(
            Arc::clone(&connections),
            "waddles.a.b.c".to_string(),
            "2.0.0".to_string(),
            "sha256:aa".to_string(),
            "bundles/waddles.a.b.c/2.0.0/aa.wasm".to_string(),
            "bundles/waddles.a.b.c/2.0.0/aa.json".to_string(),
            2000,
            shutdown_rx,
        ));

        // The loop ticks every 500ms; give it a couple of ticks to observe
        // the already-active connection and send `load`.
        tokio::time::sleep(std::time::Duration::from_millis(1200)).await;
        let _ = shutdown_tx.send(());
        tokio::time::timeout(std::time::Duration::from_secs(5), loader)
            .await
            .expect("env_bundle_loader_loop must return promptly once shutdown resolves")
            .expect("loader task must not panic");

        let load_body = received_load
            .lock()
            .await
            .clone()
            .expect("the fake executor must have received a load frame");
        assert_eq!(load_body.app_id, "waddles.a.b.c");
        assert_eq!(load_body.version, "2.0.0");
        assert_eq!(load_body.digest, "sha256:aa");
        assert_eq!(
            load_body.component_key,
            "bundles/waddles.a.b.c/2.0.0/aa.wasm"
        );
        assert_eq!(load_body.sidecar_key, "bundles/waddles.a.b.c/2.0.0/aa.json");
    }

    /// `env_bundle_loader_loop` must terminate promptly once `shutdown`
    /// resolves even when no connection is ever active (hub-api-independent
    /// -- and here, executor-independent too): the loop must never block on
    /// a connection that never arrives.
    #[tokio::test]
    async fn env_bundle_loader_loop_stops_promptly_without_a_connection() {
        let connections = Arc::new(host_api::ConnectionRegistry::new());
        let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel();
        let loader = tokio::spawn(env_bundle_loader_loop(
            connections,
            "waddles.a.b.c".to_string(),
            "1".to_string(),
            "sha256:aa".to_string(),
            "component-key".to_string(),
            "sidecar-key".to_string(),
            2000,
            shutdown_rx,
        ));
        shutdown_tx.send(()).unwrap();
        tokio::time::timeout(std::time::Duration::from_secs(5), loader)
            .await
            .expect("env_bundle_loader_loop must return promptly once shutdown resolves")
            .expect("loader task must not panic");
    }
}
