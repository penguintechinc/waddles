//! `svc-ingest`: the Waddles chat/event data-plane ingest service (Rust
//! rewrite, M5).
//!
//! This crate is split into a library (this file) and a thin binary
//! (`src/main.rs`) so integration tests under `tests/` can exercise the
//! router and config loader directly instead of spawning a subprocess --
//! same split as `core/svc_streaming`.
//!
//! **M5 scope: the full e2e produce path is live.** Per the M5 milestone
//! row in `docs/superpowers/specs/2026-09-14-rust-data-plane-design.md`
//! S16 and `docs/superpowers/plans/2026-09-14-rust-data-plane-m5-svc-
//! ingest.md`, this chunk wires the primary e2e path end to end: a real
//! Twitch IRC chat message (`crate::ingest::twitch`, secondary: Discord
//! Gateway, `crate::ingest::discord`) is normalized (`crate::normalize`,
//! ported from the Python predecessor's `builtin_handlers/{twitch,discord}_ingest.py`
//! plus `receivers/twitch_irc.py`'s IRCv3 tag decoding), minted into a
//! D30-complete envelope (`workstream_id`/`event_id`/`trace`/
//! `binding.mac`, via `penguin_spine::KeyRing`/`compute_binding_mac`,
//! `crate::publish`), and `XADD`ed via `penguin_spine::SpineClient` onto
//! its ingest source's spine stream -- so `svc_process` (M4) can drain it.
//! The Twitch outbound relay (`crate::outbound`) drains the plain Valkey
//! list `svc_action`'s `relay` host capability `LPUSH`es onto
//! (`waddles:transport:irc:twitch:outbound`) and sends each queued message
//! via `penguin_connector_twitch::irc::TwitchIrcSender`.
//!
//! **Resolved:** `penguin-connector-{twitch,discord}` previously could not
//! be linked into this binary alongside `penguin-spine`/`penguin-logging`
//! (a `serde`/`serde_json`/`tokio`/`thiserror` exact-pin conflict across
//! the two crate families) -- fixed upstream in `penguin-libs` PR #98 (the
//! connectors' pins were bumped to match spine/logging exactly). See
//! `Cargo.toml`'s dependency-pattern comment for the pinned rev and the
//! verification evidence.
//!
//! The whole bundle above is additionally gated on the
//! `waddles.core.rust-data-plane` PostHog flag (spec S13.5,
//! `crate::license`) -- OFF (the default until validated) means this
//! service serves `/health`/`/healthz`/`/metrics` and receives/produces/
//! drains nothing at all, checked once at startup before any
//! `try_start_*` call below. Each receiver/the outbound drain
//! additionally, independently no-ops with a logged reason when its own
//! configuration/secret is absent
//! (`try_start_twitch_irc`/`try_start_discord`/`try_start_twitch_outbound`),
//! the same graceful-degradation contract
//! `core/svc_process::try_start_spine_drain` uses for a missing
//! `SpineConfig`.
//!
//! Left as a documented seam per explicit coordinator direction (a
//! separate follow-up owns it): any `penguin_spine::SpineClient::
//! dead_letter` call-site change -- `penguin-spine` was rev'd for a
//! `dead_letter`/`Delivered.group` fix, but this crate makes no
//! `dead_letter` call and constructs no `Delivered` at all (ingest mints
//! and `append`s only, per D23/D24/S10.6 -- it never reads a stage
//! stream), so there is no call site to chase.
//!
//! Also out of scope, per the milestone's own stated priority order:
//! Slack/YouTube/Kick receivers, the generic signed-webhook and JWT REST
//! intake surfaces (S10.1), `penguin_spine::SocketLease` single-owner-
//! socket guarding (S10.2, a separate crate gap, PA-LEASE), and D31
//! usage-delta metering (`UsageBatcher`/`append_usage`, a `penguin-spine`
//! crate gap at the pinned rev -- see `crate::publish`'s module doc).
//!
//! Per spec S4.1, this service has **no database** -- no `sea-orm`
//! dependency, unlike `svc_process`/`svc_action`.

pub mod active_context;
pub mod commands_reconcile;
pub mod config;
pub mod connector;
pub mod connector_runtime;
pub mod crypto;
pub mod discord_rest;
pub mod display_names;
pub mod egress;
pub mod error;
pub mod http;
pub mod identity_crypto;
pub mod ingest;
pub mod interaction_store;
pub mod interaction_tap;
pub mod license;
pub mod normalize;
pub mod outbound;
pub mod outbound_boot;
pub mod outbound_creds;
pub mod outbound_creds_discord;
pub mod outbound_creds_kick;
pub mod outbound_creds_youtube;
pub mod outbound_ops;
pub mod outbound_pin;
pub mod pin_sig;
pub mod platform_users;
pub mod publish;
pub mod scope_resolver;
pub mod telemetry;
pub mod tenant_discord;
pub mod twitch_sessions;

use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use tokio::signal;

/// Default `tracing`/OTel service name, also the fallback `--healthcheck`
/// target and the resource `service.name` when `OTEL_SERVICE_NAME` is
/// unset.
pub const SERVICE_NAME: &str = "svc-ingest";

/// One receiver's spine-connect readiness: `enabled` is set once (true the
/// moment the receiver's own config checks pass and it starts attempting a
/// connect); `ready` tracks the live connect state and flips back to
/// `false` on every disconnect/retry. A receiver that was never enabled
/// (not configured) is always considered "ok" -- it was never supposed to
/// start, so it can't block readiness.
///
/// Regression: one-shot valkey probe disabled discord receiver (alpha
/// 2026-10-02) -- the pod stayed `Running 1/1` forever because nothing
/// tracked whether a receiver that decided to start ever actually
/// connected.
#[derive(Default)]
pub struct ReceiverFlag {
    enabled: AtomicBool,
    ready: AtomicBool,
}

impl ReceiverFlag {
    pub fn mark_enabled(&self) {
        self.enabled.store(true, Ordering::Relaxed);
    }

    pub fn set_ready(&self, ready: bool) {
        self.ready.store(ready, Ordering::Relaxed);
    }

    pub fn is_enabled(&self) -> bool {
        self.enabled.load(Ordering::Relaxed)
    }

    pub fn is_ready(&self) -> bool {
        self.ready.load(Ordering::Relaxed)
    }

    /// `true` if this receiver was never enabled (nothing to wait for), or
    /// it's enabled AND connected.
    fn ok(&self) -> bool {
        !self.is_enabled() || self.is_ready()
    }
}

/// Aggregates every fixed-platform receiver's [`ReceiverFlag`] -- shared
/// between `crate::http::AppState` (backing `GET /readyz`) and this
/// module's `try_start_*` functions (which flip each flag as they attempt/
/// succeed/lose a spine connection).
#[derive(Default)]
pub struct ReceiverReadiness {
    pub twitch_irc: ReceiverFlag,
    pub discord: ReceiverFlag,
    pub twitch_outbound: ReceiverFlag,
    /// Connector-runtime platforms and the runtime's readiness handle; set
    /// once at boot by [`ReceiverReadiness::set_connector`].
    connector: std::sync::OnceLock<ConnectorReadiness>,
}

/// The connector-ON platforms and, once the runtime is built, its readiness
/// handle. `handle: None` with platforms listed means the runtime is not
/// wired -- reported not-ready, never silently nothing.
struct ConnectorReadiness {
    platforms: Vec<&'static str>,
    handle: Option<Arc<dyn connector_runtime::RuntimeReady>>,
}

/// One connector platform's `/readyz` line.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConnectorStatus {
    pub platform: &'static str,
    pub readiness: connector_runtime::Readiness,
}

impl ReceiverReadiness {
    pub fn new() -> Self {
        Self::default()
    }

    /// `true` only when every *enabled* receiver is also connected --
    /// `GET /readyz`'s overall status.
    pub fn all_ready(&self) -> bool {
        self.twitch_irc.ok()
            && self.discord.ok()
            && self.twitch_outbound.ok()
            && self
                .connector_status()
                .iter()
                .all(|s| !matches!(s.readiness, connector_runtime::Readiness::NotReady(_)))
    }

    /// Records the connector-ON platforms and the runtime's readiness handle.
    /// Set once; a second call is ignored (boot runs once).
    pub fn set_connector(
        &self,
        platforms: Vec<&'static str>,
        handle: Option<Arc<dyn connector_runtime::RuntimeReady>>,
    ) {
        // A second set is a no-op by contract (boot runs once).
        if self
            .connector
            .set(ConnectorReadiness { platforms, handle })
            .is_err()
        {
            tracing::warn!("connector readiness already set; ignoring");
        }
    }

    /// Per connector-ON platform readiness. A platform whose runtime is not
    /// wired reports `NotReady("runtime_not_wired")`.
    pub fn connector_status(&self) -> Vec<ConnectorStatus> {
        let Some(o) = self.connector.get() else {
            return Vec::new();
        };
        o.platforms
            .iter()
            .map(|p| ConnectorStatus {
                platform: p,
                readiness: match &o.handle {
                    Some(h) => h.ready(p),
                    None => connector_runtime::Readiness::NotReady("runtime_not_wired"),
                },
            })
            .collect()
    }
}

