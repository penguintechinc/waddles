//! Boot wiring of the connector runtime (spec
//! `2026-11-05-connector-runtime-inbound-manager.md` sections 3-5, PR-E).
//!
//! Everything here runs ONLY when an `OVERLAY_<P>_ENABLED` baseline is ON.
//! With every overlay OFF `lib.rs` never calls into this module: nothing is
//! built, bound or spawned (pinned by `default_boot_starts_nothing_new`).
//!
//! ```text
//!  HostBridge::new ──> ExecutorLink (+ OverlayHostCalls over ActiveConnections)
//!       │                      ▲ executor dials in (mTLS listener, shared with outbound)
//!  assemble(deps) ─────────────┴─> ConnectorRuntime
//!       feed      FeedSet{ HubBindingFeed<profile> per enabled platform }
//!       secrets   ResolverSecrets (+ TwitchFieldSecrets wrapper for twitch)
//!       tenants   ScopeTenantResolve  (refuse: unresolved / mismatch -> parked)
//!       sinks     ScopedSinkFactory   (one resolved-scope sink per connection)
//!       activate  TableActivator      (connection_id -> scope for host calls)
//!       webhook   IngressAttach       (activate_plan for Webhook plans)
//! ```
//!
//! Binding metadata and guest data never supply a tenant, a URL or a scope.

use std::collections::BTreeMap;
use std::net::IpAddr;
use std::sync::Arc;
use std::time::Duration;

use connection_resolver::ConnectionResolve;
use penguin_spine::{KeyRing, SpineMetrics};

use super::feed::{ConnKey, DynFeed, FeedSet, HubBindingFeed, TransportPlan, WebhookPlan};
use super::metrics::RuntimeMetrics;
use super::plan::{OverlayProfile, RuntimeEnv};
use super::profiles::{discord, twitch, youtube};
use super::scoped::{ConnectionSink, ScopeLookup, ScopeTenantResolve, ScopedSinkFactory};
use super::selector::{BootError, OverlaySelector, IDENTIFY_BUDGET_READY};
use super::supervisor::{
    ConnectionActivator, ConnectorRuntime, ResolverSecrets, RuntimeConfig, RuntimeDeps,
    RuntimeError, SecretsWrapper, WebhookAttach,
};
use super::webhook_boot::{self, WebhookEnv};
use super::ConnectorValkeyLease;
use crate::config::CliConfig;
use crate::connector::host_calls::{
    ActiveConnections, CallScope, HostCallConfig, IdentityTokenizer, OverlayHostCalls,
};
use crate::connector::secrets::TransportSecretSource;
use crate::connector::webhook::{WebhookIngress, WebhookTransport};
use crate::connector::{
    register_connector_metrics, ConnectorRegistry, ExecutorLink, FrameRuleMetrics, PinnedConnector,
    TransportError, WireConnectorInvoker,
};
use crate::identity_crypto::DekProvider;
use crate::publish::EventAppender;

/// Frame-size ceiling for the pinned inbound connectors (matches outbound).
pub const MAX_FRAME_BYTES: usize = 64 * 1024;
/// Bound on one binding-list call made by a feed.
pub const FEED_CALL_TIMEOUT: Duration = Duration::from_secs(10);
/// Env: strict bool, driver heartbeat baseline (`ingest.connector-heartbeat`).
pub const ENV_HEARTBEAT: &str = "CONNECTOR_HEARTBEAT_ENABLED";

/// The platforms' pinned-digest env names (shared with the outbound executor).
fn digest_env(platform: &str) -> &'static str {
    match platform {
        "twitch" => "SENDER_EXEC_DIGEST_TWITCH",
        "discord" => "SENDER_EXEC_DIGEST_DISCORD",
        _ => "SENDER_EXEC_DIGEST_YOUTUBE",
    }
}

/// Host-constant `app_id` and hub-api provider of each overlay platform.
fn platform_consts(platform: &str) -> Option<(&'static str, &'static str)> {
    match platform {
        "twitch" => Some((twitch::APP_ID, twitch::PROVIDER)),
        "discord" => Some((discord::APP_ID, discord::PROVIDER)),
        "youtube" => Some((youtube::APP_ID, youtube::PROVIDER)),
        _ => None,
    }
}

