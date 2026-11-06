//! Boot wiring of the connector runtime (spec
//! `2026-11-05-connector-runtime-inbound-manager.md` sections 3-5, PR-E).
//!
//! Everything here runs ONLY when an `CONNECTOR_<P>_ENABLED` baseline is ON.
//! With every connector OFF `lib.rs` never calls into this module: nothing is
//! built, bound or spawned (pinned by `default_boot_starts_nothing_new`).
//!
//! ```text
//!  HostBridge::new ──> ExecutorLink (+ ConnectorHostCalls over ActiveConnections)
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

use bundle_host_http::guarded::{GuardedClient, GuardedLimits};
use connection_resolver::{ConnectionRef, ConnectionResolve};
use penguin_spine::{KeyRing, SpineMetrics};

use super::feed::{ConnKey, DynFeed, FeedSet, HubBindingFeed, TransportPlan, WebhookPlan};
use super::metrics::RuntimeMetrics;
use super::plan::{ConnectorProfile, RuntimeEnv};
use super::profiles::{discord, spectrum, twitch, wix, youtube};
use super::scoped::{ConnectionSink, ScopeLookup, ScopeTenantResolve, ScopedSinkFactory};
use super::selector::{BootError, ConnectorSelector, IDENTIFY_BUDGET_READY};
use super::supervisor::{
    ConnectionActivator, ConnectorRuntime, HandshakeGateFactory, ResolverSecrets, RuntimeConfig,
    RuntimeDeps, RuntimeError, SecretsWrapper, WebhookAttach,
};
use super::webhook_boot::{self, WebhookEnv};
use super::ConnectorValkeyLease;
use crate::config::CliConfig;
use crate::connector::host_calls::{
    ActiveConnections, CallScope, ConnectorHostCalls, HostCallConfig, IdentityTokenizer,
};
use crate::connector::secrets::TransportSecretSource;
use crate::connector::spectrum::{RedisSpectrumStore, SpectrumStore};
use crate::connector::webhook::{WebhookIngress, WebhookTransport};
use crate::connector::{
    register_connector_metrics, ConnectorRegistry, DiscordIdentifyGate, ExecutorLink,
    FrameRuleMetrics, GatewayBotSource, HandshakeGate, IdentifyBudgetMetrics, PinnedConnector,
    RedisIdentifyBudget, TransportError, WireConnectorInvoker,
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
        "spectrum" => "SENDER_EXEC_DIGEST_SPECTRUM",
        "wix" => "SENDER_EXEC_DIGEST_WIX",
        _ => "SENDER_EXEC_DIGEST_YOUTUBE",
    }
}

/// Host-constant `app_id` and hub-api provider of each connector platform.
fn platform_consts(platform: &str) -> Option<(&'static str, &'static str)> {
    match platform {
        "twitch" => Some((twitch::APP_ID, twitch::PROVIDER)),
        "discord" => Some((discord::APP_ID, discord::PROVIDER)),
        "youtube" => Some((youtube::APP_ID, youtube::PROVIDER)),
        "spectrum" => Some((spectrum::APP_ID, spectrum::PROVIDER)),
        "wix" => Some((wix::APP_ID, wix::PROVIDER)),
        _ => None,
    }
}

