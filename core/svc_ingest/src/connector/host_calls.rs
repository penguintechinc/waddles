//! The connector host-call bridge, svc-ingest side (connector runtime spec
//! `docs/superpowers/specs/2026-11-05-connector-runtime-inbound-manager.md`
//! decision 2.4, PR-A2).
//!
//! The executor dials svc-ingest's host API and every host-call a connector
//! connector makes (`identity.lookup`, `get-context`, `flags`, `log`, `clock`)
//! arrives here as a `host-call` frame. [`ConnectorHostCalls`] is the explicit
//! allowlist that services them; everything it does not name is refused with a
//! typed `denied` result. The link's default (no handler) stays "deny all".
//!
//! **Scope is host-derived and fail-closed.** The executor stamps
//! `args._scope = {connection_id}` into every host-call of an export that
//! carries a `ConnectionCtx` (`on-connect`/`on-frame`/...), after guest code, so
//! the guest cannot choose it. `tool-call` and `build-request` exports carry no
//! ctx, so the executor strips the key and those calls arrive UNSCOPED. The
//! handler maps the stamped id to a [`CallScope`] through a host-owned
//! [`ScopeSource`] (the supervisor's ACTIVE-connection table) and DENIES:
//!
//! | Situation | Result |
//! |---|---|
//! | no / malformed `_scope` | `denied` (`unscoped_call`) |
//! | `connection_id` not active (unknown, stale) | `denied` (`unknown_connection`) |
//! | `app_id` is not a known connector | `denied` (`unknown_connector`) |
//! | connector's platform differs from the connection's | `denied` (`platform_mismatch`) |
//! | op needs a grant the connector's manifest lacks | `denied` (`grant_missing`) |
//!
//! There is no fallback to a default scope, a guessed community, or a guest
//! supplied value: the community, tenant and platform come from the
//! [`CallScope`] only. Grants per connector are a strict subset of the connector's
//! `bundles/rust/<platform>/hub-manifest.yaml` permissions (a unit test parses
//! the real manifests and asserts it).
//!
//! **PII.** `identity.lookup` returns only the pseudonymous UUID (hub-api owns
//! the PII boundary): never a handle or display name. No platform user id, UUID
//! or guest log text reaches a log line or a metric label.

use std::collections::{HashMap, HashSet};
use std::fmt;
use std::sync::{Arc, LazyLock, Mutex, RwLock};
use std::time::{Duration, Instant};

use hub_client::{HubClient, HubClientError};
use penguin_bundle_host::wire::{CapabilityKind, HostCallBody};
use prometheus::{IntCounterVec, Opts, Registry};
use service_auth::{MachineJwtClient, ServiceAuthError};
use tracing::{debug, error, warn};

use super::webhook::BoxFut;

/// Reserved top-level `args` key the executor stamps the host scope into.
pub const SCOPE_KEY: &str = "_scope";
/// Per-host-call wall-clock deadline (`EXECUTOR_CALL_TIMEOUT_MS` default).
pub const HOST_CALL_DEADLINE: Duration = Duration::from_secs(2);
/// In-flight host-calls allowed per connector.
pub const MAX_IN_FLIGHT_PER_CONNECTOR: usize = 16;
/// In-flight host-calls allowed per connection (per-scope fairness).
pub const MAX_IN_FLIGHT_PER_CONNECTION: usize = 4;
/// `identity.lookup` calls per second allowed per connector.
pub const IDENTITY_RATE_PER_SEC: u32 = 200;
/// Longest accepted platform user id (bytes).
pub const MAX_PLATFORM_USER_ID_LEN: usize = 128;
/// Machine-JWT scope hub-api requires for `MintEphemeralPseudonyms`.
pub const IDENTITY_MINT_SCOPE: &str = "identity:ephemeral:mint";

/// A permission an connector's `hub-manifest.yaml` declares and the host honors.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Grant {
    /// `connector.pii.read`: gates `identity.lookup` and `get-context`.
    PiiRead,
    /// `flags.read`: gates `flags.enabled` / `flags.tier`.
    FlagsRead,
    /// `platform.log`: gates `log.write`.
    PlatformLog,
}

impl Grant {
    /// The permission id as written in the manifest.
    #[must_use]
    pub const fn manifest_id(self) -> &'static str {
        match self {
            Self::PiiRead => "connector.pii.read",
            Self::FlagsRead => "flags.read",
            Self::PlatformLog => "platform.log",
        }
    }
}

/// One platform connector the host services: its identity and its grants.
#[derive(Debug, Clone, Copy)]
pub struct Connector {
    /// Platform slug, the only platform this connector may act on.
    pub platform: &'static str,
    /// The connector's `app_id` (set by the executor, never the guest).
    pub app_id: &'static str,
    /// The grants the host honors for this connector (subset of its manifest).
    pub grants: &'static [Grant],
}

impl Connector {
    /// True when this connector holds `grant`.
    #[must_use]
    pub fn has(&self, grant: Grant) -> bool {
        self.grants.contains(&grant)
    }
}

/// Every connector the host-call bridge knows, with its manifest-derived grants.
pub const CONNECTORS: &[Connector] = &[
    Connector {
        platform: "twitch",
        app_id: "waddles.core.service.twitch",
        grants: &[Grant::PiiRead, Grant::FlagsRead, Grant::PlatformLog],
    },
    Connector {
        platform: "discord",
        app_id: "waddles.core.service.discord",
        grants: &[Grant::PiiRead, Grant::PlatformLog],
    },
    Connector {
        platform: "youtube",
        app_id: "waddles.core.service.youtube",
        grants: &[Grant::PiiRead],
    },
];

/// The connector registered for `app_id`, if any.
#[must_use]
pub fn connector_for(app_id: &str) -> Option<&'static Connector> {
    CONNECTORS.iter().find(|o| o.app_id == app_id)
}

/// The connector registered for `platform`, if any.
#[must_use]
pub fn platform_slug(platform: &str) -> Option<&'static str> {
    CONNECTORS
        .iter()
        .find(|o| o.platform == platform)
        .map(|o| o.platform)
}

/// The host-derived scope of one connector connection. Never built from guest
/// data: it comes out of the active-connection table.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CallScope {
    /// Platform slug.
    pub platform: &'static str,
    /// Owning community (numeric, >= 1).
    pub community_id: i64,
    /// Owning tenant (numeric, >= 1).
    pub tenant_id: i64,
    /// Hub-api connection row (numeric, >= 1).
    pub connection_id: i64,
}

/// Why a call was refused. A bounded set so it can be a metric label.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DenyReason {
    /// The call carried no (valid) `_scope`.
    UnscopedCall,
    /// The stamped `connection_id` is not an active connection.
    UnknownConnection,
    /// The call's `app_id` is not a known connector.
    UnknownConnector,
    /// The connector is not the one serving this connection's platform.
    PlatformMismatch,
    /// The connector's manifest lacks the grant the op needs.
    GrantMissing,
    /// The capability or op is not on the allowlist.
    NotServiced,
    /// `identity.lookup` of a kind svc-ingest must not serve (uuid -> PII).
    UnsupportedKind,
    /// `args.tenant` is absent.
    TenantMissing,
    /// `args.tenant` differs from the connection's tenant.
    TenantMismatch,
}

impl DenyReason {
    /// Stable label (metric label and wire reason).
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::UnscopedCall => "unscoped_call",
            Self::UnknownConnection => "unknown_connection",
            Self::UnknownConnector => "unknown_connector",
            Self::PlatformMismatch => "platform_mismatch",
            Self::GrantMissing => "grant_missing",
            Self::NotServiced => "not_serviced",
            Self::UnsupportedKind => "unsupported_kind",
            Self::TenantMissing => "tenant_missing",
            Self::TenantMismatch => "tenant_mismatch",
        }
    }
}

/// A typed host-call failure, mapped 1:1 onto the wire `HostResultError`.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum HostCallError {
    /// Refused by the allowlist / scope checks.
    #[error("host-call denied: {}", .0.as_str())]
    Denied(DenyReason),
    /// The args were malformed.
    #[error("host-call invalid: {0}")]
    Invalid(&'static str),
    /// The identity does not exist (erased / never minted).
    #[error("host-call target not found")]
    NotFound,
    /// Over the per-connector rate; retry after this many seconds.
    #[error("host-call rate limited")]
    RateLimited(u32),
    /// Over an in-flight cap; not queued.
    #[error("host-call quota exceeded")]
    QuotaExceeded,
    /// The backing service failed or the 2 s deadline elapsed.
    #[error("host-call unavailable")]
    Unavailable,
}

impl HostCallError {
    /// The wire `HostResultError::code` the executor maps (`denied`,
    /// `not_found` and `rate_limited` have dedicated WIT variants).
    #[must_use]
    pub const fn code(&self) -> &'static str {
        match self {
            Self::Denied(_) => "denied",
            Self::Invalid(_) => "invalid_args",
            Self::NotFound => "not_found",
            Self::RateLimited(_) => "rate_limited",
            Self::QuotaExceeded => "quota_exceeded",
            Self::Unavailable => "unavailable",
        }
    }

    /// The wire message: fixed text (and the retry-after seconds for
    /// `rate_limited`, which the executor parses). Never guest data.
    #[must_use]
    pub fn wire_message(&self) -> String {
        match self {
            Self::Denied(r) => r.as_str().to_string(),
            Self::Invalid(m) => (*m).to_string(),
            Self::RateLimited(secs) => secs.to_string(),
            Self::NotFound => "not_found".to_string(),
            Self::QuotaExceeded => "quota_exceeded".to_string(),
            Self::Unavailable => "unavailable".to_string(),
        }
    }

    /// Bounded outcome label for the call counter.
    #[must_use]
    pub const fn outcome(&self) -> &'static str {
        self.code()
    }
}

/// Resolves a call's connection to its host-owned [`CallScope`].
pub trait ScopeSource: Send + Sync {
    /// The scope of an ACTIVE connection; `None` for an unknown or stale id.
    fn scope_of(&self, connection_id: i64) -> Option<CallScope>;
}

/// Why a scope could not be registered as active.
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum ActivateError {
    /// An id was not a positive number.
    #[error("scope ids must be positive")]
    InvalidId,
    /// The platform is not one the host-call bridge serves.
    #[error("platform has no host-call connector")]
    UnknownPlatform,
    /// The table's lock was poisoned; nothing can be trusted.
    #[error("active-connection table is unusable")]
    Poisoned,
    /// A DIFFERENT scope (other platform/community/tenant) is already active for
    /// this connection id; it is never overwritten.
    #[error("a different scope is already active for this connection id")]
    Conflict,
}

/// The host-owned table of ACTIVE connections: `connection_id -> scope`. The
/// supervisor `activate`s a connection when its driver starts and
/// `deactivate`s it when it ends, so a stale id is refused.
#[derive(Default)]
pub struct ActiveConnections {
    /// `connection_id -> (scope, live registrations of exactly that scope)`.
    table: RwLock<HashMap<i64, (CallScope, usize)>>,
}

