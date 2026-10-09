//! The bundle `reputation` host capability's store (issue #726) -- the
//! reference bundle-called host capability every later one (economy #714,
//! streaming-lifecycle #716) reuses.
//!
//! # Where this sits
//!
//! ```text
//! bundle --reputation.adjust--> executor --host-call(db, "reputation.adjust")-->
//!   svc_process::capabilities::handle_reputation
//!     1. CapabilityGate::authorize(ReputationCommunityWrite, ReputationScoped{target, delta})
//!          grant + declared delta bounds + per-call cap + in-memory quotas
//!          + MembershipCheck (SnapshotMembership, fast pre-filter)
//!     2. flag gate / wiring check (fail-loud `not_implemented` / `feature_disabled`)
//!     3. ReputationStore::adjust   <-- THIS CRATE: the authoritative, durable part
//!          one transaction: live membership re-check -> row lock -> rolling-24h
//!          cap -> balance update -> audit-ledger insert
//! ```
//!
//! The gate's quota ledger is in-memory (per process, resets on restart, not
//! shared across replicas); the store's cap is the durable one, derived from
//! the audit ledger under the score row's lock, so concurrent adjusts for the
//! same user serialize and the cap can never be exceeded by a race, a restart
//! or a second replica.
//!
//! # Scope derivation
//!
//! The bundle supplies only `user` (a target UUID), `delta` and `reason`.
//! `tenant_id`, `community_id` and `app_id` come from the host-built
//! [`ReputationScope`] -- never from guest input. The user must be an ACTIVE
//! member of that community of that tenant (`community_members.user_uuid`,
//! alembic 0043) at the moment of the call, re-verified inside the same
//! transaction as the write.
//!
//! # Audit
//!
//! Every APPLIED adjustment writes one `bundle_reputation_adjustments` row
//! (alembic 0041) in the same transaction as the score update -- the score
//! can never move without a ledger row, and the ledger row can never exist
//! without the score having moved. Denied/rejected attempts never reach the
//! store; the gate records those (`bundle_capability_gate::audit`).

mod metrics;
mod postgres;

use std::future::Future;
use std::pin::Pin;

pub use postgres::{
    connect, load_membership, run_membership_refresh, ConnectConfig, PostgresReputationStore,
};

/// Boxed future, so [`ReputationStore`] is object-safe without `async_trait`.
pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// Longest accepted `reason` code (matches the ledger's `VARCHAR(100)`).
pub const MAX_REASON_LEN: usize = 100;

/// The host-derived scope of one reputation call (never guest-supplied).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ReputationScope {
    pub tenant_id: i32,
    pub community_id: i32,
    pub app_id: String,
}

/// Every way a store call can fail. `wire_code` is the stable string the
/// stage puts in the host-result error and the executor maps back onto the
/// WIT `reputation.error` variant.
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum ReputationError {
    /// The target is not an active member of the invocation's community.
    #[error("target user is not an active member of this community")]
    NotAMember,
    /// Applying `delta` would push the user's rolling-24h absolute-delta
    /// total past `cap`.
    #[error("rolling-24h per-user reputation cap ({cap}) would be exceeded")]
    DailyCapExceeded { cap: i64 },
    /// Malformed argument (bad `reason`, ...).
    #[error("invalid argument: {0}")]
    Invalid(String),
    /// Database/connection/timeout failure.
    #[error("backend error: {0}")]
    Backend(String),
}

impl ReputationError {
    /// Stable machine code carried over the host-API wire.
    pub fn wire_code(&self) -> &'static str {
        match self {
            Self::NotAMember => "not_a_member",
            Self::DailyCapExceeded { .. } => "daily_cap_exceeded",
            Self::Invalid(_) => "invalid_args",
            Self::Backend(_) => "backend",
        }
    }
}

/// Validates a `reason` code: 1..=[`MAX_REASON_LEN`] chars of
/// `[a-z0-9._:-]`. Rejecting everything else keeps free text -- and so PII --
/// out of the audit ledger by construction.
pub fn validate_reason(reason: &str) -> Result<(), ReputationError> {
    if reason.is_empty() || reason.len() > MAX_REASON_LEN {
        return Err(ReputationError::Invalid(format!(
            "reason must be 1..={MAX_REASON_LEN} characters"
        )));
    }
    if !reason.bytes().all(|b| {
        b.is_ascii_lowercase() || b.is_ascii_digit() || matches!(b, b'.' | b'_' | b':' | b'-')
    }) {
        return Err(ReputationError::Invalid(
            "reason must match [a-z0-9._:-]".to_string(),
        ));
    }
    Ok(())
}

/// The durable, community-scoped reputation store.
pub trait ReputationStore: Send + Sync {
    /// Current score of `user` in `scope`'s community (0 for an active member
    /// with no adjustments). [`ReputationError::NotAMember`] if `user` is not
    /// an active member right now.
    fn get<'a>(
        &'a self,
        scope: &'a ReputationScope,
        user: uuid::Uuid,
    ) -> BoxFuture<'a, Result<i64, ReputationError>>;

    /// Atomically applies `delta` and returns the NEW score. `daily_cap` is
    /// the maximum rolling-24h sum of absolute applied deltas for this user
    /// (the catalog's `per_user_daily_abs_max`, passed in by the stage).
    fn adjust<'a>(
        &'a self,
        scope: &'a ReputationScope,
        user: uuid::Uuid,
        delta: i32,
        reason: &'a str,
        daily_cap: i64,
    ) -> BoxFuture<'a, Result<i64, ReputationError>>;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reason_accepts_machine_codes() {
        for ok in ["game.win", "loyalty:daily-bonus", "a", "x_y.z-1:2"] {
            assert!(validate_reason(ok).is_ok(), "{ok}");
        }
        assert!(validate_reason(&"a".repeat(MAX_REASON_LEN)).is_ok());
    }

    #[test]
    fn reason_rejects_empty_oversized_and_free_text() {
        assert!(validate_reason("").is_err());
        assert!(validate_reason(&"a".repeat(MAX_REASON_LEN + 1)).is_err());
        for bad in [
            "Has Upper",
            "has space",
            "user@example.com",
            "emoji-\u{1f600}",
            "semi;colon",
            "new\nline",
        ] {
            assert!(
                matches!(validate_reason(bad), Err(ReputationError::Invalid(_))),
                "{bad:?}"
            );
        }
    }

    #[test]
    fn wire_codes_are_stable() {
        assert_eq!(ReputationError::NotAMember.wire_code(), "not_a_member");
        assert_eq!(
            ReputationError::DailyCapExceeded { cap: 1 }.wire_code(),
            "daily_cap_exceeded"
        );
        assert_eq!(
            ReputationError::Invalid(String::new()).wire_code(),
            "invalid_args"
        );
        assert_eq!(
            ReputationError::Backend(String::new()).wire_code(),
            "backend"
        );
    }
}
