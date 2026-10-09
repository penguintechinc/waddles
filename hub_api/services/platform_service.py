"""Platform-wide admin business logic -- ported from `platformController.js`.

Cross-tenant by design (Node's own comment: "Platform-wide admin
features") -- gated behind `require_scope("platform:admin")` in
`blueprints/v1/platform.py`, which only a global-admin-bundle token
(`*:admin` wildcard, granted for `hub_users.is_super_admin`) can satisfy
today (see that blueprint's module docstring for why). A caller who
clears that bar is, by definition, a global admin -- seeing across every
tenant is the intended behavior here, not a tenant-isolation gap (mirrors
security.md Tenant Isolation's own carve-out: "Admin tokens also
tenant-scoped (except super-admin)"). Contrast with `public_service.py`,
where the SAME cross-tenant `communities` query would leak to an
unauthenticated caller and is deliberately scoped there.

pydal query builder only (never raw SQL) -- see `hub_api/PORTING.md`
Gotcha #1 (`async_dal`'s raw-SQL helpers are Postgres-only, `%s`-only
placeholders 500 against sqlite).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

from services.errors import bad_request, not_found

_VALID_ROLES: tuple[str | None, ...] = ("platform-admin", "support", None)


async def list_users(
    async_dal: Any, dal: Any, *, page: int, limit: int, search: str, platform: str | None
) -> tuple[list[Any], int, int]:
    """List distinct platform users from `community_members` (any community, any platform)."""
    page = max(1, page)
    limit = min(100, max(1, limit))
    offset = (page - 1) * limit

    query = dal.community_members.id > 0
    if search:
        query &= dal.community_members.display_name.like(
            f"%{search}%", case_sensitive=False
        ) | dal.community_members.platform_user_id.like(f"%{search}%", case_sensitive=False)
    if platform:
        query &= dal.community_members.platform == platform

    # Node: `SELECT DISTINCT ON (user_id) ... ORDER BY user_id, last_activity DESC`.
    # pydal has no DISTINCT ON, and `AsyncDAL.count_async`/`select_async`
    # (flask_core.database) take no `distinct=<Field>` kwarg -- only a plain
    # `distinct=True` (distinct across every selected column), so this is an
    # approximation: a user_id with genuinely differing display_name/
    # platform/platform_user_id across communities can appear more than
    # once, unlike Node's exact-one-row-per-user_id semantics. Acceptable
    # approximation for an admin listing endpoint -- flagged here rather
    # than silently claimed exact.
    fields = (
        dal.community_members.user_id,
        dal.community_members.display_name,
        dal.community_members.platform,
        dal.community_members.platform_user_id,
        dal.community_members.created_at,
        dal.community_members.last_activity,
    )
    distinct_user_ids = await async_dal.select_async(
        dal(query), dal.community_members.user_id, distinct=True
    )
    total = len(distinct_user_ids)
    rows = await async_dal.select_async(
        dal(query),
        *fields,
        distinct=True,
        orderby=dal.community_members.user_id | ~dal.community_members.last_activity,
        limitby=(offset, offset + limit),
    )
    total_pages = math.ceil(total / limit) if limit else 0
    return list(rows), total, total_pages


async def get_user(async_dal: Any, dal: Any, *, user_id: int) -> dict[str, Any]:
    """Get a platform user's memberships + platform-admin status across all communities."""
    memberships = await async_dal.select_async(
        dal(
            (dal.community_members.user_id == str(user_id))
            & (dal.community_members.is_active == True)  # noqa: E712
        ),
        dal.community_members.ALL,
        dal.communities.ALL,
        left=dal.communities.on(dal.community_members.community_id == dal.communities.id),
    )
    if not memberships:
        raise not_found("User not found")

    first = memberships[0].community_members
    admin_rows = await async_dal.select_async(
        dal(
            (dal.platform_admins.user_id == user_id) & (dal.platform_admins.is_active == True)  # noqa: E712
        )
    )
    return {
        "user_id": user_id,
        "display_name": first.display_name,
        "platform": first.platform,
        "platform_user_id": first.platform_user_id,
        "is_platform_admin": bool(admin_rows),
        "platform_role": admin_rows.first().role if admin_rows else None,
        "memberships": [
            {
                "community_id": row.community_members.community_id,
                "community_name": row.communities.name if row.communities else None,
                "role": row.community_members.role,
                "reputation_score": row.community_members.reputation or 0,
                "joined_at": row.community_members.joined_at,
                "last_activity": row.community_members.last_activity,
            }
            for row in memberships
        ],
    }


