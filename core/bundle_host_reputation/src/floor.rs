//! Automated hard-ban (reputation floor) types shared by the store trait, the Postgres store and
//! the `svc_process` host gate. The SQL half is `scripts/db/bundle_reputation_floor_ban.sql`
//! (alembic 0099): re-verification, the audit/idempotency/durable-retry table and the fan-out set.

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

/// Result of enqueueing a floor ban (`reputation_floor_ban_enqueue`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FloorEnqueue {
    /// A QUEUED row was recorded (breach re-verified in SQL under the community lock); the
    /// action stage will dispatch the `mod.ban` command. Carries the row id (the correlation id).
    Queued(i64),
    /// The slot was already queued / in flight / done / failed / dead: an idempotent no-op.
    AlreadyKnown(i64),
    /// The per-community hourly quota was hit: recorded, retried after the quota window.
    RateLimited(i64),
    /// SQL re-verification said the floor is not breached (or the switch is off): recorded.
    Refused(i64),
}

impl FloorEnqueue {
    /// Parses the SQL `r_state` smallint of an enqueue result (0 queued, 1 known, 2 limited,
    /// 3 refused) plus the row id.
    #[must_use]
    pub fn from_sql(state: i16, id: i64) -> Option<Self> {
        match state {
            0 => Some(Self::Queued(id)),
            1 => Some(Self::AlreadyKnown(id)),
            2 => Some(Self::RateLimited(id)),
            3 => Some(Self::Refused(id)),
            _ => None,
        }
    }
}

/// Persisted status of a floor-ban row (the SQL `status` smallint).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FloorBanStatus {
    /// Verified and queued for the action stage.
    Queued = 1,
    /// The sender confirmed the ban.
    Done = 2,
    /// Failed; a retry is due at `next_retry_at`.
    Failed = 3,
    /// Attempts exhausted: needs an operator (alerted).
    Dead = 4,
    /// Quota hit; retried after the quota window.
    RateLimited = 5,
    /// The floor was not breached at (re-)verification.
    Refused = 6,
    /// In flight under a lease.
    Dispatched = 7,
}

impl FloorBanStatus {
    /// Parses the SQL `status` smallint.
    #[must_use]
    pub fn from_code(code: i16) -> Option<Self> {
        match code {
            1 => Some(Self::Queued),
            2 => Some(Self::Done),
            3 => Some(Self::Failed),
            4 => Some(Self::Dead),
            5 => Some(Self::RateLimited),
            6 => Some(Self::Refused),
            7 => Some(Self::Dispatched),
            _ => None,
        }
    }
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
    fn enqueue_states_and_statuses_match_the_sql_contract() {
        assert_eq!(FloorEnqueue::from_sql(0, 9), Some(FloorEnqueue::Queued(9)));
        assert_eq!(
            FloorEnqueue::from_sql(1, 9),
            Some(FloorEnqueue::AlreadyKnown(9))
        );
        assert_eq!(
            FloorEnqueue::from_sql(2, 9),
            Some(FloorEnqueue::RateLimited(9))
        );
        assert_eq!(FloorEnqueue::from_sql(3, 9), Some(FloorEnqueue::Refused(9)));
        assert_eq!(FloorEnqueue::from_sql(4, 9), None);
        for code in 1..=7 {
            assert_eq!(
                FloorBanStatus::from_code(code).map(|s| s as i16),
                Some(code)
            );
        }
        assert_eq!(FloorBanStatus::from_code(0), None);
        assert_eq!(FloorBanStatus::from_code(8), None);
    }
}
