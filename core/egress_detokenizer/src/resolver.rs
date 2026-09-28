//! [`NameResolver`]: the trait each sink's host process implements to
//! reach the PII-boundary identity store (hub-api's `hub_users` mapping,
//! S10.1) for a batched `uuid -> display_name` lookup. This crate never
//! talks to a database or an HTTP client directly -- it only ever asks a
//! caller-supplied resolver, keeping the detokenizer itself free of any
//! transport/storage dependency so `svc_action` and (in a future landing)
//! `browser_source_core_module`'s Rust helper can each plug in their own
//! backing store without this crate growing per-consumer feature flags.

use std::collections::HashMap;
use std::sync::Arc;

use async_trait::async_trait;
use uuid::Uuid;

/// The fixed neutral label an erased, unknown, or otherwise unresolvable
/// user renders as (spec S10.4: "never the UUID, never a blank string,
/// never a visible error").
pub const NEUTRAL_LABEL: &str = "a former viewer";

/// A resolver-side failure (backend unreachable, timeout, malformed
/// response). Deliberately narrow -- [`crate::render::Detokenizer::render`]
/// treats every variant identically (log and fall back to
/// [`NEUTRAL_LABEL`] for the affected users), so this type exists for
/// diagnostics/logging, never for caller-side branching that could tempt a
/// fallback to unrendered/raw-UUID text.
#[derive(Debug, thiserror::Error)]
pub enum ResolveError {
    #[error("name resolver backend error: {0}")]
    Backend(String),
}

/// Batched display-name resolution for one tenant's user UUIDs, backed by
/// the PII-boundary identity store. Implementors own tenant scoping,
/// network/DB access, and rename/erasure bookkeeping; this trait's only
/// contract is: return a name for every UUID you can currently resolve,
/// and omit (never error-out) the ones you can't -- an erased or
/// never-seen UUID is absence from the returned map, not a per-key error.
#[async_trait]
pub trait NameResolver: Send + Sync {
    /// Resolves as many of `users` as possible within `tenant`, in a
    /// single batched call (spec S10.4: "one batched lookup, not one
    /// query per mention"). Returns `Err` only for a whole-batch backend
    /// failure (e.g. the store is unreachable) -- a specific user simply
    /// being erased or unknown is NOT an error, it is that UUID's absence
    /// from the returned map.
    async fn resolve_batch(
        &self,
        tenant: &str,
        users: &[Uuid],
    ) -> Result<HashMap<Uuid, String>, ResolveError>;
}

/// Lets a caller share one resolver instance between a [`crate::render::
/// Detokenizer`] (which owns it via [`crate::cache::NameCache`]) and its
/// own direct handle -- e.g. to mutate a backing store on a rename/erasure
/// event while the cache-wrapped copy renders concurrently. `svc_action`'s
/// real resolver is expected to be `Arc`-wrapped for exactly this reason.
#[async_trait]
impl<T> NameResolver for Arc<T>
where
    T: NameResolver + ?Sized,
{
    async fn resolve_batch(
        &self,
        tenant: &str,
        users: &[Uuid],
    ) -> Result<HashMap<Uuid, String>, ResolveError> {
        (**self).resolve_batch(tenant, users).await
    }
}

#[cfg(test)]
pub(crate) mod test_support {
    //! Shared test-only [`NameResolver`] fixtures, used by both this
    //! crate's own tests (`cache.rs`, `render.rs`) and re-exported for any
    //! downstream crate's tests via `#[cfg(test)]`-gated visibility --
    //! kept in `resolver.rs` rather than duplicated per test module.
    use super::*;
    use std::sync::Mutex;

    /// A resolver over a fixed, in-memory `(tenant, uuid) -> name` map,
    /// with an optional forced error and a call counter so cache-hit
    /// behavior (should NOT re-invoke the resolver) is assertable.
    #[derive(Default)]
    pub(crate) struct FixtureResolver {
        pub names: Mutex<HashMap<(String, Uuid), String>>,
        pub fail: Mutex<bool>,
        pub calls: Mutex<u32>,
    }

    impl FixtureResolver {
        pub(crate) fn with(names: &[(&str, Uuid, &str)]) -> Self {
            let mut map = HashMap::new();
            for (tenant, uuid, name) in names {
                map.insert(((*tenant).to_string(), *uuid), (*name).to_string());
            }
            Self {
                names: Mutex::new(map),
                fail: Mutex::new(false),
                calls: Mutex::new(0),
            }
        }

        pub(crate) fn call_count(&self) -> u32 {
            *self.calls.lock().unwrap()
        }

        pub(crate) fn set_failing(&self, failing: bool) {
            *self.fail.lock().unwrap() = failing;
        }

        /// Simulates a rename: mutates the backing store directly. Cache
        /// invalidation is the caller's job ([`crate::cache::NameCache::
        /// invalidate`]) -- this fixture only models the store side.
        pub(crate) fn rename(&self, tenant: &str, user: Uuid, new_name: &str) {
            self.names
                .lock()
                .unwrap()
                .insert((tenant.to_string(), user), new_name.to_string());
        }

        /// Simulates erasure (DSAR/right-to-erasure): removes the row
        /// entirely, so a subsequent resolve omits it from the batch
        /// result -- exactly what a real erased user looks like.
        pub(crate) fn erase(&self, tenant: &str, user: Uuid) {
            self.names
                .lock()
                .unwrap()
                .remove(&(tenant.to_string(), user));
        }
    }

    #[async_trait]
    impl NameResolver for FixtureResolver {
        async fn resolve_batch(
            &self,
            tenant: &str,
            users: &[Uuid],
        ) -> Result<HashMap<Uuid, String>, ResolveError> {
            *self.calls.lock().unwrap() += 1;
            if *self.fail.lock().unwrap() {
                return Err(ResolveError::Backend("fixture forced failure".into()));
            }
            let store = self.names.lock().unwrap();
            let mut out = HashMap::new();
            for u in users {
                if let Some(name) = store.get(&(tenant.to_string(), *u)) {
                    out.insert(*u, name.clone());
                }
            }
            Ok(out)
        }
    }
}
