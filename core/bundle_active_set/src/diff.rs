//! Pure diff between "what the stage has already told the executor is
//! loaded" and "what the DB says should be active" -- no I/O, no `sea_orm`
//! dependency, so it is unit-testable without a database or a live
//! executor connection. Each service's own `bundle_loader` module (`
//! core/svc_process/src/bundle_loader.rs`, `core/svc_action/src/
//! bundle_loader.rs`) owns the local `loaded: HashMap<app_id, digest>`
//! view this operates on and the actual `Load`/`Unload` wire calls --
//! there is no way to ask the executor "what do you currently have
//! loaded" (`penguin_bundle_host::wire::Message` has no such variant), so
//! that local view *is* the stage's only record of executor state, reset
//! on every reconnect exactly like `core/svc_process/src/spine.rs`'s
//! `LoadState`.

use std::collections::HashMap;

use crate::multi_tenant::AppScope;
use crate::query::ActiveBundleRow;

/// What [`plan`] decided to do this tick: bundles to `load` (new `app_id`
/// or a changed `digest` for an existing one) and bundles to `unload`
/// (`app_id`s no longer in the active set, paired with the digest last
/// known loaded so the executor's own digest-match check in `on_unload`
/// -- `core/bundle_executor/src/invoke.rs` -- succeeds).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct DiffPlan {
    pub to_load: Vec<ActiveBundleRow>,
    pub to_unload: Vec<(String, String)>,
}

impl DiffPlan {
    pub fn is_empty(&self) -> bool {
        self.to_load.is_empty() && self.to_unload.is_empty()
    }
}

/// Computes [`DiffPlan`] for `active` (the DB's ACTIVE+APPROVED set for
/// this tick) against `loaded` (`app_id` -> `digest` for what the caller
/// has already successfully told the executor to load). An `app_id`
/// present in both with the same digest is left alone; a changed digest
/// for the same `app_id` is a `load` (the executor's own `on_load`
/// unconditionally overwrites its registry entry for that `app_id`, spec
/// `core/bundle_executor/src/invoke.rs::on_load`, so no separate `unload`
/// is needed first); an `app_id` missing from `active` is an `unload`.
pub fn plan(loaded: &HashMap<String, String>, active: &[ActiveBundleRow]) -> DiffPlan {
    let mut to_load = Vec::new();
    let mut seen = std::collections::HashSet::with_capacity(active.len());

    for row in active {
        seen.insert(row.app_id.as_str());
        match loaded.get(&row.app_id) {
            Some(current_digest) if current_digest == &row.digest => {}
            _ => to_load.push(row.clone()),
        }
    }

    let to_unload = loaded
        .iter()
        .filter(|(app_id, _)| !seen.contains(app_id.as_str()))
        .map(|(app_id, digest)| (app_id.clone(), digest.clone()))
        .collect();

    DiffPlan { to_load, to_unload }
}

/// [`plan_scoped`]'s decision: bundles to `load` (a newly-referenced scope,
/// or a changed digest for an already-referenced one) and bundles to
/// `unload` (an `AppScope` no longer in the active set, paired with the
/// digest last known loaded so the executor's own digest-keyed `on_unload`
/// -- `core/bundle_executor/src/invoke.rs` -- finds and decrements the
/// right registry entry).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ScopedDiffPlan {
    pub to_load: Vec<(AppScope, ActiveBundleRow)>,
    pub to_unload: Vec<(AppScope, String)>,
}

impl ScopedDiffPlan {
    pub fn is_empty(&self) -> bool {
        self.to_load.is_empty() && self.to_unload.is_empty()
    }
}

