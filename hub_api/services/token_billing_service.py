"""Metered token billing -- the third metering axis (node/seat/token).

Real pydal ledger, owned by hub-api's marketplace module (NOT the
license-server -- see `critical-rules.md` "Licensing Model: Nodes &
Seats"). Advertised feature: premium metered consumables (e.g. an
AI-routing feature spends tokens per call). Schema: migration 076
(`token_products`, `community_token_balances`, `token_transactions` --
see that migration's own docstring for the full rationale).

**Atomicity -- a single combined executor job, NOT `transaction_async()`.**
`debit_tokens()`/`credit_tokens()` each submit ONE synchronous function
(`_guarded_debit_sync`/`_guarded_credit_sync`) to `AsyncDAL`'s own
`ThreadPoolExecutor` via `loop.run_in_executor()` -- the guarded UPDATE,
the balance re-read, the ledger INSERT, and the COMMIT all happen inside
that one submitted callable, with no `await` anywhere in the middle of
it. A `ThreadPoolExecutor` worker runs one submitted callable to
completion before picking up the next, so this whole sequence is
uninterruptible by any other coroutine's own submitted job -- including
another concurrent `debit_tokens()` call for the SAME `(community_id,
product_id)` pair.

This was NOT the first design tried. The obvious-looking
`async with async_dal.transaction_async(): await update_async(...); ...
await insert_async(...)` shape (i.e. `AsyncDAL.update_async()`/
`insert_async()`/`transaction_async()` used individually, each a
SEPARATE executor submission with `await` points in between) was tried
first and FAILED under `TestConcurrentDebitsDoNotOversell`'s own
concurrency test: `transaction_async()`'s commit/rollback act on
`self.dal`'s single ambient (thread-local, connection-scoped) transaction
state, not a transaction scoped to just the current coroutine's own
writes. With `pool_size=1` (this suite's own file-backed-sqlite fixture,
`hub_api/PORTING.md`'s `auth_db` gotcha) every concurrent `debit_tokens()`
call's executor submissions interleave on the SAME single worker
thread/connection -- so one coroutine's blocked-debit rollback (nothing
of ITS OWN to undo) instead rolled back OTHER, concurrently-in-flight
coroutines' successful, not-yet-committed decrements too. Confirmed
empirically: the first version of this test failed with the community's
final balance still at its STARTING value even though 10 of 25 debits
reported `ok=True` -- their writes existed only until a later sibling
coroutine's `transaction_async()` rollback wiped the whole shared
transaction. Bundling everything (including the commit) into one
executor-submitted synchronous function removes the multi-`await`
window entirely, so no sibling coroutine's job can ever run "in the
middle" of another's guarded update + ledger write + commit.

The WHERE-guarded UPDATE itself (`balance >= amount` alongside
`balance = balance - amount` in the SAME statement) is still what makes
this the "database, not application logic, arbitrates the race" pattern
`hub_oauth_exchange_codes` (migration 075) and `community_welcomed_users`
(migration 068) already established for single-use claims -- that
property holds independent of the executor-job restructuring above, and
is also what keeps this correct against a real multi-connection Postgres
pool in production (row-level locking serializes two concurrent UPDATEs
against the same row regardless of which connection issues them). The
`community_token_balances.balance >= 0` CHECK constraint (migration 076)
is a second, DB-level backstop against the same class of bug.

**BLOCK-WITH-FALLBACK enforcement.** `debit_tokens()` never raises to its
caller and never silently allows an under-funded spend:
  - Insufficient balance -> `TokenLedgerResult(ok=False,
    blocked_reason=REASON_INSUFFICIENT_BALANCE)` -- the caller (e.g.
    premium-AI routing) checks `.ok` and denies the metered action with a
    refill/upgrade path, exactly like `blueprints/v1/token_billing.py`'s
    `debit_tokens` route does for the HTTP caller (402 Payment Required +
    `upgrade_path`).
  - Ledger DB unavailable (any exception from the DAL call chain) ->
    `TokenLedgerResult(ok=False, blocked_reason=REASON_LEDGER_UNAVAILABLE)`
    -- fail CLOSED (deny the spend) rather than fail open, logged via
    `logger.error`, never propagated as an unhandled exception that would
    crash the calling feature.

Per `hub_api/PORTING.md` Gotcha #1: pydal query builder only, no raw SQL
(`%s`-placeholder helpers are Postgres-only and 500 against sqlite in
tests). Per Gotcha #2: pydal never autocommits -- both guarded-write
helpers below call `dal.commit()` themselves as their last step.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

from services.errors import bad_request, not_found

logger = logging.getLogger(__name__)

#: `TokenLedgerResult.blocked_reason` values -- a caller-facing, stable
#: vocabulary (never a raw exception message) so both the internal
#: `debit_tokens()` API and the HTTP blueprint can branch on it.
REASON_UNKNOWN_PRODUCT = "unknown_product"
REASON_INSUFFICIENT_BALANCE = "insufficient_balance"
REASON_NO_BALANCE = "no_balance"
REASON_LEDGER_UNAVAILABLE = "ledger_unavailable"
#: An idempotency key was reused by a different actor, with a different amount, or after its
#: replay window closed -- never a free replay (HTTP 409).
REASON_IDEMPOTENCY_CONFLICT = "idempotency_key_conflict"

#: Sentinel `_guarded_debit_sync` returns as its `outcome` on success --
#: distinct from the `REASON_*` vocabulary above (a fully successful debit
#: has no "reason", blocked ones do).
_OUTCOME_OK = "ok"


@dataclass(slots=True, frozen=True)
class TokenProductDTO:
    """One `token_products` catalog row."""

    id: int
    key: str
    name: str
    unit: str
    price_cents: int
    tokens_granted: int
    active: bool


@dataclass(slots=True, frozen=True)
class TokenBalanceDTO:
    """A community's balance for one product -- `balance=0` if never credited."""

    product_id: int
    product_key: str
    product_name: str
    unit: str
    balance: int
    updated_at: str | None


