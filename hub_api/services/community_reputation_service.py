"""Community reputation service -- single store = the bundle store (alembic 0096).

Backs the member-facing reads (`GET .../reputation/me`, `.../leaderboard`), the admin
reputation-config / at-risk / history routes and the admin score override. Everything lives in
`bundle_reputation_*`:

- scores: `bundle_reputation_scores` (community) and `bundle_reputation_tenant_scores` (the
  tenant aggregate; a caller reads ONLY their own row);
- policy: `bundle_reputation_policy` (community) and `bundle_reputation_tenant_policy` (tenant),
  each with its own auto-ban floor / at-risk threshold -- independent of each other;
- writes: only through the Postgres `reputation_apply()` function (row lock, clamp 300-850,
  idempotent ledger insert) -- never an UPDATE from here, so the ledger cannot drift from the
  score. Admin overrides call it with the admin's `hub_users.uuid` as actor (never the numeric id).

Auto-ban enforcement is NOT here: this module stores the policy (default OFF); the accrual
bundle reads it and enforces behind the `waddles.reputation-auto-ban` flag.

The tier table is `flask_core.reputation_tiers` (shared with the `!rep` reply). Tables whose
schema has no `id` column are always read with explicit fields (pydal would otherwise SELECT a
nonexistent `id`).
"""

from __future__ import annotations

import logging
import uuid as uuid_mod
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import IntEnum
from typing import Any

from flask_core.reputation_tiers import REPUTATION_TIERS, reputation_tier

from services.errors import ApiError, bad_request, conflict, forbidden, not_found
from services.schema import (
    bind_auth_tables,
    bind_erasure_reputation_tables,
    bind_reputation_policy_tables,
)

logger = logging.getLogger(__name__)

__all__ = [
    "REPUTATION_TIERS",
    "ApplyResult",
    "SoftBanEntryDTO",
    "SoftBanReason",
    "clear_soft_ban",
    "list_soft_bans",
    "set_soft_ban",
    "AtRiskEntryDTO",
    "HistoryEntryDTO",
    "LeaderboardEntryDTO",
    "MyReputationDTO",
    "ReasonClass",
    "ReasonCode",
    "apply_reputation",
    "get_at_risk",
    "get_community_config",
    "get_history",
    "get_leaderboard",
    "get_my_reputation",
    "get_tenant_config",
    "override_member_reputation",
    "reputation_tier",
    "set_community_config",
    "set_tenant_config",
]

#: Product scale (user decision: legacy FICO-style 300-850, start 600). The DB function reads the
#: per-community policy row; these are the defaults when no row exists.
SCALE_MIN = 300
SCALE_MAX = 850
DEFAULT_SCORE = 600
DEFAULT_SOFT_BAN_FLOOR = 550
DEFAULT_HARD_BAN_FLOOR = 400
DEFAULT_AT_RISK_THRESHOLD = 600
DEFAULT_TENANT_AUTO_BAN_FLOOR = 350
DEFAULT_TENANT_AT_RISK_THRESHOLD = 450

#: Hard ceiling for list endpoints -- matches `services/pagination.py::DEFAULT_MAX_PAGE_SIZE`.
_MAX_LIMIT = 100


class SoftBanReason(IntEnum):
    """`bundle_reputation_soft_bans.reason_code`: set by the score floor or manually by an admin."""

    FLOOR = 1
    MANUAL = 2


class ReasonClass(IntEnum):
    """`reputation_apply(p_reason_class)` values.

    1 activity (capped), 2 moderation (capped, negative-only), 3 admin_override (uncapped;
    hub-api / superuser sessions only).
    """

    ACTIVITY = 1
    MODERATION = 2
    ADMIN_OVERRIDE = 3


class ReasonCode(IntEnum):
    """Numeric ledger `reason_code_id` (smallint) for hub-originated writes; bundles use 1-99."""

    ADMIN_ADJUST = 100
    ADMIN_OVERRIDE = 101


