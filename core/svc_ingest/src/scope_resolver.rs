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
//! - **fails closed**: a 404/409/transport/auth/parse failure, or a response
//!   that fails the id/string consistency checks, resolves to the tenant-wide
//!   sentinel `(configured tenant id, community 0)` -- it never guesses and
//!   never mis-routes. Failures are cached for a short negative TTL so a sick
//!   hub-api is not hammered, and every outcome is counted in a metric.
//!
//! Nothing here logs a token, a source id's message content, or any PII: log
//! lines carry only the platform, the outcome and (for the sentinel path) the
//! numeric ids.

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

    /// The tenant-wide sentinel for `tenant_id` under `tenant_slug`.
    pub fn sentinel(tenant_id: i64, tenant_slug: &str) -> Self {
        Self {
            tenant_id,
            community_id: TENANT_WIDE_COMMUNITY_ID,
            tenant_slug: tenant_slug.to_string(),
            community_name: None,
        }
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
    /// Records one resolution outcome (`hit`, `resolved`, `not_found`,
    /// `ambiguous`, `invalid`, `inconsistent`, `error`, `no_source`).
    fn scope_resolution(&self, platform: &str, outcome: &'static str);
}

/// `(lowercased platform, source id)`.
type CacheKey = (String, String);

#[derive(Clone)]
struct Entry {
    scope: ResolvedScope,
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
    sentinel: ResolvedScope,
    metrics: Arc<dyn ResolverMetrics>,
    cache: Mutex<Cache>,
    /// Per-key single-flight locks; entries are dropped when idle.
    inflight: Mutex<HashMap<CacheKey, Arc<Mutex<()>>>>,
}

impl<S: ScopeSource> ScopeResolver<S> {
    /// `sentinel` is the fail-closed scope `(configured tenant id, community 0)`.
    pub fn new(
        source: S,
        cfg: ResolverConfig,
        sentinel: ResolvedScope,
        metrics: Arc<dyn ResolverMetrics>,
    ) -> Self {
        debug_assert_eq!(sentinel.community_id, TENANT_WIDE_COMMUNITY_ID);
        Self {
            source,
            cfg,
            sentinel,
            metrics,
            cache: Mutex::new(Cache {
                map: HashMap::new(),
                tick: 0,
            }),
            inflight: Mutex::new(HashMap::new()),
        }
    }

    /// The fail-closed scope this resolver returns on any miss.
    pub fn sentinel(&self) -> &ResolvedScope {
        &self.sentinel
    }

    /// Records that an event carried no resolvable source (e.g. a Discord DM)
    /// and returns the sentinel without any upstream call.
    pub fn no_source(&self, platform: &str) -> ResolvedScope {
        self.metrics.scope_resolution(platform, "no_source");
        self.sentinel.clone()
    }

    /// Resolves a source to its scope; **never fails** -- every failure mode
    /// returns the tenant-wide sentinel (fail closed).
    pub async fn resolve(&self, platform: &str, source_id: &str) -> ResolvedScope {
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

    async fn fetch_and_store(
        &self,
        key: &(String, String),
        platform: &str,
        source_id: &str,
    ) -> ResolvedScope {
        let (scope, outcome, ttl) = match self.source.fetch(platform, source_id).await {
            Ok(raw) => {
                let name = (!raw.community_name.is_empty()).then_some(raw.community_name.as_str());
                match ResolvedScope::validated(
                    raw.tenant_id,
                    raw.community_id,
                    &raw.tenant_slug,
                    name,
                ) {
                    Ok(scope) => (scope, "resolved", self.cfg.ttl),
                    Err(why) => {
                        tracing::error!(
                            platform,
                            reason = %why,
                            "scope resolver returned an inconsistent scope; failing closed to tenant-wide sentinel"
                        );
                        (self.sentinel.clone(), "inconsistent", self.cfg.negative_ttl)
                    }
                }
            }
            Err(FetchError::NotFound) => {
                (self.sentinel.clone(), "not_found", self.cfg.negative_ttl)
            }
            Err(FetchError::Ambiguous) => {
                (self.sentinel.clone(), "ambiguous", self.cfg.negative_ttl)
            }
            Err(FetchError::Invalid) => (self.sentinel.clone(), "invalid", self.cfg.negative_ttl),
            Err(FetchError::Transient(reason)) => {
                tracing::warn!(
                    platform,
                    reason,
                    "scope resolve failed; failing closed to tenant-wide sentinel"
                );
                (self.sentinel.clone(), "error", self.cfg.error_ttl)
            }
        };
        self.metrics.scope_resolution(platform, outcome);
        self.store(key.clone(), scope.clone(), ttl).await;
        scope
    }

    async fn lookup(&self, key: &(String, String)) -> Option<ResolvedScope> {
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

    async fn store(&self, key: (String, String), scope: ResolvedScope, ttl: Duration) {
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
        let r = ScopeResolver::new(
            Arc::clone(fake),
            cfg,
            ResolvedScope::sentinel(7, "global"),
            rec.clone(),
        );
        (r, rec)
    }

    #[tokio::test(start_paused = true)]
    async fn resolves_then_serves_from_cache() {
        let fake = Arc::new(Fake::new(Ok(raw(2, 9, "acme", "main"))));
        let (r, rec) = resolver(&fake, ResolverConfig::default());
        let a = r.resolve("discord", "111").await;
        let b = r.resolve("discord", "111").await;
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
            assert_eq!(t.await.unwrap().community_id, 9);
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
    async fn not_found_and_ambiguous_fail_closed_to_sentinel() {
        for (err, label) in [
            (FetchError::NotFound, "not_found"),
            (FetchError::Ambiguous, "ambiguous"),
            (FetchError::Invalid, "invalid"),
            (FetchError::Transient("transport"), "error"),
        ] {
            let fake = Arc::new(Fake::new(Err(err)));
            let (r, rec) = resolver(&fake, ResolverConfig::default());
            let got = r.resolve("twitch", "chan").await;
            assert_eq!((got.tenant_id, got.community_id), (7, 0), "{label}");
            assert_eq!(got.community_name, None);
            assert_eq!(rec.count(label), 1, "{label} counted");
        }
    }

    #[tokio::test(start_paused = true)]
    async fn ttl_expiry_refetches_and_picks_up_the_new_scope() {
        let fake = Arc::new(Fake::new(Err(FetchError::NotFound)));
        let (r, _rec) = resolver(&fake, ResolverConfig::default());
        assert_eq!(r.resolve("discord", "1").await.community_id, 0);
        // The link is created upstream; still negatively cached inside the TTL.
        *fake.reply.lock().unwrap() = Ok(raw(2, 9, "acme", "main"));
        assert_eq!(r.resolve("discord", "1").await.community_id, 0);
        assert_eq!(fake.calls.load(Ordering::SeqCst), 1);
        tokio::time::advance(Duration::from_secs(11)).await;
        assert_eq!(
            r.resolve("discord", "1").await.community_id,
            9,
            "negative TTL expired"
        );
        assert_eq!(fake.calls.load(Ordering::SeqCst), 2);
        tokio::time::advance(Duration::from_secs(31)).await;
        r.resolve("discord", "1").await;
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
        r.resolve("discord", "a").await;
        r.resolve("discord", "b").await;
        r.resolve("discord", "a").await; // a is now most recent
        r.resolve("discord", "c").await; // evicts b
        assert_eq!(fake.calls.load(Ordering::SeqCst), 3);
        r.resolve("discord", "a").await;
        assert_eq!(fake.calls.load(Ordering::SeqCst), 3, "a survived");
        r.resolve("discord", "b").await;
        assert_eq!(fake.calls.load(Ordering::SeqCst), 4, "b was evicted");
    }

    #[tokio::test(start_paused = true)]
    async fn inconsistent_upstream_data_fails_closed() {
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
            assert_eq!((got.tenant_id, got.community_id), (7, 0), "{bad:?}");
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
}
