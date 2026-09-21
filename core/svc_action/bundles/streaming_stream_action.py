"""Streaming live-stream listings ACTION bundle -- queries live streams by community.

Ported from `hub_api/services/stream_service.py` (v2 `get_live_streams`,
`get_featured_streams`, `get_stream_details`). Platform-agnostic query
bundle that reads from pre-existing `coordination` and `community_servers`
tables. No external HTTP call; queries the local database only.

Receives an event with payload `{"query": "get_live_streams|featured|details",
"community_id": <int>, "entity_id": <str> (details only)}` and returns
formatted live-stream DTOs via the `detail` field of TransportResult.
Raises `NonRetryableTransportError` for invalid queries or missing data
(no matching community or stream).

`community_servers`/`coordination` are never bound anywhere else on this
service's `dal` (svc-action's own startup only binds `tenants`/
`communities`/`app_catalog`/`action_dispatch_log`), so this bundle binds
its own minimal stubs (`_ensure_streaming_tables`, idempotent,
`migrate=False` -- schema owned by `config/postgres/migrations/
000_create_base_schema.sql`/`004_add_missing_tables.sql`), same "bind
only the columns this bundle actually touches" convention
`twitch_shoutout_action.py::_ensure_shoutout_tables` establishes.

DB access is `penguin-dal`'s public API (docs/superpowers/specs/
2026-09-14-rust-data-plane-design.md D21a / M1.5), replacing the legacy
DAL wrapper this module used before. The `community_servers JOIN
coordination` filter (`_build_join_query`) is the one query in this
bundle set that compares two tables' columns to each other rather than a
table's column to a literal -- `penguin_dal.field_proxy.FieldProxy`'s
comparison operators build a bind-parameter `Query` for that shape, so
the join condition is built from the raw SQLAlchemy `.column` on each
side instead and wrapped back into a `penguin_dal.Query`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

import httpx
from flask_core import StageEnvelope, get_bundle_context, get_bundle_dal
from penguin_dal import Field, Query
from waddle_transports import NonRetryableTransportError, TransportResult

if TYPE_CHECKING:
    from penguin_dal import AsyncDB

#: Live streams platform filter -- node hardcoded this; port verbatim.
_LIVE_PLATFORM = "twitch"


async def _ensure_streaming_tables(async_dal: Any) -> None:
    """Idempotently bind `community_servers`/`coordination` -- only the columns this bundle reads.

    Mirrors `twitch_shoutout_action.py::_ensure_shoutout_tables`'s own
    "minimal stub, no DDL" convention, `migrate=False` throughout. Must
    run on a `dal` that already has `communities` defined (svc-action's
    own `app.py` startup binds it before `set_bundle_dal()`).
    `penguin_dal.db.AsyncDB.define_table()` is async, so this helper is
    awaited from its call site.
    """
    if "community_servers" not in async_dal.tables:
        await async_dal.define_table(
            "community_servers",
            Field("community_id", "reference communities", notnull=True),
            Field("platform", "string", notnull=True),
            Field("platform_server_id", "string", notnull=True),
            Field("status", "string", default="pending"),
            migrate=False,
        )
    if "coordination" not in async_dal.tables:
        await async_dal.define_table(
            "coordination",
            Field("entity_id", "string", notnull=True),
            Field("platform", "string", notnull=True),
            Field("server_id", "string"),
            Field("channel_id", "string"),
            Field("channel_name", "string"),
            Field("is_live", "boolean", default=False),
            Field("viewer_count", "integer", default=0),
            Field("live_since", "datetime"),
            Field("stream_title", "string"),
            Field("game_name", "string"),
            Field("thumbnail_url", "string"),
            Field("last_updated", "datetime"),
            migrate=False,
        )


@dataclass(slots=True, frozen=True)
class LiveStreamDTO:  # noqa: N801 -- camelCase fields match v2 wire contract
    """Wire DTO for one live stream row -- camelCase matching v2 API contract."""

    entityId: str  # noqa: N815
    platform: str
    channelId: str | None  # noqa: N815
    channelName: str | None  # noqa: N815
    isLive: bool  # noqa: N815
    liveSince: str | None  # noqa: N815
    viewerCount: int  # noqa: N815
    title: str
    game: str
    thumbnailUrl: str  # noqa: N815


@dataclass(slots=True, frozen=True)
class StreamDetailsDTO(LiveStreamDTO):  # noqa: N801
    """`LiveStreamDTO` plus `lastActivity` -- only `get_stream_details` returns this."""

    lastActivity: str | None = None  # noqa: N815


def _iso(value: datetime | str | None) -> str | None:
    """Format a datetime as ISO8601, or pass through if already a string."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat()