/// Process-level runtime settings, validated once at boot.
#[derive(Debug, Clone)]
pub struct RuntimeSettings {
    /// Connector-ON platforms.
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
        selector: &ConnectorSelector,
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
    /// Builds the link with `ConnectorHostCalls` installed (`enabled` = the
    /// connector-ON platforms, whose cutover flags answer ON).
    ///
    /// # Errors
    /// [`BootError::Malformed`] for a platform with no host-call connector.
    pub fn new(
        tokenizer: Arc<dyn IdentityTokenizer>,
        enabled: &[&'static str],
    ) -> Result<Self, BootError> {
        let active = Arc::new(ActiveConnections::new());
        let cfg = HostCallConfig::new(enabled.iter().copied())
            .map_err(|_| BootError::Malformed("connector platform"))?;
        let handler = ConnectorHostCalls::new(Arc::clone(&active) as _, tokenizer, cfg);
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

    fn deactivate(&self, key: &ConnKey, tenant_id: i64) {
        self.0.deactivate(&CallScope {
            platform: key.platform,
            community_id: key.community_id,
            tenant_id,
            connection_id: key.connection_id,
        });
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

/// Scope namespacing the Discord IDENTIFY budget keys in Valkey (host constant).
pub const IDENTIFY_BUDGET_SCOPE: &str = "discord";
/// Deadline of one `GET /gateway/bot` limit fetch.
const GATEWAY_BOT_TIMEOUT: Duration = Duration::from_secs(10);
/// Ceiling of the `GET /gateway/bot` body.
const GATEWAY_BOT_MAX_BYTES: usize = 64 * 1024;

/// The Valkey-backed IDENTIFY budget the Discord connector requires. Without it
/// Discord ON cannot start: a restart storm would burn the bot's daily
/// `session_start_limit`.
pub struct IdentifyBudgetBackend {
    /// Connected Valkey shared by every replica (the fleet-wide budget).
    pub conn: redis::aio::MultiplexedConnection,
    /// Discord REST base for `GET /gateway/bot` (production:
    /// [`crate::connector::identify_budget::DISCORD_API_BASE`]).
    pub api_base: String,
    /// Per-bucket window; production keeps Discord's 5s
    /// ([`crate::connector::identify_budget::IDENTIFY_WINDOW`]).
    pub window: Duration,
}

/// [`HandshakeGateFactory`] giving every Discord connection a
/// [`DiscordIdentifyGate`]: one IDENTIFY per bucket fleet-wide, RESUME exempt,
/// Valkey/limit failures fail closed. Other platforms get no gate.
pub struct DiscordGateFactory {
    budget: Arc<RedisIdentifyBudget>,
    client: GuardedClient,
    api_base: String,
    metrics: IdentifyBudgetMetrics,
}

impl DiscordGateFactory {
    /// Builds the factory over `backend`; `egress_allow` is the SSRF guard's
    /// allow-list (empty in production).
    #[must_use]
    pub fn new(
        backend: IdentifyBudgetBackend,
        egress_allow: Vec<IpAddr>,
        metrics: IdentifyBudgetMetrics,
    ) -> Self {
        Self {
            budget: Arc::new(
                RedisIdentifyBudget::new(backend.conn, IDENTIFY_BUDGET_SCOPE)
                    .with_window(backend.window),
            ),
            client: GuardedClient::new(
                GuardedLimits {
                    max_redirects: 0,
                    timeout: GATEWAY_BOT_TIMEOUT,
                    max_response_bytes: GATEWAY_BOT_MAX_BYTES,
                },
                egress_allow,
            ),
            api_base: backend.api_base,
            metrics,
        }
    }
}

impl HandshakeGateFactory for DiscordGateFactory {
    fn for_connection(
        &self,
        desired: &super::feed::DesiredConnection,
        secrets: Arc<dyn TransportSecretSource>,
    ) -> Result<Option<Arc<dyn HandshakeGate>>, TransportError> {
        if desired.key.platform != discord::PLATFORM {
            return Ok(None);
        }
        let source = GatewayBotSource::new(
            self.client.clone(),
            Arc::new(super::observed::SharedSecrets(secrets)),
            ConnectionRef {
                community_id: desired.key.community_id,
                connection_id: desired.key.connection_id,
            },
            self.api_base.clone(),
        )
        .with_metrics(self.metrics.clone());
        let gate = DiscordIdentifyGate::new(
            Arc::clone(&self.budget),
            Arc::new(source),
            0,
            self.metrics.clone(),
        );
        Ok(Some(Arc::new(gate)))
    }
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
    /// The Discord IDENTIFY budget backend. REQUIRED when Discord is ON:
    /// [`assemble`] refuses to build a Discord runtime without it.
    pub identify_budget: Option<IdentifyBudgetBackend>,
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
    route_keys: Option<&webhook_boot::RouteKeyDeriver>,
) -> Option<Arc<dyn DynFeed>> {
    fn make<R: ConnectionResolve + 'static, P: ConnectorProfile + 'static>(
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
        "spectrum" => Some(make(
            resolver,
            spectrum::SpectrumProfile::new(),
            env,
            metrics,
        )),
        "wix" => Some(make(
            resolver,
            wix::WixProfile::new(route_keys.cloned()),
            env,
            metrics,
        )),
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

    let gates: Option<Arc<dyn HandshakeGateFactory>> = if settings.platforms.contains(&"discord") {
        let backend = deps
            .identify_budget
            .ok_or(BootError::Missing("the Valkey identify budget backend"))?;
        let metrics = IdentifyBudgetMetrics::register(&deps.registry)
            .map_err(|_| BootError::Malformed("identify budget metrics"))?;
        Some(Arc::new(DiscordGateFactory::new(
            backend,
            settings.egress_allow.clone(),
            metrics,
        )))
    } else {
        None
    };
    let env = settings.runtime_env();
    let mut feeds = FeedSet::new();
    let mut providers = BTreeMap::new();
    let route_keys = deps
        .webhook
        .as_ref()
        .and_then(|(_, e)| e.deriver().cloned());
    for p in &settings.platforms {
        let feed = feed_for(p, &deps.resolver, &env, &metrics, route_keys.as_ref())
            .ok_or(BootError::Malformed("platform"))?;
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
    let spectrum_store: Option<Arc<dyn SpectrumStore>> =
        settings.platforms.contains(&"spectrum").then(|| {
            Arc::new(RedisSpectrumStore::new(deps.lease_conn.clone())) as Arc<dyn SpectrumStore>
        });
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
        gates,
        tenants: Arc::new(ScopeTenantResolve::new(deps.scope)),
        executor: Arc::clone(&deps.bridge.link) as _,
        connector_metrics,
        metrics,
        frame_metrics: Some(frame_metrics),
        webhook,
        activator: Some(Arc::new(TableActivator(deps.bridge.active))),
        // Dedupe/snapshot state rides the same real Valkey as the leases; only
        // built when the selector enabled Spectrum (otherwise Spectrum plans
        // would park loudly, but none exist).
        spectrum: spectrum_store,
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

/// Binds the host-API listener (mTLS, SAN-pinned executor identity) over a
/// link with the host-call allowlist installed, and returns the bridge plus the
/// bound address. The tokenizer is the only hub-api dependency of the bridge.
///
/// # Errors
/// An unreadable certificate, an unknown platform, or a bind failure.
pub async fn start_bridge_with(
    tls: &crate::connector::host_api_listener::HostApiTls,
    addr: std::net::SocketAddr,
    tokenizer: Arc<dyn IdentityTokenizer>,
    enabled: &[&'static str],
    shutdown: impl Fn() -> tokio::sync::oneshot::Receiver<()>,
) -> anyhow::Result<(HostBridge, std::net::SocketAddr)> {
    let bridge = HostBridge::new(tokenizer, enabled)?;
    let acceptor = crate::connector::host_api_listener::build_acceptor(tls)?;
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .map_err(|e| anyhow::anyhow!("host-api listener bind failed: {e}"))?;
    let bound = listener
        .local_addr()
        .map_err(|e| anyhow::anyhow!("host-api listener address unavailable: {e}"))?;
    tokio::spawn(crate::connector::host_api_listener::serve(
        listener,
        acceptor,
        Arc::clone(&bridge.link),
        shutdown(),
    ));
    tracing::info!(addr = %bound, "connector host-api listener started");
    Ok((bridge, bound))
}

/// The hub-api identity tokenizer (`MintEphemeralPseudonyms`, machine JWT with
/// the mint scope) from the process environment.
///
/// # Errors
/// A missing hub endpoint or an unreachable/untrusted hub-api.
pub async fn connect_tokenizer(
    cli: &CliConfig,
    lookup: &(dyn Fn(&str) -> Option<String> + Sync),
) -> anyhow::Result<Arc<dyn IdentityTokenizer>> {
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
    Ok(Arc::new(
        crate::connector::host_calls::HubIdentityTokenizer::from_client(client),
    ))
}

/// [`start_bridge_with`] configured from the environment: TLS/port items first
/// (named when missing), then the hub-api tokenizer.
///
/// # Errors
/// Missing/malformed TLS or hub items, an unreadable certificate, a bind failure
/// or an unreachable hub-api -- all fatal, named without values.
pub async fn start_bridge(
    cli: &CliConfig,
    lookup: &(dyn Fn(&str) -> Option<String> + Sync),
    enabled: &[&'static str],
    shutdown: impl Fn() -> tokio::sync::oneshot::Receiver<()>,
) -> anyhow::Result<HostBridge> {
    let (tls, addr) = crate::outbound_boot::host_api_from_lookup(cli, lookup)?;
    let tokenizer = connect_tokenizer(cli, lookup).await?;
    Ok(start_bridge_with(&tls, addr, tokenizer, enabled, shutdown)
        .await?
        .0)
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
/// dependencies is a boot error, never a silently idle connector.
///
/// # Errors
/// Any missing prerequisite (scope resolver, binding keyring, spine, Valkey) or
/// assembly failure -- fatal to the process by design.
#[allow(clippy::too_many_arguments)]
pub async fn start_runtime<R: ConnectionResolve + 'static>(
    config: &crate::config::Config,
    settings: &RuntimeSettings,
    bridge: HostBridge,
    resolver: Arc<R>,
    scope: Arc<dyn ScopeLookup>,
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
    let budget_conn = lease_conn.clone();
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
            identify_budget: settings.platforms.contains(&"discord").then(|| {
                IdentifyBudgetBackend {
                    conn: budget_conn,
                    api_base: crate::connector::identify_budget::DISCORD_API_BASE.to_owned(),
                    window: crate::connector::identify_budget::IDENTIFY_WINDOW,
                }
            }),
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

/// The whole connector boot sequence `lib.rs` runs when any `CONNECTOR_<P>_ENABLED`
/// is ON, from the real process environment: the hub-api resolver, identity
/// tokenizer and the installed dataplane scope resolver, then [`start_connector_with`].
///
/// # Errors
/// Any missing prerequisite -- fatal to the process, never a silent fallback.
pub async fn start_connector(
    config: &crate::config::Config,
    selector: &ConnectorSelector,
    webhook_env: &Arc<WebhookEnv>,
    state: &mut crate::http::AppState,
    metrics: Arc<crate::telemetry::IngestMetrics>,
    dek: crate::identity_crypto::ConfiguredDekProvider<
        crate::identity_crypto::UnimplementedMachineJwtProvider,
    >,
) -> anyhow::Result<Started> {
    let lookup = |k: &str| std::env::var(k).ok();
    let scope: Arc<dyn ScopeLookup> = crate::publish::installed_scope_resolver()
        .ok_or_else(|| anyhow::anyhow!("connector runtime needs the dataplane scope resolver"))?;
    let resolver = Arc::new(build_resolver(&config.cli)?);
    let tokenizer = connect_tokenizer(&config.cli, &lookup).await?;
    start_connector_with(
        config,
        selector,
        webhook_env,
        state,
        metrics,
        dek,
        ConnectorDeps {
            resolver,
            scope,
            tokenizer,
            lookup: &lookup,
        },
    )
    .await
}

/// The injected dependencies of [`start_connector_with`] (hub-api resolver, scope
/// authority, identity tokenizer and the env lookup).
pub struct ConnectorDeps<'a, R: ConnectionResolve + 'static> {
    /// hub-api connection resolver.
    pub resolver: Arc<R>,
    /// Scope authority (tenant refusal + sink scope).
    pub scope: Arc<dyn ScopeLookup>,
    /// Identity tokenizer behind the host-call allowlist.
    pub tokenizer: Arc<dyn IdentityTokenizer>,
    /// Env lookup (digests, TLS, ports).
    pub lookup: &'a (dyn Fn(&str) -> Option<String> + Sync),
}

/// Settings, webhook ingress (stored on `state` BEFORE the router is built),
/// host-API listener, spine/Valkey and the runtime spawn.
///
/// # Errors
/// Any missing prerequisite -- fatal to the process, never a silent fallback.
pub async fn start_connector_with<R: ConnectionResolve + 'static>(
    config: &crate::config::Config,
    selector: &ConnectorSelector,
    webhook_env: &Arc<WebhookEnv>,
    state: &mut crate::http::AppState,
    metrics: Arc<crate::telemetry::IngestMetrics>,
    dek: crate::identity_crypto::ConfiguredDekProvider<
        crate::identity_crypto::UnimplementedMachineJwtProvider,
    >,
    deps: ConnectorDeps<'_, R>,
) -> anyhow::Result<Started> {
    let cli = &config.cli;
    let settings = RuntimeSettings::from_inputs(cli, selector, deps.lookup)?;
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
    // BEFORE `http::router(state)`: the route is 503 only while this is `None`.
    let ingress = build_webhook(
        webhook_env,
        Arc::clone(&deps.resolver),
        &redis_client,
        &registry,
    )
    .await?;
    state.webhook = ingress.clone();
    let (tls, addr) = crate::outbound_boot::host_api_from_lookup(cli, deps.lookup)?;
    let (bridge, _) = start_bridge_with(
        &tls,
        addr,
        deps.tokenizer,
        &settings.platforms,
        crate::shutdown_receiver,
    )
    .await?;
    start_runtime(
        config,
        &settings,
        bridge,
        deps.resolver,
        deps.scope,
        ingress.map(|i| (i, Arc::clone(webhook_env))),
        &redis_client,
        spine_cfg,
        metrics,
        dek,
        registry,
    )
    .await
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use std::collections::HashMap;
    use std::time::Duration;

    use clap::Parser;
    use connection_resolver::{ConnectionRef, SecretString};

    use super::*;
    use crate::connector::host_calls::{ScopeSource, TokenizeError};
    use crate::connector::webhook::{
        BoxFut, DedupToken, SecretError, WebhookDedup, WebhookSecretSource,
    };
    use crate::connector_runtime::testkit::{desired_webhook, desired_ws};

    fn env(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let m: HashMap<String, String> = pairs
            .iter()
            .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
            .collect();
        move |k| m.get(k).cloned()
    }

    fn cli() -> CliConfig {
        CliConfig::parse_from(["svc-ingest"])
    }

    fn cli_alpha() -> CliConfig {
        CliConfig::parse_from(["svc-ingest", "--environment", "alpha"])
    }

    fn selector(pairs: &[(&str, &str)]) -> ConnectorSelector {
        ConnectorSelector::from_env(&env(pairs)).unwrap()
    }

    #[test]
    fn settings_read_the_digest_of_every_enabled_platform_and_default_the_rest() {
        let sel = selector(&[
            ("CONNECTOR_TWITCH_ENABLED", "true"),
            ("CONNECTOR_YOUTUBE_ENABLED", "true"),
        ]);
        let lookup = env(&[
            ("SENDER_EXEC_DIGEST_TWITCH", " sha256:aa "),
            ("SENDER_EXEC_DIGEST_YOUTUBE", "sha256:bb"),
        ]);
        let s = RuntimeSettings::from_inputs(&cli(), &sel, &lookup).unwrap();
        assert_eq!(s.platforms, vec!["twitch", "youtube"]);
        assert_eq!(s.digests["twitch"], "sha256:aa");
        assert_eq!(s.reconcile_interval, Duration::from_secs(60));
        assert_eq!((s.max_connections, s.start_concurrency), (500, 8));
        assert!(!s.heartbeat_enabled && s.egress_allow.is_empty());
        let cfg = s.runtime_config();
        assert!(cfg.start_pace.is_empty(), "no discord pace without discord");
        assert_eq!(s.registry().unwrap().len(), 2);
    }

    #[test]
    fn a_missing_digest_or_a_malformed_heartbeat_is_fatal_and_named() {
        let sel = selector(&[("CONNECTOR_TWITCH_ENABLED", "true")]);
        assert_eq!(
            RuntimeSettings::from_inputs(&cli(), &sel, &env(&[])).unwrap_err(),
            BootError::Missing("SENDER_EXEC_DIGEST_TWITCH")
        );
        assert_eq!(
            RuntimeSettings::from_inputs(
                &cli(),
                &sel,
                &env(&[("SENDER_EXEC_DIGEST_TWITCH", "  ")])
            )
            .unwrap_err(),
            BootError::Missing("SENDER_EXEC_DIGEST_TWITCH")
        );
        for (raw, want) in [("true", true), ("1", true), ("false", false), ("0", false)] {
            let s = RuntimeSettings::from_inputs(
                &cli(),
                &sel,
                &env(&[("SENDER_EXEC_DIGEST_TWITCH", "d"), (ENV_HEARTBEAT, raw)]),
            )
            .unwrap();
            assert_eq!(s.heartbeat_enabled, want, "{raw}");
        }
        let err = RuntimeSettings::from_inputs(
            &cli(),
            &sel,
            &env(&[("SENDER_EXEC_DIGEST_TWITCH", "d"), (ENV_HEARTBEAT, "yes")]),
        )
        .unwrap_err();
        assert_eq!(err, BootError::Malformed(ENV_HEARTBEAT));
    }

    #[test]
    fn discord_pace_applies_only_when_discord_is_enabled() {
        let mut s = RuntimeSettings {
            platforms: vec!["discord"],
            digests: BTreeMap::from([("discord", "d".to_owned())]),
            reconcile_interval: Duration::from_secs(5),
            max_connections: 3,
            start_concurrency: 2,
            discord_pace: Duration::from_millis(5500),
            environment: "alpha".into(),
            heartbeat_enabled: true,
            egress_allow: vec![],
        };
        let cfg = s.runtime_config();
        assert_eq!(cfg.start_pace["discord"], Duration::from_millis(5500));
        assert_eq!(cfg.max_connections, 3);
        let env = s.runtime_env();
        assert_eq!(
            (env.max_frame_bytes, env.environment.as_str()),
            (MAX_FRAME_BYTES, "alpha")
        );
        s.discord_pace = Duration::ZERO;
        assert!(s.runtime_config().start_pace.is_empty());
        s.digests.clear();
        assert_eq!(
            s.registry().unwrap_err(),
            BootError::Missing("SENDER_EXEC_DIGEST_DISCORD")
        );
        s.platforms = vec!["slack"];
        assert_eq!(s.registry().unwrap_err(), BootError::Malformed("platform"));
    }

    struct NoTok;
    impl IdentityTokenizer for NoTok {
        fn tokenize<'a>(
            &'a self,
            _s: &'a CallScope,
            _id: &'a str,
        ) -> BoxFut<'a, Result<String, TokenizeError>> {
            Box::pin(async { Err(TokenizeError::Unavailable) })
        }
    }

    #[test]
    fn the_bridge_rejects_an_unknown_platform_and_builds_an_unattached_link() {
        let b = HostBridge::new(Arc::new(NoTok), &["twitch"]).unwrap();
        assert!(!b.link.is_attached());
        assert!(b.active.is_empty());
        assert!(HostBridge::new(Arc::new(NoTok), &["slack"]).is_err());
    }

    /// The activator publishes the RESOLVED scope the host-call handler reads,
    /// and removal makes the id unknown again (stale ids are refused).
    #[test]
    fn the_activator_writes_and_removes_the_host_call_scope() {
        let b = HostBridge::new(Arc::new(NoTok), &["twitch"]).unwrap();
        let act = TableActivator(Arc::clone(&b.active));
        let key = ConnKey {
            platform: "twitch",
            community_id: 7,
            connection_id: 9,
        };
        act.activate(&key, 5).unwrap();
        let scope = b.active.scope_of(9).unwrap();
        assert_eq!(
            (
                scope.platform,
                scope.community_id,
                scope.tenant_id,
                scope.connection_id
            ),
            ("twitch", 7, 5, 9)
        );
        act.deactivate(&key, 5);
        assert!(b.active.scope_of(9).is_none());
        // A tenant that is not positive (or an unknown platform) is refused.
        assert!(act.activate(&key, 0).is_err());
        let slack = ConnKey {
            platform: "slack",
            ..key
        };
        assert!(act.activate(&slack, 5).is_err());
    }

    /// Security review MEDIUM: another community cannot take over a live
    /// connection id, and a stale guard cannot remove the new activation.
    #[test]
    fn the_activator_refuses_takeover_and_a_stale_guard_removes_nothing() {
        let b = HostBridge::new(Arc::new(NoTok), &["twitch"]).unwrap();
        let act = TableActivator(Arc::clone(&b.active));
        let mine = ConnKey {
            platform: "twitch",
            community_id: 7,
            connection_id: 9,
        };
        let other = ConnKey {
            community_id: 8,
            ..mine.clone()
        };
        act.activate(&mine, 5).unwrap();
        assert!(act.activate(&other, 5).is_err(), "other community refused");
        assert!(act.activate(&mine, 6).is_err(), "other tenant refused");
        assert_eq!(b.active.scope_of(9).unwrap().community_id, 7);
        // Overlapping restart of the same connection: old guard drops after re-activation.
        act.activate(&mine, 5).unwrap();
        act.deactivate(&mine, 5);
        assert!(
            b.active.scope_of(9).is_some(),
            "new activation survives the old guard"
        );
        // A stale guard for a different scope removes nothing.
        act.deactivate(&other, 5);
        assert!(b.active.scope_of(9).is_some());
        act.deactivate(&mine, 5);
        assert!(b.active.scope_of(9).is_none());
    }

    #[test]
    fn the_twitch_wrapper_refuses_non_websocket_plans_and_wraps_websocket_ones() {
        let wrap = twitch_secrets_wrapper();
        struct Inner;
        impl TransportSecretSource for Inner {
            fn resolve(
                &self,
                _r: ConnectionRef,
                _d: crate::connector::secrets::Destination,
            ) -> BoxFut<'_, Result<crate::connector::secrets::ResolvedAuth, SecretError>>
            {
                Box::pin(async { Err(SecretError::Missing) })
            }
        }
        let inner: Arc<dyn TransportSecretSource> = Arc::new(Inner);
        let webhook = desired_webhook(1, 2, "route-key-1", 1);
        assert!(wrap(Arc::clone(&inner), &webhook).is_err());
        // The test-kit ws config has no twitch handles/self-echo: the wrapper
        // refuses it (never a half-built credential source).
        let ws = desired_ws("twitch", 1, 2, 1, "twitch:chan", 1);
        assert!(wrap(inner, &ws).is_err());
    }

    struct Sec;
    impl WebhookSecretSource for Sec {
        fn resolve(&self, _r: ConnectionRef) -> BoxFut<'_, Result<SecretString, SecretError>> {
            Box::pin(async { Err(SecretError::Missing) })
        }
    }
    struct Dd;
    impl WebhookDedup for Dd {
        fn first_seen<'a>(&'a self, _k: &'a str) -> BoxFut<'a, Result<Option<DedupToken>, ()>> {
            Box::pin(async { Ok(None) })
        }
        fn release<'a>(&'a self, _k: &'a str, _t: &'a DedupToken) -> BoxFut<'a, Result<(), ()>> {
            Box::pin(async { Ok(()) })
        }
    }

    /// Webhook plans go through `activate_plan`: a route key that is not the
    /// host derivation is refused; the derived one registers, binds and detaches.
    #[test]
    fn webhook_plans_activate_only_the_host_derived_route() {
        const SECRET: &str = "0123456789abcdef0123456789abcdef";
        let wenv = Arc::new(
            WebhookEnv::from_lookup(&env(&[
                ("CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS", "discord"),
                ("CONNECTOR_ROUTE_KEY_SECRET", SECRET),
            ]))
            .unwrap(),
        );
        let ingress = Arc::new(WebhookIngress::new(Arc::new(Sec), Arc::new(Dd), None));
        let attach = IngressAttach::new(Arc::clone(&ingress), Arc::clone(&wenv));
        let key = ConnKey {
            platform: "discord",
            community_id: 7,
            connection_id: 9,
        };
        // Forged key: refused.
        let mut forged = desired_webhook(7, 9, "forged-route-key-0000000000000000000000000", 1);
        if let TransportPlan::Webhook(p) = &mut forged.plan {
            p.route.owner_bundle = "discord".into();
            assert!(attach.attach(p, &key).is_err());
        }
        // The derived key: accepted.
        let route = webhook_boot::build_route(
            &wenv,
            "discord",
            ConnectionRef {
                community_id: 7,
                connection_id: 9,
            },
        )
        .unwrap();
        let plan = WebhookPlan {
            route,
            authorized_community: 7,
        };
        assert!(attach.attach(&plan, &key).is_ok());
        attach.detach(&plan);
        // Detaching twice only logs (never panics).
        attach.detach(&plan);
    }

    #[tokio::test]
    async fn start_bridge_names_a_missing_hub_endpoint() {
        let lookup = env(&[
            ("HOST_API_SERVER_CERT_FILE", "/x"),
            ("HOST_API_SERVER_KEY_FILE", "/x"),
            ("HOST_API_CLIENT_CA_FILE", "/x"),
            ("HOST_API_CLIENT_IDENTITY", "spiffe://x"),
        ]);
        let err = start_bridge(&cli(), &lookup, &["twitch"], || {
            tokio::sync::oneshot::channel().1
        })
        .await
        .err()
        .expect("must fail");
        assert!(err.to_string().contains("HUB_API_GRPC_ENDPOINT"), "{err}");
        // And TLS items are checked first, by name.
        let err = start_bridge(&cli(), &env(&[]), &["twitch"], || {
            tokio::sync::oneshot::channel().1
        })
        .await
        .err()
        .expect("must fail");
        assert!(
            err.to_string().contains("HOST_API_SERVER_CERT_FILE"),
            "{err}"
        );
    }

    #[test]
    fn the_resolver_builds_from_cli_defaults() {
        assert!(build_resolver(&cli_alpha()).is_ok());
    }

    struct EmptyHub(std::sync::atomic::AtomicUsize);
    impl ConnectionResolve for EmptyHub {
        async fn resolve(
            &self,
            _r: ConnectionRef,
        ) -> Result<connection_resolver::ResolvedConnection, connection_resolver::ResolveError>
        {
            Err(connection_resolver::ResolveError::Malformed("none"))
        }
        async fn list_bindings(
            &self,
            _p: &str,
            _c: Option<&str>,
            _a: i64,
            _l: u32,
        ) -> Result<connection_resolver::BindingPage, connection_resolver::ResolveError> {
            self.0.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            Ok(connection_resolver::BindingPage {
                items: vec![],
                next_after: None,
            })
        }
    }

    struct NoScope;
    impl ScopeLookup for NoScope {
        fn lookup<'a>(
            &'a self,
            _p: &'a str,
            _s: &'a str,
        ) -> BoxFut<
            'a,
            Result<crate::scope_resolver::ResolvedScope, crate::scope_resolver::Unresolved>,
        > {
            Box::pin(async { Err(crate::scope_resolver::Unresolved::NotFound) })
        }
    }

    struct NullAppender;
    impl EventAppender for NullAppender {
        async fn append(
            &self,
            _s: &str,
            _e: &penguin_spine::StageEnvelope,
        ) -> Result<String, penguin_spine::SpineError> {
            Ok("1-0".into())
        }
    }

    struct FixedDek;
    impl DekProvider for FixedDek {
        async fn get_dek(
            &self,
            _t: &str,
        ) -> Result<(crate::identity_crypto::Dek, u32), crate::identity_crypto::DekUnavailableError>
        {
            Ok((zeroize::Zeroizing::new([1u8; 32]), 1))
        }
    }

    async fn lease_conn() -> redis::aio::MultiplexedConnection {
        let url = std::env::var("VALKEY_URL")
            .expect("VALKEY_URL must be set: this test needs a REAL Valkey (no silent skip)");
        redis::Client::open(url)
            .unwrap()
            .get_multiplexed_async_connection()
            .await
            .unwrap()
    }

    fn settings(platforms: Vec<&'static str>) -> RuntimeSettings {
        RuntimeSettings {
            digests: platforms
                .iter()
                .map(|p| (*p, format!("sha256:{p}")))
                .collect(),
            platforms,
            reconcile_interval: Duration::from_millis(200),
            max_connections: 4,
            start_concurrency: 2,
            discord_pace: Duration::ZERO,
            environment: "test".into(),
            heartbeat_enabled: true,
            egress_allow: vec![],
        }
    }

    async fn deps(
        hub: Arc<EmptyHub>,
        registry: prometheus::Registry,
    ) -> AssembleDeps<EmptyHub, NullAppender, FixedDek> {
        AssembleDeps {
            identify_budget: Some(IdentifyBudgetBackend {
                conn: lease_conn().await,
                api_base: "http://127.0.0.1:9".into(),
                window: Duration::from_secs(5),
            }),
            resolver: hub,
            scope: Arc::new(NoScope),
            appender: Arc::new(NullAppender),
            spine_metrics: Arc::new(penguin_spine::NoopMetrics),
            keyring: Arc::new(KeyRing::new(vec![("k1".into(), vec![7u8; 32])])),
            active_kid: "k1".into(),
            dek: Arc::new(FixedDek),
            identity_metrics: prometheus::IntCounterVec::new(
                prometheus::Opts::new("boot_unit_identity_total", "t"),
                &["tenant", "status"],
            )
            .unwrap(),
            lease_conn: lease_conn().await,
            bridge: HostBridge::new(Arc::new(NoTok), &["twitch", "youtube"]).unwrap(),
            webhook: None,
            registry,
            seams: LoopbackSeams::default(),
        }
    }

    /// `assemble` builds a runtime that reconciles every enabled platform's feed
    /// (non-zero list calls), reports per-platform readiness, and registers the
    /// runtime/connector series exactly once (a second assembly on the same
    /// registry is a typed boot error, not a panic).
    #[tokio::test]
    async fn assemble_reconciles_each_enabled_platform_and_registers_series_once() {
        let hub = Arc::new(EmptyHub(std::sync::atomic::AtomicUsize::new(0)));
        let registry = prometheus::Registry::new();
        let rt = assemble(
            &settings(vec!["twitch", "discord", "youtube"]),
            deps(hub.clone(), registry.clone()).await,
        )
        .unwrap();
        let ready = rt.ready_handle();
        assert!(matches!(
            ready.ready("twitch"),
            super::super::Readiness::NotReady(_)
        ));
        assert_eq!(ready.ready("slack"), super::super::Readiness::Disabled);
        let (stop, rx) = tokio::sync::oneshot::channel();
        let task = tokio::spawn(rt.run(rx));
        for _ in 0..100 {
            if hub.0.load(std::sync::atomic::Ordering::SeqCst) >= 3 {
                break;
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        assert!(
            hub.0.load(std::sync::atomic::Ordering::SeqCst) >= 2,
            "both platforms listed"
        );
        let _ = stop.send(());
        task.await.unwrap();
        let again = assemble(&settings(vec!["twitch"]), deps(hub, registry).await);
        assert_eq!(
            again.err(),
            Some(BootError::Malformed("connector runtime metrics")),
            "duplicate series on one registry"
        );
    }

    /// Discord ON without the Valkey identify budget backend is refused at
    /// assembly (fail closed), while a runtime with Discord OFF needs none.
    #[tokio::test]
    async fn assemble_refuses_discord_without_the_identify_budget_backend() {
        let hub = Arc::new(EmptyHub(std::sync::atomic::AtomicUsize::new(0)));
        let mut d = deps(hub.clone(), prometheus::Registry::new()).await;
        d.identify_budget = None;
        assert_eq!(
            assemble(&settings(vec!["discord"]), d).err(),
            Some(BootError::Missing("the Valkey identify budget backend"))
        );
        let mut d = deps(hub, prometheus::Registry::new()).await;
        d.identify_budget = None;
        assert!(
            assemble(&settings(vec!["twitch", "youtube"]), d).is_ok(),
            "no Discord, no budget needed"
        );
    }

    /// The Twitch wrapper over a REAL profile-derived plan yields a credential
    /// source that answers `nick`/`channel` itself and refuses unknown refs.
    #[tokio::test]
    async fn the_twitch_wrapper_wraps_a_real_profile_plan() {
        use crate::connector_runtime::plan::ConnectorProfile;
        let binding = connection_resolver::BindingSummary {
            community_id: 7,
            connection_id: 9,
            tenant_id: 5,
            secret_version: 1,
            provider: "twitch".into(),
            kind: connection_resolver::ConnectionKind::PlatformOauth,
            metadata: [("channel", "somechan"), ("bot_login", "waddlebot")]
                .into_iter()
                .map(|(k, v)| (k.to_owned(), v.to_owned()))
                .collect(),
        };
        let env = settings(vec!["twitch"]).runtime_env();
        let desired = twitch::TwitchProfile::from_feed()
            .plan(&binding, &env)
            .unwrap()
            .remove(0);
        struct Pass;
        impl TransportSecretSource for Pass {
            fn resolve(
                &self,
                _r: ConnectionRef,
                _d: crate::connector::secrets::Destination,
            ) -> BoxFut<'_, Result<crate::connector::secrets::ResolvedAuth, SecretError>>
            {
                Box::pin(async {
                    Ok(crate::connector::secrets::ResolvedAuth {
                        secret: SecretString::new("tok".to_owned()),
                        header: None,
                    })
                })
            }
        }
        let wrapped = twitch_secrets_wrapper()(Arc::new(Pass), &desired).unwrap();
        let dest = || {
            crate::connector::secrets::Destination::from_url(
                &reqwest::Url::parse("wss://irc-ws.chat.twitch.tv:443").unwrap(),
            )
            .unwrap()
        };
        let pass = ConnectionRef {
            community_id: 7,
            connection_id: 9,
        };
        assert!(wrapped.resolve(pass, dest()).await.is_ok());
        let nick = ConnectionRef {
            community_id: 7,
            connection_id: twitch::NICK_CONN_ID,
        };
        assert!(wrapped.resolve(nick, dest()).await.is_ok());
        let foreign = ConnectionRef {
            community_id: 8,
            connection_id: 9,
        };
        assert!(
            wrapped.resolve(foreign, dest()).await.is_err(),
            "other community refused"
        );
    }

    /// The test doubles themselves fail closed (so a test can never pass because
    /// a fake quietly said yes).
    #[tokio::test]
    async fn the_test_doubles_fail_closed() {
        let scope = CallScope {
            platform: "twitch",
            community_id: 1,
            tenant_id: 1,
            connection_id: 1,
        };
        assert!(NoTok.tokenize(&scope, "u").await.is_err());
        assert!(Sec
            .resolve(ConnectionRef {
                community_id: 1,
                connection_id: 1
            })
            .await
            .is_err());
        assert!(Dd.first_seen("k").await.unwrap().is_none());
        Dd.release("k", &DedupToken("k".into())).await.unwrap();
        let hub = EmptyHub(std::sync::atomic::AtomicUsize::new(0));
        assert!(hub
            .resolve(ConnectionRef {
                community_id: 1,
                connection_id: 1
            })
            .await
            .is_err());
        assert!(NoScope.lookup("twitch", "x").await.is_err());
        assert!(FixedDek.get_dek("t").await.is_ok());
    }

    #[tokio::test]
    async fn assemble_refuses_an_unknown_platform_and_a_missing_digest() {
        let hub = Arc::new(EmptyHub(std::sync::atomic::AtomicUsize::new(0)));
        let err = assemble(
            &settings(vec!["slack"]),
            deps(hub.clone(), prometheus::Registry::new()).await,
        )
        .err();
        assert_eq!(err, Some(BootError::Malformed("platform")));
        let mut s = settings(vec!["twitch"]);
        s.digests.clear();
        let err = assemble(&s, deps(hub, prometheus::Registry::new()).await).err();
        assert_eq!(err, Some(BootError::Missing("SENDER_EXEC_DIGEST_TWITCH")));
    }

    #[tokio::test]
    async fn no_webhook_platforms_means_no_ingress() {
        let resolver = Arc::new(build_resolver(&cli_alpha()).unwrap());
        let client = redis::Client::open("redis://127.0.0.1:1/").unwrap();
        let wenv = WebhookEnv::from_lookup(&env(&[])).unwrap();
        let ingress = build_webhook(&wenv, resolver, &client, &prometheus::Registry::new())
            .await
            .unwrap();
        assert!(ingress.is_none(), "route stays 503 when nothing is enabled");
    }
}
