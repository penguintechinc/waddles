"""Free-tier entity caps: 1 user-created community, tenant, and admin-role user.

Cap scope map -- the tier is ALWAYS resolved against the SAME scope the count uses:

- community: count per caller tenant; tier = that tenant; lock ``community:<tenant_id>``.
- tenant: count platform-wide (active, non-`system`); tier = platform (catch-all tenant);
  lock ``tenant:platform``.
- super-admin grant (the GLOBAL admin cap): count platform-wide `is_super_admin` users only,
  the explicitly-marked bootstrap admin (``hub_users.is_bootstrap``) excluded; tier =
  platform; lock ``admin:platform``.
- tenant-admin add: count ``tenant_admins`` rows of the TARGET tenant; tier = the TARGET
  tenant (never the caller's); lock ``tenant_admin:<tenant_id>``.

The two admin caps are DISTINCT and never cross-count: a free deployment may hold 1
global/super admin AND 1 tenant admin per tenant; a 2nd of either kind is blocked alone.

"Platform (catch-all tenant)" = the ``is_global`` tenant row's slug (the deployment
baseline tier); when no such row exists it falls back to the caller's tenant.

Atomicity: count-then-insert is a TOCTOU race, so each guarded create runs as ONE
synchronous executor job on one thread-local pydal connection (the repo's
single-job pattern, see ``AsyncDAL.transaction_async``'s caveat): take
``pg_advisory_xact_lock(hashtext(<key>))`` first, THEN count, THEN write, THEN commit.
Concurrent creators serialize on the DB lock (released by the commit/rollback); no
sync lock is held across an ``await`` -- the only awaits (tier/flag resolution) happen
before the job starts.

The unlicensed/baseline (``free``) tier may hold at most ONE user-created
community, tenant, and admin-role user (critical-rules.md Licensing Model:
team overage / seat overage -> hard block with an upgrade path). Professional
and Enterprise are never blocked here -- their limits come from the license
pool, enforced elsewhere -- and an already-provisioned entity is never
mutated or suspended: the check only refuses the NEW create.

Reserved/system entities are excluded so platform scaffolding never consumes the
one free slot: the ``system`` tenant (tenant cap only -- the catch-all tenant
COUNTS), any ``is_global`` community, plus the bootstrap admin,
identified by the explicit ``hub_users.is_bootstrap`` marker (migration 0065 backfills it onto
migration 081's seeded initial admin) -- never by position, so deleting/demoting it can never
promote another super admin into the free "bootstrap" slot.

Core-mechanism kill-switch ``waddles.disable-free-tier-entity-caps``:
unseen/OFF = enforcement ON; ON = no cap (air-gapped deploys). Tier comes
from ``flask_core.feature_flags.get_tier`` which fails closed to ``free`` and
never raises; ``feature_enabled`` degrades to last-known/default and never
raises either, so a license/flag-server outage can never crash a create path.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from flask_core.feature_flags import feature_enabled, get_tier
from flask_core.tier_catalog import TIER_FREE, TIER_PROFESSIONAL, tier_level

from services.errors import ApiError

logger = logging.getLogger(__name__)

FLAG_DISABLE_FREE_TIER_CAPS = "waddles.disable-free-tier-entity-caps"
SYSTEM_TENANT_SLUG = "system"
# Only the operator-plane tenant is excluded from the tenant cap. The catch-all tenant
# (slug `global`, `is_global=TRUE` per migration 058) COUNTS: a free deployment whose
# only tenant is the catch-all is already at the cap (multi-tenancy is paid).
RESERVED_TENANT_SLUGS: frozenset[str] = frozenset({SYSTEM_TENANT_SLUG})
FREE_TIER_CAP = 1
CAP_ERROR_CODE = "FREE_TIER_CAP_REACHED"
CAP_STATUS = 402


async def _caps_apply(tenant_slug: str) -> bool:
    """True when the caller is on the free tier and the kill-switch is not ON."""
    if await feature_enabled(FLAG_DISABLE_FREE_TIER_CAPS, tenant=tenant_slug):
        return False
    tier = await get_tier(tenant=tenant_slug)
    return bool(tier_level(tier) < tier_level(TIER_PROFESSIONAL))


def _cap_error(kind: str, noun: str, tenant_slug: str) -> ApiError:
    """Build the hard-block 402 carrying the upgrade path + required tier."""
    logger.info("free_tier_cap_blocked kind=%s tenant=%s", kind, tenant_slug)
    return ApiError(
        message=(
            f"The free tier is limited to {FREE_TIER_CAP} user-created {noun}. "
            f"Upgrade to Professional or Enterprise to create more."
        ),
        status_code=CAP_STATUS,
        code=CAP_ERROR_CODE,
        details={
            "cap": kind,
            "limit": FREE_TIER_CAP,
            "currentTier": TIER_FREE,
            "requiredTier": TIER_PROFESSIONAL,
            "upgrade": True,
        },
    )


def count_user_created_communities(dal: Any, *, tenant_id: int) -> int:
    """Active, non-global communities owned by ``tenant_id`` (sync; SQL ``COUNT`` in the job).

    NULL ``is_global`` is NOT global (user-created) and NULL ``is_active`` is active, so both
    are matched explicitly (a plain ``!=`` would drop NULL rows under SQL three-valued logic).
    """
    c = dal.communities
    query = (
        (c.tenant_id == tenant_id)
        & ((c.is_global == None) | (c.is_global == False))  # noqa: E711, E712
        & ((c.is_active == None) | (c.is_active == True))  # noqa: E711, E712
    )
    return int(dal(query).count())


def count_user_created_tenants(dal: Any) -> int:
    """Active tenants other than the operator-plane `system` tenant (catch-all counts)."""
    t = dal.tenants
    query = (
        (t.id > 0)
        & ((t.is_active == None) | (t.is_active == True))  # noqa: E711, E712
        & ~t.slug.belongs(sorted(RESERVED_TENANT_SLUGS))
    )
    return int(dal(query).count())


def super_admin_ids(dal: Any) -> set[int]:
    """Platform super admins only, bootstrap admin removed (tenant admins are NOT counted).

    The bootstrap admin is the row explicitly marked ``is_bootstrap`` (migration 0065 marks
    the 081-seeded initial admin) -- the allowed "1" outside the cap, never counted. It is
    matched by marker, not position: removing/demoting it frees nothing.
    """
    h = dal.hub_users
    query = (h.is_super_admin == True) & (  # noqa: E712
        (h.is_bootstrap == None) | (h.is_bootstrap == False)  # noqa: E711, E712
    )
    return {int(r.id) for r in dal(query).select(h.id)}


def count_tenant_admins(dal: Any, *, tenant_id: int) -> int:
    """Admin rows of one TARGET tenant (the per-tenant admin cap's count scope)."""
    return len(dal(dal.tenant_admins.tenant_id == tenant_id).select())


def _platform_tier_slug(dal: Any, fallback: str) -> str:
    """Slug whose tier is the platform's: the catch-all (`is_global`) tenant, else ``fallback``."""
    for r in dal(dal.tenants.id > 0).select():
        if r.is_global and r.is_active is not False and r.slug:
            return str(r.slug)
    return fallback


def _take_lock(dal: Any, key: str) -> None:
    """Serialize concurrent creators on ``key`` for the rest of this transaction (Postgres).

    ``pg_advisory_xact_lock`` blocks until the holder commits/rolls back, so the count
    that follows sees every earlier creator's row. Other dialects (sqlite in tests)
    already serialize writers at the connection level and have no such function.
    """
    if str(getattr(dal, "_dbname", "")).startswith("postgres"):
        dal.executesql("SELECT pg_advisory_xact_lock(hashtext(%s))", placeholders=[key])


async def capped_write[T](
    async_dal: Any,
    dal: Any,
    *,
    kind: str,
    noun: str,
    tier_slug: str,
    lock_key: str,
    is_full: Callable[[Any], bool],
    write: Callable[[Any], T],
) -> T:
    """Atomically (lock -> count -> write -> commit) run ``write`` unless the cap is reached.

    ``tier_slug`` MUST be the tenant whose scope ``is_full`` counts. Tier/kill-switch
    are resolved first (async, outside the DB job); the guarded section is a single
    synchronous executor job so no ``await`` can interleave between count and insert.
    Raises the 402 ``FREE_TIER_CAP_REACHED`` ApiError from inside the job (rolled back).
    """
    applies = await _caps_apply(tier_slug)
    loop = asyncio.get_running_loop()

    def _job() -> T:
        try:
            if applies:
                _take_lock(dal, f"free_cap:{lock_key}")
                if is_full(dal):
                    raise _cap_error(kind, noun, tier_slug)
            result = write(dal)
            dal.commit()
            return result
        except Exception:
            dal.rollback()
            raise

    return await loop.run_in_executor(async_dal.executor, _job)


async def create_community_capped(
    async_dal: Any,
    dal: Any,
    *,
    tenant_slug: str,
    tenant_id: int,
    values: dict[str, Any],
    member_values: Callable[[int], dict[str, Any]] | None = None,
) -> int:
    """Insert a community unless the caller's tenant already holds its 1 free community.

    ``member_values(new_community_id)`` (optional) builds the owner ``community_members`` row,
    inserted in the SAME transaction as the community (all-or-nothing): a failure rolls both
    back, so no half-created community can count toward the cap.
    """

    def _write(d: Any) -> int:
        new_id = int(d.communities.insert(**values))
        if member_values is not None:
            d.community_members.insert(**member_values(new_id))
        return new_id

    return int(
        await capped_write(
            async_dal,
            dal,
            kind="community",
            noun="community",
            tier_slug=tenant_slug,
            lock_key=f"community:{tenant_id}",
            is_full=lambda d: (
                count_user_created_communities(d, tenant_id=tenant_id) >= FREE_TIER_CAP
            ),
            write=_write,
        )
    )


async def create_tenant_capped(
    async_dal: Any, dal: Any, *, caller_tenant_slug: str, values: dict[str, Any]
) -> int:
    """Insert a tenant unless the platform already holds its 1 free user-created tenant."""
    tier_slug = await asyncio.to_thread(_platform_tier_slug, dal, caller_tenant_slug)
    return int(
        await capped_write(
            async_dal,
            dal,
            kind="tenant",
            noun="tenant",
            tier_slug=tier_slug,
            lock_key="tenant:platform",
            is_full=lambda d: count_user_created_tenants(d) >= FREE_TIER_CAP,
            write=lambda d: d.tenants.insert(**values),
        )
    )


async def grant_super_admin_capped(
    async_dal: Any,
    dal: Any,
    *,
    caller_tenant_slug: str,
    user_id: int,
    update_fields: dict[str, Any],
) -> None:
    """Grant platform super admin unless the platform already holds its 1 free super admin.

    Counts super admins only (never tenant admins). Re-granting an existing one is a no-op.
    """
    tier_slug = await asyncio.to_thread(_platform_tier_slug, dal, caller_tenant_slug)

    def _is_full(d: Any) -> bool:
        existing = super_admin_ids(d)
        return user_id not in existing and len(existing) >= FREE_TIER_CAP

    await capped_write(
        async_dal,
        dal,
        kind="admin",
        noun="admin user",
        tier_slug=tier_slug,
        lock_key="admin:platform",
        is_full=_is_full,
        write=lambda d: d(d.hub_users.id == user_id).update(**update_fields),
    )


async def add_tenant_admin_capped(
    async_dal: Any,
    dal: Any,
    *,
    target_tenant_slug: str,
    tenant_id: int,
    values: dict[str, Any],
) -> int:
    """Insert a tenant-admin row unless the TARGET tenant already holds its 1 free admin.

    Tier and count are both the target tenant's -- never the caller's.
    """
    return int(
        await capped_write(
            async_dal,
            dal,
            kind="admin",
            noun="admin user",
            tier_slug=target_tenant_slug,
            lock_key=f"tenant_admin:{tenant_id}",
            is_full=lambda d: count_tenant_admins(d, tenant_id=tenant_id) >= FREE_TIER_CAP,
            write=lambda d: d.tenant_admins.insert(**values),
        )
    )
