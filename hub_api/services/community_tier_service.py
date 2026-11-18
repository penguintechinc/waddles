"""Community tier allocation: Pro/Ent entitlements drawn from a tenant's team-axis license pool.

Model (critical-rules.md Licensing Model, team overage):

- A community is Free (no allocation row, consumes no pool slot, never blocked), Professional
  (code 2) or Enterprise (code 3). Pro/Ent are *allocated* from the tenant's pool.
- Effective tier = ``max(tenant tier, active allocation)``, failing closed to Free.
- Overage: a NEW Pro/Ent allocation beyond that tier's pool is BLOCKED (:class:`PoolExhausted`,
  402 later); an already-provisioned allocation is NEVER stripped -- shrinking the pool or
  downgrading the tenant only blocks new allocations. Super-admin ``operator_override`` is never
  blocked but still counts (reported as ``overage``).
- Staleness (REPORTING ONLY, automatic reclamation is NOT active): the licensing rule "30 days
  no-contact frees the slot" needs a real activity signal, and hub-api has none (community activity
  happens in the data plane, so :func:`touch_active` has no production caller). Until a real
  signal feeds :func:`touch_active`, EVERY unreleased allocation counts against the pool however
  old its ``last_active_at`` is; ``stale`` is informational only. A stale allocation keeps both its
  tier and its slot until the tenant admin or an operator releases it. Counting only fresh rows
  would let a tenant at its limit re-allocate a whole new pool every 30 days (pool cap defeat).
  The rule is enabled by wiring a real activity signal (deferred issue), not by a toggle.

Concurrency (same pattern as ``services/free_tier_caps.py``): the guarded section is ONE
synchronous executor job on one thread-local pydal connection: take
``pg_advisory_xact_lock(hashtext('team_pool:<tenant_id>:<tier>'))``, THEN re-read the stored pool
row (a fresh snapshot: a pool shrink committed after the async provider lookup is honored), THEN
count, THEN write, THEN commit. Racing allocators for the last slot serialize on the DB lock;
exactly one wins. No sync lock is ever held across an ``await``; the only awaits (pool lookup,
audit) are outside the job. Every executor job ends in commit or rollback (reads roll back) so no
connection idles in a transaction holding a stale snapshot.
A per-community lock is taken first (fixed order: community, then pool) so two concurrent changes
to one community cannot both pass.

Everything is keyed by NUMERIC ids (tenant/community/user), never slugs, except the one tenant
slug lookup :func:`resolve_effective_tier` needs to ask the entitlement client for the tenant tier.
``communities.license_tier`` / ``is_premium`` are denormalized copies written in the same
transaction as the ledger row.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal, Protocol

from flask_core.feature_flags import get_tier
from flask_core.tier_catalog import (
    TIER_ENTERPRISE,
    TIER_FREE,
    TIER_PROFESSIONAL,
    tier_level,
)

from services import community_tier_telemetry as telemetry
from services.audit_events import (
    ActorKind,
    AuditAction,
    AuditCategory,
    AuditEvent,
    AuditOutcome,
)
from services.free_tier_caps import _take_lock
from services.schema import bind_community_tier_tables

logger = logging.getLogger(__name__)

#: Tier codes. Free is implicit (no row); only 2/3 are ever stored.
TIER_CODE_FREE: Final[int] = 1
TIER_CODE_PROFESSIONAL: Final[int] = 2
TIER_CODE_ENTERPRISE: Final[int] = 3
TIER_NAMES: Final[dict[int, str]] = {
    TIER_CODE_FREE: TIER_FREE,
    TIER_CODE_PROFESSIONAL: TIER_PROFESSIONAL,
    TIER_CODE_ENTERPRISE: TIER_ENTERPRISE,
}
POOLED_TIER_CODES: Final[tuple[int, int]] = (TIER_CODE_PROFESSIONAL, TIER_CODE_ENTERPRISE)

#: ``tenant_license_pools.axis_code``: the team axis (nodes/seats are other axes, not stored here).
AXIS_TEAM: Final[int] = 3

#: ``community_tier_allocations.source_code``.
SOURCE_TENANT_ADMIN: Final[int] = 1
SOURCE_OPERATOR_OVERRIDE: Final[int] = 2
SOURCE_BACKFILL: Final[int] = 3
_ALLOCATION_SOURCES: Final[frozenset[int]] = frozenset(
    {SOURCE_TENANT_ADMIN, SOURCE_OPERATOR_OVERRIDE, SOURCE_BACKFILL}
)
_SOURCE_LABELS: Final[dict[int, str]] = {
    SOURCE_TENANT_ADMIN: "tenant_admin",
    SOURCE_OPERATOR_OVERRIDE: "operator_override",
    SOURCE_BACKFILL: "backfill",
}

#: ``community_tier_allocations.release_reason_code``.
RELEASE_DEALLOC: Final[int] = 1
RELEASE_TIER_CHANGE: Final[int] = 2
RELEASE_COMMUNITY_DELETED: Final[int] = 3

#: ``tenant_license_pools.source_code``.
POOL_SOURCE_LICENSE_SERVER: Final[int] = 1
POOL_SOURCE_OFFLINE: Final[int] = 2
POOL_SOURCE_OPERATOR_SET: Final[int] = 3
PoolSourceName = Literal["license_server", "offline", "operator_set"]
_POOL_SOURCE_BY_CODE: Final[dict[int, PoolSourceName]] = {
    POOL_SOURCE_LICENSE_SERVER: "license_server",
    POOL_SOURCE_OFFLINE: "offline",
    POOL_SOURCE_OPERATOR_SET: "operator_set",
}

#: Pool limit sentinel: ``None`` = tier not licensed, ``UNLIMITED`` = no cap.
UNLIMITED: Final[int] = -1

#: An allocation with no contact for this long stops counting against the pool.
STALE_AFTER: Final[timedelta] = timedelta(days=30)
#: ``last_active_at`` is only rewritten when older than this (cheap, bounded write rate).
TOUCH_MIN_INTERVAL: Final[timedelta] = timedelta(hours=1)
#: In-process short-circuit in front of the DB check; one dict entry per hot community.
_TOUCH_LOCAL_SECONDS: Final[float] = 300.0
_TOUCH_LOCAL_MAX: Final[int] = 10_000

_REASON_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9_.\-]{1,64}$")

_touch_seen: dict[int, float] = {}


class PoolExhausted(Exception):  # noqa: N818 - domain name; maps to HTTP 402 in the API layer
    """No free slot in the tenant's pool for ``required_tier`` (limit/used at decision time)."""

    def __init__(self, required_tier: int, limit: int, used: int) -> None:
        """Carry the tier code that was requested and the pool numbers that blocked it."""
        super().__init__(
            f"team pool exhausted for tier {TIER_NAMES.get(required_tier, required_tier)}: "
            f"{used} used of {limit}"
        )
        self.required_tier = required_tier
        self.limit = limit
        self.used = used


