//! `CapabilityGate::authorize` (spec SS5): "One crate, one function, every
//! host import calls it first."
//!
//! The spec's pseudocode signature (`authorize(scope, permission, resource)
//! -> Result<AuthorizedCall, Denied>`) omits the state a real
//! implementation needs to hold -- the grant cache, the reputation/profile
//! membership snapshot, and the quota ledger (spec SS5.5's "GrantSnapshot...
//! reputation/profile membership check... one additional hashmap read").
//! [`CapabilityGate`] is that state; [`CapabilityGate::authorize`] is the
//! spec's pseudocode as a method. `CapabilityHandler::handle` in
//! `svc_process`/`svc_action` calling this as the first statement in every
//! match arm is a later, data-plane integration task (spec SS12 Phase 4) --
//! this crate is deliberately not wired into either service yet.

use std::sync::Arc;
use std::time::Duration;

use crate::audit;
use crate::denied::Denied;
use crate::grant::GrantSnapshot;
use crate::instance_policy::{InstanceAction, InstancePolicySnapshot};
use crate::membership::MembershipCheck;
use crate::permission::{PermissionFamily, PermissionId, Quota};
use crate::quota::{QuotaDenial, QuotaLedger};
use crate::resource::{
    resolve_kv_key_prefix, resolve_object_prefix, resolve_overlay, resolve_table,
    AppScopedResource, AuthorizedCall, EconomyTarget, ResolvedResource, ResourceRef, ScopeKind,
};
use crate::scope::{GrantScopeKey, InvokeScope};

const REPUTATION_AGGREGATE_WINDOW: Duration = Duration::from_secs(24 * 60 * 60);
const ECONOMY_AGGREGATE_WINDOW: Duration = Duration::from_secs(24 * 60 * 60);

/// The gate's held state: a hot-path grant snapshot, a hot-path membership
/// check, and a quota ledger -- all sync/zero-I/O per spec SS5.5's
/// performance budget. Cheap to clone (every field is an `Arc`).
#[derive(Clone)]
pub struct CapabilityGate {
    snapshot: Arc<dyn GrantSnapshot>,
    membership: Arc<dyn MembershipCheck>,
    quota: Arc<dyn QuotaLedger>,
    instance_policy: Arc<dyn InstancePolicySnapshot>,
}

impl CapabilityGate {
    pub fn new(
        snapshot: Arc<dyn GrantSnapshot>,
        membership: Arc<dyn MembershipCheck>,
        quota: Arc<dyn QuotaLedger>,
        instance_policy: Arc<dyn InstancePolicySnapshot>,
    ) -> Self {
        Self {
            snapshot,
            membership,
            quota,
            instance_policy,
        }
    }

    fn deny(&self, scope: &InvokeScope, permission: &PermissionId, reason: Denied) -> Denied {
        audit::record_denied(scope, permission, reason);
        reason
    }