@dataclass(slots=True, frozen=True)
class TokenTransactionDTO:
    """One append-only `token_transactions` ledger row."""

    id: int
    community_id: int
    product_id: int
    product_key: str
    delta: int
    reason: str
    ref: str | None
    balance_after: int
    created_at: str | None


@dataclass(slots=True, frozen=True)
class TokenLedgerResult:
    """Outcome of a `credit_tokens()`/`debit_tokens()` call -- never raises for a blocked spend."""

    ok: bool
    balance_after: int | None
    transaction_id: int | None
    blocked_reason: str | None


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else value


def _product_dto(row: Any) -> TokenProductDTO:
    return TokenProductDTO(
        id=row.id,
        key=row.key,
        name=row.name,
        unit=row.unit,
        price_cents=row.price_cents,
        tokens_granted=row.tokens_granted,
        active=row.active,
    )


async def _get_active_product(async_dal: Any, dal: Any, product_key: str) -> Any | None:
    """Return the `token_products` row for `product_key`, or `None` if missing/inactive.

    Read-only, so it's fine as an ordinary `select_async()` call ahead of
    the guarded write job below -- a product being deactivated in the
    (negligible) window between this lookup and the guarded write doesn't
    threaten balance correctness, only the "did we spend against an
    active product" check, which is not the security-critical property
    this module's atomicity design protects.
    """
    rows = await async_dal.select_async(
        dal((dal.token_products.key == product_key) & (dal.token_products.active == True))  # noqa: E712
    )
    return rows.first() if rows else None


async def list_products(
    async_dal: Any, dal: Any, *, include_inactive: bool = False
) -> list[TokenProductDTO]:
    """The token catalog -- active products only unless `include_inactive` (admin view)."""
    query = dal.token_products.id > 0
    if not include_inactive:
        query &= dal.token_products.active == True  # noqa: E712
    rows = await async_dal.select_async(dal(query), orderby=dal.token_products.name)
    return [_product_dto(r) for r in rows]


async def list_balances(async_dal: Any, dal: Any, *, community_id: int) -> list[TokenBalanceDTO]:
    """Every active product's balance for `community_id` -- 0 for products never credited.

    LEFT JOIN selecting fields from both `token_products` and
    `community_token_balances` -- rows nest under `row.<table>.<field>`
    (`hub_api/PORTING.md` Gotcha #6: this only happens when 2+ tables are
    selected together, unlike a single-table selection across a JOIN
    condition).
    """
    rows = await async_dal.select_async(
        dal(dal.token_products.active == True),  # noqa: E712
        dal.token_products.ALL,
        dal.community_token_balances.ALL,
        left=dal.community_token_balances.on(
            (dal.community_token_balances.product_id == dal.token_products.id)
            & (dal.community_token_balances.community_id == community_id)
        ),
        orderby=dal.token_products.name,
    )
    result: list[TokenBalanceDTO] = []
    for row in rows:
        product, balance_row = row.token_products, row.community_token_balances
        # pydal's LEFT JOIN never yields a bare `None` for the unmatched
        # side -- it's a real `Row` object with every field set to
        # `None` (confirmed empirically), so the presence check has to
        # be on a field, not identity against `None` itself.
        has_balance = balance_row is not None and balance_row.id is not None
        result.append(
            TokenBalanceDTO(
                product_id=product.id,
                product_key=product.key,
                product_name=product.name,
                unit=product.unit,
                balance=int(balance_row.balance) if has_balance else 0,
                updated_at=_iso(balance_row.updated_at) if has_balance else None,
            )
        )
    return result


