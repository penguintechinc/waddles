"""Platform-level user CRUD for super admins -- ported from `userManagementController.js`.

Mounted at `/api/v1/superadmin/users*` in Node (`routes/superadmin.js`);
gated there by `requireAuth` + `requireSuperAdmin`. This port gates the
equivalent blueprint with `require_scope("users:admin")` -- present in
`flask_core.auth.SCOPE_BUNDLES["global"]["admin"]`, which
`auth_service.create_session_token` grants exactly when `hub_users.
is_super_admin` is true, so the scope check is equivalent to Node's
boolean check, expressed the OIDC-scope way security.md requires.

SECURITY (C3, A01/BOLA fix): that invariant -- "`users:admin` is granted
exactly when `hub_users.is_super_admin` is true" -- did NOT hold until
this fix: `flask_core.auth.SCOPE_BUNDLES["tenant"]["admin"]` also granted
the identical literal `users:admin`, so any tenant owner's JWT satisfied
this whole blueprint's `require_scope("users:admin")` gate, including
`assign_super_admin_role` below -- letting a tenant owner promote
themselves (or anyone) to PLATFORM super admin. The bundle collision is
fixed at its root in `flask_core/auth.py` (tenant bundle no longer grants
`users:admin` at all), but `assign_super_admin_role` additionally
re-validates the caller against `hub_users.is_super_admin` directly
(`services.community_authz.is_super_admin`, "THE canonical super-admin
check -- DB-authoritative") before ever toggling that flag on anyone --
defense in depth, independent of the JWT `scope` claim, so a future
`SCOPE_BUNDLES` regression can't silently reopen platform-wide privilege
escalation through this one action.
"""

from __future__ import annotations

import asyncio
import math
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt

from services import session_service
from services.community_authz import is_super_admin
from services.errors import bad_request, conflict, forbidden, not_found

SALT_ROUNDS = (
    10  # matches userManagementController.js's own SALT_ROUNDS (distinct from auth_service's 12)
)


def _hash_password_sync(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=SALT_ROUNDS)).decode()


async def list_users(
    async_dal: Any,
    dal: Any,
    *,
    page: int,
    limit: int,
    search: str,
    role: str | None,
    is_active: bool | None,
) -> tuple[list[Any], int, int]:
    """List users."""
    page = max(1, page)
    limit = min(100, max(1, limit))
    offset = (page - 1) * limit

    # pydal query builder (not raw SQL) -- portable across DB_TYPE backends
    # (backend-database.md "Support ALL databases") and, not incidentally,
    # the only form testable against sqlite: flask_core.database.AsyncDAL's
    # raw-SQL helpers hardcode `%s` (psycopg2-only) placeholders, so a raw
    # `... ILIKE %s` string here would 500 under every DB_TYPE except
    # Postgres -- see hub_api/PORTING.md.
    query = dal.hub_users.id > 0
    if search:
        query &= dal.hub_users.email.like(
            f"%{search}%", case_sensitive=False
        ) | dal.hub_users.username.like(f"%{search}%", case_sensitive=False)
    if role == "super_admin":
        query &= dal.hub_users.is_super_admin == True  # noqa: E712 - pydal Field comparison
    elif role == "vendor":
        query &= dal.hub_users.is_vendor == True  # noqa: E712 - pydal Field comparison
    if is_active is not None:
        query &= dal.hub_users.is_active == is_active

    total = await async_dal.count_async(query)
    rows = await async_dal.select_async(
        dal(query),
        orderby=~dal.hub_users.created_at,
        limitby=(offset, offset + limit),
    )
    total_pages = math.ceil(total / limit) if limit else 0
    return list(rows), total, total_pages


async def get_user(async_dal: Any, dal: Any, *, user_id: int) -> Any:
    """Get user."""
    rows = await async_dal.select_async(dal(dal.hub_users.id == user_id))
    if not rows:
        raise not_found("User not found")
    return rows.first()


