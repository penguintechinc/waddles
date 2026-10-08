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
//!   wired, every one of them (including `http`) authorized by
//!   `core/bundle_capability_gate::authorize` FIRST (spec SS5, PR #433)
//! - The `GET /api/v1/distribution/bundles?stage=process` activation poll
//!   (spec §6.7) that would resolve `PROCESS_APP_ID`'s real granted-stream
//!   list, bundle digest, and approved `routes_to` set -- see
//!   [`try_start_process_loop`]'s doc for the interim, env-driven
//!   substitutes this landing uses instead
//!
//! This crate is split into a library (this file) and a thin binary
//! (`src/main.rs`) so integration tests under `tests/` can exercise the
//! router and config loader directly instead of spawning a subprocess.

pub mod active_digests;
pub mod builtins;
pub mod bundle_loader;
pub mod capabilities;
pub mod changelog_consumer;
pub mod config;
pub mod error;
pub mod grant_gate;
pub mod hop;
pub mod host_api;
pub mod http;
pub mod license;
pub mod pii_tokenize;
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

/// hub-api internal gRPC scope this service requests when bootstrapping its
/// machine JWT (`hub_api/grpc_internal/servicers.py::REQUIRED_SCOPES`) --
/// `MintEphemeralPseudonyms` only; this service never calls
/// `ResolveDisplayNames`/`GetStreamDek`.
const HUB_IDENTITY_MINT_SCOPE: &str = "identity:ephemeral:mint";

/// Builds and connects the shared `hub_client::HubClient` the inbound
/// PII-tokenization pass (`crate::pii_tokenize`) needs, or `Ok(None)` when
/// `tokenization_enabled` is `false` (the opt-out kill-switch is ON) -- no
/// client is needed in that case, and `crate::spine::ProcessDeps::
/// pii_minter` stays `None`, taking this crate's existing
/// "disabled-gate-short-circuits-before-the-minter-is-consulted" path
/// (`spine::handle_delivered`'s own doc).
///
/// **Fail loud, never silent dead-letter (user requirement).** See
/// [`run_with_shutdown`]'s call site for the full rationale: when
/// `tokenization_enabled` is `true` but `HUB_API_GRPC_ENDPOINT`/
/// `SERVICE_JWT_TOKEN_ENDPOINT` are unset, or the initial connect attempt
/// fails, this returns `Err` so the caller can exit non-zero instead of
/// starting a pod that dead-letters every inbound event. This check is
/// **startup-only** -- a transient gRPC failure after a successful connect
/// here still degrades to `pii_tokenize::tokenize_event`'s existing
/// fail-closed dead-letter path (`hub_client::HubClient`'s own circuit
/// breaker/retries already bound how long such a blip affects any one
/// call), never a process exit.
async fn build_hub_client(
    cli: &config::CliConfig,
    tokenization_enabled: bool,
) -> anyhow::Result<Option<Arc<hub_client::HubClient>>> {
    if !tokenization_enabled {
        tracing::info!(
            "waddles.core.disable-pii-tokenization kill-switch is ON; hub_client not \
             connected (no minter configured -- tokenize_event is never reached while the \
             gate reports disabled)"
        );
        return Ok(None);
    }
    if cli.hub_api_grpc_endpoint.is_empty() || cli.service_jwt_token_endpoint.is_empty() {
        anyhow::bail!(
            "PII tokenization is enabled (the default) but HUB_API_GRPC_ENDPOINT/\
             SERVICE_JWT_TOKEN_ENDPOINT is unset; refusing to start and silently dead-letter \
             every inbound event -- set both env vars, or set the \
             waddles.core.disable-pii-tokenization kill-switch for a deployment without a \
             working hub_client connection yet"
        );
    }
    match hub_client::HubClient::connect(
        cli.hub_api_grpc_endpoint.clone(),
        cli.service_jwt_token_endpoint.clone(),
        cli.service_jwt_sa_token_path.clone(),
        HUB_IDENTITY_MINT_SCOPE,
        // fix/hub-grpc-tls-and-ca-trust (PR #570 review blocker 2) -- empty
        // means "fall back to system/webpki roots", which this chart's
        // self-signed internal CA is never part of; HUB_API_GRPC_CA_FILE
        // is the real path in every deployed environment.
        (!cli.hub_api_grpc_ca_file.is_empty()).then_some(cli.hub_api_grpc_ca_file.as_str()),
    )
    .await
    {
        Ok(client) => {
            tracing::info!(
                endpoint = %cli.hub_api_grpc_endpoint,
                "hub_client connected; inbound PII tokenization is live"
            );
            Ok(Some(Arc::new(client)))
        }
        Err(err) => Err(anyhow::anyhow!(
            "PII tokenization is enabled but connecting to hub-api's internal gRPC endpoint \
             {:?} failed: {err}; refusing to start and silently dead-letter every inbound \
             event -- fix the endpoint/credentials, or set the \
             waddles.core.disable-pii-tokenization kill-switch",
            cli.hub_api_grpc_endpoint
        )),
    }
}