impl ActiveConnections {
    /// An empty table.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Registers the scope of a live connection. Re-registering the SAME scope
    /// (a restart overlapping its predecessor) counts one more registration; a
    /// DIFFERENT scope for an id that is already active is refused, never
    /// overwritten -- one connection id can never be re-pointed at another
    /// community's scope while a registration is live.
    ///
    /// # Errors
    /// [`ActivateError`] when an id is not positive, the platform has no
    /// connector, the table is poisoned, or the id is active under another scope.
    pub fn activate(&self, scope: CallScope) -> Result<(), ActivateError> {
        if scope.community_id < 1 || scope.tenant_id < 1 || scope.connection_id < 1 {
            return Err(ActivateError::InvalidId);
        }
        if platform_slug(scope.platform).is_none() {
            return Err(ActivateError::UnknownPlatform);
        }
        let mut table = self.table.write().map_err(|_| ActivateError::Poisoned)?;
        match table.get_mut(&scope.connection_id) {
            Some((existing, n)) if *existing == scope => *n += 1,
            Some(_) => return Err(ActivateError::Conflict),
            None => {
                table.insert(scope.connection_id, (scope, 1));
            }
        }
        Ok(())
    }

    /// Drops ONE registration of exactly `scope` (compare-and-remove): the
    /// entry goes away only when its last registration of that same scope is
    /// released, and a guard holding a different (stale) scope removes nothing.
    pub fn deactivate(&self, scope: &CallScope) {
        match self.table.write() {
            Ok(mut table) => {
                let last = match table.get_mut(&scope.connection_id) {
                    Some((existing, n)) if existing == scope => {
                        *n = n.saturating_sub(1);
                        *n == 0
                    }
                    _ => false,
                };
                if last {
                    table.remove(&scope.connection_id);
                }
            }
            Err(_) => error!("active-connection table poisoned; deactivate dropped"),
        }
    }

    /// Number of active connections.
    #[must_use]
    pub fn len(&self) -> usize {
        match self.table.read() {
            Ok(t) => t.len(),
            Err(_) => 0,
        }
    }

    /// True when no connection is active.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

impl ScopeSource for ActiveConnections {
    fn scope_of(&self, connection_id: i64) -> Option<CallScope> {
        match self.table.read() {
            Ok(t) => t.get(&connection_id).map(|(sc, _)| sc.clone()),
            Err(_) => {
                // A poisoned table can no longer be trusted: fail closed.
                error!("active-connection table poisoned; refusing every scope lookup");
                None
            }
        }
    }
}

/// Why tokenizing an identity failed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TokenizeError {
    /// The identity does not exist.
    NotFound,
    /// hub-api refused the machine credential or the tenant.
    Denied,
    /// hub-api rate limited the mint.
    RateLimited,
    /// The request was rejected as malformed.
    Invalid,
    /// hub-api was unreachable or answered unusably.
    Unavailable,
}

impl From<TokenizeError> for HostCallError {
    fn from(e: TokenizeError) -> Self {
        match e {
            TokenizeError::NotFound => Self::NotFound,
            TokenizeError::Denied => Self::Denied(DenyReason::GrantMissing),
            TokenizeError::RateLimited => Self::RateLimited(1),
            TokenizeError::Invalid => Self::Invalid("identity.lookup rejected"),
            TokenizeError::Unavailable => Self::Unavailable,
        }
    }
}

/// Turns a platform user id into its canonical pseudonymous UUID inside the
/// PII boundary. The raw id goes in; only the UUID comes out.
pub trait IdentityTokenizer: Send + Sync {
    /// The canonical UUID for `platform_user_id` on `scope.platform`, minted
    /// under `scope.tenant_id`.
    fn tokenize<'a>(
        &'a self,
        scope: &'a CallScope,
        platform_user_id: &'a str,
    ) -> BoxFut<'a, Result<String, TokenizeError>>;
}

/// Maps a hub client failure to its typed class. Carries no content.
fn classify(e: &HubClientError) -> TokenizeError {
    match e {
        HubClientError::Grpc(s) => match s.code() {
            tonic::Code::NotFound => TokenizeError::NotFound,
            tonic::Code::PermissionDenied | tonic::Code::Unauthenticated => TokenizeError::Denied,
            tonic::Code::ResourceExhausted => TokenizeError::RateLimited,
            tonic::Code::InvalidArgument | tonic::Code::FailedPrecondition => {
                TokenizeError::Invalid
            }
            _ => TokenizeError::Unavailable,
        },
        HubClientError::Auth(ServiceAuthError::BootstrapRejected(_)) => TokenizeError::Denied,
        _ => TokenizeError::Unavailable,
    }
}

/// The hub-api backed [`IdentityTokenizer`]: `MintEphemeralPseudonyms` with
/// svc-ingest's machine JWT. The handle is never sent (the connector only hands
/// over the platform user id), so hub-api records no display handle.
pub struct HubIdentityTokenizer {
    client: HubClient,
}

impl HubIdentityTokenizer {
    /// Wraps a client whose token client carries [`IDENTITY_MINT_SCOPE`].
    #[must_use]
    pub fn from_client(client: HubClient) -> Self {
        Self { client }
    }

    /// A loopback/plaintext channel with the mint scope (tests, dev).
    #[must_use]
    pub fn from_channel(
        channel: tonic::transport::Channel,
        token_endpoint: impl Into<String>,
        sa_token_path: impl Into<String>,
    ) -> Self {
        let tokens = Arc::new(MachineJwtClient::new(
            token_endpoint,
            sa_token_path,
            IDENTITY_MINT_SCOPE,
        ));
        Self::from_client(HubClient::with_channel(channel, tokens))
    }
}

impl IdentityTokenizer for HubIdentityTokenizer {
    fn tokenize<'a>(
        &'a self,
        scope: &'a CallScope,
        platform_user_id: &'a str,
    ) -> BoxFut<'a, Result<String, TokenizeError>> {
        Box::pin(async move {
            let item = hub_client::pb::MintEphemeralPseudonymRequest {
                tenant_id: scope.tenant_id.to_string(),
                platform: scope.platform.to_string(),
                platform_user_id: platform_user_id.to_string(),
                handle: String::new(),
            };
            let minted = self
                .client
                .mint_ephemeral_pseudonyms(vec![item])
                .await
                .map_err(|e| {
                    let class = classify(&e);
                    warn!(?class, "identity mint failed");
                    class
                })?;
            let mut matching = minted
                .into_iter()
                .filter(|p| p.platform_user_id == platform_user_id);
            let (Some(found), None) = (matching.next(), matching.next()) else {
                warn!("identity mint reply did not carry exactly one pseudonym for the id");
                return Err(TokenizeError::Unavailable);
            };
            match uuid::Uuid::parse_str(&found.pseudonym) {
                Ok(u) if !u.is_nil() => Ok(u.hyphenated().to_string()),
                _ => {
                    error!("identity mint returned a malformed or nil pseudonym; refusing it");
                    Err(TokenizeError::Unavailable)
                }
            }
        })
    }
}

/// Where a host-call came from, as recorded by the HOST link from its own
/// pending-invocation table (keyed by the call's `call_id`, the `invoke` frame
/// id the call happened during) -- never from guest-supplied data.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CallOrigin {
    /// Not known to be anything special: a call with no `_scope` stamp is
    /// UNSCOPED and denied.
    Other,
    /// During a `sender.build-request` invocation this host sent for the
    /// pinned connector `app_id`. `build-request` carries no `ConnectionCtx`, so
    /// its calls have no connection scope; the ONLY thing this origin unlocks is
    /// the connector's own cutover-flag read (see [`ConnectorHostCalls`]).
    BuildRequest {
        /// The `app_id` the host invoked (host constant, from the registry).
        app_id: String,
    },
}

/// The host-call servicing seam [`super::wire_invoker::ExecutorLink`] plugs in.
///
/// Takes the raw call (not a pre-resolved scope): resolving the scope, and
/// denying an unscoped call, is the handler's job so a link can never skip it.
pub trait HostCallHandler: Send + Sync {
    /// Services one connector `host-call`, or refuses it typed.
    fn handle<'a>(
        &'a self,
        call: &'a HostCallBody,
    ) -> BoxFut<'a, Result<serde_json::Value, HostCallError>>;

    /// [`Self::handle`] with the host-recorded [`CallOrigin`]. The default
    /// ignores the origin (a handler with no origin-specific rules); the link
    /// always calls this form.
    fn handle_from<'a>(
        &'a self,
        call: &'a HostCallBody,
        _origin: &'a CallOrigin,
    ) -> BoxFut<'a, Result<serde_json::Value, HostCallError>> {
        self.handle(call)
    }
}

static CALLS: LazyLock<IntCounterVec> = LazyLock::new(|| {
    // The definition is a compile-time constant; construction cannot fail.
    #[allow(clippy::expect_used)]
    IntCounterVec::new(
        Opts::new(
            "connector_host_call_total",
            "Connector host-calls serviced by svc-ingest, by capability and outcome",
        ),
        &["capability", "outcome"],
    )
    .expect("valid metric definition")
});

static DENIED: LazyLock<IntCounterVec> = LazyLock::new(|| {
    // The definition is a compile-time constant; construction cannot fail.
    #[allow(clippy::expect_used)]
    IntCounterVec::new(
        Opts::new(
            "connector_host_call_denied_total",
            "Connector host-calls refused by svc-ingest, by bounded reason",
        ),
        &["reason"],
    )
    .expect("valid metric definition")
});

/// Registers the host-call counters on the service registry.
///
/// # Errors
/// Propagates a duplicate-registration error from `prometheus`.
pub fn register_metrics(registry: &Registry) -> Result<(), prometheus::Error> {
    registry.register(Box::new(CALLS.clone()))?;
    registry.register(Box::new(DENIED.clone()))
}

/// Count of one `(capability, outcome)` pair (test/observability helper).
#[must_use]
pub fn call_count(capability: &str, outcome: &str) -> u64 {
    CALLS.with_label_values(&[capability, outcome]).get()
}

/// Count of one denial reason (test/observability helper).
#[must_use]
pub fn denied_count(reason: &str) -> u64 {
    DENIED.with_label_values(&[reason]).get()
}

/// Bounded capability label.
const fn capability_label(c: CapabilityKind) -> &'static str {
    match c {
        CapabilityKind::Http => "http",
        CapabilityKind::Kv => "kv",
        CapabilityKind::Db => "db",
        CapabilityKind::Relay => "relay",
        CapabilityKind::Flags => "flags",
        CapabilityKind::Log => "log",
        CapabilityKind::Clock => "clock",
        CapabilityKind::Context => "context",
    }
}

