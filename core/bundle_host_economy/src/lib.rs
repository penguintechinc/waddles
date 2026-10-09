//! The bundle `economy` host capability's store (issue #714) -- the shared
//! community currency, cloned from `core/bundle_host_reputation`'s recipe.
//!
//! # Where this sits
//!
//! ```text
//! bundle --economy.wager--> executor --host-call(db, "economy.wager")-->
//!   svc_process::capabilities::handle_economy
//!     1. CapabilityGate::authorize(EconomyWager, EconomyScoped{player, stake})
//!          grant + declared max_bet + EconomyAmount quotas
//!          + MembershipCheck (SnapshotMembership, fast pre-filter)
//!     2. flag gate / wiring check (fail-loud `not_implemented` / `feature_disabled`)
//!     3. EconomyStore::wager   <-- THIS CRATE: the authoritative, durable part
//!          ONE statement: live membership check + balance guard + debit/credit
//!          + ledger row (data-modifying CTEs)
//! ```
//!
//! # Atomicity
//!
//! Money never moves through a read-modify-write. A wager is the single
//! guarded statement
//! `UPDATE economy_balances SET balance = balance - $stake + $payout
//!  WHERE ... AND balance >= $stake RETURNING balance`
//! so N concurrent wagers on one balance serialize on the row lock and each
//! re-evaluates `balance >= stake` against the committed value: the balance
//! can never be overdrawn by a race, a restart or a second replica (the table
//! additionally carries `CHECK (balance >= 0)` as the database's own
//! backstop). The ledger row is inserted by a data-modifying CTE of the SAME
//! statement, so the balance cannot move without a ledger row. A transfer
//! locks both rows in a deterministic (uuid) order inside one transaction
//! (opposite-direction transfers cannot deadlock) and then moves the money in
//! one statement.
//!
//! # Server-enforced caps
//!
//! `max_bet` (wager) / `max_amount` (transfer) is computed by the STAGE from
//! the grant's declared param clamped to the catalog ceiling
//! (`PermissionFamily::economy_amount_bound`) and passed in; the store
//! enforces it ([`EconomyError::OverCap`]) before touching the database, so
//! even a bug in the gate can't let an oversized stake through. A payout may
//! not exceed `stake * `[`MAX_PAYOUT_MULTIPLE`] -- the bundle decides the game
//! outcome, so the host bounds how much a single call can mint.
//!
//! # Scope derivation
//!
//! The bundle supplies only target user UUID(s) and amounts. `tenant_id`,
//! `community_id` and `app_id` come from the host-built [`EconomyScope`] --
//! never from guest input. Every named user must be an ACTIVE member of that
//! community of that tenant (`community_members.user_uuid`, alembic 0043) at
//! the moment of the call, re-verified inside the write itself.
//!
//! # Funding
//!
//! This capability only MOVES existing balance (wager outcomes and
//! transfers). Initial funding / earn flows are a hub-side concern (the
//! loyalty module) written by a different, privileged writer; a member with
//! no `economy_balances` row reads as balance 0.

mod metrics;
mod postgres;

use std::future::Future;
use std::pin::Pin;

pub use postgres::{
    connect, load_membership, run_membership_refresh, ConnectConfig, PostgresEconomyStore,
    MAX_SNAPSHOT_ROWS,
};

/// Boxed future, so [`EconomyStore`] is object-safe without `async_trait`.
pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// A single wager's payout may be at most this many times its stake.
pub const MAX_PAYOUT_MULTIPLE: i64 = 100;

/// Largest accepted leaderboard page.
pub const MAX_LEADERBOARD_LIMIT: u32 = 100;

/// The host-derived scope of one economy call (never guest-supplied).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EconomyScope {
    pub tenant_id: i32,
    pub community_id: i32,
    pub app_id: String,
}

/// One leaderboard row: a member (UUID only) and their balance.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LeaderboardEntry {
    pub user: uuid::Uuid,
    pub balance: i64,
}