@dataclass(slots=True, frozen=True)
class MyReputationDTO:
    """`GET .../reputation/me` response payload -- both scoring tiers, each with a label."""

    community_score: int
    community_tier: str
    tenant_score: int
    tenant_tier: str
    total_events: int
    last_event_at: str | None


@dataclass(slots=True, frozen=True)
class LeaderboardEntryDTO:
    """One `GET .../reputation/leaderboard` row -- display name only, no ids/emails."""

    display_name: str
    score: int
    tier: str


@dataclass(slots=True, frozen=True)
class AtRiskEntryDTO:
    """One at-risk member (score below the community's at-risk threshold)."""

    userId: str | None
    displayName: str
    score: int
    tier: str
    belowSoftBanFloor: bool
    belowHardBanFloor: bool


@dataclass(slots=True, frozen=True)
class HistoryEntryDTO:
    """One ledger row, safe for an admin view: actor is a category, never an internal id."""

    id: int
    userUuid: str
    delta: int
    balanceAfter: int | None
    kind: str
    reasonCode: int | None
    actorType: str
    sourceEvent: str | None
    occurredAt: str | None


@dataclass(slots=True, frozen=True)
class SoftBanEntryDTO:
    """One active soft ban: the member cannot WIN anything in this community (not removed)."""

    userId: str | None
    displayName: str
    reason: str
    setByType: str
    setAt: str | None
    expiresAt: str | None


@dataclass(slots=True, frozen=True)
class ApplyResult:
    """Outcome of `reputation_apply()`: the new community score and whether it was newly applied."""

    new_score: int
    applied: bool


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


async def _admin_actor(async_dal: Any, dal: Any, actor_user_id: int | None) -> str:
    """The acting admin's `hub_users.uuid` string (the only human actor form SQL accepts).

    Never the numeric id; fails closed (403) when it cannot be resolved.
    """
    if actor_user_id is None:
        raise forbidden("A signed-in admin is required for this action")
    users = dal.hub_users
    rows = await async_dal.select_async(dal(users.id == actor_user_id), users.uuid)
    row = rows.first() if rows else None
    if row is None or not row.uuid:
        raise forbidden("Acting admin has no identity UUID")
    return str(row.uuid)


def _int_literal(dal: Any, value: int) -> Any:
    """A typed integer SQL literal (`int()`-coerced, so injection-safe) for COALESCE/compare.

    pydal renders a plain Python int next to a `bigint` field as a quoted string, which sqlite
    then orders as text; an integer-typed expression keeps the comparison numeric everywhere.
    """
    from pydal.objects import Expression

    return Expression(dal, str(int(value)), None, None, "integer")


def _actor_type(actor: str | None) -> str:
    """Category of a stored actor for admin views: `system` (`system:*`), `admin` (a UUID)."""
    if not actor:
        return "unknown"
    if actor.startswith("system:"):
        return "system"
    try:
        uuid_mod.UUID(actor)
    except ValueError:
        # Stored actors are `system:*` or a UUID; anything else is legacy/corrupt data. The value
        # itself is not logged (it may be a pre-tokenization handle).
        logger.warning("reputation.actor_type.unrecognized_actor")
        return "unknown"
    return "admin"


def _translate_db_error(exc: Exception) -> Exception:
    """Map the SQL functions' typed RAISEs onto API errors; anything else is returned unchanged."""
    text = str(exc)
    if "not_a_member" in text:
        return not_found("Member not found")
    if "daily_cap_exceeded" in text or "quota_exceeded" in text:
        return ApiError("Reputation rate limit reached; try again later", 429, "RATE_LIMITED")
    if "invalid_input" in text or "floor_not_breached" in text:
        return bad_request("Invalid reputation request")
    return exc


