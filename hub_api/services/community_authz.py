"""Per-community authorization -- DB-backed, mirrors Node's `requireCommunityAdmin`.

`flask_core.authz.require_scope` checks a flat JWT `scope` claim -- correct
for tenant-level gates, but wrong here: `flask_core.auth.SCOPE_BUNDLES
["community"]["admin"]` is never granted at JWT-issuance time anywhere in
this port (`auth_service.create_session_token` only ever grants the
`global`/`tenant` bundles), and even if it were, a flat scope claim can't
answer "admin of *which* community" -- using it alone for a `communityId`
URL path param would let any caller holding a broad community-shaped scope
act on a community they don't actually belong to. That is the exact IDOR
class this module exists to prevent (see `hub_api/PORTING.md`'s "SECURITY"
note on this port: "IDOR from request-param community_id instead of JWT").

Node's real authorization for these routes
(`middleware/auth.js::requireCommunityAdmin`/`requireMember`) is a live DB
lookup -- `community_members` JOIN `community_roles` on
`(community_id, user_id)`, `is_active = true` -- never the JWT scope claim
alone. This module ports that check faithfully: `community_id` always
comes from the URL (as Node's own routes take it), but the caller's
*membership and role in that specific community* is resolved from the
database using `user_id` sourced from the validated JWT
(`services.current_user.get_current_user_id`), never trusted from the
request itself. A caller with no active membership row for that exact
`community_id`, or an active membership whose role doesn't carry one of
the required scopes, is rejected -- regardless of what scope claims their
JWT carries for *other* communities or the tenant at large.

Scope strings (`community:manage_members`, `community:manage_channels`,
...) come from `community_roles.base_claims.scopes`
(`config/postgres/migrations/058_tenants_and_claims.sql`'s
`seed_community_system_roles()`), not from `flask_core.auth.SCOPE_BUNDLES`
-- a different, DB-seeded vocabulary Node's own controllers already use,
kept as-is rather than remapped to the (unrelated, unwired-for-community)
JWT bundle names.

The M2 Core Tenancy-Misc group's own scope this module originally shipped
under explicitly did NOT cover Node's tenant-admin/platform-admin bypasses
in `requireCommunityAdmin` (`req.isTenantAdmin`, `req.user.roles.includes
('platform-admin')`) -- only the global `hub_users.is_super_admin` bypass
(`is_super_admin`/`require_community_scope` below). The M-automation group
(workflow/github_sync port) needs the FULL set of bypasses
`routes/workflow.js`/`routes/githubSync.js` actually ship with in Node, so
`require_community_admin`/`require_valid_community_id` below port those
remaining two bypasses (tenant-admin via `tenant_admins`, platform-admin via
the JWT `roles` claim) faithfully, as a second, independent entry point
rather than reworking `require_community_scope`'s own (still relied upon by
`community_profile_service.py`/`join_request_service.py`) narrower
contract. `roles` is an audit/display claim per security.md, but these are
Node's ALREADY-SHIPPED authz decisions, not a new role-based check invented
by this port; flagged here rather than silently dropped or silently kept
without comment.

The M7 Streaming group needed a THIRD variant for `blueprints/v1/music.py`/
`streaming.py`: same bypass chain as `require_community_admin` above
(super-admin/platform-admin/tenant-admin), but ALSO re-validates that the
target `community_id` actually belongs to the caller's own tenant
(`communities.tenant_id`) before ever checking membership -- Node's model
never re-validated this, but per security.md Tenant Isolation this port
closes that gap for the music/streaming write paths specifically. Rather
than change `require_community_admin`'s already-relied-upon (`workflow.py`/
`github_sync.py`) contract, this group's own entry points are suffixed
`_scoped` throughout (`require_community_admin_scoped`, its `requireMember`
counterpart `require_community_member_scoped`, and the `resolve_
community_membership_scoped()`/`authorize_community()` machinery backing
both) -- same "second, independent entry point" precedent the M-automation
paragraph above already established, not a third overlapping implementation
invented for its own sake.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from flask_core.auth import verify_jwt_token
from flask_core.secrets import require_secret_key
from flask_core.tenancy import TenantContext, get_tenant_context
from quart import Request

from services.current_user import get_current_user_id
from services.errors import ApiError, forbidden, unauthorized
from services.global_identity import is_global_admin
from services.schema import bind_community_authz_tables


@dataclass(slots=True, frozen=True)
class CommunityRole:
    """The caller's resolved role + granted scopes for one specific community."""

    name: str
    priority: int
    scopes: frozenset[str]


