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
//! - The `db`/`http`/`flags` host capabilities
//!   (`crate::capabilities::StageCapabilities`) -- `context`/`clock`/`log`/
//!   `kv` are fully wired
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
pub mod bundle_loader;
pub mod capabilities;
pub mod config;
pub mod error;
pub mod grant_gate;
pub mod hop;
pub mod host_api;
pub mod http;
pub mod license;
pub mod source_supervisor;
pub mod spine;
pub mod telemetry;

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use tokio::signal;

use crate::license::FeatureGate;

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

    // Registered before `prom_registry` is moved into `AppState::new`
    // below (`register_bundle_loader_excluded_metrics` only borrows it) --
    // ops-visibility fix (security review): the DB-driven bundle loader's
    // excluded-row counter.
    let bundle_loader_excluded_metric =
        telemetry::register_bundle_loader_excluded_metrics(&prom_registry);
    let source_supervisor_metrics =
        telemetry::register_source_binding_supervisor_metrics(&prom_registry);

    let state = http::AppState::new(config.clone(), prom_registry);

    let connections = try_start_host_api(&config.cli);
    // Mutual exclusion, resolved ONCE at startup -- see
    // `resolve_db_path_active`'s own doc for why this is not re-evaluated
    // per-tick for this specific dispatch decision, and why that's an
    // accepted tradeoff (a live kill-switch flip mid-run still stops
    // DB-driven work via the existing per-tick gates inside
    // `bundle_loader::run_tick`/`source_supervisor::run_tick`, it just
    // doesn't fail OVER to the legacy loop without a pod restart).
    if resolve_db_path_active(&config).await {
        if !config.cli.process_app_id.is_empty() {
            tracing::info!(
                process_app_id = %config.cli.process_app_id,
                "DB-driven bundle-config path active at startup; ignoring legacy \
                 PROCESS_APP_ID/PROCESS_INGEST_* env selection (restart required to fall back)"
            );
        }
        try_start_db_bundle_loader(
            &config,
            connections,
            bundle_loader_excluded_metric,
            source_supervisor_metrics,
        );
    } else {
        tracing::info!(
            "DB-driven bundle-config path inactive at startup (kill-switch on, or \
             DB_READER_*/BUNDLE_SCOPE_TENANT_ID not configured); using legacy \
             PROCESS_APP_ID/PROCESS_INGEST_* env selection"
        );
        try_start_process_loop(&config, connections);
    }

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

/// Opens the direct Valkey connection the `kv` host capability is backed
/// by (`spine::ProcessDeps::kv_conn`'s doc), built from the same
/// `VALKEY_URL`/username/password/TLS/CA-file settings
/// `penguin_spine::SpineClient` connects with -- byte-for-byte the same
/// connection-building logic as `core/svc_action::usage::connect`
/// (duplicated rather than shared: it is a dozen lines of `redis`-crate
/// client construction, not the `kv` capability's own logic, which
/// already lives in exactly one place, `bundle_host_kv`). Never fatal on
/// failure -- returns `None` (logged) so the caller can start every other
/// capability regardless (`crate::capabilities::StageCapabilities::with_kv`'s
/// doc).
/// Builds (never connects) a [`redis::Client`] from `cfg`'s `VALKEY_URL`/
/// username/password/TLS/CA-file settings -- shared by [`connect_kv`] (which
/// opens a connection over it for the `kv` host capability) and
/// `crate::grant_gate::build_production_gate`'s callers (which need the
/// `redis::Client` itself, to hand to `run_grant_gate_refresh_loop`, not a
/// pre-opened connection). `None` (logged) on a malformed `VALKEY_URL` or a
/// TLS client-build failure -- never fatal to the caller.
fn build_redis_client(cfg: &penguin_spine::SpineConfig) -> Option<redis::Client> {
    use redis::IntoConnectionInfo;

    let info: redis::ConnectionInfo = match cfg.valkey_url.as_str().into_connection_info() {
        Ok(info) => info,
        Err(err) => {
            tracing::warn!(error = %err, "invalid VALKEY_URL; Valkey-backed features disabled");
            return None;
        }
    };
    let mut settings = info.redis_settings().clone();
    if let Some(username) = &cfg.valkey_username {
        settings = settings.set_username(username);
    }
    if let Some(password) = &cfg.valkey_password {
        settings = settings.set_password(password);
    }
    let info = info.set_redis_settings(settings);

    if cfg.security_transport_tls {
        host_api::ensure_crypto_provider_installed();
        let root_cert = std::fs::read(&cfg.valkey_ca_file).ok();
        match redis::Client::build_with_tls(
            info,
            redis::TlsCertificates {
                client_tls: None,
                root_cert,
            },
        ) {
            Ok(client) => Some(client),
            Err(err) => {
                tracing::warn!(error = %err, "TLS Valkey client build failed; Valkey-backed features disabled");
                None
            }
        }
    } else {
        match redis::Client::open(info) {
            Ok(client) => Some(client),
            Err(err) => {
                tracing::warn!(error = %err, "Valkey client build failed; Valkey-backed features disabled");
                None
            }
        }
    }
}

async fn connect_kv(cfg: &penguin_spine::SpineConfig) -> Option<redis::aio::MultiplexedConnection> {
    let client = build_redis_client(cfg)?;
    match client.get_multiplexed_async_connection().await {
        Ok(conn) => Some(conn),
        Err(err) => {
            tracing::warn!(error = %err, "kv capability: Valkey connection failed; kv disabled (not_implemented on every kv host-call)");
            None
        }
    }
}

