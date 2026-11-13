//! Live per-community bundle config: the gRPC client (`BundleConfigService`) and the stage-side
//! cache that feeds a bundle's `get-context().config_json`.
//!
//! A community admin saves config through hub-api's bundle-admin gateway (append-only, highest
//! version live). hub-api serves the merged value (manifest `community_config_schema` defaults
//! overlaid with the latest saved version) over [`BundleConfigClient`]; [`BundleConfigCache`] keeps
//! it keyed `(tenant_id, community_id, app_id)` inside svc-process / svc-action.
//!
//! **Cache policy (seed + lazy-load, never invalidate-only)**
//! * [`BundleConfigCache::seed`] batch-loads the active scopes at bundle load;
//! * a miss lazy-loads through the source, so an unseeded scope is never answered empty;
//! * on a save hub-api bumps a Valkey *version key* ([`version_key`]); a hit older than
//!   `recheck` asks the [`VersionProbe`] and reloads when the key is newer than the entry;
//! * entries also expire after `ttl` (safety net when the probe/Valkey is down);
//! * source unreachable -> the last-known value is served indefinitely (ERROR + counter; no
//!   max-age cutoff), never-seen -> the registered schema defaults, else an ERROR log and `{}` --
//!   never a silent empty.
//!
//! **Sources (issue #1306).** The data plane reads the database directly
//! ([`crate::bundle_config_db::DbConfigSource`], read-only replica, one batched warm-up query) and
//! never needs hub-api on the hot path; the legacy hub-api gRPC [`BundleConfigClient`] remains
//! behind the opt-out kill switch `waddles.disable-db-bundle-config-reads`
//! ([`crate::bundle_config_db::RoutedConfigSource`]). Warm-up seeds from many bundle loads are
//! coalesced into ONE batched load ([`BundleConfigCache::enqueue_seed`]).
//!
//! No lock is held across an `.await`; every critical section is a short sync map operation.
//! Logs/spans/metric labels carry numeric ids and outcomes only -- never a config body.

use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock, RwLock};
use std::time::{Duration, Instant};

use async_trait::async_trait;
use opentelemetry::metrics::{Counter, Histogram};
use opentelemetry::KeyValue;
use service_auth::MachineJwtClient;
use tonic::transport::{Channel, Endpoint};
use tracing::Instrument;

use crate::bundle_config_db::{DbConfigSource, DbReadConfig, ReadPathGate, RoutedConfigSource};
use crate::pb::bundle_config_service_client::BundleConfigServiceClient;
use crate::pb::{BatchGetCommunityConfigRequest, GetCommunityConfigRequest};
use crate::rpc::RpcGuard;
use crate::{build_tls_config, HubClientError};

/// Machine-JWT scope hub-api requires for both RPCs.
pub const SCOPE: &str = "bundle-config:read";
/// Proto `api_version` this client speaks.
pub const API_VERSION: &str = "v1";
/// Valkey key namespace hub-api writes on a save (mirrors
/// `hub_api/services/bundle_community_config_runtime.py::VERSION_KEY_PREFIX`).
pub const VERSION_KEY_PREFIX: &str = "waddles:bundle_config:v";
/// Server-side batch ceiling (`MAX_BATCH` in the servicer).
pub const MAX_BATCH: usize = 200;

/// The Valkey key whose value is the latest saved config version of one scope.
#[must_use]
pub fn version_key(key: &ConfigKey) -> String {
    format!(
        "{VERSION_KEY_PREFIX}:{}:{}:{}",
        key.tenant_id, key.community_id, key.app_id
    )
}

/// `(tenant_id, community_id, app_id)` -- numeric ids, never slugs.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct ConfigKey {
    pub tenant_id: i64,
    pub community_id: i64,
    pub app_id: String,
}

impl ConfigKey {
    /// Builds a key.
    #[must_use]
    pub fn new(tenant_id: i64, community_id: i64, app_id: impl Into<String>) -> Self {
        Self {
            tenant_id,
            community_id,
            app_id: app_id.into(),
        }
    }
}

/// One served config: the saved version it reflects and the merged JSON object.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConfigValue {
    pub key: ConfigKey,
    /// Latest saved version (0 = never saved, defaults only).
    pub version: i64,
    pub config_json: String,
}

/// Where the cache loads from. [`BundleConfigClient`] in production; a fake in tests.
#[async_trait]
pub trait ConfigSource: Send + Sync {
    /// Loads one scope.
    async fn get(&self, key: &ConfigKey) -> Result<ConfigValue, HubClientError>;
    /// Loads many scopes; scopes hub-api refuses/does not know are simply absent.
    async fn batch_get(&self, keys: &[ConfigKey]) -> Result<Vec<ConfigValue>, HubClientError>;
    /// Installs the read-path kill switch on a source that routes between paths; a no-op for
    /// every single-path source.
    fn set_read_path_gate(&self, _gate: Arc<dyn ReadPathGate>) {}
}

