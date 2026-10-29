//! Gate tests for the `tool.call` family (bundle-MCP-tools P2).

use std::collections::HashMap;
use std::sync::Arc;

use crate::denied::Denied;
use crate::gate::CapabilityGate;
use crate::grant::{GrantSet, GrantedPermission, InMemoryGrantSnapshot};
use crate::instance_policy::InMemoryInstancePolicySnapshot;
use crate::membership::InMemoryMembership;
use crate::permission::PermissionId;
use crate::quota::InMemoryQuotaLedger;
use crate::resource::{
    AppScopedResource, EconomyTarget, ResolvedResource, ResourceRef, ToolAccess, ToolCallTarget,
};
use crate::scope::{GrantScopeKey, HostInvokeScopeBuilder, InvokeScope, TenantTier};

fn scope() -> InvokeScope {
    HostInvokeScopeBuilder::new()
        .tenant_id(7)
        .community_id(3)
        .app_id("waddles.test.tools")
        .app_version(1)
        .tenant_tier(TenantTier::Free)
        .build()
        .unwrap()
}

fn id() -> PermissionId {
    PermissionId::ToolCall {
        target: "svc.target".to_string(),
        tool: "do_thing".to_string(),
    }
}

fn gate(granted: bool) -> CapabilityGate {
    let snapshot = InMemoryGrantSnapshot::new();
    let mut grants = HashMap::new();
    if granted {
        for cid in [id().canonical_id(), "storage.kv".to_string()] {
            grants.insert(
                cid.clone(),
                GrantedPermission {
                    permission_id: cid,
                    params: serde_json::json!({}),
                },
            );
        }
    }
    snapshot.set(
        GrantScopeKey::from_scope(&scope()),
        GrantSet {
            permission_snapshot_hash: "h".to_string(),
            grants,
        },
    );
    CapabilityGate::new(
        Arc::new(snapshot),
        Arc::new(InMemoryMembership::new()),
        Arc::new(InMemoryQuotaLedger::new()),
        Arc::new(InMemoryInstancePolicySnapshot::new()),
    )
}

fn target() -> ToolCallTarget {
    ToolCallTarget {
        target_app_id: "svc.target".to_string(),
        tool: "do_thing".to_string(),
        access: ToolAccess::Write,
    }
}

#[test]
fn authorize_tool_granted_is_allowed() {
    let call = gate(true)
        .authorize(&scope(), id(), ResourceRef::ToolScoped(target()))
        .unwrap();
    assert_eq!(call.resource, ResolvedResource::ToolTarget(target()));
}

#[test]
fn authorize_tool_not_granted_is_denied() {
    let err = gate(false)
        .authorize(&scope(), id(), ResourceRef::ToolScoped(target()))
        .unwrap_err();
    assert_eq!(err, Denied::NotGranted);
}

#[test]
fn authorize_tool_with_economy_ref_is_scope_mismatch() {
    let eco = ResourceRef::EconomyScoped(EconomyTarget {
        target_user: None,
        counterparty: None,
        amount: None,
        payout: None,
    });
    let err = gate(true).authorize(&scope(), id(), eco).unwrap_err();
    assert_eq!(err, Denied::ResourceScopeMismatch);
}

#[test]
fn authorize_tool_with_app_scoped_ref_is_scope_mismatch() {
    let err = gate(true)
        .authorize(
            &scope(),
            id(),
            ResourceRef::AppScoped(AppScopedResource::None),
        )
        .unwrap_err();
    assert_eq!(err, Denied::ResourceScopeMismatch);
}

#[test]
fn authorize_tool_ref_with_non_tool_id_is_scope_mismatch() {
    let other = PermissionId::parse("storage.kv").unwrap();
    let err = gate(true)
        .authorize(&scope(), other, ResourceRef::ToolScoped(target()))
        .unwrap_err();
    assert_eq!(err, Denied::ResourceScopeMismatch);
}
