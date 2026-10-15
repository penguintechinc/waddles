//! Dataplane scope resolver: `(platform, source id)` -> authoritative numeric
//! `(tenant_id, community_id)` for the schema-v3 envelope producer.
//!
//! svc-ingest has no database, so it asks hub-api's
//! `GET /api/v1/internal/dataplane/resolve-scope` which community a Discord
//! guild / Twitch channel belongs to. The call is authenticated with a
//! `service_auth::MachineJwtClient` token (scope `dataplane:resolve-scope`).
//!
//! # Hot-path behavior
//!
//! [`ScopeResolver::resolve`] is called once per inbound event and therefore:
//! - serves from an in-memory **LRU cache** (capacity-bounded, short TTL);
//! - **single-flights** cold misses -- N concurrent events for one source make
//!   exactly one upstream call (per-key lock, followers re-read the cache);
//! - **never defaults a scope**: svc-ingest is a cross-tenant ingest point, so
//!   an unresolvable source (404/409/400, inconsistent upstream data, DM /
//!   sourceless event) or a failed lookup (hub-api down, timeout, 5xx, auth)
//!   yields `Err(Unresolved)` and the caller DROPS the event (counted in a
//!   metric, never published, never stamped with any tenant/community).
//!   Transient failures are retried with bounded backoff before giving up and
//!   are negatively cached very briefly so a sick hub-api is not hammered.
//!
//! Nothing here logs a token or message content: log lines carry only the
//! platform and the outcome label.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use serde::Deserialize;
use tokio::sync::Mutex;
use tokio::time::Instant;

/// Machine-JWT scope svc-ingest's identity holds for this endpoint.
pub const RESOLVE_SCOPE_JWT_SCOPE: &str = "dataplane:resolve-scope";
/// Feature flag gating the whole v3 producer path (default OFF).
pub const INGEST_DYNAMIC_CONNECTIONS_FLAG: &str = "waddles.ingest-dynamic-connections";
/// `community_id` meaning "tenant-wide" (the `_tenant` stream segment).
pub const TENANT_WIDE_COMMUNITY_ID: i32 = 0;

/// Upstream attempts per cold miss for transient failures (1 + 2 retries).
const MAX_ATTEMPTS: u32 = 3;
/// First retry delay; doubles per retry.
const RETRY_BASE: Duration = Duration::from_millis(100);

/// Cache sizing and lifetimes. Positive TTL mirrors hub-api's own
/// `Cache-Control: private, max-age=30`.
#[derive(Debug, Clone, Copy)]
pub struct ResolverConfig {
    /// Maximum cached sources (least-recently-used evicted beyond this).
    pub capacity: usize,
    /// Lifetime of a successful resolution.
    pub ttl: Duration,
    /// Lifetime of a 404/409/invalid-data (definitive "no community") result.
    pub negative_ttl: Duration,
    /// Lifetime of a transient failure (network/auth/5xx) -- kept short so
    /// recovery is quick, but non-zero so an outage is not a stampede.
    pub error_ttl: Duration,
}

impl Default for ResolverConfig {
    fn default() -> Self {
        Self {
            capacity: 4096,
            ttl: Duration::from_secs(30),
            negative_ttl: Duration::from_secs(10),
            error_ttl: Duration::from_secs(3),
        }
    }
}

/// A scope the producer may stamp: numeric keys plus display-only strings.
///
/// Invariants enforced by [`ResolvedScope::validated`]: `tenant_id >= 1`,
/// `community_id >= 0`, and `community_name.is_none() == (community_id == 0)`
/// (finding 4: the string scope the stream key/`verify_hop` compares must never
/// diverge from the authoritative numeric scope).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedScope {
    /// Authoritative tenant key.
    pub tenant_id: i64,
    /// Authoritative community key; `0` = tenant-wide.
    pub community_id: i32,
    /// Display-only tenant slug (also the stream-key tenant segment).
    pub tenant_slug: String,
    /// Display-only community name; `None` iff `community_id == 0`.
    pub community_name: Option<String>,
}

