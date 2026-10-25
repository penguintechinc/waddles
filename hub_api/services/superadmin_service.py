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

import contextlib
import math
import secrets
from datetime import UTC, datetime
from typing import Any

from services.errors import bad_request, conflict, forbidden, not_found
from services.free_tier_caps import create_community_capped, create_tenant_capped
from services.schema import bind_admin_tables, bind_superadmin_tenant_fields

VALID_COMMUNITY_TYPES = ("shared_interest_group", "gaming", "creator", "corporate", "other")
VALID_JOIN_MODES = ("open", "approval", "invite")


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
    owner_id: str | None,
    owner_name: str | None,
    is_public: bool | None,
    community_type: str | None,
    caller_id: int,
    tenant_slug: str,
    tenant_id: int,
) -> Any:
    """Create a new community owned by ``tenant_id`` (free tier: capped at 1)."""
    _ensure_tables(dal)
    if not name or not platform:
        raise bad_request("Name and platform are required")

    validated_type = community_type or "creator"
    if validated_type not in VALID_COMMUNITY_TYPES:
        raise bad_request(
            f"Invalid community type. Must be one of: {', '.join(VALID_COMMUNITY_TYPES)}"
        )

    slug = name.lower().strip().replace(" ", "-")
    existing = await async_dal.select_async(dal(dal.communities.name == slug))
    if existing:
        raise conflict("Community name already exists")

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
            owner_id=owner_id,
            owner_name=owner_name,
            is_public=is_public is not False,
            community_type=validated_type,
            tenant_id=tenant_id,
            member_count=1,
            created_by=str(caller_id),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        ),
    )
    community = await get_community(async_dal, dal, community_id=new_id)
    await async_dal.insert_async(
        dal.community_members,
        community_id=new_id,
        user_id=str(caller_id),
        role="owner",
        reputation=600,
        is_active=True,
        joined_at=datetime.now(UTC),
    )
    return community


async def update_community(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    fields: dict[str, Any],
) -> None:
    """Update community details.

    `fields` are already-validated update deltas (camelCase->column).
    """
    _ensure_tables(dal)
    await get_community(async_dal, dal, community_id=community_id)  # 404 if missing

    community_type = fields.get("community_type")
    if community_type is not None and community_type not in VALID_COMMUNITY_TYPES:
        raise bad_request(
            f"Invalid community type. Must be one of: {', '.join(VALID_COMMUNITY_TYPES)}"
        )
    if not fields:
        raise bad_request("No updates provided")

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


# ── Marketplace module registry ─────────────────────────────────────────


async def get_all_modules(
    async_dal: Any,
    dal: Any,
    *,
    page: int,
    limit: int,
    search: str,
    category: str | None,
    is_published: bool | None,
) -> tuple[list[Any], int, int]:
    """List all modules (including unpublished) -- superadmin registry view."""
    _ensure_tables(dal)
    page = max(1, page)
    limit = min(100, max(1, limit))
    offset = (page - 1) * limit

    query = dal.hub_modules.id > 0
    if search:
        query &= dal.hub_modules.name.like(f"%{search}%", case_sensitive=False) | (
            dal.hub_modules.display_name.like(f"%{search}%", case_sensitive=False)
        )
    if category:
        query &= dal.hub_modules.category == category
    if is_published is not None:
        query &= dal.hub_modules.is_published == is_published

    total = await async_dal.count_async(query)
    rows = await async_dal.select_async(
        dal(query), orderby=~dal.hub_modules.is_core | ~dal.hub_modules.created_at,
        limitby=(offset, offset + limit),
    )

    modules = []
    for m in rows:
        reviews = await async_dal.select_async(dal(dal.hub_module_reviews.module_id == m.id))
        installs = await async_dal.select_async(dal(dal.hub_module_installations.module_id == m.id))
        ratings = [r.rating for r in reviews if r.rating is not None]
        avg_rating = round(sum(ratings) / len(ratings), 1) if ratings else 0.0
        modules.append((m, avg_rating, len(reviews), len(installs)))

    total_pages = math.ceil(total / limit) if limit else 0
    return modules, total, total_pages


async def _provision_module_db_account(
    async_dal: Any, *, name: str, category: str, caller_id: int
) -> dict[str, Any]:
    """Best-effort DB-account provisioning (Postgres-only stored function).

    Mirrors Node's own non-fatal try/catch exactly: `provision_module_db_
    account()` is a Postgres stored procedure (`config/postgres/migrations`)
    -- unavailable against sqlite (tests) and any DB_TYPE other than
    postgresql. Node already treats provisioning failure as non-fatal
    ("Log but don't fail module creation - DB account can be provisioned
    later"); this port preserves that exactly via a blanket except, which
    doubles as graceful degradation on every non-Postgres backend
    (backend-database.md "Support ALL databases") -- module creation
    itself (the pydal insert above) is unaffected either way.
    """
    module_type_map = {
        "general": ("interactive", "interactive_standard"),
        "moderation": ("core", "core_broad"),
        "entertainment": ("interactive", "interactive_standard"),
        "music": ("interactive", "interactive_standard"),
        "utility": ("core", "core_broad"),
        "games": ("interactive", "interactive_standard"),
        "ai": ("core", "core_broad"),
    }
    mod_type, template = module_type_map.get(category, module_type_map["general"])
    db_password = secrets.token_urlsafe(32)
    with contextlib.suppress(Exception):
        rows = await async_dal.executesql_async(
            "SELECT * FROM provision_module_db_account(%s, %s, %s, %s, %s, %s, %s, %s)",
            [name, mod_type, template, db_password, None, None, None, caller_id],
        )
        if rows:
            outcome = rows[0]
            if outcome and outcome[0]:  # success column
                return {"provisioned": True, "username": outcome[1] if len(outcome) > 1 else None}
    return {"provisioned": False, "message": "Provisioning skipped"}