class PoolUnavailable(Exception):  # noqa: N818 - domain name; maps to HTTP 503 in the API layer
    """The pool limit could not be determined (never seen / no team axis / server error)."""


class CommunityNotInTenant(LookupError):  # noqa: N818 - domain name
    """The community does not exist or belongs to a different tenant (never allocate across)."""


@dataclass(slots=True, frozen=True)
class PoolInfo:
    """One tier's pool as known to a :class:`PoolProvider`.

    ``limit``: ``None`` = tier not licensed, ``-1`` = unlimited, ``>= 0`` = slot count.
    ``stale`` is informational (the lib's grace window); a provider that cannot serve the pool at
    all raises :class:`PoolUnavailable` instead.
    """

    limit: int | None
    observed_at: datetime
    stale: bool
    source: PoolSourceName


class PoolProvider(Protocol):
    """Source of the team-pool limit. Implemented by the license lib; DB-backed here."""

    async def get_pool(self, tenant_id: int, tier_code: int) -> PoolInfo:
        """Return the pool for ``(tenant_id, tier_code)`` or raise :class:`PoolUnavailable`."""
        ...


@dataclass(slots=True, frozen=True)
class PoolRow:
    """A ``tenant_license_pools`` row."""

    tenant_id: int
    tier_code: int
    limit_count: int | None
    observed_at: datetime
    source_code: int
    set_by: int | None


