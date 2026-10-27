"""Community-scoped current live status -- gh #287 S10 (Python side, hub-api half).

Reads the SAME `coordination` (`004_add_missing_tables.sql`) JOIN
`community_servers` (`000_create_base_schema.sql`) projection
`services/stream_service.py`/`services/public_service.py`/
`services/community_music_queue_service.py` already read -- the write
side is `core/svc_process/services/live_status.py`, upserted from Twitch
EventSub `stream.online`/`stream.offline` (`core/svc_ingest/builtin_handlers/
twitch_eventsub_ingest.py`).

Unlike `stream_service.py`'s own `_join_query` (which filters to
`is_live == True` -- "list the currently-live streams"), this module
reads EVERY approved, connected channel's row regardless of live state --
a community's "LIVE" badge must be able to report `live=False` too, not
just omit a row. `platform` is NOT restricted to `_LIVE_PLATFORM`
("twitch") the way `stream_service.py` hardcodes -- this projection is
platform-agnostic by table shape even though the MVP writer is
Twitch-only today (module docstring, `core/svc_process/services/
live_status.py`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


@dataclass(slots=True, frozen=True)
class LiveStreamStatusDTO:
    """One connected channel's current live state."""

    platform: str
    channel: str | None
    live: bool
    since: str | None
    viewer_count: int


@dataclass(slots=True, frozen=True)
class CommunityLiveStatusDTO:
    """The community-wide live summary -- `live`/`platform`/`channel`/`since`/`viewer_count`.

    Summary fields mirror the first LIVE stream found (`streams` ordered
    live-first, then by viewer count descending) -- `live=False` and every
    summary field `None`/`0` when no connected channel is currently live.
    """

    live: bool
    platform: str | None
    channel: str | None
    since: str | None
    viewer_count: int
    streams: list[LiveStreamStatusDTO]


def _join_query(dal: Any, community_id: int) -> Any:
    """`coordination JOIN community_servers` for one community -- no `is_live` filter.

    Unlike `stream_service._join_query`, this intentionally returns BOTH
    live and offline connected channels -- see module docstring.
    """
    return (
        (dal.community_servers.community_id == community_id)
        & (dal.community_servers.status == "approved")
        & (dal.community_servers.platform == dal.coordination.platform)
        & (dal.community_servers.platform_server_id == dal.coordination.server_id)
    )


def _stream_dto(row: Any) -> LiveStreamStatusDTO:
    return LiveStreamStatusDTO(
        platform=row.platform,
        channel=row.channel_name or row.channel_id,
        live=bool(row.is_live),
        since=_iso(row.live_since) if row.is_live else None,
        viewer_count=row.viewer_count or 0,
    )


async def get_live_status(async_dal: Any, dal: Any, *, community_id: int) -> CommunityLiveStatusDTO:
    """One community's current live status -- every connected channel, live-first ordering."""
    rows = await async_dal.select_async(
        dal(_join_query(dal, community_id)),
        dal.coordination.ALL,
        orderby=(~dal.coordination.is_live, ~dal.coordination.viewer_count),
    )
    streams = [_stream_dto(row) for row in rows]
    primary = next((s for s in streams if s.live), None)
    if primary is None:
        return CommunityLiveStatusDTO(
            live=False, platform=None, channel=None, since=None, viewer_count=0, streams=streams
        )
    return CommunityLiveStatusDTO(
        live=True,
        platform=primary.platform,
        channel=primary.channel,
        since=primary.since,
        viewer_count=primary.viewer_count,
        streams=streams,
    )
