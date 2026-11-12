"""Platform-level user CRUD for super admins -- ported from `userManagementController.js`.

Mounted at `/api/v1/superadmin/users*`. Gated by `require_scope("users:admin")`, which
`auth_service` grants exactly to global admins (tenant admins of the reserved `system` tenant,
`services.global_identity.is_global_admin`) -- there is no `hub_users.is_super_admin` flag.

SECURITY (C3, A01/BOLA): `SCOPE_BUNDLES["tenant"]["admin"]` never carries `users:admin`, and every
state-changing action here additionally re-validates the caller against the database
(`is_global_admin`) -- defense in depth, independent of the JWT `scope` claim, so a future bundle
regression cannot reopen platform-wide privilege escalation.
"""

from __future__ import annotations

import asyncio
import math
import secrets
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt

from services import session_service
from services.audit_events import AuditAction, AuditCategory, AuditOutcome
from services.audit_http import record_privileged_user_change
from services.errors import (
    ApiError,
    bad_request,
    conflict,
    forbidden,
    not_found,
    unprocessable,
)
from services.free_tier_caps import (
    FREE_TIER_CAP,
    _platform_tier_slug,
    capped_write,
    count_tenant_admins,
    grant_super_admin_capped,
    super_admin_ids,
)
from services.global_identity import (
    SYSTEM_TENANT_SLUG,
    add_tenant_admin_sync,
    add_tenant_member_sync,
    ensure_system_tenant,
    global_admin_ids,
    has_system_admin_row_sync,
    is_global_admin,
    is_global_admin_sync,
    revoke_global_admin_sync,
    take_global_admin_lock,
)
from services.platform_account import guard_platform_account

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
        admin_ids = await asyncio.to_thread(global_admin_ids, dal)
        query &= dal.hub_users.id.belongs(sorted(admin_ids) or [-1])
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


#: Role written for the new admin: the full tenant-owner bundle inside its tenant.
TENANT_OWNER_ROLE = "tenant-owner"
USER_TYPE_GLOBAL_ADMIN = "global_admin"
USER_TYPE_TENANT_ADMIN = "tenant_admin"
VALID_USER_TYPES = (USER_TYPE_GLOBAL_ADMIN, USER_TYPE_TENANT_ADMIN)