/// Capped exponential backoff for [`connect_spine_with_retry`]'s retry
/// delay: 1s, 2s, 4s, 8s, 16s, then `max` thereafter.
fn backoff_for_attempt(attempt: u32, max: Duration) -> Duration {
    let secs = 1u64
        .checked_shl(attempt.saturating_sub(1).min(16))
        .unwrap_or(u64::MAX);
    Duration::from_secs(secs).min(max)
}

/// Connects to the spine with capped exponential backoff, logging every
/// attempt at `WARN` with the attempt number and the error -- never a
/// one-shot probe. Updates `readiness` and `metrics.consumer_loop_running`
/// on every transition. Returns `Some(client)` on success, or `None` once
/// `grace_deadline` has passed without a successful connect -- the caller
/// treats that as fatal (see `run_with_shutdown`'s `process::exit(1)`
/// escalation): a receiver that never connects must never leave the pod
/// looking healthy forever.
///
/// Regression: one-shot valkey probe disabled discord receiver (alpha
/// 2026-10-02).
async fn connect_spine_with_retry(
    cfg: penguin_spine::SpineConfig,
    spine_metrics: Arc<dyn penguin_spine::SpineMetrics>,
    ingest_metrics: &telemetry::IngestMetrics,
    receiver: &'static str,
    readiness: &ReceiverFlag,
    backoff_max: Duration,
    grace_deadline: Instant,
) -> Option<penguin_spine::SpineClient> {
    readiness.mark_enabled();
    let mut attempt: u32 = 0;
    loop {
        attempt += 1;
        ingest_metrics.record_spine_connect_attempt(receiver);
        match penguin_spine::SpineClient::connect(cfg.clone(), spine_metrics.clone()).await {
            Ok(client) => {
                readiness.set_ready(true);
                ingest_metrics.set_consumer_loop_running(receiver, true);
                return Some(client);
            }
            Err(err) => {
                readiness.set_ready(false);
                ingest_metrics.set_consumer_loop_running(receiver, false);
                tracing::warn!(
                    receiver,
                    attempt,
                    error = %err,
                    "spine connect failed, retrying"
                );
                let now = Instant::now();
                if now >= grace_deadline {
                    return None;
                }
                let wait = backoff_for_attempt(attempt, backoff_max)
                    .min(grace_deadline.saturating_duration_since(now));
                tokio::time::sleep(wait).await;
            }
        }
    }
}

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
    // MUST run before anything builds a TLS-capable client (the OTLP
    // exporter `telemetry::init` may construct next, the license/flag
    // HTTPS client, the Valkey/IRC/Discord TLS sockets) -- see
    // `crate::crypto`'s module doc for why two rustls crypto backends
    // linked into this binary otherwise panic on first TLS use.
    crypto::ensure_installed();

    let (_telemetry_guard, prom_registry) = telemetry::init(SERVICE_NAME);

    tracing::info!(
        http_port = config.cli.http_port,
        metrics_port = config.cli.metrics_port,
        "starting {SERVICE_NAME}"
    );

    // The env Discord application serves only this tenant (tenant_discord).
    tenant_discord::set_default_tenant_id(config.cli.default_tenant_id);
    let ingest_metrics = Arc::new(telemetry::register_ingest_metrics(&prom_registry));
    let mut state = http::AppState::new(config.clone(), prom_registry);
    let receiver_readiness = Arc::clone(&state.receiver_readiness);

    // `waddles.core.rust-data-plane` (spec S13.5): OFF (default until
    // validated) means serve /health + /metrics and start nothing below --
    // the Twitch EventSub webhook route (`http::eventsub`) still mounts
    // (ingress never changes), but responds 503 while `state.eventsub` is
    // `None`. `flag_enabled` never performs inline network I/O (see
    // `crate::license`'s module doc), so this check never blocks startup
    // regardless of license/flag-server reachability.
    // A contradictory Twitch config (dynamic flag ON + a static identity, or
    // bad timing) fails loudly even when the data plane below is OFF.
    config::validate_twitch_mode(&config)?;
    config::validate_default_tenant(&config)?;
    // Host selector (spec 2026-11-05 s5): env baselines only, never the flag
    // client. Validated BEFORE any receiver starts; fatal and loud.
    let env_lookup = |k: &str| std::env::var(k).ok();
    let selector = connector_runtime::selector::ConnectorSelector::from_env(&env_lookup)?;
    let webhook_platforms = connector_runtime::selector::parse_webhook_platforms(
        &config.cli.connector_runtime_webhook_platforms,
    )?;
    connector_runtime::selector::validate(&connector_runtime::selector::BootInputs {
        cli: &config.cli,
        selector: &selector,
        webhook_platforms: &webhook_platforms,
        spine_configured: penguin_spine::SpineConfig::from_env().is_ok(),
        lookup: &env_lookup,
    })?;
    let tools_enabled = connector_runtime::selector::tools_enabled(&env_lookup)?;
    let legacy_plan =
        connector_runtime::selector::LegacyPlan::new(&selector, &webhook_platforms, tools_enabled);
    // Webhook env (PR-D call-site contract): parsed strictly even when OFF.
    let webhook_env = Arc::new(connector_runtime::webhook_boot::WebhookEnv::from_lookup(
        &env_lookup,
    )?);
    // The runtime's readiness handle, once (and only if) it is built below.
    // With every connector OFF nothing is built, bound or spawned.
    let mut connector_ready: Option<Arc<dyn connector_runtime::RuntimeReady>> = None;
    let license_client = license::build_license_client();
    license::set_shared_license_client(license_client.clone());
    // Egress detokenizer: installed BEFORE any outbound drain starts. The drain is the only
    // platform egress; it substitutes `{user:<token>}` right before each send.
    egress::install(egress::Egress::from_cli(
        &config.cli,
        license_client.clone(),
        std::env::var("DATABASE_URL").ok(),
    ));
    if license::rust_data_plane_enabled(license_client.as_ref()).await {
        // Schema-v3 producer: resolver is installed BEFORE any receiver can
        // publish. A flag that is ON but misconfigured fails the process
        // loudly -- never a silent downgrade to the v2/static path.
        try_install_scope_resolver(&config, ingest_metrics.clone()).await?;
        // Twitch IRC (primary e2e path), Discord Gateway (secondary), the
        // Twitch outbound relay drain, and the Twitch EventSub webhook
        // receiver each independently no-op with a logged reason when
        // unconfigured -- see each function's own doc comment. Slack/
        // YouTube/Kick receivers, the generic webhook/JWT intake, and D31
        // usage metering are `// TODO(M5)` -- not started here, see this
        // module's doc comment.
        // One shared identity-field DEK provider for every receiver --
        // `ConfiguredDekProvider` is cheaply `Clone` (see its doc comment),
        // so each independently-spawned receiver task gets its own handle
        // onto the same underlying TTL cache / HTTP client.
        let dek_provider = build_dek_provider(&config)?;
        start_bundle_schedules(
            &config,
            &state.metrics,
            ingest_metrics.clone(),
            dek_provider.clone(),
        )
        .await?;

        // Connector service bundle runtime (spec 2026-11-05 PR-E): only when an
        // `CONNECTOR_<P>_ENABLED` baseline is ON. It owns the single executor
        // link + listener; the outbound executor path rides the same link.
        let mut shared_link = None;
        if selector.any_connector() {
            let started = connector_runtime::boot::start_connector(
                &config,
                &selector,
                &webhook_env,
                &mut state,
                ingest_metrics.clone(),
                dek_provider.clone(),
            )
            .await?;
            shared_link = Some(Arc::clone(&started.link));
            connector_ready = Some(started.ready);
        }

        // Outbound drains are owned by `start_outbound` (the
        // `waddles.connector-tools` mode picks executor vs legacy for every
        // platform); the selector's outbound callbacks are therefore no-ops.
        start_outbound(&config, shared_link).await?;
        let mut legacy = RealLegacy {
            config: &config,
            metrics: ingest_metrics.clone(),
            readiness: Arc::clone(&receiver_readiness),
            dek_provider,
            state: &mut state,
        };
        connector_runtime::selector::start_legacy(&legacy_plan, &mut legacy).await?;
    } else {
        if selector.any_connector() {
            // Connector ON but the data plane is OFF: the runtime can never run.
            // Fatal and named, never a silent fallback to nothing.
            return Err(connector_runtime::selector::BootError::Prerequisite(
                license::RUST_DATA_PLANE_FLAG,
            )
            .into());
        }
        // WARN, not INFO: with the flag OFF no outbound drain runs, so
        // `svc_action` refuses every Discord chat.delete/dm.send (no
        // drain-ready key) -- that must be visible to an operator.
        tracing::warn!(
            flag = license::RUST_DATA_PLANE_FLAG,
            "flag is OFF; receive/produce/outbound-drain NOT started -- discord chat.delete/dm.send bundle ops will be refused until it is ON"
        );
    }

    // `/readyz` reports every connector-ON platform (none by default).
    state
        .receiver_readiness
        .set_connector(selector.enabled_platforms(), connector_ready);

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

