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
//! (tenant, community) -- ONE budget shared by every app bundle of the
//! community, never per app) are ALSO enforced durably here, derived from the
//! append-only ledger inside the write transaction under a transaction-scoped
//! advisory lock keyed on `(tenant, community, window)`: the
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
    /// Max rolling-24h sum for one acting user within one community (across
    /// ALL of its apps -- the budget is per community, never per app):
    /// of stakes AND (separately) of payouts credited for a wager, of amounts
    /// sent for a transfer.
    pub per_user_daily_max: i64,
    /// Max rolling-24h sum across the whole (tenant, community) -- every app
    /// bundle of the community shares it -- measured the same way as
    /// [`Self::per_user_daily_max`].
    pub per_scope_daily_max: i64,
}

/// Default per-community money-supply ceiling (spec SS5.1): the total of every
/// balance in one `(tenant, community)` a mint may bring the economy to. The
/// STAGE passes the effective value (`max_supply`) -- never a guest.
pub const DEFAULT_MAX_SUPPLY: i64 = 1_000_000;

/// Most entries one `batch_credit` / `pay_pot` call may carry (bounds the
/// statement size, the lock-hold time and the per-call ledger growth).
pub const MAX_BATCH_ENTRIES: usize = 100;

/// Largest numerator or denominator an `exchange` rate may carry.
pub const MAX_EXCHANGE_RATE_PART: i64 = 1_000_000;

/// An `exchange` may credit at most this many units of currency per external
/// point (`rate_num / rate_den <= MAX_EXCHANGE_MULTIPLE`): the host-side bound
/// on a bundle-chosen rate, so a buggy or hostile rate cannot mint absurdly.
pub const MAX_EXCHANGE_MULTIPLE: i64 = 1_000;

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
    /// The acting user's rolling-24h total for this community would pass
    /// `cap`. Surfaces under the same stable `quota_exceeded` code the gate's
    /// in-memory per-user check uses.
    #[error("rolling-24h per-user economy cap ({cap}) would be exceeded")]
    UserQuotaExceeded { cap: i64 },
    /// The community's rolling-24h total (all apps) would
    /// pass `cap`. Same `quota_exceeded` code as the gate's per-scope check.
    #[error("rolling-24h per-scope economy cap ({cap}) would be exceeded")]
    ScopeQuotaExceeded { cap: i64 },
    /// The call's idempotency key was already used by a DIFFERENT movement
    /// (other parameters or another kind of call). Never applied: a replay
    /// must present the same parameters it had the first time.
    #[error("idempotency key was already used by a different economy operation")]
    IdempotencyConflict,
    /// A mint would take the community's total currency past the money-supply
    /// ceiling `cap` (`SUM(balance) + amount > max_supply`). Surfaces on the
    /// wire as `over_cap` carrying the ceiling.
    #[error("community money supply cap ({cap}) would be exceeded")]
    SupplyCapReached { cap: i64 },
    /// A privileged op (`admin_adjust`) was called without the stage's
    /// admin-authorization guarantee. Never applied.
    #[error("operation requires admin authorization")]
    NotAuthorized,
    /// An elevated call (see [`ElevatedRequest`]): the CALLING app holds no live
    /// `economy.transfer_from` / `economy.app_mint` grant in this community
    /// (never approved, or revoked). Never applied.
    #[error("the calling app holds no approved elevated economy grant")]
    ElevatedNotGranted,
    /// An elevated call exceeded the grant's per-call maximum `cap`. Wire
    /// `over_cap`, carrying the cap.
    #[error("amount exceeds the app's approved per-call cap ({cap})")]
    ElevatedOverCap { cap: i64 },
    /// An elevated call would pass the grant's rolling-24h per-app or
    /// per-target-member total `cap`. Wire `quota_exceeded`.
    #[error("the app's approved rolling-24h elevated cap ({cap}) would be exceeded")]
    ElevatedQuotaExceeded { cap: i64 },
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
            Self::OverCap { .. } | Self::SupplyCapReached { .. } | Self::ElevatedOverCap { .. } => {
                "over_cap"
            }
            Self::NotAuthorized => "admin_required",
            Self::ElevatedNotGranted => "elevated_not_granted",
            Self::UserQuotaExceeded { .. }
            | Self::ScopeQuotaExceeded { .. }
            | Self::ElevatedQuotaExceeded { .. } => "quota_exceeded",
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
            Self::OverCap { cap }
            | Self::SupplyCapReached { cap }
            | Self::ElevatedOverCap { cap } => cap.to_string(),
            other => other.to_string(),
        }
    }
}

