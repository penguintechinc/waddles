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
//! Both rolling-24h daily aggregates ([`EconomyCaps`]: per user, per
//! (tenant, community, app)) are ALSO enforced durably here, derived from the
//! append-only ledger inside the write transaction under a transaction-scoped
//! advisory lock keyed on `(tenant, community, app, wager|transfer)`: the
//! gate's in-memory copy resets on restart and multiplies across replicas, the
//! ledger does not. The lock is always the FIRST lock a transaction takes (one
//! per transaction), so the lock order is uniform and cannot cycle with the
//! row locks. Only APPLIED movements count (a refused call writes no ledger
//! row and so consumes no budget).
//!
//! # The mint cap is on the PAYOUT
//!
//! A wager is the one call that creates currency: the bundle chooses the
//! payout. The same two ceilings therefore bound it twice, in independent
//! rolling-24h windows: the STAKE (throughput -- how much a user can put at
//! risk, which also bounds ledger growth) and the PAYOUT (what the wager
//! CREDITS -- the mint). A stake-1 wager paying 100 spends 100 of the mint
//! budget, and a large losing stake spends none of it. (The cap used to sum
//! the stake only, so a bundle picking its own payouts could mint without
//! bound.) A transfer moves existing money and is metered on the amount sent,
//! against the SENDER.
//!
//! # Idempotency (no double-credit, no double-spend)
//!
//! Every money-moving call carries a host-derived [`IdempotencyKey`], stored
//! on the ledger row that originates the movement (the `wager` row, or the
//! `transfer_out` row) under a partial UNIQUE index scoped to
//! `(tenant, community, app)`. Inside the write transaction, after the
//! advisory lock and BEFORE any cap or balance check:
//!
//! * no row has the key: the call applies and writes its keyed ledger row in
//!   the same statement as the balance move;
//! * a row has the key and the SAME parameters: the call is a replay -- the
//!   original result is returned, nothing moves, no budget is consumed;
//! * a row has the key and DIFFERENT parameters (or a different kind of
//!   movement): [`EconomyError::IdempotencyConflict`] -- never applied. A
//!   replayed bundle that re-rolls its payout cannot credit twice.
//!
//! A refused or failed call writes no ledger row, so it records no key and a
//! later retry under the same key applies normally. The unique index is the
//! database's backstop for a race the advisory lock does not serialize.
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

/// The two durable rolling-24h aggregate ceilings one money-moving call is
/// checked against (the catalog's `Quota::EconomyAmount`
/// `per_user_daily_abs_max` / `per_scope_daily_abs_max` for the permission
/// being exercised, passed in by the stage). A struct rather than two bare
/// `i64`s so they can never be transposed at a call site.
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub struct EconomyCaps {
    /// Max rolling-24h sum for one acting user within one app and community:
    /// of stakes AND (separately) of payouts credited for a wager, of amounts
    /// sent for a transfer.
    pub per_user_daily_max: i64,
    /// Max rolling-24h sum by one app across the whole (tenant, community),
    /// measured the same way as [`Self::per_user_daily_max`].
    pub per_scope_daily_max: i64,
}

/// Largest accepted leaderboard page.
pub const MAX_LEADERBOARD_LIMIT: u32 = 100;

/// Longest accepted idempotency key (the `economy_ledger.idempotency_key`
/// column is `VARCHAR(128)`).
pub const MAX_IDEMPOTENCY_KEY_LEN: usize = 128;

/// The replay-stable identity of ONE money-moving call, scoped by the store to
/// `(tenant, community, app)`. Built by the HOST (never taken from a guest) so
/// a retried or replayed call presents the same key and is applied at most
/// once; see the module docs. Opaque and validated: ASCII alphanumerics and
/// `:_.-` only, `1..=`[`MAX_IDEMPOTENCY_KEY_LEN`] bytes -- no whitespace, no
/// control characters, nothing that could be mistaken for SQL or a log line.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct IdempotencyKey(String);