/// The real legacy starters, driven by
/// [`connector_runtime::selector::start_legacy`].
struct RealLegacy<'a> {
    config: &'a config::Config,
    metrics: Arc<telemetry::IngestMetrics>,
    readiness: Arc<ReceiverReadiness>,
    dek_provider: identity_crypto::ConfiguredDekProvider,
    state: &'a mut http::AppState,
}

impl connector_runtime::selector::LegacyStarters for RealLegacy<'_> {
    fn twitch_inbound(&mut self) -> anyhow::Result<()> {
        start_twitch_inbound(
            self.config,
            self.metrics.clone(),
            Arc::clone(&self.readiness),
            self.dek_provider.clone(),
        )
    }

    fn discord_inbound(&mut self) {
        try_start_discord(
            self.config,
            self.metrics.clone(),
            Arc::clone(&self.readiness),
            self.dek_provider.clone(),
        );
    }

    fn twitch_outbound(&mut self) -> anyhow::Result<()> {
        // Owned by `start_outbound` (executor vs legacy by connector-tools mode).
        Ok(())
    }

    fn discord_outbound(&mut self) {
        // Owned by `start_outbound`.
    }

    async fn twitch_eventsub(&mut self) {
        self.state.eventsub =
            try_build_eventsub_state(self.config, self.metrics.clone(), self.dek_provider.clone())
                .await;
    }
}

/// A fresh shutdown receiver completed by SIGINT/SIGTERM -- the same
/// per-drain pattern the legacy `try_start_*` functions use.
fn shutdown_receiver() -> tokio::sync::oneshot::Receiver<()> {
    let (tx, rx) = tokio::sync::oneshot::channel();
    tokio::spawn(async move {
        shutdown_signal().await;
        let _ = tx.send(());
    });
    rx
}

/// Starts the outbound drains for the path the `waddles.connector-tools`
/// startup flag selects ([`outbound_boot::mode`]). OFF: the legacy drains,
/// exactly as before this flag existed. ON: the generic executor path, where
/// ANY missing/malformed prerequisite is a fatal `Err` -- never a silent
/// downgrade to legacy.
async fn start_outbound(
    config: &config::Config,
    shared_link: Option<Arc<connector::ExecutorLink>>,
) -> anyhow::Result<()> {
    match outbound_boot::mode(&config.cli) {
        outbound_boot::OutboundMode::Executor => {
            outbound_boot::start_shared(&config.cli, shutdown_receiver, shared_link).await
        }
        outbound_boot::OutboundMode::Legacy => {
            if config.cli.dynamic_connections {
                // Per-community credentials: never the process-wide identity.
                try_start_twitch_outbound_percommunity(config)?;
            } else {
                try_start_twitch_outbound(config);
            }
            try_start_discord_outbound(config);
            Ok(())
        }
    }
}

/// Installs the dataplane scope resolver when
/// `waddles.ingest-dynamic-connections` is ON, i.e. its env baseline
/// `INGEST_DYNAMIC_CONNECTIONS=true` (default OFF; the chart turns it on in
/// alpha only). Deliberately NOT read through the `penguin_licensing` client:
/// this service's hardcoded `*.waddles.app` domain bypass ([`license`]) makes
/// that client evaluate EVERY flag ON, so it could never provide a default-OFF
/// gate. Flag OFF is a no-op: every event keeps the unchanged v2/static envelope. Flag ON with a
/// missing machine-JWT bootstrap config, or a plaintext
/// `http://` hub-api outside alpha is an `Err` -- fail loud, never silently
/// fall back.
async fn try_install_scope_resolver(
    config: &config::Config,
    metrics: Arc<telemetry::IngestMetrics>,
) -> anyhow::Result<()> {
    if !config.cli.dynamic_connections {
        tracing::info!(
            flag = scope_resolver::INGEST_DYNAMIC_CONNECTIONS_FLAG,
            "flag is OFF; publishing schema-v2 static-scope envelopes"
        );
        return Ok(());
    }
    let cli = &config.cli;
    if cli.service_jwt_token_endpoint.is_empty() {
        anyhow::bail!(
            "{} is ON but SERVICE_JWT_TOKEN_ENDPOINT is unset: no machine JWT for the resolver",
            scope_resolver::INGEST_DYNAMIC_CONNECTIONS_FLAG
        );
    }
    let hub_grpc_endpoint = std::env::var("HUB_API_GRPC_ENDPOINT")
        .ok()
        .map(|v| v.trim().to_owned())
        .filter(|v| !v.is_empty())
        .ok_or_else(|| {
            anyhow::anyhow!(
                "{} is ON but HUB_API_GRPC_ENDPOINT is unset: no hub-api gRPC endpoint for the resolver",
                scope_resolver::INGEST_DYNAMIC_CONNECTIONS_FLAG
            )
        })?;
    let hub_grpc_ca = std::env::var("HUB_API_GRPC_CA_FILE")
        .ok()
        .map(|v| v.trim().to_owned())
        .filter(|v| !v.is_empty());
    let source = scope_resolver::HubScopeSource::connect(
        &hub_grpc_endpoint,
        &cli.service_jwt_token_endpoint,
        &cli.service_jwt_sa_token_path,
        hub_grpc_ca.as_deref(),
    )
    .await?;
    let resolver = Arc::new(scope_resolver::ScopeResolver::new(
        source,
        scope_resolver::ResolverConfig::default(),
        metrics,
    ));
    if !publish::install_scope_resolver(resolver) {
        anyhow::bail!("scope resolver was already installed");
    }
    tracing::info!(
        flag = scope_resolver::INGEST_DYNAMIC_CONNECTIONS_FLAG,
        "flag is ON; publishing schema-v3 envelopes with resolver-supplied numeric scope"
    );
    Ok(())
}

/// Env baseline gating the APP/SERVICE bundle reconcile scheduler (default OFF until validated).
const BUNDLE_SCHEDULES_ENV: &str = "BUNDLE_SCHEDULES_ENABLED";

/// Starts the APP/SERVICE bundle reconcile scheduler
/// ([`connector::bundle_schedule`]) when `BUNDLE_SCHEDULES_ENABLED=true`. OFF is a logged no-op;
/// ON with any missing prerequisite (machine-JWT bootstrap, binding keyring, spine, Valkey) is an
/// `Err` that fails the process -- never a silent downgrade to "no schedules".
async fn start_bundle_schedules(
    config: &config::Config,
    registry: &prometheus::Registry,
    metrics: Arc<telemetry::IngestMetrics>,
    dek_provider: identity_crypto::ConfiguredDekProvider,
) -> anyhow::Result<()> {
    use connector::bundle_schedule::{
        run_manager, HubScheduleSource, ScheduleRunner, SpineReconcileSink, ValkeyScheduleLease,
    };
    if std::env::var(BUNDLE_SCHEDULES_ENV).as_deref() != Ok("true") {
        tracing::info!(
            env = BUNDLE_SCHEDULES_ENV,
            "flag is OFF; bundle reconcile schedules not started"
        );
        return Ok(());
    }
    let cli = &config.cli;
    if cli.service_jwt_token_endpoint.is_empty() {
        anyhow::bail!("{BUNDLE_SCHEDULES_ENV} is ON but SERVICE_JWT_TOKEN_ENDPOINT is unset");
    }
    let hub_grpc_endpoint = std::env::var("HUB_API_GRPC_ENDPOINT")
        .ok()
        .map(|v| v.trim().to_owned())
        .filter(|v| !v.is_empty())
        .ok_or_else(|| {
            anyhow::anyhow!("{BUNDLE_SCHEDULES_ENV} is ON but HUB_API_GRPC_ENDPOINT is unset")
        })?;
    let hub_grpc_ca = std::env::var("HUB_API_GRPC_CA_FILE")
        .ok()
        .map(|v| v.trim().to_owned())
        .filter(|v| !v.is_empty());
    let keyring = resolve_binding_keyring(config, "bundle schedules").ok_or_else(|| {
        anyhow::anyhow!(
            "bundle schedules need a valid ENVELOPE_BINDING_KEYS + ENVELOPE_BINDING_ACTIVE_KID"
        )
    })?;
    let spine_cfg = penguin_spine::SpineConfig::from_env()
        .map_err(|e| anyhow::anyhow!("spine config unavailable for bundle schedules: {e}"))?;
    let redis_client = outbound::build_redis_client(&spine_cfg).map_err(|e| {
        anyhow::anyhow!(
            "valkey client build failed for bundle schedules ({:?})",
            e.kind()
        )
    })?;
    let spine_metrics: Arc<dyn penguin_spine::SpineMetrics> = metrics.clone();
    let appender = connect_spine_with_retry(
        spine_cfg,
        Arc::clone(&spine_metrics),
        metrics.as_ref(),
        "bundle_schedules",
        &ReceiverFlag::default(),
        Duration::from_secs(cli.spine_connect_backoff_max_secs),
        Instant::now() + Duration::from_secs(cli.spine_connect_grace_secs),
    )
    .await
    .ok_or_else(|| {
        anyhow::anyhow!("bundle schedules: spine did not connect within the grace period")
    })?;
    let lease_conn = redis_client
        .get_multiplexed_async_connection()
        .await
        .map_err(|e| anyhow::anyhow!("bundle schedules: valkey connect failed ({:?})", e.kind()))?;
    let source = HubScheduleSource::connect(
        &hub_grpc_endpoint,
        &cli.service_jwt_token_endpoint,
        &cli.service_jwt_sa_token_path,
        hub_grpc_ca.as_deref(),
    )
    .await?;
    if let Err(e) = connector::reconcile_hook::register_metrics(registry) {
        tracing::warn!(error = %e, "reconcile metrics already registered; reusing");
    }
    let sink = SpineReconcileSink {
        appender: Arc::new(appender),
        metrics: spine_metrics,
        keyring: Arc::new(keyring),
        active_kid: cli.binding_active_kid.clone(),
        dek: Arc::new(dek_provider),
        identity_metrics: metrics.identity_encryption_total.clone(),
    };
    let runner = Arc::new(ScheduleRunner::new(
        ValkeyScheduleLease::new(lease_conn),
        sink,
    ));
    tokio::spawn(run_manager(
        runner,
        source,
        Duration::from_secs(60),
        async {
            let _ = shutdown_receiver().await;
        },
    ));
    tracing::info!(
        env = BUNDLE_SCHEDULES_ENV,
        "bundle reconcile schedules started"
    );
    Ok(())
}

