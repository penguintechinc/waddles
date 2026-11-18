"""Live-activity feed -- recent bot interaction events + the SSE poll generator.

`live_activity_events` (frozen contract this port task was given: `id
BIGSERIAL PK, community_id INT NOT NULL, platform VARCHAR(50), actor
VARCHAR(255), message_in TEXT, reply_out TEXT, channel_id VARCHAR(255),
occurred_at TIMESTAMPTZ`) is a brand-new table for the WebUI's live
activity feed -- no existing migration owns it yet; `_ensure_table()`
below only maps pydal onto it (`migrate=False` in production, matching
every `bind_*_tables()` convention documented in `services/schema.py`).
Bound locally in THIS module rather than added to the shared
`services/schema.py`/`app.py::_bind_reference_tables` -- this task's
scope is "new files only" (the same per-group isolation boundary every
`bind_*_tables()` docstring in `schema.py` already cites for the
parallel port wave), so a brand-new, single-owner table gets its own
idempotent bind function instead of touching a file every other group
also edits.

Two read surfaces share this module (`blueprints/v1/live_activity.py`):
a bounded-`limit` list (`GET .../live-activity`) and an SSE polling
stream (`GET .../live-activity/stream`, `~1s` poll cadence). Both use
plain synchronous `dal` calls -- mirrors `services/community_common.py`'s
own documented convention (raw pydal, not `AsyncDAL.*_async()`; see this
repo's mem0 notes on `select_async()`/AsyncDAL concurrency gotchas) --
wrapped in `asyncio.to_thread()` only in the SSE loop, where the same
query re-runs roughly once a second for the lifetime of a client
connection: blocking the shared event loop on every poll iteration is a
real responsiveness cost that a single per-request call (the list
endpoint) doesn't have.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from services.community_public_id import lookup_public_ids_sync

#: Poll cadence for the SSE generator -- matches this port task's own spec
#: ("~1s"). Overridable per-call (tests pass a much shorter interval so
#: the suite doesn't wait a full second per assertion).
_POLL_INTERVAL_SECONDS = 1.0


def _ensure_table(dal: Any, *, migrate: bool = False) -> None:
    """Idempotently define `live_activity_events` -- see module docstring.

    Cheap membership check (`dal.tables` is a plain list), safe to call
    at the top of every entry point in this module -- matches
    `community_common.ensure_community_tables()`'s own established
    idempotent-bind pattern.
    """
    if "live_activity_events" in dal.tables:
        return
    dal.define_table(
        "live_activity_events",
        dal.Field("community_id", "integer", notnull=True),
        dal.Field("platform", "string", length=50, notnull=True),
        dal.Field("actor", "string", length=255),
        dal.Field("message_in", "text"),
        dal.Field("reply_out", "text"),
        dal.Field("channel_id", "string", length=255),
        dal.Field("occurred_at", "datetime"),
        migrate=migrate,
    )


def _iso(value: Any) -> str:
    """Render a pydal datetime column value as an ISO-8601 string.

    `occurred_at` is the one frozen-contract field with no `|null` in its
    JSON shape -- callers are expected to always populate it at insert
    time; a `None` value (never expected in practice) degrades to `""`
    rather than raising, matching this repo's "a bad value degrades to a
    safe default, it never 500s the request" convention
    (`services/pagination.py::parse_limit`'s own docstring states the
    same rule for `?limit=`).
    """
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value else ""


@dataclass(slots=True, frozen=True)
class LiveActivityEvent:
    """One `live_activity_events` row, shaped to the frozen WebUI JSON contract.

    Deliberately excludes `channel_id` -- present in the table, not in
    the contract's event JSON shape.
    """

    id: int
    community_id: str
    platform: str
    actor: str | None
    message_in: str | None
    reply_out: str | None
    occurred_at: str


@dataclass(slots=True, frozen=True)
class LiveActivityListResponse:
    """Response DTO for `GET .../live-activity`."""

    success: bool
    events: list[LiveActivityEvent]


def _to_event(row: Any, public_community_id: str) -> LiveActivityEvent:
    """Event DTO; `community_id` is the community's PUBLIC UUID (never the numeric id)."""
    return LiveActivityEvent(
        id=row.id,
        community_id=public_community_id,
        platform=row.platform,
        actor=row.actor,
        message_in=row.message_in,
        reply_out=row.reply_out,
        occurred_at=_iso(row.occurred_at),
    )


