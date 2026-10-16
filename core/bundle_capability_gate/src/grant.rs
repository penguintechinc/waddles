//! Grant storage read path (spec SS4): `GrantSnapshot` is the sync,
//! zero-I/O, hot-path trait `authorize()` reads on every call (spec SS5.5:
//! "one hashmap read... no allocation on the hot path"); [`GrantLoader`] is
//! the async, I/O-performing trait a real RO-replica implementation (a
//! later task, once the migration in spec SS12 Phase 0 lands) fulfills;
//! [`GrantCache`] bridges the two -- a sync-readable in-memory map kept
//! fresh by whichever of push-invalidation or poll-fallback (spec SS4) calls
//! [`GrantCache::refresh`]/[`GrantCache::invalidate`].
//!
//! This crate has no dependency on the real grant-table schema's storage
//! engine (SeaORM/Postgres) -- that schema is spec SS4's `app_permission_
//! requests`/`app_tenant_permission_restrictions`/`community_permission_
//! grants`/`app_permission_grant_versions` tables, owned by another agent's
//! migration, never created here (task instruction). [`InMemoryGrantLoader`]
//! is this crate's own test double standing in for that eventual RO reader.

use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex, RwLock};
use std::time::{Duration, Instant};

use crate::scope::GrantScopeKey;

/// One community's/tenant's actual grant for a single permission id --
/// `params` is the community's own bound within the global-approved ceiling
/// (spec SS4: e.g. a tighter `delta_max`).
#[derive(Clone, Debug, PartialEq)]
pub struct GrantedPermission {
    pub permission_id: String,
    pub params: serde_json::Value,
}

/// The full grant set for one `(tenant, community, app_id, app_version)`
/// (spec SS4). `permission_snapshot_hash` is the version-pinning value (spec
/// SS4: "the single value the data plane compares to know whether its
/// cached grant set for a given active version is current") -- opaque to
/// this crate, just carried through for the loader/cache layer's own
/// staleness bookkeeping.
#[derive(Clone, Debug, PartialEq, Default)]
pub struct GrantSet {
    pub permission_snapshot_hash: String,
    pub grants: HashMap<String, GrantedPermission>,
}

impl GrantSet {
    pub fn get(&self, canonical_permission_id: &str) -> Option<&GrantedPermission> {
        self.grants.get(canonical_permission_id)
    }
}

/// The sync, zero-I/O read path `authorize()` uses on every call (spec
/// SS5.5). A missing entry (`None`) is fail-closed: the caller must treat it
/// identically to "not granted" -- there is no default-allow shape anywhere
/// in this trait.
pub trait GrantSnapshot: Send + Sync {
    fn current(&self, key: &GrantScopeKey) -> Option<Arc<GrantSet>>;

    /// Lazy-load hook, called from the async host-call entry BEFORE the sync
    /// `authorize()`: a snapshot backed by a loader populates a cold `key`
    /// here so a valid grant that was never refreshed is not denied as
    /// `not_granted`. The default is a no-op (fixed-map snapshots have
    /// nothing to load). Never weakens fail-closed: a key the loader cannot
    /// produce stays absent and `current()` still returns `None`.
    fn ensure_loaded<'a>(
        &'a self,
        _key: &'a GrantScopeKey,
    ) -> Pin<Box<dyn Future<Output = ()> + Send + 'a>> {
        Box::pin(async {})
    }
}

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum GrantLoadError {
    #[error("grant loader backend error: {0}")]
    Backend(String),
}

/// The RO-replica loader boundary (spec SS4: "the data plane... holds a
/// read-only role against a read replica"). Object-safe (a manually-boxed
/// future rather than `async fn` in a trait, matching `core/svc_process`'s
/// `CapabilityHandler` pattern) so `GrantCache` can hold `Arc<dyn
/// GrantLoader>` without an `async-trait` dependency. A real implementation
/// (SeaORM against the migration in spec SS12 Phase 0, once it lands) is a
/// later task -- this crate ships only the trait and [`InMemoryGrantLoader`].
pub trait GrantLoader: Send + Sync {
    fn load<'a>(
        &'a self,
        key: &'a GrantScopeKey,
    ) -> Pin<Box<dyn Future<Output = Result<Option<GrantSet>, GrantLoadError>> + Send + 'a>>;

    /// Every `(tenant, community, app, version)` currently active, used to
    /// prime the cache at startup ([`GrantCache::seed`]). The default is
    /// empty (no seeding; lazy-load still covers cold keys).
    fn list_active_keys<'a>(
        &'a self,
    ) -> Pin<Box<dyn Future<Output = Result<Vec<GrantScopeKey>, GrantLoadError>> + Send + 'a>> {
        Box::pin(async { Ok(Vec::new()) })
    }
}