/// Process-level runtime settings, validated once at boot.
#[derive(Debug, Clone)]
pub struct RuntimeSettings {
    /// Overlay-ON platforms.
    pub platforms: Vec<&'static str>,
    /// Pinned connector digest per platform (host config only).
    pub digests: BTreeMap<&'static str, String>,
    /// Reconcile cadence.
    pub reconcile_interval: Duration,
    /// Per-replica connection cap.
    pub max_connections: usize,
    /// Concurrent connect bound.
    pub start_concurrency: usize,
    /// Minimum gap between Discord connects.
    pub discord_pace: Duration,
    /// Deployment environment label.
    pub environment: String,
    /// Driver heartbeat baseline.
    pub heartbeat_enabled: bool,
    /// IPs granted despite the SSRF denylist (empty in production).
    pub egress_allow: Vec<IpAddr>,
}

impl RuntimeSettings {
    /// Reads the settings for the enabled platforms from `cli` and `lookup`
    /// (env in production). Missing digests and a malformed heartbeat value are
    /// fatal and named, never defaulted.
    ///
    /// # Errors
    /// [`BootError`] naming the first missing/malformed item.
    pub fn from_inputs(
        cli: &CliConfig,
        selector: &OverlaySelector,
        lookup: &dyn Fn(&str) -> Option<String>,
    ) -> Result<Self, BootError> {
        let platforms = selector.enabled_platforms();
        let mut digests = BTreeMap::new();
        for p in &platforms {
            let name = digest_env(p);
            let digest = lookup(name)
                .map(|v| v.trim().to_owned())
                .filter(|v| !v.is_empty())
                .ok_or(BootError::Missing(name))?;
            digests.insert(*p, digest);
        }
        let heartbeat_enabled = match lookup(ENV_HEARTBEAT) {
            None => false,
            Some(raw) => match raw.trim().to_ascii_lowercase().as_str() {
                "true" | "1" => true,
                "false" | "0" => false,
                _ => return Err(BootError::Malformed(ENV_HEARTBEAT)),
            },
        };
        Ok(Self {
            platforms,
            digests,
            reconcile_interval: Duration::from_secs(cli.connector_reconcile_interval_secs),
            max_connections: cli.connector_max_connections,
            start_concurrency: cli.connector_start_concurrency,
            discord_pace: Duration::from_millis(cli.connector_discord_start_pace_ms),
            environment: cli.environment.clone(),
            heartbeat_enabled,
            egress_allow: Vec::new(),
        })
    }

    fn runtime_env(&self) -> RuntimeEnv {
        RuntimeEnv {
            environment: self.environment.clone(),
            max_frame_bytes: MAX_FRAME_BYTES,
            egress_allow: self.egress_allow.clone(),
            heartbeat_enabled: self.heartbeat_enabled,
        }
    }

    fn runtime_config(&self) -> RuntimeConfig {
        let mut c = RuntimeConfig::new(self.platforms.clone());
        c.reconcile_interval = self.reconcile_interval;
        c.max_connections = self.max_connections;
        c.start_concurrency = self.start_concurrency;
        c.heartbeat_enabled = self.heartbeat_enabled;
        if self.platforms.contains(&"discord") && !self.discord_pace.is_zero() {
            c = c.with_start_pace("discord", self.discord_pace);
        }
        c
    }

    fn registry(&self) -> Result<ConnectorRegistry, BootError> {
        let mut registry = ConnectorRegistry::new();
        for p in &self.platforms {
            let (app_id, _) = platform_consts(p).ok_or(BootError::Malformed("platform"))?;
            let digest = self
                .digests
                .get(p)
                .ok_or(BootError::Missing(digest_env(p)))?;
            registry
                .register(PinnedConnector::new(*p, app_id, digest, MAX_FRAME_BYTES))
                .map_err(|_| BootError::Malformed("connector registry"))?;
        }
        Ok(registry)
    }
}

