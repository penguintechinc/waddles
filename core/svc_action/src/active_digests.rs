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
    ///
    /// **Defense in depth: an empty `digest` is refused, never stored.**
    /// `bundle_active_set::query::canonical_digest` already guarantees every
    /// `ActiveBundleRow::digest` this crate's own writer (`apply_active_set`)
    /// passes here is non-empty -- but that guarantee is enforced upstream
    /// by a `debug_assert!` (compiled out in the release profile alpha
    /// actually runs), so this is the one runtime backstop against an
    /// empty digest ever becoming "current" for a scope and flowing on to
    /// `crate::dispatch::handle_delivered`'s invoke call, bypassing its own
    /// `NO_ACTIVE_DIGEST` guard (which only catches a missing entry, not an
    /// empty one). regression: same-digest manifest-only release (ping
    /// 1.0.2/1.0.3) emptied svc-action dispatch digest (alpha 2026-10-03)
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
    /// digest, bypassing [`Self::set`]'s own non-empty guard -- used to
    /// prove `crate::dispatch::handle_delivered`'s `NO_ACTIVE_DIGEST` guard
    /// independently catches an empty digest even if this map's own writer
    /// guard were ever bypassed (defense in depth, not an either/or).
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

/// Shared, concurrently-readable per-SESSION loaded-bundle tracker --
/// `crate::changelog_consumer::ConsumerState`'s `Arc`-shared counterpart to
/// `bundle_active_set::SessionLoaded<AppScope>`, read by every spawned
/// `dispatch_supervisor::run_app_consumer` task to pick a live executor
/// session that actually has the target digest loaded
/// (`crate::dispatch::DigestSource::Active`'s own doc), never just
/// whichever connection `ConnectionRegistry::active()` happens to call
/// "newest".
///
/// Direct port of the per-session half of `bundle_active_set::session_sync`
/// wired for svc-action's own dispatch path -- `crate::changelog_consumer`
/// is the sole writer (via `apply_active_set`, in lock-step with
/// `ActiveDigests` above at the exact same call sites); every spawned
/// dispatch consumer task is a reader only.
///
/// regression: bundles loaded only onto a terminating executor during
/// rollout; live executor got none (alpha 2026-10-03)
#[derive(Default)]
pub struct LoadedSessions {
    inner: RwLock<bundle_active_set::SessionLoaded<AppScope>>,
}

impl LoadedSessions {
    pub fn new() -> Self {
        Self::default()
    }

    /// Records `digest` as loaded for `scope` on `session` -- called by
    /// `apply_active_set` after a successful `Load` wire call to that
    /// specific session, in lock-step with `ConsumerState`'s own bookkeeping.
    pub fn mark_loaded(
        &self,
        session: bundle_active_set::SessionId,
        scope: AppScope,
        digest: String,
    ) {
        self.inner
            .write()
            .unwrap_or_else(|e| e.into_inner())
            .mark_loaded(session, scope, digest);
    }

    /// Clears `scope`'s entry for `session` -- called after a successful
    /// `Unload` wire call to that specific session.
    pub fn mark_unloaded(&self, session: bundle_active_set::SessionId, scope: &AppScope) {
        self.inner
            .write()
            .unwrap_or_else(|e| e.into_inner())
            .mark_unloaded(session, scope);
    }

    /// A session has been removed from the host-API registry -- drops ALL
    /// of its loaded-state at once. See
    /// `bundle_active_set::SessionLoaded::on_session_removed`'s own doc.
    pub fn on_session_removed(
        &self,
        session: bundle_active_set::SessionId,
    ) -> HashMap<AppScope, String> {
        self.inner
            .write()
            .unwrap_or_else(|e| e.into_inner())
            .on_session_removed(session)
    }

    /// Number of live sessions that currently have `scope` loaded (any
    /// digest) -- `0` is the fail-closed "no live executor can serve this
    /// bundle" state callers must log as ERROR and keep retrying.
    pub fn loaded_count(&self, scope: &AppScope) -> usize {
        self.inner
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .loaded_count(scope)
    }

    /// Total `(session, scope)` pairs currently tracked -- backs the
    /// `bundles_loaded` gauge (fan-out visibility, not just presence).
    pub fn total_entries(&self) -> usize {
        self.inner
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .total_entries()
    }

    /// The live session (if any) that has `scope` loaded at exactly
    /// `digest`, preferring newest -- the exact lookup
    /// `crate::dispatch::handle_delivered` performs on every single
    /// delivered entry before ever invoking. `None` means no live session
    /// can serve this digest right now; callers MUST dead-letter
    /// (`NO_LOADED_EXECUTOR`), never fall back to `ConnectionRegistry::
    /// active()` alone (the alpha 2026-10-03 failure mode).
    pub fn pick_session_with_digest(
        &self,
        scope: &AppScope,
        digest: &str,
    ) -> Option<bundle_active_set::SessionId> {
        bundle_active_set::pick_session_with_digest(
            &self.inner.read().unwrap_or_else(|e| e.into_inner()),
            scope,
            digest,
        )
    }