async def get_balance(async_dal: Any, dal: Any, *, community_id: int, product_key: str) -> int:
    """Single-product spendable balance for `community_id` -- 0 if the product/balance don't exist.

    Read-only, single-row convenience wrapper `services/token_ledger.py`'s
    delegation layer uses ahead of its own `debit_tokens()` call (the AI
    router's pre-check, `services/ai_routing/router.py::route_completion`)
    -- cheaper than scanning `list_balances()`'s all-active-products LEFT
    JOIN when the caller only cares about one product. An unknown/inactive
    `product_key` is indistinguishable from "never credited" here (both
    read as 0) -- same "unknown collapses to empty/zero rather than a
    special-cased error" convention `list_transactions()` above already
    uses for its own `product_key` filter.
    """
    product = await _get_active_product(async_dal, dal, product_key)
    if product is None:
        return 0
    rows = await async_dal.select_async(
        dal(
            (dal.community_token_balances.community_id == community_id)
            & (dal.community_token_balances.product_id == product.id)
        )
    )
    if not rows:
        return 0
    return int(rows.first().balance)


async def list_transactions(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    product_key: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[TokenTransactionDTO], int]:
    """Paginated, filtered ledger history for `community_id`. Returns `(rows, total_count)`."""
    query = dal.token_transactions.community_id == community_id
    if product_key is not None:
        product = await _get_active_product(async_dal, dal, product_key)
        # An unknown/inactive product key legitimately has zero matching
        # transactions -- `-1` never matches a real `product_id`, so this
        # collapses to an empty page rather than a special-cased error.
        query &= dal.token_transactions.product_id == (product.id if product is not None else -1)
    if start is not None:
        query &= dal.token_transactions.created_at >= start
    if end is not None:
        query &= dal.token_transactions.created_at <= end

    total = await async_dal.count_async(query)
    rows = await async_dal.select_async(
        dal(query),
        dal.token_transactions.ALL,
        dal.token_products.ALL,
        left=dal.token_products.on(dal.token_transactions.product_id == dal.token_products.id),
        # `id` DESC as a tie-break -- `created_at` alone can tie at
        # whatever the platform's clock resolution is, especially for two
        # writes issued microseconds apart in a hot loop (e.g. this
        # group's own concurrency test); insertion order (`id`) is always
        # unambiguous.
        orderby=~dal.token_transactions.created_at | ~dal.token_transactions.id,
        limitby=(offset, offset + limit),
    )
    dtos = [
        TokenTransactionDTO(
            id=row.token_transactions.id,
            community_id=row.token_transactions.community_id,
            product_id=row.token_transactions.product_id,
            # Same pydal LEFT JOIN caveat as `list_balances()` above -- an
            # unmatched side is a real `Row` with all-`None` fields, not
            # a bare `None` (shouldn't happen here in practice, since
            # every `token_transactions.product_id` is written from a
            # real `token_products.id` by this module's own write paths,
            # but defensive rather than an `AttributeError` if data ever
            # drifts).
            product_key=(
                row.token_products.key
                if row.token_products is not None and row.token_products.id is not None
                else ""
            ),
            delta=row.token_transactions.delta,
            reason=row.token_transactions.reason,
            ref=row.token_transactions.ref,
            balance_after=row.token_transactions.balance_after,
            created_at=_iso(row.token_transactions.created_at),
        )
        for row in rows
    ]
    return dtos, int(total)