/// Tunables; the defaults are the spec's numbers.
#[derive(Debug, Clone)]
pub struct HostCallConfig {
    /// Platforms whose connector cutover flag is ON by env baseline.
    pub connector_enabled: HashSet<&'static str>,
    /// Per-call deadline.
    pub deadline: Duration,
    /// In-flight cap per connector.
    pub max_per_connector: usize,
    /// In-flight cap per connection.
    pub max_per_connection: usize,
    /// `identity.lookup` per-second budget per connector.
    pub identity_rate_per_sec: u32,
}

/// An unknown platform in the enabled-connector baseline.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("platform `{0}` has no host-call connector")]
pub struct UnknownPlatform(pub String);

impl HostCallConfig {
    /// The spec defaults with the given connectors' cutover flags ON.
    ///
    /// # Errors
    /// [`UnknownPlatform`] for a name that is not a known connector platform.
    pub fn new<I, S>(enabled_platforms: I) -> Result<Self, UnknownPlatform>
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        let mut connector_enabled = HashSet::new();
        for p in enabled_platforms {
            let slug =
                platform_slug(p.as_ref()).ok_or_else(|| UnknownPlatform(p.as_ref().to_string()))?;
            connector_enabled.insert(slug);
        }
        Ok(Self {
            connector_enabled,
            deadline: HOST_CALL_DEADLINE,
            max_per_connector: MAX_IN_FLIGHT_PER_CONNECTOR,
            max_per_connection: MAX_IN_FLIGHT_PER_CONNECTION,
            identity_rate_per_sec: IDENTITY_RATE_PER_SEC,
        })
    }
}

/// A token bucket refilled continuously at `rate` per second.
struct Bucket {
    tokens: f64,
    last: Instant,
}

/// In-flight counters per connector and per connection.
#[derive(Default)]
struct Inflight {
    connector: HashMap<&'static str, usize>,
    connection: HashMap<i64, usize>,
}

/// An admitted call's slot; releases both counters on drop.
struct Permit {
    inflight: Arc<Mutex<Inflight>>,
    app_id: &'static str,
    connection_id: i64,
}

impl Drop for Permit {
    fn drop(&mut self) {
        let Ok(mut state) = self.inflight.lock() else {
            error!("in-flight table poisoned; a permit could not be released");
            return;
        };
        release(&mut state.connector, self.app_id);
        release(&mut state.connection, self.connection_id);
    }
}

/// Decrements `key`, removing it at zero so the table cannot grow.
fn release<K: std::hash::Hash + Eq>(map: &mut HashMap<K, usize>, key: K) {
    if let Some(n) = map.get_mut(&key) {
        *n = n.saturating_sub(1);
        if *n == 0 {
            map.remove(&key);
        }
    }
}

/// The production [`HostCallHandler`]: the explicit connector allowlist.
pub struct ConnectorHostCalls {
    scopes: Arc<dyn ScopeSource>,
    tokenizer: Arc<dyn IdentityTokenizer>,
    config: HostCallConfig,
    inflight: Arc<Mutex<Inflight>>,
    buckets: Mutex<HashMap<&'static str, Bucket>>,
    monotonic_origin: Instant,
}

impl fmt::Debug for ConnectorHostCalls {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ConnectorHostCalls")
            .field("config", &self.config)
            .finish_non_exhaustive()
    }
}

impl ConnectorHostCalls {
    /// Builds the handler over the active-connection table and tokenizer.
    #[must_use]
    pub fn new(
        scopes: Arc<dyn ScopeSource>,
        tokenizer: Arc<dyn IdentityTokenizer>,
        config: HostCallConfig,
    ) -> Self {
        Self {
            scopes,
            tokenizer,
            config,
            inflight: Arc::new(Mutex::new(Inflight::default())),
            buckets: Mutex::new(HashMap::new()),
            monotonic_origin: Instant::now(),
        }
    }

    /// Extracts the stamped connection id; any shape problem is "unscoped".
    fn stamped_connection_id(call: &HostCallBody) -> Result<i64, HostCallError> {
        let id = call
            .args
            .get(SCOPE_KEY)
            .and_then(|s| s.get("connection_id"))
            .and_then(serde_json::Value::as_str)
            .and_then(|s| s.parse::<i64>().ok())
            .filter(|id| *id >= 1);
        id.ok_or(HostCallError::Denied(DenyReason::UnscopedCall))
    }

    /// Resolves the scope and connector for `call`, or denies. The platform on
    /// the scope must be the connector's own platform.
    fn authorize(
        &self,
        call: &HostCallBody,
    ) -> Result<(CallScope, &'static Connector), HostCallError> {
        let connection_id = Self::stamped_connection_id(call)?;
        let scope = self
            .scopes
            .scope_of(connection_id)
            .ok_or(HostCallError::Denied(DenyReason::UnknownConnection))?;
        let connector = connector_for(&call.app_id)
            .ok_or(HostCallError::Denied(DenyReason::UnknownConnector))?;
        if connector.platform != scope.platform {
            return Err(HostCallError::Denied(DenyReason::PlatformMismatch));
        }
        Ok((scope, connector))
    }

    /// Admission: per-connector then per-connection in-flight caps.
    fn admit(
        &self,
        connector: &'static Connector,
        scope: &CallScope,
    ) -> Result<Permit, HostCallError> {
        let mut state = self
            .inflight
            .lock()
            .map_err(|_| HostCallError::Unavailable)?;
        let over_connector = state
            .connector
            .get(connector.app_id)
            .copied()
            .is_some_and(|n| n >= self.config.max_per_connector);
        let over_connection = state
            .connection
            .get(&scope.connection_id)
            .copied()
            .is_some_and(|n| n >= self.config.max_per_connection);
        if over_connector || over_connection {
            return Err(HostCallError::QuotaExceeded);
        }
        *state.connector.entry(connector.app_id).or_insert(0) += 1;
        *state.connection.entry(scope.connection_id).or_insert(0) += 1;
        Ok(Permit {
            inflight: Arc::clone(&self.inflight),
            app_id: connector.app_id,
            connection_id: scope.connection_id,
        })
    }

    /// Takes one `identity.lookup` token for `connector`.
    fn take_identity_token(&self, connector: &'static Connector) -> Result<(), HostCallError> {
        let rate = f64::from(self.config.identity_rate_per_sec);
        let mut buckets = self
            .buckets
            .lock()
            .map_err(|_| HostCallError::Unavailable)?;
        let now = Instant::now();
        let bucket = buckets.entry(connector.app_id).or_insert(Bucket {
            tokens: rate,
            last: now,
        });
        let elapsed = now.duration_since(bucket.last).as_secs_f64();
        bucket.tokens = (bucket.tokens + elapsed * rate).min(rate);
        bucket.last = now;
        if bucket.tokens < 1.0 {
            return Err(HostCallError::RateLimited(1));
        }
        bucket.tokens -= 1.0;
        Ok(())
    }

    /// Requires `grant` for the connector.
    fn require(connector: &Connector, grant: Grant) -> Result<(), HostCallError> {
        if connector.has(grant) {
            Ok(())
        } else {
            Err(HostCallError::Denied(DenyReason::GrantMissing))
        }
    }

