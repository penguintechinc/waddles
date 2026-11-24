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
closed flag or guest opt-in. hub-api therefore records every room it creates in `call_rooms`
(`services/call_rooms.py`) and `facts_from_room` consumes ONLY that record plus the live
`locked`: a room hub-api never recorded has unknown facts and is treated fail-closed (`None`;
the join refuses non-staff). This function stays the single place facts enter the policy, so
svc-streaming's own room facts can replace or cross-check the record later.
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


#: Wire/DB int code of each kind (order matches `contract/grants.json` `kinds`).
KIND_CODES: tuple[GrantKind, ...] = (
    GrantKind.COMMUNITY,
    GrantKind.EVENT,
    GrantKind.BREAKOUT,
    GrantKind.STAGE,
    GrantKind.GREEN_ROOM,
)


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


@dataclass(slots=True, frozen=True)
class RoomFacts:
    """What hub-api positively knows about a room it recorded (see `services/call_rooms.py`)."""

    kind: GrantKind
    closed: bool = False
    allow_guests: bool = False
    town_hall: bool = False


def facts_from_room(
    room: RoomInfo, recorded: RoomFacts | None
) -> tuple[GrantKind, GrantCtx] | None:
    """The kind and context for `room`, or None when hub-api does not know the room (fail closed).

    `locked` is read live from svc-streaming; everything else comes from hub-api's own record.
    """
    if recorded is None:
        return None
    return recorded.kind, GrantCtx(
        closed=recorded.closed, locked=room.locked, allow_guests=recorded.allow_guests
    )