async def create_user(async_dal: Any, dal: Any, *, email: str, password: str) -> Any:
    """Create user."""
    if not email or not password:
        raise bad_request("Email and password required")
    if len(password) < 8:
        raise bad_request("Password must be at least 8 characters")

    existing = await async_dal.select_async(dal(dal.hub_users.email == email))
    if existing:
        raise conflict("Email already exists")

    password_hash = await asyncio.to_thread(_hash_password_sync, password)
    new_id = await async_dal.insert_async(
        dal.hub_users,
        email=email,
        username=email,
        password_hash=password_hash,
        is_active=True,
        email_verified=False,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    return await get_user(async_dal, dal, user_id=new_id)


async def update_user(
    async_dal: Any, dal: Any, *, user_id: int, email: str | None, is_active: bool | None
) -> Any:
    """Update user."""
    if email is None and is_active is None:
        raise bad_request("At least one field to update is required")

    current = await get_user(async_dal, dal, user_id=user_id)

    update_fields: dict[str, Any] = {}
    if email and email != current.email:
        dupe = await async_dal.select_async(
            dal((dal.hub_users.email == email) & (dal.hub_users.id != user_id))
        )
        if dupe:
            raise conflict("Email already in use")
        update_fields["email"] = email
        update_fields["username"] = email
    if is_active is not None:
        update_fields["is_active"] = is_active

    if not update_fields:
        return current

    update_fields["updated_at"] = datetime.now(UTC)
    await async_dal.update_async(dal.hub_users.id == user_id, **update_fields)
    if is_active is False:
        await session_service.revoke_user_sessions(
            async_dal, dal, user_id=user_id, reason="user_deactivated"
        )
    return await get_user(async_dal, dal, user_id=user_id)


async def delete_user(async_dal: Any, dal: Any, *, user_id: int, caller_id: int) -> None:
    """Delete user."""
    if user_id == caller_id:
        raise forbidden("Cannot delete your own account")
    await get_user(async_dal, dal, user_id=user_id)  # 404 if missing
    await async_dal.update_async(
        dal.hub_users.id == user_id, is_active=False, updated_at=datetime.now(UTC)
    )
    await session_service.revoke_user_sessions(
        async_dal, dal, user_id=user_id, reason="user_deleted"
    )


async def assign_super_admin_role(
    async_dal: Any, dal: Any, *, user_id: int, grant: bool, caller_id: int
) -> bool:
    """Assign super admin role.

    SECURITY: only a caller who is already a DB-authoritative platform
    super admin (`hub_users.is_super_admin`, never the JWT `scope` claim
    alone -- see this module's docstring) may grant OR revoke platform
    super admin on anyone, including themselves. This is the "separate,
    higher authority" gate for platform-wide role promotion -- a tenant
    owner must never be able to grant a platform-wide role, regardless of
    what scopes their token otherwise carries.
    """
    if not await is_super_admin(async_dal, dal, user_id=caller_id):
        raise forbidden("Platform super admin access required")
    user = await get_user(async_dal, dal, user_id=user_id)
    if bool(user.is_super_admin) == grant:
        return False
    await async_dal.update_async(
        dal.hub_users.id == user_id, is_super_admin=grant, updated_at=datetime.now(UTC)
    )
    if not grant:
        # A demotion must not wait out the demoted admin's remaining access-token life: their
        # existing tokens still carry the admin scope bundle.
        await session_service.revoke_user_sessions(
            async_dal, dal, user_id=user_id, reason="super_admin_revoked"
        )
    return True


async def assign_vendor_role(async_dal: Any, dal: Any, *, user_id: int, grant: bool) -> bool:
    """Assign vendor role."""
    user = await get_user(async_dal, dal, user_id=user_id)
    if bool(user.is_vendor) == grant:
        return False
    await async_dal.update_async(
        dal.hub_users.id == user_id, is_vendor=grant, updated_at=datetime.now(UTC)
    )
    return True


async def set_email_verification(async_dal: Any, dal: Any, *, user_id: int, verified: bool) -> bool:
    """Set email verification."""
    user = await get_user(async_dal, dal, user_id=user_id)
    if bool(user.email_verified) == verified:
        return False
    update_fields: dict[str, Any] = {
        "email_verified": verified,
        "updated_at": datetime.now(UTC),
    }
    if verified:
        update_fields["email_verification_token"] = None
    await async_dal.update_async(dal.hub_users.id == user_id, **update_fields)
    return True


async def assign_analytics_consumer_role(
    async_dal: Any, dal: Any, *, user_id: int, enabled: bool
) -> Any:
    """Assign analytics consumer role."""
    await get_user(async_dal, dal, user_id=user_id)  # 404 if missing
    await async_dal.update_async(
        dal.hub_users.id == user_id,
        is_analytics_consumer=enabled,
        updated_at=datetime.now(UTC),
    )
    return await get_user(async_dal, dal, user_id=user_id)


async def get_user_deletion_request(async_dal: Any, dal: Any, *, user_id: int) -> Any | None:
    """Get user deletion request."""
    rows = await async_dal.executesql_async(
        "SELECT requested_at, completed_at, status FROM data_deletion_requests "
        "WHERE hub_user_id = %s ORDER BY requested_at DESC LIMIT 1",
        [user_id],
    )
    return rows[0] if rows else None


async def generate_password_reset(
    async_dal: Any, dal: Any, *, user_id: int
) -> tuple[str, datetime]:
    """Generate password reset."""
    await get_user(async_dal, dal, user_id=user_id)  # 404 if missing
    reset_token = secrets.token_hex(16)
    reset_expires = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=24)
    await async_dal.update_async(
        dal.hub_users.id == user_id,
        password_reset_token=reset_token,
        password_reset_expires=reset_expires,
        updated_at=datetime.now(UTC),
    )
    return reset_token, reset_expires


async def list_user_sessions(
    async_dal: Any, dal: Any, *, user_id: int
) -> list[session_service.SessionInfo]:
    """Every login session of `user_id` -- ids and timestamps only, never a token."""
    await get_user(async_dal, dal, user_id=user_id)  # 404 if missing
    return await session_service.list_sessions(async_dal, dal, user_id=user_id)


async def revoke_user_sessions(
    async_dal: Any, dal: Any, *, user_id: int
) -> session_service.RevocationOutcome:
    """Admin "revoke user": end every session of `user_id` and cut off every token issued so far."""
    await get_user(async_dal, dal, user_id=user_id)  # 404 if missing
    return await session_service.revoke_user_sessions(
        async_dal, dal, user_id=user_id, reason="admin_revoke_user"
    )


async def revoke_user_session(
    async_dal: Any, dal: Any, *, user_id: int, session_id: int
) -> session_service.RevocationOutcome:
    """Admin "revoke session": end one login session of `user_id` (404 if it is not theirs)."""
    await get_user(async_dal, dal, user_id=user_id)  # 404 if missing
    owner = await session_service.get_session_owner(async_dal, dal, session_id=session_id)
    if owner != user_id:
        raise not_found("Session not found")
    return await session_service.revoke_session(
        async_dal, dal, session_id=session_id, reason="admin_revoke_session"
    )
