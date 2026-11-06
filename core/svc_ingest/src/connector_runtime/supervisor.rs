//! The reconcile loop and per-connection supervision (spec section 3).
//!
//! Every [`ConnectorRuntime::run`] pass asks the feed for the desired set of
//! each enabled platform, diffs it against the supervised connections by
//! fingerprint, and starts/restarts/stops. Each connection is one future
//! running [`ConnectorDriver::run`] with its own sink, secrets, backoff and
//! lease; a panic is caught inside that future (`catch_unwind`), counted and
//! the connection restarted after [`RESTART_COOLDOWN`]. A feed error keeps the
//! current set. Terminal failures park a connection until its fingerprint
//! changes.
//!
//! Per-frame context: the driver reuses the connect-time `ConnectionCtx` for
//! every frame of a connection, so session state captured AFTER connect (a
//! Discord READY's `self_user_id`) reaches the guest only on the next connect.
//! The supervisor does not change that; the Discord profile closes the gap with
//! a `READY -> Reconnect` directive so the next connect is a RESUME whose
//! session token carries `self_user_id` (see `profiles/discord.rs`).
//!
//! The connection futures are polled by the `run` future itself
//! (`FuturesUnordered`), not `tokio::spawn`ed: the connector traits are
//! `async fn`-in-trait without a `Send` bound, so a generic `spawn` cannot be
//! proven `Send`. `run` is `Send` whenever the concrete trait implementations
//! are, so the caller still `tokio::spawn`s it as one task.

use std::collections::{BTreeMap, HashMap};
use std::future::Future;
use std::panic::AssertUnwindSafe;
use std::sync::Arc;
use std::time::Duration;

use connection_resolver::ConnectionResolve;
use futures_util::stream::{FuturesUnordered, StreamExt};
use futures_util::FutureExt;
use tokio::sync::{oneshot, Semaphore};
use tokio::time::Instant;

use super::feed::{BindingFeed, ConnKey, DesiredConnection, TransportPlan, WebhookPlan};
use super::lease::{lease_tuple, platform_ns};
use super::metrics::{
    saturating_u32, LeaseOutcome, ParkReason, ReconcileOutcome, RestartReason, RuntimeMetrics,
};
use super::observed::{ObservedInvoker, ObservedTransport, Pacer, SharedSecrets};
use super::ready::{ExecutorStatus, RuntimeReady, RuntimeStatus};
use super::tenant::{TenantError, TenantResolve, TENANT_UNRESOLVED};
use crate::connector::webhook::BoxFut;
use crate::connector::webhook::WebhookTransport;
use crate::connector::ConnectorInvoker;
use crate::connector::{
    ConnectorDriver, ConnectorMetrics, ConnectorRegistry, EventSink, FrameRuleMetrics,
    HandshakeGate, PollTransport, RunEnd, Transport, TransportError, TransportSecretSource,
    TransportSecrets, WsTransport,
};
use crate::twitch_sessions::SessionLease;

/// Wait after a connection ends on its own before it is restarted.
pub const RESTART_COOLDOWN: Duration = Duration::from_secs(120);
/// Default per-connection lease lifetime.
pub const LEASE_TTL: Duration = Duration::from_secs(250);
/// Bound on connection tasks finishing after their stop signal.
pub const STOP_TIMEOUT: Duration = Duration::from_secs(10);
/// Bound on one `feed.desired` call.
pub const FEED_TIMEOUT: Duration = Duration::from_secs(10);
/// Consecutive renew errors after which a connection stops (fail-closed).
const MAX_RENEW_FAILURES: u32 = 2;

/// Invalid runtime configuration or construction input.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum RuntimeError {
    /// A configuration value is out of range (fixed reason, no values).
    #[error("invalid connector runtime config: {0}")]
    Config(&'static str),
}

/// Tunables of the runtime (spec 1.3 env table plus lifecycle constants).
#[derive(Debug, Clone)]
pub struct RuntimeConfig {
    /// Enabled connector platforms; each must be a supported platform slug.
    pub platforms: Vec<&'static str>,
    /// `CONNECTOR_RECONCILE_INTERVAL_SECS` (a 0-20% jitter is added).
    pub reconcile_interval: Duration,
    /// Bound on one `feed.desired` call.
    pub feed_timeout: Duration,
    /// `CONNECTOR_MAX_CONNECTIONS`: hard cap on supervised connections.
    pub max_connections: usize,
    /// `CONNECTOR_START_CONCURRENCY`: bounded concurrent connects.
    pub start_concurrency: usize,
    /// Minimum gap between connect attempts, per platform (`CONNECTOR_DISCORD_START_PACE_MS`).
    pub start_pace: BTreeMap<&'static str, Duration>,
    /// Whether drivers run the heartbeat schedule.
    pub heartbeat_enabled: bool,
    /// Wait before restarting a connection that ended on its own.
    pub restart_cooldown: Duration,
    /// Per-connection lease lifetime.
    pub lease_ttl: Duration,
    /// How often a running connection renews its lease.
    pub lease_renew_every: Duration,
    /// Bound on connection tasks finishing after their stop signal.
    pub stop_timeout: Duration,
    /// Driver reconnect backoff cap.
    pub backoff_cap: Duration,
}

