"""hub-api's record of the call rooms it created (`call_rooms`, alembic 0110).

The REST join trusts only this record for the facts svc-streaming's `Room` does not carry
(kind, closed, guest opt-in): a room that is not recorded is unknown and the join fails closed
for non-staff (`services/call_access.py`). Rows hold opaque ids and flags only (no PII).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydal import Field

from services.call_access import KIND_CODES, GrantKind, RoomFacts

_TABLE = "call_rooms"


def bind_call_rooms_table(dal: Any, *, migrate: bool = False) -> None:
    """Idempotently bind `call_rooms` (schema owned by alembic 0110)."""
    if _TABLE in dal.tables:
        return
    dal.define_table(
        _TABLE,
        Field("community_id", "integer", notnull=True),
        Field("room_id", "string", length=36, notnull=True, unique=True),
        Field("kind", "integer", notnull=True, default=0),
        Field("town_hall", "boolean", notnull=True, default=False),
        Field("closed", "boolean", notnull=True, default=False),
        Field("allow_guests", "boolean", notnull=True, default=False),
        Field("created_at", "datetime", default=lambda: datetime.now(UTC)),
        migrate=migrate,
    )


def _table(dal: Any) -> Any:
    bind_call_rooms_table(dal)
    return dal[_TABLE]


async def record_room(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    room_id: str,
    kind: GrantKind = GrantKind.COMMUNITY,
    town_hall: bool = False,
    allow_guests: bool = False,
) -> None:
    """Record a room hub-api just created; raises on a duplicate (unique room / Town Hall)."""
    await async_dal.insert_async(
        _table(dal),
        community_id=community_id,
        room_id=str(uuid.UUID(room_id)),
        kind=KIND_CODES.index(kind),
        town_hall=town_hall,
        allow_guests=allow_guests,
    )


async def get_facts(
    async_dal: Any, dal: Any, *, community_id: int, room_id: str
) -> RoomFacts | None:
    """The recorded facts of `room_id` IN THIS COMMUNITY, or None when hub-api does not know it."""
    table = _table(dal)
    rows = await async_dal.select_async(
        dal((table.community_id == community_id) & (table.room_id == str(uuid.UUID(room_id)))),
        table.kind,
        table.town_hall,
        table.closed,
        table.allow_guests,
    )
    if not rows:
        return None
    row = rows[0]
    kind = int(row.kind)
    if not 0 <= kind < len(KIND_CODES):
        return None  # a corrupt code is unknown, never a default kind
    return RoomFacts(
        kind=KIND_CODES[kind],
        closed=bool(row.closed),
        allow_guests=bool(row.allow_guests),
        town_hall=bool(row.town_hall),
    )


async def forget_room(async_dal: Any, dal: Any, *, community_id: int, room_id: str) -> None:
    """Drop the record of a room that was deleted upstream."""
    table = _table(dal)
    await async_dal.delete_async(
        (table.community_id == community_id) & (table.room_id == str(uuid.UUID(room_id)))
    )