/// Reads the invalidation version key. `None` = unavailable/absent (the TTL is then the backstop).
#[async_trait]
pub trait VersionProbe: Send + Sync {
    /// The latest saved version hub-api announced for `key`, if readable.
    async fn latest(&self, key: &ConfigKey) -> Option<i64>;
}

/// [`VersionProbe`] over a stage's existing Valkey connection (a plain `GET`).
pub struct ValkeyVersionProbe {
    conn: redis::aio::MultiplexedConnection,
}

impl ValkeyVersionProbe {
    /// Wraps the stage's (already authenticated/TLS) multiplexed connection.
    #[must_use]
    pub fn new(conn: redis::aio::MultiplexedConnection) -> Self {
        Self { conn }
    }
}

#[async_trait]
impl VersionProbe for ValkeyVersionProbe {
    async fn latest(&self, key: &ConfigKey) -> Option<i64> {
        use redis::AsyncCommands;
        let mut conn = self.conn.clone();
        match conn.get::<_, Option<String>>(version_key(key)).await {
            Ok(v) => v.and_then(|s| s.trim().parse().ok()),
            Err(err) => {
                tracing::warn!(
                    tenant_id = key.tenant_id,
                    community_id = key.community_id,
                    error = %err,
                    "bundle_config: version probe failed; relying on the cache TTL"
                );
                None
            }
        }
    }
}

/// Opt-out kill switch (`waddles.disable-bundle-community-config`) as seen by the cache: the
/// stage adapts its license/flag client to this. `live_enabled` is the already-inverted answer --
/// `true` (the default for unseen/OFF/unreachable) means live config is on.
pub trait LiveConfigGate: Send + Sync {
    /// Whether live community config is currently enabled.
    fn live_enabled<'a>(&'a self) -> Pin<Box<dyn Future<Output = bool> + Send + 'a>>;
}

/// PostHog kill-switch key: unseen/OFF = live community config on, ON = no `config_json` lookup.
pub const DISABLE_FLAG: &str = "waddles.disable-bundle-community-config";

/// A [`LiveConfigGate`] with a fixed answer (stage fallback when no flag client exists, tests).
pub struct StaticLiveGate(pub bool);

impl LiveConfigGate for StaticLiveGate {
    fn live_enabled<'a>(&'a self) -> Pin<Box<dyn Future<Output = bool> + Send + 'a>> {
        Box::pin(async move { self.0 })
    }
}

/// gRPC client for `BundleConfigService`.
#[derive(Clone)]
pub struct BundleConfigClient {
    client: BundleConfigServiceClient<Channel>,
    guard: RpcGuard,
}

impl BundleConfigClient {
    /// Dials hub-api's internal gRPC endpoint over TLS (CA-pinned via [`build_tls_config`]).
    ///
    /// # Errors
    /// [`HubClientError`] if the CA is unreadable or hub-api is unreachable/untrusted.
    pub async fn connect(
        endpoint: impl Into<String>,
        token_endpoint: impl Into<String>,
        sa_token_path: impl Into<String>,
        ca_cert_path: Option<&str>,
    ) -> Result<Self, HubClientError> {
        let tls = build_tls_config(ca_cert_path)?;
        let channel = Endpoint::from_shared(endpoint.into())?
            .tls_config(tls)?
            .connect()
            .await?;
        Ok(Self::with_channel(
            channel,
            Arc::new(MachineJwtClient::new(token_endpoint, sa_token_path, SCOPE)),
        ))
    }

    /// Builds a client over an established channel (loopback servers in tests). The token client
    /// must carry [`SCOPE`].
    #[must_use]
    pub fn with_channel(channel: Channel, token_client: Arc<MachineJwtClient>) -> Self {
        Self {
            client: BundleConfigServiceClient::new(channel),
            guard: RpcGuard::new(token_client),
        }
    }

    /// Overrides the per-call deadline (tests).
    #[must_use]
    pub fn with_deadline(mut self, deadline: Duration) -> Self {
        self.guard = self.guard.with_deadline(deadline);
        self
    }

    fn scope_msg(key: &ConfigKey) -> GetCommunityConfigRequest {
        GetCommunityConfigRequest {
            api_version: API_VERSION.to_owned(),
            tenant_id: key.tenant_id,
            community_id: key.community_id,
            app_id: key.app_id.clone(),
        }
    }

    fn to_value(msg: crate::pb::GetCommunityConfigResponse) -> ConfigValue {
        ConfigValue {
            key: ConfigKey::new(msg.tenant_id, msg.community_id, msg.app_id),
            version: msg.version,
            config_json: String::from_utf8_lossy(&msg.config_json).into_owned(),
        }
    }
}