impl RuntimeConfig {
    /// Production defaults for `platforms`.
    #[must_use]
    pub fn new(platforms: Vec<&'static str>) -> Self {
        Self {
            platforms,
            reconcile_interval: Duration::from_secs(60),
            feed_timeout: FEED_TIMEOUT,
            max_connections: 500,
            start_concurrency: 8,
            start_pace: BTreeMap::new(),
            heartbeat_enabled: false,
            restart_cooldown: RESTART_COOLDOWN,
            lease_ttl: LEASE_TTL,
            lease_renew_every: LEASE_TTL / 5,
            stop_timeout: STOP_TIMEOUT,
            backoff_cap: Duration::from_secs(30),
        }
    }

    /// Sets the minimum connect gap for `platform`.
    #[must_use]
    pub fn with_start_pace(mut self, platform: &'static str, gap: Duration) -> Self {
        self.start_pace.insert(platform, gap);
        self
    }

    /// Rejects a configuration that cannot run correctly.
    pub fn validate(&self) -> Result<(), RuntimeError> {
        let bad = |m: &'static str| Err(RuntimeError::Config(m));
        if self.platforms.is_empty() {
            return bad("no platforms enabled");
        }
        if self.platforms.iter().any(|p| platform_ns(p).is_none()) {
            return bad("unsupported platform");
        }
        if self.reconcile_interval.is_zero() || self.feed_timeout.is_zero() {
            return bad("reconcile interval and feed timeout must be positive");
        }
        if self.max_connections == 0 || self.start_concurrency == 0 {
            return bad("max connections and start concurrency must be at least 1");
        }
        if self.lease_ttl.is_zero() || self.lease_renew_every.is_zero() {
            return bad("lease ttl and renew interval must be positive");
        }
        // The renew failures tolerated must not outrun the lease's own expiry.
        if self.lease_renew_every * (MAX_RENEW_FAILURES + 2) > self.lease_ttl {
            return bad("lease renew interval too long for the lease ttl");
        }
        if self.start_pace.keys().any(|p| !self.platforms.contains(p)) {
            return bad("start pace set for a platform that is not enabled");
        }
        Ok(())
    }
}

/// The sink for a connection could not be built.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("sink build failed: {0}")]
pub struct SinkBuildError(pub &'static str);

/// Builds one [`EventSink`] per connection (scope from the connection's
/// resolved numeric `tenant_id`/`community_id`).
pub trait SinkFactory<S>: Send + Sync {
    /// The sink for `desired`, scoped to the RESOLVED `tenant_id` (never the
    /// profile's `desired.tenant_id`, which is only a cross-check).
    fn build<'a>(
        &'a self,
        desired: &'a DesiredConnection,
        tenant_id: i64,
    ) -> BoxFut<'a, Result<S, SinkBuildError>>;
}

/// Builds the per-connection, community-bound credential source.
pub trait SecretsFactory: Send + Sync {
    /// Secrets bound to `desired.key.community_id`; guest data can never
    /// change that. Gets the whole plan so a platform can wrap the source
    /// with plan-derived field resolution (Twitch).
    fn for_connection(
        &self,
        desired: &DesiredConnection,
    ) -> Result<Arc<dyn TransportSecretSource>, TransportError>;
}

/// Wraps the community-bound secret source for one platform, given the plan
/// (e.g. Twitch: `TwitchFieldSecrets::from_config(inner, &ws_cfg)`).
pub type SecretsWrapper = Arc<
    dyn Fn(
            Arc<dyn TransportSecretSource>,
            &DesiredConnection,
        ) -> Result<Arc<dyn TransportSecretSource>, TransportError>
        + Send
        + Sync,
>;

/// [`SecretsFactory`] over a [`ConnectionResolve`], pinning each platform to
/// its hub-api provider name.
pub struct ResolverSecrets<R: ConnectionResolve + 'static> {
    resolver: Arc<R>,
    providers: BTreeMap<&'static str, &'static str>,
    wrappers: BTreeMap<&'static str, SecretsWrapper>,
}

impl<R: ConnectionResolve + 'static> ResolverSecrets<R> {
    /// `providers` maps platform slug -> provider name (host constants).
    pub fn new(resolver: Arc<R>, providers: BTreeMap<&'static str, &'static str>) -> Self {
        Self {
            resolver,
            providers,
            wrappers: BTreeMap::new(),
        }
    }

    /// Wraps every `platform` connection's secret source with `wrapper`; a
    /// wrapper error refuses the connection (it parks as `transport_build`).
    #[must_use]
    pub fn with_wrapper(mut self, platform: &'static str, wrapper: SecretsWrapper) -> Self {
        self.wrappers.insert(platform, wrapper);
        self
    }
}

impl<R: ConnectionResolve + 'static> SecretsFactory for ResolverSecrets<R> {
    fn for_connection(
        &self,
        desired: &DesiredConnection,
    ) -> Result<Arc<dyn TransportSecretSource>, TransportError> {
        let key = &desired.key;
        let provider = self
            .providers
            .get(key.platform)
            .ok_or_else(|| TransportError::Connect("no provider for platform".into()))?;
        let s = TransportSecrets::new(self.resolver.clone(), key.community_id)?
            .with_expected_provider(*provider);
        let inner: Arc<dyn TransportSecretSource> = Arc::new(s);
        match self.wrappers.get(key.platform) {
            Some(wrap) => wrap(inner, desired),
            None => Ok(inner),
        }
    }
}