async def is_super_admin(async_dal: Any, dal: Any, *, user_id: int) -> bool:
    """True iff `user_id` is a tenant admin of the reserved `system` tenant (global admin).

    THE canonical super-admin check -- DB-authoritative, per security.md's
    "authz decisions on scope [or, here, a dedicated DB flag], never role
    names/claims" (the JWT `roles` claim is audit/display only). Public
    (no leading underscore) because `services/community_access.py` also
    needs this exact check for its own `require_community_admin`/
    `require_community_member` super-admin bypass -- that module
    previously carried its own independent reimplementation that decoded
    the bearer JWT's `roles` claim directly (`"super_admin" in roles`), a
    security.md violation this consolidation also fixes: a claim can go
    stale relative to a since-revoked `hub_users.is_super_admin` flag
    within the token's validity window, where this DB lookup cannot.
    """
    return await is_global_admin(async_dal, dal, user_id=user_id)


async def get_caller_community_role(
    async_dal: Any, dal: Any, *, community_id: int, user_id: int
) -> CommunityRole | None:
    """Resolve the caller's active `community_members` row + its `community_roles` scopes.

    Mirrors `middleware/auth.js`'s `requireCommunityAdmin`/`requireMember`
    query (LEFT JOIN on `community_role_id`, `is_active = true`).
    `community_members.user_id` is a legacy VARCHAR column (see
    `services/schema.py`'s own note on that field) -- compared as a string
    here to match how Node's pg driver serializes it and how every other
    `community_members.user_id` query in this port already does.

    Returns `None` if the caller has no active membership row for this
    community at all (never raises -- callers decide whether "no role" is
    an error).
    """
    rows = await async_dal.select_async(
        dal(
            (dal.community_members.community_id == community_id)
            & (dal.community_members.user_id == str(user_id))
            & (dal.community_members.is_active == True)  # noqa: E712
        ),
        dal.community_members.ALL,
        dal.community_roles.ALL,
        left=dal.community_roles.on(
            dal.community_members.community_role_id == dal.community_roles.id
        ),
    )
    if not rows:
        return None
    cm, cr = rows[0].community_members, rows[0].community_roles
    base_claims = cr.base_claims if cr is not None else None
    scopes = base_claims.get("scopes", []) if isinstance(base_claims, dict) else []
    role_name = (cr.name if cr is not None and cr.name else None) or cm.role or "member"
    priority = (cr.priority if cr is not None and cr.priority is not None else 0) or 0
    return CommunityRole(name=role_name, priority=priority, scopes=frozenset(scopes))


async def require_community_scope(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    user_id: int,
    any_of: tuple[str, ...],
) -> CommunityRole:
    """Require the caller hold at least one of `any_of` scopes for `community_id`.

    Global super admins bypass (`hub_users.is_super_admin`, DB-authoritative
    -- mirrors Node's `req.user?.isSuperAdmin` bypass). Fails closed: no
    active membership, or a membership whose role grants none of the
    required scopes, is a 403 (`forbidden()`) -- never a silent pass. This
    is the ONLY place `community_id` from a URL param is trusted to mean
    anything -- it is always cross-checked against a real
    `community_members` row for the caller's own `user_id`, never assumed.
    """
    if await is_super_admin(async_dal, dal, user_id=user_id):
        return CommunityRole(name="super-admin", priority=999, scopes=frozenset(any_of))

    role = await get_caller_community_role(
        async_dal, dal, community_id=community_id, user_id=user_id
    )
    if role is None or not (role.scopes & set(any_of)):
        raise forbidden("Community admin access required")
    return role