/// Why a [`ResolvedScope`] failed its consistency checks.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum ScopeInconsistency {
    /// `tenant_id` below 1.
    #[error("tenant_id {0} must be >= 1")]
    BadTenantId(i64),
    /// `community_id` negative.
    #[error("community_id {0} must be >= 0")]
    BadCommunityId(i64),
    /// Tenant slug empty or not stream-key-safe.
    #[error("tenant slug is empty or not stream-key-safe")]
    BadTenantSlug,
    /// `community.is_none() != (community_id == 0)`.
    #[error("community name presence disagrees with community_id (tenant-wide iff id 0)")]
    CommunityPresenceMismatch,
    /// Community name empty or not stream-key-safe.
    #[error("community name is empty or not stream-key-safe")]
    BadCommunityName,
}

impl ResolvedScope {
    /// Builds a scope, enforcing every invariant; the only constructor
    /// resolution results pass through.
    pub fn validated(
        tenant_id: i64,
        community_id: i64,
        tenant_slug: &str,
        community_name: Option<&str>,
    ) -> Result<Self, ScopeInconsistency> {
        if tenant_id < 1 {
            return Err(ScopeInconsistency::BadTenantId(tenant_id));
        }
        let community_id32 = i32::try_from(community_id)
            .ok()
            .filter(|c| *c >= TENANT_WIDE_COMMUNITY_ID)
            .ok_or(ScopeInconsistency::BadCommunityId(community_id))?;
        if community_name.is_some() != (community_id32 != TENANT_WIDE_COMMUNITY_ID) {
            return Err(ScopeInconsistency::CommunityPresenceMismatch);
        }
        let scope = Self {
            tenant_id,
            community_id: community_id32,
            tenant_slug: tenant_slug.to_string(),
            community_name: community_name.map(str::to_string),
        };
        // The strings become the Valkey stream-key segments that consumers
        // re-parse with `parse_scope_from_key` and string-compare (verify_hop):
        // anything that does not round-trip would silently route elsewhere.
        if !scope.strings_round_trip() {
            return Err(
                if scope.community_name.is_some() && is_key_safe(tenant_slug) {
                    ScopeInconsistency::BadCommunityName
                } else {
                    ScopeInconsistency::BadTenantSlug
                },
            );
        }
        Ok(scope)
    }

    /// The spine string scope (stream key / hop-verification display scope).
    pub fn spine_scope(&self) -> penguin_spine::Scope {
        penguin_spine::Scope::new(self.tenant_slug.clone(), self.community_name.clone())
    }

    /// True when the spine key built from the display strings parses back to
    /// the same strings (rejects ':' / whitespace / empty / reserved segments).
    fn strings_round_trip(&self) -> bool {
        if !is_key_safe(&self.tenant_slug) {
            return false;
        }
        if let Some(c) = &self.community_name {
            if !is_key_safe(c) {
                return false;
            }
        }
        let key = self.spine_scope().source_stream("p", "s");
        penguin_spine::parse_scope_from_key(&key)
            == Some((self.tenant_slug.clone(), self.community_name.clone()))
    }
}

/// Conservative stream-key segment charset (no `:`, no whitespace/controls).
fn is_key_safe(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 128
        && s != penguin_spine::TENANT_WIDE_SEGMENT
        && s.chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.'))
}

/// Result of one upstream lookup.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FetchError {
    /// 404 -- no active community linked.
    NotFound,
    /// 409 -- the source maps to more than one community.
    Ambiguous,
    /// 400 -- hub-api refused the source id as invalid.
    Invalid,
    /// Transport/auth/5xx/parse failure (reason is a fixed label, no data).
    Transient(&'static str),
}

/// One upstream scope lookup (hub-api in production; a fake in tests).
#[allow(async_fn_in_trait)]
pub trait ScopeSource: Send + Sync {
    /// Resolves `(platform, source_id)`; `source_id` is the platform's guild
    /// or channel identifier.
    async fn fetch(&self, platform: &str, source_id: &str) -> Result<RawScope, FetchError>;
}