def _ensure_reputation_tables(dal: Any, *, migrate: bool = False) -> None:
    """Idempotently bind `community_members` + every `bundle_reputation_*` table.

    Safe to call every request (each binder no-ops once bound). `migrate` is `False` in
    production (schema owned by alembic 0050/0072/0096); tests pass `migrate=True` against a
    throwaway sqlite DAL, same convention as every `bind_*` in `services/schema.py`.
    """
    bind_auth_tables(dal, migrate=migrate)
    bind_erasure_reputation_tables(dal, migrate=migrate)
    bind_reputation_policy_tables(dal, migrate=migrate)


async def _member_uuid(
    async_dal: Any, dal: Any, *, community_id: int, hub_user_id: int
) -> str | None:
    """The member's PII-free `user_uuid` token in this community, or None (never linked yet)."""
    rows = await async_dal.select_async(
        dal(
            (dal.community_members.community_id == community_id)
            & (dal.community_members.user_id == str(hub_user_id))
        ),
        dal.community_members.user_uuid,
    )
    row = rows.first() if rows else None
    return str(row.user_uuid) if row is not None and row.user_uuid else None


async def _policy(async_dal: Any, dal: Any, *, community_id: int) -> dict[str, Any]:
    """The community policy row as a dict, defaults filled when no row exists."""
    pol = dal.bundle_reputation_policy
    rows = await async_dal.select_async(
        dal(pol.community_id == community_id),
        pol.score_min,
        pol.score_max,
        pol.score_default,
        pol.soft_ban_enabled,
        pol.soft_ban_floor,
        pol.hard_ban_enabled,
        pol.hard_ban_floor,
        pol.at_risk_threshold,
        pol.updated_at,
    )
    row = rows.first() if rows else None
    if row is None:
        return {
            "score_min": SCALE_MIN,
            "score_max": SCALE_MAX,
            "score_default": DEFAULT_SCORE,
            "soft_ban_enabled": False,
            "soft_ban_floor": DEFAULT_SOFT_BAN_FLOOR,
            "hard_ban_enabled": False,
            "hard_ban_floor": DEFAULT_HARD_BAN_FLOOR,
            "at_risk_threshold": DEFAULT_AT_RISK_THRESHOLD,
            "updated_at": None,
        }
    return {
        "score_min": int(row.score_min),
        "score_max": int(row.score_max),
        "score_default": int(row.score_default),
        "soft_ban_enabled": bool(row.soft_ban_enabled),
        "soft_ban_floor": int(row.soft_ban_floor),
        "hard_ban_enabled": bool(row.hard_ban_enabled),
        "hard_ban_floor": int(row.hard_ban_floor),
        "at_risk_threshold": int(row.at_risk_threshold),
        "updated_at": row.updated_at,
    }


async def get_my_reputation(
    async_dal: Any, dal: Any, *, community_id: int, hub_user_id: int, tenant_id: int
) -> MyReputationDTO:
    """Caller's own community + tenant reputation, each defaulted to 600 (baseline) if unset.

    `tenant_id` MUST come from the caller's validated `TenantContext`. The tenant score is the
    caller's OWN aggregate row only (`tenant_id` + their own `user_uuid`) -- never another
    user's. A member not yet linked to a `user_uuid` looks like "everyone starts at 600".
    """
    _ensure_reputation_tables(dal)
    user_uuid = await _member_uuid(
        async_dal, dal, community_id=community_id, hub_user_id=hub_user_id
    )
    community_score = DEFAULT_SCORE
    tenant_score = DEFAULT_SCORE
    total_events = 0
    last_event_at: str | None = None
    if user_uuid is not None:
        scores = dal.bundle_reputation_scores
        rows = await async_dal.select_async(
            dal((scores.community_id == community_id) & (scores.user_uuid == user_uuid)),
            scores.balance,
        )
        row = rows.first() if rows else None
        if row is not None and row.balance is not None:
            community_score = int(row.balance)
        agg = dal.bundle_reputation_tenant_scores
        agg_rows = await async_dal.select_async(
            dal((agg.tenant_id == tenant_id) & (agg.user_uuid == user_uuid)),
            agg.score,
            agg.event_count,
            agg.last_event_at,
        )
        agg_row = agg_rows.first() if agg_rows else None
        if agg_row is not None:
            tenant_score = int(agg_row.score) if agg_row.score is not None else DEFAULT_SCORE
            total_events = int(agg_row.event_count or 0)
            last_event_at = _iso(agg_row.last_event_at)
    return MyReputationDTO(
        community_score=community_score,
        community_tier=reputation_tier(community_score),
        tenant_score=tenant_score,
        tenant_tier=reputation_tier(tenant_score),
        total_events=total_events,
        last_event_at=last_event_at,
    )