@dataclass(slots=True, frozen=True)
class AllocationResult:
    """Outcome of :func:`allocate`."""

    community_id: int
    tier_code: int
    previous_tier_code: int
    changed: bool
    allocation_public_id: str | None
    used: int
    limit: int | None
    overage: int
    #: Active (non-stale) count of the tier just vacated by a release/tier change, for the gauge.
    previous_used: int | None = None


@dataclass(slots=True, frozen=True)
class TierUsage:
    """Per-tier pool usage.

    ``used`` counts EVERY unreleased allocation; ``stale_excluded`` (legacy name, kept for API
    stability) is the informational count of those whose ``last_active_at`` is older than
    :data:`STALE_AFTER`. Stale rows are INCLUDED in ``used`` (no activity signal is wired yet).
    """

    tier_code: int
    used: int
    limit: int | None
    available: int | None
    overage: int
    stale_excluded: int
    licensed: bool
    unlimited: bool


def now_utc() -> datetime:
    """Current time, timezone-aware UTC (a single seam tests can monkeypatch)."""
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    """Treat a naive datetime (sqlite) as UTC."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _is_stale(row: Any, now: datetime) -> bool:
    last = _aware(row.last_active_at)
    return last is None or now - last > STALE_AFTER


def _compute_usage(used: int, stale: int, limit: int | None, tier_code: int) -> TierUsage:
    """Fold the active/stale counts of one tier + its limit into :class:`TierUsage`."""
    unlimited = limit == UNLIMITED
    licensed = limit is not None
    if limit is None:
        available: int | None = 0
        overage = used
    elif unlimited:
        available, overage = None, 0
    else:
        available, overage = max(0, limit - used), max(0, used - limit)
    return TierUsage(tier_code, used, limit, available, overage, stale, licensed, unlimited)


def _count_active(dal: Any, tenant_id: int, tier_code: int, now: datetime) -> tuple[int, int]:
    """SQL-side ``(used, stale)`` counts of a tier's unreleased allocations (no ledger load).

    ``used`` is EVERY unreleased row (stale ones keep their slot: no activity signal is wired, see
    the module docstring); ``stale`` is a subset count for reporting only.
    """
    t = dal.community_tier_allocations
    base = (t.tenant_id == tenant_id) & (t.tier_code == tier_code) & (t.released_at == None)  # noqa: E711
    total = int(dal(base).count())
    cutoff = now - STALE_AFTER
    # a NULL last_active_at never satisfies ">=", so it counts as stale (matches `_is_stale`)
    fresh = int(dal(base & (t.last_active_at >= cutoff)).count())
    return total, total - fresh


def _end_read(dal: Any) -> None:
    """Close the implicit read transaction (never leave a connection idle-in-transaction)."""
    try:
        dal.rollback()
    except Exception as exc:  # noqa: BLE001 - best effort; the next job starts clean anyway
        logger.warning("community_tier_end_read_failed error_type=%s", type(exc).__name__)


def _community_active_row(dal: Any, community_id: int) -> Any | None:
    t = dal.community_tier_allocations
    return dal((t.community_id == community_id) & (t.released_at == None)).select().first()  # noqa: E711


def _write_denormalized(dal: Any, community_id: int, tier_code: int) -> None:
    """Keep ``communities.license_tier`` / ``is_premium`` equal to the allocation (same txn)."""
    dal(dal.communities.id == community_id).update(
        license_tier=TIER_NAMES[tier_code], is_premium=tier_code >= TIER_CODE_PROFESSIONAL
    )


def _release(dal: Any, row: Any, *, actor: int | None, reason: int, now: datetime) -> None:
    dal(dal.community_tier_allocations.id == row.id).update(
        released_at=now, released_by=actor, release_reason_code=reason, updated_at=now
    )


def _validate(tier_code: int, source: int, reason: str | None) -> None:
    if tier_code not in TIER_NAMES:
        raise ValueError(f"unknown tier code {tier_code!r}")
    if source not in _ALLOCATION_SOURCES:
        raise ValueError(f"unknown allocation source {source!r}")
    if reason is not None and not _REASON_RE.match(reason):
        raise ValueError("reason must be a short snake_case token (no free text)")


def _read_pool_row(dal: Any, tenant_id: int, tier_code: int) -> PoolRow | None:
    """Read one team-axis pool row on the caller's connection (no transaction handling)."""
    t = dal.tenant_license_pools
    row = (
        dal((t.tenant_id == tenant_id) & (t.axis_code == AXIS_TEAM) & (t.tier_code == tier_code))
        .select()
        .first()
    )
    if row is None:
        return None
    return PoolRow(
        tenant_id=int(row.tenant_id),
        tier_code=int(row.tier_code),
        limit_count=None if row.limit_count is None else int(row.limit_count),
        observed_at=_aware(row.observed_at) or now_utc(),
        source_code=int(row.source_code),
        set_by=None if row.set_by is None else int(row.set_by),
    )


