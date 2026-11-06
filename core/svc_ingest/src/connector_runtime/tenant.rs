//! Tenant derivation for supervised connections.
//!
//! `BindingSummary` carries no tenant, and profiles deliberately never read a
//! tenant from binding metadata (a binding's metadata is guest-adjacent data).
//! The runtime therefore derives the tenant per connection through a
//! [`TenantResolve`] (in production the dataplane scope resolver) and refuses
//! the connection when the tenant is unresolved or disagrees with the one the
//! profile supplied. A profile that cannot know the tenant sets
//! [`TENANT_UNRESOLVED`] and lets the resolver fill it in.

use super::feed::{DesiredConnection, TransportPlan};
use crate::connector::webhook::BoxFut;
use crate::scope_resolver::{ResolvedScope, Unresolved};

/// The `DesiredConnection::tenant_id` a profile sets when it cannot know the
/// tenant; the resolver's answer is then authoritative.
pub const TENANT_UNRESOLVED: i64 = 0;

/// Why a connection's tenant could not be established.
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum TenantError {
    /// No tenant is mapped to the connection's community/source.
    #[error("tenant is not resolvable")]
    Unresolved,
    /// The resolved community is not the connection's community.
    #[error("resolved community disagrees with the connection")]
    Mismatch,
    /// The lookup failed transiently; retried after the cooldown.
    #[error("tenant lookup failed")]
    Transient,
}

/// Resolves the tenant owning one connection. Implementations run at the
/// wiring site where the concrete scope source is known (so the future is
/// provably `Send`); see [`tenant_from_scope`] and [`source_key_of`].
pub trait TenantResolve: Send + Sync {
    /// The authoritative numeric tenant of `desired` (>= 1).
    fn resolve<'a>(
        &'a self,
        desired: &'a DesiredConnection,
    ) -> BoxFut<'a, Result<i64, TenantError>>;
}

/// The upstream resource a Ws/Poll plan targets (the scope lookup key);
/// `None` for a webhook plan, which has no scope-resolvable source.
#[must_use]
pub fn source_key_of(d: &DesiredConnection) -> Option<&str> {
    match &d.plan {
        TransportPlan::Ws(c) => Some(&c.source_key),
        TransportPlan::Poll(c) => Some(&c.source_key),
        TransportPlan::Spectrum(s) => Some(&s.poll.source_key),
        TransportPlan::Webhook(_) => None,
    }
}

/// Maps a scope-resolver answer for `community_id` to a tenant: the resolved
/// community must be exactly the connection's community.
pub fn tenant_from_scope(
    res: Result<ResolvedScope, Unresolved>,
    community_id: i64,
) -> Result<i64, TenantError> {
    match res {
        Ok(s) if i64::from(s.community_id) == community_id => Ok(s.tenant_id),
        Ok(_) => Err(TenantError::Mismatch),
        Err(Unresolved::Transient) => Err(TenantError::Transient),
        Err(_) => Err(TenantError::Unresolved),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::connector_runtime::testkit::{desired_webhook, desired_ws};

    fn scope(tenant: i64, community: i64) -> ResolvedScope {
        ResolvedScope::validated(tenant, community, "acme", Some("main")).expect("valid scope")
    }

    #[test]
    fn the_resolved_community_must_equal_the_connections_community() {
        assert_eq!(tenant_from_scope(Ok(scope(5, 7)), 7), Ok(5));
        assert_eq!(
            tenant_from_scope(Ok(scope(5, 7)), 8),
            Err(TenantError::Mismatch)
        );
    }

    #[test]
    fn every_unresolved_reason_maps_without_defaulting_a_tenant() {
        assert_eq!(
            tenant_from_scope(Err(Unresolved::Transient), 7),
            Err(TenantError::Transient)
        );
        for u in [
            Unresolved::NotFound,
            Unresolved::Ambiguous,
            Unresolved::Invalid,
            Unresolved::Inconsistent,
            Unresolved::NoSource,
        ] {
            assert_eq!(tenant_from_scope(Err(u), 7), Err(TenantError::Unresolved));
        }
    }

    #[test]
    fn source_key_comes_from_ws_and_poll_plans_only() {
        assert_eq!(
            source_key_of(&desired_ws("twitch", 1, 2, 9, "chan", 1)),
            Some("chan")
        );
        assert_eq!(
            source_key_of(&desired_webhook(1, 2, "route-key-1", 1)),
            None
        );
    }
}