async def update_user_role(async_dal: Any, dal: Any, *, user_id: int, role: str | None) -> None:
    """Update (or clear) a user's platform-admin role -- upsert into `platform_admins`."""
    if role not in _VALID_ROLES:
        raise bad_request("Invalid role")

    if role is None:
        await async_dal.update_async(
            dal.platform_admins.user_id == user_id,
            is_active=False,
            deactivated_at=datetime.now(UTC),
        )
        return

    existing = await async_dal.select_async(dal(dal.platform_admins.user_id == user_id))
    if existing:
        await async_dal.update_async(
            dal.platform_admins.user_id == user_id,
            role=role,
            is_active=True,
            updated_at=datetime.now(UTC),
        )
    else:
        await async_dal.insert_async(
            dal.platform_admins,
            user_id=user_id,
            role=role,
            is_active=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )


async def deactivate_user(
    async_dal: Any, dal: Any, *, user_id: int, reason: str | None, actor_id: int
) -> None:
    """Deactivate a user across every community + revoke sessions + platform-admin role."""
    await async_dal.update_async(
        dal.community_members.user_id == str(user_id),
        is_active=False,
    )
    await async_dal.update_async(
        dal.platform_admins.user_id == user_id,
        is_active=False,
        deactivated_at=datetime.now(UTC),
    )
    await async_dal.update_async(
        dal.hub_sessions.user_id == user_id,
        is_active=False,
        revoked_at=datetime.now(UTC),
    )


async def list_communities(
    async_dal: Any, dal: Any, *, page: int, limit: int, search: str, is_active: bool
) -> tuple[list[Any], int, int]:
    """List every community platform-wide (no tenant filter -- see module docstring)."""
    page = max(1, page)
    limit = min(100, max(1, limit))
    offset = (page - 1) * limit

    # `id > 0` excludes the tenant-wide sentinel community (id 0), incl. the is_active=False path.
    query = (dal.communities.is_active == is_active) & (dal.communities.id > 0)
    if search:
        query &= dal.communities.name.like(
            f"%{search}%", case_sensitive=False
        ) | dal.communities.display_name.like(f"%{search}%", case_sensitive=False)

    total = await async_dal.count_async(query)
    rows = await async_dal.select_async(
        dal(query),
        orderby=~dal.communities.member_count | ~dal.communities.created_at,
        limitby=(offset, offset + limit),
    )
    total_pages = math.ceil(total / limit) if limit else 0
    return list(rows), total, total_pages


async def get_community(async_dal: Any, dal: Any, *, community_id: int) -> dict[str, Any]:
    """Get a single community's admin-view detail: owner, module count, domain count."""
    rows = await async_dal.select_async(dal(dal.communities.id == community_id))
    if not rows:
        raise not_found("Community not found")
    community = rows.first()

    owner_rows = await async_dal.select_async(
        dal(
            (dal.community_members.community_id == community_id)
            & (dal.community_members.role == "community-owner")
            & (dal.community_members.is_active == True)  # noqa: E712
        ),
        limitby=(0, 1),
    )
    owner = owner_rows.first() if owner_rows else None

    module_count = await async_dal.count_async(
        dal.hub_module_installations.community_id == community_id
    )

    return {
        "community": community,
        "owner": owner,
        "module_count": module_count,
        # community_domains isn't bound by this group (owned by the
        # Tenancy/community-entity group's own port); Node's own
        # domain_count is 0 for any community with no bound table to
        # query here yet, rather than a fabricated non-zero value.
        "domain_count": 0,
    }


async def update_community(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    display_name: str | None,
    description: str | None,
    is_public: bool | None,
    is_active: bool | None,
) -> None:
    """Admin-override update of a community's public-facing fields."""
    updates: dict[str, Any] = {}
    if display_name is not None:
        updates["display_name"] = display_name
    if description is not None:
        updates["description"] = description
    if is_public is not None:
        updates["is_public"] = is_public
    if is_active is not None:
        updates["is_active"] = is_active
    if not updates:
        raise bad_request("No updates provided")

    existing = await async_dal.select_async(dal(dal.communities.id == community_id))
    if not existing:
        raise not_found("Community not found")

    updates["updated_at"] = datetime.now(UTC)
    await async_dal.update_async(dal.communities.id == community_id, **updates)


