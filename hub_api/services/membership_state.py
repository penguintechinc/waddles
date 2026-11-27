"""`community_members.membership_state` -- the int-enum standing of a membership row.

A ban is a membership STATE, never a role name: authority lives in `community_roles` (scopes on
the role, linked via `community_role_id`), standing lives here. Every gate denies only while the
state is BANNED; a lift writes ACTIVE back, which restores normal access exactly as if the member
had never been banned (the role link is untouched by a ban, so nothing else needs restoring).

WHY a row is banned is `community_members.ban_sources` (alembic 0148): a :class:`BanSource`
bitmask. Every ban path adds its own bit, every lift removes only its own bit, and the state is
BANNED while ANY bit remains (`ban_suppression` holds the one add / remove / recompute trio), so
lifting one reason never un-bans a member who is still banned for another.
"""

from __future__ import annotations

from enum import IntEnum, IntFlag
from typing import Any, Final


class MembershipState(IntEnum):
    """Closed set mirrored by the `ck_community_members_membership_state` CHECK (alembic 0146)."""

    ACTIVE = 1
    BANNED = 2


class BanSource(IntFlag):
    """Closed set of ban reasons, bits of `community_members.ban_sources` (CHECK, alembic 0148)."""

    MANUAL = 1
    SOFT_BAN = 2
    FLOOR_BAN = 4
    SUPPRESSION = 8


#: Every defined bit; the `ck_community_members_ban_sources` CHECK bounds the column to it.
ALL_BAN_SOURCES: Final = int(
    BanSource.MANUAL | BanSource.SOFT_BAN | BanSource.FLOOR_BAN | BanSource.SUPPRESSION
)

#: Column holding the :class:`BanSource` bitmask.
BAN_SOURCES_FIELD: Final = "ban_sources"


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
