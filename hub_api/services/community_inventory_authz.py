"""Per-community authorization for the Quartermaster (`community.inventory`) routes.

`tenant_middleware` + `require_scope` only prove a valid same-tenant token; the community in the
URL is client-controlled, and `*:read` (carried by every session) satisfies any `:read` scope. So
the inventory routes authorise against the REQUESTED community, using the same sources as the
other community-scoped groups (`community_authz` / `community_access`):

* members (browse, check out, check in, my-items): an ACTIVE `community_members` row that is not
  banned (`role == 'banned'`, written with `is_active=False` by the reputation policy) and has no
  live soft-ban (`bundle_reputation_soft_bans`, keyed by the member's `user_uuid`);
* admins (items, checkouts, summary, audit log): the dedicated `community:admin` scope held via
  the caller's community role, or a tenant admin of the community's tenant.

A global admin (system-tenant admin) passes both. No scope is minted globally: a member action
is authorised by membership in THAT community, never by a session-wide grant.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from flask_core.tenancy import TenantContext

from services.community_authz import member_scopes_sync
from services.community_reputation_service import _ensure_reputation_tables
from services.errors import forbidden
from services.global_identity import is_global_admin_sync
from services.scope_registry import SCOPE_COMMUNITY_ADMIN

BANNED_ROLE = "banned"


def _is_tenant_admin(dal: Any, ctx: TenantContext, user_id: int) -> bool:
    """True iff `user_id` holds a `tenant_admins` row for the caller's tenant."""
    if "tenant_admins" not in dal.tables:
        return False
    return bool(
        dal(
            (dal.tenant_admins.user_id == user_id) & (dal.tenant_admins.tenant_id == ctx.tenant_id)
        ).count()
    )


def _soft_banned(dal: Any, community_id: int, user_uuid: str | None) -> bool:
    """True iff the member has an unexpired soft-ban in this community."""
    if not user_uuid:
        return False
    _ensure_reputation_tables(dal)
    bans = dal.bundle_reputation_soft_bans
    now = datetime.now(UTC)  # expires_at is TIMESTAMPTZ (alembic 0097): compare aware, never naive
    return bool(
        dal(
            (bans.community_id == community_id)
            & (bans.user_uuid == user_uuid)
            & ((bans.expires_at == None) | (bans.expires_at > now))  # noqa: E711 - pydal idiom
        ).count()
    )


def require_inventory_member(
    dal: Any, ctx: TenantContext, *, community_id: int, user_id: int
) -> None:
    """Raise 403 unless `user_id` is an active, unbanned, non-soft-banned member of the community.

    Call AFTER the tenant<->community check (`community_in_tenant`), so a cross-tenant id is a 404
    before any membership detail is revealed.
    """
    if is_global_admin_sync(dal, user_id):
        return
    member = (
        dal(
            (dal.community_members.community_id == community_id)
            & (dal.community_members.user_id == str(user_id))
            & (dal.community_members.is_active == True)  # noqa: E712 - pydal query operator
        )
        .select()
        .first()
    )
    if member is None:
        raise forbidden("Community membership required")
    role = getattr(member, "role", None)
    if isinstance(role, str) and role.strip().lower() == BANNED_ROLE:
        raise forbidden("Community membership required")
    if _soft_banned(dal, community_id, getattr(member, "user_uuid", None)):
        raise forbidden("Community membership required")


def require_inventory_admin(
    dal: Any, ctx: TenantContext, *, community_id: int, user_id: int
) -> None:
    """Raise 403 unless the caller administers THIS community (never via the universal `*:read`)."""
    if is_global_admin_sync(dal, user_id) or _is_tenant_admin(dal, ctx, user_id):
        return
    if SCOPE_COMMUNITY_ADMIN not in member_scopes_sync(dal, community_id, user_id):
        raise forbidden("Community admin access required")
