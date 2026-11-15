"""Community-membership authorization -- the IDOR/token-takeover fix this port group adds.

Node's `requireCommunityAdmin` middleware (`admin/hub_module/backend/src/
middleware/auth.js`) verifies the caller is an active owner/admin member
of the exact `:communityId` in the URL before `overlayController.js`'s
four routes run -- every one of them is mounted behind it in `admin.js`.
`hub_api/PORTING.md`'s documented Auth pattern table only covers
tenant+scope (`tenant_middleware` + `require_scope`), which is NOT the
same guarantee: a caller holding a global `streaming.overlay:admin` /
`streaming.calls:admin` scope grant would pass `require_scope` for ANY
`community_id` path parameter -- including one in a different tenant
entirely, or one they have no membership in at all. Overlay URLs
(unguessable but not access-controlled) and RTC join tokens (a live
LiveKit credential) are exactly what security.md's Output Validation and
Service-to-Service Auth sections both warn against minting for the wrong
caller. `require_community_admin()`/`require_community_member()` below
are this group's own addition, layered strictly AFTER `tenant_middleware`
/`require_scope`, never a replacement for either.

Worse gap, member-facing surface: Node's `memberVoiceRouter`
(`routes/calls.js`) applies `requireAuth` ONLY -- no community-membership
check of any kind -- to `POST .../voice/rooms/:roomName/join`. ANY
authenticated user, member of the target community or not, could mint a
LiveKit join token for ANY `community_id` by supplying it in the URL.
`require_community_member()` closes this; ported in
`blueprints/v1/calls.py`.

Simplification vs. Node's full model (documented, not silently dropped --
`hub_api/PORTING.md` Gotcha #4's own precedent for "document, don't
silently invent"): Node resolves the caller's admin-ness via a
`community_roles.base_claims` JSON scopes join
(`community:manage_members` / `community:manage_channels`); this port
checks `community_members.role` directly against the two system-seeded
admin-tier role names instead (`communityController.js`/
`adminController.js`/`platformController.js` all also check
`role === 'community-owner'` directly in several places, so this is not
an invented convention). Binding `community_roles` +
`hub_channel_permission_overrides` for a claims graph neither overlay
nor calls otherwise needs is out of scope for this port group.
"""

from __future__ import annotations

from typing import Any

from flask_core.tenancy import TenantContext
from quart import Request

from services.community_authz import (
    community_belongs_to_tenant,
    get_caller_community_role,
    is_super_admin,
)
from services.errors import forbidden
from services.scope_registry import SCOPE_COMMUNITY_ADMIN


async def _require_community_in_tenant(
    async_dal: Any, dal: Any, ctx: TenantContext, *, community_id: int
) -> None:
    """Raise 403 unless `community_id` resolves to a real row inside `ctx`'s tenant.

    Backed by `services.community_authz.community_belongs_to_tenant()` --
    THE canonical tenant<->community cross-check (#226 IDOR-fix module,
    see that function's own docstring) -- rather than this module's own,
    now-removed `tenant_scoped()`-based reimplementation of the identical
    "does `community_id` belong to `tenant_id`" query. Same outcome for
    every `(community_id, tenant_id)` pair either query construction ever
    produced -- pure dedup, not a behavior change. 403, not 404: matches
    Node's `requireCommunityAdmin` ("Community admin access required" for
    both "doesn't exist" and "not a member"), and avoids confirming to an
    unauthorized caller that a given community_id exists at all in
    another tenant.
    """
    if not await community_belongs_to_tenant(
        async_dal, dal, community_id=community_id, tenant_id=ctx.tenant_id
    ):
        raise forbidden("Community admin access required")


async def require_community_admin(
    async_dal: Any,
    dal: Any,
    request: Request,
    ctx: TenantContext,
    *,
    community_id: int,
    user_id: int,
) -> None:
    """Raise 403 unless `user_id` holds `community:admin` in `community_id` (inside `ctx`'s tenant).

    Port of Node's `requireCommunityAdmin` -- gates every overlay route
    and every admin-facing calls route (`blueprints/v1/overlay.py`,
    `blueprints/v1/calls.py`'s `calls_admin_bp`).

    `request` is unused directly here (kept for call-site signature
    stability with `require_community_member`'s counterpart and the three
    existing callers) -- the super-admin bypass is now
    `services.community_authz.is_super_admin()`, the DB-backed
    `hub_users.is_super_admin` check, not a re-decode of this request's
    JWT `roles` claim. `roles` is audit/display only per security.md
    (never the source of an authz decision); this module's own bypass
    previously decoded it directly, which was the one place in this file
    that check leaked in -- `is_super_admin()` also removes an entirely
    redundant second JWT decode, since every caller of this function
    already ran `get_current_user_id(request)` (which decodes the same
    token) to obtain `user_id` before calling here.
    """
    if await is_super_admin(async_dal, dal, user_id=user_id):
        return

    await _require_community_in_tenant(async_dal, dal, ctx, community_id=community_id)

    # Scope-based (dedicated `community:admin`, held by the admin kind), never a role-name match.
    caller_role = await get_caller_community_role(
        async_dal, dal, community_id=community_id, user_id=user_id
    )
    if caller_role is None or SCOPE_COMMUNITY_ADMIN not in caller_role.scopes:
        raise forbidden("Community admin access required")


async def require_community_member(
    async_dal: Any,
    dal: Any,
    request: Request,
    ctx: TenantContext,
    *,
    community_id: int,
    user_id: int,
) -> None:
    """Raise 403 unless `user_id` is an ACTIVE member (any role) of `community_id`.

    The fix `blueprints/v1/calls.py`'s member-facing voice routes add
    that Node never had at all -- see module docstring's "Worse gap"
    paragraph. Any active role qualifies (unlike
    `require_community_admin`): a regular member is allowed to list
    rooms, create an ad-hoc room, and join/leave voice, matching the
    feature intent (member-facing voice, not an admin surface) while
    still refusing a caller with no membership in the target community
    at all.

    `request` unused directly here for the same reason documented in
    `require_community_admin` above -- see that docstring.
    """
    if await is_super_admin(async_dal, dal, user_id=user_id):
        return

    await _require_community_in_tenant(async_dal, dal, ctx, community_id=community_id)

    membership_query = dal(
        (dal.community_members.community_id == community_id)
        & (dal.community_members.user_id == str(user_id))
        & (dal.community_members.is_active == True)  # noqa: E712 - pydal query operator
    )
    rows = await async_dal.select_async(membership_query, dal.community_members.id)
    if not rows:
        raise forbidden("Community membership required")