/// Resolves `app_versions.id` (the numeric id [`bundle_capability_gate::
/// GrantScopeKey::app_version`]/`InvokeScope::app_version` actually uses --
/// same value `app_active_versions.version_id` stores) for one
/// `(app_id, version)` semver-text pair, through the same RO reader
/// connection every other query in this function uses. `None` on no
/// matching row OR a query error -- both mean "cannot safely resolve a real
/// app_version," which every caller treats as fail-closed (process loop not
/// started), never a `0` placeholder.
async fn resolve_app_version_id(
    db: &sea_orm::DatabaseConnection,
    app_id: &str,
    version: &str,
) -> Option<i64> {
    use bundle_active_set::entities::app_versions;
    use sea_orm::{ColumnTrait, EntityTrait, QueryFilter};

    app_versions::Entity::find()
        .filter(app_versions::Column::AppId.eq(app_id.to_string()))
        .filter(app_versions::Column::Version.eq(version.to_string()))
        .one(db)
        .await
        .ok()
        .flatten()
        .map(|row| row.id)
}

/// Attempts to start the **legacy, single-consumer** process-stage drain
/// loop (`crate::spine::run`) as its own background task, mirroring
/// `core/svc_action::try_start_dispatch`'s shape: three independent reasons
/// this never starts, all logged and none an error -- `PROCESS_APP_ID`
/// unset (no bundle assigned yet, multi-bundle scheduling is blocked on the
/// distribution poll); `penguin_spine::SpineConfig::from_env()`/
/// `ENVELOPE_BINDING_KEYS` parsing failing (hop verification must never
/// silently fail open, so a missing keyring disables the loop rather than
/// starting it unverified); or the license client failing to construct (a
/// malformed `LICENSE_SERVER_URL`/`POSTHOG_HOST` -- see `crate::license`).
/// Once started, the loop itself is additionally gated per-batch on
/// `waddles.core.rust-data-plane` (spec §13.5) -- OFF drains nothing
/// without stopping the loop or affecting `/health`/`/metrics`, see
/// `crate::spine::drain_batch`.
///
/// **Superseded as the primary source-consumption path by
/// `crate::source_supervisor` (one dedicated consumer per DB-driven
/// `(app_id, platform, source_id)` binding, `try_start_db_bundle_loader`).**
/// This function now exists ONLY as the kill-switch/missing-config
/// fallback (`crate::license::DISABLE_DB_BUNDLE_CONFIG_FLAG`'s doc, point
/// 4): it still starts unconditionally whenever `PROCESS_APP_ID` is set,
/// exactly as before, so operators relying on the env-configured single
/// stream (no DB bindings provisioned yet, or the kill-switch flipped ON)
/// keep working unchanged; once `app_source_bindings` rows exist for this
/// tenant/scope, operators should stop setting `PROCESS_APP_ID`/
/// `PROCESS_INGEST_*` -- otherwise this loop and `crate::source_supervisor`
/// would both run a `GroupReader` against the same stream/group/consumer
/// identity, an unsupported operational overlap this landing does not
/// attempt to detect or reconcile.
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

    // Grant-gate scope resolution (spec SS4/SS5.1): when this process is
    // ALSO configured with a real `BUNDLE_SCOPE_TENANT_ID` and a DB reader
    // account -- both already used by `try_start_db_bundle_loader`, never
    // exclusive to it -- resolve the numeric `tenant_id`/`community_id`/
    // `app_version` this loop's own invocations authorize under from the
    // SAME RO-replica tables, instead of the `(0, 0, 0)` placeholder
    // `crate::capabilities::StageCapabilities::new` used to hardcode
    // unconditionally (a placeholder that only ever matched a grant row
    // ALSO written under tenant/community/version `0` -- not a real scope).
    // `None` when unconfigured: this loop's env-only mode (this function's
    // own doc) keeps running exactly as before, still fail-closed for every
    // non-platform permission via the `(0, 0, 0)` sentinel -- but a
    // resolution FAILURE once configured (below) stops the loop from
    // starting at all, never silently falling back to that sentinel.
    let scope_reader_cfg = cli.bundle_scope_tenant_id.get().and_then(|tenant_id| {
        config.db_reader_password.as_ref().map(|password| {
            (
                tenant_id,
                cli.bundle_scope_community_id,
                password.expose().to_string(),
            )
        })
    });
    let reader_cfg = bundle_active_set::ReaderConfig {
        host: cli.db_reader_host.clone(),
        port: cli.db_reader_port,
        name: cli.db_reader_name.clone(),
        user: cli.db_reader_user.clone(),
    };
    let poll_interval = cli.bundle_config_poll_interval();

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

        // Fail-closed scope resolution -- see `scope_reader_cfg`'s doc.
        // `grant_db` is the RO-replica connection `grant_gate::
        // PgGrantLoader` reads through when scope resolution succeeds;
        // `None` (unconfigured) falls back to `InMemoryGrantLoader` (always
        // denies every non-platform permission, same posture as today).
        let (grant_db, tenant_id, community_id, app_version) = match scope_reader_cfg {
            Some((tenant_id, community_id, password)) => {
                let db = match bundle_active_set::reader::connect(&reader_cfg, &password).await {
                    Ok(db) => db,
                    Err(err) => {
                        tracing::error!(error = %err, "db-reader connection failed; process loop not started (fail-closed: BUNDLE_SCOPE_TENANT_ID is configured, so a (0, 0, 0) placeholder scope is never substituted)");
                        return;
                    }
                };
                match bundle_active_set::scope::resolve_scope(&db, tenant_id, community_id).await {
                    Ok(Some(_resolved)) => {}
                    Ok(None) => {
                        tracing::error!(tenant_id, community_id, "BUNDLE_SCOPE_TENANT_ID/_COMMUNITY_ID could not be resolved via the RO reader connection (missing row, or a community id belonging to a different tenant); process loop not started (fail-closed)");
                        return;
                    }
                    Err(err) => {
                        tracing::error!(error = %err, tenant_id, community_id, "tenant/community scope resolution query failed; process loop not started (fail-closed)");
                        return;
                    }
                }
                let app_version = if cli.process_bundle_version.is_empty() {
                    // No bundle configured yet -- same "empty means
                    // unconfigured" sentinel `ProcessDeps::digest`'s doc
                    // already documents, not a resolution failure.
                    0
                } else {
                    match resolve_app_version_id(&db, &app_id, &cli.process_bundle_version).await {
                        Some(id) => id,
                        None => {
                            tracing::error!(app_id = %app_id, version = %cli.process_bundle_version, "PROCESS_BUNDLE_VERSION has no matching app_versions row; process loop not started (fail-closed)");
                            return;
                        }
                    }
                };
                (Some(db), tenant_id, community_id, app_version)
            }
            None => (None, 0, 0, 0),
        };
        // Wrapped in a snapshot (rather than a plain `i64`) purely to share
        // `spine::ProcessDeps::app_version_snapshot`'s type with the
        // DB-driven paths -- this legacy single-consumer loop resolves
        // ONCE at startup, same as before this change (hot-swap-aware
        // per-invocation resolution is `crate::bundle_loader`/
        // `crate::source_supervisor`'s own concern, not this env-configured
        // fallback's).
        let app_version_snapshot = bundle_active_set::ActiveVersionSnapshot::new();
        app_version_snapshot.update(&[bundle_active_set::ActiveBundleRow {
            app_id: app_id.clone(),
            version: cli.process_bundle_version.clone(),
            version_id: app_version,
            digest: cli.process_bundle_digest.clone(),
            component_key: cli.process_bundle_component_key.clone(),
            sidecar_key: cli.process_bundle_sidecar_key.clone(),
        }]);

        let metrics: Arc<dyn penguin_spine::SpineMetrics> = Arc::new(penguin_spine::NoopMetrics);
        // `kv` host capability: opened once here, cloned into every
        // per-invoke `StageCapabilities` (`spine::ProcessDeps::kv_conn`'s
        // doc) rather than reopened per invoke. `None` on failure is not
        // fatal to the process loop -- every `kv` host-call then sees
        // `not_implemented` instead (`connect_kv`'s doc).
        let kv_conn = connect_kv(&spine_cfg).await;
        // Production grant gate (spec SS4/SS5): `PgGrantLoader` against the
        // just-resolved RO-replica connection when scope was configured and
        // resolved successfully, `InMemoryGrantLoader` (always denies every
        // non-platform permission) otherwise -- `build_production_gate`
        // unions the always-granted platform trio over either, and spawns
        // the push-invalidation/poll-refresh loop when a Valkey client is
        // available.
        let redis_client = build_redis_client(&spine_cfg);
        let gate: Arc<bundle_capability_gate::CapabilityGate> = match grant_db {
            Some(db) => grant_gate::build_production_gate(
                grant_gate::PgGrantLoader::new(db),
                redis_client,
                poll_interval,
            ),
            None => grant_gate::build_production_gate(
                bundle_capability_gate::InMemoryGrantLoader::new(),
                redis_client,
                poll_interval,
            ),
        };
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
            kv_conn,
            tenant_id,
            community_id,
            app_version_snapshot,
            gate,
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