/// Builds the pre-handshake admission gate of one connection (Discord: the
/// fleet-wide IDENTIFY budget). The gate is awaited by the driver before an
/// on-connect handshake is sent, so a refusal sends nothing upstream.
pub trait HandshakeGateFactory: Send + Sync {
    /// The gate for `desired`, built over the connection's own community-bound
    /// `secrets`; `Ok(None)` for a platform with no gate. An `Err` refuses the
    /// connection (it parks as `transport_build`): a platform that needs a
    /// gate never runs without one.
    ///
    /// # Errors
    /// [`TransportError`] when the gate cannot be built.
    fn for_connection(
        &self,
        desired: &DesiredConnection,
        secrets: Arc<dyn TransportSecretSource>,
    ) -> Result<Option<Arc<dyn HandshakeGate>>, TransportError>;
}

/// Publishes a running connection to the host-owned table the host-call bridge
/// scopes by (`connection_id -> platform/community/tenant`). The supervisor
/// `activate`s once the tenant is resolved and BEFORE any frame is driven, and
/// `deactivate`s on EVERY exit (stop, park, failure, panic, abort), so a host
/// call naming a stale or unknown connection id is refused.
pub trait ConnectionActivator: Send + Sync {
    /// Makes `key` callable with the RESOLVED `tenant_id`. A refusal (fixed
    /// reason, no values) parks the connection: it must never run unscoped.
    fn activate(&self, key: &ConnKey, tenant_id: i64) -> Result<(), &'static str>;
    /// Releases the registration `activate(key, tenant_id)` made (compare-and-
    /// remove: a stale guard never removes another registration). Idempotent.
    fn deactivate(&self, key: &ConnKey, tenant_id: i64);
}

/// Deactivates its connection when dropped, however the connection future
/// ends (including cancellation by the runtime's shutdown abort).
struct ActiveGuard {
    activator: Arc<dyn ConnectionActivator>,
    key: ConnKey,
    tenant_id: i64,
}

impl Drop for ActiveGuard {
    fn drop(&mut self) {
        self.activator.deactivate(&self.key, self.tenant_id);
    }
}

/// Registers/binds the webhook routes of a connection (implemented by the
/// webhook boot; absent means webhook plans park loudly).
pub trait WebhookAttach: Send + Sync {
    /// Registers `plan.route` and binds a transport for it.
    fn attach(&self, plan: &WebhookPlan, key: &ConnKey)
        -> Result<WebhookTransport, TransportError>;
    /// Unregisters the route when its connection ends.
    fn detach(&self, plan: &WebhookPlan);
}

/// Everything the runtime needs besides its [`RuntimeConfig`].
pub struct RuntimeDeps<F, I, S, L>
where
    F: BindingFeed,
    I: ConnectorInvoker + 'static,
    S: EventSink + 'static,
    L: SessionLease + 'static,
{
    /// Desired-set source.
    pub feed: F,
    /// The real invoker (executor link).
    pub invoker: Arc<I>,
    /// Pinned connectors.
    pub registry: Arc<ConnectorRegistry>,
    /// One sink per connection.
    pub sinks: Arc<dyn SinkFactory<S>>,
    /// Replica-singleton lease.
    pub lease: Arc<L>,
    /// Per-connection credential source.
    pub secrets: Arc<dyn SecretsFactory>,
    /// Pre-handshake admission gates (Discord IDENTIFY budget); `None` = no
    /// platform in this runtime needs one.
    pub gates: Option<Arc<dyn HandshakeGateFactory>>,
    /// Resolves each connection's tenant from its community (never from
    /// binding metadata).
    pub tenants: Arc<dyn TenantResolve>,
    /// Executor attachment, for `/readyz`.
    pub executor: Arc<dyn ExecutorStatus>,
    /// Existing connector driver metrics.
    pub connector_metrics: ConnectorMetrics,
    /// Runtime metrics.
    pub metrics: RuntimeMetrics,
    /// Frame-rule counters shared by Ws/Poll transports.
    pub frame_metrics: Option<FrameRuleMetrics>,
    /// Webhook binder; `None` parks webhook plans.
    pub webhook: Option<Arc<dyn WebhookAttach>>,
    /// Active-connection table for the host-call bridge; `None` when no host
    /// calls are serviced (the link then denies every call).
    pub activator: Option<Arc<dyn ConnectionActivator>>,
}

/// How one connection future ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum TaskEnd {
    /// Stopped on request (or shutdown).
    Stopped,
    /// Terminal; parked until the fingerprint changes.
    Parked(ParkReason),
    /// Ended without being asked; restart after the cooldown.
    Failed(RestartReason),
    /// Another replica holds the lease (or the store failed): not started.
    LeaseDenied,
    /// The lease was lost while running.
    LeaseLost,
}

/// State shared by every connection future.
struct Shared<I: ConnectorInvoker, S: EventSink, L: SessionLease> {
    invoker: Arc<ObservedInvoker<I>>,
    registry: Arc<ConnectorRegistry>,
    sinks: Arc<dyn SinkFactory<S>>,
    lease: Arc<L>,
    secrets: Arc<dyn SecretsFactory>,
    gates: Option<Arc<dyn HandshakeGateFactory>>,
    tenants: Arc<dyn TenantResolve>,
    status: Arc<RuntimeStatus>,
    connector_metrics: ConnectorMetrics,
    metrics: RuntimeMetrics,
    frame_metrics: Option<FrameRuleMetrics>,
    webhook: Option<Arc<dyn WebhookAttach>>,
    activator: Option<Arc<dyn ConnectionActivator>>,
    start_sem: Arc<Semaphore>,
    pacers: BTreeMap<&'static str, Arc<Pacer>>,
    heartbeat_enabled: bool,
    backoff_cap: Duration,
    lease_ttl: Duration,
    lease_renew_every: Duration,
}