/// How an `admin_adjust` changes a balance.
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum AdminAdjust {
    /// Add (`> 0`) or subtract (`< 0`) a non-zero amount.
    Delta(i64),
    /// Set the balance to this absolute value (`>= 0`).
    Set(i64),
}

/// The reserved per-community POT account (spec SS5.1): the UUID `pay_pot`
/// debits. Deterministic from `(tenant, community)`, and in a reserved UUID
/// space (version nibble 0xB, never produced by the v4 user identities) so it
/// can never collide with a real member. It is the ONLY account exempt from
/// the active-membership predicate, and only `pay_pot` may debit it.
pub fn pot_account(scope: &EconomyScope) -> uuid::Uuid {
    let mut b = [0u8; 16];
    b[0..4].copy_from_slice(&scope.tenant_id.to_be_bytes());
    b[4..6].copy_from_slice(b"PO");
    b[6] = 0xb0; // reserved version nibble (never v4), fixed
    b[7] = 0x00;
    b[8] = 0x80; // RFC 4122 variant, fixed
    b[9..12].copy_from_slice(b"POT");
    b[12..16].copy_from_slice(&scope.community_id.to_be_bytes());
    uuid::Uuid::from_bytes(b)
}

/// Hard ceilings the admin-tunable elevated caps are clamped to (mirrored by
/// hub-api's `_ECONOMY_ELEVATED_CAP_PARAMS` approval-time check).
pub const ELEVATED_MAX_PER_OP_CEILING: i64 = 10_000;
/// See [`ELEVATED_MAX_PER_OP_CEILING`]: per-app rolling-24h total.
pub const ELEVATED_MAX_PER_DAY_CEILING: i64 = 100_000;
/// See [`ELEVATED_MAX_PER_OP_CEILING`]: per-target-member rolling-24h total.
pub const ELEVATED_MAX_PER_TARGET_DAY_CEILING: i64 = 50_000;
/// Conservative defaults when the admin's grant params leave a cap unset.
pub const ELEVATED_DEFAULT_MAX_PER_OP: i64 = 100;
/// See [`ELEVATED_DEFAULT_MAX_PER_OP`].
pub const ELEVATED_DEFAULT_MAX_PER_DAY: i64 = 1_000;
/// See [`ELEVATED_DEFAULT_MAX_PER_OP`].
pub const ELEVATED_DEFAULT_MAX_PER_TARGET_DAY: i64 = 500;

/// Longest accepted audit `reason` code.
pub const MAX_REASON_LEN: usize = 64;

/// The two ELEVATED permissions an APP bundle needs, on top of the economy
/// service bundle's own grants, to use the service's dangerous primitives.
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum ElevatedPermission {
    /// Move or burn a NON-actor member's funds.
    TransferFrom,
    /// Create or set currency through the service.
    AppMint,
}

impl ElevatedPermission {
    /// The catalog permission id (`community_permission_grants.permission_id`).
    pub fn id(self) -> &'static str {
        match self {
            Self::TransferFrom => "economy.transfer_from",
            Self::AppMint => "economy.app_mint",
        }
    }
}

/// The admin-set host caps of one elevated grant (from the grant's
/// `params_json`: `max_per_op`, `max_per_day`, `max_per_target_per_day`),
/// already defaulted and clamped to the hard ceilings.
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub struct ElevatedCaps {
    /// Max amount of ONE call.
    pub max_per_op: i64,
    /// Max rolling-24h total for the calling app in this community.
    pub max_per_day: i64,
    /// Max rolling-24h total against ONE target member (the victim of a
    /// `transfer_from`, the recipient of a mint) for the calling app.
    pub max_per_target_per_day: i64,
}