/// A settable, in-memory [`GrantLoader`] for tests -- simulates the RO
/// replica without any real database.
#[derive(Default)]
pub struct InMemoryGrantLoader {
    rows: RwLock<HashMap<GrantScopeKey, GrantSet>>,
}

impl InMemoryGrantLoader {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn set(&self, key: GrantScopeKey, grants: GrantSet) {
        self.rows
            .write()
            .expect("lock poisoned")
            .insert(key, grants);
    }

    pub fn remove(&self, key: &GrantScopeKey) {
        self.rows.write().expect("lock poisoned").remove(key);
    }

    #[cfg(test)]
    fn rows_snapshot(&self, key: &GrantScopeKey) -> Option<GrantSet> {
        self.rows.read().expect("lock poisoned").get(key).cloned()
    }
}

impl GrantLoader for InMemoryGrantLoader {
    fn load<'a>(
        &'a self,
        key: &'a GrantScopeKey,
    ) -> Pin<Box<dyn Future<Output = Result<Option<GrantSet>, GrantLoadError>> + Send + 'a>> {
        let result = self.rows.read().expect("lock poisoned").get(key).cloned();
        Box::pin(async move { Ok(result) })
    }
}

/// A [`GrantSnapshot`] backed directly by a fixed map -- for tests that
/// don't need the loader/refresh machinery at all, just a fixed answer to
/// `current()`.
#[derive(Default)]
pub struct InMemoryGrantSnapshot {
    rows: RwLock<HashMap<GrantScopeKey, Arc<GrantSet>>>,
}

impl InMemoryGrantSnapshot {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn set(&self, key: GrantScopeKey, grants: GrantSet) {
        self.rows
            .write()
            .expect("lock poisoned")
            .insert(key, Arc::new(grants));
    }

    pub fn invalidate(&self, key: &GrantScopeKey) {
        self.rows.write().expect("lock poisoned").remove(key);
    }
}

impl GrantSnapshot for InMemoryGrantSnapshot {
    fn current(&self, key: &GrantScopeKey) -> Option<Arc<GrantSet>> {
        self.rows.read().expect("lock poisoned").get(key).cloned()
    }
}

/// Bridges [`GrantLoader`] (async, I/O) to [`GrantSnapshot`] (sync, zero-I/O)
/// -- the shape spec SS4 describes: "`svc_process`/`svc_action` subscribe
/// and, on receipt, re-fetch just that `(tenant, community, app_id)`'s grant
/// set and refresh the in-memory `GrantSnapshot`... zero per-invoke DB round
/// trips regardless of which path refreshed it." Wiring the actual Valkey
/// pub/sub subscriber that calls [`GrantCache::refresh`] on a push
/// notification (and the `BUNDLE_CONFIG_POLL_SECONDS` fallback poll that
/// calls it periodically) is the data-plane integration task (spec SS12
/// Phase 4), not this crate -- this type only provides the cache mechanics
/// both paths would call into.
pub struct GrantCache<L: GrantLoader> {
    loader: Arc<L>,
    memo: RwLock<HashMap<GrantScopeKey, Arc<GrantSet>>>,
    /// Keys whose last lazy load found nothing (or failed), with the time of
    /// that attempt -- a repeated miss inside [`NEGATIVE_TTL`] is answered
    /// without hitting the loader again, so denied keys cannot hammer it.
    negative: Mutex<HashMap<GrantScopeKey, Instant>>,
    /// Per-key invalidation generation. `invalidate` bumps it; `refresh`
    /// captures it before its load and applies the result only if it is
    /// unchanged, so an in-flight load can never overwrite a newer
    /// invalidation (revocation race). Always mutated while holding the
    /// `memo` write lock, which makes the compare-and-set atomic.
    generations: Mutex<HashMap<GrantScopeKey, u64>>,
}

/// How long a lazy-load miss is remembered before the loader is retried.
const NEGATIVE_TTL: Duration = Duration::from_secs(5);

impl<L: GrantLoader> GrantCache<L> {
    pub fn new(loader: Arc<L>) -> Self {
        Self {
            loader,
            memo: RwLock::new(HashMap::new()),
            negative: Mutex::new(HashMap::new()),
            generations: Mutex::new(HashMap::new()),
        }
    }

