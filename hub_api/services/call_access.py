"""Room access policy for the REST join: the Python twin of the canonical `grants_for`.

`bundles/rust/webrtc/src/grants.rs` is the single source of truth for who may publish or
subscribe in a room and for the join-token numbers; `bundles/rust/webrtc/contract/grants.json`
is its machine-readable copy. The `rooms` app bundle compiles the Rust source itself; hub-api is
Python, so this module re-implements the same rules and `tests/test_calls_access.py` replays
EVERY case of the contract file against it (and `scripts/ci/tests/test_rooms_webrtc_contract.py`
does the same from the repo root), so the two can never silently drift.

Policy in one breath (first matching rule wins): closed rooms refuse everybody; outside the
scheduled window and locked rooms refuse non-staff; a green room is staff-only; guests (opt-in
per room) can only subscribe; a stage lets only staff publish; everyone else publishes and
subscribes. Staff (moderator/admin) are never refused by lock or window.

What hub-api can actually see: svc-streaming's `Room` carries `locked` but not the kind,
closed flag, schedule or guest opt-in -- those live in the `rooms` app bundle's records.
`facts_from_room` therefore fills what is known and treats every other room as an open
community room; a room whose extra facts hub-api cannot see is still guarded by svc-streaming's
own lock check and by the rooms app's `join_token`. Exposing those facts on `Room` (so this
adapter can read them) is the open follow-up recorded in the PR.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # type-only: keeps this module importable without the gRPC stack
    from services.calls_proxy import RoomInfo

#: Lifetime (seconds) of a join token minted for a room join (contract `token.join_ttl_secs`).
JOIN_TTL_SECS = 60
#: Longest lifetime any join path may request (contract `token.join_ttl_max_secs`).
JOIN_TTL_MAX_SECS = 900


class GrantRole(enum.IntEnum):
    """Access role, ordered `GUEST < MEMBER < MODERATOR < ADMIN`."""

    GUEST = 0
    MEMBER = 1
    MODERATOR = 2
    ADMIN = 3

    @property
    def is_staff(self) -> bool:
        """Moderators and admins bypass locks, windows and the green-room gate."""
        return self >= GrantRole.MODERATOR


class GrantKind(enum.Enum):
    """How a room came to exist (decides stage / green-room rules)."""

    COMMUNITY = "community"
    EVENT = "event"
    BREAKOUT = "breakout"
    STAGE = "stage"
    GREEN_ROOM = "green_room"


class Window(enum.Enum):
    """Where `now` sits relative to the room's optional scheduled window."""

    UNSCHEDULED = "none"
    INSIDE = "inside"
    OUTSIDE = "outside"


@dataclass(slots=True, frozen=True)
class GrantCtx:
    """Everything about the room (besides kind and role) the policy looks at."""

    closed: bool = False
    locked: bool = False
    allow_guests: bool = False
    window: Window = Window.UNSCHEDULED


@dataclass(slots=True, frozen=True)
class Grants:
    """The media directions a token may carry."""

    publish: bool
    subscribe: bool


class AccessDeniedError(Exception):
    """The policy refuses the join; `reason` is one closed string from the contract."""

    def __init__(self, reason: str) -> None:
        """Record the closed refusal reason."""
        super().__init__(reason)
        self.reason = reason


def grants_for(kind: GrantKind, role: GrantRole, ctx: GrantCtx) -> Grants:
    """Grants `role` gets in a `kind` room under `ctx`; raises `AccessDeniedError` otherwise."""
    if ctx.closed:
        raise AccessDeniedError("room is closed")
    staff = role.is_staff
    if ctx.window is Window.OUTSIDE and not staff:
        raise AccessDeniedError("room is outside its scheduled window")
    if ctx.locked and not staff:
        raise AccessDeniedError("room is locked")
    if kind is GrantKind.GREEN_ROOM and not staff:
        raise AccessDeniedError("green room is for moderators")
    if role is GrantRole.GUEST:
        if not ctx.allow_guests:
            raise AccessDeniedError("guests are not allowed in this room")
        return Grants(publish=False, subscribe=True)
    if kind is GrantKind.STAGE and not staff:
        return Grants(publish=False, subscribe=True)
    return Grants(publish=True, subscribe=True)


def facts_from_room(room: RoomInfo) -> tuple[GrantKind, GrantCtx]:
    """The kind and context hub-api can derive from what svc-streaming reports about `room`."""
    return GrantKind.COMMUNITY, GrantCtx(locked=room.locked)
