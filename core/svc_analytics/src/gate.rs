//! Exclusion and tier gates: bounded TTL caches that are SEEDED at startup and
//! LAZY-LOADED on a miss (never only invalidated -- an empty decision cache would
//! silently fail-closed every check, or worse, fail-open on a stale negative).
//!
//! Fail-closed contract: if the exclusion lookup is unavailable for any user that is not
//! covered by a fresh cache entry, [`ExclusionCache::resolve`] returns an error and the
//! caller must NOT write for that batch. The tier cache fails closed to Free (basic counts
//! only), never open to Premium.

use std::collections::{HashMap, HashSet};
use std::hash::Hash;
use std::sync::Mutex;
use std::time::Duration;

use tokio::time::Instant;
use uuid::Uuid;

use crate::db::{AnalyticsDb, DbFault};

/// A bounded boolean-valued TTL cache.
pub struct TtlCache<K> {
    map: Mutex<HashMap<K, (bool, Instant)>>,
    ttl: Duration,
    cap: usize,
}

impl<K: Hash + Eq + Copy> TtlCache<K> {
    /// New cache holding at most `cap` entries, each valid for `ttl`.
    pub fn new(ttl: Duration, cap: usize) -> Self {
        Self {
            map: Mutex::new(HashMap::new()),
            ttl,
            cap,
        }
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, HashMap<K, (bool, Instant)>> {
        self.map
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    /// The cached value when present and not expired.
    pub fn fresh(&self, k: &K) -> Option<bool> {
        let now = Instant::now();
        self.lock()
            .get(k)
            .filter(|(_, at)| now.duration_since(*at) < self.ttl)
            .map(|(v, _)| *v)
    }

    /// Inserts `(key, value)` pairs, evicting to stay within the bound
    /// (expired first, then negatives, then arbitrary entries).
    pub fn put_many(&self, items: impl IntoIterator<Item = (K, bool)>) {
        let now = Instant::now();
        let mut m = self.lock();
        for (k, v) in items {
            m.insert(k, (v, now));
        }
        if m.len() > self.cap {
            let ttl = self.ttl;
            m.retain(|_, (_, at)| now.duration_since(*at) < ttl);
        }
        if m.len() > self.cap {
            m.retain(|_, (v, _)| *v);
        }
        while m.len() > self.cap {
            let Some(k) = m.keys().next().copied() else {
                break;
            };
            m.remove(&k);
        }
    }

    /// Number of entries held.
    pub fn len(&self) -> usize {
        self.lock().len()
    }

    /// Whether the cache is empty.
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

/// Cache hit/miss counts for one resolve call.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct CacheStats {
    /// Keys answered from a fresh entry.
    pub hits: u64,
    /// Keys that needed a database lookup.
    pub misses: u64,
}

/// Cache of `data_use_exclusions` membership by user token.
pub struct ExclusionCache {
    inner: TtlCache<Uuid>,
}

impl ExclusionCache {
    /// New cache.
    pub fn new(ttl: Duration, cap: usize) -> Self {
        Self {
            inner: TtlCache::new(ttl, cap),
        }
    }

    /// Seeds the cache with up to `cap` excluded tokens (positives).
    ///
    /// # Errors
    /// The database fault; the caller retries (the consumer does not read before a seed).
    pub async fn seed<D: AnalyticsDb>(&self, db: &D, cap: usize) -> Result<usize, DbFault> {
        let ids = db.load_exclusions(cap).await?;
        let n = ids.len();
        self.inner.put_many(ids.into_iter().map(|u| (u, true)));
        Ok(n)
    }

    /// Returns the subset of `users` that is excluded, consulting the cache first and
    /// lazily loading misses.
    ///
    /// # Errors
    /// The lookup fault when any user lacks a fresh answer: the caller MUST fail closed.
    pub async fn resolve<D: AnalyticsDb>(
        &self,
        db: &D,
        users: &[Uuid],
    ) -> Result<(HashSet<Uuid>, CacheStats), DbFault> {
        let mut excluded = HashSet::new();
        let mut misses = Vec::new();
        let mut stats = CacheStats::default();
        for u in users {
            match self.inner.fresh(u) {
                Some(true) => {
                    stats.hits += 1;
                    excluded.insert(*u);
                }
                Some(false) => stats.hits += 1,
                None => {
                    stats.misses += 1;
                    misses.push(*u);
                }
            }
        }
        if !misses.is_empty() {
            let found: HashSet<Uuid> = db.lookup_exclusions(&misses).await?.into_iter().collect();
            self.inner
                .put_many(misses.iter().map(|u| (*u, found.contains(u))));
            excluded.extend(found);
        }
        Ok((excluded, stats))
    }

    /// Entries currently held.
    pub fn len(&self) -> usize {
        self.inner.len()
    }

    /// Whether nothing is cached.
    pub fn is_empty(&self) -> bool {
        self.inner.is_empty()
    }
}

/// Cache of "is this community Premium" by numeric community id.
pub struct TierCache {
    inner: TtlCache<i32>,
}

impl TierCache {
    /// New cache.
    pub fn new(ttl: Duration, cap: usize) -> Self {
        Self {
            inner: TtlCache::new(ttl, cap),
        }
    }

    /// Premium subset of `communities`. A lookup failure yields `Err` alongside the Premium set
    /// proven by fresh cache entries; the caller treats every unresolved community as Free.
    pub async fn resolve<D: AnalyticsDb>(
        &self,
        db: &D,
        communities: &[i32],
    ) -> (HashSet<i32>, CacheStats, Option<DbFault>) {
        let mut premium = HashSet::new();
        let mut misses = Vec::new();
        let mut stats = CacheStats::default();
        for c in communities {
            match self.inner.fresh(c) {
                Some(p) => {
                    stats.hits += 1;
                    if p {
                        premium.insert(*c);
                    }
                }
                None => {
                    stats.misses += 1;
                    misses.push(*c);
                }
            }
        }
        let mut fault = None;
        if !misses.is_empty() {
            match db.premium_communities(&misses).await {
                Ok(found) => {
                    let found: HashSet<i32> = found.into_iter().collect();
                    self.inner
                        .put_many(misses.iter().map(|c| (*c, found.contains(c))));
                    premium.extend(found);
                }
                Err(e) => fault = Some(e),
            }
        }
        (premium, stats, fault)
    }
}