/// The executor link with the production host-call allowlist installed, plus
/// the host-owned active-connection table it scopes by.
pub struct HostBridge {
    /// The one executor session (shared with the outbound executor path).
    pub link: Arc<ExecutorLink>,
    /// `connection_id -> scope`, written by the supervisor, read by the handler.
    pub active: Arc<ActiveConnections>,
}

impl HostBridge {
    /// Builds the link with `OverlayHostCalls` installed (`enabled` = the
    /// overlay-ON platforms, whose cutover flags answer ON).
    ///
    /// # Errors
    /// [`BootError::Malformed`] for a platform with no host-call overlay.
    pub fn new(
        tokenizer: Arc<dyn IdentityTokenizer>,
        enabled: &[&'static str],
    ) -> Result<Self, BootError> {
        let active = Arc::new(ActiveConnections::new());
        let cfg = HostCallConfig::new(enabled.iter().copied())
            .map_err(|_| BootError::Malformed("overlay platform"))?;
        let handler = OverlayHostCalls::new(Arc::clone(&active) as _, tokenizer, cfg);
        let link = Arc::new(ExecutorLink::new().with_host_calls(Arc::new(handler)));
        Ok(Self { link, active })
    }
}

/// Publishes supervisor-started connections to the host-call table.
pub struct TableActivator(pub Arc<ActiveConnections>);

impl ConnectionActivator for TableActivator {
    fn activate(&self, key: &ConnKey, tenant_id: i64) -> Result<(), &'static str> {
        self.0
            .activate(CallScope {
                platform: key.platform,
                community_id: key.community_id,
                tenant_id,
                connection_id: key.connection_id,
            })
            .map_err(|_| "active-connection table refused the scope")
    }

    fn deactivate(&self, key: &ConnKey) {
        self.0.deactivate(key.connection_id);
    }
}

/// `WebhookAttach` over the boot-built ingress: `activate_plan` for a Webhook
/// plan, `deactivate_route` when its connection ends.
pub struct IngressAttach {
    ingress: Arc<WebhookIngress>,
    env: Arc<WebhookEnv>,
}

impl IngressAttach {
    /// Wraps the ingress `lib.rs` also hands to `http::router` via `AppState`.
    #[must_use]
    pub fn new(ingress: Arc<WebhookIngress>, env: Arc<WebhookEnv>) -> Self {
        Self { ingress, env }
    }
}

impl WebhookAttach for IngressAttach {
    fn attach(
        &self,
        plan: &WebhookPlan,
        _key: &ConnKey,
    ) -> Result<WebhookTransport, TransportError> {
        let owned = WebhookPlan {
            route: plan.route.clone(),
            authorized_community: plan.authorized_community,
        };
        webhook_boot::activate_plan(&self.ingress, &self.env, owned).map_err(|e| {
            // The error names an item, never a route key or secret.
            tracing::warn!(error = %e, "webhook route activation refused");
            TransportError::Connect("webhook route activation refused".into())
        })
    }

    fn detach(&self, plan: &WebhookPlan) {
        if let Err(e) = webhook_boot::deactivate_route(
            &self.ingress,
            &plan.route.route_key,
            plan.authorized_community,
        ) {
            tracing::warn!(error = %e, "webhook route deactivation failed");
        }
    }
}

/// The Twitch secrets wrapper (spec 1.2): `pass` resolves through the
/// community-bound source; `nick`/`channel` are answered from the validated
/// logins in the derived config and can never return the OAuth secret.
#[must_use]
pub fn twitch_secrets_wrapper() -> SecretsWrapper {
    Arc::new(|inner, desired| {
        let TransportPlan::Ws(cfg) = &desired.plan else {
            return Err(TransportError::Connect(
                "twitch plan is not a websocket".into(),
            ));
        };
        let source =
            twitch::TwitchFieldSecrets::from_config(super::observed::SharedSecrets(inner), cfg)
                .map_err(|_| {
                    TransportError::Connect("twitch secrets could not be derived".into())
                })?;
        let wrapped: Arc<dyn TransportSecretSource> = Arc::new(source);
        Ok(wrapped)
    })
}

