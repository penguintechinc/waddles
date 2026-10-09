//! Gate tests for the `economy.*` families (issue #714): the dedicated
//! `EconomyAmount` quota family, the declared `max_bet`/`max_amount` bound,
//! and call-time membership for every user an economy call names.

use std::collections::HashMap;
use std::sync::Arc;

use uuid::Uuid;

use crate::denied::Denied;
use crate::gate::CapabilityGate;
use crate::grant::{GrantSet, GrantedPermission, InMemoryGrantSnapshot};
use crate::instance_policy::InMemoryInstancePolicySnapshot;
use crate::membership::InMemoryMembership;
use crate::permission::{PermissionFamily, PermissionId, Quota, Risk};
use crate::quota::InMemoryQuotaLedger;
use crate::resource::{
    AppScopedResource, EconomyTarget, ReputationTarget, ResolvedResource, ResourceRef, ScopeKind,
};
use crate::scope::{GrantScopeKey, HostInvokeScopeBuilder, InvokeScope, TenantTier};

fn scope() -> InvokeScope {
    HostInvokeScopeBuilder::new()
        .tenant_id(7)
        .community_id(3)
        .app_id("waddles.test.economy")
        .app_version(1)
        .tenant_tier(TenantTier::Free)
        .build()
        .unwrap()
}

fn grant_set(entries: &[(&str, serde_json::Value)]) -> GrantSet {
    let mut grants = HashMap::new();
    for (id, params) in entries {
        grants.insert(
            (*id).to_string(),
            GrantedPermission {
                permission_id: (*id).to_string(),
                params: params.clone(),
            },
        );
    }
    GrantSet {
        permission_snapshot_hash: "h".to_string(),
        grants,
    }
}

fn gate(entries: &[(&str, serde_json::Value)], members: &[Uuid]) -> CapabilityGate {
    let snapshot = InMemoryGrantSnapshot::new();
    snapshot.set(GrantScopeKey::from_scope(&scope()), grant_set(entries));
    let membership = InMemoryMembership::new();
    for m in members {
        membership.add_community_member(7, 3, *m);
    }
    CapabilityGate::new(
        Arc::new(snapshot),
        Arc::new(membership),
        Arc::new(InMemoryQuotaLedger::new()),
        Arc::new(InMemoryInstancePolicySnapshot::new()),
    )
}

fn wager(user: Uuid, stake: i64) -> ResourceRef {
    ResourceRef::EconomyScoped(EconomyTarget {
        target_user: Some(user),
        counterparty: None,
        amount: Some(stake),
    })
}

fn transfer(from: Uuid, to: Uuid, amount: i64) -> ResourceRef {
    ResourceRef::EconomyScoped(EconomyTarget {
        target_user: Some(from),
        counterparty: Some(to),
        amount: Some(amount),
    })
}

fn read(user: Option<Uuid>) -> ResourceRef {
    ResourceRef::EconomyScoped(EconomyTarget {
        target_user: user,
        counterparty: None,
        amount: None,
    })
}

#[test]
fn economy_quota_is_its_own_family_not_reputations_five_point_caps() {
    let Quota::EconomyAmount {
        per_call_abs_max,
        per_user_daily_abs_max,
        per_scope_daily_abs_max,
    } = PermissionFamily::EconomyWager.catalog_entry().default_quota
    else {
        panic!("economy.wager must carry the EconomyAmount quota");
    };
    // Reputation's ceilings are 5 / 5 / 50: a currency on that scale is unusable.
    assert!(per_call_abs_max > 5 && per_user_daily_abs_max > 5 && per_scope_daily_abs_max > 50);
    assert!(per_call_abs_max <= per_user_daily_abs_max);
    assert!(per_user_daily_abs_max <= per_scope_daily_abs_max);
    for family in [
        PermissionFamily::EconomyWager,
        PermissionFamily::EconomyTransfer,
    ] {
        assert!(matches!(
            family.catalog_entry().default_quota,
            Quota::EconomyAmount { .. }
        ));
        assert_eq!(family.catalog_entry().risk, Risk::Dangerous);
    }
    assert_eq!(
        PermissionFamily::EconomyRead.catalog_entry().risk,
        Risk::Normal
    );
}