/// Unvalidated upstream payload.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RawScope {
    /// Tenant key.
    pub tenant_id: i64,
    /// Community key.
    pub community_id: i64,
    /// Tenant slug.
    pub tenant_slug: String,
    /// Community name (empty string means none).
    pub community_name: String,
}

/// Counter sink for resolver outcomes -- implemented over Prometheus in
/// production ([`crate::telemetry::IngestMetrics`]) and a recorder in tests.
pub trait ResolverMetrics: Send + Sync {
    /// Records one resolution outcome: `hit`, `resolved`, or an
    /// [`Unresolved::label`] (each of which means the event is dropped).
    fn scope_resolution(&self, platform: &str, outcome: &'static str);
}

/// `(lowercased platform, source id)`.
type CacheKey = (String, String);

/// Why a source could not be resolved; every variant means the event is
/// dropped, never routed under a default scope.
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum Unresolved {
    /// 404: no active community linked to this source.
    #[error("source is not mapped to a community")]
    NotFound,
    /// 409: source maps to more than one community.
    #[error("source maps to more than one community")]
    Ambiguous,
    /// 400: hub-api refused the source id.
    #[error("source id is invalid")]
    Invalid,
    /// Upstream payload failed the id/string consistency checks.
    #[error("upstream scope failed consistency checks")]
    Inconsistent,
    /// Lookup failed (hub-api down/timeout/5xx/auth) after bounded retries.
    #[error("scope lookup failed")]
    Transient,
    /// The event carried no resolvable platform source (DM / sourceless).
    #[error("event has no resolvable platform source")]
    NoSource,
}

impl Unresolved {
    /// Metric / log label.
    pub fn label(self) -> &'static str {
        match self {
            Self::NotFound => "not_found",
            Self::Ambiguous => "ambiguous",
            Self::Invalid => "invalid",
            Self::Inconsistent => "inconsistent",
            Self::Transient => "error",
            Self::NoSource => "no_source",
        }
    }
}

/// A cached resolution: the real scope, or why there is none.
type Resolution = Result<ResolvedScope, Unresolved>;

#[derive(Clone)]
struct Entry {
    scope: Resolution,
    expires: Instant,
    last_used: u64,
}

struct Cache {
    map: HashMap<CacheKey, Entry>,
    tick: u64,
}

/// Async LRU + single-flight resolver. Cheap to share behind an `Arc`.
pub struct ScopeResolver<S: ScopeSource> {
    source: S,
    cfg: ResolverConfig,
    metrics: Arc<dyn ResolverMetrics>,
    cache: Mutex<Cache>,
    /// Per-key single-flight locks; entries are dropped when idle.
    inflight: Mutex<HashMap<CacheKey, Arc<Mutex<()>>>>,
}

impl<S: ScopeSource> ScopeResolver<S> {
    /// Builds a resolver over `source`.
    pub fn new(source: S, cfg: ResolverConfig, metrics: Arc<dyn ResolverMetrics>) -> Self {
        Self {
            source,
            cfg,
            metrics,
            cache: Mutex::new(Cache {
                map: HashMap::new(),
                tick: 0,
            }),
            inflight: Mutex::new(HashMap::new()),
        }
    }

    /// Records that an event carried no resolvable source (e.g. a Discord DM)
    /// and returns the drop reason -- no upstream call, no default scope.
    pub fn no_source(&self, platform: &str) -> Unresolved {
        self.metrics
            .scope_resolution(platform, Unresolved::NoSource.label());
        Unresolved::NoSource
    }

