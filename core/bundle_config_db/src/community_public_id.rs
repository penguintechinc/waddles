//! Read-replica lookup of a community's PUBLIC UUID (`communities.public_id`) for the bundle
//! context.
//!
//! The data plane keys everything on the NUMERIC community id (never exposed outside the
//! platform: ids are enumerable). A bundle that builds a USER-FACING link (the webui only accepts
//! the community's public UUID in URLs) therefore needs the public id from the host. The stages
//! (svc-process, svc-action) call [`CommunityPublicIds::lookup`] while answering `get-context` and
//! fold the result into the guest-visible `config-json` as the host-reserved
//! `community_public_id` key -- the same additive channel `web_base_url` already uses, so the
//! FROZEN WIT `context-v2.bundle-context` record is untouched and every already-compiled bundle
//! keeps instantiating (an old bundle simply never reads the key).
//!
//! **Read path.** hub-api is the control-plane writer; this reads the read-only REPLICA
//! (`DATABASE_REPLICA_URL`, never the primary) under the stage's own column-scoped role. One
//! statement, [`PUBLIC_ID_SQL`], filtered by BOTH the verified `tenant_id` and `community_id`, so
//! a mismatched pair resolves to nothing rather than another tenant's UUID.
//!
//! **Cache.** Bounded (capacity-capped, least-recently-used eviction) with a TTL: a found UUID is
//! kept for [`CacheConfig::ttl`], an absent row for the shorter [`CacheConfig::negative_ttl`].
//! No lock is held across an `.await`.
//!
//! **Failure behaviour (never blocks, never fabricates).** A miss, a database error, a timeout or
//! a malformed value yields `None` -- the field is simply absent and the bundle must omit the
//! link. A database error is answered from a stale cached UUID when one exists (a community's
//! public id never changes), otherwise `None`; after an error further lookups fail fast for
//! [`CacheConfig::failure_backoff`] so an outage costs one timeout per window, not one per
//! invocation. Every outcome bumps `waddles_community_public_id_lookups{outcome}`; failures log a
//! rate-limited WARN carrying only the numeric ids and the error kind -- never the DSN, never the
//! UUID.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use async_trait::async_trait;
use opentelemetry::metrics::Counter;
use opentelemetry::KeyValue;
use sea_orm::{ConnectionTrait, DbBackend, Statement};

use crate::{error_kind, redact, DbConfigSource, DB_TIMEOUT, REPLICA_URL_ENV};

/// The host-reserved `config-json` key carrying the community's public UUID.
pub const CONTEXT_KEY: &str = "community_public_id";

/// The single lookup statement: both ids are bound (numeric only), the verified pair must match.
pub const PUBLIC_ID_SQL: &str = "\
SELECT public_id::text FROM communities \
WHERE id = $1::bigint AND tenant_id = $2::bigint LIMIT 1";

/// Minimum spacing between failure WARN lines.
const WARN_EVERY: Duration = Duration::from_secs(60);

/// Where a community's public id comes from (the replica in production, a fake in tests).
#[async_trait]
pub trait PublicIdSource: Send + Sync {
    /// The raw `public_id` of `(tenant_id, community_id)`; `Ok(None)` when no such row exists.
    /// Errors carry a short kind string only (never a DSN).
    async fn fetch(&self, tenant_id: i64, community_id: i64) -> Result<Option<String>, String>;
}

/// Cache tuning. Defaults suit production; tests shrink the durations.
#[derive(Clone, Debug)]
pub struct CacheConfig {
    /// Maximum cached communities; the least recently used entry is evicted beyond it.
    pub capacity: usize,
    /// Lifetime of a found UUID.
    pub ttl: Duration,
    /// Lifetime of an "absent" answer (shorter: a community can be created after a miss).
    pub negative_ttl: Duration,
    /// Fast-fail window after a source error.
    pub failure_backoff: Duration,
    /// Hard bound on one lookup so a slow replica can never hold a bundle invocation.
    pub lookup_timeout: Duration,
}

impl Default for CacheConfig {
    fn default() -> Self {
        Self {
            capacity: 4096,
            ttl: Duration::from_secs(300),
            negative_ttl: Duration::from_secs(30),
            failure_backoff: Duration::from_secs(2),
            lookup_timeout: Duration::from_millis(1500),
        }
    }
}