#[test]
fn wager_for_a_member_within_bounds_authorizes_and_resolves_the_target() {
    let user = Uuid::new_v4();
    let g = gate(
        &[("economy.wager", serde_json::json!({"max_bet": 100}))],
        &[user],
    );
    let call = g
        .authorize(&scope(), PermissionId::EconomyWager, wager(user, 100))
        .expect("granted");
    assert_eq!(
        call.resource,
        ResolvedResource::EconomyTarget(EconomyTarget {
            target_user: Some(user),
            counterparty: None,
            amount: Some(100),
        })
    );
}

#[test]
fn wager_for_a_non_member_is_denied_user_not_in_scope() {
    let g = gate(&[("economy.wager", serde_json::json!({}))], &[]);
    let err = g
        .authorize(
            &scope(),
            PermissionId::EconomyWager,
            wager(Uuid::new_v4(), 1),
        )
        .unwrap_err();
    assert_eq!(err, Denied::UserNotInScope);
}

#[test]
fn transfer_requires_both_sender_and_recipient_to_be_members() {
    let (a, b) = (Uuid::new_v4(), Uuid::new_v4());
    let only_sender = gate(&[("economy.transfer", serde_json::json!({}))], &[a]);
    assert_eq!(
        only_sender
            .authorize(&scope(), PermissionId::EconomyTransfer, transfer(a, b, 10))
            .unwrap_err(),
        Denied::UserNotInScope
    );
    let only_recipient = gate(&[("economy.transfer", serde_json::json!({}))], &[b]);
    assert_eq!(
        only_recipient
            .authorize(&scope(), PermissionId::EconomyTransfer, transfer(a, b, 10))
            .unwrap_err(),
        Denied::UserNotInScope
    );
    let both = gate(&[("economy.transfer", serde_json::json!({}))], &[a, b]);
    assert!(both
        .authorize(&scope(), PermissionId::EconomyTransfer, transfer(a, b, 10))
        .is_ok());
}

#[test]
fn ungranted_economy_permission_is_denied_not_granted() {
    let user = Uuid::new_v4();
    let g = gate(&[("economy.read", serde_json::json!({}))], &[user]);
    assert_eq!(
        g.authorize(&scope(), PermissionId::EconomyWager, wager(user, 1))
            .unwrap_err(),
        Denied::NotGranted
    );
}

#[test]
fn stake_above_the_declared_max_bet_is_amount_out_of_bounds() {
    let user = Uuid::new_v4();
    let g = gate(
        &[("economy.wager", serde_json::json!({"max_bet": 50}))],
        &[user],
    );
    assert!(g
        .authorize(&scope(), PermissionId::EconomyWager, wager(user, 50))
        .is_ok());
    assert_eq!(
        g.authorize(&scope(), PermissionId::EconomyWager, wager(user, 51))
            .unwrap_err(),
        Denied::AmountOutOfBounds
    );
}

#[test]
fn a_declared_bound_above_the_catalog_ceiling_is_clamped_to_it() {
    let user = Uuid::new_v4();
    let g = gate(
        &[("economy.wager", serde_json::json!({"max_bet": 1_000_000}))],
        &[user],
    );
    assert_eq!(
        g.authorize(&scope(), PermissionId::EconomyWager, wager(user, 1_001))
            .unwrap_err(),
        Denied::AmountOutOfBounds
    );
    assert!(g
        .authorize(&scope(), PermissionId::EconomyWager, wager(user, 1_000))
        .is_ok());
}

#[test]
fn zero_and_negative_amounts_are_out_of_bounds() {
    let user = Uuid::new_v4();
    let g = gate(&[("economy.wager", serde_json::json!({}))], &[user]);
    for bad in [0_i64, -1, i64::MIN] {
        assert_eq!(
            g.authorize(&scope(), PermissionId::EconomyWager, wager(user, bad))
                .unwrap_err(),
            Denied::AmountOutOfBounds,
            "{bad}"
        );
    }
}

