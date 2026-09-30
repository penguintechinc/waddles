//! The verified-manifest interface the per-component `Linker`
//! (`crate::engine::build_linker_for`/`LinkerCache`) codes against.
//!
//! `docs/superpowers/specs/2026-09-28-connector-bundles.md` S3.2.1 gate 1
//! is Ed25519 artifact-signature verification (PR #431,
//! `feature/bundle-artifact-signing`) that must run BEFORE a `Linker` is
//! ever constructed for a component. That branch is not yet merged into
//! this branch's base (`release/v3.0.X`) at the time of this task, so per
//! this task's own instruction this module is the seam: [`VerifiedManifest`]
//! is what gate 1's output looks like from gate 2's (this module's
//! `Linker`-isolation) point of view. Once PR #431 lands, its signature
//! verification becomes the sole producer of this type in the real load
//! path (`crate::invoke`) -- gate 2 below does not change.

use std::collections::HashSet;

/// The WIT world a signed manifest declares (spec S3.2.1 gate 1: "a
/// vendor-submitted component is never signed under a manifest declaring
/// `wit-world: connector`"). `StageV1_1` is reserved for when
/// `docs/wit-stage-v1-1-design.md`'s own per-component Linker isolation
/// lands; only `Stage`/`Connector` are wired by this task.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum WitWorld {
    Stage,
    Connector,
}

/// Namespace prefix reserved for first-party core bundles (mirrors PR #419
/// S1.1's `CORE_NAMESPACE_PREFIX` for `storage.tables`, applied here to the
/// `connector` world per this design's S3.1/S3.2 "no exceptions" carve-out:
/// `connector.*`/`connector.pii.read` are never grantable to a non-core
/// `app_id`).
pub const CORE_NAMESPACE_PREFIX: &str = "waddles.core.";

/// Gates `identity.lookup` (spec S1, S3.1, S3.2.1 gate 2).
pub const PERM_CONNECTOR_PII_READ: &str = "connector.pii.read";

/// The already-signature-verified permission set for one component
/// instantiation -- see this module's doc comment for what "verified"
/// means today vs. once PR #431 lands.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VerifiedManifest {
    pub app_id: String,
    pub digest: String,
    pub world: WitWorld,
    pub granted_permissions: HashSet<String>,
}

impl VerifiedManifest {
    #[must_use]
    pub fn new(
        app_id: impl Into<String>,
        digest: impl Into<String>,
        world: WitWorld,
        granted_permissions: HashSet<String>,
    ) -> Self {
        Self {
            app_id: app_id.into(),
            digest: digest.into(),
            world,
            granted_permissions,
        }
    }

    #[must_use]
    pub fn is_core_namespace(&self) -> bool {
        self.app_id.starts_with(CORE_NAMESPACE_PREFIX)
    }

    #[must_use]
    pub fn has_permission(&self, id: &str) -> bool {
        self.granted_permissions.contains(id)
    }

    /// Spec S3.2.1 gate 2: `identity.lookup` links only for a
    /// `connector`-world component that is BOTH first-party core-namespaced
    /// AND was granted `connector.pii.read`. Either condition failing means
    /// `crate::engine::build_linker_for` never registers `identity.lookup`
    /// at all -- not merely a call that returns `denied` (the exact
    /// distinction spec S3.2.1's test requirement draws).
    #[must_use]
    pub fn may_link_identity(&self) -> bool {
        self.world == WitWorld::Connector
            && self.is_core_namespace()
            && self.has_permission(PERM_CONNECTOR_PII_READ)
    }

    /// Cache key for `crate::engine::LinkerCache`'s per-(digest,
    /// permission-set) cache (task requirement: "linking is cached per
    /// (digest, permission-set)"). Permissions are sorted so two manifests
    /// granting the identical set in a different iteration order collide on
    /// the same key rather than diverging.
    #[must_use]
    pub fn linker_cache_key(&self) -> LinkerCacheKey {
        let mut permissions: Vec<String> = self.granted_permissions.iter().cloned().collect();
        permissions.sort();
        LinkerCacheKey {
            digest: self.digest.clone(),
            world: self.world,
            permissions,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct LinkerCacheKey {
    digest: String,
    world: WitWorld,
    permissions: Vec<String>,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn perms(ids: &[&str]) -> HashSet<String> {
        ids.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn core_connector_with_grant_may_link_identity() {
        let m = VerifiedManifest::new(
            "waddles.core.connector.discord",
            "sha256:aa",
            WitWorld::Connector,
            perms(&[PERM_CONNECTOR_PII_READ]),
        );
        assert!(m.may_link_identity());
    }

    #[test]
    fn vendor_namespace_never_links_identity_even_if_granted() {
        // Defense in depth: gate 1 (artifact signing) should already
        // prevent this manifest from ever being signed, but gate 2 must
        // independently refuse it too (spec S3.2.1 "why both, not just
        // one").
        let m = VerifiedManifest::new(
            "vendor.acme.connector.discord",
            "sha256:bb",
            WitWorld::Connector,
            perms(&[PERM_CONNECTOR_PII_READ]),
        );
        assert!(!m.may_link_identity());
    }

    #[test]
    fn core_connector_without_grant_never_links_identity() {
        let m = VerifiedManifest::new(
            "waddles.core.connector.discord",
            "sha256:cc",
            WitWorld::Connector,
            perms(&["connector.receive:discord"]),
        );
        assert!(!m.may_link_identity());
    }

    #[test]
    fn stage_world_never_links_identity_regardless_of_grants() {
        let m = VerifiedManifest::new(
            "waddles.core.some-app",
            "sha256:dd",
            WitWorld::Stage,
            perms(&[PERM_CONNECTOR_PII_READ]),
        );
        assert!(!m.may_link_identity());
    }

    #[test]
    fn cache_key_is_stable_under_permission_iteration_order() {
        let a = VerifiedManifest::new(
            "waddles.core.x",
            "sha256:ee",
            WitWorld::Connector,
            perms(&["b", "a"]),
        );
        let b = VerifiedManifest::new(
            "waddles.core.x",
            "sha256:ee",
            WitWorld::Connector,
            perms(&["a", "b"]),
        );
        assert_eq!(a.linker_cache_key(), b.linker_cache_key());
    }

    #[test]
    fn cache_key_diverges_on_permission_set_or_digest() {
        let base = VerifiedManifest::new(
            "waddles.core.x",
            "sha256:ff",
            WitWorld::Connector,
            perms(&["a"]),
        );
        let diff_perms = VerifiedManifest::new(
            "waddles.core.x",
            "sha256:ff",
            WitWorld::Connector,
            perms(&["a", "b"]),
        );
        let diff_digest = VerifiedManifest::new(
            "waddles.core.x",
            "sha256:00",
            WitWorld::Connector,
            perms(&["a"]),
        );
        assert_ne!(base.linker_cache_key(), diff_perms.linker_cache_key());
        assert_ne!(base.linker_cache_key(), diff_digest.linker_cache_key());
    }
}