/// Renews the lease until it is lost or unrenewable; returns then.
async fn renew_until_lost<L: SessionLease>(
    lease: &L,
    tuple: (i64, i64),
    ttl: Duration,
    every: Duration,
) {
    let mut failures = 0u32;
    loop {
        tokio::time::sleep(every).await;
        match lease.renew(tuple, ttl).await {
            Ok(true) => failures = 0,
            Ok(false) => return,
            Err(_) => {
                failures += 1;
                if failures > MAX_RENEW_FAILURES {
                    return;
                }
            }
        }
    }
}

/// Runs one driver over `transport` until it ends, is stopped, or the lease
/// is lost.
async fn drive<T, I, S, L>(
    sh: &Shared<I, S, L>,
    driver: &ConnectorDriver<ObservedInvoker<I>, S>,
    transport: T,
    key: &ConnKey,
    tuple: (i64, i64),
    permit: tokio::sync::OwnedSemaphorePermit,
    stop_rx: oneshot::Receiver<()>,
) -> TaskEnd
where
    T: Transport,
    I: ConnectorInvoker + 'static,
    S: EventSink + 'static,
    L: SessionLease + 'static,
{
    let gauge = sh.metrics.live.with_label_values(&[key.platform]);
    let observed = ObservedTransport::new(
        transport,
        sh.status.clone(),
        gauge,
        key.platform,
        sh.pacers.get(key.platform).cloned(),
        Some(permit),
    )
    .with_connection_id(key.connection_id.to_string());
    tokio::select! {
        r = driver.run(observed, stop_rx) => match r {
            Ok(RunEnd::Shutdown) => TaskEnd::Stopped,
            Ok(RunEnd::Parked) => TaskEnd::Parked(ParkReason::Fatal),
            Err(_) => TaskEnd::Parked(ParkReason::DriverError),
        },
        () = renew_until_lost(&*sh.lease, tuple, sh.lease_ttl, sh.lease_renew_every) => {
            sh.metrics.lease(key.platform, LeaseOutcome::Lost);
            TaskEnd::LeaseLost
        }
    }
}

/// Logs a tenant-resolution outcome with numeric ids only.
fn log_tenant(key: &ConnKey, what: &'static str) {
    tracing::warn!(
        platform = key.platform,
        community_id = key.community_id,
        connection_id = key.connection_id,
        "{what}"
    );
}

/// Lease, start permit, sink and secrets for one connection, or the end that
/// stopped it from starting.
async fn prepare<I, S, L>(
    sh: &Shared<I, S, L>,
    desired: &DesiredConnection,
) -> Result<
    (
        (i64, i64),
        tokio::sync::OwnedSemaphorePermit,
        S,
        SharedSecrets,
        i64,
    ),
    TaskEnd,
>
where
    I: ConnectorInvoker + 'static,
    S: EventSink + 'static,
    L: SessionLease + 'static,
{
    let key = &desired.key;
    let Some(tuple) = lease_tuple(key) else {
        return Err(TaskEnd::Parked(ParkReason::TransportBuild));
    };
    match sh.lease.acquire(tuple, sh.lease_ttl).await {
        Ok(true) => sh.metrics.lease(key.platform, LeaseOutcome::Acquired),
        Ok(false) => {
            sh.metrics.lease(key.platform, LeaseOutcome::Denied);
            return Err(TaskEnd::LeaseDenied);
        }
        Err(e) => {
            sh.metrics.lease(key.platform, LeaseOutcome::Error);
            tracing::warn!(
                platform = key.platform,
                community_id = key.community_id,
                connection_id = key.connection_id,
                error = %e,
                "connector lease unavailable; not starting"
            );
            return Err(TaskEnd::LeaseDenied);
        }
    }
    let Ok(permit) = sh.start_sem.clone().acquire_owned().await else {
        return Err(TaskEnd::Failed(RestartReason::Ended));
    };
    let tenant_id = match sh.tenants.resolve(desired).await {
        Ok(t) if t >= 1 && (desired.tenant_id == TENANT_UNRESOLVED || desired.tenant_id == t) => t,
        Ok(t) if t >= 1 => {
            log_tenant(key, "profile tenant disagrees with the resolved tenant");
            return Err(TaskEnd::Parked(ParkReason::TenantMismatch));
        }
        Ok(_) | Err(TenantError::Unresolved) => {
            log_tenant(key, "tenant could not be resolved");
            return Err(TaskEnd::Parked(ParkReason::TenantUnresolved));
        }
        Err(TenantError::Mismatch) => {
            log_tenant(key, "resolved community disagrees with the connection");
            return Err(TaskEnd::Parked(ParkReason::TenantMismatch));
        }
        Err(TenantError::Transient) => {
            log_tenant(key, "tenant lookup failed; will retry");
            return Err(TaskEnd::Failed(RestartReason::TenantLookup));
        }
    };
    let sink = match sh.sinks.build(desired, tenant_id).await {
        Ok(s) => s,
        Err(e) => {
            tracing::warn!(
                platform = key.platform,
                community_id = key.community_id,
                connection_id = key.connection_id,
                error = %e,
                "could not build the connection sink"
            );
            return Err(TaskEnd::Failed(RestartReason::SinkBuild));
        }
    };
    let secrets = match sh.secrets.for_connection(desired) {
        Ok(s) => SharedSecrets(s),
        Err(e) => {
            tracing::warn!(
                platform = key.platform,
                community_id = key.community_id,
                connection_id = key.connection_id,
                kind = e.kind(),
                "could not build the connection secrets"
            );
            return Err(TaskEnd::Parked(ParkReason::TransportBuild));
        }
    };
    Ok((tuple, permit, sink, secrets, tenant_id))
}