class PoolStore:
    """DB-backed ``tenant_license_pools`` accessor (the license lib's persistence layer)."""

    def __init__(self, async_dal: Any, dal: Any) -> None:
        """Bind to the shared pydal handle; tables are bound idempotently."""
        self._async_dal = async_dal
        self._dal = dal
        bind_community_tier_tables(dal)

    async def get(self, tenant_id: int, tier_code: int) -> PoolRow | None:
        """Return the stored team-axis pool row for ``(tenant_id, tier_code)`` or ``None``."""
        dal = self._dal

        def _job() -> PoolRow | None:
            try:
                return _read_pool_row(dal, tenant_id, tier_code)
            finally:
                _end_read(dal)

        return await asyncio.get_running_loop().run_in_executor(self._async_dal.executor, _job)

    async def put(
        self,
        tenant_id: int,
        tier_code: int,
        limit_count: int | None,
        source_code: int,
        observed_at: datetime,
        set_by: int | None = None,
    ) -> None:
        """Upsert the pool row (one per tenant/axis/tier) in a single committed job."""
        if tier_code not in POOLED_TIER_CODES:
            raise ValueError("pools exist only for professional (2) and enterprise (3)")
        if limit_count is not None and limit_count < UNLIMITED:
            raise ValueError("limit_count must be None, -1 (unlimited) or >= 0")
        if source_code not in _POOL_SOURCE_BY_CODE:
            raise ValueError(f"unknown pool source {source_code!r}")
        dal = self._dal

        def _job() -> None:
            try:
                # Serialize concurrent writers of this row (PG advisory lock; sqlite already
                # serializes writers) so update-then-insert cannot double-insert.
                _take_lock(dal, f"pool_row:{tenant_id}:{tier_code}")
                t = dal.tenant_license_pools
                query = (
                    (t.tenant_id == tenant_id)
                    & (t.axis_code == AXIS_TEAM)
                    & (t.tier_code == tier_code)
                )
                fields = {
                    "limit_count": limit_count,
                    "observed_at": observed_at,
                    "source_code": source_code,
                    "set_by": set_by,
                }
                if dal(query).update(**fields) == 0:
                    t.insert(
                        tenant_id=tenant_id, axis_code=AXIS_TEAM, tier_code=tier_code, **fields
                    )
                dal.commit()
            except Exception:
                dal.rollback()
                raise

        await asyncio.get_running_loop().run_in_executor(self._async_dal.executor, _job)

    async def delete_license_server_rows(self, tenant_id: int) -> int:
        """Evict the tenant's license_server-sourced pool rows; operator_set rows are untouched.

        Called when the tenant's license key is set, replaced or cleared so a cached pool never
        outlives the key that produced it.
        """
        dal = self._dal

        def _job() -> int:
            try:
                t = dal.tenant_license_pools
                n = int(
                    dal(
                        (t.tenant_id == tenant_id)
                        & (t.axis_code == AXIS_TEAM)
                        & (t.source_code == POOL_SOURCE_LICENSE_SERVER)
                    ).delete()
                )
                dal.commit()
                return n
            except Exception:
                dal.rollback()
                raise

        return int(await asyncio.get_running_loop().run_in_executor(self._async_dal.executor, _job))