async def get_leaderboard(
    async_dal: Any, dal: Any, *, community_id: int, limit: int = 10
) -> list[LeaderboardEntryDTO]:
    """Top `limit` ACTIVE members of THIS community by score, highest first (community-only).

    Unscored members count at the policy default (COALESCE, 600 unless the policy says
    otherwise). Display-name + score + tier only -- never `user_id`/`user_uuid`/platform ids.
    """
    _ensure_reputation_tables(dal)
    clamped = max(1, min(int(limit), _MAX_LIMIT))
    policy = await _policy(async_dal, dal, community_id=community_id)
    scores = dal.bundle_reputation_scores
    members = dal.community_members
    balance = scores.balance.coalesce(_int_literal(dal, policy["score_default"]))
    rows = await async_dal.select_async(
        dal((members.community_id == community_id) & (members.is_active == True)),  # noqa: E712 - pydal idiom
        balance,
        members.display_name,
        left=scores.on(
            (scores.community_id == members.community_id) & (scores.user_uuid == members.user_uuid)
        ),
        orderby=~balance,
        limitby=(0, clamped),
    )
    entries = []
    for row in rows:
        score = int(row[balance])
        entries.append(
            LeaderboardEntryDTO(
                display_name=row.community_members.display_name or "unknown",
                score=score,
                tier=reputation_tier(score),
            )
        )
    return entries


async def apply_reputation(
    async_dal: Any,
    *,
    tenant_id: int,
    community_id: int,
    user_uuid: str,
    delta: int,
    reason_code: ReasonCode,
    actor: str,
    source_event: str,
    idempotency_key: str,
    reason_class: ReasonClass = ReasonClass.ACTIVITY,
    app_id: str | None = None,
) -> ApplyResult:
    """Apply a delta through the DB `reputation_apply()` function -- the only score write path.

    All validation, membership, caps, idempotency and clamping happen inside the function;
    `actor` must be a human admin's UUID string or `system:<name>`.
    """
    try:
        rows = await async_dal.executesql_async(
            "SELECT new_score, applied FROM reputation_apply(%s, %s, %s::uuid, %s, %s::smallint, "
            "%s::smallint, %s, %s, %s, %s)",
            [
                tenant_id,
                community_id,
                user_uuid,
                delta,
                int(reason_class),
                int(reason_code),
                app_id,
                actor,
                source_event,
                idempotency_key,
            ],
        )
    except Exception as exc:  # noqa: BLE001 - re-raised after typed translation
        translated = _translate_db_error(exc)
        if translated is exc:
            raise
        raise translated from exc
    if not rows:
        raise RuntimeError("reputation_apply returned no row")
    return ApplyResult(new_score=int(rows[0][0]), applied=bool(rows[0][1]))


