"""The per-community "Town Hall": one persistent default call room, created lazily.

Rooms are identified by server-assigned UUIDs and svc-streaming does not enforce unique labels,
so uniqueness is enforced here by convergence: every caller lists the community's rooms, picks
the deterministic winner among rooms labelled `TOWN_HALL_LABEL` (oldest `created_at`, then the
lowest room id) and only creates one when none exists. Two requests racing (even on different
hub-api replicas) may both create; whoever sees duplicates afterwards deletes the empty
non-winners (get-after-conflict), so every caller returns -- and the community ends up with --
the same single room. An in-process lock per community removes the common same-replica race
outright.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from services.calls_proxy import RoomInfo, RoomsClient, RoomsRpcError

logger = logging.getLogger(__name__)

#: Label of the persistent default room (a display alias, never an identifier).
TOWN_HALL_LABEL = "Town Hall"

_locks: dict[int, asyncio.Lock] = {}


@dataclass(slots=True, frozen=True)
class TownHall:
    """The community's Town Hall room and whether this call created it."""

    room: RoomInfo
    created: bool


def _winner(rooms: list[RoomInfo]) -> RoomInfo | None:
    """Deterministic Town Hall among `rooms` (oldest, then lowest id), or None."""
    candidates = [r for r in rooms if r.label == TOWN_HALL_LABEL]
    return min(candidates, key=lambda r: (r.created_at, r.room_id)) if candidates else None


async def _prune_duplicates(
    client: RoomsClient, community_id: int, rooms: list[RoomInfo], winner: RoomInfo
) -> None:
    """Delete empty Town Hall rooms that lost the tie-break (never a room with people in it)."""
    for room in rooms:
        if room.label != TOWN_HALL_LABEL or room.room_id == winner.room_id:
            continue
        if room.publisher_ids or room.subscriber_count:
            continue  # occupied: left for a later reconcile once it empties
        try:
            await client.delete_room(community_id, room.room_id)
        except RoomsRpcError as exc:
            # Already gone is the desired state; the client already logged every other failure.
            if exc.status_code != 404:
                logger.error("town hall duplicate cleanup failed: %s %s", exc.status_code, exc.code)


async def ensure_town_hall(client: RoomsClient, community_id: int) -> TownHall:
    """Return the community's Town Hall, creating it (never-expiring) when it does not exist."""
    lock = _locks.setdefault(community_id, asyncio.Lock())
    async with lock:
        rooms = await client.list_rooms(community_id)
        existing = _winner(rooms)
        if existing is not None:
            await _prune_duplicates(client, community_id, rooms, existing)
            return TownHall(room=existing, created=False)
        created = await client.create_room(
            community_id, label=TOWN_HALL_LABEL, max_publishers=None, empty_timeout_secs=0
        )
        rooms = await client.list_rooms(community_id)
        winner = _winner(rooms) or created
        await _prune_duplicates(client, community_id, rooms, winner)
        return TownHall(room=winner, created=winner.room_id == created.room_id)