/// Wraps one platform's feed (a loopback test rewrites the host-constant URL).
pub type FeedWrap = Arc<dyn Fn(&'static str, Arc<dyn DynFeed>) -> Arc<dyn DynFeed> + Send + Sync>;

/// Test-only seams for real-socket e2e tests, where the host-constant upstream
/// URL and the destination-allowlisted credential source cannot point at a
/// loopback fake. Production passes [`LoopbackSeams::default`] (all `None`),
/// which is exactly what `start_runtime` does; nothing here is reachable from
/// env or config.
#[derive(Default)]
pub struct LoopbackSeams {
    /// Wraps each platform's feed.
    pub feed_wrap: Option<FeedWrap>,
    /// Replaces the resolver-backed secrets factory.
    pub secrets: Option<Arc<dyn super::supervisor::SecretsFactory>>,
}

/// Everything [`assemble`] needs from the process.
pub struct AssembleDeps<R, A, D>
where
    R: ConnectionResolve + 'static,
    A: EventAppender + 'static,
    D: DekProvider + 'static,
{
    /// hub-api connection resolver (binding list + credentials).
    pub resolver: Arc<R>,
    /// The scope authority shared by the tenant refusal and the sinks.
    pub scope: Arc<dyn ScopeLookup>,
    /// Spine appender.
    pub appender: Arc<A>,
    /// Spine metrics.
    pub spine_metrics: Arc<dyn SpineMetrics>,
    /// Binding keyring and active kid.
    pub keyring: Arc<KeyRing>,
    /// Active binding kid.
    pub active_kid: String,
    /// Identity DEK provider.
    pub dek: Arc<D>,
    /// Identity-encryption counter vec.
    pub identity_metrics: prometheus::IntCounterVec,
    /// Connected Valkey for the per-connection lease.
    pub lease_conn: redis::aio::MultiplexedConnection,
    /// The shared executor link + active table.
    pub bridge: HostBridge,
    /// Webhook ingress + env when any webhook platform is listed.
    pub webhook: Option<(Arc<WebhookIngress>, Arc<WebhookEnv>)>,
    /// Registry that receives the runtime/connector series.
    pub registry: prometheus::Registry,
    /// Loopback test seams (`Default` in production).
    pub seams: LoopbackSeams,
}

/// The concrete runtime type this boot builds.
pub type BootedRuntime<A, D> =
    ConnectorRuntime<FeedSet, WireConnectorInvoker, ConnectionSink<A, D>, ConnectorValkeyLease>;

/// Builds the profile-specific feed for `platform`.
fn feed_for<R: ConnectionResolve + 'static>(
    platform: &'static str,
    resolver: &Arc<R>,
    env: &RuntimeEnv,
    metrics: &RuntimeMetrics,
) -> Option<Arc<dyn DynFeed>> {
    fn make<R: ConnectionResolve + 'static, P: OverlayProfile + 'static>(
        resolver: &Arc<R>,
        profile: P,
        env: &RuntimeEnv,
        metrics: &RuntimeMetrics,
    ) -> Arc<dyn DynFeed> {
        Arc::new(
            HubBindingFeed::new(
                Arc::clone(resolver),
                profile,
                env.clone(),
                FEED_CALL_TIMEOUT,
            )
            .with_metrics(metrics.clone()),
        )
    }
    match platform {
        "twitch" => Some(make(
            resolver,
            twitch::TwitchProfile::from_feed(),
            env,
            metrics,
        )),
        "discord" => Some(make(
            resolver,
            discord::DiscordProfile::new().with_budget_ready(IDENTIFY_BUDGET_READY),
            env,
            metrics,
        )),
        "youtube" => Some(make(resolver, youtube::YoutubeProfile::new(), env, metrics)),
        _ => None,
    }
}