    /// Resolves a source to its real scope, or says why it cannot be (the
    /// caller must then drop the event -- there is no default scope).
    pub async fn resolve(&self, platform: &str, source_id: &str) -> Resolution {
        let key = (platform.to_ascii_lowercase(), source_id.to_string());
        if let Some(hit) = self.lookup(&key).await {
            self.metrics.scope_resolution(platform, "hit");
            return hit;
        }

        // Single flight: one leader per key does the upstream call; followers
        // block on the key lock, then find the leader's (possibly negative)
        // entry in the cache.
        let gate = {
            let mut inflight = self.inflight.lock().await;
            Arc::clone(inflight.entry(key.clone()).or_default())
        };
        let result = {
            let _leader = gate.lock().await;
            if let Some(hit) = self.lookup(&key).await {
                self.metrics.scope_resolution(platform, "hit");
                hit
            } else {
                self.fetch_and_store(&key, platform, source_id).await
            }
        };
        let mut inflight = self.inflight.lock().await;
        // 2 = the map's reference + our `gate`: no other waiter is queued.
        if inflight
            .get(&key)
            .is_some_and(|g| Arc::strong_count(g) <= 2)
        {
            inflight.remove(&key);
        }
        result
    }

    async fn fetch_and_store(&self, key: &CacheKey, platform: &str, source_id: &str) -> Resolution {
        let mut attempt = 0u32;
        let fetched = loop {
            match self.source.fetch(platform, source_id).await {
                Err(FetchError::Transient(reason)) if attempt + 1 < MAX_ATTEMPTS => {
                    tracing::debug!(platform, reason, attempt, "scope lookup failed; retrying");
                    tokio::time::sleep(RETRY_BASE * 2u32.pow(attempt)).await;
                    attempt += 1;
                }
                other => break other,
            }
        };
        let (scope, ttl): (Resolution, Duration) = match fetched {
            Ok(raw) => {
                let name = (!raw.community_name.is_empty()).then_some(raw.community_name.as_str());
                match ResolvedScope::validated(
                    raw.tenant_id,
                    raw.community_id,
                    &raw.tenant_slug,
                    name,
                ) {
                    Ok(scope) => (Ok(scope), self.cfg.ttl),
                    Err(why) => {
                        tracing::error!(platform, reason = %why, "scope resolver returned an inconsistent scope; event will be dropped");
                        (Err(Unresolved::Inconsistent), self.cfg.negative_ttl)
                    }
                }
            }
            Err(FetchError::NotFound) => (Err(Unresolved::NotFound), self.cfg.negative_ttl),
            Err(FetchError::Ambiguous) => (Err(Unresolved::Ambiguous), self.cfg.negative_ttl),
            Err(FetchError::Invalid) => (Err(Unresolved::Invalid), self.cfg.negative_ttl),
            Err(FetchError::Transient(reason)) => {
                tracing::warn!(
                    platform,
                    reason,
                    "scope lookup failed after retries; event will be dropped"
                );
                (Err(Unresolved::Transient), self.cfg.error_ttl)
            }
        };
        self.metrics.scope_resolution(
            platform,
            match &scope {
                Ok(_) => "resolved",
                Err(u) => u.label(),
            },
        );
        self.store(key.clone(), scope.clone(), ttl).await;
        scope
    }

    async fn lookup(&self, key: &CacheKey) -> Option<Resolution> {
        let mut cache = self.cache.lock().await;
        cache.tick += 1;
        let tick = cache.tick;
        let now = Instant::now();
        match cache.map.get_mut(key) {
            Some(e) if e.expires > now => {
                e.last_used = tick;
                Some(e.scope.clone())
            }
            Some(_) => {
                cache.map.remove(key);
                None
            }
            None => None,
        }
    }

    async fn store(&self, key: CacheKey, scope: Resolution, ttl: Duration) {
        let mut cache = self.cache.lock().await;
        cache.tick += 1;
        let tick = cache.tick;
        if cache.map.len() >= self.cfg.capacity && !cache.map.contains_key(&key) {
            if let Some(oldest) = cache
                .map
                .iter()
                .min_by_key(|(_, e)| e.last_used)
                .map(|(k, _)| k.clone())
            {
                cache.map.remove(&oldest);
            }
        }
        cache.map.insert(
            key,
            Entry {
                scope,
                expires: Instant::now() + ttl,
                last_used: tick,
            },
        );
    }
}

