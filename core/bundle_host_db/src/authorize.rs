//! The single seam that decides whether the `db` capability is granted for
//! one invocation's app -- mirrors `core/bundle_host_kv::authorize`
//! byte-for-byte in structure (see that module's doc for the full
//! rationale, condensed here for `db`).
//!
//! **Interim implementation.** The standard permission gate
//! (`core/bundle_capability_gate::authorize`, PR #428) is not merged yet.
//! Until it lands, [`authorize_db`] grants `db` **only if the invoking
//! app's currently-active, currently-approved manifest version declares
//! [`DB_PERMISSION_ID`]** in [`CapabilitySnapshot`] -- **undeclared means
//! denied**. An `app_id` this snapshot has never heard of (including every
//! deployment before its own `bundle_loader` wiring lands, see
//! `crate::schema`'s doc) denies exactly like a known app that omitted the
//! permission -- fail closed, never fail open.

use std::collections::{HashMap, HashSet};
use std::sync::RwLock;

use crate::scope::DbScope;

/// The permission id `db`/`tables.*` is known by in the standard
/// permission model, and the label value this crate attaches to every log
/// line and OTel metric it emits.
pub const DB_PERMISSION_ID: &str = "storage.tables";

/// One denied authorization decision.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Denied {
    pub code: &'static str,
    pub message: String,
}

/// Per-`app_id` snapshot of manifest-declared capability/permission ids.
/// Refreshed by each service's own DB-driven `bundle_loader` (mirrors
/// `bundle_host_kv::authorize::CapabilitySnapshot` exactly).
#[derive(Default)]
pub struct CapabilitySnapshot {
    declared: RwLock<HashMap<String, HashSet<String>>>,
}

impl CapabilitySnapshot {
    pub fn new() -> Self {
        Self::default()
    }

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

    pub fn remove(&self, app_id: &str) {
        self.declared
            .write()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .remove(app_id);
    }

    pub fn declares(&self, app_id: &str, permission: &str) -> bool {
        self.declared
            .read()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .get(app_id)
            .is_some_and(|caps| caps.contains(permission))
    }
}

/// Decides whether `scope.app_id` may use the `db` capability, per
/// `snapshot`'s currently-declared set.
///
/// # Errors
/// Returns [`Denied`] (`code: "not_granted"`) if `storage.tables` is not
/// declared for this scope's `app_id`. Never panics.
pub fn authorize_db(scope: &DbScope, snapshot: &CapabilitySnapshot) -> Result<(), Denied> {
    if snapshot.declares(&scope.app_id, DB_PERMISSION_ID) {
        return Ok(());
    }

    crate::metrics::record_authorize_denied(&scope.app_id, DB_PERMISSION_ID);
    tracing::debug!(
        tenant_id = scope.tenant_id,
        community_id = scope.community_id,
        app_id = %scope.app_id,
        permission = DB_PERMISSION_ID,
        "db capability denied: not declared in this app version's approved manifest capabilities"
    );
    Err(Denied {
        code: "not_granted",
        message: format!("{DB_PERMISSION_ID} is not declared for this app version"),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn snapshot_granting(app_id: &str) -> CapabilitySnapshot {
        let snapshot = CapabilitySnapshot::new();
        snapshot.update(app_id, [DB_PERMISSION_ID.to_string()]);
        snapshot
    }

    #[test]
    fn authorize_db_grants_when_the_app_declares_storage_tables() {
        let scope = DbScope::new(7, 3, "waddles.bot.a");
        let snapshot = snapshot_granting("waddles.bot.a");
        assert_eq!(authorize_db(&scope, &snapshot), Ok(()));
    }

    #[test]
    fn authorize_db_denies_when_the_app_never_declared_storage_tables() {
        let scope = DbScope::new(7, 3, "waddles.bot.a");
        let snapshot = CapabilitySnapshot::new();
        snapshot.update("waddles.bot.a", ["context".to_string()]);
        let err = authorize_db(&scope, &snapshot).unwrap_err();
        assert_eq!(err.code, "not_granted");
    }

    #[test]
    fn authorize_db_denies_an_app_id_the_snapshot_has_never_seen() {
        let scope = DbScope::new(7, 3, "waddles.bot.unknown");
        let snapshot = snapshot_granting("waddles.bot.a");
        let err = authorize_db(&scope, &snapshot).unwrap_err();
        assert_eq!(err.code, "not_granted");
    }

    #[test]
    fn authorize_db_denies_against_a_completely_empty_snapshot() {
        let scope = DbScope::new(7, 0, "waddles.bot.a");
        let snapshot = CapabilitySnapshot::new();
        let err = authorize_db(&scope, &snapshot).unwrap_err();
        assert_eq!(err.code, "not_granted");
    }

    #[test]
    fn capability_snapshot_update_replaces_rather_than_merges() {
        let snapshot = CapabilitySnapshot::new();
        snapshot.update("app", ["storage.tables".to_string(), "http".to_string()]);
        assert!(snapshot.declares("app", "storage.tables"));
        snapshot.update("app", ["http".to_string()]);
        assert!(!snapshot.declares("app", "storage.tables"));
    }

    #[test]
    fn capability_snapshot_remove_clears_the_entry() {
        let snapshot = snapshot_granting("app");
        assert!(snapshot.declares("app", DB_PERMISSION_ID));
        snapshot.remove("app");
        assert!(!snapshot.declares("app", DB_PERMISSION_ID));
    }

    #[test]
    fn permission_id_matches_the_agreed_identifier() {
        assert_eq!(DB_PERMISSION_ID, "storage.tables");
    }
}