/// One connection's whole life: prepare, transport, driver. A stop request
/// is honored in every phase, including while still preparing.
async fn run_connection<I, S, L>(
    sh: Arc<Shared<I, S, L>>,
    desired: Arc<DesiredConnection>,
    mut stop_rx: oneshot::Receiver<()>,
) -> TaskEnd
where
    I: ConnectorInvoker + 'static,
    S: EventSink + 'static,
    L: SessionLease + 'static,
{
    let key = &desired.key;
    let prepared = tokio::select! {
        biased;
        _ = &mut stop_rx => return TaskEnd::Stopped,
        r = prepare(&sh, &desired) => r,
    };
    let (tuple, permit, sink, secrets, tenant_id) = match prepared {
        Ok(p) => p,
        Err(end) => return end,
    };
    // Held for the whole connection: dropped on every exit path.
    let _active = match &sh.activator {
        Some(activator) => match activator.activate(key, tenant_id) {
            Ok(()) => Some(ActiveGuard {
                activator: Arc::clone(activator),
                key: key.clone(),
                tenant_id,
            }),
            Err(reason) => {
                tracing::warn!(
                    platform = key.platform,
                    community_id = key.community_id,
                    connection_id = key.connection_id,
                    reason,
                    "could not activate the connection for host calls; parking it"
                );
                return TaskEnd::Parked(ParkReason::ActivationFailed);
            }
        },
        None => None,
    };
    let mut driver = ConnectorDriver::new(
        sh.registry.clone(),
        sh.invoker.clone(),
        Arc::new(sink),
        sh.connector_metrics.clone(),
    )
    .with_heartbeat_enabled(sh.heartbeat_enabled)
    .with_backoff_cap(sh.backoff_cap);
    if let Some(gates) = &sh.gates {
        match gates.for_connection(&desired, Arc::clone(&secrets.0)) {
            Ok(Some(gate)) => driver = driver.with_handshake_gate(gate),
            Ok(None) => {}
            Err(e) => {
                tracing::warn!(
                    platform = key.platform,
                    community_id = key.community_id,
                    connection_id = key.connection_id,
                    kind = e.kind(),
                    "could not build the handshake gate; parking the connection"
                );
                return TaskEnd::Parked(ParkReason::TransportBuild);
            }
        }
    }
    let build_failed = |kind: &'static str| {
        tracing::warn!(
            platform = key.platform,
            community_id = key.community_id,
            connection_id = key.connection_id,
            kind,
            "could not build the connection transport"
        );
        TaskEnd::Parked(ParkReason::TransportBuild)
    };
    match &desired.plan {
        TransportPlan::Ws(cfg) => {
            let mut t = match WsTransport::new((**cfg).clone(), secrets) {
                Ok(t) => t,
                Err(e) => return build_failed(e.kind()),
            };
            if let Some(m) = &sh.frame_metrics {
                t = t.with_metrics(m.clone());
            }
            drive(&sh, &driver, t, key, tuple, permit, stop_rx).await
        }
        TransportPlan::Poll(cfg) => {
            let mut t = match PollTransport::new((**cfg).clone(), secrets) {
                Ok(t) => t,
                Err(e) => return build_failed(e.kind()),
            };
            if let Some(m) = &sh.frame_metrics {
                t = t.with_metrics(m.clone());
            }
            drive(&sh, &driver, t, key, tuple, permit, stop_rx).await
        }
        TransportPlan::Webhook(plan) => {
            let Some(binder) = &sh.webhook else {
                return TaskEnd::Parked(ParkReason::WebhookUnavailable);
            };
            let t = match binder.attach(plan, key) {
                Ok(t) => t,
                Err(e) => return build_failed(e.kind()),
            };
            let end = drive(&sh, &driver, t, key, tuple, permit, stop_rx).await;
            binder.detach(plan);
            end
        }
    }
}

/// Releases the lease of `key` (best effort; it expires on its own otherwise).
/// A free function over an owned `Arc` so no `&Core` is held across the await.
async fn release_lease<I, S, L>(sh: &Arc<Shared<I, S, L>>, key: &ConnKey)
where
    I: ConnectorInvoker + 'static,
    S: EventSink + 'static,
    L: SessionLease + 'static,
{
    if let Some(t) = lease_tuple(key) {
        if let Err(e) = sh.lease.release(t).await {
            tracing::warn!(
                platform = key.platform,
                connection_id = key.connection_id,
                error = %e,
                "connector lease release failed; it will expire"
            );
        }
    }
}

/// A supervised connection's bookkeeping.
struct Slot {
    desired: Arc<DesiredConnection>,
    state: SlotState,
    /// Set while a stop was requested; the next end is expected.
    stopping: bool,
    /// Start this once the current task has ended.
    pending: Option<Arc<DesiredConnection>>,
    stop: Option<oneshot::Sender<()>>,
}

enum SlotState {
    /// A connection future is in flight.
    Active,
    /// Ended on its own; restart at `until` if still desired.
    Cooldown {
        until: Instant,
        reason: RestartReason,
    },
    /// Terminal; stays until the fingerprint changes.
    Parked,
}