/// Every way a store call can fail. [`EconomyError::wire_code`] is the stable
/// string the stage puts in the host-result error and the executor maps back
/// onto the WIT `economy.error` variant; the two numeric variants carry their
/// number in the wire message ([`EconomyError::wire_message`]).
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum EconomyError {
    /// A named user is not an active member of the invocation's community.
    #[error("target user is not an active member of this community")]
    NotAMember,
    /// The debited user holds less than the stake/amount; `balance` is what
    /// they hold.
    #[error("insufficient funds: balance is {balance}")]
    InsufficientFunds { balance: i64 },
    /// The stake/amount (or a payout) exceeds the enforced cap `cap`.
    #[error("amount exceeds the enforced cap ({cap})")]
    OverCap { cap: i64 },
    /// Malformed argument (non-positive amount, self-transfer, bad limit, ...).
    #[error("invalid argument: {0}")]
    Invalid(String),
    /// Database/connection/timeout failure.
    #[error("backend error: {0}")]
    Backend(String),
}

impl EconomyError {
    /// Stable machine code carried over the host-API wire.
    pub fn wire_code(&self) -> &'static str {
        match self {
            Self::NotAMember => "not_a_member",
            Self::InsufficientFunds { .. } => "insufficient_funds",
            Self::OverCap { .. } => "over_cap",
            Self::Invalid(_) => "invalid_args",
            Self::Backend(_) => "backend",
        }
    }

    /// The wire message: for the two numeric variants it is the bare decimal
    /// number (the executor parses it back into the WIT `u64`), for every
    /// other variant the human-readable message.
    pub fn wire_message(&self) -> String {
        match self {
            Self::InsufficientFunds { balance } => balance.to_string(),
            Self::OverCap { cap } => cap.to_string(),
            other => other.to_string(),
        }
    }
}

/// The durable, community-scoped currency store.
pub trait EconomyStore: Send + Sync {
    /// `user`'s balance (0 for an active member with no row yet).
    /// [`EconomyError::NotAMember`] if `user` is not an active member now.
    fn balance<'a>(
        &'a self,
        scope: &'a EconomyScope,
        user: uuid::Uuid,
    ) -> BoxFuture<'a, Result<i64, EconomyError>>;

    /// The largest stake `user` may place right now: `min(cap, balance)`.
    fn max_bet<'a>(
        &'a self,
        scope: &'a EconomyScope,
        user: uuid::Uuid,
        cap: i64,
    ) -> BoxFuture<'a, Result<i64, EconomyError>>;

    /// Atomically debits `stake` and credits `payout` (net `payout - stake`)
    /// and returns the NEW balance. `max_bet` is the host-computed stake cap.
    /// Requires `1 <= stake <= max_bet`, `0 <= payout <=
    /// stake * `[`MAX_PAYOUT_MULTIPLE`], and `balance >= stake`.
    fn wager<'a>(
        &'a self,
        scope: &'a EconomyScope,
        user: uuid::Uuid,
        stake: i64,
        payout: i64,
        max_bet: i64,
    ) -> BoxFuture<'a, Result<i64, EconomyError>>;

    /// Atomically moves `amount` from `from` to `to` (both must be active
    /// members, `from != to`). `max_amount` is the host-computed cap.
    fn transfer<'a>(
        &'a self,
        scope: &'a EconomyScope,
        from: uuid::Uuid,
        to: uuid::Uuid,
        amount: i64,
        max_amount: i64,
    ) -> BoxFuture<'a, Result<(), EconomyError>>;

    /// The community's top `limit` (`1..=`[`MAX_LEADERBOARD_LIMIT`]) active
    /// members by balance, highest first (ties broken by user id).
    fn leaderboard<'a>(
        &'a self,
        scope: &'a EconomyScope,
        limit: u32,
    ) -> BoxFuture<'a, Result<Vec<LeaderboardEntry>, EconomyError>>;
}