    /// Loads `key` if it is cold (not resident and not in the negative
    /// window) -- the lazy-load half of the fix: without it nothing ever put
    /// a key into the memo, so every non-platform permission was denied.
    pub async fn ensure_loaded(&self, key: &GrantScopeKey) {
        if self.memo.read().expect("lock poisoned").contains_key(key) {
            return;
        }
        if let Some(at) = self.negative.lock().expect("lock poisoned").get(key) {
            if at.elapsed() < NEGATIVE_TTL {
                return;
            }
        }
        // Concurrent first-use calls for the same key may each load once;
        // the load is an idempotent read and the result converges.
        let resident = match self.refresh(key).await {
            Ok(()) => self.memo.read().expect("lock poisoned").contains_key(key),
            Err(err) => {
                tracing::warn!(error = %err, app_id = %key.app_id, "lazy grant load failed");
                false
            }
        };
        let mut negative = self.negative.lock().expect("lock poisoned");
        // A resident result already cleared any tombstone inside `refresh`'s
        // compare-and-set; never clear one here (it may be newer).
        if !resident {
            negative.insert(key.clone(), Instant::now());
        }
    }

    /// Primes the cache for every active key the loader reports -- run once
    /// at startup so the first message is not racing the lazy load. Returns
    /// how many keys were examined.
    pub async fn seed(&self) -> Result<usize, GrantLoadError> {
        let keys = self.loader.list_active_keys().await?;
        for key in &keys {
            self.ensure_loaded(key).await;
        }
        Ok(keys.len())
    }

    /// Re-fetches `key` from the loader and updates the sync-readable memo --
    /// call on a push-invalidation notification or a poll-fallback tick
    /// (spec SS4). A loader answer of `None` (revoked/deactivated) removes
    /// the memo entry, so the very next `current()` call fails closed.
    pub async fn refresh(&self, key: &GrantScopeKey) -> Result<(), GrantLoadError> {
        // Capture the generation BEFORE the load; no lock is held across the
        // await below.
        let gen_before = self.generation(key);
        let loaded = self.loader.load(key).await?;
        let mut memo = self.memo.write().expect("lock poisoned");
        if self.generation(key) != gen_before {
            // An `invalidate` (revocation) landed during the load: the
            // result is stale, so discard it and leave the key invalidated
            // and tombstoned -- the next lazy load/refresh reads fresh.
            return Ok(());
        }
        match loaded {
            Some(grants) => {
                memo.insert(key.clone(), Arc::new(grants));
                self.negative.lock().expect("lock poisoned").remove(key);
            }
            None => {
                memo.remove(key);
            }
        }
        Ok(())
    }

    fn generation(&self, key: &GrantScopeKey) -> u64 {
        self.generations
            .lock()
            .expect("lock poisoned")
            .get(key)
            .copied()
            .unwrap_or(0)
    }

    /// Drops a memo entry without re-fetching -- the immediate half of
    /// push-invalidation (spec SS4/SS5.3: a revocation "blocks every
    /// subsequent host call" before any refresh completes). A caller
    /// typically follows this with [`GrantCache::refresh`] once the fetch
    /// completes; between the two, `current()` fails closed exactly like a
    /// cache miss.
    pub fn invalidate(&self, key: &GrantScopeKey) {
        // Hold the memo write lock while bumping the generation so a
        // concurrent `refresh` compare-and-set cannot interleave.
        let mut memo = self.memo.write().expect("lock poisoned");
        memo.remove(key);
        *self
            .generations
            .lock()
            .expect("lock poisoned")
            .entry(key.clone())
            .or_insert(0) += 1;
        // Tombstone: until an explicit `refresh` completes, the lazy-load
        // path must NOT silently reload this key (that would defeat the
        // fail-closed-on-revocation window above).
        self.negative
            .lock()
            .expect("lock poisoned")
            .insert(key.clone(), Instant::now());
    }

    /// Every key currently resident in the memo -- the poll-fallback path
    /// (spec SS4: "the existing 300s poll as a fallback" alongside push
    /// invalidation) iterates this to re-[`Self::refresh`] each one, since a
    /// poll tick has no invalidation payload naming a single key the way a
    /// `bundle:grants:invalidate` pub/sub message does.
    pub fn keys(&self) -> Vec<GrantScopeKey> {
        self.memo
            .read()
            .expect("lock poisoned")
            .keys()
            .cloned()
            .collect()
    }
}