/// Slots plus the in-flight connection futures; generic over the future type
/// so the (unnameable) `async fn` future stays unboxed and `Send`-inferable.
struct Core<Fut, M, I, S, L>
where
    Fut: Future<Output = (ConnKey, TaskEnd)>,
    M: Fn(Arc<DesiredConnection>, oneshot::Receiver<()>) -> Fut,
    I: ConnectorInvoker + 'static,
    S: EventSink + 'static,
    L: SessionLease + 'static,
{
    make: M,
    tasks: FuturesUnordered<Fut>,
    slots: HashMap<ConnKey, Slot>,
    sh: Arc<Shared<I, S, L>>,
    status: Arc<RuntimeStatus>,
    config: RuntimeConfig,
}

impl<Fut, M, I, S, L> Core<Fut, M, I, S, L>
where
    Fut: Future<Output = (ConnKey, TaskEnd)>,
    M: Fn(Arc<DesiredConnection>, oneshot::Receiver<()>) -> Fut,
    I: ConnectorInvoker + 'static,
    S: EventSink + 'static,
    L: SessionLease + 'static,
{
    fn start(&mut self, desired: Arc<DesiredConnection>, reason: Option<RestartReason>) {
        if let Some(r) = reason {
            self.sh.metrics.restart(desired.key.platform, r);
        }
        let (tx, rx) = oneshot::channel();
        self.tasks.push((self.make)(desired.clone(), rx));
        tracing::info!(
            platform = desired.key.platform,
            community_id = desired.key.community_id,
            connection_id = desired.key.connection_id,
            plan = desired.plan.kind(),
            "starting connector connection"
        );
        self.slots.insert(
            desired.key.clone(),
            Slot {
                desired,
                state: SlotState::Active,
                stopping: false,
                pending: None,
                stop: Some(tx),
            },
        );
    }

    /// Asks an active connection to stop; its end is then expected.
    fn request_stop(slot: &mut Slot) {
        slot.stopping = true;
        if let Some(tx) = slot.stop.take() {
            if tx.send(()).is_err() {
                // The receiver is gone only if the task already ended; its
                // end is applied by the next poll of the task set.
                tracing::debug!("stop signal not delivered: connection already ended");
            }
        }
    }

    /// Applies a finished connection future.
    async fn on_end(&mut self, key: ConnKey, end: TaskEnd) {
        let held_lease = !matches!(end, TaskEnd::LeaseDenied | TaskEnd::LeaseLost);
        if held_lease {
            release_lease(&self.sh.clone(), &key).await;
        }
        let Some(slot) = self.slots.remove(&key) else {
            return;
        };
        tracing::info!(
            platform = key.platform,
            community_id = key.community_id,
            connection_id = key.connection_id,
            end = ?end,
            "connector connection ended"
        );
        if let Some(next) = slot.pending {
            if !matches!(end, TaskEnd::LeaseDenied | TaskEnd::LeaseLost) {
                self.start(next, Some(RestartReason::Fingerprint));
            }
            return;
        }
        if slot.stopping {
            return;
        }
        let mut slot = Slot {
            stop: None,
            stopping: false,
            pending: None,
            ..slot
        };
        match end {
            TaskEnd::Parked(r) => {
                self.sh.metrics.parked(key.platform, r);
                slot.state = SlotState::Parked;
            }
            TaskEnd::Failed(reason) => {
                slot.state = SlotState::Cooldown {
                    until: Instant::now() + self.config.restart_cooldown,
                    reason,
                };
            }
            TaskEnd::Stopped => {
                // Ended without a stop request (the driver saw its shutdown
                // channel close): treat like any unexpected end.
                slot.state = SlotState::Cooldown {
                    until: Instant::now() + self.config.restart_cooldown,
                    reason: RestartReason::Ended,
                };
            }
            // No lease: drop the slot; the next pass re-acquires.
            TaskEnd::LeaseDenied | TaskEnd::LeaseLost => return,
        }
        self.slots.insert(key, slot);
    }

    /// Polls `fut` to completion while serving connection ends.
    async fn pump<T>(&mut self, fut: impl Future<Output = T>) -> T {
        tokio::pin!(fut);
        loop {
            tokio::select! {
                out = &mut fut => return out,
                Some((key, end)) = self.tasks.next(), if !self.tasks.is_empty() => {
                    self.on_end(key, end).await;
                }
            }
        }
    }

    /// Diffs one platform's desired set against the supervised connections.
    fn apply(&mut self, platform: &'static str, desired: Vec<DesiredConnection>) {
        let mut wanted: HashMap<ConnKey, Arc<DesiredConnection>> = HashMap::new();
        for d in desired {
            // Defensive: the feed already guarantees this; never trust it.
            if d.key.platform == platform && !wanted.contains_key(&d.key) {
                wanted.insert(d.key.clone(), Arc::new(d));
            }
        }
        let mut keys: Vec<ConnKey> = wanted.keys().cloned().collect();
        keys.sort();
        self.status
            .reconcile_ok(platform, saturating_u32(keys.len()));
        self.sh
            .metrics
            .desired
            .with_label_values(&[platform])
            .set(i64::from(saturating_u32(keys.len())));

        // Stop what is no longer desired.
        let gone: Vec<ConnKey> = self
            .slots
            .keys()
            .filter(|k| k.platform == platform && !wanted.contains_key(*k))
            .cloned()
            .collect();
        for k in gone {
            let remove_now = match self.slots.get_mut(&k) {
                Some(slot) if matches!(slot.state, SlotState::Active) => {
                    slot.pending = None;
                    Self::request_stop(slot);
                    false
                }
                Some(_) => true,
                None => false,
            };
            if remove_now {
                self.slots.remove(&k);
            }
        }

        let mut capped = 0u64;
        for k in keys {
            let Some(d) = wanted.remove(&k) else { continue };
            enum Act {
                Start(Option<RestartReason>),
                Nothing,
            }
            let act = match self.slots.get_mut(&k) {
                None => Act::Start(None),
                Some(slot) => {
                    let changed = slot.desired.fingerprint != d.fingerprint;
                    match &slot.state {
                        SlotState::Active => {
                            if changed || slot.stopping {
                                slot.pending = Some(d.clone());
                                Self::request_stop(slot);
                            }
                            Act::Nothing
                        }
                        SlotState::Cooldown { until, reason } => {
                            if changed {
                                Act::Start(Some(RestartReason::Fingerprint))
                            } else if Instant::now() >= *until {
                                Act::Start(Some(*reason))
                            } else {
                                Act::Nothing
                            }
                        }
                        SlotState::Parked => {
                            if changed {
                                Act::Start(Some(RestartReason::Fingerprint))
                            } else {
                                Act::Nothing
                            }
                        }
                    }
                }
            };
            if let Act::Start(reason) = act {
                let is_new = reason.is_none();
                if is_new && self.slots.len() >= self.config.max_connections {
                    capped += 1;
                    continue;
                }
                self.start(d, reason);
            }
        }
        if capped > 0 {
            tracing::warn!(platform, skipped = capped, "connection cap reached");
        }
        self.sh.metrics.capped(platform, capped);
    }

    /// One reconcile pass for one platform.
    async fn reconcile<F: BindingFeed>(&mut self, feed: &F, platform: &'static str) {
        let timeout = self.config.feed_timeout;
        let res = self
            .pump(tokio::time::timeout(timeout, feed.desired(platform)))
            .await;
        match res {
            Ok(Ok(desired)) => {
                self.apply(platform, desired);
                self.sh.metrics.reconcile(platform, ReconcileOutcome::Ok);
            }
            Ok(Err(e)) => {
                self.status.reconcile_failed(platform);
                self.sh
                    .metrics
                    .reconcile(platform, ReconcileOutcome::FeedError);
                tracing::warn!(platform, error = %e, "binding feed failed; keeping the current set");
            }
            Err(_) => {
                self.status.reconcile_failed(platform);
                self.sh
                    .metrics
                    .reconcile(platform, ReconcileOutcome::FeedTimeout);
                tracing::warn!(platform, "binding feed timed out; keeping the current set");
            }
        }
    }

    /// Stops everything: signal, wait up to the stop timeout, then abort.
    async fn shutdown(&mut self) {
        for slot in self.slots.values_mut() {
            slot.pending = None;
            Self::request_stop(slot);
        }
        let deadline = Instant::now() + self.config.stop_timeout;
        while !self.tasks.is_empty() {
            match tokio::time::timeout_at(deadline, self.tasks.next()).await {
                Ok(Some((key, end))) => self.on_end(key, end).await,
                Ok(None) => break,
                Err(_) => break,
            }
        }
        if !self.tasks.is_empty() {
            tracing::warn!(
                remaining = self.tasks.len(),
                "stop timeout elapsed; aborting remaining connections"
            );
        }
        // Dropping the futures aborts them; release what they still held.
        self.tasks = FuturesUnordered::new();
        let leftover: Vec<ConnKey> = self
            .slots
            .iter()
            .filter(|(_, s)| matches!(s.state, SlotState::Active))
            .map(|(k, _)| k.clone())
            .collect();
        for k in leftover {
            release_lease(&self.sh.clone(), &k).await;
        }
        self.slots.clear();
        for p in &self.config.platforms {
            self.sh.metrics.live.with_label_values(&[p]).set(0);
        }
    }
}

