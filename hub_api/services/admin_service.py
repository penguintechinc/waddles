"""Community-scoped platform-admin operations -- ported from `adminController.js`.

Mounted at `/api/v1/admin/<communityId>/*` in Node (`routes/admin.js`),
gated there by `requireAuth` + `requireCommunityAdmin` (super-admin/
tenant-admin/platform-admin bypass, else a per-community `community_members`
role lookup requiring `community:manage_members`/`community:manage_channels`
in the resolved role's `base_claims.scopes`). This port covers the
"system settings" + "platform admin" slice of `adminController.js` --
community settings, per-community module toggles, member/reputation
management, connected-platform aggregation, and the commands reference
list. See `hub_api/PORTING.md` and this module's blueprint
(`blueprints/v1/admin.py`) for the full scope note on what's deliberately
NOT ported here (mirror groups, AI insights/researcher, bot detection,
domains/servers/join-requests, browser sources, translation -- all
community-*feature* surfaces better suited to their own dedicated port).

Auth model (security.md: OIDC scopes only, never role names): every
function here assumes the caller already passed `@tenant_middleware` +
`@require_scope("community:admin")` at the blueprint layer (the
`SCOPE_BUNDLES["community"]["admin"]` bundle -- see `flask_core.auth`).
That is necessary but not sufficient: `community:admin` is a *tenant-wide*
scope grant, so every function additionally calls `_community_in_tenant()`
first -- the tenant-isolation gate preventing a tenant-A admin from
reading/mutating a tenant-B community by guessing its numeric id
(security.md Tenant Isolation; IDOR). `update_member_role()`'s
admin-promotion check additionally re-derives the *caller's own*
`community_members.role` for this specific community (mirroring Node's
`req.communityRole` check) since scope alone doesn't carry per-community
role granularity yet -- see this repo's mem0 note on the Event module
needing the same "invent scope, document the gap" treatment.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
from flask_core.tenancy import TenantContext

from services import community_authz
from services import community_reputation_service as reputation_svc
from services.errors import bad_request, forbidden, not_found
from services.schema import bind_admin_tables

SALT_ROUNDS = 12  # matches adminController.js's own SALT_ROUNDS

VALID_MEMBER_ROLES = ("community-admin", "moderator", "vip", "member")
ADMIN_PROMOTION_ROLES = ("community-owner", "community-admin")


def _ensure_tables(dal: Any) -> None:
    bind_admin_tables(dal)


async def _community_in_tenant(
    async_dal: Any, dal: Any, *, community_id: int, ctx: TenantContext
) -> bool:
    """True iff `community_id` exists and belongs to the caller's validated tenant."""
    rows = await async_dal.select_async(
        dal((dal.communities.id == community_id) & (dal.communities.tenant_id == ctx.tenant_id))
    )
    return bool(rows)


async def _require_community(
    async_dal: Any, dal: Any, *, community_id: int, ctx: TenantContext
) -> None:
    """Raise 404 if `community_id` doesn't exist or isn't in the caller's tenant.

    404 (not 403) deliberately masks whether the community exists at all
    outside the caller's tenant -- the IDOR fix this group's security
    review flagged (task note: "these are ADMIN endpoints, so MISSING
    require_scope / privilege checks are the top risk").
    """
    if not await _community_in_tenant(async_dal, dal, community_id=community_id, ctx=ctx):
        raise not_found("Community not found")


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


# ── Community settings ──────────────────────────────────────────────────


async def get_community_settings(
    async_dal: Any, dal: Any, *, community_id: int, ctx: TenantContext
) -> Any:
    """Get community settings."""
    _ensure_tables(dal)
    await _require_community(async_dal, dal, community_id=community_id, ctx=ctx)
    rows = await async_dal.select_async(dal(dal.communities.id == community_id))
    row = rows.first()
    if row is None:
        raise not_found("Community not found")
    return row