#[test]
fn a_malformed_declared_bound_falls_back_to_the_ceiling() {
    let user = Uuid::new_v4();
    for params in [
        serde_json::json!({"max_bet": "lots"}),
        serde_json::json!({"max_bet": 0}),
        serde_json::json!({"max_bet": -5}),
    ] {
        let g = gate(&[("economy.wager", params)], &[user]);
        assert!(g
            .authorize(&scope(), PermissionId::EconomyWager, wager(user, 1_000))
            .is_ok());
    }
}

#[test]
fn transfer_uses_max_amount_not_max_bet() {
    let (a, b) = (Uuid::new_v4(), Uuid::new_v4());
    let g = gate(
        &[(
            "economy.transfer",
            serde_json::json!({"max_amount": 20, "max_bet": 999}),
        )],
        &[a, b],
    );
    assert_eq!(
        g.authorize(&scope(), PermissionId::EconomyTransfer, transfer(a, b, 21))
            .unwrap_err(),
        Denied::AmountOutOfBounds
    );
    assert!(g
        .authorize(&scope(), PermissionId::EconomyTransfer, transfer(a, b, 20))
        .is_ok());
}

#[test]
fn per_user_daily_aggregate_exhausts_across_calls() {
    let user = Uuid::new_v4();
    let g = gate(&[("economy.wager", serde_json::json!({}))], &[user]);
    // 10 x 1_000 = the 10_000 per-user daily ceiling.
    for _ in 0..10 {
        g.authorize(&scope(), PermissionId::EconomyWager, wager(user, 1_000))
            .expect("within the per-user daily aggregate");
    }
    assert_eq!(
        g.authorize(&scope(), PermissionId::EconomyWager, wager(user, 1))
            .unwrap_err(),
        Denied::QuotaExceeded
    );
    // A different member (separate gate / ledger) is unaffected.
    let other = Uuid::new_v4();
    let g2 = gate(&[("economy.wager", serde_json::json!({}))], &[user, other]);
    assert!(g2
        .authorize(&scope(), PermissionId::EconomyWager, wager(other, 1_000))
        .is_ok());
}

#[test]
fn per_user_aggregate_is_per_member_within_one_gate() {
    let (a, b) = (Uuid::new_v4(), Uuid::new_v4());
    let g = gate(&[("economy.wager", serde_json::json!({}))], &[a, b]);
    for _ in 0..10 {
        g.authorize(&scope(), PermissionId::EconomyWager, wager(a, 1_000))
            .unwrap();
    }
    assert_eq!(
        g.authorize(&scope(), PermissionId::EconomyWager, wager(a, 1))
            .unwrap_err(),
        Denied::QuotaExceeded
    );
    assert!(g
        .authorize(&scope(), PermissionId::EconomyWager, wager(b, 1_000))
        .is_ok());
}

#[test]
fn per_scope_daily_aggregate_exhausts_across_users() {
    // 100_000 transfer scope ceiling / 1_000 per call, spread across enough
    // senders that no per-user (5_000) cap trips first.
    let senders: Vec<Uuid> = (0..25).map(|_| Uuid::new_v4()).collect();
    let recipient = Uuid::new_v4();
    let mut members = senders.clone();
    members.push(recipient);
    let g = gate(&[("economy.transfer", serde_json::json!({}))], &members);
    let mut allowed = 0;
    let mut last = Ok(());
    'outer: for s in &senders {
        for _ in 0..5 {
            match g.authorize(
                &scope(),
                PermissionId::EconomyTransfer,
                transfer(*s, recipient, 1_000),
            ) {
                Ok(_) => allowed += 1,
                Err(e) => {
                    last = Err(e);
                    break 'outer;
                }
            }
        }
    }
    assert_eq!(allowed, 100, "exactly the per-scope ceiling's worth");
    assert_eq!(last, Err(Denied::QuotaExceeded));
}