/// Resolves, ONCE at startup, whether the DB-driven bundle-config path
/// (`crate::bundle_loader` + `crate::source_supervisor`) or the legacy
/// `PROCESS_APP_ID`/`PROCESS_INGEST_*` single-consumer env path
/// (`try_start_process_loop`) is authoritative for this process's entire
/// lifetime -- **mutual exclusion, not operator discipline**: exactly one
/// of the two ever starts, regardless of what `PROCESS_APP_ID` happens to
/// be set to.
///
/// Deliberately evaluated only here, once, rather than per-tick the way
/// `bundle_loader::run_tick`/`source_supervisor::run_tick` re-check their
/// own kill-switch gate on every poll: a live `waddles.core.
/// disable-db-bundle-config` flip mid-run is still caught by those
/// per-tick gates (DB-driven load/consumption stops immediately), but this
/// function's own path-selection decision does NOT re-run -- falling back
/// to (or away from) the legacy loop requires a pod restart. That is an
/// accepted tradeoff (see the coordinator's own framing: "acceptable to
/// require a restart to switch paths"), not an oversight: it guarantees
/// the two paths can never run concurrently against the same stream/group,
/// which a live re-evaluation racing against already-spawned consumer
/// tasks could not cleanly guarantee.
///
/// The DB path is active when [`db_path_selected`] says so: DB config
/// present (`DB_READER_PASSWORD` set, `BUNDLE_SCOPE_TENANT_ID` configured)
/// AND the kill-switch gate reports enabled (`license::
/// DbBundleConfigGate::enabled`, already the negated "is the DB path
/// enabled" answer -- default `true` when the flag is unseen or the
/// license server is unreachable). A malformed `LICENSE_SERVER_URL`/
/// `POSTHOG_HOST` (the only way `license::build_license_client` itself can
/// fail) is treated as "DB path inactive" -- the same fail-safe posture
/// `try_start_db_bundle_loader`'s own internal gate uses for the identical
/// failure.
async fn resolve_db_path_active(config: &config::Config) -> bool {
    let db_config_present =
        config.db_reader_password.is_some() && config.cli.bundle_scope_tenant_id.get().is_some();
    if !db_config_present {
        return false;
    }

    let gate_enabled = match license::build_license_client("waddles") {
        Ok(client) => license::DbBundleConfigGate::new(client).enabled().await,
        Err(err) => {
            tracing::warn!(
                error = %err,
                "license client config invalid; treating DB-driven bundle-config path as inactive at startup"
            );
            false
        }
    };

    db_path_selected(db_config_present, gate_enabled)
}