def _guarded_credit_sync(
    dal: Any, *, community_id: int, product_id: int, amount: int, reason: str, ref: str | None
) -> tuple[int, int]:
    """The atomic credit unit -- see this module's top-of-file docstring for why.

    UPDATE-first, INSERT-if-missing for the balance row; the migration
    076 `UNIQUE (community_id, product_id)` constraint arbitrates a race
    against a genuinely concurrent (different-connection, e.g. real
    Postgres pool_size > 1) first-ever credit for the same pair -- the
    loser's INSERT raises, caught below and retried as an UPDATE against
    the winner's now-existing row. Runs entirely on one executor thread
    (see `credit_tokens()`), so within THIS process no sibling call can
    interleave; the try/except below is a backstop for true
    cross-connection concurrency, not this process's own scheduling.
    """
    now = datetime.now(UTC)
    updated = dal(
        (dal.community_token_balances.community_id == community_id)
        & (dal.community_token_balances.product_id == product_id)
    ).update(balance=dal.community_token_balances.balance + amount, updated_at=now)
    if not updated:
        try:
            dal.community_token_balances.insert(
                community_id=community_id, product_id=product_id, balance=amount, updated_at=now
            )
        except Exception:  # noqa: BLE001 -- cross-connection race retry, see docstring above
            dal.rollback()
            dal(
                (dal.community_token_balances.community_id == community_id)
                & (dal.community_token_balances.product_id == product_id)
            ).update(balance=dal.community_token_balances.balance + amount, updated_at=now)

    row = (
        dal(
            (dal.community_token_balances.community_id == community_id)
            & (dal.community_token_balances.product_id == product_id)
        )
        .select()
        .first()
    )
    balance_after = int(row.balance)
    txn_id = dal.token_transactions.insert(
        community_id=community_id,
        product_id=product_id,
        delta=amount,
        reason=reason,
        ref=ref,
        balance_after=balance_after,
        created_at=now,
    )
    dal.commit()
    return balance_after, int(txn_id)


async def credit_tokens(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    product_key: str,
    amount: int,
    reason: str,
    ref: str | None = None,
) -> TokenLedgerResult:
    """Grant `amount` tokens of `product_key` to `community_id` (admin action or purchase grant).

    Raises :class:`ApiError` for caller-fixable input errors (non-positive
    amount, unknown/inactive product) -- these are 400/404s at the HTTP
    layer, not "blocked" ledger outcomes. A DB failure during the write
    itself degrades to `TokenLedgerResult(ok=False,
    blocked_reason=REASON_LEDGER_UNAVAILABLE)` instead of raising, per
    this module's BLOCK-WITH-FALLBACK policy.
    """
    if amount <= 0:
        raise bad_request("Credit amount must be positive")
    product = await _get_active_product(async_dal, dal, product_key)
    if product is None:
        raise not_found(f"Unknown or inactive token product '{product_key}'")

    try:
        loop = asyncio.get_running_loop()
        balance_after, txn_id = await loop.run_in_executor(
            async_dal.executor,
            partial(
                _guarded_credit_sync,
                dal,
                community_id=community_id,
                product_id=product.id,
                amount=amount,
                reason=reason,
                ref=ref,
            ),
        )
    except Exception as exc:  # noqa: BLE001 -- ledger-unavailable boundary, see module docstring
        logger.error(
            "token_billing.credit_failed community_id=%s product_key=%s error=%s",
            community_id,
            product_key,
            exc,
        )
        return TokenLedgerResult(
            ok=False,
            balance_after=None,
            transaction_id=None,
            blocked_reason=REASON_LEDGER_UNAVAILABLE,
        )

    return TokenLedgerResult(
        ok=True, balance_after=balance_after, transaction_id=txn_id, blocked_reason=None
    )


#: Default ceiling for a single debit (tokens). Override with `TOKEN_DEBIT_MAX_AMOUNT`; see
#: `docs/token-billing-debit.md`. Bounds the blast radius of a buggy or compromised caller.
DEFAULT_MAX_DEBIT_AMOUNT = 10_000

#: Longest caller-supplied idempotency key accepted (the stored key adds a `<cid>:<pid>:` prefix).
MAX_IDEMPOTENCY_KEY_LEN = 128


def max_debit_amount() -> int:
    """Return the configured per-debit ceiling (`TOKEN_DEBIT_MAX_AMOUNT`, default 10000).

    Read per call so an operator override takes effect without a code change; a malformed or
    non-positive value falls back to the default rather than disabling the bound.
    """
    try:
        value = int(os.environ.get("TOKEN_DEBIT_MAX_AMOUNT", DEFAULT_MAX_DEBIT_AMOUNT))
    except ValueError:
        return DEFAULT_MAX_DEBIT_AMOUNT
    return value if value > 0 else DEFAULT_MAX_DEBIT_AMOUNT