/// One cached answer.
struct Entry {
    value: Option<String>,
    stored: Instant,
    used: u64,
}

/// The mutable cache state (guarded by a std mutex, never held across an await).
#[derive(Default)]
struct Inner {
    map: HashMap<(i64, i64), Entry>,
    tick: u64,
    fail_until: Option<Instant>,
}

/// Bounded LRU + TTL cache over a [`PublicIdSource`].
pub struct CommunityPublicIds {
    source: Arc<dyn PublicIdSource>,
    cfg: CacheConfig,
    inner: Mutex<Inner>,
    last_warn: AtomicU64,
}

/// The process-wide lookup counter.
fn lookups() -> &'static Counter<u64> {
    static C: OnceLock<Counter<u64>> = OnceLock::new();
    C.get_or_init(|| {
        opentelemetry::global::meter("bundle_config_db")
            .u64_counter("waddles_community_public_id_lookups")
            .with_description(
                "Community public-id lookups for the bundle context by outcome \
                 (hit|loaded|absent|stale|error|backoff|invalid).",
            )
            .build()
    })
}

/// Records one lookup outcome.
fn note(outcome: &'static str) {
    lookups().add(1, &[KeyValue::new("outcome", outcome)]);
}

/// Returns the canonical lowercase hyphenated form of a UUID, or `None` when `raw` is not one.
#[must_use]
pub fn normalize_uuid(raw: &str) -> Option<String> {
    let s = raw.trim();
    if s.len() != 36 {
        return None;
    }
    let ok = s.bytes().enumerate().all(|(i, b)| match i {
        8 | 13 | 18 | 23 => b == b'-',
        _ => b.is_ascii_hexdigit(),
    });
    ok.then(|| s.to_ascii_lowercase())
}

impl CommunityPublicIds {
    /// A cache over `source`.
    #[must_use]
    pub fn new(source: Arc<dyn PublicIdSource>, cfg: CacheConfig) -> Self {
        Self {
            source,
            cfg,
            inner: Mutex::new(Inner::default()),
            last_warn: AtomicU64::new(0),
        }
    }

    /// Number of cached communities (tests / diagnostics).
    #[must_use]
    pub fn len(&self) -> usize {
        self.inner.lock().map(|g| g.map.len()).unwrap_or(0)
    }

    /// True when nothing is cached.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// The community's public UUID, or `None` when it cannot be established (never fabricated,
    /// never blocks past `lookup_timeout`, never an error).
    pub async fn lookup(&self, tenant_id: i64, community_id: i64) -> Option<String> {
        if tenant_id < 1 || community_id < 1 {
            return None;
        }
        let key = (tenant_id, community_id);
        let now = Instant::now();
        let mut stale: Option<String> = None;
        if let Ok(mut g) = self.inner.lock() {
            g.tick += 1;
            let tick = g.tick;
            if let Some(e) = g.map.get_mut(&key) {
                e.used = tick;
                let ttl = if e.value.is_some() {
                    self.cfg.ttl
                } else {
                    self.cfg.negative_ttl
                };
                if now.duration_since(e.stored) < ttl {
                    note("hit");
                    return e.value.clone();
                }
                stale = e.value.clone();
            }
            if g.fail_until.is_some_and(|until| now < until) {
                note("backoff");
                return stale;
            }
        }
        let fetched = match tokio::time::timeout(
            self.cfg.lookup_timeout,
            self.source.fetch(tenant_id, community_id),
        )
        .await
        {
            Ok(result) => result,
            Err(_elapsed) => {
                // Fail closed: a timed-out lookup is an error outcome (counted, logged with the
                // numeric ids only), answered from a stale UUID if cached, else absent.
                tracing::error!(
                    tenant_id,
                    community_id,
                    timeout_ms = self.cfg.lookup_timeout.as_millis(),
                    "community public_id lookup timed out"
                );
                Err("timeout".to_string())
            }
        };
        match fetched {
            Ok(raw) => {
                let value = match raw {
                    None => None,
                    Some(r) => match normalize_uuid(&r) {
                        Some(u) => Some(u),
                        None => {
                            note("invalid");
                            self.warn(tenant_id, community_id, "malformed public_id");
                            return None;
                        }
                    },
                };
                note(if value.is_some() { "loaded" } else { "absent" });
                self.store(key, value.clone());
                value
            }
            Err(kind) => {
                note(if stale.is_some() { "stale" } else { "error" });
                if let Ok(mut g) = self.inner.lock() {
                    g.fail_until = Some(Instant::now() + self.cfg.failure_backoff);
                }
                self.warn(tenant_id, community_id, &kind);
                stale
            }
        }
    }