    /// spec SS5: "every host import calls it first." `scope` must come only
    /// from [`crate::scope::HostInvokeScopeBuilder`] -- see that module's
    /// doc for why the type system forecloses any guest-influenced scope
    /// ever reaching this call.
    pub fn authorize(
        &self,
        scope: &InvokeScope,
        permission: PermissionId,
        resource: ResourceRef,
    ) -> Result<AuthorizedCall, Denied> {
        let key = GrantScopeKey::from_scope(scope);
        let canonical_id = permission.canonical_id();

        // Instance policy (spec: instance policy, above the 3 consent
        // tiers) -- checked BEFORE the grant lookup, and regardless of
        // whether a grant row exists, so a stale/not-yet-revoked grant can
        // never bypass a platform-wide deny (defense in depth: hub-api's
        // own cascade revoke is the primary enforcement, this is the
        // belt-and-suspenders check on the data-plane's own hot path).
        if self.instance_policy.action(permission.family()) == InstanceAction::Deny {
            return Err(self.deny(scope, &permission, Denied::InstanceDenied));
        }

        // Fail-closed on a missing snapshot or a missing grant entry alike
        // (spec SS4/SS5.3: a cache miss -- whether from never having been
        // populated, or from a push-invalidation that landed before the
        // next refresh completes -- reads as "not granted," never as a
        // default allow).
        let grant_set = self
            .snapshot
            .current(&key)
            .ok_or_else(|| self.deny(scope, &permission, Denied::NotGranted))?;
        let granted = grant_set
            .get(&canonical_id)
            .ok_or_else(|| self.deny(scope, &permission, Denied::NotGranted))?;

        let family = permission.family();

        // AppScoped/ReputationScoped shape match (spec SS5.2) -- and, for
        // AppScoped, that the *specific* resource kind requested matches
        // what this family actually derives (spec SS5's "the gate is the
        // only place resource derivation happens").
        let resolved = match &resource {
            ResourceRef::AppScoped(app_res) => {
                if !family.is_app_scoped() || *app_res != family.expected_app_scoped_resource() {
                    return Err(self.deny(scope, &permission, Denied::ResourceScopeMismatch));
                }
                match app_res {
                    AppScopedResource::KvState => resolve_kv_key_prefix(scope),
                    AppScopedResource::Table => resolve_table(scope),
                    AppScopedResource::Objects => resolve_object_prefix(scope),
                    AppScopedResource::Overlay => resolve_overlay(scope),
                    AppScopedResource::None => ResolvedResource::None,
                }
            }
            ResourceRef::ReputationScoped(target) => {
                if !family.is_reputation_scoped() {
                    return Err(self.deny(scope, &permission, Denied::ResourceScopeMismatch));
                }
                // Membership verified at call time, not just at grant time
                // (spec SS5.2: "a user who left the community since the
                // bundle was activated cannot be adjusted or profile-read").
                if !self
                    .membership
                    .is_member(scope, target.target_user, target.scope_kind)
                {
                    return Err(self.deny(scope, &permission, Denied::UserNotInScope));
                }
                ResolvedResource::ReputationTarget(target.clone())
            }
            ResourceRef::EconomyScoped(target) => {
                if !family.is_economy_scoped() {
                    return Err(self.deny(scope, &permission, Denied::ResourceScopeMismatch));
                }
                // Every user the call names must be a live community member
                // (a wager's player, a transfer's sender AND recipient).
                for user in [target.target_user, target.counterparty]
                    .into_iter()
                    .flatten()
                {
                    if !self.membership.is_member(scope, user, ScopeKind::Community) {
                        return Err(self.deny(scope, &permission, Denied::UserNotInScope));
                    }
                }
                ResolvedResource::EconomyTarget(*target)
            }
        };

        // `economy.*` (issue #714) has its own quota family, so it settles
        // here and never falls into the reputation-delta/generic arms below.
        if let ResourceRef::EconomyScoped(target) = &resource {
            self.check_economy_quota(
                scope,
                &permission,
                &canonical_id,
                &key,
                target,
                &granted.params,
            )?;
            audit::record_authorized(scope, &permission);
            return Ok(AuthorizedCall {
                permission,
                resource: resolved,
                params: granted.params.clone(),
            });
        }

        // Quota/rate enforcement (spec SS1, SS7.2 steps 2-3, SS7.3). A
        // `reputation.*.write` `adjust()` call (a target with `delta:
        // Some(_)`) runs the three-part delta check; every other call runs
        // the generic per-permission quota (a no-op for `Unlimited`/
        // `Descriptive`, a plain rate limit for `CallsPerWindow`).
        let delta = match &resource {
            ResourceRef::ReputationScoped(target) => target.delta,
            ResourceRef::AppScoped(_) | ResourceRef::EconomyScoped(_) => None,
        };

        if let Some(delta) = delta {
            let Quota::ReputationDelta {
                per_call_abs_max,
                per_user_daily_abs_max,
                per_scope_daily_abs_max,
            } = family.catalog_entry().default_quota
            else {
                // A ReputationScoped delta call against a family whose
                // catalog quota isn't ReputationDelta-shaped (e.g.
                // `reputation.read`/`users.profile.read`, which never carry
                // a delta) never reaches this branch -- `delta` is `None`
                // for those. Defensive fallback: treat as out-of-bounds
                // rather than silently skipping the check.
                return Err(self.deny(scope, &permission, Denied::DeltaOutOfBounds));
            };

            let (declared_min, declared_max) =
                declared_delta_bounds(&granted.params, per_call_abs_max);
            if delta < declared_min
                || delta > declared_max
                || i64::from(delta.unsigned_abs()) > i64::from(per_call_abs_max)
            {
                return Err(self.deny(scope, &permission, Denied::DeltaOutOfBounds));
            }

            let target_user = match &resource {
                ResourceRef::ReputationScoped(t) => t.target_user,
                ResourceRef::AppScoped(_) | ResourceRef::EconomyScoped(_) => {
                    unreachable!("delta is only Some for ReputationScoped")
                }
            };

            if self
                .quota
                .check_and_consume_per_user(
                    &key,
                    &canonical_id,
                    target_user,
                    per_user_daily_abs_max,
                    REPUTATION_AGGREGATE_WINDOW,
                    i64::from(delta),
                )
                .is_err()
            {
                return Err(self.deny(scope, &permission, Denied::QuotaExceeded));
            }

            if self
                .quota
                .check_and_consume(
                    &key,
                    &canonical_id,
                    &Quota::ReputationDelta {
                        per_call_abs_max,
                        per_user_daily_abs_max,
                        per_scope_daily_abs_max,
                    },
                    i64::from(delta),
                )
                .is_err()
            {
                return Err(self.deny(scope, &permission, Denied::QuotaExceeded));
            }
        } else {
            match self
                .quota
                .check_and_consume(&key, &canonical_id, &permission.default_quota(), 1)
            {
                Ok(()) => {}
                Err(QuotaDenial::RateLimited) => {
                    return Err(self.deny(scope, &permission, Denied::RateLimited));
                }
                Err(QuotaDenial::QuotaExceeded) => {
                    return Err(self.deny(scope, &permission, Denied::QuotaExceeded));
                }
            }
        }

        audit::record_authorized(scope, &permission);
        Ok(AuthorizedCall {
            permission,
            resource: resolved,
            params: granted.params.clone(),
        })
    }
}