async def list_streams(
    envelope: StageEnvelope,
    config: Mapping[str, Any],
    *,
    http_client: httpx.AsyncClient,
) -> TransportResult:
    """Query live streams by community -- entrypoint for the action stage.

    Payload must contain:
    - `query`: one of "get_live_streams", "get_featured_streams", "get_stream_details"
    - `entity_id`: string stream entity ID (required only for "get_stream_details")

    Community ID is always read from the bundle context (envelope's isolation boundary),
    never from the payload, to prevent cross-tenant IDOR. A payload `community_id`
    is rejected if present and differs from the context (security.md Tenant Isolation).

    Returns a TransportResult with formatted DTOs (as JSON) in the `detail` field.
    Raises `NonRetryableTransportError` for invalid queries, missing required
    fields, or no matching data (404).
    """
    payload = envelope.event.payload
    query_type = payload.get("query")

    # Get tenant/community from the frozen API (per APP_BUNDLE_AUTHORING.md §5).
    # Never read from payload -- context comes from the envelope's isolation boundary.
    async_dal = get_bundle_dal()
    await _ensure_streaming_tables(async_dal)
    ctx = get_bundle_context()

    # Reject if payload supplies a community_id that differs from context (IDOR guard).
    payload_community_id = payload.get("community_id")
    if payload_community_id is not None:
        try:
            payload_community_id_int = int(payload_community_id)
        except (ValueError, TypeError):
            payload_community_id_int = None
        ctx_community_id_int = int(ctx.community) if ctx.community else None
        if payload_community_id_int != ctx_community_id_int:
            raise NonRetryableTransportError(
                "streaming bundle: payload 'community_id' does not match envelope context; "
                "suspicious request rejected"
            )

    # Use community from context
    community_id_raw = int(ctx.community) if ctx.community else None
    if community_id_raw is None:
        raise NonRetryableTransportError(
            "streaming bundle: context.community is None; "
            "tenant-wide activation not supported"
        )

    result_dto: list[LiveStreamDTO] | StreamDetailsDTO
    if query_type == "get_live_streams":
        result_dto = await _get_live_streams(async_dal, community_id_raw)
    elif query_type == "get_featured_streams":
        result_dto = await _get_featured_streams(async_dal, community_id_raw)
    elif query_type == "get_stream_details":
        entity_id = payload.get("entity_id")
        if not isinstance(entity_id, str) or not entity_id:
            raise NonRetryableTransportError(
                "streaming bundle query 'get_stream_details' requires 'entity_id' "
                f"string; got {type(entity_id)}"
            )
        result_dto = await _get_stream_details(async_dal, community_id_raw, entity_id)
    else:
        raise NonRetryableTransportError(
            f"streaming bundle received unknown query type {query_type!r}; "
            "expected 'get_live_streams', 'get_featured_streams', or 'get_stream_details'"
        )

    # Serialize DTOs to JSON for the audit log detail field.
    if isinstance(result_dto, list):
        detail = json.dumps([asdict(dto) for dto in result_dto], default=str)
    else:
        detail = json.dumps(asdict(result_dto), default=str)

    return TransportResult(
        transport="bundle",
        detail=detail,
        http_status=200,
    )


async def _get_live_streams(async_dal: AsyncDB, community_id: int) -> list[LiveStreamDTO]:
    """Port of v2 `get_live_streams` -- all live streams ordered by viewer count DESC."""
    query = _build_join_query(async_dal, community_id)
    rows = await async_dal(query).select(
        async_dal.coordination.table,
        orderby=~async_dal.coordination.viewer_count,
    )
    return [_stream_dto(row) for row in rows]


async def _get_featured_streams(async_dal: AsyncDB, community_id: int) -> list[LiveStreamDTO]:
    """Port of v2 `get_featured_streams` -- top 5 live streams by viewer count."""
    query = _build_join_query(async_dal, community_id)
    rows = await async_dal(query).select(
        async_dal.coordination.table,
        orderby=~async_dal.coordination.viewer_count,
        limitby=(0, 5),
    )
    return [_stream_dto(row) for row in rows]


async def _get_stream_details(
    async_dal: AsyncDB, community_id: int, entity_id: str
) -> StreamDetailsDTO:
    """Port of v2 `get_stream_details` -- one stream by entity_id, raises 404 if not found."""
    query = _build_join_query(async_dal, community_id) & (
        async_dal.coordination.entity_id == entity_id
    )
    rows = await async_dal(query).select(async_dal.coordination.table)
    if not rows:
        raise NonRetryableTransportError(
            f"streaming bundle: no live stream found for entity_id={entity_id!r} "
            f"in community_id={community_id}",
            http_status=404,
        )
    row = rows.first()
    base = _stream_dto(row)
    return StreamDetailsDTO(
        entityId=base.entityId,
        platform=base.platform,
        channelId=base.channelId,
        channelName=base.channelName,
        isLive=base.isLive,
        liveSince=base.liveSince,
        viewerCount=base.viewerCount,
        title=base.title,
        game=base.game,
        thumbnailUrl=base.thumbnailUrl,
        lastActivity=_iso(row.last_updated),
    )


def _build_join_query(dal: Any, community_id: int) -> Query:
    """Port of v2's shared `coordination JOIN community_servers` WHERE clause.

    Filters to: approved community servers matching the coordination's
    platform/server_id pair, where coordination.is_live == True.

    The two column-to-column comparisons (`platform`, `platform_server_id`
    vs `server_id`) can't go through `FieldProxy.__eq__` -- it treats its
    `other` argument as a bind-parameter value, not a second column -- so
    those two are built from the raw SQLAlchemy `.column` on each side and
    wrapped back into a `penguin_dal.Query` before joining the rest with
    the normal `&` operator (module docstring).
    """
    cs = dal.community_servers
    coord = dal.coordination
    platform_match = Query(cs.platform.column == coord.platform.column, table=cs.table)
    server_id_match = Query(
        cs.platform_server_id.column == coord.server_id.column, table=cs.table
    )
    return (
        (cs.community_id == community_id)
        & (cs.status == "approved")
        & platform_match
        & server_id_match
        & (coord.platform == _LIVE_PLATFORM)
        & (coord.is_live == True)  # noqa: E712 - FieldProxy comparison, matches SQLAlchemy idiom
    )


def _stream_dto(row: Any) -> LiveStreamDTO:
    """Convert one coordination+community_servers row to a LiveStreamDTO."""
    return LiveStreamDTO(
        entityId=row.entity_id,
        platform=row.platform,
        channelId=row.channel_id,
        channelName=row.channel_name or row.channel_id,
        isLive=bool(row.is_live),
        liveSince=_iso(row.live_since),
        viewerCount=row.viewer_count or 0,
        title=row.stream_title or "",
        game=row.game_name or "",
        thumbnailUrl=row.thumbnail_url or "",
    )
