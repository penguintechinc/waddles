"""The per-community "Town Hall": one persistent default call room, recorded server-side.

The Town Hall room id lives in `call_rooms` (alembic 0110) with `town_hall = true`; a partial
unique index allows ONE per community. It is NEVER found by label: labels are member-settable
display aliases, so `is_reserved_label` rejects creating any room called "Town Hall" (case and
space insensitive) through the room-create routes.

Reading is side-effect free (`get_town_hall`). Only `ensure_town_hall` (POST) creates:
1. a recorded room that still exists is returned as is;
2. otherwise a never-expiring room is created and recorded. The unique index makes concurrent
   creators race safely: the loser's insert conflicts, it deletes ONLY the room it just created
   and returns the winner's. A recorded room that no longer exists upstream is replaced with
   compare-and-set on the old room id.
No code path deletes a room it did not create.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from services import call_rooms
from services.calls_proxy import RoomInfo, RoomsClient, RoomsRpcError

logger = logging.getLogger(__name__)

#: Display label of the persistent default room (never an identifier).
TOWN_HALL_LABEL = "Town Hall"


def is_reserved_label(label: str) -> bool:
    """True for any spelling of "Town Hall" (case- and whitespace-insensitive)."""
    return " ".join(label.split()).casefold() == TOWN_HALL_LABEL.casefold()


@dataclass(slots=True, frozen=True)
class TownHall:
    """The community's Town Hall room and whether this call created it."""

    room: RoomInfo
    created: bool


async def _recorded_room_id(async_dal: Any, dal: Any, community_id: int) -> str | None:
    """The recorded Town Hall room id for the community, or None."""
    call_rooms.bind_call_rooms_table(dal)
    table = dal["call_rooms"]
    rows = await async_dal.select_async(
        dal((table.community_id == community_id) & (table.town_hall == True)),  # noqa: E712
        table.room_id,
    )
    return str(rows[0].room_id) if rows else None


async def get_town_hall(
    client: RoomsClient, async_dal: Any, dal: Any, community_id: int
) -> TownHall | None:
    """Read-only lookup: the recorded Town Hall, or None when it was never created.

    A recorded room that upstream no longer knows surfaces as the upstream 404 (never created
    implicitly, never an empty success).
    """
    room_id = await _recorded_room_id(async_dal, dal, community_id)
    if room_id is None:
        return None
    return TownHall(room=await client.get_room(community_id, room_id), created=False)


async def ensure_town_hall(
    client: RoomsClient, async_dal: Any, dal: Any, community_id: int
) -> TownHall:
    """Return the community's Town Hall, creating it (never-expiring) when none is recorded."""
    old_id = await _recorded_room_id(async_dal, dal, community_id)
    if old_id is not None:
        try:
            return TownHall(room=await client.get_room(community_id, old_id), created=False)
        except RoomsRpcError as exc:
            if exc.status_code != 404:
                raise
            logger.error("recorded town hall room is gone upstream; recreating")
            await call_rooms.forget_room(async_dal, dal, community_id=community_id, room_id=old_id)
    created = await client.create_room(
        community_id, label=TOWN_HALL_LABEL, max_publishers=None, empty_timeout_secs=0
    )
    try:
        await call_rooms.record_room(
            async_dal, dal, community_id=community_id, room_id=created.room_id, town_hall=True
        )
        return TownHall(room=created, created=True)
    except Exception:  # unique-index conflict: another request recorded its Town Hall first
        dal.rollback()
    # We lost the race: remove ONLY the room this call created, return the winner's.
    try:
        await client.delete_room(community_id, created.room_id)
    except RoomsRpcError as exc:
        logger.error("town hall race cleanup failed: %s %s", exc.status_code, exc.code)
    winner = await _recorded_room_id(async_dal, dal, community_id)
    if winner is None:
        raise RoomsRpcError(503, "Town Hall could not be recorded", "TOWN_HALL_RECORD_FAILED")
    return TownHall(room=await client.get_room(community_id, winner), created=False)
