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

#[cfg(test)]
mod tests {
    use super::*;

    fn row(app_id: &str, digest: &str) -> ActiveBundleRow {
        ActiveBundleRow {
            app_id: app_id.to_string(),
            version: "1".to_string(),
            digest: digest.to_string(),
            component_key: format!("bundles/{digest}/component.wasm"),
            sidecar_key: format!("bundles/{digest}/sidecar.json"),
            artifact_signature: None,
            artifact_signature_key_id: None,
            artifact_signed_approval_id: None,
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
}
