//! Gate tests for the platform-op families (`moderation.*`, `chat.delete:*`, `message.*`,
//! `commands.register:*`, `eventsub.manage:*`, `interaction.respond:*`): unapproved denied,
//! approved allowed only inside the granted (tenant, community, app) scope, grants are exact
//! ids (a platform's grant never authorizes another platform's op), and revocation works.

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

const IDS: [&str; 7] = [
    "moderation.twitch",
    "chat.delete:youtube",
    "message.edit:discord",
    "message.reaction:discord",
    "commands.register:discord",
    "eventsub.manage:twitch",
    "interaction.respond:discord",
];

#[test]
fn every_platform_op_family_is_denied_until_approved() {
    let snap = Arc::new(InMemoryGrantSnapshot::new());
    let gate = gate_with(&snap);
    for id in IDS {
        assert_eq!(
            call(&gate, &scope(7, 3), id),
            Err(Denied::NotGranted),
            "{id}"
        );
    }
}

#[test]
fn approved_platform_ops_are_scoped_to_their_tenant_community_and_exact_id() {
    let snap = Arc::new(InMemoryGrantSnapshot::new());
    grant(&snap, &scope(7, 3), &IDS);
    let gate = gate_with(&snap);
    for id in IDS {
        assert_eq!(call(&gate, &scope(7, 3), id), Ok(()), "{id}");
        assert_eq!(
            call(&gate, &scope(7, 4), id),
            Err(Denied::NotGranted),
            "{id}"
        );
        assert_eq!(
            call(&gate, &scope(8, 3), id),
            Err(Denied::NotGranted),
            "{id}"
        );
    }
    // A grant on one platform never authorizes the same family on another.
    for other in [
        "moderation.discord",
        "chat.delete:twitch",
        "message.edit:twitch",
        "eventsub.manage:discord",
        "interaction.respond:twitch",
        "commands.register:twitch",
    ] {
        assert_eq!(
            call(&gate, &scope(7, 3), other),
            Err(Denied::NotGranted),
            "{other}"
        );
    }
}

#[test]
fn revoking_a_platform_op_grant_takes_effect() {
    let snap = Arc::new(InMemoryGrantSnapshot::new());
    grant(&snap, &scope(7, 3), &["eventsub.manage:twitch"]);
    let gate = gate_with(&snap);
    assert_eq!(call(&gate, &scope(7, 3), "eventsub.manage:twitch"), Ok(()));
    grant(&snap, &scope(7, 3), &[]);
    assert_eq!(
        call(&gate, &scope(7, 3), "eventsub.manage:twitch"),
        Err(Denied::NotGranted)
    );
}