/// Picks the Twitch inbound path from configuration
/// ([`config::validate_twitch_mode`]) and starts it: per-community sessions
/// (flag ON), the single-tenant static dev receiver (flag OFF + complete
/// static env), or nothing. A flag-ON conflict with the static identity, or
/// any dynamic-mode misconfiguration, is an `Err` that fails the process.
fn start_twitch_inbound(
    config: &config::Config,
    metrics: Arc<telemetry::IngestMetrics>,
    readiness: Arc<ReceiverReadiness>,
    dek_provider: identity_crypto::ConfiguredDekProvider,
) -> anyhow::Result<()> {
    let mode = config::validate_twitch_mode(config)?;
    let dek_dynamic = dek_provider.clone();
    dispatch_twitch_mode(
        mode,
        || try_start_twitch_irc(config, metrics.clone(), readiness.clone(), dek_provider),
        || {
            twitch_sessions::boot::try_start_twitch_sessions(
                config,
                metrics.clone(),
                readiness.clone(),
                dek_dynamic,
            )
        },
    )
}

/// Runs exactly the one starter matching `mode` -- `Dynamic` never starts the
/// static receiver, `StaticDev` never starts the manager. Split from
/// [`start_twitch_inbound`] so the dispatch is testable without sockets.
pub fn dispatch_twitch_mode(
    mode: config::TwitchMode,
    static_dev: impl FnOnce(),
    dynamic: impl FnOnce() -> anyhow::Result<()>,
) -> anyhow::Result<()> {
    match mode {
        config::TwitchMode::Dynamic => dynamic(),
        config::TwitchMode::StaticDev => {
            tracing::warn!("single-tenant static identity \u{2014} dev fallback");
            static_dev();
            Ok(())
        }
        config::TwitchMode::Off => {
            tracing::info!("no twitch inbound configured; twitch receiver not started");
            Ok(())
        }
    }
}

/// Attempts to start the Twitch IRC receiver (`crate::ingest::twitch::run`)
/// as its own background task and returns immediately either way -- never
/// blocks or fails `run_with_shutdown`'s caller. Three independent reasons
/// this never starts the receiver, all logged and none an error (the same
/// graceful-degradation contract `core/svc_process::try_start_spine_drain`
/// uses for a missing `SpineConfig`):
/// - `Config::twitch_irc_enabled()` is false (nick/channel/oauth token not
///   all set)
/// - the D30 binding keyring/active-kid is absent or invalid -- minting
///   without a valid keyring is unsafe, never attempted
///   ([`resolve_binding_keyring`])
/// - `penguin_spine::SpineConfig::from_env()` fails (e.g. `VALKEY_URL`
///   unset)
///
/// Once started, the spine connect itself is never one-shot: see
/// [`connect_spine_with_retry`] -- regression: one-shot valkey probe
/// disabled discord receiver (alpha 2026-10-02), fixed identically here for
/// its Twitch IRC sibling.
fn try_start_twitch_irc(
    config: &config::Config,
    metrics: Arc<telemetry::IngestMetrics>,
    readiness: Arc<ReceiverReadiness>,
    dek_provider: identity_crypto::ConfiguredDekProvider,
) {
    if !config.twitch_irc_enabled() {
        tracing::info!(
            "TWITCH_IRC_NICK/_CHANNEL/_OAUTH_TOKEN not fully set; twitch irc receiver not started"
        );
        return;
    }
    let Some(oauth_token) = config
        .twitch_irc_oauth_token
        .as_ref()
        .map(|s| s.expose().to_string())
    else {
        return; // unreachable given twitch_irc_enabled() above; fail-safe.
    };
    let keyring = match resolve_binding_keyring(config, "twitch irc") {
        Some(ring) => ring,
        None => return,
    };

    let spine_cfg = match penguin_spine::SpineConfig::from_env() {
        Ok(cfg) => cfg,
        Err(err) => {
            tracing::warn!(error = %err, "spine config unavailable; twitch irc receiver not started");
            return;
        }
    };

    let irc_cfg = ingest::twitch::irc_config(
        &config.cli.twitch_irc_host,
        config.cli.twitch_irc_port,
        &config.cli.twitch_irc_nick,
        &config.cli.twitch_irc_channel,
        &oauth_token,
        config.cli.twitch_irc_use_tls,
    );
    let channel = config.cli.twitch_irc_channel.clone();
    let nick = config.cli.twitch_irc_nick.clone();
    let active_kid = config.cli.binding_active_kid.clone();
    let scope = config.ingest_scope();
    let backoff_max = Duration::from_secs(config.cli.spine_connect_backoff_max_secs);
    let grace = Duration::from_secs(config.cli.spine_connect_grace_secs);

    let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel();
    tokio::spawn(async move {
        shutdown_signal().await;
        let _ = shutdown_tx.send(());
    });
    tokio::spawn(async move {
        let spine_metrics: Arc<dyn penguin_spine::SpineMetrics> = metrics.clone();
        let grace_deadline = Instant::now() + grace;
        let Some(appender) = connect_spine_with_retry(
            spine_cfg,
            spine_metrics,
            metrics.as_ref(),
            "twitch_irc",
            &readiness.twitch_irc,
            backoff_max,
            grace_deadline,
        )
        .await
        else {
            tracing::error!(
                grace_secs = grace.as_secs(),
                "spine connect still failing after grace period; exiting so Kubernetes restarts \
                 this pod visibly (twitch irc receiver never started)"
            );
            std::process::exit(1);
        };
        ingest::twitch::run(
            irc_cfg,
            &channel,
            &nick,
            &appender,
            metrics.as_ref(),
            &keyring,
            &active_kid,
            &scope,
            &dek_provider,
            &metrics.identity_encryption_total,
            shutdown_rx,
        )
        .await;
    });
}

