//! Shared, concurrently-readable map of each `(tenant_id, community_id,
//! app_id)` scope's CURRENT canonical digest -- the multi-tenant source of
//! truth every per-app dispatch consumer (`crate::dispatch_supervisor::
//! run_app_consumer`) reads from to build its `dispatch::DispatchDeps::
//! digest_source`, never an empty/stale digest captured once at spawn time.
//!
//! Direct port of `core/svc_process/src/active_digests.rs` (same type,
//! same API, same `AppScope` key) -- kept as its own copy rather than
//! moved into `bundle_active_set` in this landing, to avoid widening that
//! shared crate's surface (and svc_process's own already-merged wiring)
//! under this change's time budget; a follow-up can hoist both services'
//! identical copies into `bundle_active_set` once that move is itself
//! reviewed in isolation.
//!
//! Owned by `crate::changelog_consumer::ConsumerState` and written in
//! lock-step with `ConsumerState::loaded` (`apply_active_set`'s `to_load`/
//! `to_unload` loops) -- same key (`bundle_active_set::AppScope`), same
//! lifetime, kept in sync at the exact same call sites. Unlike `state.
//! loaded` (owned exclusively by the single changelog-consumer task, no
//! lock needed), this map is read concurrently by every spawned per-app
//! dispatch consumer task, so it needs its own `RwLock` and its own `Arc`
//! shared into `dispatch_supervisor::SupervisorDeps` at startup
//! (`crate::lib`).
//!
//! A hot-swapped digest (or a newly-active app) is visible to every
//! already-running dispatch consumer's very next invoke -- no consumer
//! restart, no reconnect, required.
//!
//! regression: svc-action had no multi-tenant dispatch consumers; replies
//! never sent after legacy env removal (alpha 2026-10-03)

use std::collections::HashMap;
use std::sync::RwLock;

use bundle_active_set::AppScope;

/// See the module doc. Default-constructed empty; `crate::changelog_consumer
/// ::run` is the sole writer (via `apply_active_set`), every spawned
/// `dispatch_supervisor::run_app_consumer` task is a reader.
#[derive(Default)]
pub struct ActiveDigests {
    inner: RwLock<HashMap<AppScope, String>>,
}

impl ActiveDigests {
    pub fn new() -> Self {
        Self::default()
    }

    /// The current canonical digest for `scope`, or `None` if this consumer
    /// has no active bundle loaded for it right now (never seen, unloaded,
    /// or its scope is currently failing to resolve -- see `crate::
    /// changelog_consumer`'s fail-closed-per-scope doc). Callers must treat
    /// `None` as "do not invoke" (`crate::dispatch_supervisor`'s own
    /// `DigestSource::Active` doc), never substitute an empty string.
    pub fn get(&self, scope: &AppScope) -> Option<String> {
        self.inner
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .get(scope)
            .cloned()
    }

    /// Records `digest` as the current canonical digest for `scope` --
    /// called by `apply_active_set` on every successful `load`, in
    /// lock-step with `ConsumerState::loaded`'s own insert for the same
    /// scope.
    pub fn set(&self, scope: AppScope, digest: String) {
        self.inner
            .write()
            .unwrap_or_else(|e| e.into_inner())
            .insert(scope, digest);
    }

    /// Clears `scope`'s entry -- called by `apply_active_set` on every
    /// successful `unload` (and, transitively, whenever a scope drops out
    /// of the active set and the next diff naturally unloads it), in
    /// lock-step with `ConsumerState::loaded`'s own removal for the same
    /// scope.
    pub fn remove(&self, scope: &AppScope) {
        self.inner
            .write()
            .unwrap_or_else(|e| e.into_inner())
            .remove(scope);
    }

    #[cfg(test)]
    pub fn len(&self) -> usize {
        self.inner.read().unwrap().len()
    }

    #[cfg(test)]
    pub fn is_empty(&self) -> bool {
        self.inner.read().unwrap().is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scope(app_id: &str) -> AppScope {
        (1, 0, app_id.to_string())
    }

    #[test]
    fn get_returns_none_for_an_unknown_scope() {
        let digests = ActiveDigests::new();
        assert_eq!(digests.get(&scope("waddles.a")), None);
    }

    #[test]
    fn set_then_get_round_trips_the_digest() {
        let digests = ActiveDigests::new();
        digests.set(scope("waddles.a"), "sha256:aa".to_string());
        assert_eq!(
            digests.get(&scope("waddles.a")),
            Some("sha256:aa".to_string())
        );
    }

    #[test]
    fn set_overwrites_a_hot_swapped_digest_for_the_same_scope() {
        let digests = ActiveDigests::new();
        digests.set(scope("waddles.a"), "sha256:aa".to_string());
        digests.set(scope("waddles.a"), "sha256:bb".to_string());
        assert_eq!(
            digests.get(&scope("waddles.a")),
            Some("sha256:bb".to_string())
        );
    }

    #[test]
    fn remove_clears_the_entry() {
        let digests = ActiveDigests::new();
        digests.set(scope("waddles.a"), "sha256:aa".to_string());
        digests.remove(&scope("waddles.a"));
        assert_eq!(digests.get(&scope("waddles.a")), None);
    }

    #[test]
    fn two_different_scopes_for_the_same_app_id_are_tracked_independently() {
        let digests = ActiveDigests::new();
        let scope_a = (1, 0, "waddles.a".to_string());
        let scope_b = (2, 0, "waddles.a".to_string());
        digests.set(scope_a.clone(), "sha256:aa".to_string());
        digests.set(scope_b.clone(), "sha256:bb".to_string());
        assert_eq!(digests.get(&scope_a), Some("sha256:aa".to_string()));
        assert_eq!(digests.get(&scope_b), Some("sha256:bb".to_string()));
        assert_eq!(digests.len(), 2);
    }
}
