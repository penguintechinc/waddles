"""Free-tier entity caps: 1 user-created community, tenant, and admin-role user.

The unlicensed/baseline (``free``) tier may hold at most ONE user-created
community, tenant, and admin-role user (critical-rules.md Licensing Model:
team overage / seat overage -> hard block with an upgrade path). Professional
and Enterprise are never blocked here -- their limits come from the license
pool, enforced elsewhere -- and an already-provisioned entity is never
mutated or suspended: the check only refuses the NEW create.

Reserved/system entities are excluded from every count so the platform's own
scaffolding never consumes the one free slot: the ``default`` and ``system``
tenants and any ``is_global`` tenant/community, plus the bootstrap admin
(the lowest-id super admin -- the seeded initial admin from migration 081).

Core-mechanism kill-switch ``waddles.disable-free-tier-entity-caps``:
unseen/OFF = enforcement ON; ON = no cap (air-gapped deploys). Tier comes
from ``flask_core.feature_flags.get_tier`` which fails closed to ``free`` and
never raises; ``feature_enabled`` degrades to last-known/default and never
raises either, so a license/flag-server outage can never crash a create path.
"""

from __future__ import annotations

import logging
from typing import Any

from flask_core.auth import DEFAULT_TENANT_SLUG
from flask_core.feature_flags import feature_enabled, get_tier
from flask_core.tier_catalog import TIER_FREE, TIER_PROFESSIONAL, tier_level

from services.errors import ApiError

logger = logging.getLogger(__name__)

FLAG_DISABLE_FREE_TIER_CAPS = "waddles.disable-free-tier-entity-caps"
SYSTEM_TENANT_SLUG = "system"
# `DEFAULT_TENANT_SLUG` is the code-seeded catch-all ("global"); `default` is the
# security.md-named catch-all -- reserve both spellings plus `system`.
RESERVED_TENANT_SLUGS: frozenset[str] = frozenset(
    {DEFAULT_TENANT_SLUG, "default", SYSTEM_TENANT_SLUG}
)
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


async def count_user_created_communities(async_dal: Any, dal: Any, *, tenant_id: int) -> int:
    """Active, non-global communities owned by ``tenant_id``."""
    rows = await async_dal.select_async(dal(dal.communities.tenant_id == tenant_id))
    return sum(1 for r in rows if not r.is_global and r.is_active is not False)


async def count_user_created_tenants(async_dal: Any, dal: Any) -> int:
    """Active tenants that are neither global nor the reserved default/system tenants."""
    rows = await async_dal.select_async(dal(dal.tenants.id > 0))
    return sum(
        1
        for r in rows
        if not r.is_global and r.is_active is not False and r.slug not in RESERVED_TENANT_SLUGS
    )


async def admin_user_ids(async_dal: Any, dal: Any) -> set[int]:
    """Distinct admin-role users: super admins plus tenant admins, bootstrap admin removed.

    The bootstrap admin is the lowest-id super admin (migration 081's seeded
    initial admin) -- it is the allowed "1" outside the cap, never counted.
    """
    supers = await async_dal.select_async(dal(dal.hub_users.is_super_admin == True))  # noqa: E712
    super_ids = sorted(int(r.id) for r in supers)
    tenant_admins = await async_dal.select_async(dal(dal.tenant_admins.id > 0))
    ids = set(super_ids) | {int(r.user_id) for r in tenant_admins}
    if super_ids:
        ids.discard(super_ids[0])
    return ids


async def enforce_community_cap(
    async_dal: Any, dal: Any, *, tenant_slug: str, tenant_id: int
) -> None:
    """Refuse (402) a 2nd user-created community on the free tier."""
    if not await _caps_apply(tenant_slug):
        return
    if await count_user_created_communities(async_dal, dal, tenant_id=tenant_id) >= FREE_TIER_CAP:
        raise _cap_error("community", "community", tenant_slug)


async def enforce_tenant_cap(async_dal: Any, dal: Any, *, tenant_slug: str) -> None:
    """Refuse (402) a 2nd user-created tenant on the free tier."""
    if not await _caps_apply(tenant_slug):
        return
    if await count_user_created_tenants(async_dal, dal) >= FREE_TIER_CAP:
        raise _cap_error("tenant", "tenant", tenant_slug)


async def enforce_admin_cap(
    async_dal: Any, dal: Any, *, tenant_slug: str, target_user_id: int
) -> None:
    """Refuse (402) making a 2nd user-created admin-role user on the free tier.

    A target who already holds an admin role is a no-op re-grant, never blocked.
    """
    if not await _caps_apply(tenant_slug):
        return
    existing = await admin_user_ids(async_dal, dal)
    if target_user_id in existing:
        return
    if len(existing) >= FREE_TIER_CAP:
        raise _cap_error("admin", "admin user", tenant_slug)