#: Node's `requireCommunityAdmin` accepts either scope (`community:manage_members`
#: OR `community:manage_channels`) as sufficient for "community admin".
_ADMIN_SCOPES = frozenset({"community:manage_members", "community:manage_channels"})


async def require_community_admin(
    async_dal: Any, dal: Any, request: Request, *, community_id: int
) -> None:
    """Raise :class:`ApiError` (401/403) unless the caller admins `community_id`.

    Must run after `tenant_middleware` (needs `request.tenant_context` for
    the tenant-admin bypass) -- matches the M-automation group's own Auth
    pattern entry in `hub_api/PORTING.md` ("Admin/elevated action":
    `tenant_middleware` + a scope/authz check). Unlike `require_community_scope`
    above (M2's narrower, still-in-use contract), this entry point also
    ports Node's tenant-admin and platform-admin bypasses -- see this
    module's own top-of-file docstring for why the two coexist.

    SECURITY: the tenant-admin bypass additionally cross-checks that
    `community_id` actually belongs to `ctx.tenant_id` via
    `community_belongs_to_tenant()` -- a `tenant_admins` row only proves
    the caller administers their own tenant, never that the *target*
    `community_id` in the URL also belongs to it. Without this check a
    tenant-A admin passing a tenant-B `community_id` would pass the bypass
    (cross-tenant IDOR). A cross-tenant match falls through to the
    platform-admin check and then the real per-community role lookup below
    -- same "not authorized for this community" `forbidden()` either path
    produces, never a distinct error that would leak the community exists
    under another tenant.
    """
    bind_community_authz_tables(dal)

    user_id = get_current_user_id(request)  # raises 401 if missing/invalid

    if await is_super_admin(async_dal, dal, user_id=user_id):
        return

    ctx: TenantContext | None = get_tenant_context(request)
    if ctx is not None:
        ta_rows = await async_dal.select_async(
            dal(
                (dal.tenant_admins.tenant_id == ctx.tenant_id)
                & (dal.tenant_admins.user_id == user_id)
            )
        )
        if ta_rows and await community_belongs_to_tenant(
            async_dal, dal, community_id=community_id, tenant_id=ctx.tenant_id
        ):
            return

    if "platform-admin" in _jwt_roles(request):
        return

    role = await get_caller_community_role(
        async_dal, dal, community_id=community_id, user_id=user_id
    )
    if role is None or not (role.scopes & _ADMIN_SCOPES):
        raise forbidden("Community admin access required")


def _jwt_roles(request: Request) -> frozenset[str]:
    """Return the bearer JWT's `roles` claim as a set -- empty if absent/malformed."""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return frozenset()
    payload = verify_jwt_token(auth_header[7:], require_secret_key())
    if payload is None:
        return frozenset()
    roles = payload.get("roles")
    if not isinstance(roles, list):
        return frozenset()
    return frozenset(str(r) for r in roles)


def require_valid_community_id(raw: str) -> int:
    """Parse a path-param community id, raising 400 (matching Node's `isNaN` guard) on failure."""
    try:
        community_id = int(raw)
    except (TypeError, ValueError) as exc:
        raise ApiError("Invalid community ID", 400, "BAD_REQUEST") from exc
    return community_id


@dataclass(slots=True)
class CommunityMembership:
    """The caller's resolved role/scopes for one specific community (M7 `_scoped` variant)."""

    role: str
    scopes: frozenset[str]
    is_admin: bool
    bypass: bool


def _parse_claims_scopes(raw: Any) -> frozenset[str]:
    """Port of Node's claims_cache/base_claims parsing (array or `{scopes: [...]}` shape)."""
    if raw is None:
        return frozenset()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return frozenset()
    if isinstance(raw, list):
        return frozenset(str(s) for s in raw)
    if isinstance(raw, dict):
        return frozenset(str(s) for s in raw.get("scopes", []))
    return frozenset()


async def _is_tenant_admin(async_dal: Any, dal: Any, *, user_id: int, tenant_id: int) -> bool:
    """Port of Node's `req.isTenantAdmin` -- a `tenant_admins` row for this tenant."""
    rows = await async_dal.select_async(
        dal((dal.tenant_admins.user_id == user_id) & (dal.tenant_admins.tenant_id == tenant_id))
    )
    return bool(rows)