#[async_trait]
impl ConfigSource for BundleConfigClient {
    async fn get(&self, key: &ConfigKey) -> Result<ConfigValue, HubClientError> {
        let span = tracing::info_span!(
            "hub_client.bundle_config.get",
            rpc.system = "grpc",
            rpc.method = "GetCommunityConfig",
            tenant_id = key.tenant_id,
            community_id = key.community_id,
            outcome = tracing::field::Empty,
        );
        let recorder = span.clone();
        async move {
            self.guard.admit()?;
            let result: Result<ConfigValue, HubClientError> = async {
                let request = self.guard.request(Self::scope_msg(key)).await?;
                let mut client = self.client.clone();
                let answer = client.get_community_config(request).await?.into_inner();
                Ok(Self::to_value(answer))
            }
            .await;
            recorder.record("outcome", self.guard.settle(&result));
            result
        }
        .instrument(span)
        .await
    }

    async fn batch_get(&self, keys: &[ConfigKey]) -> Result<Vec<ConfigValue>, HubClientError> {
        let span = tracing::info_span!(
            "hub_client.bundle_config.batch_get",
            rpc.system = "grpc",
            rpc.method = "BatchGetCommunityConfig",
            scopes = keys.len(),
            outcome = tracing::field::Empty,
        );
        let recorder = span.clone();
        async move {
            self.guard.admit()?;
            let result: Result<Vec<ConfigValue>, HubClientError> = async {
                let mut out = Vec::with_capacity(keys.len());
                for chunk in keys.chunks(MAX_BATCH) {
                    let request = self
                        .guard
                        .request(BatchGetCommunityConfigRequest {
                            api_version: API_VERSION.to_owned(),
                            scopes: chunk.iter().map(Self::scope_msg).collect(),
                        })
                        .await?;
                    let mut client = self.client.clone();
                    let answer = client
                        .batch_get_community_config(request)
                        .await?
                        .into_inner();
                    out.extend(answer.configs.into_iter().map(Self::to_value));
                }
                Ok(out)
            }
            .await;
            recorder.record("outcome", self.guard.settle(&result));
            result
        }
        .instrument(span)
        .await
    }
}

/// Cache timing knobs.
#[derive(Clone, Copy, Debug)]
pub struct CacheConfig {
    /// Hard expiry of an entry (safety net when invalidation does not arrive).
    pub ttl: Duration,
    /// Minimum gap between version-key probes of one entry.
    pub recheck: Duration,
}

impl Default for CacheConfig {
    fn default() -> Self {
        Self {
            ttl: Duration::from_secs(300),
            recheck: Duration::from_secs(2),
        }
    }
}

struct Instruments {
    lookups: Counter<u64>,
    load_errors: Counter<u64>,
    load_seconds: Histogram<f64>,
}

fn instruments() -> &'static Instruments {
    static I: OnceLock<Instruments> = OnceLock::new();
    I.get_or_init(|| {
        let meter = opentelemetry::global::meter("hub_client.bundle_config");
        Instruments {
            lookups: meter
                .u64_counter("waddles_bundle_config_cache_lookups")
                .with_description("Bundle community-config cache lookups by result.")
                .build(),
            load_errors: meter
                .u64_counter("waddles_bundle_config_load_errors")
                .with_description("Failed community-config loads (any read path).")
                .build(),
            load_seconds: meter
                .f64_histogram("waddles_bundle_config_load_seconds")
                .with_unit("s")
                .with_description("Community-config load latency (any read path).")
                .with_boundaries(vec![
                    0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0,
                ])
                .build(),
        }
    })
}

fn record(result: &'static str) {
    instruments()
        .lookups
        .add(1, &[KeyValue::new("result", result)]);
}

#[derive(Clone)]
struct Entry {
    value: Arc<str>,
    version: i64,
    loaded: Instant,
    probed: Instant,
}

/// Stage-side cache of live community config. Cheap to share via `Arc`.
pub struct BundleConfigCache {
    source: Arc<dyn ConfigSource>,
    probe: Option<Arc<dyn VersionProbe>>,
    cfg: CacheConfig,
    entries: RwLock<HashMap<ConfigKey, Entry>>,
    /// Schema defaults per `app_id` (JSON object text), used only for a never-seen scope while
    /// hub-api is unreachable.
    defaults: RwLock<HashMap<String, Arc<str>>>,
    /// Kill-switch gate; unset = always enabled.
    gate: OnceLock<Arc<dyn LiveConfigGate>>,
    /// Scopes waiting for the next coalesced warm-up seed ([`Self::enqueue_seed`]).
    pending_seed: Mutex<Vec<ConfigKey>>,
    /// True while a coalescing flush task is scheduled.
    seed_scheduled: AtomicBool,
    /// How long a flush waits to collect more scopes.
    seed_window: Duration,
}

/// Default coalescing window for warm-up seeds.
pub const DEFAULT_SEED_WINDOW: Duration = Duration::from_millis(250);

