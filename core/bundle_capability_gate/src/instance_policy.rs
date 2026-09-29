//! Instance-wide permission policy (spec: instance policy, above the 3
//! consent tiers) -- a GLOBAL admin can allow/deny a permission TYPE
//! (`PermissionFamily`) instance-wide, applying to every bundle/tenant/
//! community regardless of its own grant. [`InstancePolicySnapshot`] is the
//! sync, zero-I/O hot-path read [`crate::gate::CapabilityGate::authorize`]
//! consults -- the same shape as [`crate::grant::GrantSnapshot`], versioned
//! and invalidated the same way (part of the same push-invalidation/poll-
//! fallback machinery, spec SS4), so a policy change is visible to the next
//! `authorize()` call with the same staleness bound as a grant change.
//!
//! `net.http.private-ip` is deny-by-default (2026-09-28 decision) --
//! [`InMemoryInstancePolicySnapshot::new`] seeds exactly that, mirroring
//! hub-api's own `_DEFAULT_DENIED_FAMILIES` static fallback
//! (`bundle_instance_policy_service.py`) so an empty/fresh snapshot on
//! either side of the stack fails closed identically.

use std::collections::HashMap;
use std::sync::RwLock;

use crate::permission::PermissionFamily;

/// One family's instance-wide policy action.
#[derive(Copy, Clone, Debug, PartialEq, Eq, Hash)]
pub enum InstanceAction {
    Allow,
    Deny,
}

/// The sync, zero-I/O read path `authorize()` consults for every call (spec:
/// instance policy). A family with no explicit row falls back to whatever
/// the implementation's own static default says -- see
/// [`InMemoryInstancePolicySnapshot`] for the one this crate ships.
pub trait InstancePolicySnapshot: Send + Sync {
    fn action(&self, family: PermissionFamily) -> InstanceAction;
}

/// Fail-closed static default -- mirrors hub-api's `_DEFAULT_DENIED_
/// FAMILIES`. Consulted only for a family with no explicit row.
fn default_action(family: PermissionFamily) -> InstanceAction {
    match family {
        PermissionFamily::NetHttpPrivateIp => InstanceAction::Deny,
        _ => InstanceAction::Allow,
    }
}

/// An in-memory, RwLock-guarded [`InstancePolicySnapshot`] -- the real
/// implementation (kept fresh by the same push-invalidation/poll-fallback
/// path `GrantCache` uses, spec SS4) is a later data-plane integration task,
/// same as [`crate::grant::GrantCache`]'s own real loader.
pub struct InMemoryInstancePolicySnapshot {
    overrides: RwLock<HashMap<PermissionFamily, InstanceAction>>,
}

impl Default for InMemoryInstancePolicySnapshot {
    fn default() -> Self {
        Self::new()
    }
}

impl InMemoryInstancePolicySnapshot {
    /// No explicit overrides -- every family reads through [`default_action`],
    /// so `net.http.private-ip` is already deny-by-default with zero setup,
    /// exactly like a freshly-migrated hub-api instance.
    pub fn new() -> Self {
        Self {
            overrides: RwLock::new(HashMap::new()),
        }
    }

    /// Sets an explicit instance-wide action for `family`, overriding
    /// [`default_action`] -- the Rust-side mirror of hub-api's
    /// `set_instance_policy()` cascading through to this snapshot via
    /// push-invalidation.
    pub fn set(&self, family: PermissionFamily, action: InstanceAction) {
        self.overrides
            .write()
            .expect("lock poisoned")
            .insert(family, action);
    }
}

impl InstancePolicySnapshot for InMemoryInstancePolicySnapshot {
    fn action(&self, family: PermissionFamily) -> InstanceAction {
        self.overrides
            .read()
            .expect("lock poisoned")
            .get(&family)
            .copied()
            .unwrap_or_else(|| default_action(family))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn private_ip_is_denied_by_default_with_no_overrides() {
        let snapshot = InMemoryInstancePolicySnapshot::new();
        assert_eq!(
            snapshot.action(PermissionFamily::NetHttpPrivateIp),
            InstanceAction::Deny
        );
    }

    #[test]
    fn fqdn_and_public_ip_are_allowed_by_default() {
        let snapshot = InMemoryInstancePolicySnapshot::new();
        assert_eq!(
            snapshot.action(PermissionFamily::NetHttpFqdn),
            InstanceAction::Allow
        );
        assert_eq!(
            snapshot.action(PermissionFamily::NetHttpPublicIp),
            InstanceAction::Allow
        );
    }

    #[test]
    fn global_admin_override_takes_precedence_over_the_static_default() {
        let snapshot = InMemoryInstancePolicySnapshot::new();
        snapshot.set(PermissionFamily::NetHttpPrivateIp, InstanceAction::Allow);
        assert_eq!(
            snapshot.action(PermissionFamily::NetHttpPrivateIp),
            InstanceAction::Allow
        );
    }

    #[test]
    fn an_override_can_also_deny_a_normally_allowed_family() {
        let snapshot = InMemoryInstancePolicySnapshot::new();
        snapshot.set(PermissionFamily::StorageObjects, InstanceAction::Deny);
        assert_eq!(
            snapshot.action(PermissionFamily::StorageObjects),
            InstanceAction::Deny
        );
    }
}