async def override_member_reputation(
    async_dal: Any,
    dal: Any,
    *,
    tenant_id: int,
    community_id: int,
    user_id: int,
    amount: float | None,
    set_to: float | None,
    actor_user_id: int | None,
) -> tuple[int, int | None]:
    """Admin adjust (`amount`) or set (`set_to`) of a member's score. Returns (new, change|None).

    Resolves the member's `user_uuid`, computes the delta against the current stored score
    (policy default when none) and applies it via `reputation_apply()` (class admin_override)
    with the admin's UUID as actor.
    """
    _ensure_reputation_tables(dal)
    uuid = await _member_uuid(async_dal, dal, community_id=community_id, hub_user_id=user_id)
    if uuid is None:
        raise conflict("Member has no identity token yet; reputation cannot be applied")
    policy = await _policy(async_dal, dal, community_id=community_id)
    scores = dal.bundle_reputation_scores
    cur_rows = await async_dal.select_async(
        dal((scores.community_id == community_id) & (scores.user_uuid == uuid)), scores.balance
    )
    cur_row = cur_rows.first() if cur_rows else None
    current = int(cur_row.balance) if cur_row is not None else int(policy["score_default"])
    actor = await _admin_actor(async_dal, dal, actor_user_id)
    if set_to is not None:
        target = int(max(policy["score_min"], min(policy["score_max"], set_to)))
        result = await apply_reputation(
            async_dal,
            tenant_id=tenant_id,
            community_id=community_id,
            user_uuid=uuid,
            delta=target - current,
            reason_code=ReasonCode.ADMIN_OVERRIDE,
            actor=actor,
            source_event="admin.reputation.set",
            idempotency_key=f"admin.{uuid_mod.uuid4().hex}",
            reason_class=ReasonClass.ADMIN_OVERRIDE,
        )
        return result.new_score, None
    if amount is None or amount == 0:
        raise bad_request("Invalid amount - provide amount (relative) or setTo (absolute)")
    result = await apply_reputation(
        async_dal,
        tenant_id=tenant_id,
        community_id=community_id,
        user_uuid=uuid,
        delta=int(amount),
        reason_code=ReasonCode.ADMIN_ADJUST,
        actor=actor,
        source_event="admin.reputation.adjust",
        idempotency_key=f"admin.{uuid_mod.uuid4().hex}",
        reason_class=ReasonClass.ADMIN_OVERRIDE,
    )
    return result.new_score, result.new_score - current