class StorePoolProvider:
    """:class:`PoolProvider` that serves whatever :class:`PoolStore` holds (operator-set pools).

    No row = :class:`PoolUnavailable` (never a silent zero). ``stale`` is True once the row is
    older than ``stale_after`` (informational; the row is still authoritative here).
    """

    def __init__(self, store: PoolStore, *, stale_after: timedelta = timedelta(days=7)) -> None:
        """Wrap ``store``; ``stale_after`` only drives :attr:`PoolInfo.stale`."""
        self._store = store
        self._stale_after = stale_after

    async def get_pool(self, tenant_id: int, tier_code: int) -> PoolInfo:
        """Return the stored pool or raise :class:`PoolUnavailable` when none was ever seen."""
        row = await self._store.get(tenant_id, tier_code)
        if row is None:
            raise PoolUnavailable(f"no team pool recorded for tier {tier_code}")
        return PoolInfo(
            limit=row.limit_count,
            observed_at=row.observed_at,
            stale=now_utc() - row.observed_at > self._stale_after,
            source=_POOL_SOURCE_BY_CODE[row.source_code],
        )


async def _tenant_slug(async_dal: Any, dal: Any, tenant_id: int) -> str | None:
    def _job() -> str | None:
        try:
            row = dal(dal.tenants.id == tenant_id).select(dal.tenants.slug).first()
            return None if row is None or not row.slug else str(row.slug)
        finally:
            _end_read(dal)

    return await asyncio.get_running_loop().run_in_executor(async_dal.executor, _job)


async def resolve_effective_tier(
    async_dal: Any,
    dal: Any,
    tenant_id: int,
    community_id: int,
    *,
    tenant_tier: Callable[[str], Awaitable[str]] | None = None,
) -> str:
    """Return ``max(tenant tier, active allocation)`` as a tier name; fail closed to ``free``.

    Keyed by numeric ids. A stale allocation still confers its tier AND keeps its pool slot
    (automatic reclamation is not active; entitlements are never stripped). Any error logs
    ERROR (exception type only) and reports ``free``. ``tenant_tier`` overrides the entitlement
    lookup (tests / callers that already hold the tenant tier); default is
    ``flask_core.feature_flags.get_tier``.
    """
    try:
        bind_community_tier_tables(dal)
        slug = await _tenant_slug(async_dal, dal, tenant_id)
        resolver = tenant_tier or (lambda s: get_tier(tenant=s))
        tenant_name: str = await resolver(slug) if slug else TIER_FREE

        def _job() -> int:
            try:
                row = _community_active_row(dal, community_id)
                if row is None or int(row.tenant_id) != tenant_id:
                    return TIER_CODE_FREE
                return int(row.tier_code)
            finally:
                _end_read(dal)

        code = await asyncio.get_running_loop().run_in_executor(async_dal.executor, _job)
        community_name: str = TIER_NAMES.get(code, TIER_FREE)
        best: str = max(tenant_name, community_name, key=tier_level)
        return best if best in (TIER_FREE, TIER_PROFESSIONAL, TIER_ENTERPRISE) else TIER_FREE
    except Exception as exc:  # noqa: BLE001 - tier resolution fails closed, never raises
        logger.error("community_tier_resolve_failed error_type=%s", type(exc).__name__)
        return str(TIER_FREE)


async def pool_usage(
    async_dal: Any, dal: Any, tenant_id: int, *, pool: PoolProvider
) -> dict[int, TierUsage]:
    """Per-tier used/limit/available/overage/stale counts for the tenant's team pool.

    ``used`` includes stale allocations (they keep their slot until released).

    Raises :class:`PoolUnavailable` when a pool cannot be determined (no silent zero).
    """
    bind_community_tier_tables(dal)
    limits = {tier: (await pool.get_pool(tenant_id, tier)).limit for tier in POOLED_TIER_CODES}
    now = now_utc()

    def _job() -> dict[int, TierUsage]:
        try:
            out: dict[int, TierUsage] = {}
            for tier in POOLED_TIER_CODES:
                used, stale = _count_active(dal, tenant_id, tier, now)
                out[tier] = _compute_usage(used, stale, limits[tier], tier)
            return out
        finally:
            _end_read(dal)

    usage = await asyncio.get_running_loop().run_in_executor(async_dal.executor, _job)
    for tier, u in usage.items():
        telemetry.set_utilization(tenant_id, TIER_NAMES[tier], u.used, u.limit)
    return usage


