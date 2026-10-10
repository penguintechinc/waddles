"""Live ON/OFF status projection (gh #287 S10) -- feeds the overlay/webui "LIVE" badge.

`core/svc_ingest/builtin_handlers/twitch_eventsub_ingest.py` normalizes a Twitch
EventSub `stream.online`/`stream.offline` notification into a
`PlatformEvent` (`event.payload['broadcaster_id']`/`['broadcaster_login']`,
`event.payload['metadata']['started_at'|'type'|'viewer_count']` when
present). `runner.py::_maybe_live_status` is this module's only caller,
firing best-effort for every such event alongside the raid-shoutout hook
(`services/raid_shoutout.py`).

Writes the SAME `coordination` table (`config/postgres/migrations/
004_add_missing_tables.sql`, `UNIQUE(platform, channel_id)`) every
existing live-stream READ path already queries --
`core/svc_action/builtin_handlers/streaming_stream_action.py`,
`hub_api/services/stream_service.py`, `hub_api/services/public_service.py`,
`hub_api/services/community_music_queue_service.py`, and this task's own
`hub_api/services/live_status_service.py` -- rather than inventing a
second, parallel state store (a Redis mirror) that could drift from it.
This is the first WRITER any of those read paths have ever had in this
codebase; the table itself already exists, so no migration is needed.

`entity_id` (`coordination.entity_id`, NOT NULL, not part of the actual
unique key) is synthesized as `f"{platform}:{broadcaster_id}"` -- stable,
globally unique, queryable by `hub_api`'s own `get_stream_details`-shaped
endpoints. `server_id`/`channel_id` are BOTH set to `broadcaster_id` --
`community_servers.platform_server_id` (the join key every read path
above uses) must match `coordination.channel_id`
(`UNIQUE(platform, channel_id)`) for the join to ever resolve a row.

`app_catalog`'s `waddles.streaming.stream.default` action-stage handler
(`core/svc_action/builtin_handlers/streaming_stream_action.py::list_streams`) has
no `subcommand`-based online/offline announcement handler today (its
entrypoint only serves `get_live_streams`/`get_featured_streams`/
`get_stream_details` READ queries) -- per this task's own spec, this
module therefore only logs at INFO on a successful write rather than
enqueueing a synthetic action-stage envelope nobody would consume.

Never raises -- a DB failure degrades to a logged, no-op
`LiveStatusResult(recorded=False, ...)`, same "best-effort hook, must
never break the pipeline" posture as `services/raid_shoutout.py`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol, cast

from flask_core import PlatformEvent

logger = logging.getLogger(__name__)


class _ExecutableDal(Protocol):
    """Structural type for the `flask_core.AsyncDAL` surface this module calls."""

    async def execute(self, sql: str, params: list[Any] | None = None) -> list[Any]: ...


#: The two `PlatformEvent.event_type` values this module ever acts on --
#: matches `core/svc_ingest/builtin_handlers/twitch_eventsub_ingest.py`'s own
#: `KNOWN_EVENT_TYPES` entries.
STREAM_ONLINE_EVENT_TYPE = "stream.online"
STREAM_OFFLINE_EVENT_TYPE = "stream.offline"
LIVE_STATUS_EVENT_TYPES = frozenset({STREAM_ONLINE_EVENT_TYPE, STREAM_OFFLINE_EVENT_TYPE})

#: Real Postgres upsert -- `coordination.UNIQUE(platform, channel_id)` is
#: the actual conflict target; `live_since` is only overwritten on a
#: transition TO live (an `is_live=False` write never clobbers the last
#: known `live_since`, matching `hub_api` read paths that only ever
#: consult `live_since` while `is_live == True`).
_UPSERT_SQL = (
    "INSERT INTO coordination "
    "(entity_id, platform, server_id, channel_id, channel_name, is_live, "
    "viewer_count, live_since, last_updated) "
    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW()) "
    "ON CONFLICT (platform, channel_id) DO UPDATE SET "
    "channel_name = EXCLUDED.channel_name, "
    "is_live = EXCLUDED.is_live, "
    "viewer_count = EXCLUDED.viewer_count, "
    "live_since = CASE WHEN EXCLUDED.is_live THEN EXCLUDED.live_since "
    "ELSE coordination.live_since END, "
    "last_updated = NOW()"
)


@dataclass(slots=True, frozen=True)
class LiveStatusResult:
    """The outcome of `record_live_event` -- whether/how the `coordination` row was written."""

    recorded: bool
    is_live: bool | None
    reason: str


def _resolve_dal(dal: _ExecutableDal | None) -> _ExecutableDal:
    """Return `dal` if given, else the process-wide DAL bound via `flask_core.set_bundle_dal()`."""
    if dal is not None:
        return dal
    from flask_core import get_bundle_dal

    # `flask_core` ships no py.typed marker (`follow_imports = "skip"` in
    # pyproject.toml) -- see `raid_shoutout._resolve_dal`'s identical cast
    # for the same boundary.
    return cast("_ExecutableDal", get_bundle_dal())


async def record_live_event(
    event: PlatformEvent,
    *,
    community: str | None,
    dal: _ExecutableDal | None = None,
) -> LiveStatusResult:
    """Upsert the `coordination` row for one `stream.online`/`stream.offline` event.

    Args:
        event: The normalized live-status `PlatformEvent`. Any
            `event_type` outside :data:`LIVE_STATUS_EVENT_TYPES`
            short-circuits to `recorded=False` before any DB call.
        community: The pipeline's resolved community id -- logged only
            (the `coordination` table is platform+channel scoped, not
            community scoped; per-community read filtering happens at
            the `community_servers` JOIN, on the read side).
        dal: Test-only override; defaults to `flask_core.get_bundle_dal()`.

    Returns:
        A `LiveStatusResult`. Never raises.
    """
    if event.event_type not in LIVE_STATUS_EVENT_TYPES:
        return LiveStatusResult(recorded=False, is_live=None, reason="not_a_live_status_event")

    broadcaster_id = event.payload.get("broadcaster_id")
    if not isinstance(broadcaster_id, str) or not broadcaster_id:
        logger.warning(
            "live_status.missing_broadcaster_id event_type=%s community=%s",
            event.event_type,
            community,
        )
        return LiveStatusResult(recorded=False, is_live=None, reason="missing_broadcaster_id")

    broadcaster_login = event.payload.get("broadcaster_login")
    channel_name = broadcaster_login if isinstance(broadcaster_login, str) else None
    is_live = event.event_type == STREAM_ONLINE_EVENT_TYPE

    metadata = event.payload.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    viewer_count = metadata.get("viewer_count", 0) if is_live else 0
    if not isinstance(viewer_count, int):
        viewer_count = 0

    # `event.occurred_at` is always a real ISO8601 string
    # (`twitch_eventsub_ingest.normalize` stamps one unconditionally) --
    # Postgres implicitly casts the untyped text parameter to
    # `coordination.live_since`'s TIMESTAMP column via the driver's
    # normal parameterized-query path.
    live_since = event.occurred_at if is_live else None
    entity_id = f"{event.platform}:{broadcaster_id}"

    try:
        active_dal = _resolve_dal(dal)
        await active_dal.execute(
            _UPSERT_SQL,
            [
                entity_id,
                event.platform,
                broadcaster_id,
                broadcaster_id,
                channel_name,
                is_live,
                viewer_count,
                live_since,
            ],
        )
    except Exception as exc:  # noqa: BLE001 - best-effort write, must never break the pipeline
        logger.warning(
            "live_status.write_failed platform=%s broadcaster_id=%s error=%s",
            event.platform,
            broadcaster_id,
            exc,
        )
        return LiveStatusResult(recorded=False, is_live=is_live, reason="write_failed")

    logger.info(
        "live_status.recorded platform=%s broadcaster_id=%s channel=%s is_live=%s community=%s",
        event.platform,
        broadcaster_id,
        channel_name,
        is_live,
        community,
    )
    return LiveStatusResult(recorded=True, is_live=is_live, reason="ok")