impl<L: GrantLoader> GrantSnapshot for GrantCache<L> {
    fn current(&self, key: &GrantScopeKey) -> Option<Arc<GrantSet>> {
        self.memo.read().expect("lock poisoned").get(key).cloned()
    }

    fn ensure_loaded<'a>(
        &'a self,
        key: &'a GrantScopeKey,
    ) -> Pin<Box<dyn Future<Output = ()> + Send + 'a>> {
        Box::pin(GrantCache::ensure_loaded(self, key))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key(app_version: i64) -> GrantScopeKey {
        GrantScopeKey {
            tenant_id: 7,
            community_id: 3,
            app_id: "waddles.core.example_echo".to_string(),
            app_version,
        }
    }

    fn grant_set(permission_id: &str, params: serde_json::Value) -> GrantSet {
        let mut grants = HashMap::new();
        grants.insert(
            permission_id.to_string(),
            GrantedPermission {
                permission_id: permission_id.to_string(),
                params,
            },
        );
        GrantSet {
            permission_snapshot_hash: "hash-v1".to_string(),
            grants,
        }
    }

    #[tokio::test]
    async fn grant_cache_returns_none_before_any_refresh() {
        let loader = Arc::new(InMemoryGrantLoader::new());
        let cache = GrantCache::new(loader);
        assert!(cache.current(&key(1)).is_none());
    }

    #[tokio::test]
    async fn grant_cache_refresh_populates_the_sync_read_path() {
        let loader = Arc::new(InMemoryGrantLoader::new());
        loader.set(key(1), grant_set("flags.read", serde_json::json!({})));
        let cache = GrantCache::new(loader);

        cache.refresh(&key(1)).await.unwrap();

        let snapshot = cache.current(&key(1)).expect("populated by refresh");
        assert!(snapshot.get("flags.read").is_some());
    }

    /// Revocation mid-invocation (spec SS4/SS5.3, Gemini condition 2):
    /// `invalidate()` alone (no refresh yet) must already fail closed.
    #[tokio::test]
    async fn grant_cache_invalidate_fails_closed_before_any_refresh_completes() {
        let loader = Arc::new(InMemoryGrantLoader::new());
        loader.set(key(1), grant_set("flags.read", serde_json::json!({})));
        let cache = GrantCache::new(loader);
        cache.refresh(&key(1)).await.unwrap();
        assert!(cache.current(&key(1)).is_some());

        cache.invalidate(&key(1));

        assert!(
            cache.current(&key(1)).is_none(),
            "an invalidated entry must read as absent until the next refresh completes"
        );
    }

    /// A loader answer of `None` on refresh (the row was deleted/deactivated)
    /// must also clear any stale memo entry, not leave the old grant set
    /// readable.
    #[tokio::test]
    async fn grant_cache_refresh_with_no_loader_row_clears_a_stale_memo_entry() {
        let loader = Arc::new(InMemoryGrantLoader::new());
        loader.set(key(1), grant_set("flags.read", serde_json::json!({})));
        let cache = GrantCache::new(loader.clone());
        cache.refresh(&key(1)).await.unwrap();
        assert!(cache.current(&key(1)).is_some());

        loader.remove(&key(1));
        cache.refresh(&key(1)).await.unwrap();

        assert!(cache.current(&key(1)).is_none());
    }

    /// Version-pinning (spec SS4/SS3.4): a community pinned to an older,
    /// already-consented version must never match a cache entry populated
    /// for a newer version -- the two are different `GrantScopeKey`s.
    #[tokio::test]
    async fn grant_cache_does_not_serve_a_different_app_version_under_the_same_key() {
        let loader = Arc::new(InMemoryGrantLoader::new());
        loader.set(key(1), grant_set("flags.read", serde_json::json!({})));
        let cache = GrantCache::new(loader);
        cache.refresh(&key(1)).await.unwrap();

        assert!(cache.current(&key(1)).is_some());
        assert!(
            cache.current(&key(2)).is_none(),
            "a newer, not-yet-consented app_version must not see the old version's grants"
        );
    }

    /// Regression (cold-cache bug): a key present in the loader but never
    /// refreshed must load on first use and then read as granted.
    #[tokio::test]
    async fn ensure_loaded_populates_a_cold_key_present_in_the_loader() {
        let loader = Arc::new(InMemoryGrantLoader::new());
        loader.set(
            key(1),
            grant_set("chat.send:discord", serde_json::json!({})),
        );
        let cache = GrantCache::new(loader);
        assert!(cache.current(&key(1)).is_none());

        cache.ensure_loaded(&key(1)).await;

        let set = cache.current(&key(1)).expect("lazy-loaded");
        assert!(set.get("chat.send:discord").is_some());
    }

    /// A genuinely-absent grant stays absent (fail-closed), and the miss is
    /// negative-cached so a repeat does not re-hit the loader.
    #[tokio::test]
    async fn ensure_loaded_keeps_an_absent_key_absent_and_negative_caches() {
        let loader = Arc::new(InMemoryGrantLoader::new());
        let cache = GrantCache::new(loader.clone());

        cache.ensure_loaded(&key(1)).await;
        assert!(cache.current(&key(1)).is_none());

        // Grant appears; still inside the negative window -> not re-queried.
        loader.set(key(1), grant_set("flags.read", serde_json::json!({})));
        cache.ensure_loaded(&key(1)).await;
        assert!(cache.current(&key(1)).is_none());
    }

    struct ListingLoader(InMemoryGrantLoader, Vec<GrantScopeKey>);
    impl GrantLoader for ListingLoader {
        fn load<'a>(
            &'a self,
            key: &'a GrantScopeKey,
        ) -> Pin<Box<dyn Future<Output = Result<Option<GrantSet>, GrantLoadError>> + Send + 'a>>
        {
            self.0.load(key)
        }
        fn list_active_keys<'a>(
            &'a self,
        ) -> Pin<Box<dyn Future<Output = Result<Vec<GrantScopeKey>, GrantLoadError>> + Send + 'a>>
        {
            let keys = self.1.clone();
            Box::pin(async move { Ok(keys) })
        }
    }

    #[tokio::test]
    async fn seed_primes_every_active_key() {
        let inner = InMemoryGrantLoader::new();
        inner.set(key(1), grant_set("flags.read", serde_json::json!({})));
        inner.set(key(2), grant_set("flags.read", serde_json::json!({})));
        let cache = GrantCache::new(Arc::new(ListingLoader(inner, vec![key(1), key(2)])));

        assert_eq!(cache.seed().await.unwrap(), 2);
        assert!(cache.current(&key(1)).is_some());
        assert!(cache.current(&key(2)).is_some());
    }

    /// Loader that parks inside `load` until released, to model a slow read.
    struct GatedLoader {
        inner: InMemoryGrantLoader,
        started: std::sync::atomic::AtomicBool,
        release: std::sync::atomic::AtomicBool,
    }
    impl GrantLoader for GatedLoader {
        fn load<'a>(
            &'a self,
            key: &'a GrantScopeKey,
        ) -> Pin<Box<dyn Future<Output = Result<Option<GrantSet>, GrantLoadError>> + Send + 'a>>
        {
            use std::sync::atomic::Ordering::SeqCst;
            // Snapshot at load-start (the stale, pre-revocation view).
            let snapshot = self.inner.rows_snapshot(key);
            Box::pin(async move {
                self.started.store(true, SeqCst);
                std::future::poll_fn(|cx| {
                    if self.release.load(SeqCst) {
                        std::task::Poll::Ready(())
                    } else {
                        cx.waker().wake_by_ref();
                        std::task::Poll::Pending
                    }
                })
                .await;
                Ok(snapshot)
            })
        }
    }

    /// Regression (revocation TOCTOU): an `invalidate` during an in-flight
    /// `refresh` must not be clobbered by the stale load result.
    #[tokio::test]
    async fn invalidate_during_inflight_refresh_is_not_clobbered_by_stale_load() {
        use std::sync::atomic::Ordering::SeqCst;
        let inner = InMemoryGrantLoader::new();
        inner.set(
            key(1),
            grant_set("chat.send:discord", serde_json::json!({})),
        );
        let loader = Arc::new(GatedLoader {
            inner,
            started: false.into(),
            release: false.into(),
        });
        let cache = GrantCache::new(loader.clone());

        let k = key(1);
        let refresh = cache.refresh(&k);
        let revoke = async {
            while !loader.started.load(SeqCst) {
                tokio::task::yield_now().await;
            }
            cache.invalidate(&key(1));
            loader.release.store(true, SeqCst);
        };
        let (res, ()) = tokio::join!(refresh, revoke);
        res.unwrap();

        assert!(
            cache.current(&key(1)).is_none(),
            "stale in-flight load must not re-insert a revoked grant"
        );
        // Tombstone intact: lazy path does not silently reload inside the window.
        cache.ensure_loaded(&key(1)).await;
        assert!(cache.current(&key(1)).is_none());
    }
}
