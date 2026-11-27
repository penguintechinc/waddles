"""`community_members.membership_state` -- the int-enum standing of a membership row.

A ban is a membership STATE, never a role name: authority lives in `community_roles` (scopes on
the role, linked via `community_role_id`), standing lives here. Every gate denies only while the
state is BANNED; a lift writes ACTIVE back, which restores normal access exactly as if the member
had never been banned (the role link is untouched by a ban, so nothing else needs restoring).
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any, Final


class MembershipState(IntEnum):
    """Closed set mirrored by the `ck_community_members_membership_state` CHECK (alembic 0146)."""

    ACTIVE = 1
    BANNED = 2


#: Column name, kept in one place for the DAL binding, migration and queries.
MEMBERSHIP_STATE_FIELD: Final = "membership_state"


def is_banned(member: Any) -> bool:
    """True iff the member row is in the BANNED state (a missing/NULL column is ACTIVE)."""
    raw = getattr(member, MEMBERSHIP_STATE_FIELD, None)
    return raw is not None and int(raw) == MembershipState.BANNED


def not_banned(dal: Any) -> Any:
    """Query term: the `community_members` row is not BANNED (a NULL state is ACTIVE)."""
    state = dal.community_members.membership_state
    return (state == None) | (state != MembershipState.BANNED)  # noqa: E711 - pydal Field