/// Assembles the runtime from its parts; nothing connects until `run`.
///
/// # Errors
/// [`BootError`] for an unknown platform / duplicate series, or a
/// [`RuntimeError`] mapped to `Malformed`.
pub fn assemble<R, A, D>(
    settings: &RuntimeSettings,
    deps: AssembleDeps<R, A, D>,
) -> Result<BootedRuntime<A, D>, BootError>
where
    R: ConnectionResolve + 'static,
    A: EventAppender + 'static,
    D: DekProvider + 'static,
{
    let metrics = RuntimeMetrics::register(&deps.registry)
        .map_err(|_| BootError::Malformed("connector runtime metrics"))?;
    let connector_metrics = register_connector_metrics(&deps.registry)
        .map_err(|_| BootError::Malformed("connector metrics"))?;
    let frame_metrics = FrameRuleMetrics::register(&deps.registry)
        .map_err(|_| BootError::Malformed("frame rule metrics"))?;

    let env = settings.runtime_env();
    let mut feeds = FeedSet::new();
    let mut providers = BTreeMap::new();
    for p in &settings.platforms {
        let feed =
            feed_for(p, &deps.resolver, &env, &metrics).ok_or(BootError::Malformed("platform"))?;
        let feed = match &deps.seams.feed_wrap {
            Some(wrap) => wrap(p, feed),
            None => feed,
        };
        feeds = feeds.with(p, feed);
        let (_, provider) = platform_consts(p).ok_or(BootError::Malformed("platform"))?;
        providers.insert(*p, provider);
    }
    let mut secrets = ResolverSecrets::new(Arc::clone(&deps.resolver), providers);
    if settings.platforms.contains(&"twitch") {
        secrets = secrets.with_wrapper("twitch", twitch_secrets_wrapper());
    }

    let registry = Arc::new(settings.registry()?);
    let sinks = ScopedSinkFactory::new(
        deps.appender,
        deps.spine_metrics,
        deps.keyring,
        deps.active_kid,
        deps.dek,
        deps.identity_metrics,
        Arc::clone(&deps.scope),
    );
    let webhook = deps
        .webhook
        .map(|(ingress, env)| Arc::new(IngressAttach::new(ingress, env)) as Arc<dyn WebhookAttach>);
    let runtime_deps = RuntimeDeps {
        feed: feeds,
        invoker: Arc::new(WireConnectorInvoker::new(
            Arc::clone(&deps.bridge.link),
            Arc::clone(&registry),
        )),
        registry,
        sinks: Arc::new(sinks),
        lease: Arc::new(ConnectorValkeyLease::new(deps.lease_conn)),
        secrets: match deps.seams.secrets {
            Some(custom) => custom,
            None => Arc::new(secrets),
        },
        tenants: Arc::new(ScopeTenantResolve::new(deps.scope)),
        executor: Arc::clone(&deps.bridge.link) as _,
        connector_metrics,
        metrics,
        frame_metrics: Some(frame_metrics),
        webhook,
        activator: Some(Arc::new(TableActivator(deps.bridge.active))),
    };
    ConnectorRuntime::new(runtime_deps, settings.runtime_config()).map_err(|e| match e {
        RuntimeError::Config(m) => BootError::Malformed(m),
    })
}

/// What [`start`] hands back to `lib.rs`.
pub struct Started {
    /// The shared executor link; the outbound executor path rides it too.
    pub link: Arc<ExecutorLink>,
    /// Readiness handle for `/readyz`.
    pub ready: Arc<dyn super::RuntimeReady>,
}

