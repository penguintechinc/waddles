//! The per-`app_id` bundle snapshot [`crate::egress::EgressGuard`] consults
//! for a bundle's `egress` allowlist/rate limit and
//! [`crate::capabilities`]'s `secret_refs` grants.
//!
//! **Retired (2026-09-27): the `GET /api/v1/distribution/bundles?stage=action`
//! poll that used to populate this catalog (`fetch_bundles`/`run_poll_loop`,
//! spec §6.7) has been removed** -- superseded by the DB-driven active-bundle
//! loader (`crate::bundle_loader`, `core/bundle_active_set`), which is now
//! mutually exclusive with the legacy `ACTION_BUNDLE_*` env override
//! (`crate::lib::resolve_db_path_active`'s doc). Neither path writes into
//! this catalog today -- [`BundleCatalog`] and [`BundleRow`] remain only
//! because [`crate::egress::EgressGuard`] and
//! [`crate::capabilities::StageCapabilities`] are still wired against this
//! type; a bundle's egress allowlist is an honest, documented seam (empty
//! catalog -> no allowlist row -> every `http.send` call is refused, the
//! same fail-closed posture an unreachable hub-api already produced before
//! this poll was retired) until a future landing wires a real writer (most
//! likely the DB-driven loader, `core/bundle_active_set::ActiveBundleRow`
//! carrying its own egress/secret_refs columns).

use std::collections::HashMap;
use std::sync::RwLock;

/// One bundle's resolved dispatch-relevant state: the `egress` allowlist
/// [`crate::egress::EgressGuard`] enforces and the activation-config-derived
/// `secret_refs` grants [`crate::capabilities`] consults. `(host_pattern,
/// methods)` -- byte-identical shape to
/// `penguin_bundle_host::manifest::Manifest::egress`.
#[derive(Debug, Clone, PartialEq)]
pub struct BundleRow {
    pub app_id: String,
    pub version: String,
    /// `sha256:<64 hex>`, or `None` for a registration with no compiled
    /// artifact yet.
    pub artifact_digest: Option<String>,
    pub component_key: String,
    pub sidecar_key: String,
    pub egress: Vec<(String, Vec<String>)>,
    pub egress_rps: Option<u32>,
    pub config_json: String,
    /// Symbolic secret-reference name -> the actual environment variable
    /// name it resolves to (spec §8.3: "an environment-variable *name* held
    /// in the activation config, resolved at call time"; mirrors
    /// `waddle_transports.signing.resolve_secret`'s Python precedent, where
    /// `secret_ref` is likewise sourced from trusted `config`, never from
    /// bundle-runtime-supplied `payload`). [`crate::egress::EgressGuard`]
    /// validates a bundle-supplied `secret_refs` symbolic name against this
    /// map before resolving anything from the process environment: a name
    /// that is not a key here is refused (`secret_not_granted`), which is
    /// what makes arbitrary-env-var-name injection via `http.send`
    /// impossible even though the bundle picks the symbolic name per call
    /// (security review finding, post-M3-capabilities landing).
    pub granted_secret_refs: HashMap<String, String>,
}

/// The latest-known-good bundle snapshot, keyed by `app_id` -- consulted by
/// [`crate::egress::EgressGuard`] (the `http` capability's allowlist). See
/// this module's doc for why nothing currently writes to it in production.
#[derive(Default)]
pub struct BundleCatalog {
    rows: RwLock<HashMap<String, BundleRow>>,
}

impl BundleCatalog {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn update(&self, rows: Vec<BundleRow>) {
        let mut guard = self.rows.write().unwrap_or_else(|e| e.into_inner());
        for row in rows {
            guard.insert(row.app_id.clone(), row);
        }
    }

    pub fn get(&self, app_id: &str) -> Option<BundleRow> {
        self.rows
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .get(app_id)
            .cloned()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bundle_catalog_get_returns_none_before_any_update() {
        let catalog = BundleCatalog::new();
        assert!(catalog.get("waddles.a.b.c").is_none());
    }

    #[test]
    fn bundle_catalog_update_then_get_round_trips() {
        let catalog = BundleCatalog::new();
        let row = BundleRow {
            app_id: "waddles.a.b.c".to_string(),
            version: "1.0.0".to_string(),
            artifact_digest: Some("sha256:00".to_string()),
            component_key: "k".to_string(),
            sidecar_key: "s".to_string(),
            egress: vec![],
            egress_rps: None,
            config_json: "{}".to_string(),
            granted_secret_refs: HashMap::new(),
        };
        catalog.update(vec![row.clone()]);
        assert_eq!(catalog.get("waddles.a.b.c"), Some(row));
    }

    #[test]
    fn bundle_catalog_update_replaces_the_row_for_the_same_app_id() {
        let catalog = BundleCatalog::new();
        let mut row = BundleRow {
            app_id: "waddles.a.b.c".to_string(),
            version: "1.0.0".to_string(),
            artifact_digest: Some("sha256:00".to_string()),
            component_key: "k".to_string(),
            sidecar_key: "s".to_string(),
            egress: vec![],
            egress_rps: None,
            config_json: "{}".to_string(),
            granted_secret_refs: HashMap::new(),
        };
        catalog.update(vec![row.clone()]);
        row.artifact_digest = Some("sha256:11".to_string());
        catalog.update(vec![row.clone()]);
        assert_eq!(
            catalog.get("waddles.a.b.c").unwrap().artifact_digest,
            Some("sha256:11".to_string())
        );
    }
}