async def touch_active(async_dal: Any, dal: Any, community_id: int) -> bool:
    """Mark the community's active allocation as recently active (clears its stale flag).

    Count is unaffected (stale rows already count). No production caller exists yet; it becomes
    the slot-reclamation signal once a real activity feed is wired.

    Cheap and rate-limited: an in-process 5 minute short-circuit, then a DB write only when
    ``last_active_at`` is older than :data:`TOUCH_MIN_INTERVAL`. Returns True when a row was
    written. Never raises into the caller (activity tracking must not break a request).
    """
    mono = time.monotonic()
    seen = _touch_seen.get(community_id)
    if seen is not None and mono - seen < _TOUCH_LOCAL_SECONDS:
        return False
    if len(_touch_seen) >= _TOUCH_LOCAL_MAX:
        _touch_seen.clear()
    _touch_seen[community_id] = mono

    def _job() -> bool:
        try:
            row = _community_active_row(dal, community_id)
            now = now_utc()
            last = _aware(row.last_active_at) if row is not None else None
            if row is None or (last is not None and now - last < TOUCH_MIN_INTERVAL):
                dal.rollback()
                return False
            dal(dal.community_tier_allocations.id == row.id).update(
                last_active_at=now, updated_at=now
            )
            dal.commit()
            return True
        except Exception:
            dal.rollback()
            raise

    try:
        bind_community_tier_tables(dal)
        return await asyncio.get_running_loop().run_in_executor(async_dal.executor, _job)
    except Exception as exc:  # noqa: BLE001 - activity tracking is best-effort
        _touch_seen.pop(community_id, None)
        logger.warning("community_tier_touch_failed error_type=%s", type(exc).__name__)
        return False


def _audit_event(
    *,
    action: AuditAction,
    outcome: AuditOutcome,
    actor: int | None,
    tenant_id: int,
    community_id: int,
    tier_code: int,
    previous_tier_code: int,
    source: int,
    reason: str | None,
    extra: dict[str, Any] | None = None,
) -> AuditEvent:
    details: dict[str, Any] = {
        "tier": TIER_NAMES[tier_code],
        "previous_tier": TIER_NAMES[previous_tier_code],
        "source": _SOURCE_LABELS[source],
    }
    if reason:
        details["reason_code"] = reason
    if extra:
        details.update(extra)
    return AuditEvent(
        category=AuditCategory.LICENSE,
        action=action.value,
        outcome=outcome,
        actor_kind=ActorKind.USER if actor is not None else ActorKind.SYSTEM,
        actor_user_id=actor,
        tenant_id=tenant_id,
        target_type="community",
        target_id=str(community_id),
        details=details,
    )


async def _record(audit: Any | None, event: AuditEvent) -> None:
    """Record ``event`` after the DB commit: one retry, then LOUD failure (never swallowed).

    The tier change is already committed, so a persistent audit failure must be visible: an
    ERROR log (exception type only), the ``audit_write_failed`` counter, and the exception
    propagates so the caller does not report an unaudited change as clean success.
    """
    if audit is None:
        return
    try:
        await audit.record(event)
        return
    except Exception as first:  # noqa: BLE001 - retried once below, then re-raised
        logger.warning("community_tier_audit_retry error_type=%s", type(first).__name__)
    try:
        await audit.record(event)
    except Exception as exc:
        telemetry.audit_write_failed.add(1, {"area": "community_tier"})
        logger.error(
            "community_tier_audit_write_failed action=%s error_type=%s",
            event.action,
            type(exc).__name__,
        )
        raise