/// Validates the arithmetic preconditions of a wager (pure, no I/O) so the
/// rules are unit-testable without a database and enforced before any query.
pub fn validate_wager(stake: i64, payout: i64, max_bet: i64) -> Result<(), EconomyError> {
    if stake < 1 {
        return Err(EconomyError::Invalid("stake must be >= 1".to_string()));
    }
    if payout < 0 {
        return Err(EconomyError::Invalid("payout must be >= 0".to_string()));
    }
    if stake > max_bet {
        return Err(EconomyError::OverCap { cap: max_bet });
    }
    let max_payout = stake.saturating_mul(MAX_PAYOUT_MULTIPLE);
    if payout > max_payout {
        return Err(EconomyError::OverCap { cap: max_payout });
    }
    Ok(())
}

/// Validates the preconditions of a transfer (pure, no I/O).
pub fn validate_transfer(
    from: uuid::Uuid,
    to: uuid::Uuid,
    amount: i64,
    max_amount: i64,
) -> Result<(), EconomyError> {
    if amount < 1 {
        return Err(EconomyError::Invalid("amount must be >= 1".to_string()));
    }
    if from == to {
        return Err(EconomyError::Invalid(
            "cannot transfer to oneself".to_string(),
        ));
    }
    if amount > max_amount {
        return Err(EconomyError::OverCap { cap: max_amount });
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wager_validation_accepts_the_boundaries() {
        assert!(validate_wager(1, 0, 10).is_ok());
        assert!(validate_wager(10, 1_000, 10).is_ok());
        assert!(validate_wager(10, 10, 10).is_ok());
    }

    #[test]
    fn wager_validation_rejects_bad_stake_and_payout() {
        assert!(matches!(
            validate_wager(0, 0, 10),
            Err(EconomyError::Invalid(_))
        ));
        assert!(matches!(
            validate_wager(-5, 0, 10),
            Err(EconomyError::Invalid(_))
        ));
        assert!(matches!(
            validate_wager(5, -1, 10),
            Err(EconomyError::Invalid(_))
        ));
        assert_eq!(
            validate_wager(11, 0, 10),
            Err(EconomyError::OverCap { cap: 10 })
        );
        assert_eq!(
            validate_wager(10, 1_001, 10),
            Err(EconomyError::OverCap { cap: 1_000 })
        );
    }

    #[test]
    fn payout_cap_never_overflows() {
        // stake * MAX_PAYOUT_MULTIPLE saturates instead of wrapping.
        assert!(validate_wager(i64::MAX, i64::MAX, i64::MAX).is_ok());
    }

    #[test]
    fn transfer_validation() {
        let (a, b) = (uuid::Uuid::new_v4(), uuid::Uuid::new_v4());
        assert!(validate_transfer(a, b, 5, 5).is_ok());
        assert!(matches!(
            validate_transfer(a, b, 0, 5),
            Err(EconomyError::Invalid(_))
        ));
        assert!(matches!(
            validate_transfer(a, b, -3, 5),
            Err(EconomyError::Invalid(_))
        ));
        assert!(matches!(
            validate_transfer(a, a, 1, 5),
            Err(EconomyError::Invalid(_))
        ));
        assert_eq!(
            validate_transfer(a, b, 6, 5),
            Err(EconomyError::OverCap { cap: 5 })
        );
    }

    #[test]
    fn wire_codes_and_messages_are_stable() {
        assert_eq!(EconomyError::NotAMember.wire_code(), "not_a_member");
        assert_eq!(
            EconomyError::InsufficientFunds { balance: 7 }.wire_code(),
            "insufficient_funds"
        );
        assert_eq!(EconomyError::OverCap { cap: 9 }.wire_code(), "over_cap");
        assert_eq!(
            EconomyError::Invalid(String::new()).wire_code(),
            "invalid_args"
        );
        assert_eq!(EconomyError::Backend(String::new()).wire_code(), "backend");
        // The numeric variants carry the bare number for the executor to parse.
        assert_eq!(
            EconomyError::InsufficientFunds { balance: 7 }.wire_message(),
            "7"
        );
        assert_eq!(EconomyError::OverCap { cap: 9 }.wire_message(), "9");
        assert_eq!(
            EconomyError::NotAMember.wire_message(),
            "target user is not an active member of this community"
        );
    }
}
