//! The single seam that decides whether the `kv` capability is granted for
//! one invocation's app -- every other module in this crate (and every
//! caller in `core/svc_process`/`core/svc_action`) reaches the Valkey
//! backend only through [`authorize_kv`] first, so replacing *how* the
//! decision is made later never touches key derivation, quota, or metrics
//! code.
//!
//! **Interim implementation (coordinator fix on PR #425, per the approved
//! `docs/superpowers/specs/2026-09-28-bundle-permissions-and-capability-gate.md`,
//! PR #419).** That spec's standard, Android-style gate
//! (`core/bundle_capability_gate::authorize(scope, permission, resource)`,
//! push-invalidated `GrantSnapshot` sourced from new
//! `app_permission_requests`/`community_permission_grants` tables) is
//! Phase 0/4 -- not built yet. Until it lands, [`authorize_kv`] grants
//! `kv` **only if the invoking app's currently-active, currently-approved
//! manifest version declares it** (permission id [`KV_PERMISSION_ID`]),
//! read from [`CapabilitySnapshot`] -- **undeclared means denied**, the
//! opposite of this module's previous unconditional-grant stand-in.
//!
//! [`CapabilitySnapshot`] is populated from
//! `bundle_active_set::ActiveBundleRow::declared_capabilities`, itself
//! read from `app_install_approvals.summary_json`'s `"capabilities"` array
//! (`hub_api/services/bundle_approval_service.py::_derive_capabilities`,
//! now gating `"kv"` on the manifest's own `permissions:` list containing
//! `"storage.kv"`, the same way it already gates `"http"`/`"db"` on
//! `egress`/`data.tables`). Each service's own DB-driven `bundle_loader`
//! calls [`CapabilitySnapshot::update`] once per active `app_id` per poll
//! tick (or push-invalidation, once §4 of the spec lands) -- see
//! `core/svc_action::bundle_loader`/`core/svc_process::bundle_loader`'s own
//! doc for the call site.
//!
//! **The legacy, single-bundle, env-driven path
//! (`PROCESS_APP_ID`/`ACTION_BUNDLE_*`) has no active-set snapshot at all**
//! -- no DB row, no consent record, nothing to check. An `app_id` [`
//! CapabilitySnapshot`] has never heard of is, by construction, "undeclared"
//! (never "declared with no evidence"), so `kv` denies there by default
//! too, consistently with every other app -- this is the correct
//! fail-closed outcome, not a gap: a deployment that wants `kv` migrates to
//! the DB-driven active-set path, where a real consent record exists to
//! check.
//!
//! `KV_PERMISSION_ID` is the stable identifier this capability is known by
//! in the forthcoming standard gate (and is used today, ahead of time, as
//! the `permission` label on every log line and metric this crate emits)
//! so nothing about observability needs to change when [`authorize_kv`]'s
//! body is eventually replaced with a call into
//! `core/bundle_capability_gate::authorize`.

use std::collections::{HashMap, HashSet};
use std::sync::RwLock;

use crate::scope::KvScope;

/// The permission id `kv` is known by in the standard permission model
/// (PR #419 §1), and the label value this crate attaches to every log line
/// and OTel metric it emits.
pub const KV_PERMISSION_ID: &str = "storage.kv";

/// One denied authorization decision -- deliberately a plain struct
/// (rather than borrowing `HostResultError`, which lives in
/// `penguin-bundle-host` and is per-service, not per-crate) so this
/// crate's public API has no dependency on either stage's wire types;
/// `crate::KvHost`'s callers map this to whatever error shape their own
/// stage needs.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Denied {
    /// Stable, machine-matchable reason (e.g. `"not_granted"`).
    pub code: &'static str,
    pub message: String,
}

/// Per-`app_id` snapshot of manifest-declared capability/permission ids --
/// what [`authorize_kv`] checks instead of hardcoding a grant. Refreshed by
/// each service's own DB-driven `bundle_loader` from
/// `bundle_active_set::ActiveBundleRow::declared_capabilities` (this
/// module's doc). Cheap to read on every host-call (a `RwLock` read guard
/// over a hashmap/hashset lookup, no allocation, no I/O) -- the same shape
/// the standard gate's own `GrantSnapshot` (PR #419 §4/§5.5) will use once
/// it replaces this interim version.
/// **Keyed by `app_id` alone, not `(tenant, community, app_id)`** -- the
/// same single-key convention `core/svc_action::distribution::
/// BundleCatalog` already uses for its own per-app egress/secret_refs
/// snapshot (that module's own doc: "keyed by `app_id`"). A single
/// `app_id` installed with genuinely different declared capabilities in
/// two different tenants sharing one process would collide here exactly
/// like it already would in `BundleCatalog` -- an existing, accepted
/// limitation of this codebase's per-process bundle-snapshot pattern, not
/// a new gap introduced by this type.
#[derive(Default)]
pub struct CapabilitySnapshot {
    declared: RwLock<HashMap<String, HashSet<String>>>,
}

impl CapabilitySnapshot {
    pub fn new() -> Self {
        Self::default()
    }

