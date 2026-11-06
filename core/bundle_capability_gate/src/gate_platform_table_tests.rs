//! Gate tests for the `platform.table.*` families (2026-11-06 spec): unapproved
//! denied, approved allowed only inside the granted (tenant, community, app) scope,
//! grants are exact-id (a widened manifest's new ids stay denied until approved),
//! and revocation (snapshot replaced) takes effect.

use std::collections::HashMap;
use std::sync::Arc;

use crate::denied::Denied;
use crate::gate::CapabilityGate;
use crate::grant::{GrantSet, GrantedPermission, InMemoryGrantSnapshot};
use crate::instance_policy::InMemoryInstancePolicySnapshot;
use crate::membership::InMemoryMembership;
use crate::permission::PermissionId;
use crate::quota::InMemoryQuotaLedger;
use crate::resource::{AppScopedResource, ResourceRef};
use crate::scope::{GrantScopeKey, HostInvokeScopeBuilder, InvokeScope, TenantTier};

fn scope(tenant: i32, community: i32) -> InvokeScope {
    HostInvokeScopeBuilder::new()
        .tenant_id(tenant)
        .community_id(community)
        .app_id("waddles.test.role_sync")
        .app_version(1)
        .tenant_tier(TenantTier::Free)
        .build()
        .unwrap()
}

fn pid(raw: &str) -> PermissionId {
    PermissionId::parse(raw).unwrap()
}

fn gate_with(snapshot: &Arc<InMemoryGrantSnapshot>) -> CapabilityGate {
    CapabilityGate::new(
        snapshot.clone(),
        Arc::new(InMemoryMembership::new()),
        Arc::new(InMemoryQuotaLedger::new()),
        Arc::new(InMemoryInstancePolicySnapshot::new()),
    )
}

fn grant(snapshot: &InMemoryGrantSnapshot, scope: &InvokeScope, ids: &[&str]) {
    let grants: HashMap<String, GrantedPermission> = ids
        .iter()
        .map(|id| {
            (
                id.to_string(),
                GrantedPermission {
                    permission_id: id.to_string(),
                    params: serde_json::json!({}),
                },
            )
        })
        .collect();
    snapshot.set(
        GrantScopeKey::from_scope(scope),
        GrantSet {
            permission_snapshot_hash: "h".to_string(),
            grants,
        },
    );
}

fn call(gate: &CapabilityGate, s: &InvokeScope, raw: &str) -> Result<(), Denied> {
    gate.authorize(s, pid(raw), ResourceRef::AppScoped(AppScopedResource::None))
        .map(|_| ())
}

const WRITE_ROLE: &str = "platform.table.write:community_members.role";
const READ_ROLE: &str = "platform.table.read:community_members.role";

#[test]
fn unapproved_table_access_is_denied() {
    let snap = Arc::new(InMemoryGrantSnapshot::new());
    assert_eq!(
        call(&gate_with(&snap), &scope(7, 3), WRITE_ROLE),
        Err(Denied::NotGranted)
    );
}

#[test]
fn approved_is_allowed_only_in_its_own_tenant_and_community() {
    let snap = Arc::new(InMemoryGrantSnapshot::new());
    grant(&snap, &scope(7, 3), &[WRITE_ROLE]);
    let gate = gate_with(&snap);
    assert_eq!(call(&gate, &scope(7, 3), WRITE_ROLE), Ok(()));
    assert_eq!(
        call(&gate, &scope(7, 4), WRITE_ROLE),
        Err(Denied::NotGranted)
    );
    assert_eq!(
        call(&gate, &scope(8, 3), WRITE_ROLE),
        Err(Denied::NotGranted)
    );
}

#[test]
fn grants_are_exact_so_widened_ids_stay_denied_until_approved() {
    let snap = Arc::new(InMemoryGrantSnapshot::new());
    grant(&snap, &scope(7, 3), &[READ_ROLE]);
    let gate = gate_with(&snap);
    assert_eq!(call(&gate, &scope(7, 3), READ_ROLE), Ok(()));
    assert_eq!(
        call(&gate, &scope(7, 3), WRITE_ROLE),
        Err(Denied::NotGranted)
    );
}

#[test]
fn revocation_takes_effect() {
    let snap = Arc::new(InMemoryGrantSnapshot::new());
    grant(&snap, &scope(7, 3), &[WRITE_ROLE]);
    let gate = gate_with(&snap);
    assert_eq!(call(&gate, &scope(7, 3), WRITE_ROLE), Ok(()));
    grant(&snap, &scope(7, 3), &[]);
    assert_eq!(
        call(&gate, &scope(7, 3), WRITE_ROLE),
        Err(Denied::NotGranted)
    );
}