impl BundleConfigCache {
    /// A cache loading through `source`; `probe` enables version-key invalidation.
    #[must_use]
    pub fn new(
        source: Arc<dyn ConfigSource>,
        probe: Option<Arc<dyn VersionProbe>>,
        cfg: CacheConfig,
    ) -> Self {
        Self {
            source,
            probe,
            cfg,
            entries: RwLock::new(HashMap::new()),
            defaults: RwLock::new(HashMap::new()),
            gate: OnceLock::new(),
            pending_seed: Mutex::new(Vec::new()),
            seed_scheduled: AtomicBool::new(false),
            seed_window: DEFAULT_SEED_WINDOW,
        }
    }

    /// Overrides the warm-up coalescing window (tests).
    #[must_use]
    pub fn with_seed_window(mut self, window: Duration) -> Self {
        self.seed_window = window;
        self
    }

    /// Forwards the database-read-path kill switch to a routing source (no-op otherwise).
    pub fn set_read_path_gate(&self, gate: Arc<dyn ReadPathGate>) {
        self.source.set_read_path_gate(gate);
    }

    /// Queues one scope for the next coalesced warm-up seed and returns immediately. All scopes
    /// queued within `seed_window` are loaded by ONE `batch_get` -- a stage loading ~160 bundles
    /// at startup costs one query, not 160. Needs a running Tokio runtime (otherwise a no-op:
    /// the lazy-load path still covers the scope).
    pub fn enqueue_seed(self: &Arc<Self>, key: ConfigKey) {
        let Ok(handle) = tokio::runtime::Handle::try_current() else {
            return;
        };
        if let Ok(mut pending) = self.pending_seed.lock() {
            pending.push(key);
        }
        if self.seed_scheduled.swap(true, Ordering::SeqCst) {
            return;
        }
        let cache = Arc::clone(self);
        handle.spawn(async move {
            tokio::time::sleep(cache.seed_window).await;
            // Cleared BEFORE draining so a scope queued after the drain schedules a new flush.
            cache.seed_scheduled.store(false, Ordering::SeqCst);
            let keys = cache
                .pending_seed
                .lock()
                .map(|mut p| std::mem::take(&mut *p))
                .unwrap_or_default();
            cache.seed(&keys).await;
        });
    }

    /// Installs the kill-switch gate (once; later calls are ignored).
    pub fn set_gate(&self, gate: Arc<dyn LiveConfigGate>) {
        let _ = self.gate.set(gate);
    }

    /// Like [`Self::lookup`] but `None` when the kill switch has live config disabled -- callers
    /// then add no `config_json` at all (the pre-feature behaviour).
    pub async fn lookup_if_enabled(&self, key: &ConfigKey) -> Option<Arc<str>> {
        if let Some(gate) = self.gate.get() {
            if !gate.live_enabled().await {
                record("disabled");
                return None;
            }
        }
        Some(self.lookup(key).await)
    }

    /// Registers the manifest schema defaults (a JSON object) for `app_id`.
    pub fn set_defaults(&self, app_id: &str, defaults_json: &str) {
        if let Ok(mut map) = self.defaults.write() {
            map.insert(app_id.to_owned(), Arc::from(defaults_json));
        }
    }

    fn entry(&self, key: &ConfigKey) -> Option<Entry> {
        self.entries.read().ok()?.get(key).cloned()
    }

    fn store(&self, value: &ConfigValue) {
        let now = Instant::now();
        if let Ok(mut map) = self.entries.write() {
            map.insert(
                value.key.clone(),
                Entry {
                    value: Arc::from(value.config_json.as_str()),
                    version: value.version,
                    loaded: now,
                    probed: now,
                },
            );
        }
    }

    /// Drops one scope (the next lookup lazy-loads).
    pub fn evict(&self, key: &ConfigKey) {
        if let Ok(mut map) = self.entries.write() {
            map.remove(key);
        }
    }

    /// Number of cached scopes (tests/metrics).
    #[must_use]
    pub fn len(&self) -> usize {
        self.entries.read().map_or(0, |m| m.len())
    }

    /// True when nothing is cached.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// Seeds the cache for the active scopes at bundle load. Failure is logged, never fatal:
    /// the lazy-load path still covers every scope.
    pub async fn seed(&self, keys: &[ConfigKey]) -> usize {
        if keys.is_empty() {
            return 0;
        }
        let started = Instant::now();
        let span = tracing::info_span!("bundle_config.cache.seed", scopes = keys.len());
        let loaded = self.source.batch_get(keys).instrument(span).await;
        instruments()
            .load_seconds
            .record(started.elapsed().as_secs_f64(), &[]);
        match loaded {
            Ok(values) => {
                for v in &values {
                    self.store(v);
                }
                tracing::info!(
                    requested = keys.len(),
                    loaded = values.len(),
                    "bundle_config: cache seeded"
                );
                values.len()
            }
            Err(err) => {
                instruments().load_errors.add(1, &[]);
                tracing::error!(
                    requested = keys.len(),
                    error = %err,
                    "bundle_config: seed failed; scopes will lazy-load on first use"
                );
                0
            }
        }
    }