/// Wire shape of hub-api's 200 response (`ScopeDTO`).
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ScopeDto {
    tenant_id: i64,
    community_id: i64,
    tenant_slug: String,
    #[serde(default)]
    community_name: String,
}

/// Refuses a plaintext `http://` hub-api URL unless running in alpha -- the
/// machine JWT rides this connection.
pub fn check_transport(hub_api_url: &str, environment: &str) -> Result<(), String> {
    let lower = hub_api_url.trim().to_ascii_lowercase();
    if lower.starts_with("https://") {
        return Ok(());
    }
    if lower.starts_with("http://") {
        if environment.eq_ignore_ascii_case("alpha") {
            return Ok(());
        }
        return Err(format!(
            "refusing plaintext http:// HUB_API_URL for dynamic connections outside alpha (environment {environment:?})"
        ));
    }
    Err("HUB_API_URL must be an http(s) URL".to_string())
}

/// Production [`ScopeSource`]: hub-api over HTTP with a machine JWT.
pub struct HubScopeSource {
    http: reqwest::Client,
    base_url: String,
    jwt: Arc<service_auth::MachineJwtClient>,
}

impl HubScopeSource {
    /// `base_url` is `HUB_API_URL` (no trailing slash needed).
    pub fn new(
        base_url: &str,
        jwt: Arc<service_auth::MachineJwtClient>,
    ) -> Result<Self, reqwest::Error> {
        let http = reqwest::Client::builder()
            .timeout(Duration::from_secs(3))
            .connect_timeout(Duration::from_secs(2))
            .build()?;
        Ok(Self {
            http,
            base_url: base_url.trim_end_matches('/').to_string(),
            jwt,
        })
    }
}

