"""Global identity derived from tenancy -- the single source of truth for "super admin".

There is no `hub_users.is_super_admin` flag. The reserved `system` tenant (security.md Reserved
Tenants: the operator plane) is the only anchor:

- **global user**  = member of the `system` tenant (`tenant_members` row, or any admin row)
- **global admin** (super admin) = tenant admin of the `system` tenant (`tenant_admins` row)

Every table stays tenant-scoped; "global" is a join on membership/`tenant_admins`, never a flag.
All helpers fail closed: a missing `system` tenant, a missing user or an inactive user is "not
global". Token issuance (`auth_service._scopes_for`) and every authorization check go through
this module so the definition cannot drift.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from pydal import Field

SYSTEM_TENANT_SLUG = "system"
SYSTEM_TENANT_NAME = "System"
#: `tenant_admins.role` written for global admins (full tenant-owner bundle inside `system`).
GLOBAL_ADMIN_ROLE = "tenant-owner"


def ensure_tables(dal: Any, *, migrate: bool = False) -> None:
    """Bind the four tables this module reads if the DAL has not bound them yet (idempotent).

    Production binds them at startup (`services.schema.bind_auth_tables`); minimal test DALs
    that bind a subset get the same column shapes here. `migrate=False` in production (Alembic
    owns the schema); tests pass `migrate=True` for sqlite.
    """
    if "tenants" not in dal.tables:
        dal.define_table(
            "tenants",
            Field("slug", "string", length=100),
            Field("display_name", "string", length=255),
            Field("is_active", "boolean", default=True),
            migrate=migrate,
        )
    if "hub_users" not in dal.tables:
        dal.define_table("hub_users", Field("is_active", "boolean", default=True), migrate=False)
    if "tenant_admins" not in dal.tables:
        dal.define_table(
            "tenant_admins",
            Field("tenant_id", "integer", notnull=True),
            Field("user_id", "integer", notnull=True),
            Field("role", "string", length=50, default="tenant-admin"),
            Field("created_at", "datetime"),
            migrate=migrate,
        )
    if "tenant_members" not in dal.tables:
        dal.define_table(
            "tenant_members",
            Field("tenant_id", "integer", notnull=True),
            Field("user_id", "integer", notnull=True),
            Field("created_at", "datetime"),
            migrate=migrate,
        )


#: Stable advisory-lock key serialising every "count the global admins, then mutate" section
#: (revoke, deactivate, delete) so two concurrent demotions can never both pass the last-admin
#: guard and leave the platform with zero operators.
GLOBAL_ADMIN_LOCK_KEY = "global_admin"


def take_global_admin_lock(dal: Any) -> None:
    """Take the transaction-scoped `global_admin` advisory lock (Postgres; no-op elsewhere).

    Blocks until any concurrent holder commits/rolls back, so the admin count read afterwards
    sees every earlier mutation. Call it first inside the same transaction as the count and the
    write; the lock is released by that transaction's commit/rollback. Other dialects (sqlite in
    tests) serialise writers at the connection level and have no such function.
    """
    if str(getattr(dal, "_dbname", "")).startswith("postgres"):
        dal.executesql(
            "SELECT pg_advisory_xact_lock(hashtext(%s))", placeholders=[GLOBAL_ADMIN_LOCK_KEY]
        )


def _system_tenant_row(dal: Any) -> Any | None:
    """The `system` tenant row regardless of its active flag, or None when absent."""
    ensure_tables(dal)
    return dal(dal.tenants.slug == SYSTEM_TENANT_SLUG).select(dal.tenants.id).first()


def _system_tenant_id(dal: Any) -> int | None:
    """Return the ACTIVE `system` tenant's id, or None (fail closed).

    A missing tenant and a deactivated (`is_active` false or NULL) one are both "no operator
    plane": nobody is global while the reserved tenant is switched off.
    """
    ensure_tables(dal)
    query = (dal.tenants.slug == SYSTEM_TENANT_SLUG) & (dal.tenants.is_active == True)  # noqa: E712
    row = dal(query).select(dal.tenants.id).first()
    return int(row.id) if row is not None else None


def _user_is_active(dal: Any, user_id: int) -> bool:
    """True when the user row exists and is not deactivated (NULL counts as active)."""
    if "is_active" not in dal.hub_users.fields:  # reduced test bindings only; prod has the column
        return bool(dal(dal.hub_users.id == user_id).count())
    user = dal(dal.hub_users.id == user_id).select(dal.hub_users.is_active).first()
    return user is not None and user.is_active is not False


def is_global_admin_sync(dal: Any, user_id: int) -> bool:
    """True iff `user_id` is an active user holding a `tenant_admins` row on `system`."""
    sys_id = _system_tenant_id(dal)
    if sys_id is None or not _user_is_active(dal, user_id):
        return False
    query = (dal.tenant_admins.tenant_id == sys_id) & (dal.tenant_admins.user_id == user_id)
    return bool(dal(query).count())


def is_global_user_sync(dal: Any, user_id: int) -> bool:
    """True iff `user_id` is an active member (or admin) of the `system` tenant."""
    if is_global_admin_sync(dal, user_id):
        return True
    sys_id = _system_tenant_id(dal)
    if sys_id is None or not _user_is_active(dal, user_id):
        return False
    query = (dal.tenant_members.tenant_id == sys_id) & (dal.tenant_members.user_id == user_id)
    return bool(dal(query).count())


async def _run(fn: Any, async_dal: Any, dal: Any, user_id: int) -> bool:
    """Run a sync pydal helper on the AsyncDAL executor so it shares the DAL's thread model."""
    loop = asyncio.get_running_loop()
    return bool(await loop.run_in_executor(async_dal.executor, fn, dal, user_id))