    /// Services an AUTHORIZED, admitted call. `scope` is host-derived.
    async fn dispatch(
        &self,
        scope: &CallScope,
        connector: &'static Connector,
        call: &HostCallBody,
    ) -> Result<serde_json::Value, HostCallError> {
        match (call.capability, call.op.as_str()) {
            (CapabilityKind::Context, "get-context") => {
                Self::require(connector, Grant::PiiRead)?;
                Ok(serde_json::json!({
                    "tenant": scope.tenant_id.to_string(),
                    "community": scope.community_id.to_string(),
                    "app_id": connector.app_id,
                }))
            }
            (CapabilityKind::Db, "identity.lookup") => {
                Self::require(connector, Grant::PiiRead)?;
                self.identity_lookup(scope, connector, &call.args).await
            }
            (CapabilityKind::Flags, "enabled") => {
                Self::require(connector, Grant::FlagsRead)?;
                let key = call
                    .args
                    .get("key")
                    .and_then(serde_json::Value::as_str)
                    .ok_or(HostCallError::Invalid("flags.enabled requires a key"))?;
                let own_flag = format!("waddles.connector-{}", scope.platform);
                let on = key == own_flag && self.config.connector_enabled.contains(scope.platform);
                Ok(serde_json::json!({ "enabled": on }))
            }
            (CapabilityKind::Flags, "tier") => {
                Self::require(connector, Grant::FlagsRead)?;
                Ok(serde_json::json!({ "tier": "free" }))
            }
            (CapabilityKind::Log, "write") => {
                Self::require(connector, Grant::PlatformLog)?;
                Self::log_write(scope, connector, &call.args)
            }
            (CapabilityKind::Clock, "now-millis") => {
                let now = chrono::Utc::now().timestamp_millis();
                u64::try_from(now)
                    .map(serde_json::Value::from)
                    .map_err(|_| HostCallError::Unavailable)
            }
            (CapabilityKind::Clock, "now-rfc3339") => Ok(serde_json::Value::String(
                chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true),
            )),
            (CapabilityKind::Clock, "monotonic-nanos") => {
                u64::try_from(self.monotonic_origin.elapsed().as_nanos())
                    .map(serde_json::Value::from)
                    .map_err(|_| HostCallError::Unavailable)
            }
            _ => Err(HostCallError::Denied(DenyReason::NotServiced)),
        }
    }

    /// `identity.lookup`: platform-identity only, tenant cross-checked.
    async fn identity_lookup(
        &self,
        scope: &CallScope,
        connector: &'static Connector,
        args: &serde_json::Value,
    ) -> Result<serde_json::Value, HostCallError> {
        let kind = args.get("kind").and_then(serde_json::Value::as_str);
        match kind {
            Some("platform-identity") => {}
            // A uuid -> handle read is detokenization: only hub-api may do it.
            Some("uuid") => return Err(HostCallError::Denied(DenyReason::UnsupportedKind)),
            _ => return Err(HostCallError::Invalid("identity.lookup kind")),
        }
        // Cross-check only: the tenant used is always the scope's.
        let tenant = scope.tenant_id.to_string();
        match args.get("tenant").and_then(serde_json::Value::as_str) {
            Some(t) if t == tenant => {}
            Some(_) => return Err(HostCallError::Denied(DenyReason::TenantMismatch)),
            None => return Err(HostCallError::Denied(DenyReason::TenantMissing)),
        }
        match args.get("platform").and_then(serde_json::Value::as_str) {
            Some(p) if p == scope.platform => {}
            _ => return Err(HostCallError::Denied(DenyReason::PlatformMismatch)),
        }
        let id = args
            .get("platform_user_id")
            .and_then(serde_json::Value::as_str)
            .filter(|s| {
                !s.is_empty()
                    && s.len() <= MAX_PLATFORM_USER_ID_LEN
                    && !s.chars().any(char::is_control)
            })
            .ok_or(HostCallError::Invalid("identity.lookup platform_user_id"))?;
        self.take_identity_token(connector)?;
        let uuid = self.tokenizer.tokenize(scope, id).await?;
        Ok(serde_json::json!({
            "uuid": uuid,
            "linked": false,
            "handle": null,
            "display_name": null,
            "tenant": tenant,
        }))
    }

    /// `log.write`: a fixed-field, PII-free line. The guest's text and fields
    /// are dropped on purpose (only level and size are kept).
    fn log_write(
        scope: &CallScope,
        connector: &Connector,
        args: &serde_json::Value,
    ) -> Result<serde_json::Value, HostCallError> {
        let level = args
            .get("level")
            .and_then(serde_json::Value::as_str)
            .ok_or(HostCallError::Invalid("log.write level"))?;
        let message_len = args
            .get("message")
            .and_then(serde_json::Value::as_str)
            .map_or(0, str::len);
        match level {
            "trace" | "debug" => debug!(
                app_id = connector.app_id,
                community_id = scope.community_id,
                message_len,
                "connector log"
            ),
            "info" => tracing::info!(
                app_id = connector.app_id,
                community_id = scope.community_id,
                message_len,
                "connector log"
            ),
            "warn" => warn!(
                app_id = connector.app_id,
                community_id = scope.community_id,
                message_len,
                "connector log"
            ),
            "error" => error!(
                app_id = connector.app_id,
                community_id = scope.community_id,
                message_len,
                "connector log"
            ),
            _ => return Err(HostCallError::Invalid("log.write level")),
        }
        Ok(serde_json::json!({}))
    }

    /// A `build-request` flag read: the one UNSCOPED call shape that is served.
    ///
    /// `sender.build-request` has no `ConnectionCtx`, so the executor stamps no
    /// `_scope`. The scope here is the HOST's own bookkeeping instead: the link
    /// matched the call's `call_id` to a build-request invocation it sent for
    /// `invoked_app_id`. Unscoped calls with any other origin stay denied, and
    /// even from this origin only the connector's own cutover flag
    /// (`waddles.connector-<platform>`, answered from the env baseline) is served:
    /// no identity, context, log or any other capability.
    fn build_request_flag(
        &self,
        call: &HostCallBody,
        invoked_app_id: &str,
    ) -> Result<serde_json::Value, HostCallError> {
        let unscoped = HostCallError::Denied(DenyReason::UnscopedCall);
        if call.app_id != invoked_app_id {
            return Err(HostCallError::Denied(DenyReason::UnknownConnector));
        }
        let connector = connector_for(&call.app_id)
            .ok_or(HostCallError::Denied(DenyReason::UnknownConnector))?;
        if call.capability != CapabilityKind::Flags {
            return Err(unscoped);
        }
        Self::require(connector, Grant::FlagsRead)?;
        let _permit = self.admit_connector(connector)?;
        match call.op.as_str() {
            "enabled" => {
                let key = call
                    .args
                    .get("key")
                    .and_then(serde_json::Value::as_str)
                    .ok_or(HostCallError::Invalid("flags.enabled requires a key"))?;
                let own_flag = format!("waddles.connector-{}", connector.platform);
                let on =
                    key == own_flag && self.config.connector_enabled.contains(connector.platform);
                Ok(serde_json::json!({ "enabled": on }))
            }
            "tier" => Ok(serde_json::json!({ "tier": "free" })),
            _ => Err(unscoped),
        }
    }

    /// Per-connector admission only (a build-request call has no connection).
    fn admit_connector(&self, connector: &'static Connector) -> Result<Permit, HostCallError> {
        let mut state = self
            .inflight
            .lock()
            .map_err(|_| HostCallError::Unavailable)?;
        let over = state
            .connector
            .get(connector.app_id)
            .copied()
            .is_some_and(|n| n >= self.config.max_per_connector);
        if over {
            return Err(HostCallError::QuotaExceeded);
        }
        *state.connector.entry(connector.app_id).or_insert(0) += 1;
        Ok(Permit {
            inflight: Arc::clone(&self.inflight),
            app_id: connector.app_id,
            // Never a real id (those are >= 1): `release` is then a no-op.
            connection_id: 0,
        })
    }

    /// The full pipeline for one call, without metrics.
    async fn run(
        &self,
        call: &HostCallBody,
        origin: &CallOrigin,
    ) -> Result<serde_json::Value, HostCallError> {
        let (scope, connector) = match self.authorize(call) {
            Ok(ok) => ok,
            // Unscoped: only a host-recorded build-request origin may proceed,
            // and only to the flag read. Everything else keeps the denial.
            Err(HostCallError::Denied(DenyReason::UnscopedCall)) => {
                return match origin {
                    CallOrigin::BuildRequest { app_id } => self.build_request_flag(call, app_id),
                    CallOrigin::Other => Err(HostCallError::Denied(DenyReason::UnscopedCall)),
                };
            }
            Err(e) => return Err(e),
        };
        let _permit = self.admit(connector, &scope)?;
        match tokio::time::timeout(self.config.deadline, self.dispatch(&scope, connector, call))
            .await
        {
            Ok(result) => result,
            Err(_elapsed) => Err(HostCallError::Unavailable),
        }
    }
}