impl ElevatedCaps {
    /// Builds caps from optional raw grant values: absent/invalid -> default,
    /// anything above the hard ceiling -> the ceiling.
    pub fn from_raw(per_op: Option<i64>, per_day: Option<i64>, per_target: Option<i64>) -> Self {
        let pick = |raw: Option<i64>, default: i64, ceiling: i64| {
            raw.filter(|v| *v >= 1).map_or(default, |v| v.min(ceiling))
        };
        Self {
            max_per_op: pick(
                per_op,
                ELEVATED_DEFAULT_MAX_PER_OP,
                ELEVATED_MAX_PER_OP_CEILING,
            ),
            max_per_day: pick(
                per_day,
                ELEVATED_DEFAULT_MAX_PER_DAY,
                ELEVATED_MAX_PER_DAY_CEILING,
            ),
            max_per_target_per_day: pick(
                per_target,
                ELEVATED_DEFAULT_MAX_PER_TARGET_DAY,
                ELEVATED_MAX_PER_TARGET_DAY_CEILING,
            ),
        }
    }
}

/// One elevated call by an APP bundle (`caller_app`) through the economy
/// service bundle (the [`EconomyScope::app_id`]). Everything here is
/// host-derived except the amounts/users/reason the app chose.
#[derive(Clone, Debug)]
pub struct ElevatedRequest<'a> {
    /// The calling app (host-derived from the tool-call chain, never guest input).
    pub caller_app: &'a str,
    /// Which approval the call needs.
    pub permission: ElevatedPermission,
    /// Short op label for the audit row (`transfer_from`, `credit`, ...).
    pub op: &'static str,
    /// The member whose funds move/burn (`transfer_from`: the victim) or who
    /// receives/is set (mint ops).
    pub target: uuid::Uuid,
    /// The other side of a `transfer_from` (the recipient).
    pub counterparty: Option<uuid::Uuid>,
    /// Amount the call moves/mints (metered against the caps); `>= 0`.
    pub amount: i64,
    /// PII-free reason code (`[a-z0-9_.-]{1,64}`) the app supplies for the audit.
    pub reason: &'a str,
    /// The caller-namespaced, replay-stable key of this call.
    pub key: &'a IdempotencyKey,
}

/// Result of an applied (or replayed) `transfer_from`.
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub struct TransferFromOutcome {
    /// The victim's balance after the move.
    pub from_balance: i64,
    /// The recipient's balance after the move.
    pub to_balance: i64,
    /// The `economy_elevated_audit` row of the call.
    pub audit_id: i64,
    /// The approving grant (`community_permission_grants.grant_id`).
    pub grant_id: i64,
}