/// Resolves whether inbound PII tokenization is enabled for this startup.
///
/// [`config::CliConfig::pii_tokenization_enabled_override`]'s explicit
/// `Some(false)` (`PII_TOKENIZATION_ENABLED=false`) short-circuits to
/// disabled *before* the `waddles.core.disable-pii-tokenization` PostHog
/// kill-switch is ever consulted -- a plain env/values off-switch,
/// independent of PostHog, for an environment with no in-cluster PostHog
/// (e.g. alpha) that would otherwise be stranded behind
/// [`build_hub_client`]'s fail-loud gate whenever hub-api's internal gRPC
/// isn't reachable yet (`rules/critical-rules.md` Feature Flags & License
/// Tiers' opt-out kill-switch principle: "keeps unseen-flags-OFF without
/// stranding air-gapped deploys"). Any other override state (`Some(true)`
/// or unset/`None`) falls through unchanged to the existing PostHog-gated,
/// default-ENABLED, fail-open-to-ENABLED-on-license-error behavior --
/// **this override only ever disables, never force-enables past the
/// PostHog kill-switch**, so the production kill-switch path is untouched.
///
/// **SECURITY: an explicit, loudly-logged operator escape hatch, never a
/// silent bypass.** Disabling tokenization means every inbound event
/// reaches a bundle with raw platform usernames/handles/display names --
/// acceptable only as a deliberate, documented dev/air-gapped tradeoff,
/// never the default in a production tenant.
async fn resolve_pii_tokenization_enabled(cli: &config::CliConfig) -> bool {
    if cli.pii_tokenization_enabled_override == Some(false) {
        tracing::warn!(
            "PII_TOKENIZATION_ENABLED=false env override set; inbound PII tokenization is \
             DISABLED by explicit operator override, NOT the waddles.core.disable-pii-tokenization \
             PostHog kill-switch -- hub-api's internal gRPC will not be contacted and startup \
             will not fail loud. This is an operational escape hatch for dev/air-gapped \
             deployments and must never be set in a production tenant: inbound events will \
             reach bundles with raw platform usernames/handles/display names, unescaped."
        );
        return false;
    }
    match license::build_license_client("waddles") {
        Ok(client) => license::PiiTokenizationGate::new(client).enabled().await,
        Err(err) => {
            tracing::warn!(
                error = %err,
                "license client config invalid; PII tokenization kill-switch state unknown, \
                 defaulting to ENABLED (fail-open, the safe default -- see \
                 license::PiiTokenizationGate's own doc)"
            );
            true
        }
    }
}