impl CapabilityGate {
    /// The `economy.*` quota step of [`Self::authorize`] (issue #714).
    ///
    /// A call with no metered amount (a read, or `economy.wager`'s `max-bet`)
    /// takes a plain rate limit and consumes none of the amount aggregates. A money-moving call is checked, in order: the amount is
    /// positive; it is within the catalog per-call ceiling AND the
    /// community-declared bound (`params.max_bet` for a wager,
    /// `params.max_amount` for a transfer -- clamped to the ceiling, never
    /// above it); the per-user daily aggregate (keyed on the acting
    /// `target_user`, the sender for a transfer); the per-community daily
    /// aggregate. Any breach of the first two is `amount_out_of_bounds`, of
    /// the aggregates `quota_exceeded`. Nothing is consumed by a denied call.
    ///
    /// A wager meters TWO things against the SAME ceilings, each in its own
    /// window: the STAKE (throughput -- how much a user can put at risk, which
    /// also bounds ledger growth) and the PAYOUT (the currency the call
    /// CREDITS, i.e. what it mints). The mint cap is on the payout, never the
    /// stake: a wager of 1 that pays 100 consumes 100 of the mint budget, and a
    /// large losing stake consumes none of it. A wager without a payout, or
    /// with a negative one, is `amount_out_of_bounds` (fail-closed).
    fn check_economy_quota(
        &self,
        scope: &InvokeScope,
        permission: &PermissionId,
        canonical_id: &str,
        key: &crate::scope::GrantScopeKey,
        target: &EconomyTarget,
        params: &serde_json::Value,
    ) -> Result<(), Denied> {
        let family = permission.family();
        let Some(amount) = target.amount else {
            // An amount-less call (a read, or `max-bet`, which describes the
            // wager capability's limit without moving money) is rate limited,
            // never metered against the daily amount aggregates: a family whose
            // default quota is amount-shaped falls back to the read rate limit.
            let rate_limit = match permission.default_quota() {
                Quota::EconomyAmount { .. } => Quota::CallsPerWindow {
                    max_calls: 20,
                    window: Duration::from_secs(1),
                },
                other => other,
            };
            // Distinct ledger key: the quota ledger keys its windows by
            // `(scope, permission id)` and fixes a window's shape at first use,
            // so reusing the permission id here would let a metered wager and
            // an amount-less `max-bet` poison each other's window (found by the
            // real-path e2e: a `max-bet` after a wager read the stake-sum window
            // as a call counter and was spuriously `rate_limited`).
            let calls_key = format!("{canonical_id}#calls");
            return match self
                .quota
                .check_and_consume(key, &calls_key, &rate_limit, 1)
            {
                Ok(()) => Ok(()),
                Err(QuotaDenial::RateLimited) => {
                    Err(self.deny(scope, permission, Denied::RateLimited))
                }
                Err(QuotaDenial::QuotaExceeded) => {
                    Err(self.deny(scope, permission, Denied::QuotaExceeded))
                }
            };
        };
        let Quota::EconomyAmount {
            per_call_abs_max,
            per_user_daily_abs_max,
            per_scope_daily_abs_max,
        } = family.catalog_entry().default_quota
        else {
            // Defensive: a money-moving call against a family whose catalog
            // quota is not EconomyAmount-shaped is a wiring bug -- deny.
            return Err(self.deny(scope, permission, Denied::AmountOutOfBounds));
        };

        let Some(bound) = family.economy_amount_bound(params) else {
            return Err(self.deny(scope, permission, Denied::AmountOutOfBounds));
        };
        if amount < 1 || amount > bound {
            return Err(self.deny(scope, permission, Denied::AmountOutOfBounds));
        }
        // A wager's payout is validated up front, before anything is consumed:
        // a missing or negative one is a malformed call, not a free pass.
        let wager_payout = if family == PermissionFamily::EconomyWager {
            match target.payout.filter(|p| *p >= 0) {
                Some(p) => Some(p),
                None => return Err(self.deny(scope, permission, Denied::AmountOutOfBounds)),
            }
        } else {
            None
        };

        // A money-moving call must name the acting user: without one there
        // is no per-user aggregate to charge, so fail closed.
        let Some(acting_user) = target.target_user else {
            return Err(self.deny(scope, permission, Denied::ResourceScopeMismatch));
        };
        if self
            .quota
            .check_and_consume_per_user(
                key,
                canonical_id,
                acting_user,
                per_user_daily_abs_max,
                ECONOMY_AGGREGATE_WINDOW,
                amount,
            )
            .is_err()
        {
            return Err(self.deny(scope, permission, Denied::QuotaExceeded));
        }
        let aggregate = Quota::EconomyAmount {
            per_call_abs_max,
            per_user_daily_abs_max,
            per_scope_daily_abs_max,
        };
        if self
            .quota
            .check_and_consume(key, canonical_id, &aggregate, amount)
            .is_err()
        {
            return Err(self.deny(scope, permission, Denied::QuotaExceeded));
        }

        // The mint cap: a wager's PAYOUT is metered against its own windows
        // (distinct ledger key -- the quota ledger fixes a window's shape at
        // first use, so it must not share the stake's), against the same
        // per-user / per-community ceilings. Payout is NOT bounded per call by
        // `bound` (that is the stake's bound); the store bounds it to
        // `stake * MAX_PAYOUT_MULTIPLE`.
        if let Some(payout) = wager_payout {
            let payout_key = format!("{canonical_id}#payout");
            if self
                .quota
                .check_and_consume_per_user(
                    key,
                    &payout_key,
                    acting_user,
                    per_user_daily_abs_max,
                    ECONOMY_AGGREGATE_WINDOW,
                    payout,
                )
                .is_err()
            {
                return Err(self.deny(scope, permission, Denied::QuotaExceeded));
            }
            if self
                .quota
                .check_and_consume(key, &payout_key, &aggregate, payout)
                .is_err()
            {
                return Err(self.deny(scope, permission, Denied::QuotaExceeded));
            }
        }
        Ok(())
    }
}