impl HostCallHandler for ConnectorHostCalls {
    fn handle<'a>(
        &'a self,
        call: &'a HostCallBody,
    ) -> BoxFut<'a, Result<serde_json::Value, HostCallError>> {
        self.handle_from(call, &CallOrigin::Other)
    }

    fn handle_from<'a>(
        &'a self,
        call: &'a HostCallBody,
        origin: &'a CallOrigin,
    ) -> BoxFut<'a, Result<serde_json::Value, HostCallError>> {
        Box::pin(async move {
            let result = self.run(call, origin).await;
            let capability = capability_label(call.capability);
            match &result {
                Ok(_) => CALLS.with_label_values(&[capability, "ok"]).inc(),
                Err(e) => {
                    CALLS.with_label_values(&[capability, e.outcome()]).inc();
                    if let HostCallError::Denied(reason) = e {
                        DENIED.with_label_values(&[reason.as_str()]).inc();
                        // Capability + reason only: never args, ids or text.
                        warn!(
                            capability,
                            reason = reason.as_str(),
                            "connector host-call denied"
                        );
                    }
                }
            }
            result
        })
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use std::net::TcpListener;
    use std::sync::atomic::{AtomicUsize, Ordering};

    use hub_client::pb::identity_service_server::{IdentityService, IdentityServiceServer};
    use hub_client::pb::{
        EphemeralPseudonym, MintEphemeralPseudonymsRequest, MintEphemeralPseudonymsResponse,
        ResolveDisplayNamesRequest, ResolveDisplayNamesResponse, ResolveHandleRequest,
        ResolveHandleResponse, ResolvePlatformUserRequest, ResolvePlatformUserResponse,
    };
    use tonic::{Request, Response, Status};
    use wiremock::matchers::{body_partial_json, method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    use super::*;

    const UUID_A: &str = "123e4567-e89b-12d3-a456-426614174000";

    /// A real [`IdentityTokenizer`] with a scripted outcome that records the
    /// scopes and ids it was asked to tokenize.
    struct Scripted {
        result: Result<String, TokenizeError>,
        delay: Duration,
        seen: Mutex<Vec<(CallScope, String)>>,
    }

    impl Scripted {
        fn ok() -> Arc<Self> {
            Self::with(Ok(UUID_A.to_string()), Duration::ZERO)
        }
        fn with(result: Result<String, TokenizeError>, delay: Duration) -> Arc<Self> {
            Arc::new(Self {
                result,
                delay,
                seen: Mutex::new(Vec::new()),
            })
        }
        fn calls(&self) -> usize {
            self.seen.lock().unwrap().len()
        }
    }

    impl IdentityTokenizer for Scripted {
        fn tokenize<'a>(
            &'a self,
            scope: &'a CallScope,
            platform_user_id: &'a str,
        ) -> BoxFut<'a, Result<String, TokenizeError>> {
            Box::pin(async move {
                self.seen
                    .lock()
                    .unwrap()
                    .push((scope.clone(), platform_user_id.to_string()));
                if !self.delay.is_zero() {
                    tokio::time::sleep(self.delay).await;
                }
                self.result.clone()
            })
        }
    }

    fn scope(platform: &'static str, community: i64, tenant: i64, conn: i64) -> CallScope {
        CallScope {
            platform,
            community_id: community,
            tenant_id: tenant,
            connection_id: conn,
        }
    }

    /// twitch conn 11 (community 5, tenant 9), discord conn 12 (community 6,
    /// tenant 9), youtube conn 13 (community 7, tenant 3).
    fn table() -> Arc<ActiveConnections> {
        let t = Arc::new(ActiveConnections::new());
        t.activate(scope("twitch", 5, 9, 11)).unwrap();
        t.activate(scope("discord", 6, 9, 12)).unwrap();
        t.activate(scope("youtube", 7, 3, 13)).unwrap();
        t
    }

    fn handler_with(
        tok: Arc<Scripted>,
        edit: impl FnOnce(&mut HostCallConfig),
    ) -> ConnectorHostCalls {
        let mut cfg = HostCallConfig::new(["twitch"]).unwrap();
        edit(&mut cfg);
        ConnectorHostCalls::new(table(), tok, cfg)
    }

    fn handler(tok: Arc<Scripted>) -> ConnectorHostCalls {
        handler_with(tok, |_| {})
    }

    fn call(
        app: &str,
        capability: CapabilityKind,
        op: &str,
        conn: Option<&str>,
        mut args: serde_json::Value,
    ) -> HostCallBody {
        if let Some(c) = conn {
            args[SCOPE_KEY] = serde_json::json!({ "connection_id": c });
        }
        HostCallBody {
            app_id: app.to_string(),
            capability,
            op: op.to_string(),
            args,
            call_id: 1,
        }
    }

    const TWITCH: &str = "waddles.core.service.twitch";
    const DISCORD: &str = "waddles.core.service.discord";
    const YOUTUBE: &str = "waddles.core.service.youtube";

    fn lookup(app: &str, conn: Option<&str>, tenant: &str, platform: &str) -> HostCallBody {
        call(
            app,
            CapabilityKind::Db,
            "identity.lookup",
            conn,
            serde_json::json!({
                "kind": "platform-identity", "platform": platform,
                "platform_user_id": "u-100", "tenant": tenant,
            }),
        )
    }

    fn denied(reason: DenyReason) -> HostCallError {
        HostCallError::Denied(reason)
    }

    #[tokio::test]
    async fn an_unscoped_call_is_denied_for_every_capability_and_tokenizes_nothing() {
        let tok = Scripted::ok();
        let h = handler(tok.clone());
        let before = denied_count("unscoped_call");
        let calls = [
            lookup(TWITCH, None, "9", "twitch"),
            call(
                TWITCH,
                CapabilityKind::Context,
                "get-context",
                None,
                serde_json::json!({}),
            ),
            call(
                TWITCH,
                CapabilityKind::Flags,
                "enabled",
                None,
                serde_json::json!({"key": "waddles.connector-twitch"}),
            ),
            call(
                DISCORD,
                CapabilityKind::Log,
                "write",
                None,
                serde_json::json!({"level": "info"}),
            ),
            call(
                TWITCH,
                CapabilityKind::Clock,
                "now-millis",
                None,
                serde_json::json!({}),
            ),
        ];
        for c in &calls {
            assert_eq!(
                h.handle(c).await,
                Err(denied(DenyReason::UnscopedCall)),
                "{:?}",
                c.op
            );
        }
        assert_eq!(tok.calls(), 0, "no PII call without a scope");
        // Process-wide counter shared with parallel tests: at least our five.
        assert!(denied_count("unscoped_call") - before >= 5);
    }

    #[tokio::test]
    async fn a_malformed_scope_is_treated_as_unscoped() {
        let h = handler(Scripted::ok());
        for forged in [
            serde_json::json!("11"),
            serde_json::json!({}),
            serde_json::json!({"connection_id": 11}),
            serde_json::json!({"connection_id": "0"}),
            serde_json::json!({"connection_id": "-4"}),
            serde_json::json!({"connection_id": "abc"}),
            serde_json::json!({"connection_id": null}),
        ] {
            let mut c = lookup(TWITCH, None, "9", "twitch");
            c.args[SCOPE_KEY] = forged.clone();
            assert_eq!(
                h.handle(&c).await,
                Err(denied(DenyReason::UnscopedCall)),
                "{forged}"
            );
        }
    }

    #[tokio::test]
    async fn an_unknown_or_deactivated_connection_is_denied() {
        let tok = Scripted::ok();
        let t = table();
        let h = ConnectorHostCalls::new(
            t.clone(),
            tok.clone(),
            HostCallConfig::new(["twitch"]).unwrap(),
        );
        assert_eq!(
            h.handle(&lookup(TWITCH, Some("999"), "9", "twitch")).await,
            Err(denied(DenyReason::UnknownConnection))
        );
        assert!(h
            .handle(&lookup(TWITCH, Some("11"), "9", "twitch"))
            .await
            .is_ok());
        t.deactivate(&scope("twitch", 5, 9, 11));
        assert_eq!(
            h.handle(&lookup(TWITCH, Some("11"), "9", "twitch")).await,
            Err(denied(DenyReason::UnknownConnection)),
            "a stale id is refused"
        );
        assert_eq!(tok.calls(), 1);
    }

    #[tokio::test]
    async fn community_and_tenant_come_from_the_scope_never_the_guest() {
        let tok = Scripted::ok();
        let h = handler(tok.clone());
        // Guest-supplied community/tenant fields in args are ignored.
        let ctx = call(
            DISCORD,
            CapabilityKind::Context,
            "get-context",
            Some("12"),
            serde_json::json!({"community": "5", "tenant": "1"}),
        );
        let v = h.handle(&ctx).await.unwrap();
        assert_eq!(v["community"], "6");
        assert_eq!(v["tenant"], "9");
        assert_eq!(v["app_id"], DISCORD);
        // The tokenizer sees the table's scope for the stamped connection.
        h.handle(&lookup(YOUTUBE, Some("13"), "3", "youtube"))
            .await
            .unwrap();
        let seen = tok.seen.lock().unwrap();
        assert_eq!(seen[0].0, scope("youtube", 7, 3, 13));
        assert_eq!(seen[0].1, "u-100");
    }

    #[tokio::test]
    async fn a_tenant_or_platform_that_is_not_the_scopes_is_denied() {
        let tok = Scripted::ok();
        let h = handler(tok.clone());
        assert_eq!(
            h.handle(&lookup(TWITCH, Some("11"), "10", "twitch")).await,
            Err(denied(DenyReason::TenantMismatch))
        );
        let mut missing = lookup(TWITCH, Some("11"), "9", "twitch");
        missing.args.as_object_mut().unwrap().remove("tenant");
        assert_eq!(
            h.handle(&missing).await,
            Err(denied(DenyReason::TenantMissing))
        );
        assert_eq!(
            h.handle(&lookup(TWITCH, Some("11"), "9", "discord")).await,
            Err(denied(DenyReason::PlatformMismatch))
        );
        let mut no_platform = lookup(TWITCH, Some("11"), "9", "twitch");
        no_platform.args.as_object_mut().unwrap().remove("platform");
        assert_eq!(
            h.handle(&no_platform).await,
            Err(denied(DenyReason::PlatformMismatch))
        );
        assert_eq!(tok.calls(), 0);
    }

    #[tokio::test]
    async fn an_connector_cannot_act_on_another_platforms_connection() {
        let tok = Scripted::ok();
        let h = handler(tok.clone());
        // discord connector naming the twitch connection (and vice versa).
        assert_eq!(
            h.handle(&lookup(DISCORD, Some("11"), "9", "twitch")).await,
            Err(denied(DenyReason::PlatformMismatch))
        );
        assert_eq!(
            h.handle(&lookup(TWITCH, Some("12"), "9", "discord")).await,
            Err(denied(DenyReason::PlatformMismatch))
        );
        assert_eq!(
            h.handle(&lookup(
                "waddles.core.service.rogue",
                Some("11"),
                "9",
                "twitch"
            ))
            .await,
            Err(denied(DenyReason::UnknownConnector))
        );
        assert_eq!(tok.calls(), 0);
    }

    #[tokio::test]
    async fn a_grant_the_manifest_lacks_is_denied() {
        let h = handler(Scripted::ok());
        let flags = |app, conn| {
            call(
                app,
                CapabilityKind::Flags,
                "enabled",
                Some(conn),
                serde_json::json!({"key": "waddles.connector-x"}),
            )
        };
        let tier = |app, conn| {
            call(
                app,
                CapabilityKind::Flags,
                "tier",
                Some(conn),
                serde_json::json!({}),
            )
        };
        let log = |app, conn| {
            call(
                app,
                CapabilityKind::Log,
                "write",
                Some(conn),
                serde_json::json!({"level": "info"}),
            )
        };
        for c in [
            flags(DISCORD, "12"),
            flags(YOUTUBE, "13"),
            tier(DISCORD, "12"),
            tier(YOUTUBE, "13"),
            log(YOUTUBE, "13"),
        ] {
            assert_eq!(
                h.handle(&c).await,
                Err(denied(DenyReason::GrantMissing)),
                "{:?} {}",
                c.capability,
                c.app_id
            );
        }
        // twitch and discord declare `platform.log` (diagnostics, PII-free); youtube does not.
        assert!(h.handle(&log(DISCORD, "12")).await.is_ok());
        assert!(h.handle(&log(TWITCH, "11")).await.is_ok());
        assert!(h.handle(&tier(TWITCH, "11")).await.is_ok());
    }

    #[tokio::test]
    async fn everything_off_the_allowlist_is_denied_and_counted() {
        let h = handler(Scripted::ok());
        let before = call_count("http", "denied");
        for (cap, op) in [
            (CapabilityKind::Http, "send"),
            (CapabilityKind::Kv, "get"),
            (CapabilityKind::Relay, "send"),
            (CapabilityKind::Db, "query"),
            (CapabilityKind::Context, "other"),
            (CapabilityKind::Clock, "sleep"),
            (CapabilityKind::Flags, "set"),
            (CapabilityKind::Log, "read"),
        ] {
            let c = call(TWITCH, cap, op, Some("11"), serde_json::json!({}));
            assert_eq!(
                h.handle(&c).await,
                Err(denied(DenyReason::NotServiced)),
                "{op}"
            );
        }
        assert!(call_count("http", "denied") - before >= 1);
        let uuid_kind = call(
            TWITCH,
            CapabilityKind::Db,
            "identity.lookup",
            Some("11"),
            serde_json::json!({"kind": "uuid", "uuid": UUID_A, "tenant": "9"}),
        );
        assert_eq!(
            h.handle(&uuid_kind).await,
            Err(denied(DenyReason::UnsupportedKind))
        );
    }

    #[tokio::test]
    async fn identity_lookup_returns_only_the_pseudonym() {
        let tok = Scripted::ok();
        let h = handler(tok.clone());
        let before = call_count("db", "ok");
        let v = h
            .handle(&lookup(TWITCH, Some("11"), "9", "twitch"))
            .await
            .unwrap();
        assert_eq!(
            v,
            serde_json::json!({
                "uuid": UUID_A, "linked": false, "handle": null,
                "display_name": null, "tenant": "9",
            })
        );
        assert!(call_count("db", "ok") - before >= 1);
        // The PII-bearing id is in the call, never in a metric label.
        let text = prometheus::TextEncoder::new()
            .encode_to_string(&{
                let r = Registry::new();
                register_metrics(&r).unwrap();
                r.gather()
            })
            .unwrap();
        assert!(!text.contains("u-100") && !text.contains(UUID_A));
    }

    #[tokio::test]
    async fn identity_lookup_rejects_malformed_args() {
        let h = handler(Scripted::ok());
        let with = |k: &str, v: serde_json::Value| {
            let mut c = lookup(TWITCH, Some("11"), "9", "twitch");
            c.args[k] = v;
            c
        };
        for c in [
            with("kind", serde_json::json!("other")),
            with("kind", serde_json::json!(5)),
            with("platform_user_id", serde_json::json!("")),
            with(
                "platform_user_id",
                serde_json::json!("a".repeat(MAX_PLATFORM_USER_ID_LEN + 1)),
            ),
            with("platform_user_id", serde_json::json!("a\nb")),
            with("platform_user_id", serde_json::json!(7)),
        ] {
            assert!(
                matches!(h.handle(&c).await, Err(HostCallError::Invalid(_))),
                "{c:?}"
            );
        }
        let edge = with(
            "platform_user_id",
            serde_json::json!("a".repeat(MAX_PLATFORM_USER_ID_LEN)),
        );
        assert!(h.handle(&edge).await.is_ok());
    }

    #[tokio::test]
    async fn tokenizer_failures_map_to_typed_errors() {
        for (err, want) in [
            (TokenizeError::NotFound, HostCallError::NotFound),
            (TokenizeError::Denied, denied(DenyReason::GrantMissing)),
            (TokenizeError::RateLimited, HostCallError::RateLimited(1)),
            (
                TokenizeError::Invalid,
                HostCallError::Invalid("identity.lookup rejected"),
            ),
            (TokenizeError::Unavailable, HostCallError::Unavailable),
        ] {
            let h = handler(Scripted::with(Err(err), Duration::ZERO));
            assert_eq!(
                h.handle(&lookup(TWITCH, Some("11"), "9", "twitch")).await,
                Err(want)
            );
        }
    }

    #[tokio::test]
    async fn identity_lookups_are_rate_limited_per_connector() {
        let tok = Scripted::ok();
        let h = handler_with(tok.clone(), |c| c.identity_rate_per_sec = 2);
        assert!(h
            .handle(&lookup(TWITCH, Some("11"), "9", "twitch"))
            .await
            .is_ok());
        assert!(h
            .handle(&lookup(TWITCH, Some("11"), "9", "twitch"))
            .await
            .is_ok());
        assert_eq!(
            h.handle(&lookup(TWITCH, Some("11"), "9", "twitch")).await,
            Err(HostCallError::RateLimited(1))
        );
        // Another connector has its own budget.
        assert!(h
            .handle(&lookup(DISCORD, Some("12"), "9", "discord"))
            .await
            .is_ok());
        assert_eq!(tok.calls(), 3);
    }

    #[tokio::test]
    async fn in_flight_caps_refuse_instead_of_queueing() {
        let tok = Scripted::with(Ok(UUID_A.to_string()), Duration::from_millis(300));
        let h = Arc::new(handler_with(tok, |c| {
            c.max_per_connection = 1;
            c.max_per_connector = 2;
            c.deadline = Duration::from_secs(5);
        }));
        let first = {
            let h = h.clone();
            tokio::spawn(async move { h.handle(&lookup(TWITCH, Some("11"), "9", "twitch")).await })
        };
        tokio::time::sleep(Duration::from_millis(60)).await;
        // Same connection: over the per-connection cap.
        assert_eq!(
            h.handle(&lookup(TWITCH, Some("11"), "9", "twitch")).await,
            Err(HostCallError::QuotaExceeded)
        );
        // A second connection of the same connector fits the connector cap.
        let second = {
            let h = h.clone();
            tokio::spawn(
                async move { h.handle(&lookup(DISCORD, Some("12"), "9", "discord")).await },
            )
        };
        assert!(first.await.unwrap().is_ok());
        assert!(second.await.unwrap().is_ok());
        // Permits were released: the connection admits again.
        assert!(h
            .handle(&lookup(TWITCH, Some("11"), "9", "twitch"))
            .await
            .is_ok());
    }

    #[tokio::test]
    async fn the_connector_cap_applies_across_connections() {
        let tok = Scripted::with(Ok(UUID_A.to_string()), Duration::from_millis(300));
        let t = table();
        t.activate(scope("twitch", 8, 9, 21)).unwrap();
        let mut cfg = HostCallConfig::new(["twitch"]).unwrap();
        cfg.max_per_connector = 1;
        cfg.deadline = Duration::from_secs(5);
        let h = Arc::new(ConnectorHostCalls::new(t, tok, cfg));
        let first = {
            let h = h.clone();
            tokio::spawn(async move { h.handle(&lookup(TWITCH, Some("11"), "9", "twitch")).await })
        };
        tokio::time::sleep(Duration::from_millis(60)).await;
        assert_eq!(
            h.handle(&lookup(TWITCH, Some("21"), "9", "twitch")).await,
            Err(HostCallError::QuotaExceeded)
        );
        assert!(first.await.unwrap().is_ok());
    }

    #[tokio::test]
    async fn a_slow_dependency_hits_the_deadline() {
        let tok = Scripted::with(Ok(UUID_A.to_string()), Duration::from_millis(400));
        let h = handler_with(tok, |c| c.deadline = Duration::from_millis(40));
        assert_eq!(
            h.handle(&lookup(TWITCH, Some("11"), "9", "twitch")).await,
            Err(HostCallError::Unavailable)
        );
    }

    #[tokio::test]
    async fn flags_answer_the_env_baseline_for_the_connectors_own_flag_only() {
        let h = handler(Scripted::ok());
        let enabled = |key: serde_json::Value| {
            call(
                TWITCH,
                CapabilityKind::Flags,
                "enabled",
                Some("11"),
                serde_json::json!({ "key": key }),
            )
        };
        assert_eq!(
            h.handle(&enabled("waddles.connector-twitch".into()))
                .await
                .unwrap(),
            serde_json::json!({"enabled": true})
        );
        for other in ["waddles.connector-discord", "waddles.something-else", ""] {
            assert_eq!(
                h.handle(&enabled(other.into())).await.unwrap(),
                serde_json::json!({"enabled": false})
            );
        }
        assert!(matches!(
            h.handle(&enabled(serde_json::json!(3))).await,
            Err(HostCallError::Invalid(_))
        ));
        // Baseline OFF for twitch -> its own flag reads false.
        let off = ConnectorHostCalls::new(
            table(),
            Scripted::ok(),
            HostCallConfig::new(Vec::<&str>::new()).unwrap(),
        );
        assert_eq!(
            off.handle(&enabled("waddles.connector-twitch".into()))
                .await
                .unwrap(),
            serde_json::json!({"enabled": false})
        );
        let tier = call(
            TWITCH,
            CapabilityKind::Flags,
            "tier",
            Some("11"),
            serde_json::json!({}),
        );
        assert_eq!(
            h.handle(&tier).await.unwrap(),
            serde_json::json!({"tier": "free"})
        );
    }

    #[tokio::test]
    async fn log_and_clock_are_serviced_for_scoped_calls() {
        let h = handler(Scripted::ok());
        for level in ["trace", "debug", "info", "warn", "error"] {
            let c = call(
                DISCORD,
                CapabilityKind::Log,
                "write",
                Some("12"),
                serde_json::json!({"level": level, "message": "user 12345 said hi"}),
            );
            assert_eq!(h.handle(&c).await.unwrap(), serde_json::json!({}));
        }
        for bad in [serde_json::json!({"level": "fatal"}), serde_json::json!({})] {
            let c = call(DISCORD, CapabilityKind::Log, "write", Some("12"), bad);
            assert!(matches!(h.handle(&c).await, Err(HostCallError::Invalid(_))));
        }
        let clock = |op: &str| {
            call(
                TWITCH,
                CapabilityKind::Clock,
                op,
                Some("11"),
                serde_json::json!({}),
            )
        };
        assert!(
            h.handle(&clock("now-millis"))
                .await
                .unwrap()
                .as_u64()
                .unwrap()
                > 1_700_000_000_000
        );
        assert!(h
            .handle(&clock("now-rfc3339"))
            .await
            .unwrap()
            .as_str()
            .unwrap()
            .ends_with('Z'));
        assert!(h.handle(&clock("monotonic-nanos")).await.unwrap().is_u64());
    }

    #[test]
    fn errors_map_to_the_wire_codes_the_executor_understands() {
        for (e, code, msg) in [
            (denied(DenyReason::GrantMissing), "denied", "grant_missing"),
            (HostCallError::Invalid("x"), "invalid_args", "x"),
            (HostCallError::NotFound, "not_found", "not_found"),
            (HostCallError::RateLimited(7), "rate_limited", "7"),
            (
                HostCallError::QuotaExceeded,
                "quota_exceeded",
                "quota_exceeded",
            ),
            (HostCallError::Unavailable, "unavailable", "unavailable"),
        ] {
            assert_eq!((e.code(), e.wire_message()), (code, msg.to_string()));
            assert_eq!(e.outcome(), code);
            assert!(!e.to_string().is_empty());
        }
        for r in [
            DenyReason::UnscopedCall,
            DenyReason::UnknownConnection,
            DenyReason::UnknownConnector,
            DenyReason::PlatformMismatch,
            DenyReason::GrantMissing,
            DenyReason::NotServiced,
            DenyReason::UnsupportedKind,
            DenyReason::TenantMissing,
            DenyReason::TenantMismatch,
        ] {
            assert!(!r.as_str().is_empty());
        }
    }

    #[test]
    fn the_active_table_validates_and_tracks_connections() {
        let t = ActiveConnections::new();
        assert!(t.is_empty());
        assert_eq!(
            t.activate(scope("twitch", 0, 1, 1)),
            Err(ActivateError::InvalidId)
        );
        assert_eq!(
            t.activate(scope("twitch", 1, 0, 1)),
            Err(ActivateError::InvalidId)
        );
        assert_eq!(
            t.activate(scope("twitch", 1, 1, 0)),
            Err(ActivateError::InvalidId)
        );
        assert_eq!(
            t.activate(scope("slack", 1, 1, 1)),
            Err(ActivateError::UnknownPlatform)
        );
        t.activate(scope("twitch", 1, 2, 3)).unwrap();
        assert_eq!(t.len(), 1);
        assert_eq!(t.scope_of(3), Some(scope("twitch", 1, 2, 3)));
        t.deactivate(&scope("twitch", 1, 2, 3));
        assert_eq!(t.scope_of(3), None);
        assert!(t.is_empty());
    }

    /// Security review MEDIUM: a conflicting activation is refused (never
    /// overwrites), and a stale guard's deactivate removes only its own scope.
    #[test]
    fn conflicting_activation_is_refused_and_deactivate_is_compare_and_remove() {
        let t = ActiveConnections::new();
        let a = scope("twitch", 1, 2, 3);
        t.activate(a.clone()).unwrap();
        // Another community / tenant / platform reusing connection id 3.
        for other in [
            scope("twitch", 9, 2, 3),
            scope("twitch", 1, 8, 3),
            scope("youtube", 1, 2, 3),
        ] {
            assert_eq!(t.activate(other.clone()), Err(ActivateError::Conflict));
            assert_eq!(t.scope_of(3), Some(a.clone()), "never overwritten");
            // ... and a stale guard of the OTHER scope removes nothing.
            t.deactivate(&other);
            assert_eq!(t.scope_of(3), Some(a.clone()));
        }
        // Restart overlap: the same scope registered twice; the OLD guard
        // dropping must NOT remove the NEW registration.
        t.activate(a.clone()).unwrap();
        t.deactivate(&a);
        assert_eq!(t.scope_of(3), Some(a.clone()), "new registration survives");
        t.deactivate(&a);
        assert_eq!(t.scope_of(3), None);
        // Once gone, a different scope may take the id.
        t.activate(scope("twitch", 9, 2, 3)).unwrap();
        // Releasing an unregistered scope is a no-op.
        t.deactivate(&a);
        assert_eq!(t.scope_of(3), Some(scope("twitch", 9, 2, 3)));
    }

    #[test]
    fn a_poisoned_table_fails_closed() {
        let t = Arc::new(ActiveConnections::new());
        t.activate(scope("twitch", 1, 2, 3)).unwrap();
        let t2 = t.clone();
        let _ = std::thread::spawn(move || {
            let _g = t2.table.write().unwrap();
            panic!("poison");
        })
        .join();
        assert_eq!(t.scope_of(3), None);
        assert_eq!(
            t.activate(scope("twitch", 1, 2, 4)),
            Err(ActivateError::Poisoned)
        );
        t.deactivate(&scope("twitch", 1, 2, 3));
        assert_eq!(t.len(), 0);
    }

    #[test]
    fn config_rejects_an_unknown_platform() {
        assert_eq!(
            HostCallConfig::new(["slack"]).unwrap_err(),
            UnknownPlatform("slack".to_string())
        );
        let c = HostCallConfig::new(["twitch", "discord"]).unwrap();
        assert_eq!(c.connector_enabled.len(), 2);
        assert_eq!(c.max_per_connector, MAX_IN_FLIGHT_PER_CONNECTOR);
    }

    #[test]
    fn handler_grants_match_the_real_hub_manifests() {
        let known: Vec<&str> = [Grant::PiiRead, Grant::FlagsRead, Grant::PlatformLog]
            .iter()
            .map(|g| g.manifest_id())
            .collect();
        let mut examined = 0;
        for o in CONNECTORS {
            let p = format!(
                "{}/../../bundles/rust/{}/hub-manifest.yaml",
                env!("CARGO_MANIFEST_DIR"),
                o.platform
            );
            let text = std::fs::read_to_string(&p).unwrap();
            let ids: Vec<String> = text
                .lines()
                .filter_map(|l| l.trim_start().strip_prefix("- id:"))
                .map(|v| v.trim().trim_matches('"').to_string())
                .collect();
            assert!(!ids.is_empty(), "{p} declares no permissions");
            let app = text
                .lines()
                .find_map(|l| l.strip_prefix("app_id:"))
                .unwrap()
                .trim();
            assert_eq!(app, o.app_id);
            for g in o.grants {
                assert!(
                    ids.iter().any(|i| i == g.manifest_id()),
                    "{} grants {:?} not in manifest",
                    o.platform,
                    g
                );
            }
            // Exact: every known grant the manifest declares is honored.
            for k in &known {
                let declared = ids.iter().any(|i| i == k);
                let honored = o.grants.iter().any(|g| g.manifest_id() == *k);
                assert_eq!(declared, honored, "{} {}", o.platform, k);
            }
            examined += 1;
        }
        assert_eq!(examined, 3);
    }

    #[test]
    fn the_connector_lookups_resolve() {
        assert_eq!(connector_for(TWITCH).unwrap().platform, "twitch");
        assert!(connector_for("nope").is_none());
        assert_eq!(platform_slug("youtube"), Some("youtube"));
        assert_eq!(platform_slug("nope"), None);
        let h = handler(Scripted::ok());
        assert!(format!("{h:?}").contains("ConnectorHostCalls"));
    }

    // ---- the real hub-api backed tokenizer, over a real tonic server ----

    #[derive(Clone, Copy, PartialEq)]
    enum Mode {
        Normal,
        Denied,
        Missing,
        Duplicate,
        BadUuid,
        NilUuid,
        Down,
        Exhausted,
        NotFound,
        Invalid,
    }

    struct FakeHub {
        mode: Mode,
        calls: Arc<AtomicUsize>,
        seen: Arc<Mutex<Vec<hub_client::pb::MintEphemeralPseudonymRequest>>>,
    }

    #[tonic::async_trait]
    impl IdentityService for FakeHub {
        async fn mint_ephemeral_pseudonyms(
            &self,
            req: Request<MintEphemeralPseudonymsRequest>,
        ) -> Result<Response<MintEphemeralPseudonymsResponse>, Status> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            let bearer = req
                .metadata()
                .get("authorization")
                .and_then(|v| v.to_str().ok())
                .map(str::to_string);
            if bearer.as_deref() != Some("Bearer hub-jwt") {
                return Err(Status::unauthenticated("bad token"));
            }
            let items = req.into_inner().items;
            self.seen.lock().unwrap().extend(items.clone());
            match self.mode {
                Mode::Denied => return Err(Status::permission_denied("scope")),
                Mode::Down => return Err(Status::unavailable("down")),
                Mode::Exhausted => return Err(Status::resource_exhausted("slow")),
                Mode::NotFound => return Err(Status::not_found("gone")),
                Mode::Invalid => return Err(Status::invalid_argument("bad")),
                _ => {}
            }
            let id = items[0].platform_user_id.clone();
            let mk = |pseudonym: &str| EphemeralPseudonym {
                platform_user_id: id.clone(),
                pseudonym: pseudonym.to_string(),
            };
            let pseudonyms = match self.mode {
                Mode::Missing => vec![EphemeralPseudonym {
                    platform_user_id: "someone-else".to_string(),
                    pseudonym: UUID_A.to_string(),
                }],
                Mode::Duplicate => vec![mk(UUID_A), mk(UUID_A)],
                Mode::BadUuid => vec![mk("not-a-uuid")],
                Mode::NilUuid => vec![mk("00000000-0000-0000-0000-000000000000")],
                _ => vec![mk(&UUID_A.to_uppercase())],
            };
            Ok(Response::new(MintEphemeralPseudonymsResponse {
                pseudonyms,
            }))
        }
        async fn resolve_display_names(
            &self,
            _: Request<ResolveDisplayNamesRequest>,
        ) -> Result<Response<ResolveDisplayNamesResponse>, Status> {
            Err(Status::unimplemented("n/a"))
        }
        async fn resolve_handle(
            &self,
            _: Request<ResolveHandleRequest>,
        ) -> Result<Response<ResolveHandleResponse>, Status> {
            Err(Status::unimplemented("n/a"))
        }
        async fn resolve_platform_user(
            &self,
            _: Request<ResolvePlatformUserRequest>,
        ) -> Result<Response<ResolvePlatformUserResponse>, Status> {
            Err(Status::unimplemented("n/a"))
        }
    }

    struct HubRig {
        tokenizer: HubIdentityTokenizer,
        calls: Arc<AtomicUsize>,
        seen: Arc<Mutex<Vec<hub_client::pb::MintEphemeralPseudonymRequest>>>,
        _hub: MockServer,
        sa: std::path::PathBuf,
    }

    impl Drop for HubRig {
        fn drop(&mut self) {
            if let Err(e) = std::fs::remove_file(&self.sa) {
                eprintln!("synthetic sa token cleanup failed: {e}");
            }
        }
    }

    async fn hub_rig(mode: Mode) -> HubRig {
        let calls = Arc::new(AtomicUsize::new(0));
        let seen = Arc::new(Mutex::new(Vec::new()));
        let fake = FakeHub {
            mode,
            calls: calls.clone(),
            seen: seen.clone(),
        };
        let port = {
            let l = TcpListener::bind("127.0.0.1:0").unwrap();
            l.local_addr().unwrap().port()
        };
        let addr = format!("127.0.0.1:{port}").parse().unwrap();
        tokio::spawn(async move {
            let _ = tonic::transport::Server::builder()
                .add_service(IdentityServiceServer::new(fake))
                .serve(addr)
                .await;
        });
        // The mint is never retried: wait until the server accepts first.
        let mut up = false;
        for _ in 0..100 {
            if tokio::net::TcpStream::connect(("127.0.0.1", port))
                .await
                .is_ok()
            {
                up = true;
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        assert!(up, "fake hub did not start");
        let hub = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/internal/service-token"))
            .and(body_partial_json(
                serde_json::json!({"scope": IDENTITY_MINT_SCOPE}),
            ))
            .respond_with(
                ResponseTemplate::new(200)
                    .set_body_json(serde_json::json!({"token": "hub-jwt", "expires_in": 3600})),
            )
            .mount(&hub)
            .await;
        let sa = std::env::temp_dir().join(format!("hc-sa-{}", uuid::Uuid::new_v4()));
        std::fs::write(&sa, "synthetic-sa").unwrap();
        let channel = tonic::transport::Endpoint::from_shared(format!("http://127.0.0.1:{port}"))
            .unwrap()
            .connect_timeout(Duration::from_secs(2))
            .connect_lazy();
        let tokenizer = HubIdentityTokenizer::from_channel(
            channel,
            format!("{}/internal/service-token", hub.uri()),
            sa.to_string_lossy().to_string(),
        );
        HubRig {
            tokenizer,
            calls,
            seen,
            _hub: hub,
            sa,
        }
    }

    #[tokio::test]
    async fn the_hub_tokenizer_mints_under_the_scope_tenant_without_a_handle() {
        let r = hub_rig(Mode::Normal).await;
        let s = scope("twitch", 5, 9, 11);
        let u = r.tokenizer.tokenize(&s, "u-100").await.unwrap();
        assert_eq!(u, UUID_A, "canonicalized to lowercase");
        let seen = r.seen.lock().unwrap();
        assert_eq!(seen.len(), 1);
        assert_eq!(seen[0].tenant_id, "9");
        assert_eq!(seen[0].platform, "twitch");
        assert_eq!(seen[0].platform_user_id, "u-100");
        assert_eq!(seen[0].handle, "", "no handle crosses to hub-api");
    }

    #[tokio::test]
    async fn the_hub_tokenizer_classifies_every_failure() {
        for (mode, want) in [
            (Mode::Denied, TokenizeError::Denied),
            (Mode::Exhausted, TokenizeError::RateLimited),
            (Mode::NotFound, TokenizeError::NotFound),
            (Mode::Invalid, TokenizeError::Invalid),
            (Mode::Down, TokenizeError::Unavailable),
            (Mode::Missing, TokenizeError::Unavailable),
            (Mode::Duplicate, TokenizeError::Unavailable),
            (Mode::BadUuid, TokenizeError::Unavailable),
            (Mode::NilUuid, TokenizeError::Unavailable),
        ] {
            let r = hub_rig(mode).await;
            let got = r
                .tokenizer
                .tokenize(&scope("twitch", 5, 9, 11), "u-100")
                .await;
            assert_eq!(got, Err(want));
            assert!(r.calls.load(Ordering::SeqCst) >= 1);
        }
    }

    #[tokio::test]
    async fn an_unreachable_token_endpoint_is_denied_or_unavailable_never_ok() {
        let channel = tonic::transport::Endpoint::from_static("http://127.0.0.1:1").connect_lazy();
        let t = HubIdentityTokenizer::from_channel(channel, "http://127.0.0.1:1/t", "/nonexistent");
        assert!(t.tokenize(&scope("twitch", 5, 9, 11), "u").await.is_err());
    }

    // ---- real path: the real handler behind the real ExecutorLink ----

    use crate::connector::wire_invoker::ExecutorLink;
    use penguin_bundle_host::wire::message::PROTOCOL_VERSION;
    use penguin_bundle_host::wire::{
        read_frame, write_frame, Frame, HelloBody, HostResultBody, Message, SandboxInfo,
    };

    async fn attached(link: &Arc<ExecutorLink>) -> tokio::io::DuplexStream {
        let (stage_io, mut exec_io) = tokio::io::duplex(1 << 20);
        let l = Arc::clone(link);
        let attach = tokio::spawn(async move { l.attach(stage_io).await });
        let hello = Message::Hello(HelloBody {
            protocol_version: u32::from(PROTOCOL_VERSION),
            executor_version: "t".to_string(),
            wasmtime_version: "t".to_string(),
            wasmtime_abi: "t".to_string(),
            collector: "drc".to_string(),
            sandbox: SandboxInfo {
                runtime: "runc".to_string(),
                verified: false,
            },
        });
        write_frame(&mut exec_io, &Frame::new(1, hello))
            .await
            .unwrap();
        assert!(matches!(
            read_frame(&mut exec_io).await.unwrap().message,
            Message::HelloOk(_)
        ));
        attach.await.unwrap().unwrap();
        exec_io
    }

    async fn roundtrip(
        io: &mut tokio::io::DuplexStream,
        id: u64,
        c: HostCallBody,
    ) -> HostResultBody {
        write_frame(io, &Frame::new(id, Message::HostCall(c)))
            .await
            .unwrap();
        let f = read_frame(io).await.unwrap();
        assert_eq!(f.id, id);
        match f.message {
            Message::HostResult(b) => b,
            other => panic!("{other:?}"),
        }
    }

    #[tokio::test]
    async fn over_the_real_link_unscoped_is_denied_and_scoped_identity_is_served() {
        let tok = Scripted::ok();
        let link = Arc::new(ExecutorLink::new().with_host_calls(Arc::new(handler(tok.clone()))));
        let mut io = attached(&link).await;

        // An unscoped host-call (tool-call / build-request shape).
        let r = roundtrip(&mut io, 50, lookup(TWITCH, None, "9", "twitch")).await;
        let e = r.error.expect("denied");
        assert_eq!(
            (e.code.as_str(), e.message.as_str()),
            ("denied", "unscoped_call")
        );
        assert!(r.result.is_none());
        assert_eq!(tok.calls(), 0);

        // Even a flags call: no legacy unscoped answer once a handler is set.
        let flags = call(
            TWITCH,
            CapabilityKind::Flags,
            "enabled",
            None,
            serde_json::json!({"key": "waddles.connector-twitch", "default_value": true}),
        );
        assert_eq!(
            roundtrip(&mut io, 51, flags).await.error.unwrap().code,
            "denied"
        );

        // A scoped identity lookup round-trips the pseudonym.
        let r = roundtrip(&mut io, 52, lookup(TWITCH, Some("11"), "9", "twitch")).await;
        let v = r.result.expect("served");
        assert_eq!(v["uuid"], UUID_A);
        assert_eq!(v["tenant"], "9");
        assert!(r.error.is_none());

        // A rate-limited error keeps its retry-after on the wire.
        let ctx = call(
            YOUTUBE,
            CapabilityKind::Context,
            "get-context",
            Some("13"),
            serde_json::json!({}),
        );
        let r = roundtrip(&mut io, 53, ctx).await;
        assert_eq!(r.result.unwrap()["community"], "7");
    }

    #[tokio::test]
    async fn the_link_still_denies_everything_without_a_handler() {
        let link = Arc::new(ExecutorLink::new());
        let mut io = attached(&link).await;
        let r = roundtrip(&mut io, 60, lookup(TWITCH, Some("11"), "9", "twitch")).await;
        assert_eq!(r.error.unwrap().code, "HOST_CALL_DENIED");
    }

    #[tokio::test]
    async fn a_slow_host_call_does_not_stall_other_replies() {
        let tok = Scripted::with(Ok(UUID_A.to_string()), Duration::from_millis(300));
        let link = Arc::new(ExecutorLink::new().with_host_calls(Arc::new(handler_with(
            tok,
            |c| {
                c.deadline = Duration::from_secs(5);
            },
        ))));
        let mut io = attached(&link).await;
        write_frame(
            &mut io,
            &Frame::new(
                70,
                Message::HostCall(lookup(TWITCH, Some("11"), "9", "twitch")),
            ),
        )
        .await
        .unwrap();
        let clock = call(
            TWITCH,
            CapabilityKind::Clock,
            "now-millis",
            Some("11"),
            serde_json::json!({}),
        );
        write_frame(&mut io, &Frame::new(71, Message::HostCall(clock)))
            .await
            .unwrap();
        let first = read_frame(&mut io).await.unwrap();
        assert_eq!(
            first.id, 71,
            "the fast call is not blocked behind the slow one"
        );
        assert_eq!(read_frame(&mut io).await.unwrap().id, 70);
    }

    fn build_origin(app: &str) -> CallOrigin {
        CallOrigin::BuildRequest {
            app_id: app.to_string(),
        }
    }

    fn flag_call(app: &str, op: &str, key: &str) -> HostCallBody {
        call(
            app,
            CapabilityKind::Flags,
            op,
            None,
            serde_json::json!({ "key": key }),
        )
    }

    /// The A2 open issue: with a handler installed, a `build-request` flag read
    /// (no `ConnectionCtx`, so no `_scope`) was denied and the Twitch connector
    /// refused to build. The host-recorded build-request origin serves it.
    #[tokio::test]
    async fn a_build_request_origin_serves_the_connectors_own_cutover_flag() {
        let h = handler(Scripted::ok());
        let on = h
            .handle_from(
                &flag_call(TWITCH, "enabled", "waddles.connector-twitch"),
                &build_origin(TWITCH),
            )
            .await
            .unwrap();
        assert_eq!(on["enabled"], true, "twitch connector is ON by baseline");
        // Another platform's flag, and an unknown key, read OFF (never ON).
        for key in ["waddles.connector-discord", "waddles.connector-tools", ""] {
            let v = h
                .handle_from(&flag_call(TWITCH, "enabled", key), &build_origin(TWITCH))
                .await
                .unwrap();
            assert_eq!(v["enabled"], false, "{key:?}");
        }
        let tier = h
            .handle_from(&flag_call(TWITCH, "tier", ""), &build_origin(TWITCH))
            .await
            .unwrap();
        assert_eq!(tier["tier"], "free");
    }

    /// An connector whose baseline is OFF reads OFF from a build-request origin.
    #[tokio::test]
    async fn a_build_request_flag_read_honors_the_env_baseline_off() {
        let h = ConnectorHostCalls::new(
            table(),
            Scripted::ok(),
            HostCallConfig::new(Vec::<&str>::new()).unwrap(),
        );
        let v = h
            .handle_from(
                &flag_call(TWITCH, "enabled", "waddles.connector-twitch"),
                &build_origin(TWITCH),
            )
            .await
            .unwrap();
        assert_eq!(v["enabled"], false);
    }

    /// Mutation target: unscoped calls stay denied. Without a host-recorded
    /// build-request origin the same flag read is refused (`unscoped_call`), and
    /// from a build-request origin NOTHING but the flag read is served.
    #[tokio::test]
    async fn unscoped_calls_stay_denied_without_the_build_request_origin() {
        let tok = Scripted::ok();
        let h = handler(tok.clone());
        let flag = flag_call(TWITCH, "enabled", "waddles.connector-twitch");
        // No origin (`handle`), `Other`: denied, unscoped.
        assert_eq!(
            h.handle(&flag).await.unwrap_err(),
            denied(DenyReason::UnscopedCall)
        );
        assert_eq!(
            h.handle_from(&flag, &CallOrigin::Other).await.unwrap_err(),
            denied(DenyReason::UnscopedCall)
        );
        // From a build-request origin every non-flag capability is still denied.
        let others = [
            lookup(TWITCH, None, "9", "twitch"),
            call(
                TWITCH,
                CapabilityKind::Context,
                "get-context",
                None,
                serde_json::json!({}),
            ),
            call(
                TWITCH,
                CapabilityKind::Log,
                "write",
                None,
                serde_json::json!({"level": "info", "message": "x"}),
            ),
            call(
                TWITCH,
                CapabilityKind::Clock,
                "now-millis",
                None,
                serde_json::json!({}),
            ),
            call(
                TWITCH,
                CapabilityKind::Http,
                "send",
                None,
                serde_json::json!({}),
            ),
            call(
                TWITCH,
                CapabilityKind::Kv,
                "get",
                None,
                serde_json::json!({}),
            ),
            flag_call(TWITCH, "set", "waddles.connector-twitch"),
        ];
        for c in &others {
            assert_eq!(
                h.handle_from(c, &build_origin(TWITCH)).await.unwrap_err(),
                denied(DenyReason::UnscopedCall),
                "{:?}/{}",
                c.capability,
                c.op
            );
        }
        assert_eq!(tok.calls(), 0, "nothing tokenized from an unscoped origin");
    }

    /// The origin names the invoked connector: a call whose `app_id` is a
    /// different connector (or unknown) is refused even from a build-request origin.
    #[tokio::test]
    async fn a_build_request_origin_cannot_be_used_by_another_connector() {
        let h = handler(Scripted::ok());
        assert_eq!(
            h.handle_from(
                &flag_call(DISCORD, "enabled", "waddles.connector-discord"),
                &build_origin(TWITCH),
            )
            .await
            .unwrap_err(),
            denied(DenyReason::UnknownConnector)
        );
        assert_eq!(
            h.handle_from(
                &flag_call("waddles.core.service.nope", "enabled", "x"),
                &build_origin("waddles.core.service.nope"),
            )
            .await
            .unwrap_err(),
            denied(DenyReason::UnknownConnector)
        );
        // The discord connector's manifest has no `flags.read`: grant missing.
        assert_eq!(
            h.handle_from(
                &flag_call(DISCORD, "enabled", "waddles.connector-discord"),
                &build_origin(DISCORD),
            )
            .await
            .unwrap_err(),
            denied(DenyReason::GrantMissing)
        );
    }

    /// A forged `_scope` on a build-request origin goes through the normal scope
    /// checks (unknown connection), never the unscoped flag allowance.
    #[tokio::test]
    async fn a_forged_scope_on_a_build_request_origin_is_still_checked() {
        let h = handler(Scripted::ok());
        let forged = call(
            TWITCH,
            CapabilityKind::Flags,
            "enabled",
            Some("999"),
            serde_json::json!({"key": "waddles.connector-twitch"}),
        );
        assert_eq!(
            h.handle_from(&forged, &build_origin(TWITCH))
                .await
                .unwrap_err(),
            denied(DenyReason::UnknownConnection)
        );
    }

    /// The build-request path is admission-controlled like every other call:
    /// at the per-connector cap it is refused (never queued), and the slot frees.
    #[tokio::test]
    async fn build_request_flag_reads_respect_the_per_connector_in_flight_cap() {
        let h = handler_with(Scripted::ok(), |c| c.max_per_connector = 1);
        let connector = connector_for(TWITCH).unwrap();
        let held = h.admit_connector(connector).unwrap();
        let call = flag_call(TWITCH, "enabled", "waddles.connector-twitch");
        assert_eq!(
            h.handle_from(&call, &build_origin(TWITCH))
                .await
                .unwrap_err(),
            HostCallError::QuotaExceeded
        );
        drop(held);
        assert!(h.handle_from(&call, &build_origin(TWITCH)).await.is_ok());
    }
}