    /// The `config_json` object text for `key`. Never fails: see the module doc for the
    /// hub-api-unreachable ladder.
    pub async fn lookup(&self, key: &ConfigKey) -> Arc<str> {
        let now = Instant::now();
        let cached = self.entry(key);
        if let Some(entry) = &cached {
            if now.duration_since(entry.loaded) < self.cfg.ttl {
                if now.duration_since(entry.probed) < self.cfg.recheck {
                    record("hit");
                    return Arc::clone(&entry.value);
                }
                let announced = match &self.probe {
                    Some(p) => p.latest(key).await,
                    None => None,
                };
                match announced {
                    Some(v) if v > entry.version => {
                        tracing::info!(
                            tenant_id = key.tenant_id,
                            community_id = key.community_id,
                            cached = entry.version,
                            announced = v,
                            "bundle_config: newer version announced; reloading"
                        );
                        // fall through to reload (stale value kept as the fallback)
                    }
                    _ => {
                        self.touch_probed(key);
                        record("hit");
                        return Arc::clone(&entry.value);
                    }
                }
            }
        }
        self.load(key, cached).await
    }

    fn touch_probed(&self, key: &ConfigKey) {
        if let Ok(mut map) = self.entries.write() {
            if let Some(e) = map.get_mut(key) {
                e.probed = Instant::now();
            }
        }
    }

    async fn load(&self, key: &ConfigKey, stale: Option<Entry>) -> Arc<str> {
        let started = Instant::now();
        let span = tracing::info_span!(
            "bundle_config.cache.load",
            tenant_id = key.tenant_id,
            community_id = key.community_id,
        );
        let result = self.source.get(key).instrument(span).await;
        instruments()
            .load_seconds
            .record(started.elapsed().as_secs_f64(), &[]);
        match result {
            Ok(value) => {
                record(if stale.is_some() { "refresh" } else { "miss" });
                self.store(&value);
                Arc::from(value.config_json.as_str())
            }
            Err(err) => {
                instruments().load_errors.add(1, &[]);
                if let Some(entry) = stale {
                    record("stale");
                    tracing::error!(
                        tenant_id = key.tenant_id,
                        community_id = key.community_id,
                        app_id = %key.app_id,
                        error = %err,
                        "bundle_config: config source unavailable; serving last-known config"
                    );
                    return entry.value;
                }
                self.never_seen_fallback(key, &err)
            }
        }
    }

    fn never_seen_fallback(&self, key: &ConfigKey, err: &HubClientError) -> Arc<str> {
        let defaults = self
            .defaults
            .read()
            .ok()
            .and_then(|m| m.get(&key.app_id).cloned());
        if let Some(d) = defaults {
            record("defaults");
            tracing::warn!(
                tenant_id = key.tenant_id,
                community_id = key.community_id,
                app_id = %key.app_id,
                error = %err,
                "bundle_config: config unavailable and never loaded; serving manifest defaults"
            );
            return d;
        }
        record("empty");
        tracing::error!(
            tenant_id = key.tenant_id,
            community_id = key.community_id,
            app_id = %key.app_id,
            error = %err,
            "bundle_config: config unavailable, never loaded and no schema defaults known; \
             the bundle receives an EMPTY config"
        );
        Arc::from("{}")
    }
}

/// In-memory [`ConfigSource`] for stage unit/e2e tests (no network): scopes are set explicitly and
/// an unset scope answers `NOT_FOUND`, like hub-api for an inactive bundle.
#[derive(Default)]
pub struct StaticConfigSource {
    values: RwLock<HashMap<ConfigKey, (i64, String)>>,
}

impl StaticConfigSource {
    /// An empty source.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Sets (or replaces) the answer for `key`.
    pub fn set(&self, key: ConfigKey, version: i64, config_json: &str) {
        if let Ok(mut m) = self.values.write() {
            m.insert(key, (version, config_json.to_owned()));
        }
    }

    fn read(&self, key: &ConfigKey) -> Option<ConfigValue> {
        let m = self.values.read().ok()?;
        m.get(key).map(|(version, json)| ConfigValue {
            key: key.clone(),
            version: *version,
            config_json: json.clone(),
        })
    }
}

#[async_trait]
impl ConfigSource for StaticConfigSource {
    async fn get(&self, key: &ConfigKey) -> Result<ConfigValue, HubClientError> {
        self.read(key)
            .ok_or_else(|| HubClientError::Grpc(tonic::Status::not_found("bundle not active")))
    }

    async fn batch_get(&self, keys: &[ConfigKey]) -> Result<Vec<ConfigValue>, HubClientError> {
        Ok(keys.iter().filter_map(|k| self.read(k)).collect())
    }
}