/// Validates an audit reason code (pure): `[a-z0-9_.-]{1,64}`. The reason is
/// app-supplied free-ish text that lands in an audit table, so it is limited to
/// a code alphabet that cannot carry PII-shaped prose.
pub fn validate_reason(reason: &str) -> Result<(), EconomyError> {
    if reason.is_empty()
        || reason.len() > MAX_REASON_LEN
        || !reason.bytes().all(|b| {
            b.is_ascii_lowercase() || b.is_ascii_digit() || matches!(b, b'_' | b'.' | b'-')
        })
    {
        return Err(EconomyError::Invalid(format!(
            "reason must be a code of 1..={MAX_REASON_LEN} chars from [a-z0-9_.-]"
        )));
    }
    Ok(())
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
    /// rolling-24h PAYOUT (mint) caps, and `payout - stake` (what the wager
    /// CREATES) to fit the community money-supply ceiling `max_supply`
    /// ([`EconomyError::SupplyCapReached`]). `key` makes the call idempotent: a
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
        max_supply: i64,
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

    /// MINT: atomically adds `amount` to `user` (an active member) and returns
    /// the NEW balance. Bounded by the host-computed per-call `max_amount`, the
    /// rolling-24h mint `caps` (ONE window shared with `batch_credit` and
    /// `exchange`: ledger kinds `credit` + `exchange`) and the community
    /// money-supply ceiling `max_supply` (`SUM(balance) + amount <=
    /// max_supply`, checked inside the supply-locked transaction). Idempotent
    /// by `key` exactly as for [`Self::wager`].
    #[allow(clippy::too_many_arguments)]
    fn credit<'a>(
        &'a self,
        scope: &'a EconomyScope,
        user: uuid::Uuid,
        amount: i64,
        max_amount: i64,
        max_supply: i64,
        caps: EconomyCaps,
        key: &'a IdempotencyKey,
    ) -> BoxFuture<'a, Result<i64, EconomyError>>;

    /// MINT for many users in ONE transaction (all-or-nothing): every entry
    /// must be a distinct active member with `1 <= amount`, and the batch TOTAL
    /// must be `<= max_amount` (the per-call ceiling covers the whole call); the
    /// per-user and the per-scope rolling-24h mint windows and the supply
    /// ceiling are checked against the batch TOTAL. At most
    /// [`MAX_BATCH_ENTRIES`] entries. Idempotent by `key` for the whole batch.
    #[allow(clippy::too_many_arguments)]
    fn batch_credit<'a>(
        &'a self,
        scope: &'a EconomyScope,
        entries: &'a [(uuid::Uuid, i64)],
        max_amount: i64,
        max_supply: i64,
        caps: EconomyCaps,
        key: &'a IdempotencyKey,
    ) -> BoxFuture<'a, Result<(), EconomyError>>;

    /// SPEND/BURN: atomically subtracts `amount` from `user` (guarded
    /// `balance >= amount`; [`EconomyError::InsufficientFunds`] otherwise, no
    /// row changes) and returns the NEW balance. Bounded by `max_amount` and
    /// the rolling-24h spend `caps` (ledger kinds `debit` + `cond_debit`, one window).
    fn debit<'a>(
        &'a self,
        scope: &'a EconomyScope,
        user: uuid::Uuid,
        amount: i64,
        max_amount: i64,
        caps: EconomyCaps,
        key: &'a IdempotencyKey,
    ) -> BoxFuture<'a, Result<i64, EconomyError>>;

    /// Like [`Self::debit`] but refuses (never partially applies) unless the
    /// balance would stay `>= min_balance` (`balance - amount >= min_balance`,
    /// evaluated against the committed row under its lock).
    #[allow(clippy::too_many_arguments)]
    fn conditional_debit<'a>(
        &'a self,
        scope: &'a EconomyScope,
        user: uuid::Uuid,
        amount: i64,
        min_balance: i64,
        max_amount: i64,
        caps: EconomyCaps,
        key: &'a IdempotencyKey,
    ) -> BoxFuture<'a, Result<i64, EconomyError>>;

    /// PRIVILEGED absolute-set or delta of `user`'s balance (ledger kind
    /// `admin_adjust`; bypasses the 24h earn windows, still bounded by the
    /// community money-supply ceiling `max_supply`). `is_admin_authorized` is
    /// the STAGE's guarantee (gate decision), never a guest value: `false` is
    /// [`EconomyError::NotAuthorized`] before anything is touched.
    #[allow(clippy::too_many_arguments)]
    fn admin_adjust<'a>(
        &'a self,
        scope: &'a EconomyScope,
        user: uuid::Uuid,
        adjust: AdminAdjust,
        max_supply: i64,
        is_admin_authorized: bool,
        key: &'a IdempotencyKey,
    ) -> BoxFuture<'a, Result<i64, EconomyError>>;

    /// Atomic multi-winner payout from the community POT: `pot_source` MUST be
    /// [`pot_account`]`(scope)`; each winner is a distinct active member with an
    /// explicit amount `>= 1`; requires `pot balance >= SUM(amounts)`. The host
    /// does no rounding: any remainder stays in the pot. One transaction, fully
    /// commits or fully rolls back; idempotent by `key`. Moves existing
    /// currency (no mint, no supply effect).
    fn pay_pot<'a>(
        &'a self,
        scope: &'a EconomyScope,
        pot_source: uuid::Uuid,
        winners: &'a [(uuid::Uuid, i64)],
        key: &'a IdempotencyKey,
    ) -> BoxFuture<'a, Result<(), EconomyError>>;

    /// One-call cross-currency exchange: ledgers an idempotent CLAIM of
    /// `from_amount` external points (no external balance is kept or checked)
    /// and credits `floor(from_amount * rate_num / rate_den)` to `user`.
    /// The rate is bounded host-side ([`MAX_EXCHANGE_RATE_PART`],
    /// [`MAX_EXCHANGE_MULTIPLE`]); the credited amount is bounded by
    /// `max_amount`, the rolling-24h mint `caps` (the SAME window as `credit`/`batch_credit`) and
    /// `max_supply`. Returns the NEW balance.
    #[allow(clippy::too_many_arguments)]
    fn exchange<'a>(
        &'a self,
        scope: &'a EconomyScope,
        user: uuid::Uuid,
        from_amount: i64,
        rate_num: i64,
        rate_den: i64,
        max_amount: i64,
        max_supply: i64,
        caps: EconomyCaps,
        key: &'a IdempotencyKey,
    ) -> BoxFuture<'a, Result<i64, EconomyError>>;

    /// ELEVATED PRIMITIVE: atomically moves `req.amount` from `req.target` (the
    /// victim, who need NOT be the invocation's actor) to `req.counterparty`.
    /// ONE transaction under the per-(community, calling app) advisory lock:
    /// the calling app's live `economy.transfer_from` grant is read from
    /// `community_permission_grants` (absent/revoked ->
    /// [`EconomyError::ElevatedNotGranted`]); the admin-set caps are enforced
    /// (per call, per app per 24h, per victim per 24h); idempotent by
    /// `req.key` (same parameters replay the prior balances, different ones are
    /// [`EconomyError::IdempotencyConflict`]); both sides must be active
    /// members; `balance >= amount` (the `CHECK (balance >= 0)` stays the
    /// backstop); ledger kinds `transfer_from` / `transfer_from_in`; and the
    /// `economy_elevated_audit` row (caller app, approving grant + admin,
    /// from/to, amount, reason, balances) is written in the same transaction.
    fn transfer_from<'a>(
        &'a self,
        scope: &'a EconomyScope,
        req: &'a ElevatedRequest<'a>,
    ) -> BoxFuture<'a, Result<TransferFromOutcome, EconomyError>>;

    /// Admits one elevated call that another primitive (`credit`, `debit` of a
    /// non-actor, `admin_adjust`, `pay_pot`, ...) will then perform: reads the
    /// calling app's live grant, enforces the same caps and writes a RESERVED
    /// audit row (counted against the caps immediately) in one transaction
    /// under the advisory lock. Idempotent by `req.key`. Returns the audit id
    /// to pass to [`Self::elevated_settle`].
    fn elevated_reserve<'a>(
        &'a self,
        scope: &'a EconomyScope,
        req: &'a ElevatedRequest<'a>,
    ) -> BoxFuture<'a, Result<i64, EconomyError>>;

    /// Settles a reservation after the primitive ran: `applied = true` marks
    /// it applied, `false` marks it failed (no longer counted against the
    /// caps). Only a still-RESERVED row transitions; a replay never rewrites
    /// an applied row. Not called on an indeterminate backend failure, so the
    /// reservation stays counted (conservative).
    fn elevated_settle<'a>(
        &'a self,
        scope: &'a EconomyScope,
        audit_id: i64,
        applied: bool,
    ) -> BoxFuture<'a, Result<(), EconomyError>>;
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

