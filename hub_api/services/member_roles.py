"""Canonical community role names and the ONE writer of a member's community role.

All community authority lives in the per-community `community_roles` table (system roles seeded by
the database's own `seed_community_system_roles()`, custom roles added by admins); a member holds
exactly one role via `community_members.community_role_id`, which is what authz resolves scopes
through (`community_authz`, `community_access`). :func:`set_member_role` is the single writer of
that link (the legacy `role` string column is gone).

Writers can only link a role that exists for the community (``strict``), so a typo such as
``owner`` fails loudly instead of storing a scope-less member.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

#: The seeded top system role (admin kind: full computed scope set) -- the creator's role. It is
#: never assignable and its holder can be neither demoted nor removed (see :func:`is_owner_role`).
COMMUNITY_OWNER_ROLE = "community-owner"

#: The seeded system admin role (admin kind: full computed scope set) -- appointable by an owner.
COMMUNITY_ADMIN_ROLE = "community-admin"

#: The seeded baseline role every joining member gets.
COMMUNITY_MEMBER_ROLE = "member"

#: THE seeded system roles a role change may assign. Every other system role -- the owner (only
#: through an explicit ownership transfer) and the remaining seeded roles -- is refused; a
#: community's own custom roles are assignable too (see :func:`is_assignable_role`).
ASSIGNABLE_SYSTEM_ROLES: tuple[str, ...] = (
    COMMUNITY_ADMIN_ROLE,
    "moderator",
    COMMUNITY_MEMBER_ROLE,
)

#: Role-sync binding vocabulary (migration 0036's CHECK): the assignable system roles plus the
#: non-system ``vip`` (`community-owner` is never assignable).
ASSIGNABLE_MEMBER_ROLES: tuple[str, ...] = (
    COMMUNITY_ADMIN_ROLE,
    "moderator",
    "vip",
    COMMUNITY_MEMBER_ROLE,
)


#: Every role name `seed_community_system_roles()` (alembic 0002) seeds. Reserved: a custom role
#: may never take one, and system-name lookups always also require ``is_system`` so a pre-existing
#: custom row of the same name can never stand in for the system role.
SYSTEM_ROLE_NAMES: frozenset[str] = frozenset(
    {
        COMMUNITY_MEMBER_ROLE,
        "communicator",
        "speaker",
        "moderator",
        COMMUNITY_ADMIN_ROLE,
        COMMUNITY_OWNER_ROLE,
    }
)


def role_by_name_query(dal: Any, community_id: int, name: str) -> Any:
    """Query for the community role ``name``; a reserved system name matches the system row ONLY.

    A non-reserved name (e.g. ``vip``, which the seed does not define) matches the community's
    custom role of that name, as before.
    """
    cr = dal.community_roles
    query = (cr.community_id == community_id) & (cr.name == name)
    if name in SYSTEM_ROLE_NAMES:
        query &= cr.is_system == True  # noqa: E712
    return query


def is_owner_role(role_row: Any) -> bool:
    """True iff ``role_row`` (a member's LINKED `community_roles` row) is the seeded owner role.

    Ownership is decided by the role row the member links via `community_role_id` -- the system
    flag plus the reserved owner name, which a custom role can never carry -- never by the legacy
    legacy role string. An unlinked member (``None``) is never the owner.
    """
    return (
        role_row is not None
        and bool(getattr(role_row, "is_system", False))
        and getattr(role_row, "name", None) == COMMUNITY_OWNER_ROLE
    )


def is_assignable_role(role_row: Any, community_id: int) -> bool:
    """True iff a role change may link ``role_row`` (never the owner, see :func:`is_owner_role`).

    Assignable: a role of THIS community whose name is not a reserved system name (its custom
    roles), or the SYSTEM row of a name in `ASSIGNABLE_SYSTEM_ROLES`. Every other reserved name
    (owner, communicator, speaker) -- and any non-system row squatting on one -- is refused.
    """
    if role_row is None or getattr(role_row, "community_id", None) != community_id:
        return False
    if is_owner_role(role_row):
        return False
    name = getattr(role_row, "name", None)
    if name not in SYSTEM_ROLE_NAMES:
        return True
    return bool(getattr(role_row, "is_system", False)) and name in ASSIGNABLE_SYSTEM_ROLES


def seed_community_system_roles(dal: Any, community_id: int) -> None:
    """Seed a community's predefined roles via the database's `seed_community_system_roles()`.

    The role names and scopes are defined ONLY in that SQL function (alembic 0002; `role_kind` and
    the moderator scope merges come from the 0117/0118 BEFORE INSERT triggers). Idempotent
    (``ON CONFLICT DO NOTHING``); runs in the caller's transaction -- the caller commits.
    """
    dal.executesql("SELECT seed_community_system_roles(%s)", placeholders=[int(community_id)])


def set_member_role(
    dal: Any,
    *,
    community_id: int,
    role: str,
    user_id: int | str | None = None,
    members: Any = None,
    add: bool = False,
    strict: bool = True,
) -> int | None:
    """Point member row(s) at the community role named ``role``; return its `community_roles.id`.

    The role is resolved with :func:`role_by_name_query` (reserved names match the system row only).

    Targets one member (``user_id``) or every row a pydal query matches (``members``). With
    ``add=True`` (``user_id`` only) a missing membership is inserted active, and an EXISTING row
    keeps its current role (joining never demotes). Writes `community_role_id` and clears
    `claims_cache` so scopes re-resolve. ``strict`` (default) fails loudly when the role is not
    defined for the community; ``strict=False`` writes a NULL link (no scopes)
    instead. Runs in the caller's transaction -- the caller commits.
    """
    if (user_id is None) == (members is None):
        raise ValueError("set_member_role needs exactly one of user_id or members")
    role_row = (
        dal(role_by_name_query(dal, community_id, role)).select(dal.community_roles.id).first()
    )
    if role_row is None and strict:
        raise LookupError(f"community role {role!r} is not defined for community {community_id}")
    role_id = int(role_row.id) if role_row is not None else None
    now = datetime.now(UTC)
    cm = dal.community_members
    if members is None:
        members = (cm.community_id == community_id) & (cm.user_id == str(user_id))
        if add:
            if dal(members).count():
                return role_id
            cm.insert(
                community_id=community_id,
                user_id=str(user_id),
                community_role_id=role_id,
                is_active=True,
                joined_at=now,
                updated_at=now,
            )
            return role_id
    dal(members).update(community_role_id=role_id, claims_cache=None, updated_at=now)
    return role_id


async def run_write_job[T](async_dal: Any, dal: Any, job: Callable[[], T]) -> T:
    """Run sync ``job`` as ONE DAL-executor transaction: committed, or rolled back on error."""

    def _tx() -> T:
        try:
            result = job()
            dal.commit()
            return result
        except Exception:
            dal.rollback()
            raise

    return await asyncio.get_running_loop().run_in_executor(async_dal.executor, _tx)


async def set_member_role_async(async_dal: Any, dal: Any, **kwargs: Any) -> int | None:
    """:func:`set_member_role` as ONE committed executor job (rolled back on any error)."""
    return await run_write_job(async_dal, dal, lambda: set_member_role(dal, **kwargs))


async def add_joining_member_async(
    async_dal: Any, dal: Any, *, community_id: int, user_id: int
) -> int | None:
    """Add a joiner as an active member LINKED to the seeded `member` role (scopes + roleId).

    THE join-path writer: (idempotently) ensures the community's system roles exist -- so a
    legacy community seeded before roles still links -- then :func:`set_member_role` with
    ``add=True`` (an existing row keeps its role). ONE transaction; returns the role id.
    """

    def job() -> int | None:
        seed_community_system_roles(dal, community_id)
        return set_member_role(
            dal, community_id=community_id, role=COMMUNITY_MEMBER_ROLE, user_id=user_id, add=True
        )

    return await run_write_job(async_dal, dal, job)