async def create_module(
    async_dal: Any,
    dal: Any,
    *,
    name: str,
    display_name: str | None,
    description: str | None,
    version: str | None,
    author: str | None,
    category: str | None,
    icon_url: str | None,
    is_core: bool | None,
    config_schema: dict[str, Any] | None,
    caller_id: int,
) -> tuple[Any, dict[str, Any]]:
    """Create a new marketplace module + best-effort DB-account provisioning."""
    _ensure_tables(dal)
    if not name:
        raise bad_request("Module name is required")
    existing = await async_dal.select_async(dal(dal.hub_modules.name == name))
    if existing:
        raise conflict("Module name already exists")

    resolved_category = category or "general"
    new_id = await async_dal.insert_async(
        dal.hub_modules,
        name=name,
        display_name=display_name or name,
        description=description or "",
        version=version or "1.0.0",
        author=author or "Waddles",
        category=resolved_category,
        icon_url=icon_url,
        is_core=bool(is_core),
        config_schema=config_schema or {},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    rows = await async_dal.select_async(dal(dal.hub_modules.id == new_id))
    module = rows.first()
    db_account = await _provision_module_db_account(
        async_dal, name=name, category=resolved_category, caller_id=caller_id
    )
    return module, db_account


async def update_module(
    async_dal: Any, dal: Any, *, module_id: int, fields: dict[str, Any]
) -> None:
    """Update a marketplace module."""
    _ensure_tables(dal)
    existing = await async_dal.select_async(dal(dal.hub_modules.id == module_id))
    if not existing:
        raise not_found("Module not found")
    if not fields:
        raise bad_request("No updates provided")
    fields["updated_at"] = datetime.now(UTC)
    await async_dal.update_async(dal.hub_modules.id == module_id, **fields)


async def publish_module(
    async_dal: Any, dal: Any, *, module_id: int, is_published: bool | None
) -> str:
    """Publish/unpublish a module. Returns the module's name for the caller's message."""
    _ensure_tables(dal)
    if is_published is None:
        raise bad_request("isPublished field is required")
    rows = await async_dal.select_async(dal(dal.hub_modules.id == module_id))
    row = rows.first()
    if row is None:
        raise not_found("Module not found")
    await async_dal.update_async(
        dal.hub_modules.id == module_id, is_published=is_published, updated_at=datetime.now(UTC)
    )
    return str(row.name)


async def delete_module(async_dal: Any, dal: Any, *, module_id: int) -> None:
    """Delete a module (blocked if any community has it installed)."""
    _ensure_tables(dal)
    installs = await async_dal.select_async(
        dal(dal.hub_module_installations.module_id == module_id)
    )
    if installs:
        raise bad_request(
            f"Cannot delete module: {len(installs)} installations exist. Unpublish instead."
        )
    rows = await async_dal.select_async(dal(dal.hub_modules.id == module_id))
    if not rows:
        raise not_found("Module not found")
    await async_dal.delete_async(dal.hub_modules.id == module_id)


# ── Tenant management ────────────────────────────────────────────────────

_SLUG_RE_MSG = "Slug must be lowercase alphanumeric with hyphens, no leading/trailing hyphens"


def _valid_slug(slug: str) -> bool:
    if not slug or slug[0] == "-" or slug[-1] == "-":
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
        dal(query), orderby=~dal.tenants.is_global | ~dal.tenants.created_at,
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
    slug = slug.strip()
    if not _valid_slug(slug):
        raise bad_request(_SLUG_RE_MSG)

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
    await async_dal.update_async(dal.tenants.id == tenant_id, **fields)


async def delete_tenant(async_dal: Any, dal: Any, *, tenant_id: int) -> None:
    """Deactivate a tenant. The global tenant cannot be deleted."""
    bind_superadmin_tenant_fields(dal)
    rows = await async_dal.select_async(dal(dal.tenants.id == tenant_id))
    row = rows.first()
    if row is None:
        raise not_found("Tenant not found")
    if row.is_global:
        raise forbidden("Cannot delete the global tenant")
    await async_dal.update_async(
        dal.tenants.id == tenant_id, is_active=False, updated_at=datetime.now(UTC)
    )
