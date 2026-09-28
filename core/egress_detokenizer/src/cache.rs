//! Per-tenant, TTL-based cache in front of [`crate::resolver::NameResolver`]
//! (spec S10.4: "a small per-tenant `{uuid -> display_name}` cache... TTL 5
//! minutes default, invalidated immediately on a rename event rather than
//! waiting out the TTL"). A plain `RwLock<HashMap<..>>` -- this crate's
//! read:write ratio (many renders, occasional rename/erasure) doesn't
//! justify a sharded/lock-free map dependency.

use std::collections::HashMap;
use std::sync::RwLock;
use std::time::{Duration, Instant};

use uuid::Uuid;

use crate::resolver::{NameResolver, ResolveError};

/// Tunables for [`NameCache`]. Kept as its own struct (rather than bare
/// constructor args) so a future field (e.g. a max-entries cap) doesn't
/// need to change every call site.
#[derive(Debug, Clone, Copy)]
pub struct CacheConfig {
    /// Time a resolved name (or a cached "unresolvable" outcome) stays
    /// valid before a fresh lookup is required. Spec default: 5 minutes.
    pub ttl: Duration,
}

impl Default for CacheConfig {
    fn default() -> Self {
        Self {
            ttl: Duration::from_secs(300),
        }
    }
}

/// One cached outcome for a `(tenant, user)` pair. `None` caches a
/// negative result (erased/unknown at last lookup) so a burst of mentions
/// for the same erased user doesn't hit the resolver once per render --
/// the TTL (or an explicit [`NameCache::invalidate`]) is what lets a
/// later real identity re-appear, matching S10.3's "reappearance simply
/// re-counts" posture for identity data generally.
struct Entry {
    name: Option<String>,
    expires_at: Instant,
}

/// A per-tenant name cache wrapping one [`NameResolver`]. Cheap to clone
/// (wrap in `Arc` at the call site, matching this repo's Tokio shared-state
/// convention) -- the cache storage itself is not `Clone` to keep exactly
/// one lock per logical cache.
pub struct NameCache<R: NameResolver> {
    resolver: R,
    config: CacheConfig,
    entries: RwLock<HashMap<String, HashMap<Uuid, Entry>>>,
}

impl<R: NameResolver> NameCache<R> {
    pub fn new(resolver: R, config: CacheConfig) -> Self {
        Self {
            resolver,
            config,
            entries: RwLock::new(HashMap::new()),
        }
    }

    /// Resolves `users` for `tenant`, serving fresh cache hits directly and
    /// issuing exactly one batched [`NameResolver::resolve_batch`] call for
    /// the rest (spec: "one batched lookup, not one query per mention").
    /// A resolver error degrades every currently-uncached user in this
    /// batch to "unresolved" (caller renders the neutral label) rather
    /// than propagating -- see [`crate::render`]'s doc for why this crate
    /// never lets a backend failure risk falling back to raw-UUID output.
    /// The negative outcome is deliberately NOT cached on a backend error
    /// (only on a genuine resolver-confirmed absence) so a transient
    /// outage self-heals on the very next render instead of pinning every
    /// mention to the neutral label for a full TTL.
    pub async fn resolve(&self, tenant: &str, users: &[Uuid]) -> HashMap<Uuid, Option<String>> {
        let mut out = HashMap::with_capacity(users.len());
        let mut misses = Vec::new();
        let now = Instant::now();

        {
            let cache = self.entries.read().unwrap_or_else(|e| e.into_inner());
            if let Some(tenant_cache) = cache.get(tenant) {
                for &u in users {
                    match tenant_cache.get(&u) {
                        Some(entry) if entry.expires_at > now => {
                            out.insert(u, entry.name.clone());
                        }
                        _ => misses.push(u),
                    }
                }
            } else {
                misses.extend_from_slice(users);
            }
        }

        if misses.is_empty() {
            return out;
        }

        match self.resolver.resolve_batch(tenant, &misses).await {
            Ok(resolved) => {
                let expires_at = now + self.config.ttl;
                let mut cache = self.entries.write().unwrap_or_else(|e| e.into_inner());
                let tenant_cache = cache.entry(tenant.to_string()).or_default();
                for u in misses {
                    let name = resolved.get(&u).cloned();
                    tenant_cache.insert(
                        u,
                        Entry {
                            name: name.clone(),
                            expires_at,
                        },
                    );
                    out.insert(u, name);
                }
            }
            Err(ResolveError::Backend(reason)) => {
                tracing::warn!(
                    tenant,
                    reason,
                    user_count = misses.len(),
                    "egress_detokenizer: name resolver backend failed; rendering neutral label for this batch"
                );
                for u in misses {
                    out.insert(u, None);
                }
            }
        }

        out
    }

    /// Immediate invalidation for one user within one tenant (rename or
    /// erasure) -- spec: "invalidated immediately on a rename event rather
    /// than waiting out the TTL". Idempotent; a miss is not an error.
    pub fn invalidate(&self, tenant: &str, user: Uuid) {
        let mut cache = self.entries.write().unwrap_or_else(|e| e.into_inner());
        if let Some(tenant_cache) = cache.get_mut(tenant) {
            tenant_cache.remove(&user);
        }
    }