    /// Replaces `app_id`'s declared-capability set wholesale -- called once
    /// per active `app_id` per poll tick (or push-invalidation) by each
    /// stage's `bundle_loader`. Never merges with a prior set: a
    /// capability dropped from a new manifest version must disappear here
    /// too, not linger from a stale entry.
    pub fn update(
        &self,
        app_id: impl Into<String>,
        capabilities: impl IntoIterator<Item = String>,
    ) {
        let mut guard = self
            .declared
            .write()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        guard.insert(app_id.into(), capabilities.into_iter().collect());
    }

    /// Removes `app_id` entirely -- called when a bundle is unloaded/
    /// deactivated, so a stale grant can never outlive the bundle's own
    /// active lifetime in this snapshot.
    pub fn remove(&self, app_id: &str) {
        self.declared
            .write()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .remove(app_id);
    }

    /// `true` only if `app_id` has a snapshot entry **and** that entry
    /// contains `permission`. An `app_id` this snapshot has never been
    /// told about is `false`, never a default grant -- "undeclared means
    /// denied" holds both for a known app that omitted the permission and
    /// for an app this snapshot has no record of at all.
    pub fn declares(&self, app_id: &str, permission: &str) -> bool {
        self.declared
            .read()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .get(app_id)
            .is_some_and(|caps| caps.contains(permission))
    }
}

/// Decides whether `scope.app_id` may use the `kv` capability, per
/// `snapshot`'s currently-declared set. See the module doc for where
/// `snapshot` comes from and why an unknown `app_id` denies exactly like a
/// known app that omitted `storage.kv`.
///
/// # Errors
/// Returns [`Denied`] (`code: "not_granted"`) if `storage.kv` is not
/// declared for this scope's `app_id`. Never panics.
pub fn authorize_kv(scope: &KvScope, snapshot: &CapabilitySnapshot) -> Result<(), Denied> {
    if snapshot.declares(&scope.app_id, KV_PERMISSION_ID) {
        return Ok(());
    }

    crate::metrics::record_authorize_denied(&scope.app_id, KV_PERMISSION_ID);
    tracing::debug!(
        tenant = %scope.tenant,
        community = scope.community.as_deref().unwrap_or(""),
        app_id = %scope.app_id,
        permission = KV_PERMISSION_ID,
        "kv capability denied: not declared in this app version's approved manifest capabilities"
    );
    Err(Denied {
        code: "not_granted",
        message: format!("{KV_PERMISSION_ID} is not declared for this app version"),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn snapshot_granting(app_id: &str) -> CapabilitySnapshot {
        let snapshot = CapabilitySnapshot::new();
        snapshot.update(app_id, [KV_PERMISSION_ID.to_string()]);
        snapshot
    }

    #[test]
    fn authorize_kv_grants_when_the_app_declares_storage_kv() {
        let scope = KvScope::new("acme", Some("main".to_string()), "waddles.bot.a");
        let snapshot = snapshot_granting("waddles.bot.a");
        assert_eq!(authorize_kv(&scope, &snapshot), Ok(()));
    }

    #[test]
    fn authorize_kv_denies_when_the_app_never_declared_storage_kv() {
        let scope = KvScope::new("acme", Some("main".to_string()), "waddles.bot.a");
        let snapshot = CapabilitySnapshot::new();
        snapshot.update("waddles.bot.a", ["context".to_string(), "log".to_string()]);
        let err = authorize_kv(&scope, &snapshot).unwrap_err();
        assert_eq!(err.code, "not_granted");
    }

    #[test]
    fn authorize_kv_denies_an_app_id_the_snapshot_has_never_seen() {
        let scope = KvScope::new("acme", Some("main".to_string()), "waddles.bot.unknown");
        let snapshot = snapshot_granting("waddles.bot.a");
        let err = authorize_kv(&scope, &snapshot).unwrap_err();
        assert_eq!(err.code, "not_granted");
    }

    #[test]
    fn authorize_kv_denies_against_a_completely_empty_snapshot() {
        // The legacy env-driven path's exact shape: no active-set row, no
        // consent record, so the snapshot is never populated at all.
        let scope = KvScope::new("acme", None, "waddles.bot.a");
        let snapshot = CapabilitySnapshot::new();
        let err = authorize_kv(&scope, &snapshot).unwrap_err();
        assert_eq!(err.code, "not_granted");
    }

    #[test]
    fn capability_snapshot_update_replaces_rather_than_merges() {
        let snapshot = CapabilitySnapshot::new();
        snapshot.update("app", ["storage.kv".to_string(), "http".to_string()]);
        assert!(snapshot.declares("app", "storage.kv"));
        snapshot.update("app", ["http".to_string()]);
        assert!(
            !snapshot.declares("app", "storage.kv"),
            "a fresh update must fully replace the prior set, not merge into it"
        );
    }

    #[test]
    fn capability_snapshot_remove_clears_the_entry() {
        let snapshot = snapshot_granting("app");
        assert!(snapshot.declares("app", KV_PERMISSION_ID));
        snapshot.remove("app");
        assert!(!snapshot.declares("app", KV_PERMISSION_ID));
    }

    #[test]
    fn permission_id_matches_the_agreed_identifier() {
        assert_eq!(KV_PERMISSION_ID, "storage.kv");
    }
}