#: Default replay window (seconds) for an idempotency key. Override with
#: `TOKEN_DEBIT_IDEMPOTENCY_TTL_SECONDS`. A key is a retry guard for ONE admission attempt, not a
#: reusable coupon: after this window a reused key is rejected, never replayed.
DEFAULT_IDEMPOTENCY_TTL_SECONDS = 600


def idempotency_ttl_seconds() -> int:
    """Return the replay window (`TOKEN_DEBIT_IDEMPOTENCY_TTL_SECONDS`, default 600).

    A malformed or non-positive value falls back to the default rather than disabling the bound.
    """
    try:
        value = int(
            os.environ.get("TOKEN_DEBIT_IDEMPOTENCY_TTL_SECONDS", DEFAULT_IDEMPOTENCY_TTL_SECONDS)
        )
    except ValueError:
        return DEFAULT_IDEMPOTENCY_TTL_SECONDS
    return value if value > 0 else DEFAULT_IDEMPOTENCY_TTL_SECONDS


def _scoped_idempotency_key(community_id: int, product_id: int, key: str | None) -> str | None:
    """Scope a caller key to its community+product so keys never collide across tenants."""
    if key is None:
        return None
    return f"{community_id}:{product_id}:{key}"


def _replay_sync(
    dal: Any, scoped_key: str, *, actor_user_id: int | None, amount: int
) -> tuple[str, int | None, int | None] | None:
    """Resolve a previously-seen idempotency key: replay the ORIGINAL result, or refuse.

    Returns `None` when the key was never used. A hit is replayed only when it is still inside
    the replay window AND was recorded for the same actor and amount; otherwise the outcome is
    `REASON_IDEMPOTENCY_CONFLICT` (nothing debited, nothing replayed) -- a reused key must never
    turn into a free debit.
    """
    row = dal(dal.token_transactions.idempotency_key == scoped_key).select().first()
    dal.commit()
    if row is None:
        return None
    created = row.created_at
    if created is not None and created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    expired = created is None or datetime.now(UTC) - created > timedelta(
        seconds=idempotency_ttl_seconds()
    )
    stored_actor = None if row.actor_user_id is None else int(row.actor_user_id)
    if expired or stored_actor != actor_user_id or -int(row.delta) != amount:
        return REASON_IDEMPOTENCY_CONFLICT, None, None
    return _OUTCOME_OK, int(row.balance_after), int(row.id)


def _guarded_debit_sync(
    dal: Any,
    *,
    community_id: int,
    product_id: int,
    amount: int,
    reason: str,
    ref: str | None,
    idempotency_key: str | None = None,
    actor_user_id: int | None = None,
) -> tuple[str, int | None, int | None]:
    """The atomic debit unit -- see this module's top-of-file docstring for why this shape.

    The WHERE-guarded UPDATE (`balance >= amount` alongside the
    decrement, in the SAME statement) is the actual oversell guard; the
    "one combined executor job" wrapping is what makes the balance
    re-read + ledger INSERT + COMMIT that follow a successful guard
    inseparable from it, for THIS process's own concurrent callers.

    Returns `(outcome, balance_after, transaction_id)` -- `outcome` is
    `_OUTCOME_OK` or one of `REASON_INSUFFICIENT_BALANCE`/
    `REASON_NO_BALANCE`, with `balance_after`/`transaction_id` `None` for
    a blocked outcome.
    """
    scoped_key = _scoped_idempotency_key(community_id, product_id, idempotency_key)
    if scoped_key is not None:
        replayed = _replay_sync(dal, scoped_key, actor_user_id=actor_user_id, amount=amount)
        if replayed is not None:
            return replayed
    now = datetime.now(UTC)
    updated = dal(
        (dal.community_token_balances.community_id == community_id)
        & (dal.community_token_balances.product_id == product_id)
        & (dal.community_token_balances.balance >= amount)
    ).update(balance=dal.community_token_balances.balance - amount, updated_at=now)
    if not updated:
        exists = dal(
            (dal.community_token_balances.community_id == community_id)
            & (dal.community_token_balances.product_id == product_id)
        ).count()
        # Nothing was written by this call -- commit is a no-op here, not
        # a rollback, so we never touch any OTHER in-flight job's state
        # (there shouldn't be any, since this whole function is one
        # uninterruptible executor job, but this keeps the connection's
        # transaction state clean regardless).
        dal.commit()
        return (REASON_INSUFFICIENT_BALANCE if exists else REASON_NO_BALANCE), None, None

    row = (
        dal(
            (dal.community_token_balances.community_id == community_id)
            & (dal.community_token_balances.product_id == product_id)
        )
        .select()
        .first()
    )
    balance_after = int(row.balance)
    try:
        txn_id = dal.token_transactions.insert(
            community_id=community_id,
            product_id=product_id,
            delta=-amount,
            reason=reason,
            ref=ref,
            balance_after=balance_after,
            idempotency_key=scoped_key,
            actor_user_id=actor_user_id,
            created_at=now,
        )
    except Exception:
        # A concurrent same-key debit committed first (UNIQUE idempotency_key). Roll back THIS
        # call's decrement so the key debits exactly once, then return the original result.
        dal.rollback()
        if scoped_key is not None:
            replayed = _replay_sync(dal, scoped_key, actor_user_id=actor_user_id, amount=amount)
            if replayed is not None:
                return replayed
        raise
    dal.commit()
    return _OUTCOME_OK, balance_after, int(txn_id)


