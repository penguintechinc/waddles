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

/// One `(tenant, community, user)` membership fact -- the unit a membership
/// loader (`core/bundle_host_reputation::load_membership`, backed by
/// `community_members`) feeds [`SnapshotMembership::replace_all`].
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct MemberRow {
    pub tenant_id: i32,
    pub community_id: i32,
    pub user: Uuid,
}

#[derive(Default)]
struct MembershipSnapshot {
    community: HashMap<(i32, i32), HashSet<Uuid>>,
    tenant: HashMap<i32, HashSet<Uuid>>,
}

/// The production [`MembershipCheck`]: an atomically swappable, immutable
/// snapshot of active `community_members` rows (spec SS5.5's "one additional
/// hashmap read against a `(community_id) -> HashSet<user_uuid>` membership
/// snapshot"), refreshed by a loader task on the same cadence as the grant
/// cache. Sync and zero-I/O on the hot path.
///
/// **Fail-closed by construction:** a freshly built instance is EMPTY (every
/// user reads as a non-member until a loader populates it), and a poisoned
/// lock reads as "not a member" -- never a default allow. The snapshot is a
/// fast pre-filter only: any capability that WRITES on a target user's behalf
/// (`bundle_host_reputation`'s `adjust`) re-verifies membership inside its own
/// transaction against the live table, so snapshot staleness can delay a
/// removal's effect on reads but can never authorize a write for a departed
/// member.
pub struct SnapshotMembership {
    inner: RwLock<std::sync::Arc<MembershipSnapshot>>,
}

impl Default for SnapshotMembership {
    fn default() -> Self {
        Self::new()
    }
}

impl SnapshotMembership {
    /// An empty (deny-everything) snapshot.
    pub fn new() -> Self {
        Self {
            inner: RwLock::new(std::sync::Arc::new(MembershipSnapshot::default())),
        }
    }

    /// Atomically replaces the whole snapshot with `rows`. A tenant-scope
    /// membership is derived: a user is a tenant member iff they are an
    /// active member of at least one of that tenant's communities. Returns
    /// `false` (leaving the previous snapshot in place) only if the lock is
    /// poisoned.
    pub fn replace_all(&self, rows: impl IntoIterator<Item = MemberRow>) -> bool {
        let mut snap = MembershipSnapshot::default();
        for row in rows {
            snap.community
                .entry((row.tenant_id, row.community_id))
                .or_default()
                .insert(row.user);
            snap.tenant
                .entry(row.tenant_id)
                .or_default()
                .insert(row.user);
        }
        match self.inner.write() {
            Ok(mut guard) => {
                *guard = std::sync::Arc::new(snap);
                true
            }
            Err(_) => false,
        }
    }

    /// Number of distinct `(tenant, community)` pairs currently known.
    pub fn community_count(&self) -> usize {
        self.inner.read().map(|s| s.community.len()).unwrap_or(0)
    }
}

impl MembershipCheck for SnapshotMembership {
    fn is_member(&self, scope: &InvokeScope, target_user: Uuid, scope_kind: ScopeKind) -> bool {
        let Ok(guard) = self.inner.read() else {
            return false;
        };
        match scope_kind {
            ScopeKind::Community => guard
                .community
                .get(&(scope.tenant_id(), scope.community_id()))
                .is_some_and(|set| set.contains(&target_user)),
            ScopeKind::Tenant => guard
                .tenant
                .get(&scope.tenant_id())
                .is_some_and(|set| set.contains(&target_user)),
        }
    }
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

    fn row(t: i32, c: i32, user: Uuid) -> MemberRow {
        MemberRow {
            tenant_id: t,
            community_id: c,
            user,
        }
    }

    #[test]
    fn snapshot_membership_starts_empty_and_denies_everyone() {
        let m = SnapshotMembership::new();
        let user = Uuid::new_v4();
        assert!(!m.is_member(&scope(), user, ScopeKind::Community));
        assert!(!m.is_member(&scope(), user, ScopeKind::Tenant));
    }

    #[test]
    fn snapshot_membership_recognizes_loaded_members_and_derives_tenant_scope() {
        let m = SnapshotMembership::new();
        let user = Uuid::new_v4();
        assert!(m.replace_all([row(7, 3, user)]));
        assert!(m.is_member(&scope(), user, ScopeKind::Community));
        assert!(m.is_member(&scope(), user, ScopeKind::Tenant));
        assert_eq!(m.community_count(), 1);
    }

    #[test]
    fn snapshot_membership_does_not_cross_communities_or_tenants() {
        let m = SnapshotMembership::new();
        let user = Uuid::new_v4();
        // Same community id in a different tenant; and a different community
        // in the same tenant.
        assert!(m.replace_all([row(999, 3, user), row(7, 4, user)]));
        assert!(!m.is_member(&scope(), user, ScopeKind::Community));
        // ...but the same-tenant other-community row does make them a tenant member.
        assert!(m.is_member(&scope(), user, ScopeKind::Tenant));
    }

    #[test]
    fn snapshot_membership_replace_all_drops_departed_members() {
        let m = SnapshotMembership::new();
        let stays = Uuid::new_v4();
        let leaves = Uuid::new_v4();
        m.replace_all([row(7, 3, stays), row(7, 3, leaves)]);
        assert!(m.is_member(&scope(), leaves, ScopeKind::Community));
        m.replace_all([row(7, 3, stays)]);
        assert!(!m.is_member(&scope(), leaves, ScopeKind::Community));
        assert!(m.is_member(&scope(), stays, ScopeKind::Community));
    }

    #[test]
    fn snapshot_membership_is_fail_closed_when_the_lock_is_poisoned() {
        let m = std::sync::Arc::new(SnapshotMembership::new());
        let user = Uuid::new_v4();
        m.replace_all([row(7, 3, user)]);
        let m2 = std::sync::Arc::clone(&m);
        let _ = std::thread::spawn(move || {
            let _guard = m2.inner.write().unwrap();
            panic!("poison the membership lock");
        })
        .join();
        assert!(!m.is_member(&scope(), user, ScopeKind::Community));
        assert!(!m.replace_all([row(7, 3, user)]));
        assert_eq!(m.community_count(), 0);
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
