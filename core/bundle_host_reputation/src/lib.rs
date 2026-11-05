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
//!          one transaction: live membership re-check -> per-scope advisory
//!          lock -> score-row lock -> rolling-24h per-user cap -> rolling-24h
//!          per-scope cap -> balance update -> audit-ledger insert
//! ```
//!
//! The gate's quota ledger is in-memory (per process, resets on restart, not
//! shared across replicas); the store's caps are the durable ones, both
//! derived from the audit ledger inside the write transaction:
//!
//! * **per-user** (`per_user_daily_abs_max`): the SUM of the user's applied
//!   `|delta|` over the rolling 24h, read under the score row's lock;
//! * **per-scope** (`per_scope_daily_abs_max`, the Dangerous-grant blast-radius
//!   cap): the SUM of every applied `|delta|` by this app in this
//!   (tenant, community) over the rolling 24h, read under a transaction-scoped
//!   advisory lock keyed on that scope (adjusts for DIFFERENT users share no
//!   row, so the score-row lock alone cannot serialize them).
//!
//! Concurrent adjusts therefore serialize on the lock that guards the cap they
//! are checked against, and neither cap can be exceeded by a race, a restart
//! or a second replica. A zero `delta` is rejected up front
//! ([`validate_delta`]): it would otherwise consume no quota yet still append
//! a ledger row and bump `adjustment_count`, an unbounded-growth vector.
//!
//! # Scope derivation
//!
//! The bundle supplies only `user` (a target UUID), `delta` and `reason`.
//! `tenant_id`, `community_id` and `app_id` come from the host-built
//! [`ReputationScope`] -- never from guest input. The user must be an ACTIVE
//! member of that community of that tenant (`community_members.user_uuid`,
//! alembic 0050) at the moment of the call, re-verified inside the same
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

/// Lowest score the store will persist. Every write is clamped into
/// `MIN_SCORE..=MAX_SCORE`; the `ck_bundle_reputation_scores_balance_range`
/// CHECK (`scripts/db/bundle_reputation_host_ops.sql`) is the DB backstop and
/// MUST stay in lockstep with these two. Both fit `i32` doubled, so the
/// ledger's `INTEGER delta` can hold even a full-range override.
pub const MIN_SCORE: i64 = -1_000_000;
/// Highest score the store will persist (see [`MIN_SCORE`]).
pub const MAX_SCORE: i64 = 1_000_000;

/// Largest batch `adjust_many` / `get_many` accept (bounds one transaction's
/// lock set and one round-trip's size).
pub const MAX_BATCH: usize = 100;

/// Largest `leaderboard` page.
pub const MAX_LEADERBOARD_LIMIT: u32 = 100;

/// Longest accepted idempotency key (the ledger column is `VARCHAR(128)`).
pub const MAX_IDEMPOTENCY_KEY_LEN: usize = 128;

/// Clamps a raw score into the persisted range.
pub fn clamp_score(raw: i64) -> i64 {
    raw.clamp(MIN_SCORE, MAX_SCORE)
}

/// The replay-stable identity of ONE mutating call (`override`,
/// `adjust_many`), scoped by the store to `(tenant, community, app)`. Built by
/// the HOST from the event id + mutation ordinal -- never taken from a guest --
/// so a redelivered call is applied at most once. Same charset/length rules as
/// the economy's key: `[A-Za-z0-9:_.-]`, `1..=`[`MAX_IDEMPOTENCY_KEY_LEN`].
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct IdempotencyKey(String);

impl IdempotencyKey {
    /// Validates `raw`. The error never echoes the offending value.
    pub fn new(raw: impl Into<String>) -> Result<Self, ReputationError> {
        let raw = raw.into();
        if raw.is_empty() || raw.len() > MAX_IDEMPOTENCY_KEY_LEN {
            return Err(ReputationError::Invalid(format!(
                "idempotency key must be 1..={MAX_IDEMPOTENCY_KEY_LEN} bytes"
            )));
        }
        if !raw
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b':' | b'_' | b'.' | b'-'))
        {
            return Err(ReputationError::Invalid(
                "idempotency key may contain only [A-Za-z0-9:_.-]".to_string(),
            ));
        }
        Ok(Self(raw))
    }

    /// The key of the `ordinal`-th `kind` mutation made while handling the
    /// event `event_id`: `"<event_id>:<kind>:<ordinal>"`.
    pub fn for_event(event_id: &str, kind: &str, ordinal: u32) -> Result<Self, ReputationError> {
        Self::new(format!("{event_id}:{kind}:{ordinal}"))
    }

    /// The key text.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