def _validate_thresholds(
    floor: Any, at_risk: Any, *, lo: int = SCALE_MIN, hi: int = SCALE_MAX
) -> None:
    """Raise 400 unless both are ints within [lo, hi] and floor < at-risk threshold."""
    for label, value in (("auto-ban floor", floor), ("at-risk threshold", at_risk)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise bad_request(f"Invalid {label}: must be an integer")
        if not lo <= value <= hi:
            raise bad_request(f"Invalid {label}: must be within {lo}-{hi}")
    if floor >= at_risk:
        raise bad_request("Auto-ban floor must be below the at-risk threshold")


def _validate_community_floors(hard: Any, soft: Any, at_risk: Any, *, lo: int, hi: int) -> None:
    """Raise 400 unless all are ints in [lo, hi] and hard floor < soft floor < at-risk threshold."""
    for label, value in (
        ("hard-ban floor", hard),
        ("soft-ban floor", soft),
        ("at-risk threshold", at_risk),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise bad_request(f"Invalid {label}: must be an integer")
        if not lo <= value <= hi:
            raise bad_request(f"Invalid {label}: must be within {lo}-{hi}")
    if not hard < soft < at_risk:
        raise bad_request("Floors must satisfy hard-ban floor < soft-ban floor < at-risk threshold")


async def get_community_config(async_dal: Any, dal: Any, *, community_id: int) -> dict[str, Any]:
    """Community reputation policy (defaults when unset): scale + auto-ban + at-risk."""
    _ensure_reputation_tables(dal)
    policy = await _policy(async_dal, dal, community_id=community_id)
    policy["updated_at"] = _iso(policy["updated_at"])
    return policy


async def set_community_config(
    async_dal: Any,
    dal: Any,
    *,
    tenant_id: int,
    community_id: int,
    soft_ban_enabled: bool | None,
    soft_ban_floor: int | None,
    hard_ban_enabled: bool | None,
    hard_ban_floor: int | None,
    at_risk_threshold: int | None,
) -> dict[str, Any]:
    """Validate + upsert the community policy; only supplied fields change. Returns the result.

    Both floors are independent and default OFF: score < soft floor -> soft ban (cannot WIN),
    score < hard floor -> ban in this community. Enforcement is the accrual bundle's, behind
    the `waddles.reputation-auto-ban` flag.
    """
    _ensure_reputation_tables(dal)
    current = await _policy(async_dal, dal, community_id=community_id)
    soft = current["soft_ban_floor"] if soft_ban_floor is None else soft_ban_floor
    hard = current["hard_ban_floor"] if hard_ban_floor is None else hard_ban_floor
    at_risk = current["at_risk_threshold"] if at_risk_threshold is None else at_risk_threshold
    _validate_community_floors(
        hard, soft, at_risk, lo=current["score_min"], hi=current["score_max"]
    )
    for label, flag in (("softBanEnabled", soft_ban_enabled), ("hardBanEnabled", hard_ban_enabled)):
        if flag is not None and not isinstance(flag, bool):
            raise bad_request(f"Invalid {label}: must be a boolean")
    pol = dal.bundle_reputation_policy
    fields = {
        "soft_ban_enabled": current["soft_ban_enabled"]
        if soft_ban_enabled is None
        else soft_ban_enabled,
        "soft_ban_floor": soft,
        "hard_ban_enabled": current["hard_ban_enabled"]
        if hard_ban_enabled is None
        else hard_ban_enabled,
        "hard_ban_floor": hard,
        "at_risk_threshold": at_risk,
        "updated_at": datetime.now(UTC),
    }
    exists = await async_dal.select_async(dal(pol.community_id == community_id), pol.community_id)
    if exists and exists.first() is not None:
        await async_dal.update_async(pol.community_id == community_id, **fields)
    else:
        await async_dal.insert_async(pol, tenant_id=tenant_id, community_id=community_id, **fields)
    return await get_community_config(async_dal, dal, community_id=community_id)


async def _tenant_policy(async_dal: Any, dal: Any, *, tenant_id: int) -> dict[str, Any]:
    """The tenant policy row as a dict, defaults filled when no row exists."""
    pol = dal.bundle_reputation_tenant_policy
    rows = await async_dal.select_async(
        dal(pol.tenant_id == tenant_id),
        pol.tenant_auto_ban_enabled,
        pol.tenant_auto_ban_floor,
        pol.tenant_at_risk_threshold,
        pol.updated_at,
    )
    row = rows.first() if rows else None
    if row is None:
        return {
            "tenant_auto_ban_enabled": False,
            "tenant_auto_ban_floor": DEFAULT_TENANT_AUTO_BAN_FLOOR,
            "tenant_at_risk_threshold": DEFAULT_TENANT_AT_RISK_THRESHOLD,
            "updated_at": None,
        }
    return {
        "tenant_auto_ban_enabled": bool(row.tenant_auto_ban_enabled),
        "tenant_auto_ban_floor": int(row.tenant_auto_ban_floor),
        "tenant_at_risk_threshold": int(row.tenant_at_risk_threshold),
        "updated_at": row.updated_at,
    }


async def get_tenant_config(async_dal: Any, dal: Any, *, tenant_id: int) -> dict[str, Any]:
    """Tenant reputation policy (defaults when unset), compared against the tenant aggregate."""
    _ensure_reputation_tables(dal)
    policy = await _tenant_policy(async_dal, dal, tenant_id=tenant_id)
    policy["updated_at"] = _iso(policy["updated_at"])
    return policy


async def set_tenant_config(
    async_dal: Any,
    dal: Any,
    *,
    tenant_id: int,
    auto_ban_enabled: bool | None,
    auto_ban_floor: int | None,
    at_risk_threshold: int | None,
) -> dict[str, Any]:
    """Validate + upsert the tenant policy (tenant admins only -- enforced by the route)."""
    _ensure_reputation_tables(dal)
    current = await _tenant_policy(async_dal, dal, tenant_id=tenant_id)
    floor = current["tenant_auto_ban_floor"] if auto_ban_floor is None else auto_ban_floor
    at_risk = (
        current["tenant_at_risk_threshold"] if at_risk_threshold is None else at_risk_threshold
    )
    _validate_thresholds(floor, at_risk)
    if auto_ban_enabled is not None and not isinstance(auto_ban_enabled, bool):
        raise bad_request("Invalid autoBanEnabled: must be a boolean")
    enabled = current["tenant_auto_ban_enabled"] if auto_ban_enabled is None else auto_ban_enabled
    pol = dal.bundle_reputation_tenant_policy
    fields = {
        "tenant_auto_ban_enabled": enabled,
        "tenant_auto_ban_floor": floor,
        "tenant_at_risk_threshold": at_risk,
        "updated_at": datetime.now(UTC),
    }
    exists = await async_dal.select_async(dal(pol.tenant_id == tenant_id), pol.tenant_id)
    if exists and exists.first() is not None:
        await async_dal.update_async(pol.tenant_id == tenant_id, **fields)
    else:
        await async_dal.insert_async(pol, tenant_id=tenant_id, **fields)
    return await get_tenant_config(async_dal, dal, tenant_id=tenant_id)


async def get_at_risk(
    async_dal: Any, dal: Any, *, community_id: int, limit: int = 50
) -> list[AtRiskEntryDTO]:
    """Active members scoring below the community's at-risk threshold, lowest first.

    Unscored members count at the policy default (COALESCE), so they are listed only if that
    default itself sits below the threshold.
    """
    _ensure_reputation_tables(dal)
    policy = await _policy(async_dal, dal, community_id=community_id)
    scores = dal.bundle_reputation_scores
    members = dal.community_members
    balance = scores.balance.coalesce(_int_literal(dal, policy["score_default"]))
    rows = await async_dal.select_async(
        dal(
            (members.community_id == community_id)
            & (members.is_active == True)  # noqa: E712 - pydal idiom
            & (balance < _int_literal(dal, policy["at_risk_threshold"]))
        ),
        balance,
        members.display_name,
        members.user_id,
        left=scores.on(
            (scores.community_id == members.community_id) & (scores.user_uuid == members.user_uuid)
        ),
        orderby=balance,
        limitby=(0, max(1, min(int(limit), _MAX_LIMIT))),
    )
    return [
        AtRiskEntryDTO(
            userId=row.community_members.user_id,
            displayName=row.community_members.display_name or "unknown",
            score=int(row[balance]),
            tier=reputation_tier(int(row[balance])),
            belowSoftBanFloor=int(row[balance]) < policy["soft_ban_floor"],
            belowHardBanFloor=int(row[balance]) < policy["hard_ban_floor"],
        )
        for row in rows
    ]


async def get_history(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    user_id: int | None,
    limit: int = 50,
    offset: int = 0,
) -> list[HistoryEntryDTO]:
    """Ledger rows for the community (newest first), optionally one member's (by hub user id)."""
    _ensure_reputation_tables(dal)
    ledger = dal.bundle_reputation_adjustments
    query = (ledger.community_id == community_id) & (ledger.scope == "community")
    if user_id is not None:
        uuid = await _member_uuid(async_dal, dal, community_id=community_id, hub_user_id=user_id)
        if uuid is None:
            raise not_found("Member not found")
        query &= ledger.target_user_uuid == uuid
    start = max(0, offset)
    rows = await async_dal.select_async(
        dal(query),
        ledger.id,
        ledger.target_user_uuid,
        ledger.delta,
        ledger.balance_after,
        ledger.kind,
        ledger.reason_code_id,
        ledger.actor,
        ledger.source_event,
        ledger.occurred_at,
        orderby=~ledger.id,
        limitby=(start, start + max(1, min(int(limit), _MAX_LIMIT))),
    )
    return [
        HistoryEntryDTO(
            id=int(r.id),
            userUuid=str(r.target_user_uuid),
            delta=int(r.delta),
            balanceAfter=None if r.balance_after is None else int(r.balance_after),
            kind=r.kind or "adjust",
            reasonCode=None if r.reason_code_id is None else int(r.reason_code_id),
            actorType=_actor_type(r.actor),
            sourceEvent=r.source_event,
            occurredAt=_iso(r.occurred_at),
        )
        for r in rows
    ]


async def list_soft_bans(
    async_dal: Any, dal: Any, *, community_id: int, limit: int = 50
) -> list[SoftBanEntryDTO]:
    """Active (unexpired) soft bans in the community, newest first."""
    _ensure_reputation_tables(dal)
    bans = dal.bundle_reputation_soft_bans
    members = dal.community_members
    now = datetime.now(UTC).replace(tzinfo=None)
    rows = await async_dal.select_async(
        dal(
            (bans.community_id == community_id)
            & ((bans.expires_at == None) | (bans.expires_at > now))  # noqa: E711 - pydal idiom
            & (members.community_id == community_id)
            & (members.user_uuid == bans.user_uuid)
        ),
        bans.reason_code,
        bans.set_by,
        bans.set_at,
        bans.expires_at,
        members.user_id,
        members.display_name,
        orderby=~bans.set_at,
        limitby=(0, max(1, min(int(limit), _MAX_LIMIT))),
    )
    return [
        SoftBanEntryDTO(
            userId=r.community_members.user_id,
            displayName=r.community_members.display_name or "unknown",
            reason=SoftBanReason(int(r.bundle_reputation_soft_bans.reason_code)).name.lower(),
            setByType=_actor_type(r.bundle_reputation_soft_bans.set_by),
            setAt=_iso(r.bundle_reputation_soft_bans.set_at),
            expiresAt=_iso(r.bundle_reputation_soft_bans.expires_at),
        )
        for r in rows
    ]


async def set_soft_ban(
    async_dal: Any,
    dal: Any,
    *,
    tenant_id: int,
    community_id: int,
    user_id: int,
    expires_at: datetime | None,
    actor_user_id: int | None,
) -> None:
    """Manually soft-ban a member (cannot WIN in this community; stays a member)."""
    _ensure_reputation_tables(dal)
    if expires_at is not None and expires_at <= datetime.now(UTC):
        raise bad_request("expiresAt must be in the future")
    uuid = await _member_uuid(async_dal, dal, community_id=community_id, hub_user_id=user_id)
    if uuid is None:
        raise not_found("Member not found")
    actor = await _admin_actor(async_dal, dal, actor_user_id)
    try:
        await async_dal.executesql_async(
            "SELECT reputation_set_soft_ban(%s, %s, %s::uuid, %s::smallint, %s, %s)",
            [tenant_id, community_id, uuid, int(SoftBanReason.MANUAL), actor, expires_at],
        )
    except Exception as exc:  # noqa: BLE001 - re-raised after typed translation
        translated = _translate_db_error(exc)
        if translated is exc:
            raise
        raise translated from exc


async def clear_soft_ban(
    async_dal: Any,
    dal: Any,
    *,
    tenant_id: int,
    community_id: int,
    user_id: int,
    actor_user_id: int | None,
) -> bool:
    """Clear a member's soft ban (floor- or manually-set). Returns whether one existed."""
    _ensure_reputation_tables(dal)
    uuid = await _member_uuid(async_dal, dal, community_id=community_id, hub_user_id=user_id)
    if uuid is None:
        raise not_found("Member not found")
    actor = await _admin_actor(async_dal, dal, actor_user_id)
    try:
        rows = await async_dal.executesql_async(
            "SELECT reputation_clear_soft_ban(%s, %s, %s::uuid, %s, %s)",
            [tenant_id, community_id, uuid, actor, False],
        )
    except Exception as exc:  # noqa: BLE001 - re-raised after typed translation
        translated = _translate_db_error(exc)
        if translated is exc:
            raise
        raise translated from exc
    return bool(rows and rows[0][0])