static GLOBAL: OnceLock<Arc<BundleConfigCache>> = OnceLock::new();

/// Installs the process-wide cache (once, at stage startup). `false` if one is already set.
pub fn install_global(cache: Arc<BundleConfigCache>) -> bool {
    GLOBAL.set(cache).is_ok()
}

/// The process-wide cache, `None` until a stage installs one (hub-api not configured).
#[must_use]
pub fn global() -> Option<Arc<BundleConfigCache>> {
    GLOBAL.get().cloned()
}

/// Queues one `(tenant, community, app)` scope for seeding right after the stage loaded that
/// bundle onto the executor. Fire-and-forget and **coalesced**: every scope queued within the
/// cache's seed window is loaded by one batched query ([`BundleConfigCache::enqueue_seed`]), so a
/// stage warming ~160 bundles costs one round trip. A failed seed only means the first invocation
/// lazy-loads (and logs) instead.
pub fn seed_scope_in_background(tenant_id: i32, community_id: i32, app_id: &str) {
    let Some(cache) = global() else { return };
    if tenant_id < 1 || community_id < 0 {
        return;
    }
    cache.enqueue_seed(ConfigKey::new(
        i64::from(tenant_id),
        i64::from(community_id),
        app_id,
    ));
}

/// Builds, installs and returns the stage's cache with the legacy hub-api source only (no
/// database path). Prefer [`connect_and_install_routed`]; kept for callers/tests that have no
/// database settings.
pub async fn connect_and_install(
    endpoint: &str,
    token_endpoint: &str,
    sa_token_path: &str,
    ca_cert_path: Option<&str>,
    valkey: Option<redis::aio::MultiplexedConnection>,
) -> Option<Arc<BundleConfigCache>> {
    connect_and_install_routed(
        endpoint,
        token_endpoint,
        sa_token_path,
        ca_cert_path,
        valkey,
        &DbReadConfig::from_values(None, None),
    )
    .await
}