async def deactivate_community(
    async_dal: Any, dal: Any, *, community_id: int, reason: str | None
) -> None:
    """Deactivate a community platform-wide."""
    existing = await async_dal.select_async(dal(dal.communities.id == community_id))
    if not existing:
        raise not_found("Community not found")
    await async_dal.update_async(
        dal.communities.id == community_id,
        is_active=False,
        updated_at=datetime.now(UTC),
    )


async def get_system_health(async_dal: Any, dal: Any) -> tuple[bool, bool]:
    """Return `(database_ok, healthy)` -- a trivial connectivity probe."""
    try:
        await async_dal.count_async(dal.communities.id > 0)
        database_ok = True
    except Exception:  # noqa: BLE001 - any DB failure means "unhealthy", not a 500
        database_ok = False
    return database_ok, database_ok


async def get_module_registry(async_dal: Any, dal: Any) -> list[Any]:
    """List every registered collector module."""
    rows = await async_dal.select_async(
        dal(dal.collector_modules.id > 0), orderby=dal.collector_modules.module_name
    )
    return list(rows)


async def get_audit_log(
    async_dal: Any,
    dal: Any,
    *,
    page: int,
    limit: int,
    action: str | None,
    user_id: int | None,
) -> tuple[list[Any], int, int]:
    """List audit log entries, newest first."""
    page = max(1, page)
    limit = min(100, max(1, limit))
    offset = (page - 1) * limit

    query = dal.audit_log.id > 0
    if action:
        query &= dal.audit_log.action == action
    if user_id:
        query &= dal.audit_log.user_id == user_id

    total = await async_dal.count_async(query)
    rows = await async_dal.select_async(
        dal(query), orderby=~dal.audit_log.created_at, limitby=(offset, offset + limit)
    )
    total_pages = math.ceil(total / limit) if limit else 0
    return list(rows), total, total_pages


async def get_stats(async_dal: Any, dal: Any) -> dict[str, Any]:
    """Platform-wide usage stats: users, communities, per-platform breakdown, sessions."""
    active_members_query = dal.community_members.is_active == True  # noqa: E712
    distinct_users = await async_dal.select_async(
        dal(active_members_query), dal.community_members.user_id, distinct=True
    )
    total_users = len(distinct_users)

    now = datetime.now(UTC)
    active_7d = await async_dal.count_async(
        active_members_query & (dal.community_members.last_activity > now - timedelta(days=7))
    )
    active_30d = await async_dal.count_async(
        active_members_query & (dal.community_members.last_activity > now - timedelta(days=30))
    )

    total_communities = await async_dal.count_async(dal.communities.is_active == True)  # noqa: E712
    public_communities = await async_dal.count_async(
        (dal.communities.is_active == True) & (dal.communities.is_public == True)  # noqa: E712
    )
    community_rows = await async_dal.select_async(dal(dal.communities.is_active == True))  # noqa: E712
    total_members = sum(row.member_count or 0 for row in community_rows)

    # Distinct (platform, user_id) pairs, counted per platform in Python --
    # see list_users()'s docstring note on AsyncDAL.select_async's plain
    # `distinct=True` (no per-field distinct) limitation.
    platform_user_rows = await async_dal.select_async(
        dal(active_members_query),
        dal.community_members.platform,
        dal.community_members.user_id,
        distinct=True,
    )
    platforms: dict[str, int] = {}
    for row in platform_user_rows:
        if row.platform:
            platforms[row.platform] = platforms.get(row.platform, 0) + 1

    total_sessions = await async_dal.count_async(
        dal.hub_sessions.created_at > now - timedelta(hours=24)
    )
    active_sessions = await async_dal.count_async(
        (dal.hub_sessions.created_at > now - timedelta(hours=24))
        & (dal.hub_sessions.is_active == True)  # noqa: E712
    )

    return {
        "users": {"total": total_users, "active_7d": active_7d, "active_30d": active_30d},
        "communities": {
            "total": total_communities,
            "public": public_communities,
            "total_members": total_members,
        },
        "platforms": platforms,
        "sessions": {"last_24h": total_sessions, "active": active_sessions},
    }
