//! `ReputationScoped`'s membership check (spec SS5.2, SS5.5): verifies a
//! bundle-named `target_user` actually belongs to the invocation's community
//! or tenant, **at call time**, not just at grant time -- "a user who left
//! the community since the bundle was activated cannot be adjusted or
//! profile-read" (spec SS5.2).
//!
//! Sync and zero-I/O on the hot path, exactly parallel to
//! [`crate::grant::GrantSnapshot`] (spec SS5.5: "one additional hashmap read
//! against a `(community_id) -> HashSet<user_uuid>` membership snapshot").
//! A real implementation backed by the same push-invalidated cadence as the
//! grant cache is a later, data-plane integration task -- this crate ships
//! the trait plus [`InMemoryMembership`] for tests.

use std::collections::{HashMap, HashSet};
use std::sync::RwLock;

use uuid::Uuid;

use crate::resource::ScopeKind;
use crate::scope::InvokeScope;

pub trait MembershipCheck: Send + Sync {
    /// `true` iff `target_user` belongs to `scope`'s community (`scope_kind
    /// == Community`) or tenant (`scope_kind == Tenant`) *right now* -- never
    /// cached across the lifetime of a single `authorize()` call beyond what
    /// the implementation's own push-invalidated snapshot already provides.
    fn is_member(&self, scope: &InvokeScope, target_user: Uuid, scope_kind: ScopeKind) -> bool;
}

/// A settable, in-memory [`MembershipCheck`] for tests.
#[derive(Default)]
pub struct InMemoryMembership {
    community_members: RwLock<HashMap<(i32, i32), HashSet<Uuid>>>,
    tenant_members: RwLock<HashMap<i32, HashSet<Uuid>>>,
}

impl InMemoryMembership {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn add_community_member(&self, tenant_id: i32, community_id: i32, user: Uuid) {
        self.community_members
            .write()
            .expect("lock poisoned")
            .entry((tenant_id, community_id))
            .or_default()
            .insert(user);
    }

    pub fn add_tenant_member(&self, tenant_id: i32, user: Uuid) {
        self.tenant_members
            .write()
            .expect("lock poisoned")
            .entry(tenant_id)
            .or_default()
            .insert(user);
    }

    pub fn remove_community_member(&self, tenant_id: i32, community_id: i32, user: Uuid) {
        if let Some(set) = self
            .community_members
            .write()
            .expect("lock poisoned")
            .get_mut(&(tenant_id, community_id))
        {
            set.remove(&user);
        }
    }
}

impl MembershipCheck for InMemoryMembership {
    fn is_member(&self, scope: &InvokeScope, target_user: Uuid, scope_kind: ScopeKind) -> bool {
        match scope_kind {
            ScopeKind::Community => self
                .community_members
                .read()
                .expect("lock poisoned")
                .get(&(scope.tenant_id(), scope.community_id()))
                .is_some_and(|set| set.contains(&target_user)),
            ScopeKind::Tenant => self
                .tenant_members
                .read()
                .expect("lock poisoned")
                .get(&scope.tenant_id())
                .is_some_and(|set| set.contains(&target_user)),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::scope::{HostInvokeScopeBuilder, TenantTier};

    fn scope() -> InvokeScope {
        HostInvokeScopeBuilder::new()
            .tenant_id(7)
            .community_id(3)
            .app_id("waddles.core.example_echo")
            .app_version(1)
            .tenant_tier(TenantTier::Free)
            .build()
            .unwrap()
    }

    #[test]
    fn community_member_is_recognized() {
        let membership = InMemoryMembership::new();
        let user = Uuid::new_v4();
        membership.add_community_member(7, 3, user);
        assert!(membership.is_member(&scope(), user, ScopeKind::Community));
    }

    #[test]
    fn a_user_never_added_is_not_a_member() {
        let membership = InMemoryMembership::new();
        let user = Uuid::new_v4();
        assert!(!membership.is_member(&scope(), user, ScopeKind::Community));
    }

    /// "left the community since the bundle was activated" (spec SS5.2) --
    /// membership must be re-checked at call time, not cached from an
    /// earlier positive result.
    #[test]
    fn a_removed_member_is_no_longer_a_member() {
        let membership = InMemoryMembership::new();
        let user = Uuid::new_v4();
        membership.add_community_member(7, 3, user);
        assert!(membership.is_member(&scope(), user, ScopeKind::Community));

        membership.remove_community_member(7, 3, user);

        assert!(!membership.is_member(&scope(), user, ScopeKind::Community));
    }

    #[test]
    fn community_membership_does_not_imply_tenant_membership() {
        let membership = InMemoryMembership::new();
        let user = Uuid::new_v4();
        membership.add_community_member(7, 3, user);
        assert!(!membership.is_member(&scope(), user, ScopeKind::Tenant));
    }

    /// Cross-tenant regression: a user belonging to a *different* tenant's
    /// identically-numbered community must never be treated as a member.
    #[test]
    fn membership_is_not_confused_across_a_different_tenant() {
        let membership = InMemoryMembership::new();
        let user = Uuid::new_v4();
        membership.add_community_member(999, 3, user); // different tenant, same community_id
        assert!(!membership.is_member(&scope(), user, ScopeKind::Community));
    }
}