/// One leaderboard / batch-read row: a member (UUID only) and their score.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ScoreEntry {
    pub user: uuid::Uuid,
    pub score: i64,
}

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
    /// Applying `delta` would push this app's rolling-24h absolute-delta total
    /// for the whole (tenant, community) past `cap`. Surfaces under the same
    /// stable `quota_exceeded` code the gate's in-memory per-scope check uses,
    /// so a bundle sees one code however the aggregate cap tripped.
    #[error("rolling-24h per-scope reputation cap ({cap}) would be exceeded")]
    ScopeQuotaExceeded { cap: i64 },
    /// The idempotency key was already used by a DIFFERENT operation (or the
    /// same one with different parameters): nothing was applied.
    #[error("idempotency key was already used by a different reputation operation")]
    IdempotencyConflict,
    /// Malformed argument (bad `reason`, zero `delta`, ...).
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
            Self::ScopeQuotaExceeded { .. } => "quota_exceeded",
            Self::IdempotencyConflict => "idempotency_conflict",
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

/// Rejects a zero `delta`: it passes the gate's bounds and quotas (a 0-amount
/// consumption) yet would open a transaction, append a ledger row and bump
/// `adjustment_count` on every call with no rate limit. Shared by the stage
/// handler and the store so neither relies on the other.
pub fn validate_delta(delta: i32) -> Result<(), ReputationError> {
    if delta == 0 {
        return Err(ReputationError::Invalid(
            "delta must be non-zero".to_string(),
        ));
    }
    Ok(())
}

