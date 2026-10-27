//! Shared, concurrently-readable map of each `(tenant_id, community_id,
//! app_id)` scope's CURRENT canonical digest -- the multi-tenant source of
//! truth every per-binding consumer (`crate::source_supervisor::
//! run_binding_consumer`) reads from to build its `Invoke`, replacing the
//! empty `ProcessDeps::digest` sentinel the DB-driven path used to spawn
//! every consumer with (never actually populated, since bundle load/unload
//! is `crate::changelog_consumer`'s job, not `source_supervisor`'s --
//! see that module's own doc).
//!
//! Owned by `crate::changelog_consumer::ConsumerState` and written in
//! lock-step with `ConsumerState::loaded` (`apply_active_set`'s `to_load`/
//! `to_unload` loops) -- same key (`bundle_active_set::AppScope`), same
//! lifetime, kept in sync at the exact same call sites. Unlike `state.
//! loaded` (owned exclusively by the single changelog-consumer task, no
//! lock needed), this map is read concurrently by every spawned per-binding
//! consumer task, so it needs its own `RwLock` and its own `Arc` shared
//! into `source_supervisor::SupervisorDeps` at startup (`crate::lib`).
//!
//! A hot-swapped digest (or a newly-active app) is visible to every
//! already-running per-binding consumer's very next invoke -- no consumer
//! restart, no reconnect, required.
//!
//! regression: multi-tenant consumers invoked with empty legacy digest,
//! UnknownBundle (alpha 2026-10-03)

use std::collections::HashMap;
use std::sync::RwLock;

use bundle_active_set::AppScope;

/// See the module doc. Default-constructed empty; `crate::changelog_consumer
/// ::run` is the sole writer (via `apply_active_set`), every spawned
/// `source_supervisor::run_binding_consumer` task is a reader.
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
    /// `None` as "do not invoke" (`crate::source_supervisor`'s own
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
    ///
    /// **Defense in depth: an empty `digest` is refused, never stored.**
    /// Direct port of `core/svc_action/src/active_digests.rs::set`'s
    /// identical fix -- see that function's own doc for the full rationale.
    /// regression: same-digest manifest-only release (ping 1.0.2/1.0.3)
    /// emptied svc-action dispatch digest (alpha 2026-10-03)
    pub fn set(&self, scope: AppScope, digest: String) {
        if digest.is_empty() {
            tracing::error!(
                tenant_id = scope.0,
                community_id = scope.1,
                app_id = %scope.2,
                "ActiveDigests::set called with an empty digest; refusing to store it \
                 -- an empty digest must never become \"current\" for dispatch"
            );
            return;
        }
        self.inner
            .write()
            .unwrap_or_else(|e| e.into_inner())
            .insert(scope, digest);
    }

    /// Test-only adversarial seam: forces `scope`'s entry to an empty
    /// digest, bypassing [`Self::set`]'s own non-empty guard -- see
    /// `core/svc_action/src/active_digests.rs`'s identical test helper.
    #[cfg(test)]
    pub fn force_set_for_test(&self, scope: AppScope, digest: String) {
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

    /// regression: same-digest manifest-only release (ping 1.0.2/1.0.3)
    /// emptied svc-action dispatch digest (alpha 2026-10-03)
    #[test]
    fn set_refuses_an_empty_digest_on_a_new_scope() {
        let digests = ActiveDigests::new();
        digests.set(scope("waddles.a"), String::new());
        assert_eq!(digests.get(&scope("waddles.a")), None);
    }

    #[test]
    fn set_refuses_an_empty_digest_and_leaves_the_prior_value_intact() {
        let digests = ActiveDigests::new();
        digests.set(scope("waddles.a"), "sha256:aa".to_string());
        digests.set(scope("waddles.a"), String::new());
        assert_eq!(
            digests.get(&scope("waddles.a")),
            Some("sha256:aa".to_string())
        );
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