/// Pure boolean combination behind [`resolve_db_path_active`] -- split out
/// so the "which path wins" decision is directly unit-testable with a
/// fixed kill-switch-gate value, without needing a live/mocked
/// `penguin_licensing::LicenseClient` round trip to force a "kill-switch
/// ON" flag value (not achievable in a unit test against the real client).
fn db_path_selected(db_config_present: bool, gate_enabled: bool) -> bool {
    db_config_present && gate_enabled
}

/// Attempts to start the DB-driven active-bundle loader
/// (`crate::bundle_loader`) AND the DB-driven source-binding supervisor
/// (`crate::source_supervisor`) as sibling background tasks sharing one
/// read-only Postgres connection -- spec: hub-api is the sole writer, this
/// stage reads ACTIVE, APPROVED bundle config (and, now, source-stream
/// bindings) from a READ-ONLY Postgres and hot-swaps both in/out with no
/// pod restart. Two independent reasons neither ever starts, both logged
/// and neither an error -- `DB_READER_PASSWORD` unset (the RO account
/// hasn't been provisioned yet in this environment) or
/// `BUNDLE_SCOPE_TENANT_ID` unset (`None` -- see `config::TenantScopeId`'s
/// own doc for why this is no longer collapsed onto `0`, a real, selectable
/// tenant). Either way, `try_start_process_loop`'s existing
/// `PROCESS_APP_ID`/`PROCESS_BUNDLE_*` env selection remains the sole
/// source; both loaders only supplement it once actually configured, and
/// are additionally gated per-tick on the `waddles.core.disable-db-bundle-config`
/// kill-switch (`crate::license::DbBundleConfigGate` -- already the
/// negated "is the DB path enabled" answer, enabled by default) inside
/// `bundle_loader::run_tick`/`source_supervisor::run_tick` regardless of
/// whether this function's own startup gates pass.
///
/// The source-binding supervisor has its own additional, independent
/// startup gates -- `ENVELOPE_BINDING_KEYS` (hop verification must never
/// silently fail open, same rule as `try_start_process_loop`) and
/// `penguin_spine::SpineConfig::from_env()` (Valkey connectivity). Missing
/// either disables ONLY the supervisor, logged the same way; bundle
/// load/unload (`bundle_loader::run`) needs neither and still starts.
fn try_start_db_bundle_loader(
    config: &config::Config,
    connections: Arc<host_api::ConnectionRegistry>,
    excluded_metric: prometheus::IntCounterVec,
    source_supervisor_metrics: telemetry::SourceBindingSupervisorMetrics,
) {
    let Some(password) = config.db_reader_password.as_ref() else {
        tracing::info!(
            "DB_READER_PASSWORD not set; DB-driven bundle loader/source-binding supervisor not started (env selection remains authoritative)"
        );
        return;
    };
    let Some(tenant_id) = config.cli.bundle_scope_tenant_id.get() else {
        tracing::info!(
            "BUNDLE_SCOPE_TENANT_ID not set; DB-driven bundle loader/source-binding supervisor not started (env selection remains authoritative)"
        );
        return;
    };

    let license_client = match license::build_license_client("waddles") {
        Ok(c) => c,
        Err(err) => {
            tracing::warn!(error = %err, "license client config invalid; DB-driven bundle loader/source-binding supervisor not started");
            return;
        }
    };
    let gate: Arc<dyn license::FeatureGate> =
        Arc::new(license::DbBundleConfigGate::new(license_client));

    let reader_cfg = bundle_active_set::ReaderConfig {
        host: config.cli.db_reader_host.clone(),
        port: config.cli.db_reader_port,
        name: config.cli.db_reader_name.clone(),
        user: config.cli.db_reader_user.clone(),
    };
    let password = password.expose().to_string();
    let community_id = config.cli.bundle_scope_community_id;
    let poll_interval = config.cli.bundle_config_poll_interval();
    let call_timeout_ms = config.cli.executor_call_timeout_ms;

    // The source-binding supervisor's own optional dependencies -- built
    // eagerly (no network I/O) so a missing/invalid one only disables the
    // supervisor half, never the bundle load/unload half above. Shares the
    // SAME `connections` registry as `bundle_loader::run`/the host-api
    // listener (`try_start_host_api`'s own registry, threaded through this
    // function's `connections` parameter) -- a per-consumer registry of its
    // own would never see the executor connection the listener actually
    // accepts. Tenant/community scope is deliberately NOT included here --
    // see [`SupervisorPrereqs`]'s own doc for why that half can only be
    // resolved once the RO reader connection exists.
    let supervisor_prereqs =
        build_source_supervisor_prereqs(config, Arc::clone(&connections), Arc::clone(&gate));

    // Shared, poll-refreshed `app_id -> (digest, app_versions.id)` snapshot
    // (`bundle_active_set::ActiveVersionSnapshot`) -- populated by
    // `bundle_loader::run_tick`'s own poll tick below (the SAME whole-scope
    // active-set read it already performs for the Load/Unload decision), so
    // every source-binding consumer's per-DELIVERY resolution
    // (`spine::handle_delivered`) always sees the CURRENT hot-swapped
    // version, never a value captured once at connect/startup time.
    let app_version_snapshot = bundle_active_set::ActiveVersionSnapshot::new();

    tokio::spawn(async move {
        let db = match bundle_active_set::reader::connect(&reader_cfg, &password).await {
            Ok(db) => db,
            Err(err) => {
                tracing::error!(error = %err, "db-reader connection failed; DB-driven bundle loader/source-binding supervisor not started");
                return;
            }
        };

        let (bundle_loader_shutdown_tx, bundle_loader_shutdown_rx) =
            tokio::sync::oneshot::channel();
        tokio::spawn(async move {
            shutdown_signal().await;
            let _ = bundle_loader_shutdown_tx.send(());
        });
        let bundle_loader_task = bundle_loader::run(
            db.clone(),
            tenant_id,
            community_id,
            poll_interval,
            call_timeout_ms,
            Arc::clone(&gate),
            connections,
            excluded_metric,
            bundle_loader_shutdown_rx,
            app_version_snapshot.clone(),
        );

        let Some(prereqs) = supervisor_prereqs else {
            bundle_loader_task.await;
            return;
        };
        // `kv` host capability for every source-binding consumer this
        // supervisor spawns -- opened once here (network I/O deliberately
        // kept out of `build_source_supervisor_prereqs`, see that
        // function's doc) and cloned into each consumer's `ProcessDeps`
        // (`source_supervisor::run_binding_consumer`).
        let kv_conn = connect_kv(&prereqs.spine_cfg).await;

        // Tenant-isolation fix: resolve the real tenant slug/community name
        // for THIS process's own numeric scope through the same RO reader
        // connection, rather than ever hardcoding a fixed scope -- see
        // `bundle_active_set::scope::resolve_scope`'s own fail-closed
        // contract. A resolution failure (missing row, cross-tenant
        // community id, or a query error) disables ONLY the supervisor;
        // bundle load/unload has no scope-string dependency and keeps
        // running.
        let resolved =
            match bundle_active_set::scope::resolve_scope(&db, tenant_id, community_id).await {
                Ok(Some(resolved)) => Some(resolved),
                Ok(None) => {
                    tracing::error!(
                    tenant_id,
                    community_id,
                    "tenant/community scope could not be resolved via the RO reader connection \
                     (missing row, or a community id belonging to a different tenant); \
                     source-binding supervisor not started (fail-closed -- never defaulting to a \
                     hardcoded scope)"
                );
                    None
                }
                Err(err) => {
                    tracing::error!(
                        error = %err,
                        tenant_id,
                        community_id,
                        "tenant/community scope resolution query failed; source-binding supervisor \
                         not started (fail-closed)"
                    );
                    None
                }
            };

        match resolved.map(|r| {
            // Production grant gate for the DB-driven supervisor path (spec
            // SS4/SS5): `PgGrantLoader` against the SAME RO-replica
            // connection `resolve_scope`/`bundle_loader::run` above already
            // opened, never the `AlwaysGrantedLoader`-over-
            // `InMemoryGrantLoader` stand-in this used to hardcode
            // unconditionally. Built only once resolution actually
            // succeeds (no point opening a redis client / spawning a
            // refresh loop for a supervisor that never starts).
            let supervisor_gate = grant_gate::build_production_gate(
                grant_gate::PgGrantLoader::new(db.clone()),
                build_redis_client(&prereqs.spine_cfg),
                poll_interval,
            );
            finish_supervisor_deps(
                prereqs,
                r,
                kv_conn,
                tenant_id,
                community_id,
                supervisor_gate,
                app_version_snapshot.clone(),
            )
        }) {
            Some(deps) => {
                let (supervisor_shutdown_tx, supervisor_shutdown_rx) =
                    tokio::sync::oneshot::channel();
                tokio::spawn(async move {
                    shutdown_signal().await;
                    let _ = supervisor_shutdown_tx.send(());
                });
                let spawner: Arc<dyn source_supervisor::ConsumerSupervisor> =
                    Arc::new(source_supervisor::SpineConsumerSupervisor {
                        deps: Arc::new(deps),
                    });
                let supervisor_task = source_supervisor::run(
                    db,
                    tenant_id,
                    community_id,
                    poll_interval,
                    gate,
                    spawner,
                    source_supervisor_metrics,
                    supervisor_shutdown_rx,
                );
                tokio::join!(bundle_loader_task, supervisor_task);
            }
            None => bundle_loader_task.await,
        }
    });
}