    /// Drops every cached entry for `tenant` -- used for a tenant-wide
    /// bulk event (e.g. a bulk DSAR erasure run) rather than one
    /// [`invalidate`] call per affected user.
    pub fn invalidate_tenant(&self, tenant: &str) {
        self.entries
            .write()
            .unwrap_or_else(|e| e.into_inner())
            .remove(tenant);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::resolver::test_support::FixtureResolver;

    fn uuid(n: u8) -> Uuid {
        Uuid::parse_str(&format!("11111111-1111-4111-8111-11111111111{n:x}")).unwrap()
    }

    #[tokio::test]
    async fn resolves_and_caches_a_hit() {
        let resolver = FixtureResolver::with(&[("t1", uuid(1), "Alice")]);
        let cache = NameCache::new(resolver, CacheConfig::default());

        let first = cache.resolve("t1", &[uuid(1)]).await;
        assert_eq!(first.get(&uuid(1)).unwrap().as_deref(), Some("Alice"));

        let second = cache.resolve("t1", &[uuid(1)]).await;
        assert_eq!(second.get(&uuid(1)).unwrap().as_deref(), Some("Alice"));
        assert_eq!(
            cache.resolver.call_count(),
            1,
            "second resolve for the same tenant/uuid must be served from cache, not re-hit the resolver"
        );
    }

    #[tokio::test]
    async fn unknown_user_resolves_to_none_and_is_cached() {
        let resolver = FixtureResolver::with(&[]);
        let cache = NameCache::new(resolver, CacheConfig::default());

        let out = cache.resolve("t1", &[uuid(9)]).await;
        assert_eq!(out.get(&uuid(9)).unwrap(), &None);
        assert_eq!(cache.resolver.call_count(), 1);

        // Second call still a cache hit (negative caching).
        let _ = cache.resolve("t1", &[uuid(9)]).await;
        assert_eq!(cache.resolver.call_count(), 1);
    }

    #[tokio::test]
    async fn invalidate_forces_a_fresh_lookup_on_rename() {
        let resolver = FixtureResolver::with(&[("t1", uuid(1), "Alice")]);
        let cache = NameCache::new(resolver, CacheConfig::default());

        let _ = cache.resolve("t1", &[uuid(1)]).await;
        cache.resolver.rename("t1", uuid(1), "AliceNew");
        cache.invalidate("t1", uuid(1));

        let out = cache.resolve("t1", &[uuid(1)]).await;
        assert_eq!(out.get(&uuid(1)).unwrap().as_deref(), Some("AliceNew"));
        assert_eq!(cache.resolver.call_count(), 2);
    }

    #[tokio::test]
    async fn erasure_flips_a_cached_hit_to_none_after_invalidation() {
        let resolver = FixtureResolver::with(&[("t1", uuid(1), "Alice")]);
        let cache = NameCache::new(resolver, CacheConfig::default());

        let _ = cache.resolve("t1", &[uuid(1)]).await;
        cache.resolver.erase("t1", uuid(1));
        cache.invalidate("t1", uuid(1));

        let out = cache.resolve("t1", &[uuid(1)]).await;
        assert_eq!(out.get(&uuid(1)).unwrap(), &None);
    }

    #[tokio::test]
    async fn invalidate_tenant_drops_every_cached_entry_for_that_tenant() {
        let resolver = FixtureResolver::with(&[("t1", uuid(1), "Alice"), ("t1", uuid(2), "Bob")]);
        let cache = NameCache::new(resolver, CacheConfig::default());

        let _ = cache.resolve("t1", &[uuid(1), uuid(2)]).await;
        assert_eq!(cache.resolver.call_count(), 1);

        cache.invalidate_tenant("t1");
        let _ = cache.resolve("t1", &[uuid(1), uuid(2)]).await;
        assert_eq!(cache.resolver.call_count(), 2);
    }

    #[tokio::test]
    async fn expired_ttl_forces_a_fresh_lookup() {
        let resolver = FixtureResolver::with(&[("t1", uuid(1), "Alice")]);
        let cache = NameCache::new(
            resolver,
            CacheConfig {
                ttl: Duration::from_millis(1),
            },
        );

        let _ = cache.resolve("t1", &[uuid(1)]).await;
        tokio::time::sleep(Duration::from_millis(20)).await;
        let _ = cache.resolve("t1", &[uuid(1)]).await;
        assert_eq!(cache.resolver.call_count(), 2);
    }

    #[tokio::test]
    async fn backend_failure_degrades_to_none_without_caching_the_failure() {
        let resolver = FixtureResolver::with(&[("t1", uuid(1), "Alice")]);
        resolver.set_failing(true);
        let cache = NameCache::new(resolver, CacheConfig::default());

        let out = cache.resolve("t1", &[uuid(1)]).await;
        assert_eq!(out.get(&uuid(1)).unwrap(), &None);

        cache.resolver.set_failing(false);
        let out = cache.resolve("t1", &[uuid(1)]).await;
        assert_eq!(
            out.get(&uuid(1)).unwrap().as_deref(),
            Some("Alice"),
            "a transient backend failure must self-heal on the next render, not pin to None for a full TTL"
        );
    }

    #[tokio::test]
    async fn tenants_are_isolated() {
        let resolver = FixtureResolver::with(&[("t1", uuid(1), "Alice")]);
        let cache = NameCache::new(resolver, CacheConfig::default());

        let _ = cache.resolve("t1", &[uuid(1)]).await;
        let out = cache.resolve("t2", &[uuid(1)]).await;
        assert_eq!(
            out.get(&uuid(1)).unwrap(),
            &None,
            "the same uuid under a different tenant must not reuse t1's cached/resolved name"
        );
    }
}
