"""Canonical `community_members.role` values and the write-time guard for them.

Every authorization gate (backend and frontend) compares the stored role string against
``community-owner`` / ``community-admin`` etc., so a typo such as ``owner`` silently locks the
owner out of the admin panel. This module is the single source of truth for the allowed values,
and :func:`require_valid_member_role` makes an unknown value fail loudly at write time.
"""

from __future__ import annotations

from services.errors import bad_request

#: The one owner role every gate checks; never aliased or normalized.
COMMUNITY_OWNER_ROLE = "community-owner"

#: Roles an admin may assign to a member (owner is deliberately excluded: never assignable).
ASSIGNABLE_MEMBER_ROLES: tuple[str, ...] = ("community-admin", "moderator", "vip", "member")

#: Every role value a `community_members` write may store.
ALL_MEMBER_ROLES: tuple[str, ...] = (COMMUNITY_OWNER_ROLE, *ASSIGNABLE_MEMBER_ROLES)


def require_valid_member_role(role: str) -> str:
    """Return ``role`` if canonical, else raise a 400 (a typo like ``owner`` is never stored)."""
    if role not in ALL_MEMBER_ROLES:
        raise bad_request(f"Invalid community member role {role!r}")
    return role