/// The multi-tenant counterpart to [`plan`]: diffs `active`
/// (`bundle_active_set::multi_tenant::scoped_active_rows`'s `AppScope`-keyed
/// output for this tick) against `loaded` (`AppScope` -> digest for what
/// the caller has already successfully told the executor to load) --
/// **never collapsing two different scopes onto one `app_id`,** unlike the
/// retired `flatten_by_scope` + this module's own `plan`, which is exactly
/// the bug this function's caller (`crate::changelog_consumer::
/// apply_active_set` in each service) replaced it to fix: two different
/// `(tenant, community)` scopes independently active at two different
/// digests of the same `app_id` now produce two independent `to_load`/
/// `to_unload` decisions, one per scope, which the executor's digest-keyed,
/// refcounted registry (`core/bundle_executor/src/invoke.rs`) resolves
/// correctly regardless of how many scopes reference the same or different
/// digests.
pub fn plan_scoped(
    loaded: &HashMap<AppScope, String>,
    active: &HashMap<AppScope, ActiveBundleRow>,
) -> ScopedDiffPlan {
    let mut to_load = Vec::new();
    for (scope, row) in active {
        match loaded.get(scope) {
            Some(current_digest) if current_digest == &row.digest => {}
            _ => to_load.push((scope.clone(), row.clone())),
        }
    }

    let to_unload = loaded
        .iter()
        .filter(|(scope, _)| !active.contains_key(*scope))
        .map(|(scope, digest)| (scope.clone(), digest.clone()))
        .collect();

    ScopedDiffPlan { to_load, to_unload }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(app_id: &str, digest: &str) -> ActiveBundleRow {
        ActiveBundleRow {
            app_id: app_id.to_string(),
            version: "1".to_string(),
            version_id: 1,
            digest: digest.to_string(),
            component_key: format!("bundles/{digest}/component.wasm"),
            sidecar_key: format!("bundles/{digest}/sidecar.json"),
        }
    }

    #[test]
    fn plan_loads_a_newly_active_app_id() {
        let loaded = HashMap::new();
        let active = vec![row("waddles.a", "d1")];
        let diff = plan(&loaded, &active);
        assert_eq!(diff.to_load, vec![row("waddles.a", "d1")]);
        assert!(diff.to_unload.is_empty());
    }

    #[test]
    fn plan_is_a_noop_when_digest_is_unchanged() {
        let mut loaded = HashMap::new();
        loaded.insert("waddles.a".to_string(), "d1".to_string());
        let active = vec![row("waddles.a", "d1")];
        assert!(plan(&loaded, &active).is_empty());
    }

    #[test]
    fn plan_reloads_on_a_changed_digest_for_the_same_app_id() {
        let mut loaded = HashMap::new();
        loaded.insert("waddles.a".to_string(), "d1".to_string());
        let active = vec![row("waddles.a", "d2")];
        let diff = plan(&loaded, &active);
        assert_eq!(diff.to_load, vec![row("waddles.a", "d2")]);
        assert!(
            diff.to_unload.is_empty(),
            "a digest change is a load, never an unload+load"
        );
    }

    #[test]
    fn plan_unloads_an_app_id_no_longer_active() {
        let mut loaded = HashMap::new();
        loaded.insert("waddles.a".to_string(), "d1".to_string());
        let diff = plan(&loaded, &[]);
        assert_eq!(
            diff.to_unload,
            vec![("waddles.a".to_string(), "d1".to_string())]
        );
        assert!(diff.to_load.is_empty());
    }

    #[test]
    fn plan_handles_a_mixed_multi_app_tick() {
        let mut loaded = HashMap::new();
        loaded.insert("waddles.unchanged".to_string(), "d0".to_string());
        loaded.insert("waddles.changed".to_string(), "d1".to_string());
        loaded.insert("waddles.removed".to_string(), "d2".to_string());
        let active = vec![
            row("waddles.unchanged", "d0"),
            row("waddles.changed", "d1-new"),
            row("waddles.added", "d3"),
        ];
        let diff = plan(&loaded, &active);
        assert_eq!(diff.to_load.len(), 2, "changed + added, got {diff:?}");
        assert!(diff
            .to_load
            .iter()
            .any(|r| r.app_id == "waddles.changed" && r.digest == "d1-new"));
        assert!(diff.to_load.iter().any(|r| r.app_id == "waddles.added"));
        assert_eq!(
            diff.to_unload,
            vec![("waddles.removed".to_string(), "d2".to_string())]
        );
    }

    fn scope(tenant_id: i32, community_id: i32, app_id: &str) -> AppScope {
        (tenant_id, community_id, app_id.to_string())
    }

    /// **The primary regression test for `plan_scoped`:** two different
    /// scopes independently active at two DIFFERENT digests of the SAME
    /// `app_id` must both load, as two independent decisions -- the exact
    /// case the retired `flatten_by_scope` + app_id-keyed `plan` collapsed
    /// into one, silently dropping one tenant's version.
    #[test]
    fn plan_scoped_loads_two_scopes_independently_even_with_the_same_app_id() {
        let loaded = HashMap::new();
        let mut active = HashMap::new();
        active.insert(scope(1, 0, "waddles.shared"), row("waddles.shared", "d1"));
        active.insert(scope(2, 0, "waddles.shared"), row("waddles.shared", "d2"));

        let plan = plan_scoped(&loaded, &active);
        assert_eq!(plan.to_load.len(), 2);
        assert!(plan
            .to_load
            .iter()
            .any(|(s, r)| s == &scope(1, 0, "waddles.shared") && r.digest == "d1"));
        assert!(plan
            .to_load
            .iter()
            .any(|(s, r)| s == &scope(2, 0, "waddles.shared") && r.digest == "d2"));
    }

    #[test]
    fn plan_scoped_is_a_noop_when_a_scope_digest_is_unchanged() {
        let mut loaded = HashMap::new();
        loaded.insert(scope(1, 0, "waddles.a"), "d1".to_string());
        let mut active = HashMap::new();
        active.insert(scope(1, 0, "waddles.a"), row("waddles.a", "d1"));
        assert!(plan_scoped(&loaded, &active).is_empty());
    }

    #[test]
    fn plan_scoped_reloads_on_a_changed_digest_for_the_same_scope() {
        let mut loaded = HashMap::new();
        loaded.insert(scope(1, 0, "waddles.a"), "d1".to_string());
        let mut active = HashMap::new();
        active.insert(scope(1, 0, "waddles.a"), row("waddles.a", "d2"));
        let plan = plan_scoped(&loaded, &active);
        assert_eq!(
            plan.to_load,
            vec![(scope(1, 0, "waddles.a"), row("waddles.a", "d2"))]
        );
        assert!(plan.to_unload.is_empty());
    }

    /// Unloading one scope must never touch a different scope that happens
    /// to reference the exact same digest for the exact same `app_id` --
    /// each scope's own `AppScope` key is diffed independently, so a scope
    /// leaving the active set is one `to_unload` entry regardless of
    /// whether its digest is also still referenced elsewhere (the
    /// executor's own refcount, not this diff, is what decides whether the
    /// digest actually gets evicted -- see `core/bundle_executor/src/
    /// invoke.rs`).
    #[test]
    fn plan_scoped_unloads_only_the_scope_that_left_even_when_another_scope_shares_the_digest() {
        let mut loaded = HashMap::new();
        loaded.insert(scope(1, 0, "waddles.shared"), "d1".to_string());
        loaded.insert(scope(2, 0, "waddles.shared"), "d1".to_string());
        let mut active = HashMap::new();
        // Only scope (2, 0) remains active this tick.
        active.insert(scope(2, 0, "waddles.shared"), row("waddles.shared", "d1"));

        let plan = plan_scoped(&loaded, &active);
        assert_eq!(
            plan.to_unload,
            vec![(scope(1, 0, "waddles.shared"), "d1".to_string())]
        );
        assert!(plan.to_load.is_empty());
    }

    #[test]
    fn plan_scoped_is_empty_for_two_empty_maps() {
        assert!(plan_scoped(&HashMap::new(), &HashMap::new()).is_empty());
    }
}
