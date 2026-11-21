"""Community loyalty core-currency service (gh-317).

Replaces the previous reverse-proxy client to the separate
`loyalty-interaction` deployment (giveaways/games/gear-shop, a Node port
of `loyaltyController.js`) with a Python-native implementation against
this port's own `loyalty_*` tables (migration `0015_loyalty_core_tables`,
bound via `services.schema.bind_loyalty_tables()` -- read that function's
own docstring for the exact column list before touching this module).
Module path (`services.community_loyalty`) and blueprint import surface
are kept identical so `blueprints/v1/community_loyalty.py` and every test
importing `services.community_loyalty` keep resolving; every symbol the
old proxy exported (`get_or_default`/`call`/`LoyaltyProxyError`/
`DEFAULT_LOYALTY_CONFIG`) is gone, along with the giveaways/games/gear-shop
surface it backed -- that feature set belonged to the separate downstream
deployment being retired, not to this MVP's core-currency scope (the
migration's own docstring: "loyalty MVP core-currency tables").

Every function is tenant-scoped by `community_id` alone -- the caller
(blueprint layer) has already resolved and authorized `community_id`
against the token's tenant (`services.community_common.community_in_tenant`)
or against a valid `X-Service-Key` (service-to-service), same trust
boundary this port's other service modules follow (see
`services/community_music_queue_service.py`'s own module docstring).

Atomicity follows that same module's `_sync_get_live_state`/
`_sync_guarded_advance` precedent: every read-modify-write against
`loyalty_balances`/`loyalty_shop_items`/`loyalty_redemptions` is bundled
into ONE synchronous function submitted as a single
`loop.run_in_executor()` job (`AsyncDAL.transaction_async()`'s own
docstring is explicit that composing multiple awaited `*_async()` calls
does NOT guarantee cross-statement atomicity), with `SELECT ... FOR
UPDATE` locking on every non-sqlite adapter and an explicit
`dal.commit()`/`dal.rollback()` pair. Every balance mutation appends
exactly one `loyalty_transactions` row (`delta`/`balance_after` ledger,
append-only, never edited/deleted) -- see `bind_loyalty_tables()`'s own
docstring on that naming convention.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from services.errors import ApiError, bad_request, conflict, not_found
from services.schema import bind_loyalty_tables

logger = logging.getLogger(__name__)

_VALID_EARN_KINDS = frozenset({"earn_chat", "earn_watch"})
_KIND_ADJUST = "adjust"
_KIND_REDEEM = "redeem"
_KIND_REFUND = "refund"
_KIND_WIPE = "wipe"

#: Default/hard-ceiling page size for `leaderboard()` -- matches
#: `services/pagination.py`'s own `DEFAULT_MAX_PAGE_SIZE` convention,
#: kept local (not imported) since this module has no other dependency on
#: that HTTP-layer helper.
_MAX_LEADERBOARD_LIMIT = 100


# ---------------------------------------------------------------------------
# Wire DTOs -- snake_case, matching this MVP's own internal-route body/query
# param naming (`platform_user_id`/`actor_platform_user_id`/etc., pinned by
# the chat-bundle callers of the internal blueprint) rather than the other
# camelCase v1 groups' convention; see `blueprints/v1/community_loyalty.py`'s
# own module docstring for the same choice on the admin surface.
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ConfigDTO:
    """One community's `loyalty_config` row."""

    community_id: int
    currency_name: str
    currency_symbol: str
    earn_chat_points: int
    earn_chat_cooldown_s: int
    earn_watch_points_per_min: int
    earn_watch_enabled: bool
    max_balance: int | None
    enabled: bool
    updated_at: str | None


@dataclass(slots=True, frozen=True)
class BalanceDTO:
    """One `(community_id, platform, platform_user_id)` balance -- zeroed if no row exists yet."""

    community_id: int
    platform: str
    platform_user_id: str
    balance: int
    lifetime_earned: int
    lifetime_spent: int
    currency_name: str
    currency_symbol: str


@dataclass(slots=True, frozen=True)
class EarnResultDTO:
    """Result of `earn()` -- the resulting balance plus the delta actually applied.

    `applied_delta` can be less than the requested `points` when
    `max_balance` clamps the credit -- never more.
    """

    balance: BalanceDTO
    applied_delta: int