/// Everything the source-binding supervisor needs EXCEPT its tenant/
/// community scope -- deliberately split from [`source_supervisor::
/// SupervisorDeps`] because those two fields can only be resolved (
/// `bundle_active_set::scope::resolve_scope`) once the RO reader
/// connection exists, whereas everything else here is built eagerly, with
/// no network I/O, at startup. [`finish_supervisor_deps`] combines the two
/// halves once resolution succeeds.
struct SupervisorPrereqs {
    spine_cfg: penguin_spine::SpineConfig,
    key_ring: hop::KeyRing,
    connections: Arc<host_api::ConnectionRegistry>,
    call_timeout_ms: u64,
    approved_targets: std::collections::HashMap<String, String>,
    metrics: Arc<dyn penguin_spine::SpineMetrics>,
    license: Arc<dyn license::FeatureGate>,
}

/// Builds [`SupervisorPrereqs`] from `config`'s optional dependencies, or
/// `None` (logged) if either is unavailable -- split out of
/// [`try_start_db_bundle_loader`] so that function's own control flow reads
/// as "two independent startup gates, then dispatch" rather than nesting
/// the supervisor's setup inline. Never touches the network itself
/// (`penguin_spine::SpineConfig::from_env` only parses env vars).
fn build_source_supervisor_prereqs(
    config: &config::Config,
    connections: Arc<host_api::ConnectionRegistry>,
    gate: Arc<dyn license::FeatureGate>,
) -> Option<SupervisorPrereqs> {
    let Some(keys_raw) = config.envelope_binding_keys.as_ref() else {
        tracing::warn!(
            "ENVELOPE_BINDING_KEYS not set; source-binding supervisor disabled (hop verification must never fail open)"
        );
        return None;
    };
    let key_ring = match hop::KeyRing::parse(keys_raw.expose()) {
        Ok(r) => r,
        Err(err) => {
            tracing::warn!(error = %err, "ENVELOPE_BINDING_KEYS invalid; source-binding supervisor disabled");
            return None;
        }
    };
    let spine_cfg = match penguin_spine::SpineConfig::from_env() {
        Ok(c) => c,
        Err(err) => {
            tracing::warn!(error = %err, "spine config unavailable; source-binding supervisor disabled");
            return None;
        }
    };

    let approved_targets = builtins::parse_approved_targets(&config.cli.process_routes_to_approved);
    let metrics: Arc<dyn penguin_spine::SpineMetrics> = Arc::new(penguin_spine::NoopMetrics);

    Some(SupervisorPrereqs {
        spine_cfg,
        key_ring,
        connections,
        call_timeout_ms: config.cli.executor_call_timeout_ms,
        approved_targets,
        metrics,
        license: gate,
    })
}