async def is_global_admin(async_dal: Any, dal: Any, *, user_id: int) -> bool:
    """Async `is_global_admin_sync`: the canonical super-admin check (DB-authoritative)."""
    return await _run(is_global_admin_sync, async_dal, dal, user_id)


async def is_global_user(async_dal: Any, dal: Any, *, user_id: int) -> bool:
    """Async `is_global_user_sync`: member of the `system` tenant."""
    return await _run(is_global_user_sync, async_dal, dal, user_id)


def global_admin_ids(dal: Any) -> set[int]:
    """Ids of every active global admin (system-tenant admin). Empty when no `system` tenant."""
    sys_id = _system_tenant_id(dal)
    if sys_id is None:
        return set()
    rows = dal(dal.tenant_admins.tenant_id == sys_id).select(dal.tenant_admins.user_id)
    ids = {int(r.user_id) for r in rows}
    if not ids:
        return set()
    return {uid for uid in ids if _user_is_active(dal, uid)}


def ensure_system_tenant(dal: Any) -> int:
    """Return the `system` tenant id, creating the reserved row if missing (idempotent)."""
    existing = _system_tenant_row(dal)
    if existing is not None:
        active = _system_tenant_id(dal)
        if active is None:
            raise RuntimeError(
                "the reserved `system` tenant exists but is inactive; refusing to grant "
                "global admin until an operator re-activates it"
            )
        return active
    values: dict[str, Any] = {"slug": SYSTEM_TENANT_SLUG, "is_active": True}
    if "display_name" in dal.tenants.fields:  # minimal test DALs bind a reduced `tenants`
        values["display_name"] = SYSTEM_TENANT_NAME
    return int(dal.tenants.insert(**values))


def add_tenant_member_sync(dal: Any, *, tenant_id: int, user_id: int) -> None:
    """Idempotently add a `tenant_members` row."""
    query = (dal.tenant_members.tenant_id == tenant_id) & (dal.tenant_members.user_id == user_id)
    if not dal(query).count():
        dal.tenant_members.insert(
            tenant_id=tenant_id, user_id=user_id, created_at=datetime.now(UTC)
        )


def add_tenant_admin_sync(dal: Any, *, tenant_id: int, user_id: int, role: str) -> None:
    """Idempotently add a `tenant_admins` row (an existing row's role is left untouched)."""
    query = (dal.tenant_admins.tenant_id == tenant_id) & (dal.tenant_admins.user_id == user_id)
    if not dal(query).count():
        dal.tenant_admins.insert(
            tenant_id=tenant_id, user_id=user_id, role=role, created_at=datetime.now(UTC)
        )


def grant_global_admin_sync(dal: Any, user_id: int) -> int:
    """Make `user_id` member + admin of `system` (global admin). Returns the system tenant id."""
    sys_id = ensure_system_tenant(dal)
    add_tenant_member_sync(dal, tenant_id=sys_id, user_id=user_id)
    add_tenant_admin_sync(dal, tenant_id=sys_id, user_id=user_id, role=GLOBAL_ADMIN_ROLE)
    return sys_id


def revoke_global_admin_sync(dal: Any, user_id: int) -> None:
    """Remove `user_id`'s admin row on `system` (membership is kept: still a global user)."""
    sys_id = _system_tenant_id(dal)
    if sys_id is not None:
        query = (dal.tenant_admins.tenant_id == sys_id) & (dal.tenant_admins.user_id == user_id)
        dal(query).delete()