@dataclass(slots=True, frozen=True)
class LeaderboardEntryDTO:
    """One `leaderboard()` row."""

    platform: str
    platform_user_id: str
    balance: int
    #: The member's public UUID (`community_members.user_uuid`) -- the only id admin write routes
    #: accept in a URL. `None` when the member row has no resolved UUID.
    user_uuid: str | None = None


@dataclass(slots=True, frozen=True)
class ShopItemDTO:
    """One `loyalty_shop_items` row."""

    id: int
    community_id: int
    sku: str
    name: str
    description: str | None
    cost: int
    stock: int | None
    enabled: bool
    requires_mod_approval: bool


@dataclass(slots=True, frozen=True)
class RedemptionDTO:
    """One `loyalty_redemptions` row."""

    id: int
    community_id: int
    item_id: int
    platform: str
    platform_user_id: str
    cost: int
    status: str
    note: str | None
    item_name: str
    currency_name: str
    currency_symbol: str


@dataclass(slots=True, frozen=True)
class StatsDTO:
    """Community-wide loyalty currency stats -- backs the admin `GET .../stats` route."""

    community_id: int
    total_users: int
    total_currency: int
    average_balance: float


def _ensure_tables(dal: Any) -> None:
    """Idempotently bind the `loyalty_*` tables -- cheap membership check, see module docstring."""
    bind_loyalty_tables(dal)


def _for_update(dal: Any) -> bool:
    """`SELECT ... FOR UPDATE` on every adapter except sqlite (rejects the syntax outright)."""
    return bool(dal._adapter.dbengine != "sqlite")


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _config_dto(row: Any) -> ConfigDTO:
    return ConfigDTO(
        community_id=int(row.community_id),
        currency_name=row.currency_name,
        currency_symbol=row.currency_symbol,
        earn_chat_points=int(row.earn_chat_points),
        earn_chat_cooldown_s=int(row.earn_chat_cooldown_s),
        earn_watch_points_per_min=int(row.earn_watch_points_per_min),
        earn_watch_enabled=bool(row.earn_watch_enabled),
        max_balance=int(row.max_balance) if row.max_balance is not None else None,
        enabled=bool(row.enabled),
        updated_at=_iso(row.updated_at),
    )


def _balance_dto(
    community_id: int,
    platform: str,
    platform_user_id: str,
    row: Any | None,
    currency_name: str = "points",
    currency_symbol: str = "",
) -> BalanceDTO:
    if row is None:
        return BalanceDTO(
            community_id,
            platform,
            platform_user_id,
            0,
            0,
            0,
            currency_name,
            currency_symbol,
        )
    return BalanceDTO(
        community_id=community_id,
        platform=platform,
        platform_user_id=platform_user_id,
        balance=int(row.balance),
        lifetime_earned=int(row.lifetime_earned),
        lifetime_spent=int(row.lifetime_spent),
        currency_name=currency_name,
        currency_symbol=currency_symbol,
    )


def _item_dto(row: Any) -> ShopItemDTO:
    return ShopItemDTO(
        id=int(row.id),
        community_id=int(row.community_id),
        sku=row.sku,
        name=row.name,
        description=row.description,
        cost=int(row.cost),
        stock=int(row.stock) if row.stock is not None else None,
        enabled=bool(row.enabled),
        requires_mod_approval=bool(row.requires_mod_approval),
    )


