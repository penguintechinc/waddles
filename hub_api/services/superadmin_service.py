"""Cross-tenant platform-admin operations -- ported from `superadminController.js`.

Mounted at `/api/v1/superadmin/*` in Node (`routes/superadmin.js`), gated
there by `requireAuth` + `requireSuperAdmin` (a boolean `hub_users.
is_super_admin` check, no per-tenant scoping -- superadmin operates across
every community/tenant on the platform). This port follows the exact
precedent `blueprints/v1/user_management.py` (M1) already established for
this same "superadmin" surface: `@tenant_middleware` still runs first
(every JWT in this system carries a `tenant` claim, per security.md), but
`@require_scope("communities:admin" | "modules:admin" | "tenants:admin")`
is what actually gates the endpoint -- all three satisfied by the
`global:admin` bundle's `*:admin` wildcard scope
(`auth_service.create_session_token` grants that bundle exactly when
`hub_users.is_super_admin` is true), the OIDC-scope-native equivalent of
Node's boolean check. Data operations below are deliberately NOT
tenant-filtered -- matches Node's own cross-tenant behavior for this
controller.

**Ported here (this PR's M3 slice):** dashboard stats, community CRUD +
ownership reassignment, marketplace module registry CRUD, tenant CRUD.
**Not ported:** `getModuleDbAccounts`/`rotateModuleDbPassword`/
`deactivateModuleDbAccount` -- dead code in Node itself (exported by
`superadminController.js` but never wired to any Express route in
`routes/superadmin.js`; confirmed by grep across the whole backend
source). Platform config / hub settings / software+service discovery are
a different controller (`platformConfigController.js`) not in this PR's
scope (see task brief: "CONTROLLERS: adminController.js +
superadminController.js").
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from typing import Any

from flask_core.auth import DEFAULT_TENANT_SLUG
from opentelemetry import metrics

from services import community_tier_service
from services.errors import (
    ApiError,
    bad_request,
    conflict,
    forbidden,
    not_found,
    tenant_tier_required,
)
from services.free_tier_caps import create_community_capped, create_tenant_capped
from services.global_identity import SYSTEM_TENANT_SLUG
from services.schema import bind_admin_tables, bind_superadmin_tenant_fields

logger = logging.getLogger(__name__)
_TIER_OVERRIDE_FAILED = metrics.get_meter("waddles.hub_api.superadmin").create_counter(
    "waddles.superadmin.tier_override_failed",
    unit="1",
    description="Operator tier overrides whose allocation or audit write failed (loud).",
)

VALID_COMMUNITY_TYPES = ("shared_interest_group", "gaming", "creator", "corporate", "other")
VALID_JOIN_MODES = ("open", "approval", "invite")
#: Reserved tenant slugs (security.md): `system` = operator plane (its admins ARE the global
#: admins), `default` = catch-all. Never creatable, renamable-to, renamable or deactivatable here.
RESERVED_TENANT_SLUGS = frozenset({SYSTEM_TENANT_SLUG, DEFAULT_TENANT_SLUG})
#: Slugs that would shadow a fixed route segment (public DSAR routes under
#: `/api/v1/public/<tenant_slug>/...` and the webui `/privacy-request/:tenantSlug` pages) or a
#: top-level path. Rejected on tenant create and rename-to only; unlike RESERVED_TENANT_SLUGS this
#: does not affect deactivation/rename-from of existing tenants.
RESERVED_ROUTE_SLUGS = frozenset(
    {
        "verify",
        "resend-verification",
        "privacy-requests",
        "captcha-config",
        "altcha-challenge",
        "preview",
        "public",
        "api",
        "admin",
        "tenant",
        "superadmin",
    }
)


def _ensure_tables(dal: Any) -> None:
    bind_admin_tables(dal)


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


# ── Dashboard ────────────────────────────────────────────────────────────


async def get_dashboard_stats(async_dal: Any, dal: Any) -> dict[str, Any]:
    """Get superadmin dashboard stats."""
    _ensure_tables(dal)
    all_communities = await async_dal.select_async(dal(dal.communities.id > 0))
    total = len(all_communities)
    active = sum(1 for c in all_communities if c.is_active)
    platform_counts = {"discord": 0, "twitch": 0, "slack": 0}
    total_members = 0
    for c in all_communities:
        if c.platform in platform_counts:
            platform_counts[c.platform] += 1
        total_members += c.member_count or 0

    admin_rows = await async_dal.select_async(dal(dal.hub_admins.is_active == True))  # noqa: E712
    recent = await async_dal.select_async(
        dal(dal.communities.id > 0), orderby=~dal.communities.created_at, limitby=(0, 5)
    )

    return {
        "stats": {
            "totalCommunities": total,
            "activeCommunities": active,
            "platformBreakdown": platform_counts,
            "totalMembers": total_members,
            "adminCount": len(admin_rows),
        },
        "recentCommunities": [
            {
                "id": c.id,
                "name": c.name,
                "displayName": c.display_name or c.name,
                "platform": c.platform,
                "createdAt": _iso(c.created_at),
            }
            for c in recent
        ],
    }


# ── Community management ────────────────────────────────────────────────


async def list_communities(
    async_dal: Any,
    dal: Any,
    *,
    page: int,
    limit: int,
    search: str,
    platform: str | None,
    is_active: bool | None,
) -> tuple[list[Any], int, int]:
    """List all communities (cross-tenant, superadmin view)."""
    _ensure_tables(dal)
    page = max(1, page)
    limit = min(100, max(1, limit))
    offset = (page - 1) * limit

    query = dal.communities.id > 0
    if search:
        query &= dal.communities.name.like(f"%{search}%", case_sensitive=False) | (
            dal.communities.display_name.like(f"%{search}%", case_sensitive=False)
        )
    if platform:
        query &= dal.communities.platform == platform
    if is_active is not None:
        query &= dal.communities.is_active == is_active

    total = await async_dal.count_async(query)
    rows = await async_dal.select_async(
        dal(query), orderby=~dal.communities.created_at, limitby=(offset, offset + limit)
    )
    total_pages = math.ceil(total / limit) if limit else 0
    return list(rows), total, total_pages


async def get_community(async_dal: Any, dal: Any, *, community_id: int) -> Any:
    """Get a single community by id (no tenant filter -- superadmin view)."""
    _ensure_tables(dal)
    rows = await async_dal.select_async(dal(dal.communities.id == community_id))
    row = rows.first()
    if row is None:
        raise not_found("Community not found")
    return row


async def create_community(
    async_dal: Any,
    dal: Any,
    *,
    name: str,
    display_name: str | None,
    description: str | None,
    platform: str,
    platform_server_id: str | None,
    is_public: bool | None,
    community_type: str | None,
    caller_id: int,
    tenant_slug: str,
    tenant_id: int,
) -> Any:
    """Create a new community owned by ``tenant_id`` (free tier: capped at 1).

    Authority is the per-community role table: the caller becomes a member holding the seeded
    system admin role (see `create_community_capped`); `communities.owner_id` is no longer written.
    """
    _ensure_tables(dal)
    if not name or not platform:
        raise bad_request("Name and platform are required")

    validated_type = community_type or "creator"
    if validated_type not in VALID_COMMUNITY_TYPES:
        raise bad_request(
            f"Invalid community type. Must be one of: {', '.join(VALID_COMMUNITY_TYPES)}"
        )

    slug = name.lower().strip().replace(" ", "-")
    comm = dal.communities
    # Names stay globally unique (`communities_name_key`): name-only lookups (Rust tenant
    # resolver, seeds) would cross tenants otherwise. Per-tenant uniqueness is a follow-up; the
    # generic message never says WHICH tenant holds the name. A platform server (e.g. a Discord
    # guild) may be claimed by ONE community across ALL tenants.
    if await async_dal.select_async(dal(comm.name == slug)):
        raise conflict("Community name is unavailable")
    if platform_server_id and await async_dal.select_async(
        dal((comm.platform == platform) & (comm.platform_server_id == platform_server_id))
    ):
        raise conflict("Platform server is already linked to a community")

    new_id = await create_community_capped(
        async_dal,
        dal,
        tenant_slug=tenant_slug,
        tenant_id=tenant_id,
        values=dict(
            name=slug,
            display_name=display_name or name,
            description=description or "",
            platform=platform,
            platform_server_id=platform_server_id,
            is_public=is_public is not False,
            community_type=validated_type,
            tenant_id=tenant_id,
            member_count=1,
            created_by=str(caller_id),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        ),
        creator_id=caller_id,
    )
    return await get_community(async_dal, dal, community_id=new_id)


#: Short machine reasons an operator may record for a tier override (the audit rule only accepts a
#: short snake_case token, never free text -- `community_tier_service._REASON_RE`).
TIER_OVERRIDE_REASON_CODES = (
    "support_request",
    "sales_agreement",
    "trial_extension",
    "billing_correction",
    "partner_program",
    "data_correction",
    "other",
)


async def update_community(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    fields: dict[str, Any],
    license_tier: int | None = None,
    reason_code: str | None = None,
    actor: int | None = None,
    pool: Any = None,
    audit: Any = None,
) -> None:
    """Update community details and/or set its licence tier (operator override).

    `fields` are already-validated update deltas (camelCase->column); a `None` value means
    "set NULL" (the route only passes None for the nullable `description`/`seat_limit`).
    `is_premium` is derived from the tier and can never be written directly. `license_tier`
    (1 free / 2 premium) goes through
    `community_tier_service.allocate(source=operator_override)`: never blocked by a full pool,
    counted as overage and audited, with `reason_code` as the audit reason. The tenant must still
    be licensed (402 `TENANT_TIER_REQUIRED` for a Free tenant; on bypass domains set its pool
    first) and its pool determinable (503 `TEAM_POOL_UNAVAILABLE`).
    """
    _ensure_tables(dal)
    community = await get_community(async_dal, dal, community_id=community_id)  # 404 if missing

    if "is_premium" in fields:
        raise bad_request("isPremium is derived from licenseTier; send licenseTier instead")
    community_type = fields.get("community_type")
    if community_type is not None and community_type not in VALID_COMMUNITY_TYPES:
        raise bad_request(
            f"Invalid community type. Must be one of: {', '.join(VALID_COMMUNITY_TYPES)}"
        )
    if not fields and license_tier is None:
        raise bad_request("No updates provided")
    if license_tier is not None:
        if license_tier not in community_tier_service.TIER_NAMES:
            raise bad_request("licenseTier must be 1 (free) or 2 (premium)")
        if reason_code not in TIER_OVERRIDE_REASON_CODES:
            raise bad_request(
                "reasonCode is required with licenseTier and must be one of: "
                + ", ".join(TIER_OVERRIDE_REASON_CODES)
            )
        if community.tenant_id is None:
            raise bad_request("Community has no tenant; its tier cannot be allocated")

    if license_tier is not None:
        try:
            await community_tier_service.allocate(
                async_dal,
                dal,
                tenant_id=int(community.tenant_id),
                community_id=community_id,
                tier=license_tier,
                actor=actor,
                source=community_tier_service.SOURCE_OPERATOR_OVERRIDE,
                pool=pool,
                reason=reason_code,
                audit=audit,
            )
        except ApiError:
            raise
        except community_tier_service.TenantNotLicensed:
            raise tenant_tier_required() from None
        except community_tier_service.PoolUnavailable:
            raise ApiError(
                "The team pool for this tenant could not be determined",
                503,
                "TEAM_POOL_UNAVAILABLE",
            ) from None
        except Exception as exc:
            # The allocation commits BEFORE its audit event is written, so an audit-sink failure
            # leaves the override applied but unaudited: never report success, make it loud.
            logger.error(
                "community_tier_override_failed community_id=%s error_type=%s",
                community_id,
                type(exc).__name__,
            )
            _TIER_OVERRIDE_FAILED.add(1, {"error_type": type(exc).__name__})
            raise ApiError(
                "Tier override could not be completed or audited; verify the community's tier",
                500,
                "TIER_OVERRIDE_FAILED",
            ) from exc
    if fields:
        fields["updated_at"] = datetime.now(UTC)
        await async_dal.update_async(dal.communities.id == community_id, **fields)


async def delete_community(async_dal: Any, dal: Any, *, community_id: int, caller_id: int) -> None:
    """Deactivate (soft-delete) a community. Global communities cannot be deleted."""
    _ensure_tables(dal)
    community = await get_community(async_dal, dal, community_id=community_id)
    if community.is_global:
        raise forbidden("Global communities cannot be deleted")
    await async_dal.update_async(
        dal.communities.id == community_id,
        is_active=False,
        deleted_at=datetime.now(UTC),
        deleted_by=str(caller_id),
    )


async def reassign_owner(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    new_owner_id: str | None,
    new_owner_name: str | None,
) -> None:
    """Reassign community ownership."""
    _ensure_tables(dal)
    if not new_owner_name:
        raise bad_request("New owner name is required")
    await get_community(async_dal, dal, community_id=community_id)  # 404 if missing
    await async_dal.update_async(
        dal.communities.id == community_id,
        owner_id=new_owner_id,
        owner_name=new_owner_name,
        updated_at=datetime.now(UTC),
    )


# ── Tenant management ────────────────────────────────────────────────────

_SLUG_RE_MSG = "Slug must be lowercase alphanumeric with hyphens, no leading/trailing hyphens"


def _normalize_slug(slug: str) -> str:
    """The stored form of a slug (trimmed); reserved/duplicate checks use `.lower()` of this."""
    return slug.strip()


def _valid_slug(slug: str) -> bool:
    if not slug or slug[0] == "-" or slug[-1] == "-":
        return False
    if slug.isdigit():  # digits-only would collide with numeric tenant ids where slugs coerce
        return False
    return all(c.isalnum() or c == "-" for c in slug) and slug.islower()


async def list_tenants(
    async_dal: Any, dal: Any, *, page: int, limit: int, search: str
) -> tuple[list[Any], int, int]:
    """List all tenants."""
    bind_superadmin_tenant_fields(dal)
    page = max(1, page)
    limit = min(100, max(1, limit))
    offset = (page - 1) * limit

    query = dal.tenants.id > 0
    if search:
        query &= dal.tenants.slug.like(f"%{search}%", case_sensitive=False) | (
            dal.tenants.display_name.like(f"%{search}%", case_sensitive=False)
        )
    total = await async_dal.count_async(query)
    rows = await async_dal.select_async(
        dal(query),
        orderby=~dal.tenants.is_global | ~dal.tenants.created_at,
        limitby=(offset, offset + limit),
    )
    total_pages = math.ceil(total / limit) if limit else 0
    return list(rows), total, total_pages


async def create_tenant(
    async_dal: Any,
    dal: Any,
    *,
    slug: str,
    display_name: str,
    description: str | None,
    logo_url: str | None,
    seat_limit: int | None,
    allowed_module_ids: list[int] | None,
    caller_tenant_slug: str,
) -> Any:
    """Create a new tenant (free tier: capped at 1 user-created tenant)."""
    bind_superadmin_tenant_fields(dal)
    if not slug or not slug.strip():
        raise bad_request("Tenant slug is required")
    if not display_name or not display_name.strip():
        raise bad_request("Display name is required")
    slug = _normalize_slug(slug)
    if slug.isdigit():
        raise bad_request("Slug cannot be digits only (it would collide with numeric tenant ids)")
    if not _valid_slug(slug):
        raise bad_request(_SLUG_RE_MSG)
    if slug.lower() in RESERVED_TENANT_SLUGS:
        raise conflict("That tenant slug is reserved")
    if slug.lower() in RESERVED_ROUTE_SLUGS:
        raise conflict("That tenant slug is reserved")

    existing = await async_dal.select_async(dal(dal.tenants.slug == slug))
    if existing:
        raise conflict("A tenant with that slug already exists")

    new_id = await create_tenant_capped(
        async_dal,
        dal,
        caller_tenant_slug=caller_tenant_slug,
        values=dict(
            slug=slug,
            display_name=display_name.strip(),
            description=description or "",
            logo_url=logo_url,
            seat_limit=seat_limit,
            allowed_module_ids=allowed_module_ids,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        ),
    )
    rows = await async_dal.select_async(dal(dal.tenants.id == new_id))
    return rows.first()


async def update_tenant(
    async_dal: Any, dal: Any, *, tenant_id: int, fields: dict[str, Any]
) -> None:
    """Update a tenant."""
    bind_superadmin_tenant_fields(dal)
    if not fields:
        raise bad_request("No updates provided")
    fields["updated_at"] = datetime.now(UTC)
    rows = await async_dal.select_async(dal(dal.tenants.id == tenant_id))
    if not rows:
        raise not_found("Tenant not found")
    current_slug = str(rows.first().slug or "")
    new_slug = fields.get("slug")
    if new_slug is not None:
        new_slug = _normalize_slug(str(new_slug))
        fields["slug"] = new_slug  # store exactly the normalized form that was checked
        if new_slug.isdigit():
            raise bad_request(
                "Slug cannot be digits only (it would collide with numeric tenant ids)"
            )
        if not _valid_slug(new_slug):
            raise bad_request(_SLUG_RE_MSG)
        if new_slug.lower() in RESERVED_TENANT_SLUGS or new_slug.lower() in RESERVED_ROUTE_SLUGS:
            raise forbidden("That tenant slug is reserved")
    if current_slug.lower() in RESERVED_TENANT_SLUGS and (
        (new_slug is not None and new_slug != current_slug) or fields.get("is_active") is False
    ):
        raise forbidden("A reserved tenant cannot be renamed or deactivated")
    await async_dal.update_async(dal.tenants.id == tenant_id, **fields)


async def delete_tenant(async_dal: Any, dal: Any, *, tenant_id: int) -> None:
    """Deactivate a tenant. The global tenant cannot be deleted."""
    bind_superadmin_tenant_fields(dal)
    rows = await async_dal.select_async(dal(dal.tenants.id == tenant_id))
    row = rows.first()
    if row is None:
        raise not_found("Tenant not found")
    if row.is_global or str(row.slug or "") in RESERVED_TENANT_SLUGS:
        raise forbidden("Cannot delete a reserved tenant")
    await async_dal.update_async(
        dal.tenants.id == tenant_id, is_active=False, updated_at=datetime.now(UTC)
    )