async def global_admin_id_set(async_dal: Any, dal: Any) -> set[int]:
    """Ids of all active global admins (response DTOs derive `isSuperAdmin` from this)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(async_dal.executor, global_admin_ids, dal)


async def _resolve_tenant_by_public_id(async_dal: Any, dal: Any, public_id: str) -> Any:
    """Resolve an active tenant by its public UUID (never numeric id/slug); 422 if unknown."""
    try:
        canonical = str(uuid.UUID(public_id))
    except (ValueError, AttributeError, TypeError):
        raise unprocessable("tenant must be a tenant UUID") from None
    if "public_id" not in dal.tenants.fields:
        raise unprocessable("Tenant lookup unavailable")
    rows = await async_dal.select_async(dal(dal.tenants.public_id == canonical))
    row = rows.first() if rows else None
    if row is None or row.is_active is False:
        raise unprocessable("Tenant not found")
    return row


async def create_user(
    async_dal: Any,
    dal: Any,
    *,
    email: str,
    password: str,
    user_type: str | None,
    tenant_public_id: str | None,
    caller_id: int,
    caller_tenant_slug: str,
) -> tuple[Any, str, Any]:
    """Create an ADMIN user: a `global_admin` (system tenant) or a `tenant_admin` (given tenant).

    Plain-user creation is not offered here (422). Fail closed: the caller is re-validated as a
    global admin in the database, the type/tenant are validated before any write, and the user
    row + membership + admin row are written in ONE transaction subject to the free-tier admin
    cap. Returns `(user_row, user_type, tenant_row_or_None)`; `tenant_row` is the system tenant
    for a global admin.
    """
    if not await is_global_admin(async_dal, dal, user_id=caller_id):
        raise forbidden("Platform super admin access required")
    if user_type not in VALID_USER_TYPES:
        raise unprocessable("type must be one of: global_admin, tenant_admin")
    if not email or not password:
        raise bad_request("Email and password required")
    if len(password) < 8:
        raise bad_request("Password must be at least 8 characters")
    tenant_row: Any = None
    if user_type == USER_TYPE_TENANT_ADMIN:
        if not tenant_public_id:
            raise unprocessable("tenant is required when type is tenant_admin")
        tenant_row = await _resolve_tenant_by_public_id(async_dal, dal, tenant_public_id)
        if tenant_row.slug == SYSTEM_TENANT_SLUG:
            raise unprocessable("The system tenant only takes global_admin users")
    elif tenant_public_id:
        raise unprocessable("tenant must not be set when type is global_admin")

    existing = await async_dal.select_async(dal(dal.hub_users.email == email))
    if existing:
        raise conflict("Email already exists")

    password_hash = await asyncio.to_thread(_hash_password_sync, password)
    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "email": email,
        "username": email,
        "password_hash": password_hash,
        "is_active": True,
        "email_verified": False,
        "created_at": now,
        "updated_at": now,
    }

    def _insert_and_grant(d: Any, tenant_id: int | None) -> int:
        new_id = int(d.hub_users.insert(**values))
        target = tenant_id if tenant_id is not None else ensure_system_tenant(d)
        add_tenant_member_sync(d, tenant_id=target, user_id=new_id)
        add_tenant_admin_sync(d, tenant_id=target, user_id=new_id, role=TENANT_OWNER_ROLE)
        return new_id

    if user_type == USER_TYPE_GLOBAL_ADMIN:
        tier_slug = await asyncio.to_thread(_platform_tier_slug, dal, caller_tenant_slug)
        new_id = await capped_write(
            async_dal,
            dal,
            kind="admin",
            noun="admin user",
            tier_slug=tier_slug,
            lock_key="admin:platform",
            is_full=lambda d: len(super_admin_ids(d)) >= FREE_TIER_CAP,
            write=lambda d: _insert_and_grant(d, None),
        )
        tenant_row = (
            await async_dal.select_async(dal(dal.tenants.slug == SYSTEM_TENANT_SLUG))
        ).first()
    else:
        tenant_id = int(tenant_row.id)
        new_id = await capped_write(
            async_dal,
            dal,
            kind="admin",
            noun="admin user",
            tier_slug=str(tenant_row.slug),
            lock_key=f"tenant_admin:{tenant_id}",
            is_full=lambda d: count_tenant_admins(d, tenant_id=tenant_id) >= FREE_TIER_CAP,
            write=lambda d: _insert_and_grant(d, tenant_id),
        )
    return await get_user(async_dal, dal, user_id=new_id), str(user_type), tenant_row


async def _target_uuid(async_dal: Any, dal: Any, user_id: int) -> str | None:
    """`hub_users.uuid` of `user_id` for audit records (None if the column/row is absent)."""
    if "uuid" not in dal.hub_users.fields:
        return None

    def _read() -> str | None:
        row = dal(dal.hub_users.id == user_id).select(dal.hub_users.uuid).first()
        return str(row.uuid) if row is not None and row.uuid else None

    return await asyncio.get_running_loop().run_in_executor(async_dal.executor, _read)


async def _audit_admin_change(
    async_dal: Any,
    dal: Any,
    *,
    caller_id: int,
    user_id: int,
    category: AuditCategory,
    action: AuditAction,
    operation: str,
    outcome: AuditOutcome,
    reason: str | None = None,
) -> None:
    """Emit the actor/target-UUID/action/result audit record for a global-admin change."""
    await record_privileged_user_change(
        actor_user_id=caller_id,
        target_user_uuid=await _target_uuid(async_dal, dal, user_id),
        category=category,
        action=action,
        outcome=outcome,
        operation=operation,
        reason=reason,
    )


async def _run_under_admin_lock(async_dal: Any, dal: Any, work: Any) -> Any:
    """Run `work(dal)` in ONE transaction holding the `global_admin` advisory lock.

    The lock is taken before the caller counts the global admins, so two concurrent
    revoke/deactivate/delete requests serialise: the second sees the first's committed result
    and trips the last-admin guard instead of both passing it. Commits on success, rolls back
    (and re-raises) on any error, including the guard's 409/403.
    """

    def _job() -> Any:
        try:
            take_global_admin_lock(dal)
            result = work(dal)
            dal.commit()
            return result
        except Exception:
            dal.rollback()
            raise

    return await asyncio.get_running_loop().run_in_executor(async_dal.executor, _job)


def _guard_global_admin_target(d: Any, *, user_id: int, caller_id: int, removing: bool) -> bool:
    """Guard an action on `user_id`; True when the target is a (live) global admin.

    Acting on a global admin needs a DB-verified global-admin caller (403 otherwise, whatever
    the JWT scope says). When `removing` (deactivate/delete) and the target is the only global
    admin left it is refused with 409 -- the platform must never be left without an operator.
    """
    if not has_system_admin_row_sync(d, user_id):
        return False  # raw row check: a DEACTIVATED global admin is still guarded
    if removing and is_global_admin_sync(d, user_id) and global_admin_ids(d) <= {user_id}:
        raise conflict("Cannot deactivate or delete the last global admin")
    if not is_global_admin_sync(d, caller_id):
        raise forbidden("Platform super admin access required to act on a global admin")
    return True


def revoke_global_admin_guarded(*, caller_id: int, user_id: int) -> Callable[[Any], None]:
    """Build the revoke work for `_run_under_admin_lock`: re-check, last-admin guard, revoke.

    The caller is re-verified INSIDE the lock (it may have been demoted since the pre-check) and
    the count is read under the same lock, so concurrent demotions cannot both pass it.
    """

    def _revoke(d: Any) -> None:
        if not is_global_admin_sync(d, caller_id):
            raise forbidden("Platform super admin access required")
        if not is_global_admin_sync(d, user_id):
            return
        if global_admin_ids(d) <= {user_id}:
            raise conflict("Cannot remove the last global admin")
        revoke_global_admin_sync(d, user_id)

    return _revoke


def _denial_reason(exc: ApiError) -> str:
    """Closed audit reason for a refused global-admin change."""
    return "last_global_admin" if exc.status_code == 409 else "caller_not_admin"


async def update_user(
    async_dal: Any,
    dal: Any,
    *,
    user_id: int,
    email: str | None,
    is_active: bool | None,
    caller_id: int,
) -> Any:
    """Update user; deactivating revokes sessions and can never strand the last global admin."""
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
    deactivating = is_active is False
    if deactivating:
        await guard_platform_account(
            async_dal,
            dal,
            user_id=user_id,
            actor_user_id=caller_id,
            category=AuditCategory.USER,
            action=AuditAction.USER_UPDATED,
            operation="deactivate",
        )
    operation = "deactivate" if deactivating else ("reactivate" if is_active else "update")
    target_was_admin = await asyncio.get_running_loop().run_in_executor(
        async_dal.executor, has_system_admin_row_sync, dal, user_id
    )

    def _write(d: Any) -> bool:
        was_admin = _guard_global_admin_target(
            d, user_id=user_id, caller_id=caller_id, removing=deactivating
        )
        d(d.hub_users.id == user_id).update(**update_fields)
        return was_admin

    try:
        was_admin = bool(await _run_under_admin_lock(async_dal, dal, _write))
    except ApiError as exc:
        if target_was_admin:
            await _audit_admin_change(
                async_dal,
                dal,
                caller_id=caller_id,
                user_id=user_id,
                category=AuditCategory.USER,
                action=AuditAction.USER_UPDATED,
                operation=operation,
                outcome=AuditOutcome.DENIED,
                reason=_denial_reason(exc),
            )
        raise
    if deactivating:
        await session_service.revoke_user_sessions(
            async_dal, dal, user_id=user_id, reason="user_deactivated"
        )
    if was_admin:
        await _audit_admin_change(
            async_dal,
            dal,
            caller_id=caller_id,
            user_id=user_id,
            category=AuditCategory.USER,
            action=AuditAction.USER_UPDATED,
            operation=operation,
            outcome=AuditOutcome.SUCCESS,
        )
    return await get_user(async_dal, dal, user_id=user_id)


async def delete_user(async_dal: Any, dal: Any, *, user_id: int, caller_id: int) -> None:
    """Delete (soft) a user; refuses the last global admin and revokes the user's sessions."""
    await get_user(async_dal, dal, user_id=user_id)  # 404 if missing
    await guard_platform_account(
        async_dal,
        dal,
        user_id=user_id,
        actor_user_id=caller_id,
        category=AuditCategory.USER,
        action=AuditAction.USER_DELETED,
        operation="delete",
    )
    if user_id == caller_id:
        raise forbidden("Cannot delete your own account")
    target_was_admin = await asyncio.get_running_loop().run_in_executor(
        async_dal.executor, has_system_admin_row_sync, dal, user_id
    )

    def _write(d: Any) -> bool:
        was_admin = _guard_global_admin_target(
            d, user_id=user_id, caller_id=caller_id, removing=True
        )
        d(d.hub_users.id == user_id).update(is_active=False, updated_at=datetime.now(UTC))
        return was_admin

    try:
        was_admin = bool(await _run_under_admin_lock(async_dal, dal, _write))
    except ApiError as exc:
        if target_was_admin:
            await _audit_admin_change(
                async_dal,
                dal,
                caller_id=caller_id,
                user_id=user_id,
                category=AuditCategory.USER,
                action=AuditAction.USER_DELETED,
                operation="delete",
                outcome=AuditOutcome.DENIED,
                reason=_denial_reason(exc),
            )
        raise
    await session_service.revoke_user_sessions(
        async_dal, dal, user_id=user_id, reason="user_deleted"
    )
    if was_admin:
        await _audit_admin_change(
            async_dal,
            dal,
            caller_id=caller_id,
            user_id=user_id,
            category=AuditCategory.USER,
            action=AuditAction.USER_DELETED,
            operation="delete",
            outcome=AuditOutcome.SUCCESS,
        )