/// Attempts to start the Discord Gateway receiver
/// (`crate::ingest::discord::run`) as its own background task and returns
/// immediately either way -- same three-reason graceful-degradation
/// contract as [`try_start_twitch_irc`] (bot token, binding keyring, spine
/// config). The spine connect itself retries with backoff rather than
/// giving up after one attempt -- regression: one-shot valkey probe
/// disabled discord receiver, leaving the pod `Running 1/1` forever (alpha
/// 2026-10-02). See [`connect_spine_with_retry`].
fn try_start_discord(
    config: &config::Config,
    metrics: Arc<telemetry::IngestMetrics>,
    readiness: Arc<ReceiverReadiness>,
    dek_provider: identity_crypto::ConfiguredDekProvider,
) {
    if !config.discord_enabled() {
        tracing::info!("DISCORD_BOT_TOKEN not set; discord gateway receiver not started");
        return;
    }
    let Some(bot_token) = config
        .discord_bot_token
        .as_ref()
        .map(|s| s.expose().to_string())
    else {
        return; // unreachable given discord_enabled() above; fail-safe.
    };
    let keyring = match resolve_binding_keyring(config, "discord gateway") {
        Some(ring) => ring,
        None => return,
    };

    let spine_cfg = match penguin_spine::SpineConfig::from_env() {
        Ok(cfg) => cfg,
        Err(err) => {
            tracing::warn!(error = %err, "spine config unavailable; discord gateway receiver not started");
            return;
        }
    };

    let mut gateway_cfg = penguin_connector_discord::gateway::GatewayConfig::new(bot_token);
    if !config.cli.discord_gateway_url.is_empty() {
        gateway_cfg.gateway_url = config.cli.discord_gateway_url.clone();
    }
    let active_kid = config.cli.binding_active_kid.clone();
    let scope = config.ingest_scope();
    let backoff_max = Duration::from_secs(config.cli.spine_connect_backoff_max_secs);
    let grace = Duration::from_secs(config.cli.spine_connect_grace_secs);

    let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel();
    tokio::spawn(async move {
        shutdown_signal().await;
        let _ = shutdown_tx.send(());
    });
    tokio::spawn(async move {
        let spine_metrics: Arc<dyn penguin_spine::SpineMetrics> = metrics.clone();
        let grace_deadline = Instant::now() + grace;
        // regression: one-shot valkey probe disabled discord receiver (alpha 2026-10-02)
        let Some(appender) = connect_spine_with_retry(
            spine_cfg,
            spine_metrics,
            metrics.as_ref(),
            "discord_gateway",
            &readiness.discord,
            backoff_max,
            grace_deadline,
        )
        .await
        else {
            tracing::error!(
                grace_secs = grace.as_secs(),
                "spine connect still failing after grace period; exiting so Kubernetes restarts \
                 this pod visibly (discord gateway receiver never started)"
            );
            std::process::exit(1);
        };
        ingest::discord::run(
            gateway_cfg,
            &appender,
            metrics.as_ref(),
            &keyring,
            &active_kid,
            &scope,
            &dek_provider,
            &metrics.identity_encryption_total,
            shutdown_rx,
            metrics.clone(),
        )
        .await;
    });
}

/// Attempts to start the Twitch outbound relay drain
/// (`crate::outbound::run`) as its own background task and returns
/// immediately either way. Two independent reasons this never starts,
/// both logged and neither an error: the Twitch identity
/// (nick/oauth token) is not configured (the outbound sender reuses the
/// receive side's own credential -- `crate::outbound`'s "Credential
/// resolution" doc), or `penguin_spine::SpineConfig::from_env()` fails.
/// Unlike the two receivers above, no binding keyring is needed here --
/// the outbound drain never mints or `XADD`s an envelope, it only relays a
/// chat send.
///
/// **Known gap, out of scope for this fix** (not named in the alpha
/// 2026-10-02 incident this module's other two `try_start_*` functions
/// fix): `outbound::run`'s own initial Valkey connect is still a one-shot
/// `?` with no retry -- see that function's doc. `ReceiverReadiness::
/// twitch_outbound` is therefore never marked enabled here and always
/// reports `ok` on `/readyz`.
fn try_start_twitch_outbound(config: &config::Config) {
    let Some(oauth_token) = config
        .twitch_irc_oauth_token
        .as_ref()
        .map(|s| s.expose().to_string())
    else {
        tracing::info!("TWITCH_IRC_OAUTH_TOKEN not set; twitch outbound relay drain not started");
        return;
    };
    if config.cli.twitch_irc_nick.is_empty() {
        tracing::info!("TWITCH_IRC_NICK not set; twitch outbound relay drain not started");
        return;
    }

    let spine_cfg = match penguin_spine::SpineConfig::from_env() {
        Ok(cfg) => cfg,
        Err(err) => {
            tracing::warn!(error = %err, "spine config unavailable; twitch outbound relay drain not started");
            return;
        }
    };

    let endpoint = outbound::TwitchIrcEndpoint {
        host: config.cli.twitch_irc_host.clone(),
        port: config.cli.twitch_irc_port,
        use_tls: config.cli.twitch_irc_use_tls,
    };
    // LEGACY single-identity path: only reachable while
    // `waddles.ingest-dynamic-connections` is OFF (see the call site).
    let creds = outbound_creds::StaticCredentialSource::new(
        config.cli.twitch_irc_nick.clone(),
        oauth_token,
    );

    let pin = resolve_outbound_pin_keys("twitch");
    outbound_boot::record_path(outbound_boot::OutboundMode::Legacy, "twitch");
    let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel();
    tokio::spawn(async move {
        shutdown_signal().await;
        let _ = shutdown_tx.send(());
    });
    tokio::spawn(async move {
        if let Err(err) = outbound::run(&spine_cfg, endpoint, creds, pin, shutdown_rx).await {
            tracing::error!(error = %err, "twitch outbound relay drain exited");
        }
    });
}

/// Starts the Twitch outbound relay drain with PER-COMMUNITY credentials
/// (`waddles.ingest-dynamic-connections` ON): each send resolves the entry's
/// community connection through hub-api (`connection_resolver`), never caches
/// the secret, and never falls back to `TWITCH_IRC_OAUTH_TOKEN`. Every
/// misconfiguration is a fatal `Err` (the process must not run with a drain
/// that would silently use another identity or nothing at all).
fn try_start_twitch_outbound_percommunity(config: &config::Config) -> anyhow::Result<()> {
    let cli = &config.cli;
    twitch_sessions::boot::validate_dynamic_prereqs(cli, publish::scope_resolver_installed())?;
    if config.twitch_irc_oauth_token.is_some() {
        tracing::warn!(
            "TWITCH_IRC_OAUTH_TOKEN is set but IGNORED: per-community outbound credentials \
             are resolved per send; the process-wide token is never used as a fallback"
        );
    }
    let spine_cfg = penguin_spine::SpineConfig::from_env()
        .map_err(|e| anyhow::anyhow!("spine config unavailable for twitch outbound: {e}"))?;
    let resolver = connection_resolver::ConnectionResolver::new(
        cli.resolver_config()?,
        std::sync::Arc::new(twitch_sessions::boot::ResolverTelemetry),
    )
    .map_err(|e| anyhow::anyhow!("connection resolver unavailable: {e}"))?;
    let endpoint = outbound::TwitchIrcEndpoint {
        host: cli.twitch_irc_host.clone(),
        port: cli.twitch_irc_port,
        use_tls: cli.twitch_irc_use_tls,
    };
    let creds = outbound_creds::ResolverTwitchCredentials::new(resolver);
    let pin = resolve_outbound_pin_keys("twitch");
    outbound_boot::record_path(outbound_boot::OutboundMode::Legacy, "twitch");
    let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel();
    tokio::spawn(async move {
        shutdown_signal().await;
        let _ = shutdown_tx.send(());
    });
    tokio::spawn(async move {
        if let Err(err) = outbound::run(&spine_cfg, endpoint, creds, pin, shutdown_rx).await {
            tracing::error!(error = %err, "twitch outbound relay drain exited");
        }
    });
    Ok(())
}

/// Attempts to start the Discord outbound drain (`crate::outbound::
/// run_discord`): executes `chat.send`/`chat.delete`/`dm.send` queued by
/// `svc_action` over the bot-token REST client, and advertises readiness
/// (`outbound::DISCORD_DRAIN_READY_KEY`) while it runs. Never starts (and
/// never panics) when `DISCORD_BOT_TOKEN` is unset or the spine/Valkey config
/// is unavailable -- but each of those is logged at WARN/ERROR, never INFO:
/// without the drain `svc_action` refuses every Discord `chat.delete`/
/// `dm.send` (no readiness key), so an operator must be able to see why. The
/// token is read from the same `DISCORD_BOT_TOKEN` the Gateway receiver uses
/// and is never logged.
fn try_start_discord_outbound(config: &config::Config) {
    let Some(token) = config.discord_bot_token.clone() else {
        tracing::warn!(
            "DISCORD_BOT_TOKEN not set; discord outbound drain NOT started -- bundle chat.delete/dm.send ops (e.g. !secret) will be refused until it is configured"
        );
        return;
    };
    let spine_cfg = match penguin_spine::SpineConfig::from_env() {
        Ok(cfg) => cfg,
        Err(err) => {
            tracing::error!(error = %err, "spine config unavailable; discord outbound drain NOT started -- bundle chat.delete/dm.send ops will be refused");
            return;
        }
    };
    let rest = match discord_rest::DiscordRestClient::new(token, discord_rest::DEFAULT_API_BASE) {
        Ok(rest) => rest,
        Err(err) => {
            tracing::error!(error = %err, "discord REST client build failed; discord outbound drain NOT started -- bundle chat.delete/dm.send ops will be refused");
            return;
        }
    };
    let pin = resolve_outbound_pin_keys("discord");
    outbound_boot::record_path(outbound_boot::OutboundMode::Legacy, "discord");
    let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel();
    tokio::spawn(async move {
        shutdown_signal().await;
        let _ = shutdown_tx.send(());
    });
    tokio::spawn(async move {
        if let Err(err) = outbound::run_discord(&spine_cfg, &rest, pin, shutdown_rx).await {
            tracing::error!(error = %err, "discord outbound drain exited; bundle chat.delete/dm.send ops will be refused once its readiness key expires");
        }
    });
}