/// The community's own declared `delta_min`/`delta_max` (spec SS2.1's
/// manifest `params`, carried on the granted permission's own `params`),
/// clamped to the catalog's per-call ceiling regardless of what was declared
/// -- defense in depth against a declared bound that (through a bug
/// upstream) exceeds the ceiling spec SS2.2 says hub-api validation already
/// enforces at approval time.
fn declared_delta_bounds(params: &serde_json::Value, per_call_abs_max: i32) -> (i32, i32) {
    let ceiling = i64::from(per_call_abs_max);
    let declared_min = params
        .get("delta_min")
        .and_then(serde_json::Value::as_i64)
        .unwrap_or(-ceiling)
        .max(-ceiling);
    let declared_max = params
        .get("delta_max")
        .and_then(serde_json::Value::as_i64)
        .unwrap_or(ceiling)
        .min(ceiling);
    (declared_min as i32, declared_max as i32)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::grant::{
        GrantCache, GrantSet, GrantedPermission, InMemoryGrantLoader, InMemoryGrantSnapshot,
    };
    use crate::instance_policy::InMemoryInstancePolicySnapshot;
    use crate::membership::InMemoryMembership;
    use crate::quota::InMemoryQuotaLedger;
    use crate::resource::{AppScopedResource, ReputationTarget, ScopeKind};
    use crate::scope::{HostInvokeScopeBuilder, TenantTier};
    use std::collections::HashMap;
    use uuid::Uuid;

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

    fn grants(entries: &[(&str, serde_json::Value)]) -> GrantSet {
        let mut grants = HashMap::new();
        for (id, params) in entries {
            grants.insert(
                id.to_string(),
                GrantedPermission {
                    permission_id: id.to_string(),
                    params: params.clone(),
                },
            );
        }
        GrantSet {
            permission_snapshot_hash: "hash-v1".to_string(),
            grants,
        }
    }

    fn gate_with(snapshot: Arc<dyn GrantSnapshot>) -> CapabilityGate {
        gate_with_policy(snapshot, Arc::new(InMemoryInstancePolicySnapshot::new()))
    }

    fn gate_with_policy(
        snapshot: Arc<dyn GrantSnapshot>,
        instance_policy: Arc<dyn InstancePolicySnapshot>,
    ) -> CapabilityGate {
        CapabilityGate::new(
            snapshot,
            Arc::new(InMemoryMembership::new()),
            Arc::new(InMemoryQuotaLedger::new()),
            instance_policy,
        )
    }

    #[test]
    fn app_scoped_permission_authorizes_and_resolves_the_kv_prefix() {
        let snapshot = InMemoryGrantSnapshot::new();
        snapshot.set(
            GrantScopeKey::from_scope(&scope()),
            grants(&[("storage.kv", serde_json::json!({}))]),
        );
        let gate = gate_with(Arc::new(snapshot));

        let call = gate
            .authorize(
                &scope(),
                PermissionId::StorageKv,
                ResourceRef::AppScoped(AppScopedResource::KvState),
            )
            .expect("granted");

        assert_eq!(
            call.resource,
            ResolvedResource::KvKeyPrefix(
                "waddles:app:7:3:waddles.core.example_echo:state".to_string()
            )
        );
    }

    #[test]
    fn ungranted_permission_is_denied_not_granted() {
        let snapshot = InMemoryGrantSnapshot::new();
        snapshot.set(GrantScopeKey::from_scope(&scope()), grants(&[]));
        let gate = gate_with(Arc::new(snapshot));

        let err = gate
            .authorize(
                &scope(),
                PermissionId::StorageKv,
                ResourceRef::AppScoped(AppScopedResource::KvState),
            )
            .unwrap_err();
        assert_eq!(err, Denied::NotGranted);
    }

    #[test]
    fn missing_snapshot_entirely_is_denied_not_granted_fail_closed() {
        let snapshot = InMemoryGrantSnapshot::new(); // nothing set at all
        let gate = gate_with(Arc::new(snapshot));

        let err = gate
            .authorize(
                &scope(),
                PermissionId::FlagsRead,
                ResourceRef::AppScoped(AppScopedResource::None),
            )
            .unwrap_err();
        assert_eq!(err, Denied::NotGranted);
    }

    /// Cross-tenant denial: a grant set populated for tenant 7 must not
    /// authorize an identical call under tenant 999's scope.
    #[test]
    fn cross_tenant_call_is_denied_even_with_an_identical_app_id() {
        let snapshot = InMemoryGrantSnapshot::new();
        snapshot.set(
            GrantScopeKey::from_scope(&scope()),
            grants(&[("storage.kv", serde_json::json!({}))]),
        );
        let gate = gate_with(Arc::new(snapshot));

        let other_tenant = HostInvokeScopeBuilder::new()
            .tenant_id(999)
            .community_id(3)
            .app_id("waddles.core.example_echo")
            .app_version(1)
            .tenant_tier(TenantTier::Free)
            .build()
            .unwrap();

        let err = gate
            .authorize(
                &other_tenant,
                PermissionId::StorageKv,
                ResourceRef::AppScoped(AppScopedResource::KvState),
            )
            .unwrap_err();
        assert_eq!(err, Denied::NotGranted);
    }

    /// Cross-app denial: a grant set for one app_id must not authorize a
    /// different app_id under the same tenant/community.
    #[test]
    fn cross_app_call_is_denied_even_under_the_same_tenant_and_community() {
        let snapshot = InMemoryGrantSnapshot::new();
        snapshot.set(
            GrantScopeKey::from_scope(&scope()),
            grants(&[("storage.kv", serde_json::json!({}))]),
        );
        let gate = gate_with(Arc::new(snapshot));

        let other_app = HostInvokeScopeBuilder::new()
            .tenant_id(7)
            .community_id(3)
            .app_id("some_vendor.other_bundle")
            .app_version(1)
            .tenant_tier(TenantTier::Free)
            .build()
            .unwrap();

        let err = gate
            .authorize(
                &other_app,
                PermissionId::StorageKv,
                ResourceRef::AppScoped(AppScopedResource::KvState),
            )
            .unwrap_err();
        assert_eq!(err, Denied::NotGranted);
    }

    #[test]
    fn resource_kind_mismatch_for_an_app_scoped_permission_is_denied() {
        let snapshot = InMemoryGrantSnapshot::new();
        snapshot.set(
            GrantScopeKey::from_scope(&scope()),
            grants(&[("storage.kv", serde_json::json!({}))]),
        );
        let gate = gate_with(Arc::new(snapshot));

        // storage.kv granted, but the capability implementation wires it to
        // the wrong AppScopedResource kind (a host bug, not a guest one).
        let err = gate
            .authorize(
                &scope(),
                PermissionId::StorageKv,
                ResourceRef::AppScoped(AppScopedResource::Table),
            )
            .unwrap_err();
        assert_eq!(err, Denied::ResourceScopeMismatch);
    }

    #[test]
    fn app_scoped_permission_called_with_a_reputation_scoped_resource_is_denied() {
        let snapshot = InMemoryGrantSnapshot::new();
        snapshot.set(
            GrantScopeKey::from_scope(&scope()),
            grants(&[("storage.kv", serde_json::json!({}))]),
        );
        let gate = gate_with(Arc::new(snapshot));

        let err = gate
            .authorize(
                &scope(),
                PermissionId::StorageKv,
                ResourceRef::ReputationScoped(ReputationTarget {
                    target_user: Uuid::new_v4(),
                    scope_kind: ScopeKind::Community,
                    delta: None,
                }),
            )
            .unwrap_err();
        assert_eq!(err, Denied::ResourceScopeMismatch);
    }

    #[test]
    fn reputation_scoped_call_for_a_member_is_authorized() {
        let snapshot = InMemoryGrantSnapshot::new();
        snapshot.set(
            GrantScopeKey::from_scope(&scope()),
            grants(&[("reputation.read", serde_json::json!({}))]),
        );
        let membership = InMemoryMembership::new();
        let user = Uuid::new_v4();
        membership.add_community_member(7, 3, user);
        let gate = CapabilityGate::new(
            Arc::new(snapshot),
            Arc::new(membership),
            Arc::new(InMemoryQuotaLedger::new()),
            Arc::new(InMemoryInstancePolicySnapshot::new()),
        );

        let call = gate
            .authorize(
                &scope(),
                PermissionId::ReputationRead,
                ResourceRef::ReputationScoped(ReputationTarget {
                    target_user: user,
                    scope_kind: ScopeKind::Community,
                    delta: None,
                }),
            )
            .expect("member of the community");
        assert_eq!(
            call.resource,
            ResolvedResource::ReputationTarget(ReputationTarget {
                target_user: user,
                scope_kind: ScopeKind::Community,
                delta: None,
            })
        );
    }

    /// Reputation membership test: a target not in the invocation's
    /// community is denied `user_not_in_scope`, never authorized.
    #[test]
    fn reputation_scoped_call_for_a_non_member_is_denied_user_not_in_scope() {
        let snapshot = InMemoryGrantSnapshot::new();
        snapshot.set(
            GrantScopeKey::from_scope(&scope()),
            grants(&[("reputation.read", serde_json::json!({}))]),
        );
        let gate = gate_with(Arc::new(snapshot)); // InMemoryMembership starts empty

        let err = gate
            .authorize(
                &scope(),
                PermissionId::ReputationRead,
                ResourceRef::ReputationScoped(ReputationTarget {
                    target_user: Uuid::new_v4(),
                    scope_kind: ScopeKind::Community,
                    delta: None,
                }),
            )
            .unwrap_err();
        assert_eq!(err, Denied::UserNotInScope);
    }

    #[test]
    fn reputation_write_within_bounds_and_under_quota_succeeds() {
        let snapshot = InMemoryGrantSnapshot::new();
        snapshot.set(
            GrantScopeKey::from_scope(&scope()),
            grants(&[(
                "reputation.community.write",
                serde_json::json!({"delta_min": -1, "delta_max": 1}),
            )]),
        );
        let membership = InMemoryMembership::new();
        let user = Uuid::new_v4();
        membership.add_community_member(7, 3, user);
        let gate = CapabilityGate::new(
            Arc::new(snapshot),
            Arc::new(membership),
            Arc::new(InMemoryQuotaLedger::new()),
            Arc::new(InMemoryInstancePolicySnapshot::new()),
        );

        let call = gate
            .authorize(
                &scope(),
                PermissionId::ReputationCommunityWrite,
                ResourceRef::ReputationScoped(ReputationTarget {
                    target_user: user,
                    scope_kind: ScopeKind::Community,
                    delta: Some(1),
                }),
            )
            .expect("within declared bounds and under every cap");
        assert_eq!(
            call.params,
            serde_json::json!({"delta_min": -1, "delta_max": 1})
        );
    }

    #[test]
    fn reputation_write_outside_the_communitys_declared_bound_is_denied() {
        let snapshot = InMemoryGrantSnapshot::new();
        snapshot.set(
            GrantScopeKey::from_scope(&scope()),
            grants(&[(
                "reputation.community.write",
                serde_json::json!({"delta_min": -1, "delta_max": 1}),
            )]),
        );
        let membership = InMemoryMembership::new();
        let user = Uuid::new_v4();
        membership.add_community_member(7, 3, user);
        let gate = CapabilityGate::new(
            Arc::new(snapshot),
            Arc::new(membership),
            Arc::new(InMemoryQuotaLedger::new()),
            Arc::new(InMemoryInstancePolicySnapshot::new()),
        );

        let err = gate
            .authorize(
                &scope(),
                PermissionId::ReputationCommunityWrite,
                ResourceRef::ReputationScoped(ReputationTarget {
                    target_user: user,
                    scope_kind: ScopeKind::Community,
                    delta: Some(3), // outside the community's own [-1, 1] bound
                }),
            )
            .unwrap_err();
        assert_eq!(err, Denied::DeltaOutOfBounds);
    }

    #[test]
    fn reputation_write_beyond_the_catalog_ceiling_is_denied_even_if_declared_wider() {
        let snapshot = InMemoryGrantSnapshot::new();
        snapshot.set(
            GrantScopeKey::from_scope(&scope()),
            // A (buggy or malicious-upstream) declared bound wider than the
            // catalog ceiling (+-5) must still be clamped.
            grants(&[(
                "reputation.community.write",
                serde_json::json!({"delta_min": -100, "delta_max": 100}),
            )]),
        );
        let membership = InMemoryMembership::new();
        let user = Uuid::new_v4();
        membership.add_community_member(7, 3, user);
        let gate = CapabilityGate::new(
            Arc::new(snapshot),
            Arc::new(membership),
            Arc::new(InMemoryQuotaLedger::new()),
            Arc::new(InMemoryInstancePolicySnapshot::new()),
        );

        let err = gate
            .authorize(
                &scope(),
                PermissionId::ReputationCommunityWrite,
                ResourceRef::ReputationScoped(ReputationTarget {
                    target_user: user,
                    scope_kind: ScopeKind::Community,
                    delta: Some(10),
                }),
            )
            .unwrap_err();
        assert_eq!(err, Denied::DeltaOutOfBounds);
    }

    /// Quota-exhaustion test: repeated in-bounds reputation writes to
    /// different users each within their own per-user cap still exhaust the
    /// community's aggregate daily cap (default +-50, spec SS1/SS7.3) --
    /// magnitude alone, spread across many targets, is what the aggregate
    /// cap exists to bound.
    #[test]
    fn reputation_write_exhausts_the_community_aggregate_quota_across_calls() {
        let snapshot = InMemoryGrantSnapshot::new();
        snapshot.set(
            GrantScopeKey::from_scope(&scope()),
            grants(&[(
                "reputation.community.write",
                serde_json::json!({"delta_min": -5, "delta_max": 5}),
            )]),
        );
        let members: Vec<Uuid> = (0..11).map(|_| Uuid::new_v4()).collect();
        let membership = InMemoryMembership::new();
        for user in &members {
            membership.add_community_member(7, 3, *user);
        }
        let gate = CapabilityGate::new(
            Arc::new(snapshot),
            Arc::new(membership),
            Arc::new(InMemoryQuotaLedger::new()),
            Arc::new(InMemoryInstancePolicySnapshot::new()),
        );

        // First 10 members each contribute +5: 10 * 5 == 50, exactly the
        // default community aggregate cap -- every one succeeds.
        for user in &members[..10] {
            gate.authorize(
                &scope(),
                PermissionId::ReputationCommunityWrite,
                ResourceRef::ReputationScoped(ReputationTarget {
                    target_user: *user,
                    scope_kind: ScopeKind::Community,
                    delta: Some(5),
                }),
            )
            .expect("within the aggregate cap");
        }

        // The 11th member's own per-call and per-user bounds are untouched,
        // but the community's daily aggregate is already at the cap -- this
        // call must be denied, not authorized.
        let err = gate
            .authorize(
                &scope(),
                PermissionId::ReputationCommunityWrite,
                ResourceRef::ReputationScoped(ReputationTarget {
                    target_user: members[10],
                    scope_kind: ScopeKind::Community,
                    delta: Some(1),
                }),
            )
            .unwrap_err();
        assert_eq!(err, Denied::QuotaExceeded);
    }

    /// Per-user daily aggregate: two calls to the *same* target user, each
    /// within the per-call bound, still exhaust that user's own +-5/day cap.
    #[test]
    fn reputation_write_exhausts_the_per_user_daily_quota() {
        let snapshot = InMemoryGrantSnapshot::new();
        snapshot.set(
            GrantScopeKey::from_scope(&scope()),
            grants(&[(
                "reputation.community.write",
                serde_json::json!({"delta_min": -5, "delta_max": 5}),
            )]),
        );
        let membership = InMemoryMembership::new();
        let user = Uuid::new_v4();
        membership.add_community_member(7, 3, user);
        let gate = CapabilityGate::new(
            Arc::new(snapshot),
            Arc::new(membership),
            Arc::new(InMemoryQuotaLedger::new()),
            Arc::new(InMemoryInstancePolicySnapshot::new()),
        );

        assert!(gate
            .authorize(
                &scope(),
                PermissionId::ReputationCommunityWrite,
                ResourceRef::ReputationScoped(ReputationTarget {
                    target_user: user,
                    scope_kind: ScopeKind::Community,
                    delta: Some(5),
                }),
            )
            .is_ok());

        let err = gate
            .authorize(
                &scope(),
                PermissionId::ReputationCommunityWrite,
                ResourceRef::ReputationScoped(ReputationTarget {
                    target_user: user,
                    scope_kind: ScopeKind::Community,
                    delta: Some(1),
                }),
            )
            .unwrap_err();
        assert_eq!(err, Denied::QuotaExceeded);
    }

    /// Rate-limit quota exhaustion for a `CallsPerWindow`-shaped permission
    /// (`overlay.media`, 1 call/10s default).
    #[test]
    fn calls_per_window_permission_is_rate_limited_after_the_default_quota() {
        let snapshot = InMemoryGrantSnapshot::new();
        snapshot.set(
            GrantScopeKey::from_scope(&scope()),
            grants(&[("overlay.media", serde_json::json!({}))]),
        );
        let gate = gate_with(Arc::new(snapshot));

        assert!(gate
            .authorize(
                &scope(),
                PermissionId::OverlayMedia,
                ResourceRef::AppScoped(AppScopedResource::Overlay),
            )
            .is_ok());

        let err = gate
            .authorize(
                &scope(),
                PermissionId::OverlayMedia,
                ResourceRef::AppScoped(AppScopedResource::Overlay),
            )
            .unwrap_err();
        assert_eq!(err, Denied::RateLimited);
    }

    /// `identity.resolve` is a plain AppScoped/None family: granted =>
    /// authorized with no derived resource; ungranted => `not_granted`
    /// (fail-closed, a bundle that never declared it can never resolve an
    /// identity); a wrong resource shape => `resource_scope_mismatch`.
    #[test]
    fn identity_resolve_authorizes_only_when_granted_and_with_the_none_resource() {
        let snapshot = InMemoryGrantSnapshot::new();
        snapshot.set(
            GrantScopeKey::from_scope(&scope()),
            grants(&[("identity.resolve", serde_json::json!({}))]),
        );
        let gate = gate_with(Arc::new(snapshot));
        let call = gate
            .authorize(
                &scope(),
                PermissionId::IdentityResolve,
                ResourceRef::AppScoped(AppScopedResource::None),
            )
            .expect("granted");
        assert_eq!(call.resource, ResolvedResource::None);

        let err = gate
            .authorize(
                &scope(),
                PermissionId::IdentityResolve,
                ResourceRef::AppScoped(AppScopedResource::KvState),
            )
            .unwrap_err();
        assert_eq!(err, Denied::ResourceScopeMismatch);

        // A reputation-shaped target is not valid for this family either.
        let err = gate
            .authorize(
                &scope(),
                PermissionId::IdentityResolve,
                ResourceRef::ReputationScoped(ReputationTarget {
                    target_user: Uuid::new_v4(),
                    scope_kind: ScopeKind::Community,
                    delta: None,
                }),
            )
            .unwrap_err();
        assert_eq!(err, Denied::ResourceScopeMismatch);
    }

    #[test]
    fn identity_resolve_without_a_grant_is_denied_not_granted() {
        let snapshot = InMemoryGrantSnapshot::new();
        // Holding every OTHER capability must not imply identity.resolve.
        snapshot.set(
            GrantScopeKey::from_scope(&scope()),
            grants(&[
                ("economy.read", serde_json::json!({})),
                ("reputation.read", serde_json::json!({})),
            ]),
        );
        let gate = gate_with(Arc::new(snapshot));
        let err = gate
            .authorize(
                &scope(),
                PermissionId::IdentityResolve,
                ResourceRef::AppScoped(AppScopedResource::None),
            )
            .unwrap_err();
        assert_eq!(err, Denied::NotGranted);
    }

    #[test]
    fn identity_resolve_is_rate_limited_to_twenty_calls_per_second() {
        let snapshot = InMemoryGrantSnapshot::new();
        snapshot.set(
            GrantScopeKey::from_scope(&scope()),
            grants(&[("identity.resolve", serde_json::json!({}))]),
        );
        let gate = gate_with(Arc::new(snapshot));
        for n in 0..20 {
            assert!(
                gate.authorize(
                    &scope(),
                    PermissionId::IdentityResolve,
                    ResourceRef::AppScoped(AppScopedResource::None),
                )
                .is_ok(),
                "call {n} within the window must be allowed"
            );
        }
        let err = gate
            .authorize(
                &scope(),
                PermissionId::IdentityResolve,
                ResourceRef::AppScoped(AppScopedResource::None),
            )
            .unwrap_err();
        assert_eq!(err, Denied::RateLimited);
    }

    /// An instance-wide deny of the family overrides even a live grant.
    #[test]
    fn identity_resolve_honors_an_instance_wide_deny() {
        let snapshot = InMemoryGrantSnapshot::new();
        snapshot.set(
            GrantScopeKey::from_scope(&scope()),
            grants(&[("identity.resolve", serde_json::json!({}))]),
        );
        let policy = InMemoryInstancePolicySnapshot::new();
        policy.set(
            crate::permission::PermissionFamily::IdentityResolve,
            InstanceAction::Deny,
        );
        let gate = gate_with_policy(Arc::new(snapshot), Arc::new(policy));
        let err = gate
            .authorize(
                &scope(),
                PermissionId::IdentityResolve,
                ResourceRef::AppScoped(AppScopedResource::None),
            )
            .unwrap_err();
        assert_eq!(err, Denied::InstanceDenied);
    }

    /// Revoked/stale grant version test: a `GrantCache` refreshed for
    /// version 1, then `invalidate`d (simulating a push-invalidation
    /// revocation landing mid-connection, spec SS5.3), denies the very next
    /// call -- never rides out on the last-known grant.
    #[tokio::test]
    async fn revoked_grant_denies_the_next_call_after_invalidation() {
        let loader = Arc::new(InMemoryGrantLoader::new());
        let key = GrantScopeKey::from_scope(&scope());
        loader.set(
            key.clone(),
            grants(&[("flags.read", serde_json::json!({}))]),
        );
        let cache = Arc::new(GrantCache::new(loader));
        cache.refresh(&key).await.unwrap();

        let gate = gate_with(cache.clone());
        assert!(gate
            .authorize(
                &scope(),
                PermissionId::FlagsRead,
                ResourceRef::AppScoped(AppScopedResource::None),
            )
            .is_ok());

        cache.invalidate(&key);

        let err = gate
            .authorize(
                &scope(),
                PermissionId::FlagsRead,
                ResourceRef::AppScoped(AppScopedResource::None),
            )
            .unwrap_err();
        assert_eq!(err, Denied::NotGranted);
    }

    /// Stale grant version test: a community pinned to an older, already-
    /// consented app_version must never be authorized against a newer
    /// version's grant row (spec SS3.4/SS4) -- the two are different
    /// `GrantScopeKey`s entirely.
    #[tokio::test]
    async fn a_call_pinned_to_an_old_app_version_is_denied_when_only_a_newer_version_is_granted() {
        let loader = Arc::new(InMemoryGrantLoader::new());
        let newer_key = GrantScopeKey {
            tenant_id: 7,
            community_id: 3,
            app_id: "waddles.core.example_echo".to_string(),
            app_version: 2,
        };
        loader.set(
            newer_key.clone(),
            grants(&[("flags.read", serde_json::json!({}))]),
        );
        let cache = Arc::new(GrantCache::new(loader));
        cache.refresh(&newer_key).await.unwrap();

        let gate = gate_with(cache);

        // `scope()` is pinned to app_version 1 -- the community never
        // re-consented to version 2 (spec SS3.4's pinned-version rule).
        let err = gate
            .authorize(
                &scope(),
                PermissionId::FlagsRead,
                ResourceRef::AppScoped(AppScopedResource::None),
            )
            .unwrap_err();
        assert_eq!(err, Denied::NotGranted);
    }

    /// Instance policy (spec: instance policy, above the 3 consent tiers):
    /// `net.http.private-ip` is deny-by-default -- and this must hold even
    /// when the `GrantSnapshot` still carries an active grant row for it
    /// (the exact "stale grant + instance deny" defense-in-depth scenario --
    /// hub-api's own cascade revoke is the primary enforcement, but the gate
    /// must never rely on that cascade having landed yet).
    #[test]
    fn net_http_private_ip_is_instance_denied_by_default_even_with_an_active_grant() {
        let snapshot = InMemoryGrantSnapshot::new();
        snapshot.set(
            GrantScopeKey::from_scope(&scope()),
            grants(&[(
                "net.http.private-ip:10.20.0.0/16",
                serde_json::json!({"methods": ["GET"]}),
            )]),
        );
        let gate = gate_with(Arc::new(snapshot));

        let err = gate
            .authorize(
                &scope(),
                PermissionId::NetHttpPrivateIp("10.20.0.0/16".to_string()),
                ResourceRef::AppScoped(AppScopedResource::None),
            )
            .unwrap_err();
        assert_eq!(err, Denied::InstanceDenied);
    }

    /// A global admin's explicit `allow` override lets an otherwise-granted
    /// `net.http.private-ip` call through.
    #[test]
    fn net_http_private_ip_authorizes_once_a_global_admin_opts_in() {
        let snapshot = InMemoryGrantSnapshot::new();
        snapshot.set(
            GrantScopeKey::from_scope(&scope()),
            grants(&[(
                "net.http.private-ip:10.20.0.0/16",
                serde_json::json!({"methods": ["GET"]}),
            )]),
        );
        let policy = Arc::new(InMemoryInstancePolicySnapshot::new());
        policy.set(
            crate::permission::PermissionFamily::NetHttpPrivateIp,
            InstanceAction::Allow,
        );
        let gate = gate_with_policy(Arc::new(snapshot), policy);

        let call = gate
            .authorize(
                &scope(),
                PermissionId::NetHttpPrivateIp("10.20.0.0/16".to_string()),
                ResourceRef::AppScoped(AppScopedResource::None),
            )
            .unwrap();
        assert_eq!(
            call.permission,
            PermissionId::NetHttpPrivateIp("10.20.0.0/16".to_string())
        );
    }

    /// `net.http.fqdn`/`net.http.public-ip` are unaffected by the
    /// `private-ip`-only default deny -- an active grant for either still
    /// authorizes normally.
    #[test]
    fn net_http_fqdn_and_public_ip_are_unaffected_by_the_private_ip_default_deny() {
        let snapshot = InMemoryGrantSnapshot::new();
        snapshot.set(
            GrantScopeKey::from_scope(&scope()),
            grants(&[(
                "net.http.fqdn:api.example.com",
                serde_json::json!({"methods": ["GET"]}),
            )]),
        );
        let gate = gate_with(Arc::new(snapshot));

        let call = gate
            .authorize(
                &scope(),
                PermissionId::NetHttpFqdn("api.example.com".to_string()),
                ResourceRef::AppScoped(AppScopedResource::None),
            )
            .unwrap();
        assert_eq!(
            call.permission,
            PermissionId::NetHttpFqdn("api.example.com".to_string())
        );
    }
}

#[cfg(test)]
#[path = "gate_economy_tests.rs"]
mod economy_tests;