/// Builds, installs and returns the stage's cache over the database read path (preferred) and the
/// legacy hub-api gRPC path (behind the `waddles.disable-db-bundle-config-reads` kill switch).
///
/// **Non-fatal**: with neither source configured it logs (ERROR when a configured source failed)
/// and returns `None`, leaving `config_json` as before this feature (`{}`) -- never a stage start
/// failure. With the database configured the stage keeps serving config while hub-api is down or
/// unconfigured: a hub-api connect failure only removes the legacy fallback.
pub async fn connect_and_install_routed(
    endpoint: &str,
    token_endpoint: &str,
    sa_token_path: &str,
    ca_cert_path: Option<&str>,
    valkey: Option<redis::aio::MultiplexedConnection>,
    db: &DbReadConfig,
) -> Option<Arc<BundleConfigCache>> {
    let db_source: Option<Arc<dyn ConfigSource>> = db.url.clone().map(|url| {
        Arc::new(DbConfigSource::new(
            url,
            crate::bundle_config_db::DB_TIMEOUT,
        )) as Arc<dyn ConfigSource>
    });
    let legacy: Option<Arc<dyn ConfigSource>> = if endpoint.is_empty() || token_endpoint.is_empty()
    {
        tracing::info!(
            "bundle_config: HUB_API_GRPC_ENDPOINT/SERVICE_JWT_TOKEN_ENDPOINT unset; no legacy \
             hub-api config path"
        );
        None
    } else {
        match BundleConfigClient::connect(endpoint, token_endpoint, sa_token_path, ca_cert_path)
            .await
        {
            Ok(client) => Some(Arc::new(client) as Arc<dyn ConfigSource>),
            Err(err) => {
                tracing::error!(
                    error = %err,
                    "bundle_config: hub-api connect failed; the legacy hub-api config path is \
                     unavailable until the stage restarts"
                );
                None
            }
        }
    };
    if db_source.is_none() && legacy.is_none() {
        tracing::error!(
            "bundle_config: no config source (set {} or {}, or configure hub-api); bundles \
             receive an EMPTY config_json",
            crate::bundle_config_db::REPLICA_URL_ENV,
            crate::bundle_config_db::PRIMARY_URL_ENV
        );
        return None;
    }
    tracing::info!(
        db = db_source.is_some(),
        db_source = ?db.source,
        legacy_hub_api = legacy.is_some(),
        "bundle_config: live community config sources"
    );
    let probe = valkey.map(|c| Arc::new(ValkeyVersionProbe::new(c)) as Arc<dyn VersionProbe>);
    let cache = Arc::new(BundleConfigCache::new(
        Arc::new(RoutedConfigSource::new(db_source, legacy)),
        probe,
        CacheConfig::default(),
    ));
    install_global(Arc::clone(&cache));
    tracing::info!("bundle_config: live community config cache installed");
    Some(cache)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

    /// Kill switch: unset gate and enabled gate serve config; a disabled gate does no lookup.
    #[tokio::test]
    async fn kill_switch_disables_the_lookup_only_when_on() {
        let src = Arc::new(StaticConfigSource::new());
        let key = ConfigKey::new(1, 2, "app");
        src.set(key.clone(), 1, r#"{"a":1}"#);
        let cache = BundleConfigCache::new(src, None, CacheConfig::default());
        // No gate installed: enabled.
        assert_eq!(&*cache.lookup_if_enabled(&key).await.unwrap(), r#"{"a":1}"#);

        let on = BundleConfigCache::new(
            Arc::new(StaticConfigSource::new()),
            None,
            CacheConfig::default(),
        );
        on.set_gate(Arc::new(StaticLiveGate(true)));
        // Enabled gate on an unknown scope still answers (loud empty), i.e. a lookup happened.
        assert!(on.lookup_if_enabled(&key).await.is_some());

        let off = BundleConfigCache::new(
            Arc::new(StaticConfigSource::new()),
            None,
            CacheConfig::default(),
        );
        off.set_gate(Arc::new(StaticLiveGate(false)));
        assert!(off.lookup_if_enabled(&key).await.is_none());
        assert_eq!(off.len(), 0, "a disabled switch must not load anything");
    }

    use std::sync::atomic::{AtomicBool, AtomicI64, AtomicUsize, Ordering};

    struct FakeSource {
        value: RwLock<(i64, String)>,
        down: AtomicBool,
        gets: AtomicUsize,
        batches: AtomicUsize,
    }

    impl FakeSource {
        fn new(version: i64, json: &str) -> Arc<Self> {
            Arc::new(Self {
                value: RwLock::new((version, json.to_owned())),
                down: AtomicBool::new(false),
                gets: AtomicUsize::new(0),
                batches: AtomicUsize::new(0),
            })
        }
        fn set(&self, version: i64, json: &str) {
            *self.value.write().unwrap() = (version, json.to_owned());
        }
        fn val(&self, key: &ConfigKey) -> ConfigValue {
            let (version, json) = self.value.read().unwrap().clone();
            ConfigValue {
                key: key.clone(),
                version,
                config_json: json,
            }
        }
    }

    #[async_trait]
    impl ConfigSource for FakeSource {
        async fn get(&self, key: &ConfigKey) -> Result<ConfigValue, HubClientError> {
            self.gets.fetch_add(1, Ordering::SeqCst);
            if self.down.load(Ordering::SeqCst) {
                return Err(HubClientError::Grpc(tonic::Status::unavailable("down")));
            }
            Ok(self.val(key))
        }
        async fn batch_get(&self, keys: &[ConfigKey]) -> Result<Vec<ConfigValue>, HubClientError> {
            self.batches.fetch_add(1, Ordering::SeqCst);
            if self.down.load(Ordering::SeqCst) {
                return Err(HubClientError::Grpc(tonic::Status::unavailable("down")));
            }
            Ok(keys.iter().map(|k| self.val(k)).collect())
        }
    }

    struct FakeProbe(AtomicI64);
    #[async_trait]
    impl VersionProbe for FakeProbe {
        async fn latest(&self, _key: &ConfigKey) -> Option<i64> {
            let v = self.0.load(Ordering::SeqCst);
            (v >= 0).then_some(v)
        }
    }

    fn key() -> ConfigKey {
        ConfigKey::new(1, 7, "waddles.core.app.calendar")
    }

    fn fast() -> CacheConfig {
        CacheConfig {
            ttl: Duration::from_secs(60),
            recheck: Duration::from_millis(0),
        }
    }

    #[test]
    fn version_key_matches_the_hub_api_publisher() {
        assert_eq!(
            version_key(&key()),
            "waddles:bundle_config:v:1:7:waddles.core.app.calendar"
        );
    }

    #[tokio::test]
    async fn miss_lazy_loads_then_hits() {
        let src = FakeSource::new(1, r#"{"default_duration_minutes":45}"#);
        let cache = BundleConfigCache::new(src.clone(), None, CacheConfig::default());
        assert_eq!(
            &*cache.lookup(&key()).await,
            r#"{"default_duration_minutes":45}"#
        );
        assert_eq!(
            &*cache.lookup(&key()).await,
            r#"{"default_duration_minutes":45}"#
        );
        assert_eq!(src.gets.load(Ordering::SeqCst), 1, "second lookup is a hit");
    }

    #[tokio::test]
    async fn seed_batch_loads_without_per_scope_gets() {
        let src = FakeSource::new(2, r#"{"a":1}"#);
        let cache = BundleConfigCache::new(src.clone(), None, CacheConfig::default());
        assert_eq!(
            cache.seed(&[key(), ConfigKey::new(1, 8, "x.y.z.w")]).await,
            2
        );
        assert_eq!(cache.len(), 2);
        cache.lookup(&key()).await;
        assert_eq!(src.gets.load(Ordering::SeqCst), 0);
        assert_eq!(src.batches.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn announced_newer_version_reloads() {
        let src = FakeSource::new(1, r#"{"v":1}"#);
        let probe = Arc::new(FakeProbe(AtomicI64::new(1)));
        let cache = BundleConfigCache::new(src.clone(), Some(probe.clone()), fast());
        assert_eq!(&*cache.lookup(&key()).await, r#"{"v":1}"#);
        // probe says same version -> still the cached value, no reload
        assert_eq!(&*cache.lookup(&key()).await, r#"{"v":1}"#);
        assert_eq!(src.gets.load(Ordering::SeqCst), 1);
        // an admin saves: hub-api bumps the version key
        src.set(2, r#"{"v":2}"#);
        probe.0.store(2, Ordering::SeqCst);
        assert_eq!(&*cache.lookup(&key()).await, r#"{"v":2}"#);
        assert_eq!(src.gets.load(Ordering::SeqCst), 2);
    }

    #[tokio::test]
    async fn ttl_expiry_reloads_even_without_a_probe() {
        let src = FakeSource::new(1, r#"{"v":1}"#);
        let cfg = CacheConfig {
            ttl: Duration::from_millis(20),
            recheck: Duration::from_secs(60),
        };
        let cache = BundleConfigCache::new(src.clone(), None, cfg);
        cache.lookup(&key()).await;
        src.set(2, r#"{"v":2}"#);
        tokio::time::sleep(Duration::from_millis(40)).await;
        assert_eq!(&*cache.lookup(&key()).await, r#"{"v":2}"#);
    }

    #[tokio::test]
    async fn hub_down_serves_last_known() {
        let src = FakeSource::new(1, r#"{"v":1}"#);
        let cfg = CacheConfig {
            ttl: Duration::from_millis(20),
            recheck: Duration::from_secs(60),
        };
        let cache = BundleConfigCache::new(src.clone(), None, cfg);
        cache.lookup(&key()).await;
        src.down.store(true, Ordering::SeqCst);
        tokio::time::sleep(Duration::from_millis(40)).await;
        assert_eq!(&*cache.lookup(&key()).await, r#"{"v":1}"#);
    }

    #[tokio::test]
    async fn never_seen_and_down_uses_defaults_else_empty() {
        let src = FakeSource::new(1, "{}");
        src.down.store(true, Ordering::SeqCst);
        let cache = BundleConfigCache::new(src, None, CacheConfig::default());
        assert_eq!(&*cache.lookup(&key()).await, "{}");
        cache.set_defaults(
            "waddles.core.app.calendar",
            r#"{"default_duration_minutes":60}"#,
        );
        assert_eq!(
            &*cache.lookup(&key()).await,
            r#"{"default_duration_minutes":60}"#
        );
        assert!(cache.is_empty(), "a fallback is never cached as if loaded");
    }

    #[tokio::test]
    async fn evict_forces_a_reload() {
        let src = FakeSource::new(1, r#"{"v":1}"#);
        let cache = BundleConfigCache::new(src.clone(), None, CacheConfig::default());
        cache.lookup(&key()).await;
        src.set(2, r#"{"v":2}"#);
        cache.evict(&key());
        assert_eq!(&*cache.lookup(&key()).await, r#"{"v":2}"#);
    }

    /// Warm-up: N bundle loads queued within the window are loaded by ONE `batch_get`, never N.
    #[tokio::test]
    async fn enqueue_seed_coalesces_many_scopes_into_one_batch() {
        let src = FakeSource::new(1, r#"{"a":1}"#);
        let cache = Arc::new(
            BundleConfigCache::new(src.clone(), None, CacheConfig::default())
                .with_seed_window(Duration::from_millis(50)),
        );
        for community in 0..160 {
            cache.enqueue_seed(ConfigKey::new(1, community, "waddles.core.app.calendar"));
        }
        tokio::time::sleep(Duration::from_millis(250)).await;
        assert_eq!(src.batches.load(Ordering::SeqCst), 1, "one batched load");
        assert_eq!(src.gets.load(Ordering::SeqCst), 0, "no per-scope loads");
        assert_eq!(cache.len(), 160);
        // A scope queued after the flush schedules a fresh (second) batch.
        cache.enqueue_seed(ConfigKey::new(1, 999, "waddles.core.app.calendar"));
        tokio::time::sleep(Duration::from_millis(250)).await;
        assert_eq!(src.batches.load(Ordering::SeqCst), 2);
        assert_eq!(cache.len(), 161);
    }

    #[tokio::test]
    async fn seed_failure_is_not_fatal() {
        let src = FakeSource::new(1, "{}");
        src.down.store(true, Ordering::SeqCst);
        let cache = BundleConfigCache::new(src, None, CacheConfig::default());
        assert_eq!(cache.seed(&[key()]).await, 0);
        assert!(cache.is_empty());
    }
}