async def update_community_settings(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    ctx: TenantContext,
    display_name: str | None,
    description: str | None,
    is_public: bool | None,
    join_mode: str | None,
    channel_creation_policy: str | None,
    config: dict[str, Any] | None,
) -> None:
    """Update community settings."""
    _ensure_tables(dal)
    await _require_community(async_dal, dal, community_id=community_id, ctx=ctx)

    valid_join_modes = ("open", "approval", "invite")
    valid_policies = ("admin_only", "communicator", "all_members")
    if join_mode is not None and join_mode not in valid_join_modes:
        raise bad_request("Invalid join mode")
    if channel_creation_policy is not None and channel_creation_policy not in valid_policies:
        raise bad_request("Invalid channel creation policy")

    update_fields: dict[str, Any] = {}
    if display_name is not None:
        update_fields["display_name"] = display_name
    if description is not None:
        update_fields["description"] = description
    if is_public is not None:
        update_fields["is_public"] = is_public
    if join_mode is not None:
        update_fields["join_mode"] = join_mode

    # pydal has no portable JSONB-merge operator across DB_TYPE backends
    # (Postgres `||` is Postgres-only) -- merge in Python instead, which
    # works identically against every backend (backend-database.md
    # "Support ALL databases").
    config_delta: dict[str, Any] = dict(config) if config is not None else {}
    if channel_creation_policy is not None:
        config_delta["channel_creation_policy"] = channel_creation_policy
    if config_delta:
        current = await get_community_settings(async_dal, dal, community_id=community_id, ctx=ctx)
        merged = dict(current.config or {})
        merged.update(config_delta)
        update_fields["config"] = merged

    if not update_fields:
        raise bad_request("No updates provided")

    update_fields["updated_at"] = datetime.now(UTC)
    await async_dal.update_async(dal.communities.id == community_id, **update_fields)


# ── Per-community module toggles ────────────────────────────────────────


async def get_modules(
    async_dal: Any, dal: Any, *, community_id: int, ctx: TenantContext
) -> list[Any]:
    """Get installed modules for a community."""
    _ensure_tables(dal)
    await _require_community(async_dal, dal, community_id=community_id, ctx=ctx)
    rows = await async_dal.select_async(
        dal(dal.module_installations.community_id == community_id),
        orderby=dal.module_installations.module_id,
    )
    results = []
    for install in rows:
        mod = None
        if install.module_id and install.module_id.isdigit():
            mod_rows = await async_dal.select_async(dal(dal.modules.id == int(install.module_id)))
            mod = mod_rows.first()
        results.append((install, mod))
    return results


async def update_module_config(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    module_id: int,
    ctx: TenantContext,
    config: dict[str, Any] | None,
    is_enabled: bool | None,
) -> None:
    """Update a community's module configuration."""
    _ensure_tables(dal)
    await _require_community(async_dal, dal, community_id=community_id, ctx=ctx)

    if is_enabled is False:
        core_rows = await async_dal.select_async(dal(dal.hub_modules.id == module_id))
        core_row = core_rows.first()
        if core_row is not None and bool(core_row.is_core):
            raise forbidden(
                "Cannot disable a core module. This module is required for "
                "WaddleBot to function correctly."
            )

    update_fields: dict[str, Any] = {}
    if config is not None:
        update_fields["config"] = config
    if is_enabled is not None:
        update_fields["is_enabled"] = is_enabled
    if not update_fields:
        raise bad_request("No updates provided")
    update_fields["updated_at"] = datetime.now(UTC)

    query = (dal.module_installations.community_id == community_id) & (
        dal.module_installations.module_id == str(module_id)
    )
    existing = await async_dal.select_async(dal(query))
    if not existing:
        raise not_found("Module installation not found")
    await async_dal.update_async(query, **update_fields)


# ── Member management ───────────────────────────────────────────────────