#[test]
fn a_denied_call_consumes_no_quota() {
    let user = Uuid::new_v4();
    let g = gate(
        &[("economy.wager", serde_json::json!({"max_bet": 10}))],
        &[user],
    );
    for _ in 0..50 {
        assert_eq!(
            g.authorize(&scope(), PermissionId::EconomyWager, wager(user, 11))
                .unwrap_err(),
            Denied::AmountOutOfBounds
        );
    }
    for _ in 0..10 {
        assert!(g
            .authorize(&scope(), PermissionId::EconomyWager, wager(user, 10))
            .is_ok());
    }
}

#[test]
fn reads_authorize_with_and_without_a_target_and_are_rate_limited() {
    let user = Uuid::new_v4();
    let g = gate(&[("economy.read", serde_json::json!({}))], &[user]);
    assert!(g
        .authorize(&scope(), PermissionId::EconomyRead, read(Some(user)))
        .is_ok());
    assert!(g
        .authorize(&scope(), PermissionId::EconomyRead, read(None))
        .is_ok());
    // A non-member read target is still denied.
    assert_eq!(
        g.authorize(
            &scope(),
            PermissionId::EconomyRead,
            read(Some(Uuid::new_v4()))
        )
        .unwrap_err(),
        Denied::UserNotInScope
    );
    // 20 calls/s default: a tight burst trips the rate limit.
    let mut limited = false;
    for _ in 0..40 {
        if g.authorize(&scope(), PermissionId::EconomyRead, read(None)) == Err(Denied::RateLimited)
        {
            limited = true;
            break;
        }
    }
    assert!(limited, "economy.read must be rate limited");
}

#[test]
fn a_money_moving_call_without_an_acting_user_fails_closed() {
    let g = gate(&[("economy.wager", serde_json::json!({}))], &[]);
    let err = g
        .authorize(
            &scope(),
            PermissionId::EconomyWager,
            ResourceRef::EconomyScoped(EconomyTarget {
                target_user: None,
                counterparty: None,
                amount: Some(5),
            }),
        )
        .unwrap_err();
    assert_eq!(err, Denied::ResourceScopeMismatch);
}

#[test]
fn resource_shape_must_match_the_family() {
    let user = Uuid::new_v4();
    let g = gate(
        &[
            ("economy.wager", serde_json::json!({})),
            ("storage.kv", serde_json::json!({})),
            ("reputation.read", serde_json::json!({})),
        ],
        &[user],
    );
    // economy resource against a non-economy family
    assert_eq!(
        g.authorize(&scope(), PermissionId::StorageKv, wager(user, 1))
            .unwrap_err(),
        Denied::ResourceScopeMismatch
    );
    // reputation / app-scoped resource against an economy family
    assert_eq!(
        g.authorize(
            &scope(),
            PermissionId::EconomyWager,
            ResourceRef::ReputationScoped(ReputationTarget {
                target_user: user,
                scope_kind: ScopeKind::Community,
                delta: None,
            })
        )
        .unwrap_err(),
        Denied::ResourceScopeMismatch
    );
    assert_eq!(
        g.authorize(
            &scope(),
            PermissionId::EconomyWager,
            ResourceRef::AppScoped(AppScopedResource::None)
        )
        .unwrap_err(),
        Denied::ResourceScopeMismatch
    );
    // an economy resource against a reputation family
    assert_eq!(
        g.authorize(&scope(), PermissionId::ReputationRead, read(Some(user)))
            .unwrap_err(),
        Denied::ResourceScopeMismatch
    );
}

#[test]
fn economy_ids_parse_and_belong_to_exactly_the_economy_scope_class() {
    for id in ["economy.read", "economy.wager", "economy.transfer"] {
        let parsed = PermissionId::parse(id).expect(id);
        assert_eq!(parsed.canonical_id(), id);
        assert!(parsed.family().is_economy_scoped());
        assert!(!parsed.family().is_app_scoped());
        assert!(!parsed.family().is_reputation_scoped());
    }
    assert!(PermissionId::parse("economy.mint").is_err());
    assert!(PermissionId::parse("economy").is_err());
}