    /// Inserts `value`, evicting the least recently used entry when over capacity.
    fn store(&self, key: (i64, i64), value: Option<String>) {
        let Ok(mut g) = self.inner.lock() else {
            return;
        };
        g.tick += 1;
        let used = g.tick;
        g.fail_until = None;
        g.map.insert(
            key,
            Entry {
                value,
                stored: Instant::now(),
                used,
            },
        );
        while g.map.len() > self.cfg.capacity.max(1) {
            let oldest = g.map.iter().min_by_key(|(_, e)| e.used).map(|(k, _)| *k);
            match oldest {
                Some(k) => {
                    g.map.remove(&k);
                }
                None => break,
            }
        }
    }

    /// Rate-limited WARN: numeric ids and the error kind only.
    fn warn(&self, tenant_id: i64, community_id: i64, kind: &str) {
        static ORIGIN: OnceLock<Instant> = OnceLock::new();
        let now = ORIGIN.get_or_init(Instant::now).elapsed().as_secs() + 1;
        let last = self.last_warn.load(Ordering::Relaxed);
        if (last == 0 || now.saturating_sub(last) >= WARN_EVERY.as_secs())
            && self
                .last_warn
                .compare_exchange(last, now, Ordering::Relaxed, Ordering::Relaxed)
                .is_ok()
        {
            tracing::warn!(
                tenant_id,
                community_id,
                kind,
                "community public_id lookup failed; bundle context omits {CONTEXT_KEY}"
            );
        }
    }
}

/// [`PublicIdSource`] over the read replica.
pub struct PgPublicIdSource {
    db: DbConfigSource,
    url: String,
}

impl PgPublicIdSource {
    /// A source over `replica_url` (credential-bearing, never logged). Connects lazily.
    #[must_use]
    pub fn new(replica_url: String) -> Self {
        Self {
            db: DbConfigSource::new(replica_url.clone(), DB_TIMEOUT),
            url: replica_url,
        }
    }
}

#[async_trait]
impl PublicIdSource for PgPublicIdSource {
    async fn fetch(&self, tenant_id: i64, community_id: i64) -> Result<Option<String>, String> {
        let conn = self.db.connection().await?;
        let row = conn
            .query_one_raw(Statement::from_sql_and_values(
                DbBackend::Postgres,
                PUBLIC_ID_SQL,
                [community_id.into(), tenant_id.into()],
            ))
            .await
            .map_err(|e| {
                format!(
                    "query: {}: {}",
                    error_kind(&e),
                    redact(&e.to_string(), &self.url)
                )
            })?;
        // A NULL `public_id` (row not backfilled) is "unknown", never an error.
        row.map(|r| {
            r.try_get_by_index::<Option<String>>(0)
                .map_err(|e| format!("decode public_id: {}", error_kind(&e)))
        })
        .transpose()
        .map(Option::flatten)
    }
}

/// The process-wide cache the stages consult (installed once at startup).
static GLOBAL: OnceLock<Arc<CommunityPublicIds>> = OnceLock::new();

/// The installed process-wide cache, if any.
#[must_use]
pub fn global() -> Option<Arc<CommunityPublicIds>> {
    GLOBAL.get().cloned()
}

/// Installs the process-wide cache over `source` (first install wins).
pub fn install_global(
    source: Arc<dyn PublicIdSource>,
    cfg: CacheConfig,
) -> Arc<CommunityPublicIds> {
    let cache = Arc::new(CommunityPublicIds::new(source, cfg));
    Arc::clone(GLOBAL.get_or_init(|| cache))
}

/// Installs the process-wide cache from `DATABASE_REPLICA_URL` ONLY (never the primary). With it
/// unset an ERROR is logged once and no cache is installed: bundles then get no
/// `community_public_id` and must omit user-facing community links.
pub fn install_global_from_env() -> Option<Arc<CommunityPublicIds>> {
    install_global_from_value(std::env::var(REPLICA_URL_ENV).ok())
}