/// Attempts to build the Twitch EventSub webhook receiver's dependencies
/// (`crate::http::eventsub::EventSubState`) and returns `None` (never an
/// error) on any of three independent, logged reasons this doesn't start --
/// the same graceful-degradation contract as [`try_start_twitch_irc`]/
/// [`try_start_discord`] (binding keyring, spine config), plus a Valkey
/// connection for the dedup guard/revocation sink specifically:
/// - the D30 binding keyring/active-kid is absent or invalid
///   ([`resolve_binding_keyring`])
/// - `penguin_spine::SpineConfig::from_env()` fails
/// - the Valkey connection this function separately opens for
///   `ingest::twitch_eventsub::{RedisReplayGuard, RedisRevocationSink}`
///   fails to connect (a fresh connection, not shared with the IRC/Discord
///   receivers' own `SpineClient`s or the outbound relay drain's list
///   connection)
///
/// Unlike [`try_start_twitch_irc`]/[`try_start_discord`], this never spawns
/// a background task -- the webhook route (`http::eventsub::router`) is
/// already mounted unconditionally; this function only decides whether
/// `AppState::eventsub` is `Some` (process requests) or `None` (503 every
/// request).
async fn try_build_eventsub_state(
    config: &config::Config,
    metrics: Arc<telemetry::IngestMetrics>,
    dek_provider: identity_crypto::ConfiguredDekProvider,
) -> Option<Arc<http::eventsub::EventSubState>> {
    let keyring = resolve_binding_keyring(config, "twitch eventsub")?;

    let spine_cfg = match penguin_spine::SpineConfig::from_env() {
        Ok(cfg) => cfg,
        Err(err) => {
            tracing::warn!(error = %err, "spine config unavailable; twitch eventsub receiver not started");
            return None;
        }
    };

    let spine_metrics: Arc<dyn penguin_spine::SpineMetrics> = metrics.clone();
    let appender = match penguin_spine::SpineClient::connect(spine_cfg.clone(), spine_metrics).await
    {
        Ok(client) => client,
        Err(err) => {
            tracing::error!(error = %err, "spine connect failed; twitch eventsub receiver not started");
            return None;
        }
    };

    let redis_client = match outbound::build_redis_client(&spine_cfg) {
        Ok(client) => client,
        Err(err) => {
            tracing::warn!(error_kind = ?err.kind(), "valkey client build failed; twitch eventsub receiver not started");
            return None;
        }
    };
    let conn = match redis_client.get_multiplexed_async_connection().await {
        Ok(conn) => conn,
        Err(err) => {
            tracing::warn!(error_kind = ?err.kind(), "valkey connect failed; twitch eventsub receiver not started");
            return None;
        }
    };

    let resolver =
        ingest::twitch_eventsub::EnvSecretResolver::from_env(config.twitch_eventsub_secret.clone());
    let dedup = ingest::twitch_eventsub::RedisReplayGuard::new(
        conn.clone(),
        ingest::twitch_eventsub::DEDUP_TTL,
    );
    let revocation = ingest::twitch_eventsub::RedisRevocationSink::new(conn);

    tracing::info!("twitch eventsub webhook receiver configured");
    Some(Arc::new(http::eventsub::EventSubState {
        resolver,
        dedup,
        revocation,
        appender,
        metrics,
        keyring,
        active_kid: config.cli.binding_active_kid.clone(),
        scope: config.ingest_scope(),
        dek_provider,
    }))
}

/// Builds this runner's identity-field [`identity_crypto::DekProvider`] --
/// shared across every receiver via `try_start_*` (see call sites below).
///
/// Production posture (`config.ingest_dev_kek` unset): [`identity_crypto::
/// ConfiguredDekProvider::hub`] over a [`hub_client::HubClient`] holding the
/// `key:stream-dek:read` scope. The machine JWT is minted/cached/refreshed by
/// `service_auth::MachineJwtClient` from the pod's projected ServiceAccount token, the DEK
/// is fetched over hub-api's internal gRPC `KeyService` (sealed to a per-call ephemeral
/// X25519 key), and every failure fails closed. The connection is lazy: the process starts
/// (and reports readiness) before hub-api is reachable, and events are held -- never written
/// plaintext -- until it is.
///
/// Dev/alpha fallback (`config.ingest_dev_kek` set): [`identity_crypto::
/// LocalDevDekProvider`], hard-gated to [`config::Config::
/// deployment_environment`] `alpha`/`local` regardless of whether the dev KEK is configured
/// -- refused otherwise (logged loudly), in which case the broker is used, never a silently
/// accepted dev KEK outside alpha/local.
///
/// # Errors
/// The broker path needs `HUB_API_GRPC_ENDPOINT` (https), `SERVICE_JWT_TOKEN_ENDPOINT` and a
/// well-formed `SPIFFE_ID` (its service segment is the seal-context name);
/// missing/plaintext values are fatal at boot -- a pod that could only ever hold events must
/// not start looking healthy.
fn build_dek_provider(
    config: &config::Config,
) -> anyhow::Result<identity_crypto::ConfiguredDekProvider> {
    use identity_crypto::{ConfiguredDekProvider, LocalDevDekProvider, STREAM_DEK_SCOPE};

    if config.ingest_dev_kek.is_some() {
        match LocalDevDekProvider::new(config.deployment_environment(), "INGEST_DEV_KEK") {
            Ok(provider) => return Ok(ConfiguredDekProvider::local_dev(provider)),
            Err(err) => {
                tracing::error!(
                    error = %err,
                    "INGEST_DEV_KEK is set but LocalDevDekProvider was refused; \
                     using the hub-api key broker (never a silent dev-key fallback \
                     outside alpha/local)"
                );
            }
        }
    }
    let cli = &config.cli;
    let endpoint = cli.hub_api_grpc_endpoint.trim();
    if endpoint.is_empty() || cli.service_jwt_token_endpoint.trim().is_empty() {
        anyhow::bail!(
            "identity-field encryption needs HUB_API_GRPC_ENDPOINT and \
             SERVICE_JWT_TOKEN_ENDPOINT to fetch tenant DEKs from hub-api's key broker; \
             refusing to start a data plane that could only hold every event"
        );
    }
    if !endpoint.to_ascii_lowercase().starts_with("https://") {
        anyhow::bail!(
            "HUB_API_GRPC_ENDPOINT must be https:// (tenant DEK delivery is never plaintext)"
        );
    }
    let ca =
        (!cli.hub_api_grpc_ca_file.trim().is_empty()).then_some(cli.hub_api_grpc_ca_file.as_str());
    let client = hub_client::HubClient::connect_lazy(
        endpoint,
        cli.service_jwt_token_endpoint.clone(),
        cli.service_jwt_sa_token_path.clone(),
        STREAM_DEK_SCOPE,
        ca,
    )
    .map_err(|e| anyhow::anyhow!("key broker client unavailable: {e}"))?;
    tracing::info!(
        endpoint = %endpoint,
        scope = STREAM_DEK_SCOPE,
        "identity-field DEKs are fetched from hub-api's KeyService"
    );
    ConfiguredDekProvider::hub_for_spiffe_id(Arc::new(client), &cli.spiffe_id).map_err(|e| {
        anyhow::anyhow!(
            "{e}: SPIFFE_ID must be this pod's spiffe://penguintech.io/<env>/<service> id \
             (the stream-DEK seal context uses its service segment; a mismatch with hub-api \
             would fail every unseal)"
        )
    })
}

/// Shared keyring-resolution step for [`try_start_twitch_irc`]/
/// [`try_start_discord`]: `None` (unconfigured), a parse error, or an
/// empty `ENVELOPE_BINDING_ACTIVE_KID` are all logged and treated as "this
/// receiver does not start" -- minting without a valid, named active key
/// is never attempted.
fn resolve_binding_keyring(
    config: &config::Config,
    receiver: &str,
) -> Option<penguin_spine::KeyRing> {
    if config.cli.binding_active_kid.is_empty() {
        tracing::warn!(
            receiver,
            "ENVELOPE_BINDING_ACTIVE_KID not set; receiver not started"
        );
        return None;
    }
    match config.binding_keyring() {
        Some(Ok(ring)) => Some(ring),
        Some(Err(err)) => {
            tracing::warn!(receiver, error = %err, "ENVELOPE_BINDING_KEYS invalid; receiver not started");
            None
        }
        None => {
            tracing::warn!(
                receiver,
                "ENVELOPE_BINDING_KEYS not set; receiver not started"
            );
            None
        }
    }
}