/// Starts the host-API listener (mTLS, SAN-pinned executor identity) over a
/// link with the host-call allowlist installed.
///
/// # Errors
/// Missing/malformed TLS or hub items, an unreadable certificate, a bind failure
/// or an unreachable hub-api -- all fatal, named without values.
pub async fn start_bridge(
    cli: &CliConfig,
    lookup: &dyn Fn(&str) -> Option<String>,
    enabled: &[&'static str],
    shutdown: impl Fn() -> tokio::sync::oneshot::Receiver<()>,
) -> anyhow::Result<HostBridge> {
    let (tls, addr) = crate::outbound_boot::host_api_from_lookup(cli, lookup)?;
    let endpoint = lookup("HUB_API_GRPC_ENDPOINT")
        .map(|v| v.trim().to_owned())
        .filter(|v| !v.is_empty())
        .ok_or(BootError::Missing("HUB_API_GRPC_ENDPOINT"))?;
    let ca = lookup("HUB_API_GRPC_CA_FILE")
        .map(|v| v.trim().to_owned())
        .filter(|v| !v.is_empty());
    let client = hub_client::HubClient::connect(
        endpoint,
        cli.service_jwt_token_endpoint.clone(),
        cli.service_jwt_sa_token_path.clone(),
        crate::connector::host_calls::IDENTITY_MINT_SCOPE,
        ca.as_deref(),
    )
    .await
    .map_err(|e| anyhow::anyhow!("hub-api identity client unavailable: {e}"))?;
    let bridge = HostBridge::new(
        Arc::new(crate::connector::host_calls::HubIdentityTokenizer::from_client(client)),
        enabled,
    )?;
    let acceptor = crate::connector::host_api_listener::build_acceptor(&tls)?;
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .map_err(|e| anyhow::anyhow!("host-api listener bind failed: {e}"))?;
    tokio::spawn(crate::connector::host_api_listener::serve(
        listener,
        acceptor,
        Arc::clone(&bridge.link),
        shutdown(),
    ));
    tracing::info!(%addr, "connector host-api listener started");
    Ok(bridge)
}

/// Builds (but does not spawn) the webhook ingress when a webhook platform is
/// listed; `lib.rs` stores it in `AppState.webhook` BEFORE `http::router`.
///
/// # Errors
/// [`webhook_boot::BootError`] for a missing secret / Valkey.
pub async fn build_webhook<R: ConnectionResolve + 'static>(
    env: &WebhookEnv,
    resolver: Arc<R>,
    valkey: &redis::Client,
    registry: &prometheus::Registry,
) -> Result<Option<Arc<WebhookIngress>>, webhook_boot::BootError> {
    webhook_boot::build_ingress(webhook_boot::IngressDeps {
        env,
        resolver,
        valkey: Some(valkey),
        registry,
    })
    .await
}

/// The hub-api connection resolver the runtime shares with the webhook ingress.
///
/// # Errors
/// An unusable resolver configuration.
pub fn build_resolver(cli: &CliConfig) -> anyhow::Result<connection_resolver::ConnectionResolver> {
    connection_resolver::ConnectionResolver::new(
        connection_resolver::ResolverConfig::new(
            cli.hub_api_url.clone(),
            cli.environment.clone(),
            cli.service_jwt_token_endpoint.clone(),
            cli.service_jwt_sa_token_path.clone(),
        ),
        Arc::new(crate::twitch_sessions::boot::ResolverTelemetry),
    )
    .map_err(|e| anyhow::anyhow!("connection resolver unavailable: {e}"))
}

