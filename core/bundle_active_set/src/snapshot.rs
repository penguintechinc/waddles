//! A live, poll/changelog-refreshed `app_id -> (digest, app_versions.id)`
//! snapshot for one `(tenant_id, community_id)` scope.
//!
//! Resolving a live invocation's `app_version` ONCE at startup goes stale
//! the moment the active-set poller/changelog consumer hot-swaps a newer
//! digest in for the same `app_id` (spec SS4/SS5.1: grants are keyed
//! `(tenant, community, app, app_version)`) -- a pod that keeps invoking
//! under the OLD, captured version_id would either be denied every real
//! grant (if the new version's grants differ) or, worse, silently
//! authorized under a version's permissions it is no longer actually
//! running. [`ActiveVersionSnapshot`] instead holds a cheap,
//! `Clone`-able handle to a shared, wholesale-replaced map: whichever
//! poll/changelog tick already reads `bundle_active_set::read_active_set`
//! for this scope (`crate::bundle_loader`/`crate::source_supervisor` in
//! each service) also calls [`ActiveVersionSnapshot::update`] with that
//! SAME read's rows, and every invocation resolves fresh against it via
//! [`ActiveVersionSnapshot::resolve_for_digest`]/
//! [`ActiveVersionSnapshot::resolve_for_app`] -- never a captured `i64`.

use std::collections::HashMap;
use std::sync::{Arc, RwLock};

use crate::query::ActiveBundleRow;

/// Cheap, `Clone`-able handle over a shared `app_id -> (digest,
/// version_id)` map -- see the module doc.
#[derive(Clone, Default)]
pub struct ActiveVersionSnapshot(Arc<RwLock<HashMap<String, (String, i64)>>>);

impl ActiveVersionSnapshot {
    pub fn new() -> Self {
        Self::default()
    }

    /// Replaces the snapshot wholesale with `rows`'s own `app_id ->
    /// (digest, version_id)` pairs -- called once per poll/changelog tick,
    /// right after the same [`crate::query::ActiveSetRead`] a hot-swap
    /// loader already read for this scope. Wholesale replacement (never a
    /// merge) is deliberate: an `app_id` that dropped out of the current
    /// read (deactivated, or excluded per `crate::query::ExclusionReason`)
    /// must stop resolving immediately, not linger as a stale entry.
    pub fn update(&self, rows: &[ActiveBundleRow]) {
        let map = rows
            .iter()
            .map(|row| (row.app_id.clone(), (row.digest.clone(), row.version_id)))
            .collect();
        // A poisoned lock (a panic elsewhere while holding the write
        // guard) recovers with the poisoned map rather than propagating --
        // a stale snapshot must never become permanently unusable.
        *self.0.write().unwrap_or_else(|e| e.into_inner()) = map;
    }

    /// Resolves `app_id`'s current `app_versions.id` ONLY if its
    /// currently-active row's own digest matches `digest` -- for a caller
    /// about to invoke one SPECIFIC digest (svc_action's dispatch loop): a
    /// pod pinned to a digest older than the tenant's current activation
    /// must fail closed (`None`), never be handed the NEWER digest's
    /// version for the OLD one it is actually about to run.
    pub fn resolve_for_digest(&self, app_id: &str, digest: &str) -> Option<i64> {
        let map = self.0.read().unwrap_or_else(|e| e.into_inner());
        map.get(app_id).and_then(|(active_digest, version_id)| {
            (active_digest == digest).then_some(*version_id)
        })
    }

    /// Resolves `app_id`'s current `app_versions.id` regardless of digest --
    /// for a caller with no fixed digest of its own to compare against
    /// (svc_process's DB-driven source-binding consumers, which never hold
    /// a digest at all; `crate::bundle_loader`/`crate::source_supervisor`'s
    /// own `bundle_loader` half owns load/unload independently).
    pub fn resolve_for_app(&self, app_id: &str) -> Option<i64> {
        self.0
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .get(app_id)
            .map(|(_, version_id)| *version_id)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(app_id: &str, digest: &str, version_id: i64) -> ActiveBundleRow {
        ActiveBundleRow {
            app_id: app_id.to_string(),
            version: version_id.to_string(),
            version_id,
            digest: digest.to_string(),
            component_key: String::new(),
            sidecar_key: String::new(),
        }
    }

    #[test]
    fn resolve_for_digest_matches_the_currently_active_digest() {
        let snap = ActiveVersionSnapshot::new();
        snap.update(&[row("waddles.a", "sha256:a", 1)]);
        assert_eq!(snap.resolve_for_digest("waddles.a", "sha256:a"), Some(1));
    }

    #[test]
    fn resolve_for_digest_fails_closed_on_a_digest_mismatch() {
        let snap = ActiveVersionSnapshot::new();
        snap.update(&[row("waddles.a", "sha256:new", 2)]);
        assert_eq!(
            snap.resolve_for_digest("waddles.a", "sha256:old"),
            None,
            "a pod pinned to a superseded digest must never resolve the newer digest's version"
        );
    }

    /// Regression: swap the active version mid-run -- the next resolve
    /// call must see the NEW version_id, and the OLD digest must
    /// immediately stop resolving (wholesale replace, never a merge).
    #[test]
    fn update_mid_run_hot_swaps_to_the_new_version_and_denies_the_old_digest() {
        let snap = ActiveVersionSnapshot::new();
        snap.update(&[row("waddles.a", "sha256:old", 7)]);
        assert_eq!(snap.resolve_for_digest("waddles.a", "sha256:old"), Some(7));
        assert_eq!(snap.resolve_for_app("waddles.a"), Some(7));

        snap.update(&[row("waddles.a", "sha256:new", 9)]);
        assert_eq!(
            snap.resolve_for_digest("waddles.a", "sha256:old"),
            None,
            "the superseded digest must fail closed immediately after the swap"
        );
        assert_eq!(snap.resolve_for_digest("waddles.a", "sha256:new"), Some(9));
        assert_eq!(snap.resolve_for_app("waddles.a"), Some(9));
    }

    #[test]
    fn resolve_for_app_is_none_for_an_unknown_app_id() {
        let snap = ActiveVersionSnapshot::new();
        assert_eq!(snap.resolve_for_app("waddles.missing"), None);
    }

    #[test]
    fn an_app_dropped_from_a_later_update_stops_resolving() {
        let snap = ActiveVersionSnapshot::new();
        snap.update(&[
            row("waddles.a", "sha256:a", 1),
            row("waddles.b", "sha256:b", 2),
        ]);
        snap.update(&[row("waddles.b", "sha256:b", 2)]);
        assert_eq!(
            snap.resolve_for_app("waddles.a"),
            None,
            "an app_id no longer in the active set must stop resolving, never linger"
        );
        assert_eq!(snap.resolve_for_app("waddles.b"), Some(2));
    }
}