def _redemption_dto(
    row: Any,
    item_name: str = "",
    currency_name: str = "points",
    currency_symbol: str = "",
) -> RedemptionDTO:
    return RedemptionDTO(
        id=int(row.id),
        community_id=int(row.community_id),
        item_id=int(row.item_id),
        platform=row.platform,
        platform_user_id=row.platform_user_id,
        cost=int(row.cost),
        status=row.status,
        note=row.note,
        item_name=item_name,
        currency_name=currency_name,
        currency_symbol=currency_symbol,
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


async def get_config(async_dal: Any, dal: Any, *, community_id: int) -> ConfigDTO:
    """Return the community's loyalty config, creating the default (enabled) row if missing.

    The default row (all column defaults from `bind_loyalty_tables()`) is
    inserted lazily on first read -- same convention as
    `community_music_queue_service.get_policy()`.
    """
    _ensure_tables(dal)
    rows = await async_dal.select_async(dal(dal.loyalty_config.community_id == community_id))
    if rows:
        return _config_dto(rows.first())

    now = datetime.now(UTC)
    new_id = await async_dal.insert_async(
        dal.loyalty_config, community_id=community_id, updated_at=now
    )
    rows = await async_dal.select_async(dal(dal.loyalty_config.id == int(new_id)))
    logger.debug("loyalty.config.default_created community_id=%s", community_id)
    return _config_dto(rows.first())


async def set_config(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    currency_name: str | None = None,
    currency_symbol: str | None = None,
    earn_chat_points: int | None = None,
    earn_chat_cooldown_s: int | None = None,
    earn_watch_points_per_min: int | None = None,
    earn_watch_enabled: bool | None = None,
    max_balance: int | None = None,
    enabled: bool | None = None,
) -> ConfigDTO:
    """Upsert the community's config -- only the provided (non-`None`) fields change.

    `None` means "leave unchanged" for every field, same partial-update
    convention as `community_music_queue_service.set_policy()` -- there is
    currently no way to explicitly clear an already-set `max_balance` back
    to unlimited via this call (would need a sentinel-vs-`None`
    distinction this MVP doesn't yet need); a future caller needing that
    should extend this signature rather than repurpose bare `None`.
    """
    if (
        currency_name is None
        and currency_symbol is None
        and earn_chat_points is None
        and earn_chat_cooldown_s is None
        and earn_watch_points_per_min is None
        and earn_watch_enabled is None
        and max_balance is None
        and enabled is None
    ):
        raise bad_request("No config fields to update")
    if currency_name is not None and not currency_name.strip():
        raise bad_request("currency_name must not be blank")
    if earn_chat_points is not None and earn_chat_points < 0:
        raise bad_request("earn_chat_points must be >= 0")
    if earn_chat_cooldown_s is not None and earn_chat_cooldown_s < 0:
        raise bad_request("earn_chat_cooldown_s must be >= 0")
    if earn_watch_points_per_min is not None and earn_watch_points_per_min < 0:
        raise bad_request("earn_watch_points_per_min must be >= 0")
    if max_balance is not None and max_balance < 0:
        raise bad_request("max_balance must be >= 0")

    _ensure_tables(dal)
    now = datetime.now(UTC)
    existing = await async_dal.select_async(dal(dal.loyalty_config.community_id == community_id))

    if not existing:
        new_id = await async_dal.insert_async(
            dal.loyalty_config,
            community_id=community_id,
            **{
                k: v
                for k, v in {
                    "currency_name": currency_name,
                    "currency_symbol": currency_symbol,
                    "earn_chat_points": earn_chat_points,
                    "earn_chat_cooldown_s": earn_chat_cooldown_s,
                    "earn_watch_points_per_min": earn_watch_points_per_min,
                    "earn_watch_enabled": earn_watch_enabled,
                    "max_balance": max_balance,
                    "enabled": enabled,
                }.items()
                if v is not None
            },
            updated_at=now,
        )
        rows = await async_dal.select_async(dal(dal.loyalty_config.id == int(new_id)))
        logger.debug("loyalty.config.created_via_update community_id=%s", community_id)
        return _config_dto(rows.first())

    fields: dict[str, Any] = {"updated_at": now}
    if currency_name is not None:
        fields["currency_name"] = currency_name
    if currency_symbol is not None:
        fields["currency_symbol"] = currency_symbol
    if earn_chat_points is not None:
        fields["earn_chat_points"] = earn_chat_points
    if earn_chat_cooldown_s is not None:
        fields["earn_chat_cooldown_s"] = earn_chat_cooldown_s
    if earn_watch_points_per_min is not None:
        fields["earn_watch_points_per_min"] = earn_watch_points_per_min
    if earn_watch_enabled is not None:
        fields["earn_watch_enabled"] = earn_watch_enabled
    if max_balance is not None:
        fields["max_balance"] = max_balance
    if enabled is not None:
        fields["enabled"] = enabled

    query = dal.loyalty_config.community_id == community_id
    await async_dal.update_async(query, **fields)
    rows = await async_dal.select_async(dal(query))
    logger.debug("loyalty.config.updated community_id=%s fields=%s", community_id, list(fields))
    return _config_dto(rows.first())


# ---------------------------------------------------------------------------
# Balance (read)
# ---------------------------------------------------------------------------


async def get_balance(
    async_dal: Any, dal: Any, *, community_id: int, platform: str, platform_user_id: str
) -> BalanceDTO:
    """Return a caller's balance -- zeroed (no insert) if they've never earned/spent anything."""
    _ensure_tables(dal)
    config = await get_config(async_dal, dal, community_id=community_id)
    query = (
        (dal.loyalty_balances.community_id == community_id)
        & (dal.loyalty_balances.platform == platform)
        & (dal.loyalty_balances.platform_user_id == platform_user_id)
    )
    rows = await async_dal.select_async(dal(query))
    row = rows.first() if rows else None
    return _balance_dto(
        community_id,
        platform,
        platform_user_id,
        row,
        currency_name=config.currency_name,
        currency_symbol=config.currency_symbol,
    )


def _sync_get_or_create_balance_row(
    dal: Any, *, community_id: int, platform: str, platform_user_id: str, for_update: bool
) -> Any:
    """Lock (or create) the `loyalty_balances` row -- caller commits/rolls back the executor job."""
    b = dal.loyalty_balances
    query = (
        (b.community_id == community_id)
        & (b.platform == platform)
        & (b.platform_user_id == platform_user_id)
    )
    row = dal(query).select(for_update=for_update).first()
    if row is not None:
        return row

    now = datetime.now(UTC)
    new_id = b.insert(
        community_id=community_id,
        platform=platform,
        platform_user_id=platform_user_id,
        balance=0,
        lifetime_earned=0,
        lifetime_spent=0,
        updated_at=now,
    )
    return dal(b.id == new_id).select(for_update=for_update).first()


# ---------------------------------------------------------------------------
# Earn
# ---------------------------------------------------------------------------


def _sync_earn(
    dal: Any,
    *,
    community_id: int,
    platform: str,
    platform_user_id: str,
    kind: str,
    points: int,
    ref: str | None,
    now: datetime,
) -> tuple[Any, int]:
    """Single executor job: lock balance row, clamp to `max_balance`, credit, ledger, commit.

    `applied_delta` (the second return value) is `<= points` -- clamped
    down, never up, when the credit would push the balance past a
    configured `max_balance`.
    """
    for_update = _for_update(dal)
    try:
        config_row = (
            dal(dal.loyalty_config.community_id == community_id)
            .select(for_update=for_update)
            .first()
        )
        max_balance = (
            int(config_row.max_balance)
            if config_row is not None and config_row.max_balance is not None
            else None
        )

        row = _sync_get_or_create_balance_row(
            dal,
            community_id=community_id,
            platform=platform,
            platform_user_id=platform_user_id,
            for_update=for_update,
        )
        current = int(row.balance)
        proposed = current + points
        new_balance = min(proposed, max_balance) if max_balance is not None else proposed
        applied_delta = new_balance - current

        dal(dal.loyalty_balances.id == row.id).update(
            balance=new_balance,
            lifetime_earned=int(row.lifetime_earned) + applied_delta,
            updated_at=now,
        )
        dal.loyalty_transactions.insert(
            community_id=community_id,
            platform=platform,
            platform_user_id=platform_user_id,
            delta=applied_delta,
            balance_after=new_balance,
            kind=kind,
            ref=ref,
            actor_platform_user_id=None,
            created_at=now,
        )
        refreshed = dal(dal.loyalty_balances.id == row.id).select().first()
        dal.commit()
        return refreshed, applied_delta
    except Exception:
        dal.rollback()
        raise


async def earn(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    platform: str,
    platform_user_id: str,
    kind: str,
    points: int,
    ref: str | None = None,
) -> EarnResultDTO:
    """Credit `points` for one `earn_chat`/`earn_watch` event; respects `max_balance`.

    Cooldown enforcement is the CALLER's job (the chat-bundle/watch-tracker
    already knows the last-earn timestamp per viewer) -- this function only
    applies the credit and writes the ledger row, never itself rate-limits.
    """
    if kind not in _VALID_EARN_KINDS:
        raise bad_request(f"unknown earn kind '{kind}'")
    if points <= 0:
        raise bad_request("points must be a positive integer")

    _ensure_tables(dal)
    config = await get_config(async_dal, dal, community_id=community_id)
    if not config.enabled:
        raise conflict("loyalty is disabled here")

    loop = asyncio.get_event_loop()
    now = datetime.now(UTC)
    row, applied_delta = await loop.run_in_executor(
        async_dal.executor,
        lambda: _sync_earn(
            dal,
            community_id=community_id,
            platform=platform,
            platform_user_id=platform_user_id,
            kind=kind,
            points=points,
            ref=ref,
            now=now,
        ),
    )
    logger.debug(
        "loyalty.earn community_id=%s platform=%s kind=%s points=%s applied=%s",
        community_id,
        platform,
        kind,
        points,
        applied_delta,
    )
    return EarnResultDTO(
        balance=_balance_dto(community_id, platform, platform_user_id, row),
        applied_delta=applied_delta,
    )


# ---------------------------------------------------------------------------
# Adjust (admin/mod add or remove)
# ---------------------------------------------------------------------------


def _sync_adjust(
    dal: Any,
    *,
    community_id: int,
    platform: str,
    platform_user_id: str,
    delta: int,
    actor: str | None,
    note: str | None,
    allow_negative: bool,
    now: datetime,
) -> Any:
    for_update = _for_update(dal)
    try:
        row = _sync_get_or_create_balance_row(
            dal,
            community_id=community_id,
            platform=platform,
            platform_user_id=platform_user_id,
            for_update=for_update,
        )
        current = int(row.balance)
        new_balance = current + delta
        if new_balance < 0:
            if not allow_negative:
                raise conflict("insufficient points")
            new_balance = 0
        applied_delta = new_balance - current
        earned_delta = max(0, applied_delta)
        spent_delta = max(0, -applied_delta)

        dal(dal.loyalty_balances.id == row.id).update(
            balance=new_balance,
            lifetime_earned=int(row.lifetime_earned) + earned_delta,
            lifetime_spent=int(row.lifetime_spent) + spent_delta,
            updated_at=now,
        )
        dal.loyalty_transactions.insert(
            community_id=community_id,
            platform=platform,
            platform_user_id=platform_user_id,
            delta=applied_delta,
            balance_after=new_balance,
            kind=_KIND_ADJUST,
            ref=note,
            actor_platform_user_id=actor,
            created_at=now,
        )
        refreshed = dal(dal.loyalty_balances.id == row.id).select().first()
        dal.commit()
        return refreshed
    except Exception:
        dal.rollback()
        raise


async def adjust(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    platform: str,
    platform_user_id: str,
    delta: int,
    actor: str | None,
    note: str | None = None,
    allow_negative: bool = False,
) -> BalanceDTO:
    """Admin/mod add-or-remove points -- `delta` may be negative.

    `allow_negative=False` (the default) rejects a removal that would push
    the balance below zero with `insufficient points`; `allow_negative=True`
    floors the result at 0 instead of rejecting.
    """
    if delta == 0:
        raise bad_request("delta must be non-zero")

    _ensure_tables(dal)
    config = await get_config(async_dal, dal, community_id=community_id)
    loop = asyncio.get_event_loop()
    now = datetime.now(UTC)
    row = await loop.run_in_executor(
        async_dal.executor,
        lambda: _sync_adjust(
            dal,
            community_id=community_id,
            platform=platform,
            platform_user_id=platform_user_id,
            delta=delta,
            actor=actor,
            note=note,
            allow_negative=allow_negative,
            now=now,
        ),
    )
    logger.debug(
        "loyalty.adjust community_id=%s platform=%s delta=%s actor=%s",
        community_id,
        platform,
        delta,
        actor,
    )
    return _balance_dto(
        community_id,
        platform,
        platform_user_id,
        row,
        currency_name=config.currency_name,
        currency_symbol=config.currency_symbol,
    )


# ---------------------------------------------------------------------------
# Leaderboard
# ---------------------------------------------------------------------------


async def leaderboard(
    async_dal: Any, dal: Any, *, community_id: int, limit: int = 10
) -> list[LeaderboardEntryDTO]:
    """Top `limit` balances for a community, highest first."""
    _ensure_tables(dal)
    clamped_limit = max(1, min(int(limit), _MAX_LEADERBOARD_LIMIT))
    rows = await async_dal.select_async(
        dal(dal.loyalty_balances.community_id == community_id),
        orderby=~dal.loyalty_balances.balance,
        limitby=(0, clamped_limit),
    )
    uuids = _member_uuid_map(dal, community_id, rows)
    return [
        LeaderboardEntryDTO(
            platform=row.platform,
            platform_user_id=row.platform_user_id,
            balance=int(row.balance),
            user_uuid=uuids.get((row.platform, row.platform_user_id)),
        )
        for row in rows
    ]


def _member_uuid_map(dal: Any, community_id: int, rows: Any) -> dict[tuple[str, str], str]:
    """`(platform, platform_user_id) -> user_uuid` for the leaderboard rows' members."""
    pairs = {(row.platform, row.platform_user_id) for row in rows}
    if not pairs:
        return {}
    members = dal.community_members
    found = dal(members.community_id == community_id).select(
        members.platform, members.platform_user_id, members.user_uuid
    )
    return {
        (m.platform, m.platform_user_id): str(m.user_uuid)
        for m in found
        if m.user_uuid and (m.platform, m.platform_user_id) in pairs
    }


def resolve_member_identity(
    dal: Any, *, community_id: int, user_uuid: str, platform: str
) -> str | None:
    """`platform_user_id` of the member `user_uuid` ON `platform` in THIS community, else `None`.

    Scoped by `community_id`, so an unknown UUID and a UUID that belongs to another community
    are indistinguishable to the caller (both 404) -- never an existence oracle.
    """
    members = dal.community_members
    row = (
        dal(
            (members.community_id == community_id)
            & (members.user_uuid == user_uuid)
            & (members.platform == platform)
        )
        .select(members.platform_user_id)
        .first()
    )
    return str(row.platform_user_id) if row is not None else None


# ---------------------------------------------------------------------------
# Shop items
# ---------------------------------------------------------------------------


async def list_items(
    async_dal: Any, dal: Any, *, community_id: int, enabled_only: bool = False
) -> list[ShopItemDTO]:
    """List a community's shop items, optionally filtered to `enabled=True` only."""
    _ensure_tables(dal)
    query = dal.loyalty_shop_items.community_id == community_id
    if enabled_only:
        query &= dal.loyalty_shop_items.enabled == True  # noqa: E712 - pydal idiom
    rows = await async_dal.select_async(dal(query), orderby=dal.loyalty_shop_items.sku)
    return [_item_dto(row) for row in rows]


async def upsert_item(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    sku: str,
    name: str,
    description: str | None = None,
    cost: int,
    stock: int | None = None,
    enabled: bool = True,
    requires_mod_approval: bool = False,
) -> ShopItemDTO:
    """Create or update (by `(community_id, sku)`) a shop item."""
    if not sku or not sku.strip():
        raise bad_request("sku is required")
    if not name or not name.strip():
        raise bad_request("name is required")
    if cost < 0:
        raise bad_request("cost must be >= 0")
    if stock is not None and stock < 0:
        raise bad_request("stock must be >= 0")

    _ensure_tables(dal)
    now = datetime.now(UTC)
    query = (dal.loyalty_shop_items.community_id == community_id) & (
        dal.loyalty_shop_items.sku == sku
    )
    rows = await async_dal.select_async(dal(query))
    if rows:
        existing = rows.first()
        await async_dal.update_async(
            dal.loyalty_shop_items.id == existing.id,
            name=name,
            description=description,
            cost=cost,
            stock=stock,
            enabled=enabled,
            requires_mod_approval=requires_mod_approval,
            updated_at=now,
        )
        refreshed = await async_dal.select_async(dal(dal.loyalty_shop_items.id == existing.id))
        logger.debug("loyalty.item.updated community_id=%s sku=%s", community_id, sku)
        return _item_dto(refreshed.first())

    new_id = await async_dal.insert_async(
        dal.loyalty_shop_items,
        community_id=community_id,
        sku=sku,
        name=name,
        description=description,
        cost=cost,
        stock=stock,
        enabled=enabled,
        requires_mod_approval=requires_mod_approval,
        created_at=now,
        updated_at=now,
    )
    rows = await async_dal.select_async(dal(dal.loyalty_shop_items.id == int(new_id)))
    logger.debug("loyalty.item.created community_id=%s sku=%s", community_id, sku)
    return _item_dto(rows.first())


# ---------------------------------------------------------------------------
# Redeem / refund
# ---------------------------------------------------------------------------


def _sync_redeem(
    dal: Any, *, community_id: int, platform: str, platform_user_id: str, sku: str, now: datetime
) -> tuple[Any, str]:
    """Single executor job: lock item + balance rows, validate, debit, decrement stock, commit.

    Error messages here are pinned strings -- `blueprints/v1/
    community_loyalty.py`'s internal `/loyalty/redeem` route relays
    `ApiError.message` verbatim to chat, so wording changes here are a
    user-facing change, not just a log line.

    Returns `(redemption_row, item_name)` tuple.
    """
    for_update = _for_update(dal)
    try:
        config_row = (
            dal(dal.loyalty_config.community_id == community_id)
            .select(for_update=for_update)
            .first()
        )
        enabled = bool(config_row.enabled) if config_row is not None else True
        if not enabled:
            raise conflict("loyalty is disabled here")

        item_row = (
            dal(
                (dal.loyalty_shop_items.community_id == community_id)
                & (dal.loyalty_shop_items.sku == sku)
            )
            .select(for_update=for_update)
            .first()
        )
        if item_row is None or not bool(item_row.enabled):
            raise ApiError(f"unknown item '{sku}'", 409, "UNKNOWN_ITEM")
        if item_row.stock is not None and int(item_row.stock) <= 0:
            raise conflict("item out of stock")

        balance_row = _sync_get_or_create_balance_row(
            dal,
            community_id=community_id,
            platform=platform,
            platform_user_id=platform_user_id,
            for_update=for_update,
        )
        current = int(balance_row.balance)
        cost = int(item_row.cost)
        if current < cost:
            raise conflict(f"not enough points (have {current}, need {cost})")

        new_balance = current - cost
        dal(dal.loyalty_balances.id == balance_row.id).update(
            balance=new_balance,
            lifetime_spent=int(balance_row.lifetime_spent) + cost,
            updated_at=now,
        )
        if item_row.stock is not None:
            dal(dal.loyalty_shop_items.id == item_row.id).update(
                stock=int(item_row.stock) - 1, updated_at=now
            )

        dal.loyalty_transactions.insert(
            community_id=community_id,
            platform=platform,
            platform_user_id=platform_user_id,
            delta=-cost,
            balance_after=new_balance,
            kind=_KIND_REDEEM,
            ref=sku,
            actor_platform_user_id=platform_user_id,
            created_at=now,
        )
        status = "pending" if bool(item_row.requires_mod_approval) else "fulfilled"
        redemption_id = dal.loyalty_redemptions.insert(
            community_id=community_id,
            item_id=item_row.id,
            platform=platform,
            platform_user_id=platform_user_id,
            cost=cost,
            status=status,
            note=None,
            created_at=now,
            fulfilled_at=now if status == "fulfilled" else None,
            fulfilled_by=None,
        )
        redemption_row = dal(dal.loyalty_redemptions.id == redemption_id).select().first()
        dal.commit()
        logger.debug(
            "loyalty.redeem community_id=%s platform=%s sku=%s status=%s balance=%s",
            community_id,
            platform,
            sku,
            status,
            new_balance,
        )
        return redemption_row, str(item_row.name)
    except Exception:
        dal.rollback()
        raise


async def redeem(
    async_dal: Any, dal: Any, *, community_id: int, platform: str, platform_user_id: str, sku: str
) -> RedemptionDTO:
    """Atomically redeem one shop item: lock balance + item, check, debit, decrement stock.

    Inserts a `pending` redemption if the item `requires_mod_approval`,
    else `fulfilled` immediately. See `_sync_redeem()`'s own docstring for
    why its raised error messages are pinned wording, not just logs.
    """
    if not sku or not sku.strip():
        raise bad_request("sku is required")

    _ensure_tables(dal)
    config = await get_config(async_dal, dal, community_id=community_id)
    loop = asyncio.get_event_loop()
    now = datetime.now(UTC)
    row, item_name = await loop.run_in_executor(
        async_dal.executor,
        lambda: _sync_redeem(
            dal,
            community_id=community_id,
            platform=platform,
            platform_user_id=platform_user_id,
            sku=sku,
            now=now,
        ),
    )
    return _redemption_dto(
        row,
        item_name=item_name,
        currency_name=config.currency_name,
        currency_symbol=config.currency_symbol,
    )


def _sync_refund(dal: Any, *, community_id: int, redemption_id: int, now: datetime) -> Any:
    for_update = _for_update(dal)
    try:
        redemption_row = (
            dal(
                (dal.loyalty_redemptions.id == redemption_id)
                & (dal.loyalty_redemptions.community_id == community_id)
            )
            .select(for_update=for_update)
            .first()
        )
        if redemption_row is None:
            raise not_found(f"redemption {redemption_id} not found")
        if redemption_row.status == "refunded":
            raise conflict("redemption already refunded")

        balance_row = _sync_get_or_create_balance_row(
            dal,
            community_id=community_id,
            platform=redemption_row.platform,
            platform_user_id=redemption_row.platform_user_id,
            for_update=for_update,
        )
        cost = int(redemption_row.cost)
        new_balance = int(balance_row.balance) + cost
        dal(dal.loyalty_balances.id == balance_row.id).update(
            balance=new_balance,
            lifetime_spent=max(0, int(balance_row.lifetime_spent) - cost),
            updated_at=now,
        )
        dal.loyalty_transactions.insert(
            community_id=community_id,
            platform=redemption_row.platform,
            platform_user_id=redemption_row.platform_user_id,
            delta=cost,
            balance_after=new_balance,
            kind=_KIND_REFUND,
            ref=str(redemption_id),
            actor_platform_user_id=None,
            created_at=now,
        )
        dal(dal.loyalty_redemptions.id == redemption_id).update(status="refunded")

        item_row = (
            dal(dal.loyalty_shop_items.id == redemption_row.item_id)
            .select(for_update=for_update)
            .first()
        )
        if item_row is not None and item_row.stock is not None:
            dal(dal.loyalty_shop_items.id == item_row.id).update(
                stock=int(item_row.stock) + 1, updated_at=now
            )

        refreshed = dal(dal.loyalty_redemptions.id == redemption_id).select().first()
        dal.commit()
        return refreshed
    except Exception:
        dal.rollback()
        raise


async def refund(
    async_dal: Any, dal: Any, *, community_id: int, redemption_id: int
) -> RedemptionDTO:
    """Reverse one redemption: refund the cost, restock (if tracked), mark `refunded`.

    Rejects an already-`refunded` redemption (`conflict`) rather than
    double-crediting the balance.
    """
    _ensure_tables(dal)
    loop = asyncio.get_event_loop()
    now = datetime.now(UTC)
    row = await loop.run_in_executor(
        async_dal.executor,
        lambda: _sync_refund(dal, community_id=community_id, redemption_id=redemption_id, now=now),
    )
    logger.debug("loyalty.refund community_id=%s redemption_id=%s", community_id, redemption_id)
    return _redemption_dto(row)


# ---------------------------------------------------------------------------
# Stats / wipe -- back the admin `GET .../stats` and `POST .../wipe` routes.
# ---------------------------------------------------------------------------


async def get_stats(async_dal: Any, dal: Any, *, community_id: int) -> StatsDTO:
    """Community-wide currency stats: user count, total currency in circulation, average balance."""
    _ensure_tables(dal)
    rows = await async_dal.select_async(dal(dal.loyalty_balances.community_id == community_id))
    balances = [int(row.balance) for row in rows]
    total_users = len(balances)
    total_currency = sum(balances)
    average_balance = round(total_currency / total_users, 2) if total_users else 0.0
    return StatsDTO(
        community_id=community_id,
        total_users=total_users,
        total_currency=total_currency,
        average_balance=average_balance,
    )


def _sync_wipe(dal: Any, *, community_id: int, now: datetime) -> int:
    """Zero every non-zero balance in the community, ledgering each as a `wipe` transaction."""
    for_update = _for_update(dal)
    try:
        rows = dal(dal.loyalty_balances.community_id == community_id).select(for_update=for_update)
        affected = 0
        for row in rows:
            current = int(row.balance)
            if current == 0:
                continue
            dal(dal.loyalty_balances.id == row.id).update(balance=0, updated_at=now)
            dal.loyalty_transactions.insert(
                community_id=community_id,
                platform=row.platform,
                platform_user_id=row.platform_user_id,
                delta=-current,
                balance_after=0,
                kind=_KIND_WIPE,
                ref=None,
                actor_platform_user_id=None,
                created_at=now,
            )
            affected += 1
        dal.commit()
        return affected
    except Exception:
        dal.rollback()
        raise


async def wipe(async_dal: Any, dal: Any, *, community_id: int) -> int:
    """Zero every balance in the community; returns the number of balances actually changed."""
    _ensure_tables(dal)
    loop = asyncio.get_event_loop()
    now = datetime.now(UTC)
    affected = await loop.run_in_executor(
        async_dal.executor, lambda: _sync_wipe(dal, community_id=community_id, now=now)
    )
    logger.debug("loyalty.wipe community_id=%s affected=%s", community_id, affected)
    return affected