impl IdempotencyKey {
    /// Validates `raw` as a key. The error never echoes the offending value.
    pub fn new(raw: impl Into<String>) -> Result<Self, EconomyError> {
        let raw = raw.into();
        if raw.is_empty() || raw.len() > MAX_IDEMPOTENCY_KEY_LEN {
            return Err(EconomyError::Invalid(format!(
                "idempotency key must be 1..={MAX_IDEMPOTENCY_KEY_LEN} bytes"
            )));
        }
        if !raw
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b':' | b'_' | b'.' | b'-'))
        {
            return Err(EconomyError::Invalid(
                "idempotency key may contain only [A-Za-z0-9:_.-]".to_string(),
            ));
        }
        Ok(Self(raw))
    }

    /// The key of the `ordinal`-th `kind` mutation (`wager` | `transfer`) made
    /// while handling the event `event_id`: `"<event_id>:<kind>:<ordinal>"`.
    /// An event redelivered after a crash replays the same ordinals, so each
    /// call maps to the same key it had the first time.
    pub fn for_event(event_id: &str, kind: &str, ordinal: u32) -> Result<Self, EconomyError> {
        Self::new(format!("{event_id}:{kind}:{ordinal}"))
    }

    /// The key text.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl std::fmt::Display for IdempotencyKey {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

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
    /// The acting user's rolling-24h total for this app/community would pass
    /// `cap`. Surfaces under the same stable `quota_exceeded` code the gate's
    /// in-memory per-user check uses.
    #[error("rolling-24h per-user economy cap ({cap}) would be exceeded")]
    UserQuotaExceeded { cap: i64 },
    /// This app's rolling-24h total across the whole (tenant, community) would
    /// pass `cap`. Same `quota_exceeded` code as the gate's per-scope check.
    #[error("rolling-24h per-scope economy cap ({cap}) would be exceeded")]
    ScopeQuotaExceeded { cap: i64 },
    /// The call's idempotency key was already used by a DIFFERENT movement
    /// (other parameters or another kind of call). Never applied: a replay
    /// must present the same parameters it had the first time.
    #[error("idempotency key was already used by a different economy operation")]
    IdempotencyConflict,
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
            Self::UserQuotaExceeded { .. } | Self::ScopeQuotaExceeded { .. } => "quota_exceeded",
            Self::IdempotencyConflict => "idempotency_conflict",
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
    /// and returns the NEW balance. `max_bet` is the host-computed stake cap;
    /// `caps` the durable daily aggregates. Requires `1 <= stake <= max_bet`,
    /// `0 <= payout <= stake * `[`MAX_PAYOUT_MULTIPLE`], `balance >= stake`,
    /// the stake to fit both rolling-24h STAKE caps and the payout to fit both
    /// rolling-24h PAYOUT (mint) caps. `key` makes the call idempotent: a
    /// replay with identical parameters returns the original balance and moves
    /// nothing; the same key with different parameters is
    /// [`EconomyError::IdempotencyConflict`].
    #[allow(clippy::too_many_arguments)]
    fn wager<'a>(
        &'a self,
        scope: &'a EconomyScope,
        user: uuid::Uuid,
        stake: i64,
        payout: i64,
        max_bet: i64,
        caps: EconomyCaps,
        key: &'a IdempotencyKey,
    ) -> BoxFuture<'a, Result<i64, EconomyError>>;

    /// Atomically moves `amount` from `from` to `to` (both must be active
    /// members, `from != to`). `max_amount` is the host-computed per-call cap;
    /// `caps` the durable daily aggregates, charged against the SENDER. `key`
    /// makes the call idempotent exactly as for [`Self::wager`].
    #[allow(clippy::too_many_arguments)]
    fn transfer<'a>(
        &'a self,
        scope: &'a EconomyScope,
        from: uuid::Uuid,
        to: uuid::Uuid,
        amount: i64,
        max_amount: i64,
        caps: EconomyCaps,
        key: &'a IdempotencyKey,
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
        // Both durable aggregate breaches share the gate's own stable code.
        assert_eq!(
            EconomyError::UserQuotaExceeded { cap: 1 }.wire_code(),
            "quota_exceeded"
        );
        assert_eq!(
            EconomyError::ScopeQuotaExceeded { cap: 1 }.wire_code(),
            "quota_exceeded"
        );
        assert_eq!(
            EconomyError::Invalid(String::new()).wire_code(),
            "invalid_args"
        );
        assert_eq!(EconomyError::Backend(String::new()).wire_code(), "backend");
        assert_eq!(
            EconomyError::IdempotencyConflict.wire_code(),
            "idempotency_conflict"
        );
        assert_eq!(
            EconomyError::IdempotencyConflict.wire_message(),
            "idempotency key was already used by a different economy operation"
        );
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

    #[test]
    fn idempotency_keys_accept_the_host_shape_and_reject_everything_else() {
        let ok = IdempotencyKey::for_event("3fa85f64-5717-4562-b3fc-2c963f66afa6", "wager", 0)
            .expect("the host shape is valid");
        assert_eq!(ok.as_str(), "3fa85f64-5717-4562-b3fc-2c963f66afa6:wager:0");
        assert_eq!(ok.to_string(), ok.as_str());
        assert!(IdempotencyKey::new("a").is_ok());
        assert!(IdempotencyKey::new("A-b_c.d:9").is_ok());
        assert!(IdempotencyKey::new("x".repeat(MAX_IDEMPOTENCY_KEY_LEN)).is_ok());
        for bad in [
            String::new(),
            "x".repeat(MAX_IDEMPOTENCY_KEY_LEN + 1),
            "has space".to_string(),
            "tab\there".to_string(),
            "line\nbreak".to_string(),
            "nul\0byte".to_string(),
            "quote'; DROP TABLE economy_ledger;--".to_string(),
            "unicode-\u{e9}".to_string(),
            "slash/path".to_string(),
        ] {
            assert!(
                matches!(
                    IdempotencyKey::new(bad.clone()),
                    Err(EconomyError::Invalid(_))
                ),
                "{bad:?}"
            );
        }
    }

    #[test]
    fn a_rejected_key_error_never_echoes_the_offending_value() {
        let Err(EconomyError::Invalid(msg)) = IdempotencyKey::new("secret value!") else {
            panic!("expected Invalid");
        };
        assert!(!msg.contains("secret"), "{msg}");
    }

    #[test]
    fn event_keys_differ_by_event_kind_and_ordinal() {
        let k = |e: &str, kind: &str, n: u32| IdempotencyKey::for_event(e, kind, n).unwrap();
        let base = k("e1", "wager", 0);
        assert_eq!(base, k("e1", "wager", 0), "the same call keys the same way");
        assert_ne!(base, k("e2", "wager", 0));
        assert_ne!(base, k("e1", "transfer", 0));
        assert_ne!(base, k("e1", "wager", 1));
        // An over-long event id is refused rather than truncated (truncation
        // could make two events collide).
        assert!(IdempotencyKey::for_event(&"e".repeat(200), "wager", 0).is_err());
    }
}