def list_recent_events(dal: Any, *, community_id: int, limit: int) -> list[LiveActivityEvent]:
    """Most recent `limit` events for `community_id`, newest first."""
    _ensure_table(dal)
    rows = dal(dal.live_activity_events.community_id == community_id).select(
        orderby=~dal.live_activity_events.id, limitby=(0, limit)
    )
    public_id = lookup_public_ids_sync(dal, [community_id])[community_id]
    return [_to_event(row, public_id) for row in rows]


def _max_id(dal: Any, *, community_id: int) -> int:
    """Sync query: the highest existing `id` for `community_id`, or `0` if none."""
    _ensure_table(dal)
    row = (
        dal(dal.live_activity_events.community_id == community_id)
        .select(dal.live_activity_events.id, orderby=~dal.live_activity_events.id, limitby=(0, 1))
        .first()
    )
    return int(row.id) if row else 0


def _poll_since(dal: Any, *, community_id: int, since_id: int) -> list[Any]:
    """Sync query: rows for `community_id` with `id > since_id`, oldest first.

    Called from `event_stream()` via `asyncio.to_thread()` -- see module
    docstring for why this one call, unlike the rest of this module's
    per-request queries, is worth offloading.
    """
    _ensure_table(dal)
    query = (dal.live_activity_events.community_id == community_id) & (
        dal.live_activity_events.id > since_id
    )
    return list(dal(query).select(orderby=dal.live_activity_events.id))


def _sse_frame(event: LiveActivityEvent) -> bytes:
    r"""Encode one `LiveActivityEvent` as a single `data: ...\n\n` SSE frame."""
    return f"data: {json.dumps(dataclasses.asdict(event))}\n\n".encode()


async def event_stream(
    dal: Any, *, community_id: int, poll_interval: float = _POLL_INTERVAL_SECONDS
) -> AsyncGenerator[bytes]:
    r"""SSE generator: polls `live_activity_events` for new rows roughly every `poll_interval`s.

    Baseline (`last_seen`) is the highest existing id for `community_id`
    AT CONNECT TIME, established before the first frame is ever yielded --
    a newly-opened stream tails only genuinely new events from that point
    forward, it never replays existing history (the list endpoint already
    covers that; a poll-loop history replay on every reconnect would be
    surprising and, at scale, expensive). After the baseline is set, yields
    one `: keepalive` comment so the client sees the connection open
    immediately, then loops: each `id > last_seen` poll either yields one
    `data: <json>\n\n` frame per new row (oldest first, `last_seen` bumped
    to the last one emitted) or, if nothing new landed, a `: heartbeat`
    comment -- both keep an idle connection from being timed out by an
    intermediary proxy. Mirrors `core/svc_presentation/blueprints/
    overlay.py::live()`'s SSE frame shape (comment-frame keepalive,
    `data:`-frame payload), adapted from that module's Valkey pub/sub push
    model to a DB poll loop -- no pub/sub broker is wired for this table.
    """
    public_id = (await asyncio.to_thread(lookup_public_ids_sync, dal, [community_id]))[community_id]
    last_seen = await asyncio.to_thread(_max_id, dal, community_id=community_id)
    yield b": keepalive\n\n"
    while True:
        rows = await asyncio.to_thread(
            _poll_since, dal, community_id=community_id, since_id=last_seen
        )
        if rows:
            for row in rows:
                event = _to_event(row, public_id)
                last_seen = event.id
                yield _sse_frame(event)
        else:
            yield b": heartbeat\n\n"
        await asyncio.sleep(poll_interval)
