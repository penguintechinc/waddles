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
//!     3. ReputationStore::adjust   <-- THIS CRATE: a thin caller of the
//!          SECURITY DEFINER SQL function `reputation_apply()`, which is the
//!          authoritative part (membership, caps, idempotency, clamp, ledger)
//! ```
//!
//! The gate's quota ledger is in-memory (per process, resets on restart, not
//! shared across replicas) and is only a fast pre-filter. The durable per-user
//! / per-app / per-class rolling-24h caps, the membership check, idempotency
//! and clamping all live INSIDE the SQL function (`reputation_apply`), so no
//! race, restart or second replica can exceed them and this crate never
//! computes a cap itself. A zero `delta` is rejected up front
//! ([`validate_delta`]).
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

mod floor;
mod metrics;
mod postgres;
mod scale;

pub use floor::{
    DueFloorBan, FloorBanBegin, FloorBanOutcome, FloorScope, FLOOR_ACTOR, MAX_TENANT_FANOUT,
};

use std::future::Future;
use std::pin::Pin;

pub use postgres::{
    connect, floor_sql_key, load_membership, run_membership_refresh, ConnectConfig,
    PostgresReputationStore, FLOOR_SOFT_BAN_REASON,
};
pub use scale::{
    ReasonClass, ScoreScale, ACTIVITY_CAP, MODERATION_CAP, SCALE_DEFAULT, SCALE_MAX, SCALE_MIN,
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

/// Largest `history` page.
pub const MAX_HISTORY_LIMIT: u32 = 100;

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

/// Result of [`ReputationStore::apply`] (mirrors `reputation_apply()`'s
/// `(new_score, applied)`): `applied = false` means the idempotency key had
/// already been used with the same parameters, so nothing moved.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ApplyOutcome {
    pub new_score: i64,
    pub applied: bool,
}

/// A user's own aggregate across the communities of one tenant.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TenantScore {
    /// Rounded mean of the user's per-community scores (the legacy default
    /// when the user has no score rows yet).
    pub score: i64,
    /// How many communities contributed a score row.
    pub communities: u32,
}

/// One audit-ledger row of a user's history (UUID/numeric only, no PII).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HistoryEntry {
    /// Ledger row id, usable as the `before` cursor of the next page.
    pub id: i64,
    pub delta: i32,
    pub reason_code: String,
    pub kind: String,
    pub balance_after: Option<i64>,
    /// RFC 3339 UTC.
    pub occurred_at: String,
}

/// A tenant's auto-ban policy (set by tenant admins through hub-api, stored in
/// the hub-api-owned `reputation_tenant_policy` table, read-only here).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TenantPolicy {
    pub auto_ban_enabled: bool,
    /// Ban when the user's tenant aggregate falls below this (300..=850).
    pub auto_ban_floor: i64,
    /// At-risk reporting line; must exceed the floor (<= 850).
    pub at_risk_threshold: i64,
}