async def community_belongs_to_tenant(
    async_dal: Any, dal: Any, *, community_id: int, tenant_id: int
) -> bool:
    """True if `community_id` exists and belongs to `tenant_id`.

    THE canonical tenant<->community cross-check (hotfix PR #226,
    `aa360dd1`: "fix cross-tenant IDOR in community_authz tenant-admin
    bypass") -- shared IDOR guard for every tenant-scoped bypass in this
    module (the tenant-admin bypass in both `require_community_admin`
    above and `resolve_community_membership_scoped` below) -- a
    `tenant_admins` row proves the caller administers `tenant_id`, never
    that the *target* `community_id` from the request also belongs to
    that tenant. Without this check a tenant-A admin passing a tenant-B
    `community_id` would pass the bypass and act cross-tenant.

    Public (no leading underscore) because `services/community_access.py`
    also needs this exact check for its own `require_community_admin`/
    `require_community_member` tenant pre-check -- both modules previously
    carried their own independent reimplementation of "does this
    community_id belong to this tenant_id" (this function here, and a
    separate `tenant_scoped()`-based query in `community_access.py`); this
    is now the single shared implementation. `community_access.py` never
    had the #226 IDOR gap itself (its tenant pre-check always ran
    unconditionally before either bypass, not gated behind a tenant-admin
    shortcut), so consolidating onto this function is a pure dedup, not a
    behavior change for that module's callers.
    """
    rows = await async_dal.select_async(
        dal((dal.communities.id == community_id) & (dal.communities.tenant_id == tenant_id))
    )
    return bool(rows)


async def resolve_community_membership_scoped(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    user_id: int,
    tenant_id: int,
    roles_claim: list[str],
) -> CommunityMembership | None:
    """Resolve the caller's role/scopes for `community_id`, or `None` if not a member.

    Bypass chain first (super-admin / platform-admin / tenant-admin), then
    a tenant-ownership check (security.md Tenant Isolation -- IDOR
    hardening beyond Node's own model, which never re-validated that the
    path's `:communityId` actually belongs to the caller's tenant), then a
    `community_members` LEFT JOIN `community_roles` lookup scoped to
    `(community_id, user_id, is_active=True)` -- `community_members.
    user_id` is a legacy VARCHAR column (see `services/schema.py`), so
    `user_id` is compared as `str(user_id)`, matching how Node's pg driver
    serializes the JWT `sub` claim into that column.
    """
    if any(r in _BYPASS_ROLES for r in roles_claim):
        return CommunityMembership(
            role="super-admin", scopes=frozenset(), is_admin=True, bypass=True
        )

    if not await community_belongs_to_tenant(
        async_dal, dal, community_id=community_id, tenant_id=tenant_id
    ):
        # Either the community doesn't exist, or it belongs to a different
        # tenant than the caller's JWT -- both collapse to the same "not a
        # member" outcome Node's own DB-not-found path produces, never
        # leaking whether the community exists under another tenant.
        # SECURITY: this check must run BEFORE the tenant-admin bypass
        # below, not just before the membership lookup -- a `tenant_admins`
        # row only proves the caller administers their own tenant, never
        # that this `community_id` belongs to it (cross-tenant IDOR fix).
        return None

    if await _is_tenant_admin(async_dal, dal, user_id=user_id, tenant_id=tenant_id):
        return CommunityMembership(
            role="tenant-admin", scopes=frozenset(), is_admin=True, bypass=True
        )

    rows = await async_dal.select_async(
        dal(
            (dal.community_members.community_id == community_id)
            & (dal.community_members.user_id == str(user_id))
            & (dal.community_members.is_active == True)  # noqa: E712
        ),
        dal.community_members.ALL,
        dal.community_roles.ALL,
        left=dal.community_roles.on(
            dal.community_members.community_role_id == dal.community_roles.id
        ),
    )
    if not rows:
        return None

    row = rows[0]
    member, role_row = row.community_members, row.community_roles
    scopes = _parse_claims_scopes(member.claims_cache)
    if not scopes and role_row is not None:
        scopes = _parse_claims_scopes(role_row.base_claims)
    role_name = (role_row.name if role_row is not None else None) or member.role
    return CommunityMembership(
        role=role_name, scopes=scopes, is_admin=bool(scopes & ADMIN_SCOPES), bypass=False
    )