/// Connects the spine, assembles the runtime and spawns it. The spine connect
/// is bounded by the configured grace period: a runtime that cannot reach its
/// dependencies is a boot error, never a silently idle overlay.
///
/// # Errors
/// Any missing prerequisite (scope resolver, binding keyring, spine, Valkey) or
/// assembly failure -- fatal to the process by design.
#[allow(clippy::too_many_arguments)]
pub async fn start_runtime(
    config: &crate::config::Config,
    settings: &RuntimeSettings,
    bridge: HostBridge,
    resolver: Arc<connection_resolver::ConnectionResolver>,
    webhook: Option<(Arc<WebhookIngress>, Arc<WebhookEnv>)>,
    redis_client: &redis::Client,
    spine_cfg: penguin_spine::SpineConfig,
    metrics: Arc<crate::telemetry::IngestMetrics>,
    dek: crate::identity_crypto::ConfiguredDekProvider<
        crate::identity_crypto::UnimplementedMachineJwtProvider,
    >,
    registry: prometheus::Registry,
) -> anyhow::Result<Started> {
    let cli = &config.cli;
    let scope: Arc<dyn ScopeLookup> = crate::publish::installed_scope_resolver()
        .ok_or_else(|| anyhow::anyhow!("connector runtime needs the dataplane scope resolver"))?;
    let keyring = crate::resolve_binding_keyring(config, "connector runtime").ok_or_else(|| {
        anyhow::anyhow!(
            "connector runtime needs a valid ENVELOPE_BINDING_KEYS + ENVELOPE_BINDING_ACTIVE_KID"
        )
    })?;
    let spine_metrics: Arc<dyn SpineMetrics> = metrics.clone();
    let flag = crate::ReceiverFlag::default();
    let grace = Duration::from_secs(cli.spine_connect_grace_secs);
    let appender = crate::connect_spine_with_retry(
        spine_cfg,
        Arc::clone(&spine_metrics),
        metrics.as_ref(),
        "connector_runtime",
        &flag,
        Duration::from_secs(cli.spine_connect_backoff_max_secs),
        std::time::Instant::now() + grace,
    )
    .await
    .ok_or_else(|| {
        anyhow::anyhow!("connector runtime: spine did not connect within the grace period")
    })?;
    let lease_conn = redis_client
        .get_multiplexed_async_connection()
        .await
        .map_err(|e| {
            anyhow::anyhow!("connector runtime: valkey connect failed ({:?})", e.kind())
        })?;
    let link = Arc::clone(&bridge.link);
    let runtime = assemble(
        settings,
        AssembleDeps {
            resolver,
            scope,
            appender: Arc::new(appender),
            spine_metrics,
            keyring: Arc::new(keyring),
            active_kid: cli.binding_active_kid.clone(),
            dek: Arc::new(dek),
            identity_metrics: metrics.identity_encryption_total.clone(),
            lease_conn,
            bridge,
            webhook,
            registry,
            seams: LoopbackSeams::default(),
        },
    )?;
    let status = runtime.status_handle();
    super::ready::install(Arc::clone(&status))
        .map_err(|_| anyhow::anyhow!("a connector runtime status is already installed"))?;
    let ready = runtime.ready_handle();
    tokio::spawn(runtime.run(crate::shutdown_receiver()));
    tracing::info!(
        platforms = ?settings.platforms,
        "connector runtime started"
    );
    Ok(Started { link, ready })
}

/// The whole overlay boot sequence `lib.rs` runs when any `OVERLAY_<P>_ENABLED`
/// is ON: settings, host-API listener, webhook ingress (stored on `state`
/// BEFORE the router is built), spine/Valkey, runtime spawn.
///
/// # Errors
/// Any missing prerequisite -- fatal to the process, never a silent fallback.
pub async fn start_overlay(
    config: &crate::config::Config,
    selector: &OverlaySelector,
    webhook_env: &Arc<WebhookEnv>,
    state: &mut crate::http::AppState,
    metrics: Arc<crate::telemetry::IngestMetrics>,
    dek: crate::identity_crypto::ConfiguredDekProvider<
        crate::identity_crypto::UnimplementedMachineJwtProvider,
    >,
) -> anyhow::Result<Started> {
    let lookup = |k: &str| std::env::var(k).ok();
    let cli = &config.cli;
    let settings = RuntimeSettings::from_inputs(cli, selector, &lookup)?;
    let registry = (*state.metrics).clone();
    crate::connector::host_calls::register_metrics(&registry)
        .map_err(|_| anyhow::anyhow!("host-call metrics could not be registered"))?;
    let spine_cfg = penguin_spine::SpineConfig::from_env()
        .map_err(|e| anyhow::anyhow!("spine config unavailable for the connector runtime: {e}"))?;
    let redis_client = crate::outbound::build_redis_client(&spine_cfg).map_err(|e| {
        // Type only: a redis error Display can echo the URL + password.
        anyhow::anyhow!(
            "valkey client build failed for the connector runtime ({:?})",
            e.kind()
        )
    })?;
    let resolver = Arc::new(build_resolver(cli)?);
    // BEFORE `http::router(state)`: the route is 503 only while this is `None`.
    let ingress =
        build_webhook(webhook_env, Arc::clone(&resolver), &redis_client, &registry).await?;
    state.webhook = ingress.clone();
    let bridge = start_bridge(cli, &lookup, &settings.platforms, crate::shutdown_receiver).await?;
    start_runtime(
        config,
        &settings,
        bridge,
        resolver,
        ingress.map(|i| (i, Arc::clone(webhook_env))),
        &redis_client,
        spine_cfg,
        metrics,
        dek,
        registry,
    )
    .await
}