impl ScopeSource for HubScopeSource {
    async fn fetch(&self, platform: &str, source_id: &str) -> Result<RawScope, FetchError> {
        let token = self
            .jwt
            .get_token()
            .await
            .map_err(|_| FetchError::Transient("token"))?;
        let resp = self
            .http
            .get(format!(
                "{}/api/v1/internal/dataplane/resolve-scope",
                self.base_url
            ))
            .query(&[("platform", platform), ("source_id", source_id)])
            .bearer_auth(&*token)
            .send()
            .await
            .map_err(|_| FetchError::Transient("transport"))?;
        match resp.status().as_u16() {
            200 => {
                let dto: ScopeDto = resp
                    .json()
                    .await
                    .map_err(|_| FetchError::Transient("parse"))?;
                Ok(RawScope {
                    tenant_id: dto.tenant_id,
                    community_id: dto.community_id,
                    tenant_slug: dto.tenant_slug,
                    community_name: dto.community_name,
                })
            }
            404 => Err(FetchError::NotFound),
            409 => Err(FetchError::Ambiguous),
            400 => Err(FetchError::Invalid),
            401 | 403 => Err(FetchError::Transient("auth")),
            _ => Err(FetchError::Transient("status")),
        }
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Mutex as StdMutex;

    #[derive(Default)]
    struct Rec(StdMutex<Vec<(String, &'static str)>>);
    impl ResolverMetrics for Rec {
        fn scope_resolution(&self, platform: &str, outcome: &'static str) {
            self.0.lock().unwrap().push((platform.to_string(), outcome));
        }
    }
    impl Rec {
        fn count(&self, outcome: &str) -> usize {
            self.0
                .lock()
                .unwrap()
                .iter()
                .filter(|(_, o)| *o == outcome)
                .count()
        }
    }

    struct Fake {
        calls: AtomicUsize,
        reply: StdMutex<Result<RawScope, FetchError>>,
        delay: Duration,
    }
    impl Fake {
        fn new(reply: Result<RawScope, FetchError>) -> Self {
            Self {
                calls: AtomicUsize::new(0),
                reply: StdMutex::new(reply),
                delay: Duration::from_millis(50),
            }
        }
    }
    impl ScopeSource for Arc<Fake> {
        async fn fetch(&self, _p: &str, _s: &str) -> Result<RawScope, FetchError> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            tokio::time::sleep(self.delay).await;
            self.reply.lock().unwrap().clone()
        }
    }

    fn raw(t: i64, c: i64, ts: &str, cn: &str) -> RawScope {
        RawScope {
            tenant_id: t,
            community_id: c,
            tenant_slug: ts.to_string(),
            community_name: cn.to_string(),
        }
    }

    fn resolver(fake: &Arc<Fake>, cfg: ResolverConfig) -> (ScopeResolver<Arc<Fake>>, Arc<Rec>) {
        let rec = Arc::new(Rec::default());
        let r = ScopeResolver::new(Arc::clone(fake), cfg, rec.clone());
        (r, rec)
    }

    #[tokio::test(start_paused = true)]
    async fn resolves_then_serves_from_cache() {
        let fake = Arc::new(Fake::new(Ok(raw(2, 9, "acme", "main"))));
        let (r, rec) = resolver(&fake, ResolverConfig::default());
        let a = r.resolve("discord", "111").await.unwrap();
        let b = r.resolve("discord", "111").await.unwrap();
        assert_eq!(a, b);
        assert_eq!((a.tenant_id, a.community_id), (2, 9));
        assert_eq!(a.community_name.as_deref(), Some("main"));
        assert_eq!(
            fake.calls.load(Ordering::SeqCst),
            1,
            "second call is a cache hit"
        );
        assert_eq!(rec.count("resolved"), 1);
        assert_eq!(rec.count("hit"), 1);
    }

    #[tokio::test(start_paused = true)]
    async fn single_flight_makes_one_upstream_call_under_concurrency() {
        let fake = Arc::new(Fake::new(Ok(raw(2, 9, "acme", "main"))));
        let (r, _rec) = resolver(&fake, ResolverConfig::default());
        let r = Arc::new(r);
        let mut tasks = Vec::new();
        for _ in 0..32 {
            let r = Arc::clone(&r);
            tasks.push(tokio::spawn(
                async move { r.resolve("discord", "111").await },
            ));
        }
        for t in tasks {
            assert_eq!(t.await.unwrap().unwrap().community_id, 9);
        }
        assert_eq!(
            fake.calls.load(Ordering::SeqCst),
            1,
            "stampede must collapse to one call"
        );
        assert!(
            r.inflight.lock().await.is_empty(),
            "single-flight map drains"
        );
    }

    #[tokio::test(start_paused = true)]
    async fn misses_and_failures_are_unresolved_never_a_default_scope() {
        for (err, want, calls) in [
            (FetchError::NotFound, Unresolved::NotFound, 1),
            (FetchError::Ambiguous, Unresolved::Ambiguous, 1),
            (FetchError::Invalid, Unresolved::Invalid, 1),
            // transient: 1 try + 2 bounded retries, then give up
            (FetchError::Transient("transport"), Unresolved::Transient, 3),
        ] {
            let fake = Arc::new(Fake::new(Err(err)));
            let (r, rec) = resolver(&fake, ResolverConfig::default());
            assert_eq!(r.resolve("twitch", "chan").await, Err(want));
            assert_eq!(fake.calls.load(Ordering::SeqCst), calls, "{want:?}");
            assert_eq!(rec.count(want.label()), 1, "{want:?} counted");
            // negatively cached: no further upstream call within the TTL
            assert_eq!(r.resolve("twitch", "chan").await, Err(want));
            assert_eq!(fake.calls.load(Ordering::SeqCst), calls);
        }
    }

    #[tokio::test(start_paused = true)]
    async fn transient_failure_recovers_within_the_retry_budget() {
        struct Flaky(AtomicUsize);
        impl ScopeSource for Flaky {
            async fn fetch(&self, _p: &str, _s: &str) -> Result<RawScope, FetchError> {
                if self.0.fetch_add(1, Ordering::SeqCst) < 2 {
                    Err(FetchError::Transient("timeout"))
                } else {
                    Ok(raw(2, 9, "acme", "main"))
                }
            }
        }
        let r = ScopeResolver::new(
            Flaky(AtomicUsize::new(0)),
            ResolverConfig::default(),
            Arc::new(Rec::default()),
        );
        assert_eq!(r.resolve("discord", "1").await.unwrap().community_id, 9);
    }

    #[tokio::test(start_paused = true)]
    async fn ttl_expiry_refetches_and_picks_up_the_new_scope() {
        let fake = Arc::new(Fake::new(Err(FetchError::NotFound)));
        let (r, _rec) = resolver(&fake, ResolverConfig::default());
        assert_eq!(r.resolve("discord", "1").await, Err(Unresolved::NotFound));
        // The link is created upstream; still negatively cached inside the TTL.
        *fake.reply.lock().unwrap() = Ok(raw(2, 9, "acme", "main"));
        assert_eq!(r.resolve("discord", "1").await, Err(Unresolved::NotFound));
        assert_eq!(fake.calls.load(Ordering::SeqCst), 1);
        tokio::time::advance(Duration::from_secs(11)).await;
        assert_eq!(
            r.resolve("discord", "1").await.unwrap().community_id,
            9,
            "negative TTL expired"
        );
        assert_eq!(fake.calls.load(Ordering::SeqCst), 2);
        tokio::time::advance(Duration::from_secs(31)).await;
        r.resolve("discord", "1").await.unwrap();
        assert_eq!(fake.calls.load(Ordering::SeqCst), 3, "positive TTL expired");
    }

    #[tokio::test(start_paused = true)]
    async fn lru_evicts_the_least_recently_used_entry() {
        let fake = Arc::new(Fake::new(Ok(raw(2, 9, "acme", "main"))));
        let cfg = ResolverConfig {
            capacity: 2,
            ..ResolverConfig::default()
        };
        let (r, _rec) = resolver(&fake, cfg);
        r.resolve("discord", "a").await.unwrap();
        r.resolve("discord", "b").await.unwrap();
        r.resolve("discord", "a").await.unwrap(); // a is now most recent
        r.resolve("discord", "c").await.unwrap(); // evicts b
        assert_eq!(fake.calls.load(Ordering::SeqCst), 3);
        r.resolve("discord", "a").await.unwrap();
        assert_eq!(fake.calls.load(Ordering::SeqCst), 3, "a survived");
        r.resolve("discord", "b").await.unwrap();
        assert_eq!(fake.calls.load(Ordering::SeqCst), 4, "b was evicted");
    }

    #[tokio::test(start_paused = true)]
    async fn inconsistent_upstream_data_is_unresolved() {
        for bad in [
            raw(2, 9, "acme", ""),                           // community id without a name
            raw(2, 0, "acme", "main"),                       // name with the tenant-wide id
            raw(0, 9, "acme", "main"),                       // tenant id < 1
            raw(2, -1, "acme", "main"),                      // negative community id
            raw(2, 9, "ac:me", "main"),                      // key-unsafe tenant slug
            raw(2, 9, "acme", "Bar Citizen"),                // key-unsafe community name
            raw(2, i64::from(i32::MAX) + 1, "acme", "main"), // does not fit i32
        ] {
            let fake = Arc::new(Fake::new(Ok(bad.clone())));
            let (r, rec) = resolver(&fake, ResolverConfig::default());
            let got = r.resolve("discord", "1").await;
            assert_eq!(got, Err(Unresolved::Inconsistent), "{bad:?}");
            assert_eq!(rec.count("inconsistent"), 1, "{bad:?}");
        }
    }

    #[test]
    fn validated_accepts_tenant_wide_and_community_scopes() {
        let t = ResolvedScope::validated(3, 0, "acme", None).unwrap();
        assert_eq!(t.community_name, None);
        let c = ResolvedScope::validated(3, 5, "acme", Some("main_1")).unwrap();
        assert_eq!(c.spine_scope().community.as_deref(), Some("main_1"));
        assert_eq!(
            ResolvedScope::validated(3, 5, "acme", None),
            Err(ScopeInconsistency::CommunityPresenceMismatch)
        );
    }

    #[test]
    fn plaintext_guard_allows_http_only_in_alpha() {
        assert!(check_transport("https://hub:8204", "beta").is_ok());
        assert!(check_transport("http://hub:8204", "alpha").is_ok());
        assert!(check_transport("http://hub:8204", "beta").is_err());
        assert!(check_transport("http://hub:8204", "").is_err());
        assert!(check_transport("ftp://x", "alpha").is_err());
    }

    // ---- HubScopeSource over real HTTP (wiremock) ----

    use wiremock::matchers::{header, method, path, query_param};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    async fn hub_source(server: &MockServer) -> (HubScopeSource, std::path::PathBuf) {
        Mock::given(method("POST"))
            .and(path("/internal/service-token"))
            .respond_with(ResponseTemplate::new(200).set_body_json(
                serde_json::json!({"token": "machine.jwt.value", "expires_in": 3600}),
            ))
            .mount(server)
            .await;
        let sa = std::env::temp_dir().join(format!("svc-ingest-sa-{}", uuid::Uuid::new_v4()));
        std::fs::write(&sa, "projected-sa-token").unwrap();
        let jwt = Arc::new(service_auth::MachineJwtClient::new(
            format!("{}/internal/service-token", server.uri()),
            sa.to_string_lossy().to_string(),
            RESOLVE_SCOPE_JWT_SCOPE,
        ));
        (HubScopeSource::new(&server.uri(), jwt).unwrap(), sa)
    }

    #[tokio::test]
    async fn hub_source_calls_endpoint_with_machine_jwt_and_parses_scope() {
        let server = MockServer::start().await;
        let (src, sa) = hub_source(&server).await;
        Mock::given(method("GET"))
            .and(path("/api/v1/internal/dataplane/resolve-scope"))
            .and(query_param("platform", "discord"))
            .and(query_param("source_id", "111"))
            .and(header("authorization", "Bearer machine.jwt.value"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "success": true, "tenantId": 2, "communityId": 9,
                "tenantSlug": "acme", "communityName": "main"
            })))
            .expect(1)
            .mount(&server)
            .await;
        let got = src.fetch("discord", "111").await.unwrap();
        assert_eq!(got, raw(2, 9, "acme", "main"));
        let _ = std::fs::remove_file(sa);
    }