/// Loads the outbound `pin_sig` verification keys from the DEDICATED
/// `OUTBOUND_PIN_KEYS` (never the shared `ENVELOPE_BINDING_KEYS` ring).
/// Missing/invalid keys are logged at ERROR and yield `None`: the drain still
/// runs but then refuses EVERY op (`pin_key_unconfigured`) -- fail closed and
/// loud, never accept unverified.
fn resolve_outbound_pin_keys(platform: &str) -> Option<outbound_pin::PinKeys> {
    let Ok(raw) = std::env::var("OUTBOUND_PIN_KEYS") else {
        tracing::error!(
            platform,
            "OUTBOUND_PIN_KEYS not set; outbound drain will REFUSE every op (pin_key_unconfigured)"
        );
        return None;
    };
    match outbound_pin::PinKeys::parse(&raw, None) {
        Ok(keys) => Some(keys),
        Err(err) => {
            tracing::error!(platform, error = %err, "OUTBOUND_PIN_KEYS unusable; drain will REFUSE every op");
            None
        }
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
    use clap::Parser;
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

    fn base_config() -> config::Config {
        let cli = crate::config::CliConfig::parse_from(["svc-ingest"]);
        config::Config {
            cli,
            twitch_irc_oauth_token: None,
            discord_bot_token: None,
            envelope_binding_keys: None,
            twitch_eventsub_secret: None,
            ingest_dev_kek: None,
            waddles_env: "production".to_string(),
        }
    }

    /// A production-posture config whose broker prerequisites are satisfied except `SPIFFE_ID`.
    fn broker_config(spiffe_id: &str) -> config::Config {
        let mut config = base_config();
        config.cli.hub_api_grpc_endpoint = "https://hub-api:50204".to_string();
        config.cli.service_jwt_token_endpoint = "https://hub-api:50204".to_string();
        config.cli.hub_api_grpc_ca_file = String::new();
        config.cli.spiffe_id = spiffe_id.to_string();
        config
    }

    #[tokio::test]
    async fn dek_provider_boot_fails_loud_without_a_valid_spiffe_id() {
        for bad in [
            "",
            "svc-ingest",
            "spiffe://penguintech.io/svc-ingest",
            "spiffe://evil.example/alpha/svc-ingest",
        ] {
            let err = build_dek_provider(&broker_config(bad))
                .err()
                .unwrap_or_else(|| panic!("booted with SPIFFE_ID {bad:?}"));
            assert!(err.to_string().contains("SPIFFE_ID"), "{err}");
        }
    }

    #[tokio::test]
    async fn dek_provider_boots_with_a_valid_spiffe_id() {
        assert!(
            build_dek_provider(&broker_config("spiffe://penguintech.io/alpha/svc-gateway")).is_ok()
        );
    }

    fn test_readiness() -> Arc<ReceiverReadiness> {
        Arc::new(ReceiverReadiness::new())
    }

    fn test_ingest_metrics() -> Arc<telemetry::IngestMetrics> {
        let registry = prometheus::Registry::new();
        Arc::new(telemetry::register_ingest_metrics(&registry))
    }

    /// Capped exponential sequence identical to `svc_action`/`svc_process`'s
    /// own `backoff_for_attempt` -- 1s, 2s, 4s, 8s, 16s, then pinned at
    /// `max` thereafter.
    #[test]
    fn backoff_for_attempt_doubles_then_caps_at_max() {
        let max = Duration::from_secs(30);
        assert_eq!(backoff_for_attempt(1, max), Duration::from_secs(1));
        assert_eq!(backoff_for_attempt(2, max), Duration::from_secs(2));
        assert_eq!(backoff_for_attempt(5, max), Duration::from_secs(16));
        assert_eq!(backoff_for_attempt(6, max), max);
        assert_eq!(backoff_for_attempt(1000, max), max);
    }

    fn unreachable_spine_cfg() -> penguin_spine::SpineConfig {
        // A malformed URL fails `SpineClient::connect` immediately on parse
        // -- no real network attempt, no delay -- so `grace_deadline`
        // already-expired tests below return deterministically fast.
        penguin_spine::SpineConfig {
            valkey_url: "not a valid url".to_string(),
            valkey_username: None,
            valkey_password: None,
            valkey_ca_file: std::path::PathBuf::from("/nonexistent-ca.crt"),
            security_transport_tls: false,
            security_transport_auth: false,
            consumer_id: "test".to_string(),
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

    /// `grace_deadline` already in the past: exactly one connect attempt,
    /// then the grace-exhaustion path returns `None` without ever sleeping
    /// -- the escalation path `run_with_shutdown` treats as fatal.
    #[tokio::test]
    async fn connect_spine_with_retry_returns_none_once_grace_is_exhausted() {
        let metrics = test_ingest_metrics();
        let spine_metrics: Arc<dyn penguin_spine::SpineMetrics> = metrics.clone();
        let readiness = ReceiverFlag::default();
        readiness.set_ready(true); // prove it flips back to `false` on failure.

        let result = connect_spine_with_retry(
            unreachable_spine_cfg(),
            spine_metrics,
            metrics.as_ref(),
            "test_receiver",
            &readiness,
            Duration::from_secs(30),
            Instant::now(), // already expired
        )
        .await;

        assert!(result.is_none());
        assert!(readiness.is_enabled());
        assert!(!readiness.is_ready());
    }

    /// `grace_deadline` slightly in the future: the loop retries at least
    /// once (bounded by the tiny `backoff_max`) before the deadline passes,
    /// proving the retry path itself runs, not just the single-attempt
    /// exhaustion case above.
    #[tokio::test]
    async fn connect_spine_with_retry_retries_until_grace_expires() {
        let metrics = test_ingest_metrics();
        let spine_metrics: Arc<dyn penguin_spine::SpineMetrics> = metrics.clone();
        let readiness = ReceiverFlag::default();

        let grace_deadline = Instant::now() + Duration::from_millis(30);
        let result = tokio::time::timeout(
            Duration::from_secs(5),
            connect_spine_with_retry(
                unreachable_spine_cfg(),
                spine_metrics,
                metrics.as_ref(),
                "test_receiver",
                &readiness,
                Duration::from_millis(5),
                grace_deadline,
            ),
        )
        .await
        .expect("must not hang past the 5s test timeout");

        assert!(result.is_none());
    }

    /// A [`identity_crypto::ConfiguredDekProvider`] safe to construct in
    /// tests that never actually resolve a DEK (every `try_start_*`
    /// no-op-before-spawn test below returns before the spawned task would
    /// call `get_dek`).
    fn test_dek_provider() -> identity_crypto::ConfiguredDekProvider {
        let client = hub_client::HubClient::connect_lazy(
            "https://hub-api:50204",
            "https://hub-api:50204",
            "/nonexistent/sa-token",
            identity_crypto::STREAM_DEK_SCOPE,
            None,
        )
        .expect("lazy client never dials");
        identity_crypto::ConfiguredDekProvider::hub_for_spiffe_id(
            Arc::new(client),
            "spiffe://penguintech.io/alpha/svc-ingest",
        )
        .expect("well-formed test SPIFFE id")
    }

    #[tokio::test]
    async fn try_start_twitch_irc_noop_when_not_configured() {
        // No `tracing` subscriber installed in this test (see
        // `run.rs`'s own doc on why only one test per binary calls
        // `telemetry::init`) -- `tracing::info!`/`warn!` are harmless
        // no-ops without one.
        try_start_twitch_irc(
            &base_config(),
            test_ingest_metrics(),
            test_readiness(),
            test_dek_provider(),
        );
    }

    #[tokio::test]
    async fn try_start_discord_noop_when_not_configured() {
        try_start_discord(
            &base_config(),
            test_ingest_metrics(),
            test_readiness(),
            test_dek_provider(),
        );
    }

    #[tokio::test]
    async fn try_start_twitch_outbound_noop_when_not_configured() {
        try_start_twitch_outbound(&base_config());
    }

    #[tokio::test]
    async fn try_start_twitch_irc_noop_when_binding_keyring_missing() {
        let mut config = base_config();
        config.cli.twitch_irc_nick = "waddlebot".to_string();
        config.cli.twitch_irc_channel = "somechannel".to_string();
        config.twitch_irc_oauth_token = Some(crate::config::Secret::new("test-token"));
        // envelope_binding_keys stays None -> must not start.
        try_start_twitch_irc(
            &config,
            test_ingest_metrics(),
            test_readiness(),
            test_dek_provider(),
        );
    }

    #[tokio::test]
    async fn try_start_discord_noop_when_binding_active_kid_missing() {
        let mut config = base_config();
        config.discord_bot_token = Some(crate::config::Secret::new("test-token"));
        config.envelope_binding_keys = Some(crate::config::Secret::new("k1:aabbcc"));
        // binding_active_kid stays "" -> must not start.
        try_start_discord(
            &config,
            test_ingest_metrics(),
            test_readiness(),
            test_dek_provider(),
        );
    }

    /// regression: p6a-t6b -- per-community mode with its prerequisites
    /// missing is a fatal `Err`, never a silent start on the static token.
    #[tokio::test]
    async fn percommunity_outbound_misconfig_is_fatal_never_static_fallback() {
        let mut config = base_config();
        config.cli.dynamic_connections = true;
        config.twitch_irc_oauth_token = Some(crate::config::Secret::new("test-token"));
        config.cli.twitch_irc_nick = "waddlebot".to_string();
        assert!(try_start_twitch_outbound_percommunity(&config).is_err());
    }

    #[tokio::test]
    async fn try_start_twitch_outbound_noop_when_nick_missing() {
        let mut config = base_config();
        config.twitch_irc_oauth_token = Some(crate::config::Secret::new("test-token"));
        // cli.twitch_irc_nick stays "" -> must not start.
        try_start_twitch_outbound(&config);
    }

    #[tokio::test]
    async fn try_start_twitch_irc_noop_when_spine_config_unavailable() {
        let _guard = ENV_LOCK.lock().unwrap();
        // SAFETY: serialized by ENV_LOCK above.
        unsafe {
            std::env::remove_var("VALKEY_URL");
            std::env::remove_var("REDIS_URL");
        }
        let mut config = base_config();
        config.cli.twitch_irc_nick = "waddlebot".to_string();
        config.cli.twitch_irc_channel = "somechannel".to_string();
        config.cli.binding_active_kid = "k1".to_string();
        config.twitch_irc_oauth_token = Some(crate::config::Secret::new("test-token"));
        config.envelope_binding_keys = Some(crate::config::Secret::new(
            "k1:0102030405060708090a0b0c0d0e0f10",
        ));
        try_start_twitch_irc(
            &config,
            test_ingest_metrics(),
            test_readiness(),
            test_dek_provider(),
        );
    }

    #[tokio::test]
    async fn try_start_twitch_outbound_noop_when_spine_config_unavailable() {
        let _guard = ENV_LOCK.lock().unwrap();
        // SAFETY: serialized by ENV_LOCK above.
        unsafe {
            std::env::remove_var("VALKEY_URL");
            std::env::remove_var("REDIS_URL");
        }
        let mut config = base_config();
        config.cli.twitch_irc_nick = "waddlebot".to_string();
        config.twitch_irc_oauth_token = Some(crate::config::Secret::new("test-token"));
        try_start_twitch_outbound(&config);
    }

    #[tokio::test]
    async fn try_start_twitch_irc_spawns_when_everything_is_valid() {
        // Mirrors `core/svc_process`'s own
        // `try_start_spine_drain_spawns_when_spine_config_is_valid`: a
        // syntactically valid, TLS-required URL plus a password satisfies
        // `SpineConfig::validate`, but nothing is listening on port 1 (a
        // privileged port, refused immediately) -- exercises the spawn
        // path end to end (including the connect-failure log line)
        // without a live Valkey. Guard is dropped before the `.await`
        // below (clippy `await_holding_lock`) -- the env vars only need to
        // be set for the duration of this synchronous call.
        //
        // `crypto::ensure_installed()` first, defense in depth: this test
        // triggers a real TLS-capable `SpineClient::connect` (which has
        // its own internal installation, per `Cargo.toml`'s comment) --
        // explicit here too so this test never depends on execution order
        // relative to the `license::tests` in the same `--lib` binary.
        crypto::ensure_installed();
        {
            let _guard = ENV_LOCK.lock().unwrap();
            // SAFETY: serialized by ENV_LOCK above.
            unsafe {
                std::env::set_var("VALKEY_URL", "rediss://127.0.0.1:1/");
                std::env::set_var("VALKEY_PASSWORD", "test-valkey-pass");
            }
            let mut config = base_config();
            config.cli.twitch_irc_nick = "waddlebot".to_string();
            config.cli.twitch_irc_channel = "somechannel".to_string();
            config.cli.binding_active_kid = "k1".to_string();
            config.twitch_irc_oauth_token = Some(crate::config::Secret::new("test-token"));
            config.envelope_binding_keys = Some(crate::config::Secret::new(
                "k1:0102030405060708090a0b0c0d0e0f10",
            ));
            try_start_twitch_irc(
                &config,
                test_ingest_metrics(),
                test_readiness(),
                test_dek_provider(),
            );
            unsafe {
                std::env::remove_var("VALKEY_URL");
                std::env::remove_var("VALKEY_PASSWORD");
            }
        }
        // Real (not paused) sleep: lets the spawned tasks actually run on
        // this same current-thread test runtime and reach their
        // connect-failure log line before the runtime is torn down.
        tokio::time::sleep(std::time::Duration::from_secs(2)).await;
    }

    #[tokio::test]
    async fn try_start_discord_spawns_when_everything_is_valid() {
        crypto::ensure_installed();
        {
            let _guard = ENV_LOCK.lock().unwrap();
            // SAFETY: serialized by ENV_LOCK above.
            unsafe {
                std::env::set_var("VALKEY_URL", "rediss://127.0.0.1:1/");
                std::env::set_var("VALKEY_PASSWORD", "test-valkey-pass");
            }
            let mut config = base_config();
            config.discord_bot_token = Some(crate::config::Secret::new("test-token"));
            config.cli.binding_active_kid = "k1".to_string();
            config.envelope_binding_keys = Some(crate::config::Secret::new(
                "k1:0102030405060708090a0b0c0d0e0f10",
            ));
            try_start_discord(
                &config,
                test_ingest_metrics(),
                test_readiness(),
                test_dek_provider(),
            );
            unsafe {
                std::env::remove_var("VALKEY_URL");
                std::env::remove_var("VALKEY_PASSWORD");
            }
        }
        tokio::time::sleep(std::time::Duration::from_secs(2)).await;
    }

    #[tokio::test]
    async fn try_start_discord_outbound_noop_when_not_configured() {
        try_start_discord_outbound(&base_config());
    }

    #[tokio::test]
    async fn try_start_discord_outbound_spawns_when_everything_is_valid() {
        crypto::ensure_installed();
        {
            let _guard = ENV_LOCK.lock().unwrap();
            // SAFETY: serialized by ENV_LOCK above.
            unsafe {
                std::env::set_var("VALKEY_URL", "rediss://127.0.0.1:1/");
                std::env::set_var("VALKEY_PASSWORD", "test-valkey-pass");
            }
            let mut config = base_config();
            config.discord_bot_token = Some(crate::config::Secret::new("test-token"));
            try_start_discord_outbound(&config);
            unsafe {
                std::env::remove_var("VALKEY_URL");
                std::env::remove_var("VALKEY_PASSWORD");
            }
        }
        tokio::time::sleep(std::time::Duration::from_secs(2)).await;
    }

    #[tokio::test]
    async fn try_start_twitch_outbound_spawns_when_everything_is_valid() {
        crypto::ensure_installed();
        {
            let _guard = ENV_LOCK.lock().unwrap();
            // SAFETY: serialized by ENV_LOCK above.
            unsafe {
                std::env::set_var("VALKEY_URL", "rediss://127.0.0.1:1/");
                std::env::set_var("VALKEY_PASSWORD", "test-valkey-pass");
            }
            let mut config = base_config();
            config.cli.twitch_irc_nick = "waddlebot".to_string();
            config.twitch_irc_oauth_token = Some(crate::config::Secret::new("test-token"));
            try_start_twitch_outbound(&config);
            unsafe {
                std::env::remove_var("VALKEY_URL");
                std::env::remove_var("VALKEY_PASSWORD");
            }
        }
        tokio::time::sleep(std::time::Duration::from_secs(2)).await;
    }

    #[test]
    fn resolve_binding_keyring_none_when_active_kid_empty() {
        let config = base_config();
        assert!(resolve_binding_keyring(&config, "test").is_none());
    }

    #[test]
    fn resolve_binding_keyring_none_when_keys_unset() {
        let mut config = base_config();
        config.cli.binding_active_kid = "k1".to_string();
        assert!(resolve_binding_keyring(&config, "test").is_none());
    }

    #[test]
    fn resolve_binding_keyring_none_when_keys_malformed() {
        let mut config = base_config();
        config.cli.binding_active_kid = "k1".to_string();
        config.envelope_binding_keys = Some(crate::config::Secret::new("not-valid"));
        assert!(resolve_binding_keyring(&config, "test").is_none());
    }

    /// Flag OFF (the default) is exactly today's behaviour: the legacy
    /// drains are chosen, an unconfigured deployment is a logged no-op, and
    /// none of the executor-only prerequisites are ever consulted.
    #[tokio::test]
    async fn connector_tools_off_keeps_the_legacy_outbound_path() {
        let config = base_config();
        assert!(!config.cli.connector_tools);
        assert_eq!(
            outbound_boot::mode(&config.cli),
            outbound_boot::OutboundMode::Legacy
        );
        start_outbound(&config, None).await.unwrap();
    }

    /// Flag ON with the executor prerequisites absent is FATAL -- no silent
    /// fallback to the legacy drains.
    #[tokio::test]
    async fn connector_tools_on_without_prerequisites_is_fatal_not_legacy() {
        let mut config = base_config();
        config.cli.connector_tools = true;
        let err = start_outbound(&config, None).await.unwrap_err();
        assert!(
            err.to_string().contains("waddles.connector-tools is ON"),
            "{err}"
        );
    }

    #[test]
    fn resolve_binding_keyring_some_when_valid() {
        let mut config = base_config();
        config.cli.binding_active_kid = "k1".to_string();
        config.envelope_binding_keys = Some(crate::config::Secret::new(
            "k1:0102030405060708090a0b0c0d0e0f10",
        ));
        assert!(resolve_binding_keyring(&config, "test").is_some());
    }
}