/// 0-20% of `base`, drawn from a fresh random UUID.
fn jitter(base: Duration) -> Duration {
    let r = uuid::Uuid::new_v4().as_u128();
    let frac = (r % 2000) as f64 / 10_000.0;
    base.mul_f64(frac)
}

/// The connector runtime: reconciles desired connections and supervises one
/// driver per connection.
pub struct ConnectorRuntime<
    F: BindingFeed,
    I: ConnectorInvoker + 'static,
    S: EventSink + 'static,
    L: SessionLease + 'static,
> {
    feed: F,
    sh: Arc<Shared<I, S, L>>,
    config: RuntimeConfig,
    status: Arc<RuntimeStatus>,
}

impl<
        F: BindingFeed,
        I: ConnectorInvoker + 'static,
        S: EventSink + 'static,
        L: SessionLease + 'static,
    > ConnectorRuntime<F, I, S, L>
{
    /// Validates `config` and assembles the runtime; nothing connects until
    /// [`Self::run`].
    pub fn new(deps: RuntimeDeps<F, I, S, L>, config: RuntimeConfig) -> Result<Self, RuntimeError> {
        config.validate()?;
        let status = Arc::new(RuntimeStatus::new(&config.platforms, deps.executor));
        let pacers = config
            .start_pace
            .iter()
            .filter(|(_, gap)| !gap.is_zero())
            .map(|(p, gap)| (*p, Arc::new(Pacer::new(*gap))))
            .collect();
        for p in &config.platforms {
            deps.metrics.init_platform(p);
        }
        let sh = Arc::new(Shared {
            invoker: Arc::new(ObservedInvoker::new(deps.invoker, status.clone())),
            registry: deps.registry,
            sinks: deps.sinks,
            lease: deps.lease,
            secrets: deps.secrets,
            gates: deps.gates,
            tenants: deps.tenants,
            status: status.clone(),
            connector_metrics: deps.connector_metrics,
            metrics: deps.metrics,
            frame_metrics: deps.frame_metrics,
            webhook: deps.webhook,
            activator: deps.activator,
            start_sem: Arc::new(Semaphore::new(config.start_concurrency)),
            pacers,
            heartbeat_enabled: config.heartbeat_enabled,
            backoff_cap: config.backoff_cap,
            lease_ttl: config.lease_ttl,
            lease_renew_every: config.lease_renew_every,
        });
        Ok(Self {
            feed: deps.feed,
            sh,
            config,
            status,
        })
    }

    /// Runs until `shutdown` resolves (or its sender drops), then stops every
    /// connection (stop signal, [`STOP_TIMEOUT`], abort) and releases leases.
    pub async fn run(self, mut shutdown: oneshot::Receiver<()>) {
        let sh = self.sh.clone();
        let make = move |d: Arc<DesiredConnection>, rx: oneshot::Receiver<()>| {
            let sh = sh.clone();
            async move {
                let key = d.key.clone();
                let outcome = AssertUnwindSafe(run_connection(sh.clone(), d, rx))
                    .catch_unwind()
                    .await;
                let end = match outcome {
                    Ok(end) => end,
                    Err(_) => {
                        // The panic payload is never logged: it can carry
                        // frame or handle text.
                        tracing::error!(
                            platform = key.platform,
                            community_id = key.community_id,
                            connection_id = key.connection_id,
                            "connector connection panicked; isolating it"
                        );
                        TaskEnd::Failed(RestartReason::Panic)
                    }
                };
                (key, end)
            }
        };
        let mut core = Core {
            make,
            tasks: FuturesUnordered::new(),
            slots: HashMap::new(),
            sh: self.sh.clone(),
            status: self.status.clone(),
            config: self.config.clone(),
        };
        let platforms = self.config.platforms.clone();
        'outer: loop {
            for p in &platforms {
                tokio::select! {
                    _ = &mut shutdown => break 'outer,
                    () = core.reconcile(&self.feed, p) => {}
                }
            }
            let wait = self.config.reconcile_interval + jitter(self.config.reconcile_interval);
            tokio::select! {
                _ = &mut shutdown => break 'outer,
                () = core.pump(tokio::time::sleep(wait)) => {}
            }
        }
        core.shutdown().await;
    }

    /// Shared readiness view for `/readyz`.
    pub fn ready_handle(&self) -> Arc<dyn RuntimeReady> {
        self.status.clone()
    }

    /// The full status (per-platform `/readyz` fields); install it with
    /// [`super::ready::install`] to surface it on `/readyz`.
    pub fn status_handle(&self) -> Arc<RuntimeStatus> {
        self.status.clone()
    }
}