async def assign_super_admin_role(
    async_dal: Any, dal: Any, *, user_id: int, grant: bool, caller_id: int, tenant_slug: str
) -> bool:
    """Grant/revoke global admin (admin of the `system` tenant) -- the ONLY path that may.

    SECURITY: only a caller who is already a DB-authoritative global admin (never the JWT `scope`
    claim alone) may grant OR revoke it, including on themselves -- a tenant owner must never be
    able to grant a platform-wide role. Grant writes the `tenant_members` + `tenant_admins` rows.
    Revoke runs under the `global_admin` advisory lock in one transaction and refuses the LAST
    global admin (409), then revokes the demoted admin's sessions (`super_admin_revoked`) so
    their tokens stop carrying the admin scope bundle. Both directions are audited.
    """
    if not await is_global_admin(async_dal, dal, user_id=caller_id):
        raise forbidden("Platform super admin access required")
    await get_user(async_dal, dal, user_id=user_id)  # 404 if missing
    already = await is_global_admin(async_dal, dal, user_id=user_id)
    if already == grant:
        return False
    operation = "grant" if grant else "revoke"
    if not grant:
        await guard_platform_account(
            async_dal,
            dal,
            user_id=user_id,
            actor_user_id=caller_id,
            category=AuditCategory.ROLE,
            action=AuditAction.ROLE_SUPER_ADMIN_CHANGED,
            operation="demote",
        )
    if grant:
        await grant_super_admin_capped(
            async_dal, dal, caller_tenant_slug=tenant_slug, user_id=user_id
        )
    else:
        try:
            await _run_under_admin_lock(
                async_dal, dal, revoke_global_admin_guarded(caller_id=caller_id, user_id=user_id)
            )
        except ApiError as exc:
            await _audit_admin_change(
                async_dal,
                dal,
                caller_id=caller_id,
                user_id=user_id,
                category=AuditCategory.ROLE,
                action=AuditAction.ROLE_SUPER_ADMIN_CHANGED,
                operation=operation,
                outcome=AuditOutcome.DENIED,
                reason=_denial_reason(exc),
            )
            raise
        await session_service.revoke_user_sessions(
            async_dal, dal, user_id=user_id, reason="super_admin_revoked"
        )
    await _audit_admin_change(
        async_dal,
        dal,
        caller_id=caller_id,
        user_id=user_id,
        category=AuditCategory.ROLE,
        action=AuditAction.ROLE_SUPER_ADMIN_CHANGED,
        operation=operation,
        outcome=AuditOutcome.SUCCESS,
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