async def debit_tokens(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    product_key: str,
    amount: int,
    reason: str,
    ref: str | None = None,
    idempotency_key: str | None = None,
    actor_user_id: int | None = None,
) -> TokenLedgerResult:
    """Spend `amount` tokens of `product_key` from `community_id` -- the internal metering API.

    THE call other features (e.g. premium-AI routing) make to meter
    consumption: `result = await debit_tokens(async_dal, dal,
    community_id=cid, product_key="ai_routing_call", amount=1,
    reason="ai_route:gpt-4o", ref=request_id)`. Callers check `.ok`
    before performing the metered action -- this function NEVER raises
    for a blocked spend (insufficient balance / unknown product / ledger
    unavailable all return `ok=False` with a `blocked_reason`), so a
    metering call site can never crash the feature invoking it. Only a
    caller-fixable input error (non-positive `amount`) raises
    :class:`ApiError`, matching `credit_tokens()`'s same convention -- as does an `amount`
    above :func:`max_debit_amount` or an over-long `idempotency_key`.

    `idempotency_key` (optional, stable per admission -- never a timestamp) makes the debit
    replay-safe: a second call with the same key (scoped to community+product), for the same
    `actor_user_id` and `amount`, within the replay window (`idempotency_ttl_seconds`) debits
    NOTHING and returns the ORIGINAL balance_after/transaction_id; the UNIQUE column arbitrates
    concurrent same-key calls so exactly one debits. A reused key from another actor, with another
    amount, or after the window returns blocked `REASON_IDEMPOTENCY_CONFLICT` (never a replay).

    See this module's own top-of-file docstring for the atomicity
    argument (the single combined executor job + the WHERE-guarded
    UPDATE inside it).
    """
    if amount <= 0:
        raise bad_request("Debit amount must be positive")
    if amount > max_debit_amount():
        raise bad_request(f"Debit amount exceeds the maximum of {max_debit_amount()}")
    if idempotency_key is not None and not 0 < len(idempotency_key) <= MAX_IDEMPOTENCY_KEY_LEN:
        raise bad_request(f"idempotency_key must be 1-{MAX_IDEMPOTENCY_KEY_LEN} characters")

    try:
        product = await _get_active_product(async_dal, dal, product_key)
        if product is None:
            return TokenLedgerResult(
                ok=False,
                balance_after=None,
                transaction_id=None,
                blocked_reason=REASON_UNKNOWN_PRODUCT,
            )

        loop = asyncio.get_running_loop()
        outcome, balance_after, txn_id = await loop.run_in_executor(
            async_dal.executor,
            partial(
                _guarded_debit_sync,
                dal,
                community_id=community_id,
                product_id=product.id,
                amount=amount,
                reason=reason,
                ref=ref,
                idempotency_key=idempotency_key,
                actor_user_id=actor_user_id,
            ),
        )
    except Exception as exc:  # noqa: BLE001 -- ledger-unavailable boundary, see module docstring
        logger.error(
            "token_billing.debit_failed community_id=%s product_key=%s error=%s",
            community_id,
            product_key,
            exc,
        )
        return TokenLedgerResult(
            ok=False,
            balance_after=None,
            transaction_id=None,
            blocked_reason=REASON_LEDGER_UNAVAILABLE,
        )

    if outcome != _OUTCOME_OK:
        return TokenLedgerResult(
            ok=False, balance_after=None, transaction_id=None, blocked_reason=outcome
        )
    return TokenLedgerResult(
        ok=True, balance_after=balance_after, transaction_id=txn_id, blocked_reason=None
    )