/// Validates a single-user mint/spend amount (pure, no I/O).
pub fn validate_amount(amount: i64, max_amount: i64) -> Result<(), EconomyError> {
    if amount < 1 {
        return Err(EconomyError::Invalid("amount must be >= 1".to_string()));
    }
    if amount > max_amount {
        return Err(EconomyError::OverCap { cap: max_amount });
    }
    Ok(())
}

/// Validates a multi-user entry list (`batch_credit` / `pay_pot`): non-empty,
/// at most [`MAX_BATCH_ENTRIES`], distinct users, each amount `>= 1` and (when
/// `max_amount` is `Some`) within it. Returns the entries sorted by user (the
/// canonical order every row-lock and replay comparison uses) and their
/// overflow-checked total.
pub fn validate_entries(
    entries: &[(uuid::Uuid, i64)],
    max_amount: Option<i64>,
) -> Result<(Vec<(uuid::Uuid, i64)>, i64), EconomyError> {
    if entries.is_empty() || entries.len() > MAX_BATCH_ENTRIES {
        return Err(EconomyError::Invalid(format!(
            "entries must be 1..={MAX_BATCH_ENTRIES}"
        )));
    }
    let mut sorted = entries.to_vec();
    sorted.sort_by_key(|(u, _)| *u);
    if sorted.windows(2).any(|w| w[0].0 == w[1].0) {
        return Err(EconomyError::Invalid(
            "duplicate user in entries".to_string(),
        ));
    }
    let mut total: i64 = 0;
    for (_, amount) in &sorted {
        validate_amount(*amount, max_amount.unwrap_or(i64::MAX))?;
        total = total
            .checked_add(*amount)
            .ok_or_else(|| EconomyError::Invalid("entries total overflows".to_string()))?;
    }
    Ok((sorted, total))
}