async def require_community_member_scoped(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    user_id: int,
    tenant_id: int,
    roles_claim: list[str],
) -> CommunityMembership:
    """Port of `requireMember` -- raise 403 unless the caller is an active member."""
    membership = await resolve_community_membership_scoped(
        async_dal,
        dal,
        community_id=community_id,
        user_id=user_id,
        tenant_id=tenant_id,
        roles_claim=roles_claim,
    )
    if membership is None:
        raise forbidden("Community membership required")
    return membership


#: Node's `requireCommunityAdmin` treats either of these two scopes as
#: sufficient for an admin-gated action (`middleware/auth.js`). Per
#: `058_tenants_and_claims.sql`'s seeded system roles, the `moderator`
#: role bundle already carries `community:manage_channels` -- so a
#: community moderator (not just a full community-admin) satisfies this
#: gate, matching the M7 port instruction's "moderator scope" requirement
#: for music-station admin actions.
ADMIN_SCOPES: frozenset[str] = frozenset({"community:manage_members", "community:manage_channels"})

#: JWT `roles` claim entries that bypass the per-community DB check
#: entirely -- mirrors Node's `req.user.isSuperAdmin` /
#: `req.user.roles.includes('platform-admin')` bypasses in both
#: `requireMember` and `requireCommunityAdmin`.
_BYPASS_ROLES: frozenset[str] = frozenset({"super_admin", "platform-admin"})


async def require_community_admin_scoped(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    user_id: int,
    tenant_id: int,
    roles_claim: list[str],
) -> CommunityMembership:
    """Port of `requireCommunityAdmin` -- raise 403 unless bypass or an admin/moderator scope."""
    membership = await resolve_community_membership_scoped(
        async_dal,
        dal,
        community_id=community_id,
        user_id=user_id,
        tenant_id=tenant_id,
        roles_claim=roles_claim,
    )
    if membership is None or not (membership.bypass or membership.is_admin):
        raise forbidden("Community admin access required")
    return membership


def _decode_caller(request: Any) -> tuple[int, list[str]]:
    """Re-decode the bearer JWT for `(user_id, roles)`.

    Same self-contained, independent-of-`tenant_middleware`-state pattern
    `services.current_user`/`flask_core.authz.require_scope` already use
    (see their own docstrings) -- kept local here rather than importing
    `services.current_user.get_current_user_id` so this module also
    recovers the `roles` claim in the same decode, without a second
    round-trip through the token.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise unauthorized("Authentication required")
    secret_key = require_secret_key()
    payload = verify_jwt_token(auth_header[7:], secret_key)
    if payload is None:
        raise unauthorized("Invalid or expired token")
    try:
        user_id = int(payload["sub"])
    except (KeyError, TypeError, ValueError) as exc:
        raise unauthorized("Token missing subject claim") from exc
    roles = payload.get("roles") or []
    return user_id, [str(r) for r in roles]


async def authorize_community(
    request: Any, async_dal: Any, dal: Any, *, community_id: int, admin: bool
) -> CommunityMembership:
    """One-call authz gate for a `:communityId`-scoped route (M7 `_scoped` variant).

    Must run after `tenant_middleware` (reads `request.tenant_context`,
    published by that decorator -- security.md ordering contract: tenant
    before scope/resource checks). `admin=True` runs the
    `require_community_admin_scoped` port; `admin=False` runs the
    `require_community_member_scoped` port.
    """
    ctx = get_tenant_context(request)
    if ctx is None:
        raise forbidden("Tenant context not resolved")
    user_id, roles = _decode_caller(request)
    check = require_community_admin_scoped if admin else require_community_member_scoped
    return await check(
        async_dal,
        dal,
        community_id=community_id,
        user_id=user_id,
        tenant_id=ctx.tenant_id,
        roles_claim=roles,
    )