async def get_members(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    ctx: TenantContext,
    page: int,
    limit: int,
    search: str,
    role: str | None,
) -> tuple[list[Any], int, int]:
    """Get community members."""
    _ensure_tables(dal)
    await _require_community(async_dal, dal, community_id=community_id, ctx=ctx)
    page = max(1, page)
    limit = min(100, max(1, limit))
    offset = (page - 1) * limit

    query = (dal.community_members.community_id == community_id) & (
        dal.community_members.is_active == True  # noqa: E712 - pydal Field comparison
    )
    if role:
        query &= dal.community_members.role == role

    total = await async_dal.count_async(query)
    reputation_svc._ensure_reputation_tables(dal)  # noqa: SLF001 - same-package binder
    scores = dal.bundle_reputation_scores
    rows = await async_dal.select_async(
        dal(query),
        dal.community_members.ALL,
        scores.balance,
        left=scores.on(
            (scores.community_id == dal.community_members.community_id)
            & (scores.user_uuid == dal.community_members.user_uuid)
        ),
        orderby=~scores.balance.coalesce(reputation_svc.DEFAULT_SCORE)
        | dal.community_members.joined_at,
        limitby=(offset, offset + limit),
    )

    members = []
    for joined in rows:
        member = joined.community_members
        balance = joined.bundle_reputation_scores.balance
        member.reputation = int(balance) if balance is not None else reputation_svc.DEFAULT_SCORE
        user_rows = None
        if member.user_id and member.user_id.isdigit():
            user_rows = await async_dal.select_async(dal(dal.hub_users.id == int(member.user_id)))
        user = user_rows.first() if user_rows else None
        if search:
            haystack = f"{getattr(user, 'username', '') or ''} {getattr(user, 'email', '') or ''}"
            if search.lower() not in haystack.lower():
                continue
        members.append((member, user))

    total_pages = -(-total // limit) if limit else 0
    return members, total, total_pages


async def _caller_community_role(
    async_dal: Any, dal: Any, *, community_id: int, caller_id: int | None
) -> str | None:
    if caller_id is None:
        return None
    rows = await async_dal.select_async(
        dal(
            (dal.community_members.community_id == community_id)
            & (dal.community_members.user_id == str(caller_id))
            & (dal.community_members.is_active == True)  # noqa: E712
        )
    )
    row = rows.first()
    return row.role if row else None


async def update_member_role(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    user_id: int,
    ctx: TenantContext,
    role: str,
    caller_id: int | None,
) -> None:
    """Update a member's role."""
    _ensure_tables(dal)
    await _require_community(async_dal, dal, community_id=community_id, ctx=ctx)

    if role not in VALID_MEMBER_ROLES:
        raise bad_request("Invalid role")

    target_rows = await async_dal.select_async(
        dal(
            (dal.community_members.community_id == community_id)
            & (dal.community_members.user_id == str(user_id))
            & (dal.community_members.is_active == True)  # noqa: E712
        )
    )
    target = target_rows.first()
    if target is None:
        raise not_found("Member not found")
    if target.role == "community-owner":
        raise forbidden("Cannot change owner role")

    if role == "community-admin":
        caller_role = await _caller_community_role(
            async_dal, dal, community_id=community_id, caller_id=caller_id
        )
        if caller_role not in ADMIN_PROMOTION_ROLES:
            raise forbidden("Only owners can promote to admin")

    role_rows = await async_dal.select_async(
        dal((dal.community_roles.community_id == community_id) & (dal.community_roles.name == role))
    )
    role_row = role_rows.first()
    community_role_id = role_row.id if role_row else None

    await async_dal.update_async(
        (dal.community_members.community_id == community_id)
        & (dal.community_members.user_id == str(user_id)),
        role=role,
        community_role_id=community_role_id,
        claims_cache=None,
        updated_at=datetime.now(UTC),
    )


async def authorize_reputation_admin(
    async_dal: Any, dal: Any, request: Any, *, community_id: int, ctx: TenantContext
) -> None:
    """404 unless the community is in the caller's tenant, then 403 unless they admin it.

    The reputation-config routes are for COMMUNITY admins (a DB-backed per-community role, not
    the flat `community:admin` JWT scope, which is never granted to them at login) -- so this
    uses `community_authz.require_community_admin` (super-admin / tenant-admin-of-this-tenant /
    a member whose role carries an admin scope in this exact community).
    """
    _ensure_tables(dal)
    await _require_community(async_dal, dal, community_id=community_id, ctx=ctx)
    await community_authz.require_community_admin(
        async_dal, dal, request, community_id=community_id
    )


async def adjust_reputation(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    user_id: int,
    ctx: TenantContext,
    amount: float | None,
    set_to: float | None,
    actor_user_id: int | None = None,
) -> tuple[int, int | None]:
    """Adjust (or set) a member's reputation. Returns (new_score, change_or_None).

    The write goes through the bundle store's atomic `reputation_apply()` (clamp, ledger row with
    an `admin:` actor); see `community_reputation_service.override_member_reputation`.
    """
    _ensure_tables(dal)
    await _require_community(async_dal, dal, community_id=community_id, ctx=ctx)

    query = (dal.community_members.community_id == community_id) & (
        dal.community_members.user_id == str(user_id)
    )
    rows = await async_dal.select_async(dal(query), dal.community_members.id)
    if rows.first() is None:
        raise not_found("Member not found")

    return await reputation_svc.override_member_reputation(
        async_dal,
        dal,
        tenant_id=ctx.tenant_id,
        community_id=community_id,
        user_id=user_id,
        amount=amount,
        set_to=set_to,
        actor_user_id=actor_user_id,
    )


async def remove_member(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    user_id: int,
    ctx: TenantContext,
    caller_id: int | None,
    reason: str | None,
) -> None:
    """Deactivate a community member."""
    _ensure_tables(dal)
    await _require_community(async_dal, dal, community_id=community_id, ctx=ctx)

    query = (
        (dal.community_members.community_id == community_id)
        & (dal.community_members.user_id == str(user_id))
        & (dal.community_members.is_active == True)  # noqa: E712
    )
    rows = await async_dal.select_async(dal(query))
    target = rows.first()
    if target is None:
        raise not_found("Member not found")
    if target.role == "community-owner":
        raise forbidden("Cannot remove owner")

    await async_dal.update_async(
        query,
        is_active=False,
        removed_at=datetime.now(UTC),
        removed_by=caller_id,
        removal_reason=reason,
    )
    community_rows = await async_dal.select_async(dal(dal.communities.id == community_id))
    community = community_rows.first()
    if community is not None:
        await async_dal.update_async(
            dal.communities.id == community_id,
            member_count=max(0, (community.member_count or 0) - 1),
        )


async def generate_temp_password(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    ctx: TenantContext,
    user_identifier: str | None,
    force_oauth_link: bool,
    expires_in_hours: int,
    caller_id: int | None,
) -> tuple[str, datetime]:
    """Generate a one-time temp password for a community member."""
    _ensure_tables(dal)
    await _require_community(async_dal, dal, community_id=community_id, ctx=ctx)

    if not user_identifier:
        raise bad_request("User identifier required")

    temp_password = "-".join(str(uuid.uuid4()).split("-")[:2])
    password_hash = bcrypt.hashpw(
        temp_password.encode(), bcrypt.gensalt(rounds=SALT_ROUNDS)
    ).decode()
    expires_at = datetime.now(UTC) + timedelta(hours=expires_in_hours)

    await async_dal.insert_async(
        dal.hub_temp_passwords,
        community_id=community_id,
        user_identifier=user_identifier,
        password_hash=password_hash,
        expires_at=expires_at,
        force_oauth_link=force_oauth_link,
        created_at=datetime.now(UTC),
    )
    return temp_password, expires_at


# ── Read-only aggregations ──────────────────────────────────────────────


async def get_connected_platforms(
    async_dal: Any, dal: Any, *, community_id: int, ctx: TenantContext
) -> list[dict[str, Any]]:
    """Aggregate connected platforms for a community from `community_servers`."""
    _ensure_tables(dal)
    await _require_community(async_dal, dal, community_id=community_id, ctx=ctx)
    rows = await async_dal.select_async(
        dal(dal.community_servers.community_id == community_id),
        orderby=dal.community_servers.platform,
    )
    by_platform: dict[str, dict[str, Any]] = {}
    for server in rows:
        entry = by_platform.setdefault(
            server.platform, {"platform": server.platform, "serverCount": 0, "isActive": False}
        )
        entry["serverCount"] += 1
        if server.status == "approved":
            entry["isActive"] = True
    return [by_platform[p] for p in sorted(by_platform)]


async def get_commands(
    async_dal: Any, dal: Any, *, community_id: int, ctx: TenantContext
) -> list[dict[str, Any]]:
    """Get the commands reference list (community-specific + global defaults).

    A command is enabled if its module is installed+enabled for this
    community, or if no installation record exists at all (module not
    installed -> defaults to enabled) -- matches Node's
    `COALESCE(hmi.is_enabled, true)` exactly.
    """
    _ensure_tables(dal)
    await _require_community(async_dal, dal, community_id=community_id, ctx=ctx)
    command_rows = await async_dal.select_async(
        dal((dal.commands.community_id == community_id) | (dal.commands.community_id == None))  # noqa: E711
    )
    installs = await async_dal.select_async(
        dal(dal.hub_module_installations.community_id == community_id)
    )
    enabled_by_module = {i.module_name: bool(i.is_enabled) for i in installs if i.module_name}

    return [
        {
            "id": c.id,
            "command": c.command,
            "module_name": c.module_name,
            "description": c.description,
            "category": c.category,
            "permission_level": c.permission_level,
            "platforms": c.platforms,
            "is_enabled": enabled_by_module.get(c.module_name, True),
        }
        for c in sorted(command_rows, key=lambda c: (c.module_name, c.command))
    ]