/// Validates an `exchange` and returns the currency it credits
/// (`floor(from_amount * rate_num / rate_den)`, computed in `i128`, so it
/// cannot overflow). Rejects a rate outside the host bound and an exchange
/// that would credit nothing (claiming external points for no currency).
pub fn validate_exchange(
    from_amount: i64,
    rate_num: i64,
    rate_den: i64,
    max_amount: i64,
) -> Result<i64, EconomyError> {
    if from_amount < 1 {
        return Err(EconomyError::Invalid(
            "from_amount must be >= 1".to_string(),
        ));
    }
    if !(1..=MAX_EXCHANGE_RATE_PART).contains(&rate_num)
        || !(1..=MAX_EXCHANGE_RATE_PART).contains(&rate_den)
    {
        return Err(EconomyError::Invalid(format!(
            "rate parts must be 1..={MAX_EXCHANGE_RATE_PART}"
        )));
    }
    if i128::from(rate_num) > i128::from(rate_den) * i128::from(MAX_EXCHANGE_MULTIPLE) {
        return Err(EconomyError::Invalid(format!(
            "rate exceeds the host bound ({MAX_EXCHANGE_MULTIPLE}x)"
        )));
    }
    let credited = i128::from(from_amount) * i128::from(rate_num) / i128::from(rate_den);
    let credited = i64::try_from(credited)
        .map_err(|_| EconomyError::Invalid("credited amount overflows".to_string()))?;
    if credited < 1 {
        return Err(EconomyError::Invalid(
            "exchange would credit nothing".to_string(),
        ));
    }
    if credited > max_amount {
        return Err(EconomyError::OverCap { cap: max_amount });
    }
    Ok(credited)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn spec_default_rate_500_points_credits_1000_waddlebucks() {
        assert_eq!(validate_exchange(500, 1000, 500, 10_000).unwrap(), 1000);
    }

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

    #[test]
    fn amount_entries_and_exchange_validation() {
        assert!(validate_amount(1, 5).is_ok());
        assert!(matches!(
            validate_amount(0, 5),
            Err(EconomyError::Invalid(_))
        ));
        assert_eq!(validate_amount(6, 5), Err(EconomyError::OverCap { cap: 5 }));

        let (a, b) = (uuid::Uuid::from_u128(1), uuid::Uuid::from_u128(2));
        let (sorted, total) = validate_entries(&[(b, 3), (a, 4)], Some(5)).unwrap();
        assert_eq!((sorted, total), (vec![(a, 4), (b, 3)], 7));
        assert!(validate_entries(&[], None).is_err());
        assert!(validate_entries(&[(a, 1), (a, 2)], None).is_err());
        assert!(validate_entries(&[(a, 0)], None).is_err());
        assert!(validate_entries(&[(a, i64::MAX), (b, 1)], None).is_err());
        assert_eq!(
            validate_entries(&[(a, 6)], Some(5)),
            Err(EconomyError::OverCap { cap: 5 })
        );
        let many: Vec<_> = (0..=MAX_BATCH_ENTRIES as u128)
            .map(|i| (uuid::Uuid::from_u128(i), 1))
            .collect();
        assert!(validate_entries(&many, None).is_err());

        // 1000 points : 500 -> floor(7 * 1000 / 500) = 14
        assert_eq!(validate_exchange(7, 1000, 500, 100), Ok(14));
        assert!(validate_exchange(1, 1, 2, 100).is_err(), "rounds to 0");
        assert_eq!(validate_exchange(3, 1, 2, 100), Ok(1), "floors");
        assert!(validate_exchange(1, 1001, 1, 10_000).is_err(), "rate bound");
        assert!(validate_exchange(1, 1000, 1, 10_000).is_ok());
        assert!(validate_exchange(1, 0, 1, 100).is_err());
        assert!(validate_exchange(1, 1, 0, 100).is_err());
        assert!(
            validate_exchange(1, MAX_EXCHANGE_RATE_PART + 1, MAX_EXCHANGE_RATE_PART, 100).is_err()
        );
        assert_eq!(
            validate_exchange(100, 10, 1, 500),
            Err(EconomyError::OverCap { cap: 500 })
        );
        assert!(
            validate_exchange(i64::MAX, 1000, 1, i64::MAX).is_err(),
            "overflow"
        );
    }

    #[test]
    fn pot_account_is_deterministic_per_community_and_never_a_v4_user() {
        let s = |t, c| EconomyScope {
            tenant_id: t,
            community_id: c,
            app_id: "x".into(),
        };
        assert_eq!(pot_account(&s(1, 10)), pot_account(&s(1, 10)));
        assert_ne!(pot_account(&s(1, 10)), pot_account(&s(1, 11)));
        assert_ne!(pot_account(&s(1, 10)), pot_account(&s(2, 10)));
        assert_ne!(pot_account(&s(1, 10)).get_version_num(), 4);
    }

    #[test]
    fn new_error_wire_codes() {
        assert_eq!(
            EconomyError::SupplyCapReached { cap: 9 }.wire_code(),
            "over_cap"
        );
        assert_eq!(
            EconomyError::SupplyCapReached { cap: 9 }.wire_message(),
            "9"
        );
        assert_eq!(EconomyError::NotAuthorized.wire_code(), "admin_required");
    }

    #[test]
    fn elevated_error_wire_codes_and_numeric_messages() {
        assert_eq!(
            EconomyError::ElevatedNotGranted.wire_code(),
            "elevated_not_granted"
        );
        let over = EconomyError::ElevatedOverCap { cap: 50 };
        assert_eq!(
            (over.wire_code(), over.wire_message()),
            ("over_cap", "50".to_string())
        );
        assert_eq!(
            EconomyError::ElevatedQuotaExceeded { cap: 9 }.wire_code(),
            "quota_exceeded"
        );
    }

    #[test]
    fn elevated_permission_ids_match_the_catalog() {
        assert_eq!(
            ElevatedPermission::TransferFrom.id(),
            "economy.transfer_from"
        );
        assert_eq!(ElevatedPermission::AppMint.id(), "economy.app_mint");
    }

    #[test]
    fn elevated_caps_default_when_unset_or_invalid_and_clamp_to_the_ceiling() {
        let d = ElevatedCaps::from_raw(None, None, None);
        assert_eq!(
            d,
            ElevatedCaps {
                max_per_op: ELEVATED_DEFAULT_MAX_PER_OP,
                max_per_day: ELEVATED_DEFAULT_MAX_PER_DAY,
                max_per_target_per_day: ELEVATED_DEFAULT_MAX_PER_TARGET_DAY
            }
        );
        assert_eq!(ElevatedCaps::from_raw(Some(0), Some(-4), Some(0)), d);
        let set = ElevatedCaps::from_raw(Some(7), Some(70), Some(30));
        assert_eq!(
            (set.max_per_op, set.max_per_day, set.max_per_target_per_day),
            (7, 70, 30)
        );
        let big = ElevatedCaps::from_raw(Some(i64::MAX), Some(i64::MAX), Some(i64::MAX));
        assert_eq!(
            big,
            ElevatedCaps {
                max_per_op: ELEVATED_MAX_PER_OP_CEILING,
                max_per_day: ELEVATED_MAX_PER_DAY_CEILING,
                max_per_target_per_day: ELEVATED_MAX_PER_TARGET_DAY_CEILING
            }
        );
    }

    #[test]
    fn reason_is_a_pii_free_code() {
        for ok in ["steal", "a", "game.steal-v2_x", &"a".repeat(MAX_REASON_LEN)] {
            assert!(validate_reason(ok).is_ok(), "{ok}");
        }
        for bad in [
            "",
            "Steal",
            "free text",
            "a@b.c",
            "名前",
            "a\nb",
            &"a".repeat(MAX_REASON_LEN + 1),
        ] {
            assert!(
                matches!(validate_reason(bad), Err(EconomyError::Invalid(_))),
                "{bad:?}"
            );
        }
    }
}