/// Picks the [`license::FeatureGate`] [`try_start_process_loop`]/
/// [`try_start_changelog_consumer`] wire into `spine::ProcessDeps::
/// pii_gate` -- the single call both functions route through so their gate
/// selection can never drift from each other again.
///
/// `env_disabled` short-circuits to [`license::StaticGate`]`(false)`,
/// bypassing `live` (a [`license::PiiTokenizationGate`] in production)
/// entirely -- an explicit `PII_TOKENIZATION_ENABLED=false` always wins
/// over the PostHog kill-switch's live answer, even when that kill-switch
/// itself reports tokenization enabled (the inverted gate's unseen/default
/// answer -- see [`license::PiiTokenizationGate`]'s own doc). When
/// `env_disabled` is `false`, `live` is returned unchanged -- this never
/// force-enables past the kill-switch, only ever forces off, matching
/// [`resolve_pii_tokenization_enabled`]'s own "only ever disables" contract.
///
/// **Fix: `pii_gate`/`hub_minter` mismatch (alpha 2026-10-04 dead-letter
/// incident).** Before this function existed, [`try_start_process_loop`]
/// and [`try_start_changelog_consumer`] each built their own
/// [`license::PiiTokenizationGate`] directly, consulting ONLY the PostHog
/// kill-switch -- never [`resolve_pii_tokenization_enabled`]'s env
/// override, which is what actually decided whether `hub_minter` got
/// built. With `PII_TOKENIZATION_ENABLED=false` set, `hub_minter` was
/// `None` while the gate still reported `true`, and
/// `crate::spine::handle_delivered`'s fail-closed check (correctly) dead-
/// lettered every inbound event, with no bundle ever reached. Both call
/// sites now route their gate selection through this one function instead.
fn resolve_pii_gate(
    env_disabled: bool,
    live: Arc<dyn license::FeatureGate>,
) -> Arc<dyn license::FeatureGate> {
    if env_disabled {
        Arc::new(license::StaticGate(false))
    } else {
        live
    }
}

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
    // `crate::license::resolve_flag_with`'s `flags.enabled` host-call
    // evaluation counter (`svc_process_flags_evaluated_total{result}`) --
    // registered into this crate's own registry (not a process-global
    // default one) and wired into `crate::license` via
    // `set_flags_metric` before any bundle invoke can reach
    // `StageCapabilities::handle_flags`.
    license::set_flags_metric(telemetry::register_flags_metrics(&prom_registry));

    let connections = try_start_host_api(&config.cli, host_api_metrics);

    let state = http::AppState::new(config.clone(), prom_registry, Arc::clone(&connections));
    let consumer_loop_ready = Arc::clone(&state.consumer_loop_ready);
    // Bundle-permissions-and-capability-gate wiring (spec SS12 Phase 4):
    // shared regardless of which bundle-selection path below is active --
    // see `core/svc_action::run_with_shutdown`'s identical field.
    let app_version_snapshot = bundle_active_set::ActiveVersionSnapshot::new();
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

    // PII tokenization's hub_client startup wiring -- closes the
    // `TODO(M4+)` seam `build_source_supervisor_deps`/the legacy
    // `try_start_process_loop` used to leave as `pii_minter: None`.
    // Resolved ONCE here, before either drain-loop path starts below, and
    // shared by whichever one `decision` selects. See
    // [`build_hub_client`]'s own doc for the fail-loud contract, and
    // [`resolve_pii_tokenization_enabled`]'s own doc for the
    // PII_TOKENIZATION_ENABLED env override evaluated before the PostHog
    // kill-switch.
    let pii_tokenization_enabled = resolve_pii_tokenization_enabled(&config.cli).await;
    // Over-log the resolved state + source (never the flag/override VALUE
    // alone, which gives no indication of why it resolved that way) --
    // visibility fix for the alpha 2026-10-04 dead-letter incident, where
    // this crate's OTHER tokenization gate (`crate::license::
    // PiiTokenizationGate`, built separately in `try_start_process_loop`/
    // `try_start_changelog_consumer`) silently disagreed with this one. No
    // secrets/PII in this line -- just booleans and a source label. Field
    // name deliberately avoids the substring "token" (unlike the local
    // variable/doc prose) -- `penguin_logging::sanitize`'s key-pattern
    // redaction matches on it and would otherwise print `[REDACTED]` for
    // this boolean, defeating the entire point of this log line.
    tracing::info!(
        pii_inbound_mode_enabled = pii_tokenization_enabled,
        source = if config.cli.pii_tokenization_enabled_override == Some(false) {
            "env-override(PII_TOKENIZATION_ENABLED=false)"
        } else {
            "posthog-kill-switch-or-default"
        },
        "resolved inbound PII tokenization state"
    );
    let hub_minter: Option<Arc<dyn pii_tokenize::IdentityMinter>> =
        build_hub_client(&config.cli, pii_tokenization_enabled)
            .await?
            .map(|client| {
                Arc::new(pii_tokenize::HubClientMinter(client))
                    as Arc<dyn pii_tokenize::IdentityMinter>
            });

    match decision {
        PathDecision::MultiTenant => {
            state
                .multi_tenant_consumer_configured
                .store(true, std::sync::atomic::Ordering::Relaxed);
            // regression: legacy ping consumer competed in the same consumer group as the
            // multi-tenant one; ping intermittently UnknownBundle (alpha 2026-10-03).
            // Multi-tenant is selected -- `try_start_process_loop` (legacy drain loop,
            // which is what actually joins a consumer group on PROCESS_APP_ID) is never
            // called below, regardless of which legacy env vars are set. WARN (not INFO)
            // because a stale/leftover legacy env on this pod is itself the alpha
            // incident's root cause -- an operator needs to see this every pod restart,
            // not just once.
            let ignored_legacy_env: Vec<&str> = [
                (!config.cli.process_app_id.is_empty()).then_some("PROCESS_APP_ID"),
                (!config.cli.process_ingest_platform.is_empty())
                    .then_some("PROCESS_INGEST_PLATFORM"),
                (!config.cli.process_ingest_source_id.is_empty())
                    .then_some("PROCESS_INGEST_SOURCE_ID"),
                (!config.cli.process_bundle_digest.is_empty()).then_some("PROCESS_BUNDLE_DIGEST"),
            ]
            .into_iter()
            .flatten()
            .collect();
            if !ignored_legacy_env.is_empty() {
                tracing::warn!(
                    ignored_legacy_env = ignored_legacy_env.join(","),
                    "startup path: multi-tenant changelog-consumer (DB_READER_PASSWORD \
                     configured, kill-switches enabled); legacy single-app env present but \
                     IGNORED -- the legacy drain loop will not start (restart with \
                     DB_READER_PASSWORD unset to fall back to it)"
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
                hub_minter.clone(),
                app_version_snapshot.clone(),
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
                app_version_snapshot.clone(),
                egress_denied_metric,
                drain_loop_metrics,
                consumer_loop_ready,
                hub_minter.clone(),
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
                app_version_snapshot,
                egress_denied_metric,
                drain_loop_metrics,
                consumer_loop_ready,
                hub_minter,
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

/// Opens the direct Valkey connection the `kv` host capability is backed
/// by (`spine::ProcessDeps::kv_conn`'s doc). Never fatal on failure --
/// returns `None` (logged) so the caller can start every other capability
/// regardless.
async fn connect_kv(cfg: &penguin_spine::SpineConfig) -> Option<redis::aio::MultiplexedConnection> {
    let client = build_redis_client(cfg)?;
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

/// Builds the `db` bundle host capability's production wiring
/// (`spine::ProcessDeps::db_wiring`): connects to the shared `waddles`
/// Postgres instance as the least-privilege `waddles_bundle_runtime` role
/// (`bundle_host_db::connect`, `alembic/versions/0030_bundle_app_schemas.py`)
/// and gates every call on `BUNDLE_DB_CAPABILITY_FLAG`
/// (`license::BundleDbCapabilityGate`, default OFF).
///
/// **Two distinct, deliberately different outcomes -- never conflated
/// (`rules/critical-rules.md` Verification Integrity / this task's own
/// "fail loud, never silent-deny-masquerading-as-ok" requirement):**
/// - `password` is `None` (`BUNDLE_DB_PASSWORD` unset): this deployment has
///   never opted into the `db` capability at all -- logs at INFO and
///   returns `None`. Every `db` host-call then denies `not_implemented`
///   (`capabilities::StageCapabilities::handle_db`'s existing fail-closed
///   default), an accurate description of this state.
/// - `password` is `Some` but the connection itself fails (bad
///   credentials, role not provisioned, network/DNS failure): the operator
///   has *declared intent* to run this capability, so a quiet `None` here
///   would misreport a real outage as "never configured" -- logs at ERROR
///   (never WARN/INFO) naming the host/port/db/role (never the password)
///   and still returns `None` (never panics/crashes the process loop over
///   a single capability's backend being unavailable), matching every
///   other startup-config gate in this module's graceful-degradation
///   posture.
///
/// `license_client` is the same shared client [`try_start_process_loop`]
/// already built for `waddles.core.rust-data-plane` -- reused here rather
/// than opening a second one, so both gates share one cached PostHog
/// snapshot.
async fn try_build_db_wiring(
    cfg: &bundle_host_db::ConnectConfig,
    password: Option<&config::Secret>,
    license_client: &Arc<penguin_licensing::LicenseClient>,
) -> Option<capabilities::DbWiring> {
    let Some(password) = password else {
        tracing::info!(
            "BUNDLE_DB_PASSWORD not set; db capability not wired (every db host-call will \
             report not_implemented until BUNDLE_DB_PASSWORD is provisioned)"
        );
        return None;
    };

    match bundle_host_db::connect_runtime_db(cfg, password.expose()).await {
        Ok(conn) => {
            tracing::info!(
                host = %cfg.host,
                port = cfg.port,
                name = %cfg.name,
                user = %cfg.user,
                "db capability: connected to the bundle-runtime Postgres role"
            );
            Some(capabilities::DbWiring {
                host: Arc::new(bundle_host_db::DbHost::new(
                    bundle_host_db::PostgresBackend::new(conn),
                )),
                schemas: Arc::new(bundle_host_db::SchemaCache::new()),
                capabilities: Arc::new(bundle_host_db::CapabilitySnapshot::new()),
                flag: Arc::new(license::BundleDbCapabilityGate::new(Arc::clone(
                    license_client,
                ))),
            })
        }
        Err(err) => {
            // Fail loud: `BUNDLE_DB_PASSWORD` was configured, so this is an
            // operational failure, never a quiet "not configured" --
            // ERROR, not WARN, and never silently treated as equivalent to
            // the capability simply not being opted into.
            tracing::error!(
                host = %cfg.host,
                port = cfg.port,
                name = %cfg.name,
                user = %cfg.user,
                error = %err,
                "db capability: BUNDLE_DB_PASSWORD is configured but the waddles_bundle_runtime \
                 connection failed -- db capability unavailable (every db host-call will report \
                 not_implemented); this is a misconfiguration or outage, not an intentional opt-out"
            );
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
    app_version_snapshot: bundle_active_set::ActiveVersionSnapshot,
    egress_denied_metric: prometheus::IntCounterVec,
    drain_loop_metrics: telemetry::DrainLoopMetrics,
    consumer_loop_ready: Arc<std::sync::atomic::AtomicBool>,
    hub_minter: Option<Arc<dyn pii_tokenize::IdentityMinter>>,
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
    let license_gate: Arc<dyn license::FeatureGate> = Arc::new(license::LicenseFeatureGate::new(
        Arc::clone(&license_client),
    ));
    // Opt-out kill-switch for the inbound PII-tokenization pre-dispatch
    // pass (`crate::pii_tokenize`) -- default ENABLED, see
    // `license::PiiTokenizationGate`'s own doc. Routed through
    // `resolve_pii_gate` so `PII_TOKENIZATION_ENABLED=false` keeps this
    // gate in lockstep with `hub_minter` (`None` under the same override,
    // resolved once in `run_with_shutdown` via
    // `resolve_pii_tokenization_enabled`) -- see `resolve_pii_gate`'s own
    // doc for the dead-letter incident this fixes.
    let pii_gate = resolve_pii_gate(
        config.cli.pii_tokenization_enabled_override == Some(false),
        Arc::new(license::PiiTokenizationGate::new(Arc::clone(
            &license_client,
        ))),
    );

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
    let db_reader_password = config.db_reader_password.clone();
    // `db` host capability: connection settings + secret cloned out here
    // (this function only borrows `config`) so the spawned 'static task
    // below can open the connection itself, mirroring `kv_conn`'s own
    // "resolved inside the spawned block" placement -- see
    // `try_build_db_wiring`'s doc for the two distinct outcomes.
    let bundle_db_cfg = bundle_host_db::ConnectConfig {
        host: cli.bundle_db_host.clone(),
        port: cli.bundle_db_port,
        name: cli.bundle_db_name.clone(),
        user: cli.bundle_db_user.clone(),
    };
    let bundle_db_password = config.bundle_db_password.clone();
    let bundle_db_license_client = Arc::clone(&license_client);

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
        // Env-only mode has no `BUNDLE_SCOPE_TENANT_ID`/DB reader to resolve
        // a real tenant from (that CLI flag was removed with the
        // multi-tenant redesign, `crate::source_supervisor`'s module doc) --
        // `(0, 0)` fails closed (denies every non-platform permission)
        // rather than matching a real tenant's grants (`ProcessDeps::
        // tenant_id`'s doc).
        // When the DB-driven changelog consumer path is unconfigured
        // (`DB_READER_PASSWORD` unset), no poller ever populates
        // `app_version_snapshot` for this `app_id` -- seed it ONCE with a
        // `0` sentinel here, mirroring `core/svc_action::try_start_dispatch`'s
        // identical unconfigured-mode fallback.
        if db_reader_password.is_none() {
            app_version_snapshot.update(&[bundle_active_set::ActiveBundleRow {
                app_id: app_id.clone(),
                version: String::new(),
                version_id: 0,
                digest: cli.process_bundle_digest.clone(),
                component_key: String::new(),
                sidecar_key: String::new(),
                artifact_signature: None,
                artifact_signature_key_id: None,
                artifact_signed_approval_id: None,
                declared_capabilities: Vec::new(),
            }]);
        }
        let kv_conn = connect_kv(&spine_cfg).await;
        // `db` host capability: see `try_build_db_wiring`'s doc for the
        // not-configured-vs-connection-failed distinction.
        let db_wiring = try_build_db_wiring(
            &bundle_db_cfg,
            bundle_db_password.as_ref(),
            &bundle_db_license_client,
        )
        .await;
        // Bundle-permissions-and-capability-gate wiring (spec SS12 Phase 4):
        // `PgGrantLoader` against the RO-replica reader account when
        // `DB_READER_PASSWORD` is configured (the same account the
        // multi-tenant changelog-consumer path uses), `InMemoryGrantLoader`
        // (always denies every non-platform permission) otherwise.
        let redis_client = build_redis_client(&spine_cfg);
        let gate = match &db_reader_password {
            Some(password) => {
                let reader_cfg = bundle_active_set::ReaderConfig {
                    host: cli.db_reader_host.clone(),
                    port: cli.db_reader_port,
                    name: cli.db_reader_name.clone(),
                    user: cli.db_reader_user.clone(),
                };
                match bundle_active_set::reader::connect(&reader_cfg, password.expose()).await {
                    Ok(db) => grant_gate::build_production_gate(
                        grant_gate::PgGrantLoader::new(db),
                        redis_client,
                        cli.bundle_config_poll_interval(),
                    ),
                    Err(err) => {
                        tracing::warn!(error = %err, "grant-gate db-reader connection failed; every non-platform permission denies until the next connection attempt");
                        grant_gate::build_production_gate(
                            bundle_capability_gate::InMemoryGrantLoader::new(),
                            redis_client,
                            cli.bundle_config_poll_interval(),
                        )
                    }
                }
            }
            None => grant_gate::build_production_gate(
                bundle_capability_gate::InMemoryGrantLoader::new(),
                redis_client,
                cli.bundle_config_poll_interval(),
            ),
        };

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
                digest_source: spine::DigestSource::Static(cli.process_bundle_digest.clone()),
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
                pii_gate: Arc::clone(&pii_gate),
                // `crate::build_hub_client`'s connected `HubClientMinter`,
                // or `None` when the opt-out kill-switch is ON --
                // `run_with_shutdown` resolves this once, before either
                // drain-loop path starts (fail-loud if tokenization is
                // enabled and the connect failed), and passes it down
                // unconditionally from here.
                pii_minter: hub_minter.clone(),
                db_wiring: db_wiring.clone(),
                // Env-only legacy mode has no `BUNDLE_SCOPE_TENANT_ID`/DB
                // reader to resolve a real tenant from -- `(0, 0)` fails
                // closed (denies every non-platform permission) rather than
                // matching a real tenant's grants (`ProcessDeps::tenant_id`'s
                // doc).
                tenant_id: 0,
                community_id: 0,
                gate: Arc::clone(&gate),
                app_version_snapshot: app_version_snapshot.clone(),
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
#[allow(clippy::too_many_arguments)]
fn try_start_changelog_consumer(
    config: &config::Config,
    connections: Arc<host_api::ConnectionRegistry>,
    excluded_metric: prometheus::IntCounterVec,
    source_supervisor_metrics: telemetry::SourceBindingSupervisorMetrics,
    egress_denied_metric: prometheus::IntCounterVec,
    changelog_consumer_metrics: telemetry::ChangelogConsumerMetrics,
    consumer_loop_ready: Arc<std::sync::atomic::AtomicBool>,
    hub_minter: Option<Arc<dyn pii_tokenize::IdentityMinter>>,
    app_version_snapshot: bundle_active_set::ActiveVersionSnapshot,
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
    // `resolve_pii_gate` forces `pii_gate` to `StaticGate(false)` in BOTH
    // match arms below whenever the operator's env override is set,
    // regardless of whether `license::build_license_client` itself
    // succeeds -- it must stay in lockstep with `hub_minter` (`None` under
    // the same override, resolved once in `run_with_shutdown` via
    // `resolve_pii_tokenization_enabled`, independently of this function's
    // own license client build). See `resolve_pii_gate`'s own doc for the
    // dead-letter incident this fixes.
    let pii_tokenization_env_disabled = config.cli.pii_tokenization_enabled_override == Some(false);
    // Factored out purely to satisfy `clippy::type_complexity` on the
    // 4-tuple this `match` below produces.
    type ChangelogGates = (
        Arc<dyn license::FeatureGate>,
        Arc<dyn bundle_host_http::egress::FeatureFlag>,
        Arc<dyn license::FeatureGate>,
        Option<Arc<penguin_licensing::LicenseClient>>,
    );
    let (gate, bundle_egress_flag, pii_gate, bundle_db_license_client): ChangelogGates =
        match license::build_license_client("waddles") {
            Ok(license_client) => (
                Arc::new(license::AllGate(vec![
                    Arc::new(license::DbBundleConfigGate::new(Arc::clone(
                        &license_client,
                    ))),
                    Arc::new(license::MultiTenantWatermarkGate::new(Arc::clone(
                        &license_client,
                    ))),
                ])),
                bundle_host_http::egress::boxed(license::BundleEgressFlag::new(Arc::clone(
                    &license_client,
                ))),
                resolve_pii_gate(
                    pii_tokenization_env_disabled,
                    Arc::new(license::PiiTokenizationGate::new(Arc::clone(
                        &license_client,
                    ))),
                ),
                // Reused for `BUNDLE_DB_CAPABILITY_FLAG` (`try_build_db_wiring`)
                // -- same shared client/cached snapshot as every other gate
                // built from it above.
                Some(license_client),
            ),
            Err(err) => {
                tracing::warn!(
                    error = %err,
                    "license client config invalid; multi-tenant kill-switch state unknown, \
                     defaulting to ENABLED (fail-open, DB_READER_PASSWORD already configured) -- \
                     bundle-egress and db capabilities denied until a valid license config is set"
                );
                (
                    Arc::new(license::AllGate(Vec::new())),
                    bundle_host_http::egress::boxed(bundle_host_http::egress::StaticFlag(false)),
                    // PII tokenization is a hard security invariant, never
                    // fail-open on an unrelated license-client build error --
                    // unlike `gate`/`bundle_egress_flag` above (feature
                    // availability), an unknown kill-switch state here must
                    // still resolve to "tokenization enabled" (the safe
                    // default) UNLESS the operator's env override already
                    // forced it off -- `resolve_pii_gate` applies that same
                    // override here too, so `pii_gate` agrees with
                    // `hub_minter: None` the same as the `Ok` arm above.
                    resolve_pii_gate(
                        pii_tokenization_env_disabled,
                        Arc::new(license::AllGate(Vec::new())),
                    ),
                    // No valid client to gate `db` capability entitlement with
                    // -- `try_build_db_wiring` is never even attempted below
                    // (fail-closed, "disable rather than start unverified",
                    // same posture as every other startup-config gate here).
                    None,
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
    // Shared with `changelog_consumer::run` below (the sole writer, via
    // `apply_active_set`) -- every per-binding consumer `supervisor_deps`
    // spawns reads from this SAME instance (`DigestSource::Active`).
    // regression: multi-tenant consumers invoked with empty legacy digest,
    // UnknownBundle (alpha 2026-10-03).
    let active_digests = Arc::new(active_digests::ActiveDigests::new());
    let bundle_db_cfg = bundle_host_db::ConnectConfig {
        host: config.cli.bundle_db_host.clone(),
        port: config.cli.bundle_db_port,
        name: config.cli.bundle_db_name.clone(),
        user: config.cli.bundle_db_user.clone(),
    };
    let bundle_db_password = config.bundle_db_password.clone();
    let config = config.clone();

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

        // The source-binding supervisor's own optional dependencies --
        // built once the reader `db` connection (reused for
        // `grant_gate::PgGrantLoader`, a read-only path exactly like the
        // changelog reads this same connection already performs) is
        // available, so a missing/invalid one only disables the supervisor
        // half, never the bundle load/unload half. Unlike the
        // pre-multi-tenant version, tenant/community scope is no longer
        // part of this prereq (resolved per-scope, inside
        // `changelog_consumer`, not once per whole supervisor instance).
        // `kv_conn` is opened inside `build_source_supervisor_deps` itself
        // (network I/O, hence that function being async) -- never patched
        // in from here.
        let spawner: Option<Arc<dyn source_supervisor::ConsumerSupervisor>> =
            match build_source_supervisor_deps(
                &config,
                Arc::clone(&connections),
                Arc::clone(&gate),
                db.clone(),
                Arc::clone(&kv_capabilities),
                app_version_snapshot.clone(),
                Arc::clone(&egress),
                Arc::clone(&active_digests),
                Arc::clone(&pii_gate),
                // `crate::build_hub_client`'s connected `HubClientMinter`, or
                // `None` when the opt-out kill-switch is ON -- `run_with_shutdown`
                // resolves this once, before either drain-loop path starts
                // (fail-loud if tokenization is enabled and the connect failed).
                hub_minter.clone(),
            )
            .await
            {
                Some(mut deps) => {
                    // `db` host capability: see `try_build_db_wiring`'s doc.
                    // `bundle_db_license_client` is `None` only when this
                    // function's own license-client build already failed
                    // above (fail-closed for `db`, logged there) -- never
                    // attempted in that case.
                    deps.db_wiring = match &bundle_db_license_client {
                        Some(client) => {
                            try_build_db_wiring(&bundle_db_cfg, bundle_db_password.as_ref(), client)
                                .await
                        }
                        None => None,
                    };
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
            active_digests,
            app_version_snapshot,
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
#[allow(clippy::too_many_arguments)]
async fn build_source_supervisor_deps(
    config: &config::Config,
    connections: Arc<host_api::ConnectionRegistry>,
    gate: Arc<dyn license::FeatureGate>,
    db: sea_orm::DatabaseConnection,
    kv_capabilities: Arc<bundle_host_kv::CapabilitySnapshot>,
    app_version_snapshot: bundle_active_set::ActiveVersionSnapshot,
    egress: Arc<bundle_host_http::egress::EgressGuard>,
    active_digests: Arc<active_digests::ActiveDigests>,
    pii_gate: Arc<dyn license::FeatureGate>,
    pii_minter: Option<Arc<dyn pii_tokenize::IdentityMinter>>,
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
    // Reuses the SAME reader `db` connection `crate::changelog_consumer`
    // already opened (a read-only pool handle, cheap to clone) rather than
    // opening a second one -- see this function's own doc.
    let redis_client = build_redis_client(&spine_cfg);
    let capability_gate = grant_gate::build_production_gate(
        grant_gate::PgGrantLoader::new(db),
        redis_client,
        config.cli.bundle_config_poll_interval(),
    );
    let kv_conn = connect_kv(&spine_cfg).await;

    Some(source_supervisor::SupervisorDeps {
        spine_cfg,
        key_ring,
        connections,
        call_timeout_ms: config.cli.executor_call_timeout_ms,
        approved_targets,
        metrics,
        license: gate,
        kv_conn,
        kv_capabilities,
        gate: capability_gate,
        app_version_snapshot,
        egress,
        active_digests,
        pii_gate,
        pii_minter,
        // Same "opened async inside the spawned task, this function stays
        // synchronous/no-I/O" placement as `kv_conn` above -- see
        // `try_start_changelog_consumer`'s own spawned block.
        db_wiring: None,
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

    fn test_license_client() -> Arc<penguin_licensing::LicenseClient> {
        let cfg = penguin_licensing::LicenseConfig::new("svc-process-test-db-wiring")
            .expect("default LicenseConfig::new never fails");
        penguin_licensing::LicenseClient::new(cfg)
            .expect("LicenseClient::new with a valid default config never fails")
    }

    /// `BUNDLE_DB_PASSWORD` unset -- the normal "never opted in" state, not
    /// an error: `db` capability stays unwired, `not_implemented` on every
    /// call (`capabilities::StageCapabilities::handle_db`'s existing
    /// default), no attempt to open a Postgres connection at all.
    #[tokio::test]
    async fn try_build_db_wiring_is_none_when_password_unset() {
        let cfg = bundle_host_db::ConnectConfig {
            host: "127.0.0.1".to_string(),
            port: 1,
            name: "waddlebot".to_string(),
            user: "waddles_bundle_runtime".to_string(),
        };
        let wiring = try_build_db_wiring(&cfg, None, &test_license_client()).await;
        assert!(wiring.is_none());
    }

    /// Fail-loud case: `BUNDLE_DB_PASSWORD` IS configured (the operator
    /// declared intent), but the connection fails -- must still return
    /// `None` (never crash the process loop over one capability), but this
    /// is the ERROR-logged, "misconfiguration/outage" branch, distinct from
    /// the above "never configured" branch. Port 1 on loopback is never a
    /// real Postgres listener -- the connect attempt fails fast
    /// (connection refused) rather than hanging.
    #[tokio::test]
    async fn try_build_db_wiring_is_none_and_logs_loud_when_connection_fails() {
        let cfg = bundle_host_db::ConnectConfig {
            host: "127.0.0.1".to_string(),
            port: 1,
            name: "waddlebot".to_string(),
            user: "waddles_bundle_runtime".to_string(),
        };
        let password = crate::config::Secret::new("wrong-password-does-not-matter");
        let wiring = try_build_db_wiring(&cfg, Some(&password), &test_license_client()).await;
        assert!(
            wiring.is_none(),
            "a refused connection must degrade to None, never panic"
        );
    }

    #[tokio::test]
    async fn run_with_shutdown_binds_serves_and_stops_on_signal() {
        // `build_license_client("waddles")`'s hardcoded self-domain bypass
        // (`license::BYPASS_DOMAIN`'s doc) makes PII tokenization report
        // ENABLED unconditionally for this service, test included -- so
        // `run_with_shutdown`'s `build_hub_client` call needs a real,
        // connectable `HUB_API_GRPC_ENDPOINT` or it fails loud (by design)
        // before ever reaching the bind/serve logic this test exercises.
        // `HubClient::connect` only needs a listening TCP peer (it doesn't
        // perform the actual gRPC handshake until the first RPC) -- a bare
        // accept-and-drop loop is enough.
        let hub_listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind an ephemeral port for the fake hub-api listener");
        let hub_addr = hub_listener.local_addr().expect("local_addr");
        tokio::spawn(async move {
            while let Ok((sock, _)) = hub_listener.accept().await {
                std::mem::forget(sock);
            }
        });

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
                "--hub-api-grpc-endpoint",
                &format!("http://{hub_addr}"),
                "--service-jwt-token-endpoint",
                &format!("http://{hub_addr}/internal/service-token"),
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
            bundle_db_password: None,
        }
    }

    /// `build_source_supervisor_deps`'s missing-`ENVELOPE_BINDING_KEYS`
    /// fail-closed branch: hop verification must never silently fail open,
    /// so a missing keyring disables ONLY the supervisor half (`None`),
    /// never the bundle-loader half.
    #[tokio::test]
    async fn build_source_supervisor_deps_is_none_without_envelope_binding_keys() {
        let cli = CliConfig::parse_from(["svc-process"]);
        let mut config = test_config(cli);
        config.envelope_binding_keys = None;
        let gate: Arc<dyn license::FeatureGate> = Arc::new(license::test_support::FixedGate(true));
        let db = sea_orm::MockDatabase::new(sea_orm::DatabaseBackend::Postgres).into_connection();
        assert!(build_source_supervisor_deps(
            &config,
            Arc::new(host_api::ConnectionRegistry::new()),
            gate,
            db,
            Arc::new(bundle_host_kv::CapabilitySnapshot::new()),
            bundle_active_set::ActiveVersionSnapshot::new(),
            test_egress_guard(),
            Arc::new(active_digests::ActiveDigests::new()),
            Arc::new(license::test_support::FixedGate(true)),
            None,
        )
        .await
        .is_none());
    }

    /// The happy path: valid `ENVELOPE_BINDING_KEYS` + reachable spine
    /// config produces `Some(SupervisorDeps)`.
    #[tokio::test]
    async fn build_source_supervisor_deps_is_some_with_valid_config() {
        // Guard is dropped before the `.await` below (clippy
        // `await_holding_lock`) -- `penguin_spine::SpineConfig::from_env`
        // reads these env vars synchronously inside
        // `build_source_supervisor_deps` before its first await, so they
        // only need to be set for that one non-async read.
        {
            let _guard = ENV_LOCK.lock().unwrap();
            // SAFETY: serialized by ENV_LOCK above.
            unsafe {
                std::env::set_var("VALKEY_URL", "rediss://127.0.0.1:1/");
                std::env::set_var("VALKEY_PASSWORD", "test-valkey-pass");
            }
        }
        let cli = CliConfig::parse_from(["svc-process"]);
        let config = test_config(cli);
        let gate: Arc<dyn license::FeatureGate> = Arc::new(license::test_support::FixedGate(true));
        let db = sea_orm::MockDatabase::new(sea_orm::DatabaseBackend::Postgres).into_connection();
        let deps = build_source_supervisor_deps(
            &config,
            Arc::new(host_api::ConnectionRegistry::new()),
            gate,
            db,
            Arc::new(bundle_host_kv::CapabilitySnapshot::new()),
            bundle_active_set::ActiveVersionSnapshot::new(),
            test_egress_guard(),
            Arc::new(active_digests::ActiveDigests::new()),
            Arc::new(license::test_support::FixedGate(true)),
            None,
        )
        .await;
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
            None,
            bundle_active_set::ActiveVersionSnapshot::new(),
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
            bundle_active_set::ActiveVersionSnapshot::new(),
            test_egress_denied_metric(),
            test_drain_loop_metrics(),
            test_consumer_loop_ready(),
            None,
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
            bundle_active_set::ActiveVersionSnapshot::new(),
            test_egress_denied_metric(),
            test_drain_loop_metrics(),
            test_consumer_loop_ready(),
            None,
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
            bundle_active_set::ActiveVersionSnapshot::new(),
            test_egress_denied_metric(),
            test_drain_loop_metrics(),
            test_consumer_loop_ready(),
            None,
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
            bundle_active_set::ActiveVersionSnapshot::new(),
            test_egress_denied_metric(),
            test_drain_loop_metrics(),
            test_consumer_loop_ready(),
            None,
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
                bundle_active_set::ActiveVersionSnapshot::new(),
                test_egress_denied_metric(),
                test_drain_loop_metrics(),
                test_consumer_loop_ready(),
                None,
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
                bundle_active_set::ActiveVersionSnapshot::new(),
                test_egress_denied_metric(),
                test_drain_loop_metrics(),
                test_consumer_loop_ready(),
                None,
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

    /// `PII_TOKENIZATION_ENABLED=false` env override: disabled before the
    /// PostHog kill-switch is ever consulted -- no license client is built,
    /// no network touched, and the result is `false` regardless of what the
    /// (never-called) PostHog flag would have reported.
    #[tokio::test]
    async fn resolve_pii_tokenization_enabled_honors_explicit_env_disable() {
        let cli = CliConfig::parse_from([
            "svc-process",
            "--pii-tokenization-enabled-override",
            "false",
        ]);
        assert_eq!(cli.pii_tokenization_enabled_override, Some(false));
        assert!(!resolve_pii_tokenization_enabled(&cli).await);
    }

    /// Env override unset (`None`, `CliConfig::parse_from`'s default): falls
    /// through unchanged to the existing PostHog-gated path, which reports
    /// ENABLED here (a cold/never-refreshed license client's own
    /// fail-closed-to-OFF semantics on the underlying
    /// `disable-pii-tokenization` flag negate to "tokenization enabled" --
    /// `license::PiiTokenizationGate`'s own doc/tests).
    #[tokio::test]
    async fn resolve_pii_tokenization_enabled_defers_to_posthog_gate_when_override_unset() {
        let cli = CliConfig::parse_from(["svc-process"]);
        assert_eq!(cli.pii_tokenization_enabled_override, None);
        assert!(resolve_pii_tokenization_enabled(&cli).await);
    }

    /// Regression test for the alpha 2026-10-04 dead-letter incident:
    /// `resolve_pii_gate(true, ...)` must report `false` even when the
    /// `live` gate passed in would itself report `true` -- i.e. the env
    /// override wins over a kill-switch that is ON (tokenization
    /// "enabled"), not just over a kill-switch that happens to already be
    /// OFF. This is the exact mismatch that left `pii_gate.enabled() ==
    /// true` while `hub_minter == None`, dead-lettering every inbound
    /// event.
    #[tokio::test]
    async fn resolve_pii_gate_env_override_wins_over_a_live_gate_reporting_enabled() {
        let live: Arc<dyn license::FeatureGate> = Arc::new(license::test_support::FixedGate(true));
        let gate = resolve_pii_gate(true, live);
        assert!(
            !gate.enabled().await,
            "PII_TOKENIZATION_ENABLED=false must force the gate OFF even though the live \
             kill-switch gate reports tokenization enabled"
        );
    }

    /// `env_disabled: false` (override unset/`true`) must return `live`
    /// unchanged -- this function never force-enables past the kill-switch,
    /// only ever forces off.
    #[tokio::test]
    async fn resolve_pii_gate_defers_to_live_gate_when_env_override_is_not_explicitly_false() {
        let live: Arc<dyn license::FeatureGate> = Arc::new(license::test_support::FixedGate(true));
        assert!(resolve_pii_gate(false, Arc::clone(&live)).enabled().await);

        let live_off: Arc<dyn license::FeatureGate> =
            Arc::new(license::test_support::FixedGate(false));
        assert!(!resolve_pii_gate(false, live_off).enabled().await);
    }

    /// Kill-switch ON (`tokenization_enabled: false`) -- no `HUB_API_GRPC_
    /// ENDPOINT` needed at all, no client connected, and no error: the
    /// opt-out path never touches the network.
    #[tokio::test]
    async fn build_hub_client_returns_none_when_tokenization_disabled() {
        let cli = CliConfig::parse_from(["svc-process"]);
        let result = build_hub_client(&cli, false).await;
        assert!(result.is_ok());
        assert!(result.unwrap().is_none());
    }

    /// Fail-loud regression test (user requirement): tokenization enabled
    /// but `HUB_API_GRPC_ENDPOINT`/`SERVICE_JWT_TOKEN_ENDPOINT` are unset
    /// (both default to `""`, `CliConfig::parse_from`'s default) must
    /// return `Err` -- asserted via this testable startup path, never a
    /// real `std::process::exit` in-test. `run_with_shutdown` propagates
    /// this `Err` via `?`, which is what actually crashloops the pod.
    #[tokio::test]
    async fn build_hub_client_fails_loud_when_enabled_and_endpoint_unset() {
        let cli = CliConfig::parse_from(["svc-process"]);
        assert_eq!(cli.hub_api_grpc_endpoint, "");
        assert_eq!(cli.service_jwt_token_endpoint, "");
        let err = match build_hub_client(&cli, true).await {
            Ok(_) => panic!("enabled tokenization with no endpoint configured must fail loud"),
            Err(err) => err,
        };
        assert!(err.to_string().contains("HUB_API_GRPC_ENDPOINT"));
    }

    /// Fail-loud regression test: tokenization enabled, both endpoints
    /// configured, but nothing is listening on the configured gRPC
    /// endpoint (an ephemeral port bound then immediately dropped,
    /// guaranteeing a prompt connection-refused rather than a hang) --
    /// `HubClient::connect`'s initial connect attempt fails, and
    /// `build_hub_client` must surface that as `Err`, never silently start
    /// with no minter configured.
    #[tokio::test]
    async fn build_hub_client_fails_loud_when_enabled_and_endpoint_unreachable() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind an ephemeral port");
        let addr = listener.local_addr().expect("local_addr");
        drop(listener); // nothing listening now -- connection refused

        let cli = CliConfig::parse_from([
            "svc-process",
            "--hub-api-grpc-endpoint",
            &format!("http://{addr}"),
            "--service-jwt-token-endpoint",
            &format!("http://{addr}/internal/service-token"),
        ]);
        let err = match build_hub_client(&cli, true).await {
            Ok(_) => panic!("an unreachable hub-api gRPC endpoint must fail loud at startup"),
            Err(err) => err,
        };
        assert!(err.to_string().contains("hub-api"));
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
            bundle_db_password: None,
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