async def allocate(
    async_dal: Any,
    dal: Any,
    *,
    tenant_id: int,
    community_id: int,
    tier: int,
    actor: int | None,
    source: int,
    pool: PoolProvider,
    reason: str | None = None,
    audit: Any | None = None,
) -> AllocationResult:
    """Set a community's tier from the tenant pool, atomically (lock -> count -> write -> commit).

    ``tier``: 1 = Free (releases any allocation, never blocked), 2 = Professional, 3 = Enterprise.
    Raises :class:`PoolExhausted` when a NEW Pro/Ent slot would exceed the pool (the previous
    allocation, e.g. on a Pro->Ent change with Ent full, is left untouched),
    :class:`PoolUnavailable` when the pool cannot be determined (operator overrides proceed
    without a known limit), :class:`CommunityNotInTenant` for a foreign/missing community.
    ``audit`` (an ``AuditService``) is written AFTER the commit; ``None`` skips auditing.
    """
    _validate(tier, source, reason)
    operator = source == SOURCE_OPERATOR_OVERRIDE
    started = time.perf_counter()
    result_label = "error"
    source_label = _SOURCE_LABELS[source]
    tier_label = TIER_NAMES[tier]
    with telemetry.span("community_tier.allocate", tier=tier_label, source=source_label):
        try:
            bind_community_tier_tables(dal)
            limit: int | None = None
            pool_source: PoolSourceName | None = None
            if tier in POOLED_TIER_CODES:
                try:
                    info = await pool.get_pool(tenant_id, tier)
                    limit, pool_source = info.limit, info.source
                except PoolUnavailable:
                    # Not fatal yet: an idempotent re-PUT of the same tier (no new slot) and
                    # operator overrides proceed; consuming a NEW slot without a known pool
                    # raises PoolUnavailable inside the job.
                    logger.warning("community_tier_pool_unavailable tier=%s", tier_label)
            outcome = await _allocate_job(
                async_dal,
                dal,
                tenant_id=tenant_id,
                community_id=community_id,
                tier=tier,
                actor=actor,
                source=source,
                limit=limit,
                pool_source=pool_source,
            )
            result_label = (
                "unchanged"
                if not outcome.changed
                else "released"
                if tier == TIER_CODE_FREE
                else "allocated"
            )
            if outcome.changed:
                if tier == TIER_CODE_FREE:
                    action = AuditAction.LICENSE_COMMUNITY_TIER_RELEASED
                elif operator:
                    action = AuditAction.LICENSE_COMMUNITY_TIER_OVERRIDDEN
                else:
                    action = AuditAction.LICENSE_COMMUNITY_TIER_ALLOCATED
                extra = {"overage": outcome.overage} if outcome.overage else None
                await _record(
                    audit,
                    _audit_event(
                        action=action,
                        outcome=AuditOutcome.SUCCESS,
                        actor=actor,
                        tenant_id=tenant_id,
                        community_id=community_id,
                        tier_code=tier,
                        previous_tier_code=outcome.previous_tier_code,
                        source=source,
                        reason=reason,
                        extra=extra,
                    ),
                )
            if tier in POOLED_TIER_CODES and pool_source is not None:
                telemetry.set_utilization(tenant_id, tier_label, outcome.used, outcome.limit)
            if outcome.previous_used is not None:
                telemetry.update_used(
                    tenant_id,
                    TIER_NAMES[outcome.previous_tier_code],
                    outcome.previous_used,
                )
            logger.info(
                "community_tier_%s tenant_id=%s community_id=%s tier=%s source=%s overage=%s",
                result_label,
                tenant_id,
                community_id,
                tier_label,
                source_label,
                outcome.overage,
            )
            return outcome
        except PoolUnavailable:
            result_label = "pool_unavailable"
            raise
        except PoolExhausted as exc:
            result_label = "blocked"
            logger.info(
                "community_tier_blocked tenant_id=%s community_id=%s tier=%s limit=%s used=%s",
                tenant_id,
                community_id,
                tier_label,
                exc.limit,
                exc.used,
            )
            await _record(
                audit,
                _audit_event(
                    action=AuditAction.LICENSE_COMMUNITY_TIER_ALLOCATION_BLOCKED,
                    outcome=AuditOutcome.DENIED,
                    actor=actor,
                    tenant_id=tenant_id,
                    community_id=community_id,
                    tier_code=tier,
                    previous_tier_code=TIER_CODE_FREE,
                    source=source,
                    reason=reason,
                    extra={"pool_limit": exc.limit, "pool_used": exc.used},
                ),
            )
            raise
        finally:
            telemetry.allocations.add(
                1, {"tier": tier_label, "result": result_label, "source": source_label}
            )
            telemetry.allocation_duration.record(
                time.perf_counter() - started, {"result": result_label}
            )


