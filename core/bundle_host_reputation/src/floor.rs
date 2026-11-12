//! Automated hard-ban (reputation floor) types shared by the store trait, the Postgres store and
//! the `svc_process` host gate. The SQL half is `scripts/db/bundle_reputation_floor_ban.sql`
//! (alembic 0098): re-verification, the audit/idempotency/durable-retry table and the fan-out set.

/// Largest tenant the tenant-floor ban fans out over (host-enumerated; more fails loudly).
pub const MAX_TENANT_FANOUT: usize = 1000;

/// The host-asserted automated actor of a floor ban. Never guest input; attached only by the
/// `svc_process` floor gate and the only actor the SQL audit table accepts.
pub const FLOOR_ACTOR: &str = "system:reputation_floor";

/// Which automated floor policy a platform ban re-verifies and records (SQL `scope` smallint).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FloorScope {
    /// Community hard floor (`hard_ban_enabled` + `hard_ban_floor`): ban in ONE community.
    CommunityHard,
    /// Tenant floor on the fixed 300..850 aggregate: ban across EVERY community of the tenant.
    Tenant,
}

impl FloorScope {
    /// The SQL `scope` smallint (1 = community hard, 2 = tenant).
    #[must_use]
    pub fn code(self) -> i16 {
        match self {
            Self::CommunityHard => 1,
            Self::Tenant => 2,
        }
    }

    /// The audit reason string of this scope (also the ban tool's `reason` argument).
    #[must_use]
    pub fn reason(self) -> &'static str {
        match self {
            Self::CommunityHard => "community_hard_floor",
            Self::Tenant => "tenant_floor",
        }
    }

    /// Parses the ban tool's `reason`; only the two floor reasons are accepted.
    #[must_use]
    pub fn from_reason(reason: &str) -> Option<Self> {
        match reason {
            "community_hard_floor" => Some(Self::CommunityHard),
            "tenant_floor" => Some(Self::Tenant),
            _ => None,
        }
    }

    /// Inverse of [`Self::code`].
    #[must_use]
    pub fn from_code(code: i16) -> Option<Self> {
        match code {
            1 => Some(Self::CommunityHard),
            2 => Some(Self::Tenant),
            _ => None,
        }
    }
}

/// Result of claiming a floor-ban slot (`reputation_floor_ban_begin`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FloorBanBegin {
    /// Slot claimed: verify, act, then record the outcome.
    Proceed,
    /// Idempotent replay of an already-done ban: do nothing.
    AlreadyDone,
    /// Per-community floor-ban throttle hit: do nothing (recorded).
    RateLimited,
}

/// Outcome recorded for a begun floor-ban attempt (`reputation_floor_ban_record`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FloorBanOutcome {
    /// The platform ban succeeded.
    Done,
    /// The platform ban failed; a durable retry is scheduled.
    Failed,
    /// Re-verification refused the ban (floor not breached).
    Refused,
}

impl FloorBanOutcome {
    /// The SQL `p_outcome` smallint.
    #[must_use]
    pub fn code(self) -> i16 {
        match self {
            Self::Done => 1,
            Self::Failed => 2,
            Self::Refused => 3,
        }
    }
}

/// One floor ban whose durable retry is due (claimed with a lease by `floor_ban_due`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DueFloorBan {
    /// The community the ban must be executed in.
    pub community_id: i32,
    /// The community whose score event originally triggered it.
    pub origin_community_id: i32,
    /// The banned user (UUID only).
    pub user: uuid::Uuid,
    /// Which floor policy.
    pub scope: FloorScope,
    /// The SQL-normalised idempotency key of the original attempt.
    pub key: String,
    /// The platform (`discord`, `twitch`, ...) whose `<platform>.moderation.ban` tool to retry.
    pub platform: String,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scope_codes_and_reasons_round_trip() {
        for s in [FloorScope::CommunityHard, FloorScope::Tenant] {
            assert_eq!(FloorScope::from_code(s.code()), Some(s));
            assert_eq!(FloorScope::from_reason(s.reason()), Some(s));
        }
        assert_eq!(FloorScope::from_code(0), None);
        assert_eq!(FloorScope::from_reason("community_soft_floor"), None);
        assert_eq!(FloorScope::from_reason(""), None);
    }

    #[test]
    fn outcome_codes_match_the_sql_contract() {
        assert_eq!(FloorBanOutcome::Done.code(), 1);
        assert_eq!(FloorBanOutcome::Failed.code(), 2);
        assert_eq!(FloorBanOutcome::Refused.code(), 3);
    }
}