impl TenantPolicy {
    /// Validates `300 <= floor < at_risk <= 850` (same rule hub-api enforces).
    pub fn validate(&self) -> Result<(), ReputationError> {
        if self.auto_ban_floor < SCALE_MIN
            || self.at_risk_threshold > SCALE_MAX
            || self.auto_ban_floor >= self.at_risk_threshold
        {
            return Err(ReputationError::Invalid(
                "tenant policy must satisfy 300 <= floor < at_risk <= 850".to_string(),
            ));
        }
        Ok(())
    }
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
    /// A rolling-24h cap or quota enforced by the SQL functions was hit
    /// (`cap` is 0 when the function does not report it): nothing changed.
    #[error("reputation rate limit exceeded (cap {cap})")]
    RateLimited { cap: i64 },
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
            Self::RateLimited { .. } => "rate_limited",
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
    /// Current score of `user` in `scope`'s community
    /// (`COALESCE(balance, policy score_default, 600)` for an unscored active
    /// member). [`ReputationError::NotAMember`] if `user` is not
    /// an active member right now.
    fn get<'a>(
        &'a self,
        scope: &'a ReputationScope,
        user: uuid::Uuid,
    ) -> BoxFuture<'a, Result<i64, ReputationError>>;

    /// Atomically applies a non-zero `delta` and returns the NEW score.
    /// `caps` are the durable per-user and per-scope rolling-24h ceilings
    /// (see [`ReputationCaps`]); both are enforced inside the write
    /// transaction. `key` is REQUIRED (host-derived from the event id) so a
    /// retried call is never double-counted. [`ReputationError::Invalid`] for
    /// a zero `delta`.
    fn adjust<'a>(
        &'a self,
        scope: &'a ReputationScope,
        user: uuid::Uuid,
        delta: i32,
        reason: &'a str,
        caps: ReputationCaps,
        key: &'a IdempotencyKey,
    ) -> BoxFuture<'a, Result<i64, ReputationError>>;

    /// ADMIN override. NOT available to the bundle role: the SQL contract
    /// denies `admin_override` (class 3) to it, so admin overrides are
    /// hub-api only. Kept on the trait so stores/doubles stay source
    /// compatible; the default fails loudly.
    fn override_score<'a>(
        &'a self,
        _scope: &'a ReputationScope,
        _user: uuid::Uuid,
        _absolute_score: i64,
        _reason: &'a str,
        _key: &'a IdempotencyKey,
    ) -> BoxFuture<'a, Result<i64, ReputationError>> {
        Box::pin(async { Err(unsupported("override_score")) })
    }

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
    /// highest first, ties broken by user UUID. Unscored members count at
    /// `COALESCE(balance, policy score_default, 600)`.
    fn leaderboard<'a>(
        &'a self,
        scope: &'a ReputationScope,
        limit: u32,
    ) -> BoxFuture<'a, Result<Vec<ScoreEntry>, ReputationError>>;

    /// Applies one classed, idempotent delta through the shared SQL function
    /// `reputation_apply(p_tenant, p_community, p_user_uuid, p_delta,
    /// p_reason_class, p_reason_code, p_app_id, p_actor, p_source_event,
    /// p_idempotency_key)`, which AUTHORITATIVELY owns membership, the
    /// rolling-24h caps, idempotency, the scale clamp and the ledger row
    /// (kinds `adjust` / `moderation` / `admin_override`). The store only
    /// enforces the host-side class bound ([`ScoreScale::check_delta`]);
    /// `caps` is the gate's own quota and is NOT recomputed against the
    /// ledger. `class` MUST be derived by the host from the granted
    /// permission, never from guest input.
    #[allow(clippy::too_many_arguments)]
    fn apply<'a>(
        &'a self,
        _scope: &'a ReputationScope,
        _user: uuid::Uuid,
        _delta: i32,
        _class: ReasonClass,
        _source_event: &'a str,
        _caps: ReputationCaps,
        _key: &'a IdempotencyKey,
    ) -> BoxFuture<'a, Result<ApplyOutcome, ReputationError>> {
        Box::pin(async { Err(unsupported("apply")) })
    }

    /// `user`'s OWN aggregate across this tenant's communities. The caller
    /// (the host) must have verified `user` is the invoking actor.
    fn get_tenant<'a>(
        &'a self,
        _scope: &'a ReputationScope,
        _user: uuid::Uuid,
    ) -> BoxFuture<'a, Result<TenantScore, ReputationError>> {
        Box::pin(async { Err(unsupported("get_tenant")) })
    }

    /// Newest-first ledger page for `user` in this community: up to `limit`
    /// (`1..=`[`MAX_HISTORY_LIMIT`]) rows with `id < before` when given.
    fn history<'a>(
        &'a self,
        _scope: &'a ReputationScope,
        _user: uuid::Uuid,
        _limit: u32,
        _before: Option<i64>,
    ) -> BoxFuture<'a, Result<Vec<HistoryEntry>, ReputationError>> {
        Box::pin(async { Err(unsupported("history")) })
    }

    /// Active members scored strictly below `threshold`, lowest first (ties
    /// by user id), up to `limit` (`1..=`[`MAX_LEADERBOARD_LIMIT`]).
    fn below_threshold<'a>(
        &'a self,
        _scope: &'a ReputationScope,
        _threshold: i64,
        _limit: u32,
    ) -> BoxFuture<'a, Result<Vec<ScoreEntry>, ReputationError>> {
        Box::pin(async { Err(unsupported("below_threshold")) })
    }

    /// The tenant's auto-ban policy, or `None` when the tenant has none.
    fn tenant_policy<'a>(
        &'a self,
        _scope: &'a ReputationScope,
    ) -> BoxFuture<'a, Result<Option<TenantPolicy>, ReputationError>> {
        Box::pin(async { Err(unsupported("tenant_policy")) })
    }

    /// The app id of the tenant's ACTIVE reputation-logic bundle (via
    /// `reputation_active_logic_app(tenant)`, which applies the manifest
    /// `provides: reputation-logic` + exclusivity rules), or `None`.
    fn active_logic_app<'a>(
        &'a self,
        _scope: &'a ReputationScope,
    ) -> BoxFuture<'a, Result<Option<String>, ReputationError>> {
        Box::pin(async { Err(unsupported("active_logic_app")) })
    }

    /// Whether `user` is soft-banned in this community (cannot WIN anything:
    /// games, raffles, giveaways, economy payouts). Reads through
    /// `reputation_is_soft_banned()`.
    fn is_soft_banned<'a>(
        &'a self,
        _scope: &'a ReputationScope,
        _user: uuid::Uuid,
    ) -> BoxFuture<'a, Result<bool, ReputationError>> {
        Box::pin(async { Err(unsupported("is_soft_banned")) })
    }

    /// Records the FLOOR soft ban (reason code 1) for `user` via
    /// `reputation_set_floor_soft_ban()`; `reason` must be the floor reason.
    /// Manual soft bans are hub-api-only. Idempotent: already-banned returns
    /// `Ok(false)`, a new one `Ok(true)`.
    fn soft_ban<'a>(
        &'a self,
        _scope: &'a ReputationScope,
        _user: uuid::Uuid,
        _reason: &'a str,
    ) -> BoxFuture<'a, Result<bool, ReputationError>> {
        Box::pin(async { Err(unsupported("soft_ban")) })
    }

    /// Clears the FLOOR soft ban of `user` only (`only_reason` must be the
    /// floor reason; a manual ban is never touched). `Ok(true)` when a ban
    /// was lifted.
    fn lift_soft_ban<'a>(
        &'a self,
        _scope: &'a ReputationScope,
        _user: uuid::Uuid,
        _only_reason: &'a str,
    ) -> BoxFuture<'a, Result<bool, ReputationError>> {
        Box::pin(async { Err(unsupported("lift_soft_ban")) })
    }

    /// Re-verifies an automated floor ban in SQL (`reputation_verify_floor_breach`): `true` only if
    /// the scope's ban is enabled AND the user's score is below its floor. `scope.community_id`
    /// is the community to verify (tenant scope: the target community of the fan-out).
    fn verify_floor_breach<'a>(
        &'a self,
        _scope: &'a ReputationScope,
        _user: uuid::Uuid,
        _floor: FloorScope,
    ) -> BoxFuture<'a, Result<bool, ReputationError>> {
        Box::pin(async { Err(unsupported("verify_floor_breach")) })
    }

    /// Claims the (`target_community`, user, scope, `key`) floor-ban slot; `scope.community_id` is
    /// the ORIGIN community. See [`FloorBanBegin`].
    fn floor_ban_begin<'a>(
        &'a self,
        _scope: &'a ReputationScope,
        _target_community: i32,
        _user: uuid::Uuid,
        _floor: FloorScope,
        _key: &'a str,
        _platform: &'a str,
    ) -> BoxFuture<'a, Result<FloorBanBegin, ReputationError>> {
        Box::pin(async { Err(unsupported("floor_ban_begin")) })
    }

    /// Records the outcome of a begun attempt; `error` is a stable machine code, never free text.
    #[allow(clippy::too_many_arguments)]
    fn floor_ban_record<'a>(
        &'a self,
        _scope: &'a ReputationScope,
        _target_community: i32,
        _user: uuid::Uuid,
        _floor: FloorScope,
        _key: &'a str,
        _outcome: FloorBanOutcome,
        _error: &'a str,
    ) -> BoxFuture<'a, Result<(), ReputationError>> {
        Box::pin(async { Err(unsupported("floor_ban_record")) })
    }

    /// Claims (leases) up to `limit` due floor-ban retries of `scope.tenant_id`.
    fn floor_ban_due<'a>(
        &'a self,
        _scope: &'a ReputationScope,
        _limit: u32,
    ) -> BoxFuture<'a, Result<Vec<DueFloorBan>, ReputationError>> {
        Box::pin(async { Err(unsupported("floor_ban_due")) })
    }

    /// The tenant's live community ids (the tenant-ban fan-out set), host-enumerated in SQL.
    /// More than [`MAX_TENANT_FANOUT`] communities is an error, never a truncated set.
    fn tenant_communities<'a>(
        &'a self,
        _scope: &'a ReputationScope,
    ) -> BoxFuture<'a, Result<Vec<i32>, ReputationError>> {
        Box::pin(async { Err(unsupported("tenant_communities")) })
    }
}

/// Fail-loud error for a store that does not implement an op (never a silent
/// empty result).
fn unsupported(op: &str) -> ReputationError {
    ReputationError::Backend(format!("reputation store does not implement {op}"))
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

    #[test]
    fn tenant_policy_validates_floor_below_at_risk_within_scale() {
        let ok = |f, a| {
            TenantPolicy {
                auto_ban_enabled: false,
                auto_ban_floor: f,
                at_risk_threshold: a,
            }
            .validate()
        };
        assert!(ok(300, 301).is_ok());
        assert!(ok(350, 850).is_ok());
        assert!(ok(299, 400).is_err());
        assert!(ok(400, 400).is_err());
        assert!(ok(500, 400).is_err());
        assert!(ok(400, 851).is_err());
    }
}