/// The two durable rolling-24h absolute-delta ceilings one `adjust` is checked
/// against (the catalog's `reputation.community.write` `Quota::ReputationDelta`
/// `per_user_daily_abs_max` / `per_scope_daily_abs_max`, passed in by the
/// stage). A struct rather than two bare `i64`s so the two can never be
/// transposed at a call site.
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub struct ReputationCaps {
    /// Max rolling-24h sum of absolute applied deltas for one target user.
    pub per_user_daily_abs_max: i64,
    /// Max rolling-24h sum of absolute applied deltas by one app across the
    /// whole (tenant, community).
    pub per_scope_daily_abs_max: i64,
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

    /// Atomically applies a non-zero `delta` and returns the NEW score.
    /// `caps` are the durable per-user and per-scope rolling-24h ceilings
    /// (see [`ReputationCaps`]); both are enforced inside the write
    /// transaction. [`ReputationError::Invalid`] for a zero `delta`.
    fn adjust<'a>(
        &'a self,
        scope: &'a ReputationScope,
        user: uuid::Uuid,
        delta: i32,
        reason: &'a str,
        caps: ReputationCaps,
    ) -> BoxFuture<'a, Result<i64, ReputationError>>;

    /// ADMIN override: sets `user`'s score to `absolute_score` (clamped into
    /// [`MIN_SCORE`]..=[`MAX_SCORE`]) and returns the NEW score. NOT a capped
    /// delta -- the rolling-24h caps neither bind nor are consumed by it --
    /// so the caller (the host) MUST have authorized the dedicated admin
    /// permission first. Ledgered as kind `admin_override`; idempotent on
    /// `key` (a replay with the same parameters returns the original result,
    /// different parameters are [`ReputationError::IdempotencyConflict`]).
    fn override_score<'a>(
        &'a self,
        scope: &'a ReputationScope,
        user: uuid::Uuid,
        absolute_score: i64,
        reason: &'a str,
        key: &'a IdempotencyKey,
    ) -> BoxFuture<'a, Result<i64, ReputationError>>;

    /// Atomically applies every `(user, delta)` in ONE transaction: either
    /// all land or none do. Per-user and per-scope rolling-24h caps apply
    /// exactly as for [`Self::adjust`] (the batch's total counts against the
    /// per-scope cap). `1..=`[`MAX_BATCH`] items, no duplicate users, no zero
    /// delta. Idempotent on `key`.
    fn adjust_many<'a>(
        &'a self,
        scope: &'a ReputationScope,
        items: &'a [(uuid::Uuid, i32)],
        reason: &'a str,
        caps: ReputationCaps,
        key: &'a IdempotencyKey,
    ) -> BoxFuture<'a, Result<(), ReputationError>>;

    /// Scores of `users` in one round-trip, in the request order (duplicates
    /// collapsed). `1..=`[`MAX_BATCH`] users; any non-member fails the whole
    /// call with [`ReputationError::NotAMember`] (fail-closed).
    fn get_many<'a>(
        &'a self,
        scope: &'a ReputationScope,
        users: &'a [uuid::Uuid],
    ) -> BoxFuture<'a, Result<Vec<ScoreEntry>, ReputationError>>;

    /// Top `limit` (`1..=`[`MAX_LEADERBOARD_LIMIT`]) ACTIVE members by score,
    /// highest first, ties broken by user UUID. Members with no score row
    /// (never adjusted, implicitly 0) are not listed.
    fn leaderboard<'a>(
        &'a self,
        scope: &'a ReputationScope,
        limit: u32,
    ) -> BoxFuture<'a, Result<Vec<ScoreEntry>, ReputationError>>;
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
    fn zero_delta_is_rejected_nonzero_accepted() {
        assert!(matches!(
            validate_delta(0),
            Err(ReputationError::Invalid(_))
        ));
        for ok in [1, -1, i32::MAX, i32::MIN] {
            assert!(validate_delta(ok).is_ok(), "{ok}");
        }
    }

    #[test]
    fn clamp_pins_to_the_persisted_range() {
        assert_eq!(clamp_score(0), 0);
        assert_eq!(clamp_score(i64::MAX), MAX_SCORE);
        assert_eq!(clamp_score(i64::MIN), MIN_SCORE);
        assert_eq!(clamp_score(MAX_SCORE + 1), MAX_SCORE);
        assert_eq!(clamp_score(MIN_SCORE - 1), MIN_SCORE);
        // A full-range override delta must fit the ledger's INTEGER column.
        assert!(i32::try_from(MAX_SCORE - MIN_SCORE).is_ok());
    }

    #[test]
    fn idempotency_keys_are_validated_and_event_derived() {
        assert_eq!(
            IdempotencyKey::for_event("evt-1", "override", 2)
                .unwrap()
                .as_str(),
            "evt-1:override:2"
        );
        for bad in ["", "has space", "semi;colon", "new\nline"] {
            assert!(
                matches!(IdempotencyKey::new(bad), Err(ReputationError::Invalid(_))),
                "{bad:?}"
            );
        }
        assert!(IdempotencyKey::new("a".repeat(MAX_IDEMPOTENCY_KEY_LEN)).is_ok());
        assert!(IdempotencyKey::new("a".repeat(MAX_IDEMPOTENCY_KEY_LEN + 1)).is_err());
    }

    #[test]
    fn wire_codes_are_stable() {
        assert_eq!(ReputationError::NotAMember.wire_code(), "not_a_member");
        assert_eq!(
            ReputationError::DailyCapExceeded { cap: 1 }.wire_code(),
            "daily_cap_exceeded"
        );
        assert_eq!(
            ReputationError::ScopeQuotaExceeded { cap: 1 }.wire_code(),
            "quota_exceeded"
        );
        assert_eq!(
            ReputationError::Invalid(String::new()).wire_code(),
            "invalid_args"
        );
        assert_eq!(
            ReputationError::Backend(String::new()).wire_code(),
            "backend"
        );
        assert_eq!(
            ReputationError::IdempotencyConflict.wire_code(),
            "idempotency_conflict"
        );
    }
}