#[cfg(test)]
mod config_tests {
    use super::*;

    #[test]
    fn defaults_validate_and_match_the_spec_table() {
        let c = RuntimeConfig::new(vec!["twitch", "discord", "youtube"]);
        c.validate().expect("defaults valid");
        assert_eq!(c.reconcile_interval, Duration::from_secs(60));
        assert_eq!((c.max_connections, c.start_concurrency), (500, 8));
        assert_eq!(c.restart_cooldown, Duration::from_secs(120));
        assert_eq!(c.lease_ttl, Duration::from_secs(250));
        assert_eq!(c.stop_timeout, Duration::from_secs(10));
    }

    #[test]
    fn every_invalid_field_is_rejected() {
        let ok = || RuntimeConfig::new(vec!["twitch"]);
        let cases: Vec<(&str, RuntimeConfig)> = vec![
            ("no platforms", RuntimeConfig::new(vec![])),
            ("bad platform", RuntimeConfig::new(vec!["slack"])),
            (
                "zero interval",
                RuntimeConfig {
                    reconcile_interval: Duration::ZERO,
                    ..ok()
                },
            ),
            (
                "zero feed timeout",
                RuntimeConfig {
                    feed_timeout: Duration::ZERO,
                    ..ok()
                },
            ),
            (
                "zero cap",
                RuntimeConfig {
                    max_connections: 0,
                    ..ok()
                },
            ),
            (
                "zero concurrency",
                RuntimeConfig {
                    start_concurrency: 0,
                    ..ok()
                },
            ),
            (
                "zero ttl",
                RuntimeConfig {
                    lease_ttl: Duration::ZERO,
                    ..ok()
                },
            ),
            (
                "renew too slow",
                RuntimeConfig {
                    lease_renew_every: Duration::from_secs(200),
                    ..ok()
                },
            ),
            (
                "pace for disabled platform",
                ok().with_start_pace("discord", Duration::from_secs(1)),
            ),
        ];
        for (name, c) in cases {
            assert!(c.validate().is_err(), "{name} must be rejected");
        }
    }

    #[test]
    fn jitter_stays_within_twenty_percent() {
        let base = Duration::from_secs(60);
        for _ in 0..200 {
            assert!(jitter(base) <= base.mul_f64(0.2));
        }
        assert_eq!(jitter(Duration::ZERO), Duration::ZERO);
    }
}

#[cfg(test)]
mod tests;
