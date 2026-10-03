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
//! - The `db`/`flags` host capabilities
//!   (`crate::capabilities::StageCapabilities`) -- `context`/`clock`/`log`,
//!   `kv` (shared `bundle_host_kv::KvHost`, PR #425), and `http` (shared
//!   `bundle_host_http::egress::EgressGuard`, PR #459 follow-up) are fully
//!   wired
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
pub mod changelog_consumer;
pub mod config;
pub mod error;
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
    // `crate::capabilities::StageCapabilities::egress`'s
    // `svc_process_egress_denied_total{app_id,reason}` -- registered here,
    // same "before `prom_registry` moves into `AppState::new`" constraint
    // as the two metrics above, then threaded into whichever of
    // `try_start_process_loop`/`try_start_changelog_consumer` actually
    // starts.
    let egress_denied_metric = telemetry::register_egress_metrics(&prom_registry);
    let changelog_consumer_metrics = telemetry::register_changelog_consumer_metrics(&prom_registry);
    // regression: drain loop exited on NOGROUP (alpha 2026-10-02)
    let drain_loop_metrics = telemetry::register_drain_loop_metrics(&prom_registry);
    // fix/executor-link-heartbeat: `host_api_connected_executors`/
    // `host_api_heartbeat_timeouts_total`/`dispatch_dead_lettered_no_executor_total`.
    let host_api_metrics = telemetry::register_host_api_metrics(&prom_registry);

    let connections = try_start_host_api(&config.cli, host_api_metrics);

    let state = http::AppState::new(config.clone(), prom_registry, Arc::clone(&connections));
    let consumer_loop_ready = Arc::clone(&state.consumer_loop_ready);
    // Mutual exclusion, resolved ONCE at startup -- see
    // `resolve_multi_tenant_path_decision`'s own doc for why this is not
    // re-evaluated per-tick for this specific dispatch decision (a live
    // kill-switch flip mid-run still stops DB-driven work via
    // `changelog_consumer::run`'s own per-tick gate check, it just doesn't
    // fail OVER to the legacy loop without a pod restart).
    let decision = resolve_multi_tenant_path_decision(&config).await;
    // Point (d) of the alpha fix (2026-10-02): the chart is removing the
    // legacy env entirely, so a pod with neither path available must not
    // quietly serve HTTP/metrics with no drain loop at all -- exit non-zero
    // so Kubernetes crashloops it into visibility instead.
    if no_data_plane_path_available(decision, &config.cli.process_app_id) {
        tracing::error!(
            ?decision,
            "no data-plane path available at startup: multi-tenant changelog-consumer path \
             inactive and PROCESS_APP_ID unset; exiting"
        );
        anyhow::bail!(
            "no data-plane path available at startup (multi-tenant path inactive, \
             PROCESS_APP_ID unset)"
        );
    }
    match decision {
        PathDecision::MultiTenant => {
            state
                .multi_tenant_consumer_configured
                .store(true, std::sync::atomic::Ordering::Relaxed);
            if !config.cli.process_app_id.is_empty() {
                tracing::info!(
                    process_app_id = %config.cli.process_app_id,
                    "startup path: multi-tenant changelog-consumer (DB_READER_PASSWORD \
                     configured, kill-switches enabled); ignoring legacy PROCESS_APP_ID/ \
                     PROCESS_INGEST_* env selection (restart required to fall back)"
                );
            } else {
                tracing::info!(
                    "startup path: multi-tenant changelog-consumer (DB_READER_PASSWORD \
                     configured, kill-switches enabled)"
                );
            }
            try_start_changelog_consumer(
                &config,
                connections,
                bundle_loader_excluded_metric,
                source_supervisor_metrics,
                egress_denied_metric,
                changelog_consumer_metrics,
                Arc::clone(&consumer_loop_ready),
            );
        }
        PathDecision::NoDbConfig => {
            tracing::info!(
                process_app_id = %config.cli.process_app_id,
                "startup path: legacy PROCESS_APP_ID/PROCESS_INGEST_* env selection \
                 (DB_READER_PASSWORD not configured)"
            );
            try_start_process_loop(
                &config,
                connections,
                egress_denied_metric,
                drain_loop_metrics,
                consumer_loop_ready,
            );
        }
        PathDecision::KillSwitchOn => {
            tracing::warn!(
                process_app_id = %config.cli.process_app_id,
                "startup path: legacy PROCESS_APP_ID/PROCESS_INGEST_* env selection \
                 (multi-tenant kill-switch is ON)"
            );
            try_start_process_loop(
                &config,
                connections,
                egress_denied_metric,
                drain_loop_metrics,
                consumer_loop_ready,
            );
        }
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
fn try_start_host_api(
    cli: &config::CliConfig,
    metrics: telemetry::HostApiMetrics,
) -> Arc<host_api::ConnectionRegistry> {
    let registry = Arc::new(host_api::ConnectionRegistry::new());
    registry.set_dead_letter_metric(metrics.dead_lettered_no_executor_total.clone());
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
        if let Err(err) = host_api::serve(
            cli,
            registry_for_task,
            fallback_capabilities,
            shutdown_rx,
            metrics,
        )
        .await
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
async fn connect_kv(cfg: &penguin_spine::SpineConfig) -> Option<redis::aio::MultiplexedConnection> {
    use redis::IntoConnectionInfo;

    let info: redis::ConnectionInfo = match cfg.valkey_url.as_str().into_connection_info() {
        Ok(info) => info,
        Err(err) => {
            tracing::warn!(error = %err, "kv capability: invalid VALKEY_URL; kv disabled (not_implemented on every kv host-call)");
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

    let client = if cfg.security_transport_tls {
        host_api::ensure_crypto_provider_installed();
        let root_cert = std::fs::read(&cfg.valkey_ca_file).ok();
        match redis::Client::build_with_tls(
            info,
            redis::TlsCertificates {
                client_tls: None,
                root_cert,
            },
        ) {
            Ok(client) => client,
            Err(err) => {
                tracing::warn!(error = %err, "kv capability: TLS Valkey client build failed; kv disabled");
                return None;
            }
        }
    } else {
        match redis::Client::open(info) {
            Ok(client) => client,
            Err(err) => {
                tracing::warn!(error = %err, "kv capability: Valkey client build failed; kv disabled");
                return None;
            }
        }
    };

    match client.get_multiplexed_async_connection().await {
        Ok(mut conn) => {
            // Low-severity fix, security review of PR #425: `count_key`
            // has no TTL, so an `allkeys-*` `maxmemory-policy` can evict it
            // under memory pressure, silently resetting the kv quota --
            // checked once here, never on the per-op hot path
            // (`bundle_host_kv::policy`'s doc).
            let policy_check = bundle_host_kv::policy::check_maxmemory_policy(&mut conn).await;
            bundle_host_kv::policy::log_and_record(&policy_check);
            Some(conn)
        }
        Err(err) => {
            tracing::warn!(error = %err, "kv capability: Valkey connection failed; kv disabled (not_implemented on every kv host-call)");
            None
        }
    }
}

/// Builds the `http` bundle capability's shared
/// [`bundle_host_http::egress::EgressGuard`], wired with the cluster CIDR
/// denylist and instance-wide private-IP egress policy
/// (`cli.cluster_cidr_denylist()`/`cli.instance_egress_policy()`) -- shared
/// by [`try_start_process_loop`] and [`try_start_changelog_consumer`] (this
/// crate's two mutually-exclusive startup paths), pulled out so both can be
/// exercised directly in tests without a live Valkey/Postgres connection.
/// See `mod tests`'s `process_egress_guard_denies_cluster_cidr_even_with_grant_and_policy_allow`
/// regression test for #425's dropped-denylist bug this guards against.
///
/// `None` mirrors both callers' own fail-closed posture: a denylist
/// re-parse failure (should be impossible, since `CliConfig::validate`
/// already parsed it successfully at `Config::load` time) disables the
/// caller rather than starting with a silently-empty denylist.
fn build_process_egress_guard(
    cli: &config::CliConfig,
    catalog: Arc<capabilities::HttpEgressCatalog>,
    egress_denied_metric: prometheus::IntCounterVec,
    bundle_egress_flag: Arc<dyn bundle_host_http::egress::FeatureFlag>,
) -> Option<Arc<bundle_host_http::egress::EgressGuard>> {
    let cluster_denylist = match cli.cluster_cidr_denylist() {
        Ok(denylist) => denylist,
        Err(err) => {
            tracing::warn!(error = %err, "cluster CIDR denylist re-parse failed after startup validation passed; egress guard not built");
            return None;
        }
    };
    Some(Arc::new(
        bundle_host_http::egress::EgressGuard::new(
            Arc::new(bundle_host_http::egress::ReqwestTransport::new()),
            bundle_host_http::egress::EgressLimits {
                allow_private_hosts: false,
                rate_limit_rps: 10,
                rate_limit_burst: 20,
                timeout: std::time::Duration::from_secs(10),
                max_redirects: 3,
                max_response_bytes: 1_048_576,
                allowed_ports: vec![443],
                proxy_url: None,
            },
            catalog,
            egress_denied_metric,
            bundle_egress_flag,
        )
        .with_instance_policy(Arc::new(std::sync::RwLock::new(
            cli.instance_egress_policy(),
        )))
        .with_cluster_denylist(cluster_denylist),
    ))
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
fn try_start_process_loop(
    config: &config::Config,
    connections: Arc<host_api::ConnectionRegistry>,
    egress_denied_metric: prometheus::IntCounterVec,
    drain_loop_metrics: telemetry::DrainLoopMetrics,
    consumer_loop_ready: Arc<std::sync::atomic::AtomicBool>,
) {
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

    // The `http` bundle capability's shared egress guard (`crate::
    // capabilities::StageCapabilities::egress`) -- one per process, built
    // once here and cloned into every per-invoke `StageCapabilities` (see
    // that struct's doc). `HttpEgressCatalog::new()` starts empty: no
    // writer populates it yet (`crate::capabilities::HttpEgressCatalog`'s
    // doc, the same honest gap this module's own doc already documents
    // for `db`/`kv`/`flags`), so every `app_id` is undeclared and `http`
    // denies `host_not_declared` until a future DB-driven loader wires
    // real manifest data in. Reuses this same `license_client`-derived
    // gate for `waddles.core.bundle-egress` -- see `license::
    // BundleEgressFlag`.
    let bundle_egress_flag: Arc<dyn bundle_host_http::egress::FeatureFlag> =
        match license::build_license_client("waddles") {
            Ok(c) => bundle_host_http::egress::boxed(license::BundleEgressFlag::new(c)),
            // Fail-closed: no client at all is the same "disable, don't
            // start unverified" posture every other startup-config gate in
            // this function already takes.
            Err(_) => bundle_host_http::egress::boxed(bundle_host_http::egress::StaticFlag(false)),
        };
    let Some(egress) = build_process_egress_guard(
        &config.cli,
        capabilities::HttpEgressCatalog::new(),
        egress_denied_metric,
        bundle_egress_flag,
    ) else {
        tracing::warn!("cluster CIDR denylist re-parse failed after startup validation passed; process loop disabled");
        return;
    };

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
        // `kv` host capability: opened once here, cloned into every
        // per-invoke `StageCapabilities` (`spine::ProcessDeps::kv_conn`'s
        // doc) rather than reopened per invoke. `None` on failure is not
        // fatal to the process loop -- every `kv` host-call then sees
        // `not_implemented` instead (`connect_kv`'s doc).
        let kv_conn = connect_kv(&spine_cfg).await;

        let (outer_shutdown_tx, mut outer_shutdown_rx) = tokio::sync::oneshot::channel();
        tokio::spawn(async move {
            shutdown_signal().await;
            let _ = outer_shutdown_tx.send(());
        });

        // Capped exponential backoff between (re)connect attempts, same cap
        // as `crate::source_supervisor`'s own retry loop -- never a one-shot
        // connect/drain. Self-heals NOGROUP by (re)provisioning every
        // granted stream's consumer group before each attempt: on a fresh
        // Valkey nothing else in this legacy, env-driven path ever creates
        // it. regression: drain loop exited on NOGROUP (alpha 2026-10-02)
        const BACKOFF_MAX: Duration = Duration::from_secs(30);
        let mut attempt: u32 = 0;
        loop {
            attempt += 1;
            drain_loop_metrics
                .spine_connect_attempts_total
                .with_label_values(&["legacy"])
                .inc();

            for grant in &grants {
                match spine::ensure_consumer_group(&spine_cfg, &grant.stream, &app_id).await {
                    Ok(true) => {
                        drain_loop_metrics
                            .consumer_group_created_total
                            .with_label_values(&["legacy"])
                            .inc();
                        tracing::info!(stream = %grant.stream, group = %app_id, "consumer group created");
                    }
                    Ok(false) => {}
                    Err(err) => {
                        tracing::warn!(stream = %grant.stream, group = %app_id, error = %err, "ensure consumer group failed, will retry");
                    }
                }
            }

            let spine_client = match penguin_spine::SpineClient::connect(
                spine_cfg.clone(),
                metrics.clone(),
            )
            .await
            {
                Ok(c) => c,
                Err(err) => {
                    consumer_loop_ready.store(false, std::sync::atomic::Ordering::Relaxed);
                    drain_loop_metrics
                        .consumer_loop_running
                        .with_label_values(&["legacy"])
                        .set(0);
                    tracing::error!(error = %err, attempt, "spine client connect failed, retrying");
                    if wait_or_shutdown(
                        &mut outer_shutdown_rx,
                        backoff_for_attempt(attempt, BACKOFF_MAX),
                    )
                    .await
                    {
                        return;
                    }
                    continue;
                }
            };

            let deps = spine::ProcessDeps {
                app_id: app_id.clone(),
                digest: cli.process_bundle_digest.clone(),
                version: cli.process_bundle_version.clone(),
                component_key: cli.process_bundle_component_key.clone(),
                sidecar_key: cli.process_bundle_sidecar_key.clone(),
                key_ring: key_ring.clone(),
                connections: Arc::clone(&connections),
                call_timeout_ms: cli.executor_call_timeout_ms,
                load_state: Arc::new(spine::LoadState::new()),
                approved_targets: approved_targets.clone(),
                consumer_id: spine_cfg.consumer_id.clone(),
                spine: spine_client,
                metrics: metrics.clone(),
                license: Arc::clone(&license_gate),
                kv_conn: kv_conn.clone(),
                // The legacy, single-bundle, env-driven path has no
                // active-set snapshot at all (no DB row, no consent record)
                // -- `kv` denies by default here, always
                // (`bundle_host_kv::authorize`'s own module doc: "undeclared
                // means denied").
                kv_capabilities: Arc::new(bundle_host_kv::CapabilitySnapshot::new()),
                egress: Arc::clone(&egress),
            };

            let (inner_tx, inner_rx) = tokio::sync::oneshot::channel();
            let run_fut = spine::run(spine_cfg.clone(), grants.clone(), deps, inner_rx);
            tokio::pin!(run_fut);

            consumer_loop_ready.store(true, std::sync::atomic::Ordering::Relaxed);
            drain_loop_metrics
                .consumer_loop_running
                .with_label_values(&["legacy"])
                .set(1);

            tokio::select! {
                _ = &mut outer_shutdown_rx => {
                    let _ = inner_tx.send(());
                    let _ = run_fut.await;
                    consumer_loop_ready.store(false, std::sync::atomic::Ordering::Relaxed);
                    drain_loop_metrics
                        .consumer_loop_running
                        .with_label_values(&["legacy"])
                        .set(0);
                    return;
                }
                result = &mut run_fut => {
                    consumer_loop_ready.store(false, std::sync::atomic::Ordering::Relaxed);
                    drain_loop_metrics
                        .consumer_loop_running
                        .with_label_values(&["legacy"])
                        .set(0);
                    match result {
                        // Only reachable via the shutdown branch above in
                        // practice (`spine::run` returns `Ok(())` only when
                        // its own `shutdown` receiver resolves).
                        Ok(()) => return,
                        Err(err) if spine::is_nogroup_error(&err) => {
                            tracing::warn!(attempt, "process-stage drain loop: consumer group not yet provisioned (NOGROUP), self-healing and retrying");
                        }
                        Err(err) => {
                            tracing::error!(error = %err, attempt, "process-stage drain loop exited, retrying");
                        }
                    }
                    if wait_or_shutdown(&mut outer_shutdown_rx, backoff_for_attempt(attempt, BACKOFF_MAX)).await {
                        return;
                    }
                }
            }
        }
    });
}

/// Capped exponential backoff: 1s, 2s, 4s, 8s, 16s, then `max` thereafter.
/// Shared by [`try_start_process_loop`]'s connect/self-heal retry loop.
fn backoff_for_attempt(attempt: u32, max: Duration) -> Duration {
    let secs = 1u64
        .checked_shl(attempt.saturating_sub(1).min(16))
        .unwrap_or(u64::MAX);
    Duration::from_secs(secs).min(max)
}

/// Sleeps for `dur`, or returns early (reporting `true`) if `shutdown`
/// resolves first -- same shape as `crate::source_supervisor::
/// wait_or_shutdown`, duplicated here since that one is private to its own
/// module (the two retry loops share no other state).
async fn wait_or_shutdown(
    shutdown: &mut tokio::sync::oneshot::Receiver<()>,
    dur: Duration,
) -> bool {
    tokio::select! {
        _ = shutdown => true,
        () = tokio::time::sleep(dur) => false,
    }
}

/// Resolves, ONCE at startup, whether the multi-tenant, change-log-driven
/// active-set path (`crate::changelog_consumer`) or the legacy
/// `PROCESS_APP_ID`/`PROCESS_INGEST_*` single-consumer env path
/// (`try_start_process_loop`) is authoritative for this process's entire
/// lifetime -- **mutual exclusion, not operator discipline**: exactly one
/// of the two ever starts, regardless of what `PROCESS_APP_ID` happens to
/// be set to.
///
/// Deliberately evaluated only here, once, rather than per-tick the way
/// `changelog_consumer::run_incremental_tick`/`run_full_reconcile` re-check
/// their own kill-switch gate on every poll: a live kill-switch flip
/// mid-run is still caught by that per-tick gate (DB-driven load/
/// consumption stops immediately, source-binding consumers are stopped),
/// but this function's own path-selection decision does NOT re-run --
/// falling back to (or away from) the legacy loop requires a pod restart.
/// That is an accepted tradeoff (mirrors the pre-multi-tenant version of
/// this same decision): it guarantees the two paths can never run
/// concurrently against the same stream/group, which a live re-evaluation
/// racing against already-spawned consumer tasks could not cleanly
/// guarantee.
///
/// Outcome of [`resolve_multi_tenant_path_decision`] -- carries *why*, not
/// just the boolean choice, so `run_with_shutdown`'s own startup log line
/// states the reason (point (e) of the alpha fix, 2026-10-02:
/// `rules/critical-rules.md` Observability -- "log at INFO which path was
/// chosen and why").
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum PathDecision {
    /// `DB_READER_PASSWORD` configured and both kill-switch gates resolved
    /// enabled -- including the fail-open case where the license client
    /// itself couldn't be built (see this function's own doc).
    MultiTenant,
    /// `DB_READER_PASSWORD` unset/empty -- no DB path exists to select,
    /// regardless of kill-switch state.
    NoDbConfig,
    /// `DB_READER_PASSWORD` configured, but a kill-switch gate resolved
    /// genuinely disabled (a real, successfully-fetched flag value -- not
    /// an error, not unseen).
    KillSwitchOn,
}

/// The multi-tenant path is active when `DB_READER_PASSWORD` is set (no
/// more `BUNDLE_SCOPE_TENANT_ID`/`BUNDLE_SCOPE_COMMUNITY_ID` gate -- this
/// path serves EVERY tenant/community it finds, never a single configured
/// scope) AND BOTH kill-switch gates report enabled (`license::
/// DbBundleConfigGate` and `license::MultiTenantWatermarkGate`, each
/// already the negated "is this path enabled" answer -- default `true`
/// when unseen or the license server is unreachable).
///
/// **Regression fix (alpha 2026-10-02):** a malformed `LICENSE_SERVER_URL`/
/// `POSTHOG_HOST` (the only way `license::build_license_client` itself can
/// fail) used to be treated as "path inactive", silently forcing the
/// legacy loop even with `DB_READER_PASSWORD` fully configured -- the
/// *opposite* of this module's own documented fail-open contract ("unseen/
/// unreachable defaults to enabled") and inconsistent with
/// `core/svc_action`'s equivalent `flags::db_bundle_config_flag`/
/// `multi_tenant_watermark_flag`, which already default to enabled on a
/// `None` license client. A license-client build failure now resolves the
/// SAME way an unreachable license server already does: kill-switch state
/// unknown, defaulting to enabled, logged at WARN rather than silently
/// flipping the startup decision.
async fn resolve_multi_tenant_path_decision(config: &config::Config) -> PathDecision {
    if config.db_reader_password.is_none() {
        return PathDecision::NoDbConfig;
    }

    let gate_enabled = match license::build_license_client("waddles") {
        Ok(client) => {
            let db_bundle_config_enabled = license::DbBundleConfigGate::new(Arc::clone(&client))
                .enabled()
                .await;
            let multi_tenant_enabled = license::MultiTenantWatermarkGate::new(client)
                .enabled()
                .await;
            if !db_bundle_config_enabled || !multi_tenant_enabled {
                tracing::warn!(
                    disable_db_bundle_config_active = !db_bundle_config_enabled,
                    disable_multi_tenant_watermark_active = !multi_tenant_enabled,
                    "multi-tenant kill-switch is ON; falling back to legacy \
                     PROCESS_APP_ID/PROCESS_INGEST_* env selection"
                );
            }
            db_bundle_config_enabled && multi_tenant_enabled
        }
        Err(err) => {
            tracing::warn!(
                error = %err,
                "license client config invalid at startup; multi-tenant kill-switch state \
                 unknown, defaulting to DB-driven path ENABLED (fail-open, same contract as an \
                 unreachable license server)"
            );
            true
        }
    };

    if multi_tenant_path_selected(true, gate_enabled) {
        PathDecision::MultiTenant
    } else {
        PathDecision::KillSwitchOn
    }
}

/// Pure boolean combination behind [`resolve_multi_tenant_path_decision`] --
/// split out so the "which path wins" decision is directly unit-testable
/// with a fixed kill-switch-gate value, without needing a live/mocked
/// `penguin_licensing::LicenseClient` round trip to force a "kill-switch
/// ON" flag value (not achievable in a unit test against the real client).
fn multi_tenant_path_selected(db_config_present: bool, gate_enabled: bool) -> bool {
    db_config_present && gate_enabled
}

/// Point (d) of the alpha fix (2026-10-02): true when this pod has no
/// usable data-plane path at startup at all -- the multi-tenant path
/// didn't select, AND the legacy `PROCESS_APP_ID` override is also unset.
/// Pure and standalone so it's directly unit-testable without exercising
/// `run_with_shutdown`'s full bind/serve/shutdown machinery.
fn no_data_plane_path_available(decision: PathDecision, process_app_id: &str) -> bool {
    !matches!(decision, PathDecision::MultiTenant) && process_app_id.is_empty()
}

/// Attempts to start the multi-tenant, change-log-driven active-bundle
/// loader + source-binding supervisor (`crate::changelog_consumer::run`) --
/// dataplane scale design rev 4, §7/§8 step 2. One independent reason this
/// never starts, logged and not an error -- `DB_READER_PASSWORD` unset
/// (the RO account hasn't been provisioned yet in this environment).
/// Either way, `try_start_process_loop`'s existing `PROCESS_APP_ID`/
/// `PROCESS_BUNDLE_*` env selection remains the sole source, and this path
/// is additionally gated per-tick on both `waddles.core.
/// disable-db-bundle-config` and `waddles.core.disable-multi-tenant-
/// watermark` (each already the negated "is this path enabled" answer,
/// enabled by default) inside `changelog_consumer::run` regardless of
/// whether this function's own startup gate passes.
///
/// The source-binding supervisor half has its own additional, independent
/// startup gates -- `ENVELOPE_BINDING_KEYS` (hop verification must never
/// silently fail open, same rule as `try_start_process_loop`) and
/// `penguin_spine::SpineConfig::from_env()` (Valkey connectivity). Missing
/// either disables ONLY the supervisor (`changelog_consumer::run` is
/// called with `spawner: None`); bundle load/unload needs neither and
/// still starts.
fn try_start_changelog_consumer(
    config: &config::Config,
    connections: Arc<host_api::ConnectionRegistry>,
    excluded_metric: prometheus::IntCounterVec,
    source_supervisor_metrics: telemetry::SourceBindingSupervisorMetrics,
    egress_denied_metric: prometheus::IntCounterVec,
    changelog_consumer_metrics: telemetry::ChangelogConsumerMetrics,
    consumer_loop_ready: Arc<std::sync::atomic::AtomicBool>,
) {
    let Some(password) = config.db_reader_password.as_ref() else {
        tracing::info!(
            "DB_READER_PASSWORD not set; multi-tenant changelog consumer not started (env selection remains authoritative)"
        );
        return;
    };

    // Regression fix (alpha 2026-10-02, see `resolve_multi_tenant_path_decision`'s
    // doc for the full rationale): a license-client build failure here must
    // fail OPEN (kill-switch state unknown, both gates default enabled) --
    // `run_with_shutdown` already selected this path via that same
    // fail-open contract, so silently bailing out here on a second,
    // independent build attempt would contradict the very decision that
    // routed execution to this function in the first place.
    let (gate, bundle_egress_flag): (
        Arc<dyn license::FeatureGate>,
        Arc<dyn bundle_host_http::egress::FeatureFlag>,
    ) = match license::build_license_client("waddles") {
        Ok(license_client) => (
            Arc::new(license::AllGate(vec![
                Arc::new(license::DbBundleConfigGate::new(Arc::clone(
                    &license_client,
                ))),
                Arc::new(license::MultiTenantWatermarkGate::new(Arc::clone(
                    &license_client,
                ))),
            ])),
            bundle_host_http::egress::boxed(license::BundleEgressFlag::new(license_client)),
        ),
        Err(err) => {
            tracing::warn!(
                error = %err,
                "license client config invalid; multi-tenant kill-switch state unknown, \
                 defaulting to ENABLED (fail-open, DB_READER_PASSWORD already configured) -- \
                 bundle-egress capability denied until a valid license config is set"
            );
            (
                Arc::new(license::AllGate(Vec::new())),
                bundle_host_http::egress::boxed(bundle_host_http::egress::StaticFlag(false)),
            )
        }
    };
    // Same `bundle_host_http::egress::EgressGuard` wiring as
    // `try_start_process_loop` (this crate's other, mutually-exclusive
    // startup path) -- see that function's own doc for the deny-by-default
    // `HttpEgressCatalog` seam.
    let Some(egress) = build_process_egress_guard(
        &config.cli,
        capabilities::HttpEgressCatalog::new(),
        egress_denied_metric,
        bundle_egress_flag,
    ) else {
        tracing::warn!("cluster CIDR denylist re-parse failed after startup validation passed; DB-driven bundle loader/source-binding supervisor not started");
        return;
    };

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

    // supervisor half, never the bundle load/unload half. Unlike the
    // pre-multi-tenant version, tenant/community scope is no longer part
    // of this prereq (resolved per-scope, inside `changelog_consumer`, not
    // once per whole supervisor instance). Shares the SAME `connections`
    // registry as the host-api listener (`try_start_host_api`'s own
    // registry, threaded through this function's `connections` parameter).
    //
    // `kv_capabilities`: shared between `changelog_consumer::run`'s
    // DB-driven poll (writer -- every tick's `ActiveBundleRow::
    // declared_capabilities`) and every source-binding consumer's own
    // per-invoke `StageCapabilities` (reader, `bundle_host_kv::authorize::
    // authorize_kv`) -- one snapshot, one writer, many readers, mirroring
    // `core/svc_action`'s identical pattern. `kv_conn` is left `None` here
    // (opened async, once, inside the spawned task below -- this function
    // stays synchronous/no-I/O per its own doc) and filled in there before
    // the spawner is actually constructed.
    let kv_capabilities = Arc::new(bundle_host_kv::CapabilitySnapshot::new());
    let supervisor_deps = build_source_supervisor_deps(
        config,
        Arc::clone(&connections),
        Arc::clone(&gate),
        Arc::clone(&kv_capabilities),
        Arc::clone(&egress),
    );

    // Fail loud, never silent (user requirement): this path is only ever
    // entered once `PathDecision::MultiTenant` is selected, so readiness
    // must gate on it from the very first instant, not just once
    // `changelog_consumer::run` reaches its own retry loop.
    consumer_loop_ready.store(false, std::sync::atomic::Ordering::Relaxed);
    tokio::spawn(async move {
        let db = match bundle_active_set::reader::connect(&reader_cfg, &password).await {
            Ok(db) => db,
            Err(err) => {
                // Point (c) of the alpha fix (2026-10-02): this path was
                // SELECTED (`DB_READER_PASSWORD` configured, kill-switches
                // enabled) -- a connect/auth/query failure here must fail
                // loud (crashloop) rather than silently leaving the pod
                // running with no drain loop and no indication why.
                tracing::error!(
                    error = %err,
                    "db-reader connection failed for the selected multi-tenant \
                     changelog-consumer path; exiting rather than silently falling back"
                );
                std::process::exit(1);
            }
        };

        let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel();
        tokio::spawn(async move {
            shutdown_signal().await;
            let _ = shutdown_tx.send(());
        });
        // `kv` host capability connection for every source-binding consumer
        // this supervisor spawns -- opened once here (network I/O
        // deliberately kept out of `build_source_supervisor_deps`, see that
        // function's doc) and cloned into each consumer's `ProcessDeps`
        // (`source_supervisor::run_binding_consumer`). Built inside this
        // spawned task, not before it, purely because opening it is async
        // and `try_start_changelog_consumer` itself stays synchronous.
        let spawner: Option<Arc<dyn source_supervisor::ConsumerSupervisor>> = match supervisor_deps
        {
            Some(mut deps) => {
                deps.kv_conn = connect_kv(&deps.spine_cfg).await;
                Some(Arc::new(source_supervisor::SpineConsumerSupervisor {
                    deps: Arc::new(deps),
                })
                    as Arc<dyn source_supervisor::ConsumerSupervisor>)
            }
            None => None,
        };

        changelog_consumer::run(
            db,
            poll_interval,
            full_reconcile_interval,
            call_timeout_ms,
            gate,
            connections,
            spawner,
            excluded_metric,
            kv_capabilities,
            source_supervisor_metrics,
            changelog_consumer_metrics,
            consumer_loop_ready,
            shutdown_rx,
        )
        .await;
    });
}

/// Everything the source-binding supervisor needs -- built eagerly, with no
/// network I/O, at startup. `None` (logged) disables ONLY the supervisor
/// half; bundle load/unload has no dependency on any of this. Unlike the
/// pre-multi-tenant version's `SupervisorPrereqs`, this is the FULL
/// `source_supervisor::SupervisorDeps` already (no tenant/community field
/// left to resolve afterward -- see `crate::source_supervisor`'s own
/// module doc for why that moved to per-scope resolution inside
/// `crate::changelog_consumer`). `egress` is cloned into every spawned
/// binding consumer's own `ProcessDeps` -- see `crate::spine::
/// ProcessDeps::egress`'s doc.
fn build_source_supervisor_deps(
    config: &config::Config,
    connections: Arc<host_api::ConnectionRegistry>,
    gate: Arc<dyn license::FeatureGate>,
    kv_capabilities: Arc<bundle_host_kv::CapabilitySnapshot>,
    egress: Arc<bundle_host_http::egress::EgressGuard>,
) -> Option<source_supervisor::SupervisorDeps> {
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

    Some(source_supervisor::SupervisorDeps {
        spine_cfg,
        key_ring,
        connections,
        call_timeout_ms: config.cli.executor_call_timeout_ms,
        approved_targets,
        metrics,
        license: gate,
        // Opened async, once, inside `try_start_changelog_consumer`'s
        // spawned task (this function stays synchronous/no-I/O, per its own
        // doc) -- filled in there before the spawner is actually
        // constructed.
        kv_conn: None,
        kv_capabilities,
        egress,
    })
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
            // this single test. `--process-app-id` is required since point
            // (d) of the alpha fix (2026-10-02): a config with neither the
            // DB path (`DB_READER_PASSWORD` unset here) nor a legacy
            // `PROCESS_APP_ID` now fails `run_with_shutdown` fast, which
            // would otherwise make this bind/serve/shutdown test fail for
            // an unrelated reason.
            let cli = CliConfig::parse_from([
                "svc-process",
                "--http-port",
                "18291",
                "--metrics-port",
                "18292",
                "--process-app-id",
                "waddles.test.binds-serves-and-stops",
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

    /// `build_source_supervisor_deps`'s missing-`ENVELOPE_BINDING_KEYS`
    /// fail-closed branch: hop verification must never silently fail open,
    /// so a missing keyring disables ONLY the supervisor half (`None`),
    /// never the bundle-loader half.
    #[test]
    fn build_source_supervisor_deps_is_none_without_envelope_binding_keys() {
        let cli = CliConfig::parse_from(["svc-process"]);
        let mut config = test_config(cli);
        config.envelope_binding_keys = None;
        let gate: Arc<dyn license::FeatureGate> = Arc::new(license::test_support::FixedGate(true));
        assert!(build_source_supervisor_deps(
            &config,
            Arc::new(host_api::ConnectionRegistry::new()),
            gate,
            Arc::new(bundle_host_kv::CapabilitySnapshot::new()),
            test_egress_guard(),
        )
        .is_none());
    }

    /// The happy path: valid `ENVELOPE_BINDING_KEYS` + reachable spine
    /// config produces `Some(SupervisorDeps)`.
    #[test]
    fn build_source_supervisor_deps_is_some_with_valid_config() {
        let _guard = ENV_LOCK.lock().unwrap();
        // SAFETY: serialized by ENV_LOCK above.
        unsafe {
            std::env::set_var("VALKEY_URL", "rediss://127.0.0.1:1/");
            std::env::set_var("VALKEY_PASSWORD", "test-valkey-pass");
        }
        let cli = CliConfig::parse_from(["svc-process"]);
        let config = test_config(cli);
        let gate: Arc<dyn license::FeatureGate> = Arc::new(license::test_support::FixedGate(true));
        let deps = build_source_supervisor_deps(
            &config,
            Arc::new(host_api::ConnectionRegistry::new()),
            gate,
            Arc::new(bundle_host_kv::CapabilitySnapshot::new()),
            test_egress_guard(),
        );
        // SAFETY: serialized by ENV_LOCK above.
        unsafe {
            std::env::remove_var("VALKEY_URL");
            std::env::remove_var("VALKEY_PASSWORD");
        }
        assert!(deps.is_some());
    }

    /// A minimal, syntactically valid [`bundle_host_http::egress::
    /// EgressGuard`] for [`build_source_supervisor_deps`] tests -- same
    /// shape as `try_start_process_loop`'s own internally-built guard, just
    /// pre-built here since `build_source_supervisor_deps` takes it as a
    /// parameter rather than constructing its own.
    fn test_egress_guard() -> Arc<bundle_host_http::egress::EgressGuard> {
        Arc::new(bundle_host_http::egress::EgressGuard::new(
            Arc::new(bundle_host_http::egress::ReqwestTransport::new()),
            bundle_host_http::egress::EgressLimits {
                allow_private_hosts: false,
                rate_limit_rps: 10,
                rate_limit_burst: 20,
                timeout: std::time::Duration::from_secs(5),
                max_redirects: 3,
                max_response_bytes: 1_048_576,
                allowed_ports: vec![443],
                proxy_url: None,
            },
            capabilities::HttpEgressCatalog::new(),
            test_egress_denied_metric(),
            bundle_host_http::egress::boxed(bundle_host_http::egress::StaticFlag(true)),
        ))
    }

    // regression: #425 dropped cluster denylist -- both tests below go
    // through `build_process_egress_guard`, the exact function both
    // `try_start_process_loop` and `try_start_changelog_consumer` (the
    // production wiring) call, so a future merge that silently drops the
    // `.with_cluster_denylist()`/`.with_instance_policy()` calls fails
    // these tests, not just the shared `bundle_host_http::egress` crate's
    // own generic guard suite (which would keep passing even if this crate
    // stopped wiring the guard up at all).

    /// The cluster CIDR denylist must win even when a `PrivateIp` grant
    /// covers the address AND the instance policy has opted into private-IP
    /// egress -- proves `build_process_egress_guard` actually threads
    /// `cli.cluster_cidr_denylist()` into the guard via
    /// `.with_cluster_denylist()`, not just that the shared crate supports
    /// it.
    #[tokio::test]
    async fn process_egress_guard_denies_cluster_cidr_even_with_grant_and_policy_allow() {
        let cli = CliConfig::parse_from([
            "svc-process",
            "--deployment-tier",
            "production",
            "--egress-cluster-cidr-denylist",
            "10.244.0.0/16",
            "--instance-egress-allow-private-ip",
        ]);
        cli.validate().expect("populated denylist passes");
        let catalog = capabilities::HttpEgressCatalog::new();
        catalog.update(
            "waddles.a.b.c",
            bundle_host_http::egress::EgressRuleRow {
                private_ip_grants: vec![("10.244.5.6".to_string(), vec!["GET".to_string()])],
                ..Default::default()
            },
        );
        let guard = build_process_egress_guard(
            &cli,
            catalog,
            test_egress_denied_metric(),
            bundle_host_http::egress::boxed(bundle_host_http::egress::StaticFlag(true)),
        )
        .expect("valid config produces a guard");
        let err = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "GET", "url": "https://10.244.5.6/"}),
            )
            .await
            .expect_err("cluster CIDR denylist must deny despite grant + policy allow");
        assert_eq!(err.code, "ssrf_blocked_address");
    }

    /// The instance-wide private-IP policy denies a `PrivateIp` grant by
    /// default (no cluster CIDR involved) -- proves `build_process_egress_
    /// guard` actually threads `cli.instance_egress_policy()` into the
    /// guard via `.with_instance_policy()`.
    #[tokio::test]
    async fn process_egress_guard_denies_private_ip_grant_when_instance_policy_is_default_deny() {
        let cli = CliConfig::parse_from([
            "svc-process",
            "--deployment-tier",
            "production",
            "--egress-cluster-cidr-denylist",
            "10.99.0.0/16",
        ]);
        cli.validate().expect("populated denylist passes");
        assert!(
            !cli.instance_egress_policy().allow_private_ip_egress,
            "default instance policy must deny private-ip egress"
        );
        let catalog = capabilities::HttpEgressCatalog::new();
        catalog.update(
            "waddles.a.b.c",
            bundle_host_http::egress::EgressRuleRow {
                // 10.0.0.9 is outside the cluster denylist above, so only
                // the instance policy is under test here.
                private_ip_grants: vec![("10.0.0.9".to_string(), vec!["GET".to_string()])],
                ..Default::default()
            },
        );
        let guard = build_process_egress_guard(
            &cli,
            catalog,
            test_egress_denied_metric(),
            bundle_host_http::egress::boxed(bundle_host_http::egress::StaticFlag(true)),
        )
        .expect("valid config produces a guard");
        let err = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "GET", "url": "https://10.0.0.9/"}),
            )
            .await
            .expect_err("default-deny instance policy must deny an otherwise-granted private IP");
        assert_eq!(err.code, "ssrf_blocked_address");
    }

    #[test]
    fn multi_tenant_path_selected_requires_both_config_present_and_gate_enabled() {
        assert!(
            multi_tenant_path_selected(true, true),
            "config present + gate on -> multi-tenant path"
        );
        assert!(
            !multi_tenant_path_selected(true, false),
            "either kill-switch on (gate reports disabled) -> legacy path, even with config present"
        );
        assert!(
            !multi_tenant_path_selected(false, true),
            "missing DB config -> legacy path, even with the gate enabled"
        );
        assert!(!multi_tenant_path_selected(false, false));
    }

    /// Mutual-exclusion regression test, missing-config half: `DB_READER_
    /// PASSWORD` absent must resolve to "legacy path" without even
    /// constructing a license client. Unlike the pre-multi-tenant version,
    /// there is no second `BUNDLE_SCOPE_TENANT_ID` config gate to test --
    /// this path serves every tenant/community it finds, never a single
    /// configured scope.
    #[tokio::test]
    async fn resolve_multi_tenant_path_decision_is_no_db_config_when_db_reader_password_unset() {
        let cli = CliConfig::parse_from(["svc-process"]);
        let mut config = test_config(cli);
        config.db_reader_password = None;
        assert_eq!(
            resolve_multi_tenant_path_decision(&config).await,
            PathDecision::NoDbConfig
        );
    }

    /// Mutual-exclusion regression test, path-active half ("multi-tenant
    /// path active + PROCESS_APP_ID set -> legacy loop not started"): DB
    /// config present and both kill-switch flags never seen (this test's
    /// clean-env `license::build_license_client` call, same fail-closed-
    /// to-OFF cold-client contract every other license test in this crate
    /// relies on) must resolve [`PathDecision::MultiTenant`] -- proving
    /// `run_with_shutdown`'s `PathDecision::MultiTenant` match arm is the
    /// one taken, so `try_start_process_loop` (the legacy loop) is
    /// structurally never called for this config, regardless of
    /// `process_app_id` being set. The complementary "either kill-switch ON
    /// -> legacy runs" half is `multi_tenant_path_selected`'s own `false`
    /// cases above -- forcing a real `penguin_licensing::LicenseClient` to
    /// report a raw kill-switch flag ON requires a live PostHog/license
    /// server this crate's test suite deliberately never depends on.
    #[tokio::test]
    async fn resolve_multi_tenant_path_decision_is_multi_tenant_when_db_config_present_and_kill_switches_unseen(
    ) {
        let cli = CliConfig::parse_from([
            "svc-process",
            "--process-app-id",
            "waddles.bot.commands.default",
        ]);
        let mut config = test_config(cli);
        config.db_reader_password = Some(crate::config::Secret::new("real-ro-password"));
        assert_eq!(
            resolve_multi_tenant_path_decision(&config).await,
            PathDecision::MultiTenant,
            "DB config present + never-seen kill-switch flags must select the multi-tenant path"
        );
    }

    /// Regression test for the alpha 2026-10-02 bug this task fixes: a
    /// license-client build failure (malformed `LICENSE_SERVER_URL`) with
    /// `DB_READER_PASSWORD` configured must still resolve
    /// [`PathDecision::MultiTenant`] (fail OPEN), not silently fall back to
    /// the legacy loop the way this function used to.
    // regression: multi-app path silently inactive, fell back to stale legacy env (alpha 2026-10-02)
    #[tokio::test]
    async fn resolve_multi_tenant_path_decision_fails_open_when_license_client_build_errors() {
        // Guard dropped before the `.await` below (clippy `await_holding_lock`),
        // same pattern as `run_with_shutdown_binds_serves_and_stops_on_signal`.
        {
            let _guard = ENV_LOCK.lock().unwrap();
            // SAFETY: serialized by ENV_LOCK above.
            unsafe {
                std::env::set_var("LICENSE_SERVER_URL", "not a valid url");
            }
        }
        let cli = CliConfig::parse_from(["svc-process"]);
        let mut config = test_config(cli);
        config.db_reader_password = Some(crate::config::Secret::new("real-ro-password"));
        let decision = resolve_multi_tenant_path_decision(&config).await;
        {
            let _guard = ENV_LOCK.lock().unwrap();
            // SAFETY: serialized by ENV_LOCK above.
            unsafe {
                std::env::remove_var("LICENSE_SERVER_URL");
            }
        }
        assert_eq!(
            decision,
            PathDecision::MultiTenant,
            "a license-client build error must fail OPEN (DB path enabled), never silently \
             force the legacy path"
        );
    }

    #[test]
    fn no_data_plane_path_available_is_true_only_when_neither_path_exists() {
        assert!(
            !no_data_plane_path_available(PathDecision::MultiTenant, ""),
            "multi-tenant path active -> always a usable path, regardless of PROCESS_APP_ID"
        );
        assert!(!no_data_plane_path_available(
            PathDecision::NoDbConfig,
            "waddles.bot.commands.default"
        ));
        assert!(!no_data_plane_path_available(
            PathDecision::KillSwitchOn,
            "waddles.bot.commands.default"
        ));
        assert!(
            no_data_plane_path_available(PathDecision::NoDbConfig, ""),
            "no DB config and no legacy PROCESS_APP_ID -> no usable path at all"
        );
        assert!(no_data_plane_path_available(PathDecision::KillSwitchOn, ""));
    }

    /// Security review fix regression test (carried forward): `db_reader_
    /// password: None` (the value `config::Config::from_cli` now produces
    /// for both a genuinely unset `DB_READER_PASSWORD` and Helm's
    /// always-rendered-but-empty default) must take the documented no-op
    /// branch rather than attempting a DB connection. No OTel subscriber
    /// installed, so `tracing::info!` is a harmless no-op; success is
    /// simply that this returns without panicking or spawning a task that
    /// reaches the license-client/DB-connect code path.
    #[tokio::test]
    async fn try_start_changelog_consumer_noop_when_db_reader_password_unset() {
        let cli = CliConfig::parse_from(["svc-process"]);
        let mut config = test_config(cli);
        config.db_reader_password = None;
        try_start_changelog_consumer(
            &config,
            Arc::new(host_api::ConnectionRegistry::new()),
            test_excluded_metric(),
            test_source_supervisor_metrics(),
            test_egress_denied_metric(),
            test_changelog_consumer_metrics(),
            test_consumer_loop_ready(),
        );
    }

    /// A standalone, unregistered [`telemetry::ChangelogConsumerMetrics`] --
    /// same rationale as [`test_excluded_metric`].
    fn test_changelog_consumer_metrics() -> telemetry::ChangelogConsumerMetrics {
        telemetry::register_changelog_consumer_metrics(&prometheus::Registry::new())
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

    /// A standalone, unregistered `IntCounterVec` for `try_start_process_loop`/
    /// `try_start_db_bundle_loader` tests -- same rationale as
    /// [`test_excluded_metric`].
    fn test_egress_denied_metric() -> prometheus::IntCounterVec {
        prometheus::IntCounterVec::new(
            prometheus::Opts::new("test_svc_process_egress_denied_total", "test"),
            &["app_id", "reason"],
        )
        .expect("valid metric definition")
    }

    /// A standalone, unregistered [`telemetry::DrainLoopMetrics`] for
    /// `try_start_process_loop` tests -- same rationale as
    /// [`test_egress_denied_metric`]. regression: drain loop exited on
    /// NOGROUP (alpha 2026-10-02)
    fn test_drain_loop_metrics() -> telemetry::DrainLoopMetrics {
        telemetry::register_drain_loop_metrics(&prometheus::Registry::new())
    }

    /// A fresh, defaulted-`true` readiness flag for `try_start_process_loop`
    /// tests -- see `http::AppState::consumer_loop_ready`'s doc for the
    /// default rationale.
    fn test_consumer_loop_ready() -> Arc<std::sync::atomic::AtomicBool> {
        Arc::new(std::sync::atomic::AtomicBool::new(true))
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
        try_start_process_loop(
            &config,
            Arc::new(host_api::ConnectionRegistry::new()),
            test_egress_denied_metric(),
            test_drain_loop_metrics(),
            test_consumer_loop_ready(),
        );
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
        try_start_process_loop(
            &config,
            Arc::new(host_api::ConnectionRegistry::new()),
            test_egress_denied_metric(),
            test_drain_loop_metrics(),
            test_consumer_loop_ready(),
        );
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
        try_start_process_loop(
            &config,
            Arc::new(host_api::ConnectionRegistry::new()),
            test_egress_denied_metric(),
            test_drain_loop_metrics(),
            test_consumer_loop_ready(),
        );
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
        try_start_process_loop(
            &config,
            Arc::new(host_api::ConnectionRegistry::new()),
            test_egress_denied_metric(),
            test_drain_loop_metrics(),
            test_consumer_loop_ready(),
        );
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
            try_start_process_loop(
                &config,
                Arc::new(host_api::ConnectionRegistry::new()),
                test_egress_denied_metric(),
                test_drain_loop_metrics(),
                test_consumer_loop_ready(),
            );
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
            try_start_process_loop(
                &config,
                Arc::new(host_api::ConnectionRegistry::new()),
                test_egress_denied_metric(),
                test_drain_loop_metrics(),
                test_consumer_loop_ready(),
            );
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
        let registry = try_start_host_api(
            &cli,
            telemetry::register_host_api_metrics(&prometheus::Registry::new()),
        );
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
        let state = crate::http::AppState::new(
            config,
            prometheus::Registry::new(),
            Arc::new(host_api::ConnectionRegistry::new()),
        );
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