/// [`install_global_from_env`] over an explicit value (tests).
pub fn install_global_from_value(replica_url: Option<String>) -> Option<Arc<CommunityPublicIds>> {
    match replica_url.filter(|u| !u.trim().is_empty()) {
        Some(url) => Some(install_global(
            Arc::new(PgPublicIdSource::new(url)),
            CacheConfig::default(),
        )),
        None => {
            tracing::error!(
                replica_env = REPLICA_URL_ENV,
                "community public_id: replica DSN unset; bundle contexts carry no \
                 {CONTEXT_KEY} (this reader never falls back to the primary database)"
            );
            None
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicUsize;

    const U1: &str = "0b6b0a52-5f0e-4f6a-9a39-1b6a3d6d9f01";
    const U2: &str = "7f3c2d10-aaaa-4bbb-8ccc-0123456789ab";

    /// Scripted source: answers per community id, counts calls, can be switched to fail.
    struct Fake {
        rows: Mutex<HashMap<i64, String>>,
        calls: AtomicUsize,
        fail: Mutex<bool>,
        delay: Duration,
    }

    impl Fake {
        fn new(rows: &[(i64, &str)]) -> Arc<Self> {
            Arc::new(Self {
                rows: Mutex::new(rows.iter().map(|(k, v)| (*k, (*v).to_string())).collect()),
                calls: AtomicUsize::new(0),
                fail: Mutex::new(false),
                delay: Duration::ZERO,
            })
        }
        fn calls(&self) -> usize {
            self.calls.load(Ordering::SeqCst)
        }
    }

    #[async_trait]
    impl PublicIdSource for Fake {
        async fn fetch(&self, _t: i64, c: i64) -> Result<Option<String>, String> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            if !self.delay.is_zero() {
                tokio::time::sleep(self.delay).await;
            }
            if *self.fail.lock().unwrap() {
                return Err("connection".into());
            }
            Ok(self.rows.lock().unwrap().get(&c).cloned())
        }
    }

    fn cfg() -> CacheConfig {
        CacheConfig {
            capacity: 2,
            ttl: Duration::from_millis(60),
            negative_ttl: Duration::from_millis(20),
            failure_backoff: Duration::from_millis(40),
            lookup_timeout: Duration::from_millis(200),
        }
    }

    #[test]
    fn normalize_uuid_accepts_only_canonical_uuids() {
        assert_eq!(
            normalize_uuid(&format!(" {} ", U1.to_uppercase())).as_deref(),
            Some(U1)
        );
        for bad in [
            "",
            "42",
            "0b6b0a52-5f0e-4f6a-9a39-1b6a3d6d9f0",
            "0b6b0a525f0e4f6a9a391b6a3d6d9f01xxxx",
            "0b6b0a52-5f0e-4f6a-9a39-1b6a3d6d9fzz",
            "0b6b0a52_5f0e_4f6a_9a39_1b6a3d6d9f01",
            "../../etc/passwd/../../../../../../x",
        ] {
            assert_eq!(normalize_uuid(bad), None, "{bad:?}");
        }
    }

    #[test]
    fn sql_binds_both_ids_and_filters_on_both() {
        assert!(PUBLIC_ID_SQL.contains("id = $1::bigint"));
        assert!(PUBLIC_ID_SQL.contains("tenant_id = $2::bigint"));
        assert!(PUBLIC_ID_SQL.contains("public_id::text"));
    }

    #[tokio::test]
    async fn loads_once_then_serves_from_cache_and_normalises() {
        let f = Fake::new(&[(7, &U1.to_uppercase())]);
        let c = CommunityPublicIds::new(f.clone(), cfg());
        assert_eq!(c.lookup(1, 7).await.as_deref(), Some(U1));
        assert_eq!(c.lookup(1, 7).await.as_deref(), Some(U1));
        assert_eq!(f.calls(), 1);
    }

    #[tokio::test]
    async fn invalid_scope_never_queries() {
        let f = Fake::new(&[(7, U1)]);
        let c = CommunityPublicIds::new(f.clone(), cfg());
        for (t, cm) in [(0, 7), (1, 0), (-1, 7), (1, -5)] {
            assert_eq!(c.lookup(t, cm).await, None);
        }
        assert_eq!(f.calls(), 0);
    }

    #[tokio::test]
    async fn absent_row_is_none_negatively_cached_then_retried() {
        let f = Fake::new(&[]);
        let c = CommunityPublicIds::new(f.clone(), cfg());
        assert_eq!(c.lookup(1, 9).await, None);
        assert_eq!(c.lookup(1, 9).await, None);
        assert_eq!(f.calls(), 1);
        f.rows.lock().unwrap().insert(9, U2.to_string());
        tokio::time::sleep(Duration::from_millis(30)).await;
        assert_eq!(c.lookup(1, 9).await.as_deref(), Some(U2));
        assert_eq!(f.calls(), 2);
    }

    #[tokio::test]
    async fn malformed_value_is_absent_never_fabricated_or_cached() {
        let f = Fake::new(&[(3, "42")]);
        let c = CommunityPublicIds::new(f.clone(), cfg());
        assert_eq!(c.lookup(1, 3).await, None);
        assert!(c.is_empty());
    }

    #[tokio::test]
    async fn error_without_cache_is_none_and_backs_off() {
        let f = Fake::new(&[(7, U1)]);
        *f.fail.lock().unwrap() = true;
        let c = CommunityPublicIds::new(f.clone(), cfg());
        assert_eq!(c.lookup(1, 7).await, None);
        assert_eq!(c.lookup(1, 7).await, None);
        assert_eq!(
            f.calls(),
            1,
            "second call fails fast inside the backoff window"
        );
        *f.fail.lock().unwrap() = false;
        tokio::time::sleep(Duration::from_millis(50)).await;
        assert_eq!(c.lookup(1, 7).await.as_deref(), Some(U1));
    }

    #[tokio::test]
    async fn expired_entry_is_served_stale_when_the_source_fails() {
        let f = Fake::new(&[(7, U1)]);
        let c = CommunityPublicIds::new(f.clone(), cfg());
        assert_eq!(c.lookup(1, 7).await.as_deref(), Some(U1));
        tokio::time::sleep(Duration::from_millis(70)).await;
        *f.fail.lock().unwrap() = true;
        assert_eq!(c.lookup(1, 7).await.as_deref(), Some(U1));
        // Inside the backoff window the stale value is still served without a source call.
        let n = f.calls();
        assert_eq!(c.lookup(1, 7).await.as_deref(), Some(U1));
        assert_eq!(f.calls(), n);
    }

    #[tokio::test]
    async fn slow_source_times_out_to_none() {
        let f = Arc::new(Fake {
            rows: Mutex::new(HashMap::from([(7, U1.to_string())])),
            calls: AtomicUsize::new(0),
            fail: Mutex::new(false),
            delay: Duration::from_millis(400),
        });
        let c = CommunityPublicIds::new(f.clone(), cfg());
        let started = Instant::now();
        assert_eq!(c.lookup(1, 7).await, None);
        assert!(started.elapsed() < Duration::from_millis(350));
        // Fail closed: nothing cached, and the timeout armed the failure backoff so the next
        // lookup fails fast without another source call.
        assert!(c.is_empty());
        let n = f.calls();
        assert_eq!(c.lookup(1, 7).await, None);
        assert_eq!(f.calls(), n);
    }

    #[tokio::test]
    async fn capacity_evicts_the_least_recently_used_entry() {
        let f = Fake::new(&[
            (1, U1),
            (2, U2),
            (3, "11111111-2222-4333-8444-555555555555"),
        ]);
        let long = CacheConfig {
            ttl: Duration::from_secs(60),
            ..cfg()
        };
        let c = CommunityPublicIds::new(f.clone(), long);
        c.lookup(1, 1).await;
        c.lookup(1, 2).await;
        c.lookup(1, 1).await; // touch 1 => 2 is now the LRU
        c.lookup(1, 3).await; // evicts 2
        assert_eq!(c.len(), 2);
        let n = f.calls();
        c.lookup(1, 1).await;
        assert_eq!(f.calls(), n, "1 stayed cached");
        c.lookup(1, 2).await;
        assert_eq!(f.calls(), n + 1, "2 was evicted and reloaded");
    }

    #[test]
    fn install_from_blank_value_installs_nothing() {
        assert!(install_global_from_value(None).is_none());
        assert!(install_global_from_value(Some("  ".into())).is_none());
    }
}