    /// A read-only snapshot (clone) of the full per-session loaded-state --
    /// used by `apply_active_set` to compute `bundle_active_set::
    /// plan_sessions`/`any_session_diverged` against a stable view for the
    /// duration of one tick, independent of concurrent dispatch-side reads.
    pub fn snapshot(&self) -> bundle_active_set::SessionLoaded<AppScope> {
        self.inner.read().unwrap_or_else(|e| e.into_inner()).clone()
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
    /// emptied svc-action dispatch digest (alpha 2026-10-03) -- `set` must
    /// never store an empty digest, on a brand-new scope...
    #[test]
    fn set_refuses_an_empty_digest_on_a_new_scope() {
        let digests = ActiveDigests::new();
        digests.set(scope("waddles.a"), String::new());
        assert_eq!(digests.get(&scope("waddles.a")), None);
    }

    /// ...nor may it clobber an already-stored valid digest with an empty
    /// one (the exact shape a buggy caller racing a hot-swap could trigger).
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

    /// regression: bundles loaded only onto a terminating executor during
    /// rollout; live executor got none (alpha 2026-10-03)
    mod loaded_sessions {
        use super::*;

        #[test]
        fn mark_loaded_then_pick_session_with_digest_round_trips() {
            let sessions = LoadedSessions::new();
            sessions.mark_loaded(1, scope("waddles.a"), "sha256:aa".to_string());
            assert_eq!(
                sessions.pick_session_with_digest(&scope("waddles.a"), "sha256:aa"),
                Some(1)
            );
        }

        #[test]
        fn pick_session_with_digest_prefers_newest_among_sessions_holding_the_digest() {
            let sessions = LoadedSessions::new();
            sessions.mark_loaded(1, scope("waddles.a"), "sha256:aa".to_string());
            sessions.mark_loaded(3, scope("waddles.a"), "sha256:aa".to_string());
            // Session 5 is newer overall but holds a DIFFERENT digest -- must
            // never be picked for a dispatch targeting "sha256:aa".
            sessions.mark_loaded(5, scope("waddles.a"), "sha256:other".to_string());
            assert_eq!(
                sessions.pick_session_with_digest(&scope("waddles.a"), "sha256:aa"),
                Some(3)
            );
        }

        #[test]
        fn pick_session_with_digest_returns_none_when_nothing_holds_it() {
            let sessions = LoadedSessions::new();
            assert_eq!(
                sessions.pick_session_with_digest(&scope("waddles.a"), "sha256:aa"),
                None
            );
        }

        #[test]
        fn on_session_removed_drops_only_that_session() {
            let sessions = LoadedSessions::new();
            sessions.mark_loaded(1, scope("waddles.a"), "sha256:aa".to_string());
            sessions.mark_loaded(2, scope("waddles.a"), "sha256:aa".to_string());
            let dropped = sessions.on_session_removed(1);
            assert_eq!(
                dropped.get(&scope("waddles.a")),
                Some(&"sha256:aa".to_string())
            );
            assert_eq!(sessions.loaded_count(&scope("waddles.a")), 1);
            assert_eq!(
                sessions.pick_session_with_digest(&scope("waddles.a"), "sha256:aa"),
                Some(2)
            );
        }

        #[test]
        fn loaded_count_reflects_how_many_sessions_hold_a_scope() {
            let sessions = LoadedSessions::new();
            assert_eq!(sessions.loaded_count(&scope("waddles.a")), 0);
            sessions.mark_loaded(1, scope("waddles.a"), "sha256:aa".to_string());
            assert_eq!(sessions.loaded_count(&scope("waddles.a")), 1);
            sessions.mark_unloaded(1, &scope("waddles.a"));
            assert_eq!(sessions.loaded_count(&scope("waddles.a")), 0);
        }

        #[test]
        fn snapshot_reflects_a_stable_clone_of_the_current_state() {
            let sessions = LoadedSessions::new();
            sessions.mark_loaded(1, scope("waddles.a"), "sha256:aa".to_string());
            let snap = sessions.snapshot();
            assert_eq!(snap.digest_for(1, &scope("waddles.a")), Some("sha256:aa"));
            sessions.mark_loaded(1, scope("waddles.a"), "sha256:bb".to_string());
            // The snapshot taken before the second write must stay frozen.
            assert_eq!(snap.digest_for(1, &scope("waddles.a")), Some("sha256:aa"));
        }

        #[test]
        fn total_entries_counts_every_session_scope_pair() {
            let sessions = LoadedSessions::new();
            sessions.mark_loaded(1, scope("waddles.a"), "sha256:aa".to_string());
            sessions.mark_loaded(2, scope("waddles.a"), "sha256:aa".to_string());
            assert_eq!(sessions.total_entries(), 2);
        }
    }
}