async def _allocate_job(
    async_dal: Any,
    dal: Any,
    *,
    tenant_id: int,
    community_id: int,
    tier: int,
    actor: int | None,
    source: int,
    limit: int | None,
    pool_source: PoolSourceName | None = None,
) -> AllocationResult:
    """The single synchronous guarded section (see the module docstring).

    ``limit``/``pool_source`` come from the async provider lookup made BEFORE the lock; under the
    lock the stored pool row of the same source is re-read and wins, so a pool shrink committed
    in between is honored. ``pool_source is None`` means the pool could not be determined.
    """
    operator = source == SOURCE_OPERATOR_OVERRIDE

    def _job() -> AllocationResult:
        try:
            _take_lock(dal, f"team_pool_community:{community_id}")
            if tier in POOLED_TIER_CODES:
                _take_lock(dal, f"team_pool:{tenant_id}:{tier}")
            community = (
                dal(dal.communities.id == community_id)
                .select(dal.communities.id, dal.communities.tenant_id)
                .first()
            )
            if (
                community is None
                or community.tenant_id is None
                or int(community.tenant_id) != tenant_id
            ):
                raise CommunityNotInTenant(f"community {community_id} is not in tenant {tenant_id}")
            now = now_utc()
            eff_limit = limit
            if tier in POOLED_TIER_CODES and pool_source is not None:
                fresh = _read_pool_row(dal, tenant_id, tier)
                if fresh is not None and _POOL_SOURCE_BY_CODE.get(fresh.source_code) == pool_source:
                    eff_limit = fresh.limit_count
            current = _community_active_row(dal, community_id)
            previous = int(current.tier_code) if current is not None else TIER_CODE_FREE

            if tier == TIER_CODE_FREE:
                if current is not None:
                    _release(dal, current, actor=actor, reason=RELEASE_DEALLOC, now=now)
                _write_denormalized(dal, community_id, TIER_CODE_FREE)
                prev_used = (
                    _count_active(dal, tenant_id, previous, now)[0] if current is not None else None
                )
                dal.commit()
                return AllocationResult(
                    community_id,
                    TIER_CODE_FREE,
                    previous,
                    current is not None,
                    None,
                    0,
                    None,
                    0,
                    prev_used,
                )

            used, stale_count = _count_active(dal, tenant_id, tier, now)
            if current is not None and previous == tier:
                dal(dal.community_tier_allocations.id == current.id).update(
                    last_active_at=now, updated_at=now
                )
                _write_denormalized(dal, community_id, tier)
                dal.commit()
                usage = _compute_usage(used, stale_count, eff_limit, tier)
                return AllocationResult(
                    community_id, tier, previous, False, str(current.public_id),
                    usage.used, eff_limit, usage.overage,
                )  # fmt: skip

            if not operator:
                if pool_source is None:
                    raise PoolUnavailable("team pool could not be determined")
                if eff_limit is None:
                    raise PoolExhausted(tier, 0, used)
                if eff_limit != UNLIMITED and used >= eff_limit:
                    raise PoolExhausted(tier, eff_limit, used)
            prev_used = None
            if current is not None:
                _release(dal, current, actor=actor, reason=RELEASE_TIER_CHANGE, now=now)
                prev_used = _count_active(dal, tenant_id, previous, now)[0]
            public_id = str(uuid.uuid4())
            dal.community_tier_allocations.insert(
                public_id=public_id,
                tenant_id=tenant_id,
                community_id=community_id,
                tier_code=tier,
                source_code=source,
                allocated_by=actor,
                allocated_at=now,
                last_active_at=now,
                created_at=now,
                updated_at=now,
            )
            _write_denormalized(dal, community_id, tier)
            dal.commit()
            used_after = used + 1
            overage = 0
            if eff_limit is not None and eff_limit != UNLIMITED:
                overage = max(0, used_after - eff_limit)
            elif eff_limit is None:
                overage = used_after
            return AllocationResult(
                community_id, tier, previous, True, public_id, used_after, eff_limit, overage,
                prev_used,
            )  # fmt: skip
        except Exception:
            dal.rollback()
            raise

    return await asyncio.get_running_loop().run_in_executor(async_dal.executor, _job)