/// Combines [`SupervisorPrereqs`] with a successfully-[`resolve_scope`]d
/// tenant slug/community name into the final [`source_supervisor::
/// SupervisorDeps`]. Pure (no I/O, no logging of its own -- the caller
/// already logged the resolution outcome) so the "does resolution feed
/// through correctly" contract is unit-testable without a live reader
/// connection.
///
/// [`resolve_scope`]: bundle_active_set::scope::resolve_scope
fn finish_supervisor_deps(
    prereqs: SupervisorPrereqs,
    resolved: bundle_active_set::scope::ResolvedScope,
    kv_conn: Option<redis::aio::MultiplexedConnection>,
    tenant_id: i32,
    community_id: i32,
    gate: Arc<bundle_capability_gate::CapabilityGate>,
    app_version_snapshot: bundle_active_set::ActiveVersionSnapshot,
) -> source_supervisor::SupervisorDeps {
    source_supervisor::SupervisorDeps {
        spine_cfg: prereqs.spine_cfg,
        key_ring: prereqs.key_ring,
        connections: prereqs.connections,
        call_timeout_ms: prereqs.call_timeout_ms,
        approved_targets: prereqs.approved_targets,
        metrics: prereqs.metrics,
        license: prereqs.license,
        tenant: resolved.tenant_slug,
        community: resolved.community_name,
        tenant_id,
        community_id,
        kv_conn,
        gate,
        app_version_snapshot,
    }
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
            db_reader_password: None,
        }
    }

    /// A deny-all gate -- these tests only assert on `SupervisorDeps`'s own
    /// plumbing (tenant/community/tenant_id/community_id passthrough), not
    /// on any particular authorize() outcome.
    fn test_gate() -> Arc<bundle_capability_gate::CapabilityGate> {
        Arc::new(bundle_capability_gate::CapabilityGate::new(
            Arc::new(bundle_capability_gate::InMemoryGrantSnapshot::new()),
            Arc::new(bundle_capability_gate::InMemoryMembership::new()),
            Arc::new(bundle_capability_gate::InMemoryQuotaLedger::new()),
        ))
    }

    /// A minimal, syntactically valid [`SupervisorPrereqs`] -- guarded by
    /// `ENV_LOCK` since `penguin_spine::SpineConfig::from_env` reads
    /// `VALKEY_URL`/`VALKEY_PASSWORD`, same rationale as
    /// `try_start_process_loop_spawns_when_spine_config_is_valid`'s
    /// identical setup.
    fn test_supervisor_prereqs() -> SupervisorPrereqs {
        let _guard = ENV_LOCK.lock().unwrap();
        // SAFETY: serialized by ENV_LOCK above.
        unsafe {
            std::env::set_var("VALKEY_URL", "rediss://127.0.0.1:1/");
            std::env::set_var("VALKEY_PASSWORD", "test-valkey-pass");
        }
        let spine_cfg = penguin_spine::SpineConfig::from_env().expect("valid spine config");
        // SAFETY: serialized by ENV_LOCK above.
        unsafe {
            std::env::remove_var("VALKEY_URL");
            std::env::remove_var("VALKEY_PASSWORD");
        }
        SupervisorPrereqs {
            spine_cfg,
            key_ring: crate::hop::KeyRing::new(vec![("k1".to_string(), vec![9u8; 32])]),
            connections: Arc::new(host_api::ConnectionRegistry::new()),
            call_timeout_ms: 2000,
            approved_targets: std::collections::HashMap::new(),
            metrics: Arc::new(penguin_spine::NoopMetrics),
            license: Arc::new(crate::license::test_support::FixedGate(true)),
        }
    }

    /// Tenant-isolation regression test, resolved half: `finish_supervisor_
    /// deps` must carry the DB-resolved tenant slug/community name through
    /// into `SupervisorDeps` verbatim -- this is what
    /// `source_supervisor::binding_grant` then renders into the actual
    /// Valkey stream key (see that module's own regression test).
    #[test]
    fn finish_supervisor_deps_uses_the_resolved_tenant_slug_and_community() {
        let prereqs = test_supervisor_prereqs();
        let resolved = bundle_active_set::scope::ResolvedScope {
            tenant_slug: "acme".to_string(),
            community_name: Some("main".to_string()),
        };
        let deps = finish_supervisor_deps(
            prereqs,
            resolved,
            None,
            7,
            3,
            test_gate(),
            bundle_active_set::ActiveVersionSnapshot::new(),
        );
        assert_eq!(deps.tenant, "acme");
        assert_eq!(deps.community.as_deref(), Some("main"));
        assert_eq!(deps.tenant_id, 7);
        assert_eq!(deps.community_id, 3);
    }

    /// Tenant-isolation regression test, fail-closed half ("unresolved ->
    /// DB path not started"): this is the exact `Option::map` expression
    /// `try_start_db_bundle_loader`'s spawned task runs against
    /// `bundle_active_set::scope::resolve_scope`'s own `None` result --
    /// proving a failed resolution can never produce a `SupervisorDeps`, so
    /// the `None` match arm (bundle load/unload only, no supervisor spawn)
    /// is the only path reachable.
    #[test]
    fn supervisor_deps_are_never_built_when_scope_resolution_fails() {
        let prereqs = test_supervisor_prereqs();
        let resolved: Option<bundle_active_set::scope::ResolvedScope> = None;
        let deps = resolved.map(|r| {
            finish_supervisor_deps(
                prereqs,
                r,
                None,
                7,
                3,
                test_gate(),
                bundle_active_set::ActiveVersionSnapshot::new(),
            )
        });
        assert!(
            deps.is_none(),
            "an unresolved scope must never produce SupervisorDeps -- the source-binding \
             supervisor must not start"
        );
    }

    #[test]
    fn db_path_selected_requires_both_config_present_and_gate_enabled() {
        assert!(
            db_path_selected(true, true),
            "config present + gate on -> DB path"
        );
        assert!(
            !db_path_selected(true, false),
            "kill-switch on (gate reports disabled) -> legacy path, even with config present"
        );
        assert!(
            !db_path_selected(false, true),
            "missing DB config -> legacy path, even with the gate enabled"
        );
        assert!(!db_path_selected(false, false));
    }

    /// Mutual-exclusion regression test, missing-config half: `DB_READER_
    /// PASSWORD`/`BUNDLE_SCOPE_TENANT_ID` absent must resolve to "legacy
    /// path" without even constructing a license client -- mirrors
    /// `try_start_db_bundle_loader_noop_when_db_reader_password_unset`'s
    /// identical config-presence check, now hoisted to the startup
    /// path-selection decision.
    #[tokio::test]
    async fn resolve_db_path_active_is_false_when_db_reader_password_unset() {
        let cli = CliConfig::parse_from(["svc-process", "--bundle-scope-tenant-id", "1"]);
        let mut config = test_config(cli);
        config.db_reader_password = None;
        assert!(!resolve_db_path_active(&config).await);
    }

    #[tokio::test]
    async fn resolve_db_path_active_is_false_when_tenant_id_unset() {
        let cli = CliConfig::parse_from(["svc-process"]);
        assert_eq!(cli.bundle_scope_tenant_id.get(), None);
        let mut config = test_config(cli);
        config.db_reader_password = Some(crate::config::Secret::new("real-ro-password"));
        assert!(!resolve_db_path_active(&config).await);
    }

    /// Mutual-exclusion regression test, DB-path-active half ("DB path
    /// active + PROCESS_APP_ID set -> legacy loop not started"): DB config
    /// fully present and the kill-switch flag never seen (this test's
    /// clean-env `license::build_license_client` call, same fail-closed-
    /// to-OFF cold-client contract every other license test in this crate
    /// relies on) must resolve `true` -- proving `run_with_shutdown`'s
    /// `if resolve_db_path_active(...).await` branch is the one taken, so
    /// `try_start_process_loop` (the legacy loop) is structurally never
    /// called for this config, regardless of `process_app_id` being set.
    /// The complementary "kill-switch ON -> legacy runs, supervisor not"
    /// half is `db_path_selected`'s own `false` cases above -- forcing a
    /// real `penguin_licensing::LicenseClient` to report the raw
    /// kill-switch flag ON requires a live PostHog/license server this
    /// crate's test suite deliberately never depends on.
    #[tokio::test]
    async fn resolve_db_path_active_is_true_when_db_config_present_and_kill_switch_unseen() {
        let cli = CliConfig::parse_from([
            "svc-process",
            "--bundle-scope-tenant-id",
            "1",
            "--process-app-id",
            "waddles.bot.commands.default",
        ]);
        let mut config = test_config(cli);
        config.db_reader_password = Some(crate::config::Secret::new("real-ro-password"));
        assert!(
            resolve_db_path_active(&config).await,
            "DB config present + never-seen kill-switch flag must select the DB-driven path"
        );
    }

    /// Security review fix regression test: `db_reader_password: None` (the
    /// value `config::Config::from_cli` now produces for both a genuinely
    /// unset `DB_READER_PASSWORD` and Helm's always-rendered-but-empty
    /// default) must take the documented no-op branch rather than
    /// attempting a DB connection. Mirrors `try_start_process_loop_noop_
    /// when_process_app_id_unset`'s style -- no OTel subscriber installed,
    /// so `tracing::info!` is a harmless no-op; success is simply that this
    /// returns without panicking or spawning a task that reaches the
    /// license-client/DB-connect code path.
    #[tokio::test]
    async fn try_start_db_bundle_loader_noop_when_db_reader_password_unset() {
        let cli = CliConfig::parse_from(["svc-process"]);
        let mut config = test_config(cli);
        config.db_reader_password = None;
        try_start_db_bundle_loader(
            &config,
            Arc::new(host_api::ConnectionRegistry::new()),
            test_excluded_metric(),
            test_source_supervisor_metrics(),
        );
    }

    /// Same no-op contract, the other independent startup gate:
    /// `BUNDLE_SCOPE_TENANT_ID` unset (`None`, `CliConfig`'s default) even
    /// with a real reader password present.
    #[tokio::test]
    async fn try_start_db_bundle_loader_noop_when_tenant_id_unset() {
        let cli = CliConfig::parse_from(["svc-process"]);
        assert_eq!(cli.bundle_scope_tenant_id.get(), None);
        let mut config = test_config(cli);
        config.db_reader_password = Some(crate::config::Secret::new("real-ro-password"));
        try_start_db_bundle_loader(
            &config,
            Arc::new(host_api::ConnectionRegistry::new()),
            test_excluded_metric(),
            test_source_supervisor_metrics(),
        );
    }

    /// Bug fix regression: tenant `0` is a real, legitimate tenant (not the
    /// "unset" sentinel the old bare-`i32` field collapsed it onto) and
    /// must clear this gate rather than being treated as not-configured.
    /// This only asserts the gate is cleared (no panic / hang from the
    /// synchronous portion of the function); the spawned task's own DB
    /// connection failure against an unreachable host is covered by
    /// `try_start_db_bundle_loader_noop_when_db_reader_password_unset`'s
    /// sibling contract, not re-asserted here.
    #[tokio::test]
    async fn try_start_db_bundle_loader_clears_tenant_gate_when_tenant_id_is_zero() {
        let cli = CliConfig::parse_from(["svc-process", "--bundle-scope-tenant-id", "0"]);
        assert_eq!(cli.bundle_scope_tenant_id.get(), Some(0));
        let mut config = test_config(cli);
        config.db_reader_password = Some(crate::config::Secret::new("real-ro-password"));
        try_start_db_bundle_loader(
            &config,
            Arc::new(host_api::ConnectionRegistry::new()),
            test_excluded_metric(),
            test_source_supervisor_metrics(),
        );
    }

    /// A standalone, unregistered `IntCounterVec` for
    /// `try_start_db_bundle_loader` tests -- see `bundle_loader::tests::
    /// test_metric`'s identical rationale (no `Registry` needed for
    /// `.inc()` to work correctly).
    fn test_excluded_metric() -> prometheus::IntCounterVec {
        prometheus::IntCounterVec::new(
            prometheus::Opts::new("test_bundle_active_set_excluded_total", "test"),
            &["app_id", "reason"],
        )
        .expect("valid metric definition")
    }

    /// A standalone, unregistered [`telemetry::SourceBindingSupervisorMetrics`]
    /// -- same rationale as [`test_excluded_metric`].
    fn test_source_supervisor_metrics() -> telemetry::SourceBindingSupervisorMetrics {
        telemetry::SourceBindingSupervisorMetrics {
            active_consumers: prometheus::IntGauge::new(
                "test_source_binding_consumers_active",
                "test",
            )
            .expect("valid metric definition"),
            consumer_transitions_total: prometheus::IntCounterVec::new(
                prometheus::Opts::new("test_source_binding_consumer_transitions_total", "test"),
                &["action"],
            )
            .expect("valid metric definition"),
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
            db_reader_password: None,
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