    #[tokio::test]
    async fn hub_source_maps_status_codes_to_fetch_errors() {
        for (status, want) in [
            (404, FetchError::NotFound),
            (409, FetchError::Ambiguous),
            (400, FetchError::Invalid),
            (403, FetchError::Transient("auth")),
            (500, FetchError::Transient("status")),
        ] {
            let server = MockServer::start().await;
            let (src, sa) = hub_source(&server).await;
            Mock::given(method("GET"))
                .and(path("/api/v1/internal/dataplane/resolve-scope"))
                .respond_with(ResponseTemplate::new(status))
                .mount(&server)
                .await;
            assert_eq!(src.fetch("twitch", "chan").await, Err(want), "{status}");
            let _ = std::fs::remove_file(sa);
        }
    }

    #[tokio::test]
    async fn hub_source_without_a_token_is_transient_not_a_panic() {
        let jwt = Arc::new(service_auth::MachineJwtClient::new(
            "http://127.0.0.1:1/token",
            "/nonexistent/sa-token",
            RESOLVE_SCOPE_JWT_SCOPE,
        ));
        let src = HubScopeSource::new("http://127.0.0.1:1", jwt).unwrap();
        assert_eq!(
            src.fetch("twitch", "c").await,
            Err(FetchError::Transient("token"))
        );
    }
}
