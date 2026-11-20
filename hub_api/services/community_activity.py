"""Activity/leaderboard service -- port of Node's `activityController.js`.

Three call surfaces, same as Node: member-facing leaderboard reads
(watch-time/messages/my-stats), community-admin leaderboard config, and
internal (service-to-service) event ingestion (watch sessions, message
events, batch, stale-session sweep) fed by the trigger/router modules.
Complex aggregate queries (RANK() OVER, GROUP BY .. HAVING, upsert with
COALESCE-keyed ON CONFLICT) go through `dal.executesql` -- pydal's query
builder cannot express these, matching `community_common.py`'s stated
convention.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .community_common import ensure_community_tables, execute_positional

_DATE_FILTERS = {
    "weekly": "AND stat_date >= CURRENT_DATE - INTERVAL '7 days'",
    "monthly": "AND stat_date >= CURRENT_DATE - INTERVAL '30 days'",
    "alltime": "",
}


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, str):
        return value
    return str(value)


def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    hours, minutes = divmod(seconds // 60, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


@dataclass(slots=True, frozen=True)
class LeaderboardEntry:
    """One ranked row of a watch-time or message-count leaderboard."""

    rank: int
    user_id: int | None
    username: str | None
    avatar_url: str | None
    value: int
    value_formatted: str | None = None


@dataclass(slots=True, frozen=True)
class Pagination:
    """Offset/limit pagination metadata shared by both leaderboard endpoints."""

    offset: int
    limit: int
    total: int
    has_more: bool


@dataclass(slots=True, frozen=True)
class LeaderboardResponse:
    """Response DTO for `GET .../leaderboard/{watch-time,messages}`."""

    success: bool
    leaderboard: list[LeaderboardEntry]
    pagination: Pagination
    period: str


def _leaderboard_config(dal: Any, community_id: int) -> dict[str, Any]:
    ensure_community_tables(dal)
    row = dal(dal.community_leaderboard_config.community_id == community_id).select().first()
    if row is None:
        return {
            "min_watch_time_minutes": 5,
            "min_message_count": 10,
            "display_limit": 25,
        }
    return {
        "min_watch_time_minutes": row.min_watch_time_minutes or 5,
        "min_message_count": row.min_message_count or 10,
        "display_limit": row.display_limit or 25,
    }


def get_watch_time_leaderboard(
    dal: Any, community_id: int, *, period: str, limit: int, offset: int
) -> LeaderboardResponse:
    """Watch-time leaderboard, gated by `min_watch_time_minutes` (community config)."""
    cfg = _leaderboard_config(dal, community_id)
    min_seconds = cfg["min_watch_time_minutes"] * 60
    effective_limit = min(limit, cfg["display_limit"])
    date_filter = _DATE_FILTERS.get(period, "")

    # `date_filter` is always one of `_DATE_FILTERS`'s three fixed literals,
    # never user input -- the suppression comments below live on the first
    # string line only: that's the line both ruff (S608) and bandit (B608)
    # attribute the concatenation expression to (verified empirically --
    # neither tool's suppression-matching walks the full expression).
    leaderboard_sql = (
        "SELECT COALESCE(asd.hub_user_id, -1) AS user_id,"  # nosec B608  # noqa: S608
        "       COALESCE(u.username, asd.platform_username) AS username,"
        "       u.avatar_url, SUM(asd.watch_time_seconds) AS total_watch_time"
        " FROM activity_stats_daily asd LEFT JOIN hub_users u ON u.id = asd.hub_user_id"
        " WHERE asd.community_id = $1 " + date_filter + " GROUP BY COALESCE(asd.hub_user_id, -1),"
        " COALESCE(u.username, asd.platform_username), u.avatar_url"
        " HAVING SUM(asd.watch_time_seconds) >= $2"
        " ORDER BY total_watch_time DESC LIMIT $3 OFFSET $4"
    )
    rows = execute_positional(
        dal, leaderboard_sql, placeholders=[community_id, min_seconds, effective_limit, offset]
    )
    count_sql = (
        "SELECT COUNT(*) FROM (SELECT COALESCE(hub_user_id, -1) AS uid"  # nosec B608  # noqa: S608
        " FROM activity_stats_daily WHERE community_id = $1 "
        + date_filter
        + " GROUP BY COALESCE(hub_user_id, -1), COALESCE(platform_user_id, '')"
        " HAVING SUM(watch_time_seconds) >= $2) t"
    )
    total_rows = execute_positional(dal, count_sql, placeholders=[community_id, min_seconds])
    total = int(total_rows[0][0]) if total_rows else 0

    leaderboard = [
        LeaderboardEntry(
            rank=offset + idx + 1,
            user_id=int(row[0]) if row[0] and row[0] > 0 else None,
            username=row[1],
            avatar_url=row[2],
            value=int(row[3]),
            value_formatted=_format_duration(int(row[3])),
        )
        for idx, row in enumerate(rows)
    ]
    return LeaderboardResponse(
        success=True,
        leaderboard=leaderboard,
        pagination=Pagination(
            offset=offset,
            limit=effective_limit,
            total=total,
            has_more=offset + len(leaderboard) < total,
        ),
        period=period,
    )


def get_message_leaderboard(
    dal: Any, community_id: int, *, period: str, limit: int, offset: int
) -> LeaderboardResponse:
    """Message-count leaderboard, gated by `min_message_count` (community config)."""
    cfg = _leaderboard_config(dal, community_id)
    min_messages = cfg["min_message_count"]
    effective_limit = min(limit, cfg["display_limit"])
    date_filter = _DATE_FILTERS.get(period, "")

    # See `get_watch_time_leaderboard`'s matching comment on why the
    # suppressions live on the first string line of the concatenation.
    leaderboard_sql = (
        "SELECT COALESCE(asd.hub_user_id, -1) AS user_id,"  # nosec B608  # noqa: S608
        "       COALESCE(u.username, asd.platform_username) AS username,"
        "       u.avatar_url, SUM(asd.message_count) AS total_messages"
        " FROM activity_stats_daily asd LEFT JOIN hub_users u ON u.id = asd.hub_user_id"
        " WHERE asd.community_id = $1 " + date_filter + " GROUP BY COALESCE(asd.hub_user_id, -1),"
        " COALESCE(u.username, asd.platform_username), u.avatar_url"
        " HAVING SUM(asd.message_count) >= $2"
        " ORDER BY total_messages DESC LIMIT $3 OFFSET $4"
    )
    rows = execute_positional(
        dal, leaderboard_sql, placeholders=[community_id, min_messages, effective_limit, offset]
    )
    count_sql = (
        "SELECT COUNT(*) FROM (SELECT COALESCE(hub_user_id, -1) AS uid"  # nosec B608  # noqa: S608
        " FROM activity_stats_daily WHERE community_id = $1 "
        + date_filter
        + " GROUP BY COALESCE(hub_user_id, -1), COALESCE(platform_user_id, '')"
        " HAVING SUM(message_count) >= $2) t"
    )
    total_rows = execute_positional(dal, count_sql, placeholders=[community_id, min_messages])
    total = int(total_rows[0][0]) if total_rows else 0

    leaderboard = [
        LeaderboardEntry(
            rank=offset + idx + 1,
            user_id=int(row[0]) if row[0] and row[0] > 0 else None,
            username=row[1],
            avatar_url=row[2],
            value=int(row[3]),
        )
        for idx, row in enumerate(rows)
    ]
    return LeaderboardResponse(
        success=True,
        leaderboard=leaderboard,
        pagination=Pagination(
            offset=offset,
            limit=effective_limit,
            total=total,
            has_more=offset + len(leaderboard) < total,
        ),
        period=period,
    )


@dataclass(slots=True, frozen=True)
class ActivityPeriodStats:
    """Watch-time + message-count for one time window (all-time/weekly/monthly)."""

    watch_time_seconds: int
    watch_time_formatted: str
    message_count: int


@dataclass(slots=True, frozen=True)
class ActivityRanks:
    """The caller's own rank in each leaderboard, `None` if unranked."""

    watch_time: int | None
    messages: int | None


@dataclass(slots=True, frozen=True)
class MyActivityStats:
    """All-time/weekly/monthly stats + ranks, for `GET .../activity/my-stats`."""

    all_time: ActivityPeriodStats
    weekly: ActivityPeriodStats
    monthly: ActivityPeriodStats
    ranks: ActivityRanks


@dataclass(slots=True, frozen=True)
class MyActivityStatsResponse:
    """Response DTO for `GET .../activity/my-stats`."""

    success: bool
    stats: MyActivityStats


def _period_stats(
    dal: Any, community_id: int, user_id: int, date_filter: str
) -> ActivityPeriodStats:
    # `date_filter` is always one of `_DATE_FILTERS`'s fixed literals; see
    # `get_watch_time_leaderboard`'s comment on the suppression placement.
    sql = (
        "SELECT SUM(watch_time_seconds), SUM(message_count)"  # nosec B608  # noqa: S608
        " FROM activity_stats_daily"
        " WHERE community_id = $1 AND hub_user_id = $2 " + date_filter
    )
    rows = execute_positional(dal, sql, placeholders=[community_id, user_id])
    watch_time = int(rows[0][0] or 0) if rows else 0
    messages = int(rows[0][1] or 0) if rows else 0
    return ActivityPeriodStats(
        watch_time_seconds=watch_time,
        watch_time_formatted=_format_duration(watch_time),
        message_count=messages,
    )


def _user_rank(dal: Any, community_id: int, user_id: int, column: str) -> int | None:
    """The caller's rank in a leaderboard ordered by `column`.

    `column` is always one of the two fixed literals `get_my_activity_stats`
    passes below (`"watch_time_seconds"`/`"message_count"`), never
    caller-supplied; see `get_watch_time_leaderboard`'s comment on the
    suppression placement.
    """
    sql = (
        "SELECT rank FROM ("  # nosec B608  # noqa: S608
        "SELECT hub_user_id, RANK() OVER (ORDER BY SUM(" + column + ") DESC) AS rank"
        " FROM activity_stats_daily WHERE community_id = $1 AND hub_user_id IS NOT NULL"
        " GROUP BY hub_user_id) t WHERE hub_user_id = $2"
    )
    rows = execute_positional(dal, sql, placeholders=[community_id, user_id])
    return int(rows[0][0]) if rows else None


def get_my_activity_stats(dal: Any, community_id: int, user_id: int) -> MyActivityStatsResponse:
    """Caller's own all-time/weekly/monthly stats + leaderboard ranks."""
    ensure_community_tables(dal)
    all_time = _period_stats(dal, community_id, user_id, "")
    weekly = _period_stats(
        dal, community_id, user_id, "AND stat_date >= CURRENT_DATE - INTERVAL '7 days'"
    )
    monthly = _period_stats(
        dal, community_id, user_id, "AND stat_date >= CURRENT_DATE - INTERVAL '30 days'"
    )
    ranks = ActivityRanks(
        watch_time=_user_rank(dal, community_id, user_id, "watch_time_seconds"),
        messages=_user_rank(dal, community_id, user_id, "message_count"),
    )
    return MyActivityStatsResponse(
        success=True,
        stats=MyActivityStats(all_time=all_time, weekly=weekly, monthly=monthly, ranks=ranks),
    )


@dataclass(slots=True, frozen=True)
class LeaderboardConfig:
    """Community leaderboard configuration (admin-managed)."""

    enabled_platforms: list[str] = field(
        default_factory=lambda: ["twitch", "kick", "youtube", "discord"]
    )
    watch_time_enabled: bool = True
    messages_enabled: bool = True
    public_leaderboard: bool = True
    min_watch_time_minutes: int = 5
    min_message_count: int = 10
    display_limit: int = 25


@dataclass(slots=True, frozen=True)
class LeaderboardConfigResponse:
    """Response DTO for `GET .../leaderboard-config`."""

    success: bool
    config: LeaderboardConfig


def get_leaderboard_config(dal: Any, community_id: int) -> LeaderboardConfigResponse:
    """Community leaderboard config, or the documented defaults if unset."""
    ensure_community_tables(dal)
    row = dal(dal.community_leaderboard_config.community_id == community_id).select().first()
    if row is None:
        return LeaderboardConfigResponse(success=True, config=LeaderboardConfig())
    return LeaderboardConfigResponse(
        success=True,
        config=LeaderboardConfig(
            enabled_platforms=row.enabled_platforms or ["twitch", "kick", "youtube", "discord"],
            watch_time_enabled=bool(row.watch_time_enabled),
            messages_enabled=bool(row.messages_enabled),
            public_leaderboard=bool(row.public_leaderboard),
            min_watch_time_minutes=row.min_watch_time_minutes or 5,
            min_message_count=row.min_message_count or 10,
            display_limit=row.display_limit or 25,
        ),
    )


_VALID_PLATFORMS = {"twitch", "kick", "youtube", "discord", "slack", "hub"}


def update_leaderboard_config(dal: Any, community_id: int, payload: dict[str, Any]) -> list[str]:
    """Upsert `community_leaderboard_config`. Returns validation errors (empty = success)."""
    ensure_community_tables(dal)
    enabled_platforms = payload.get("enabled_platforms")
    if enabled_platforms:
        invalid = [p for p in enabled_platforms if p not in _VALID_PLATFORMS]
        if invalid:
            return [f"Invalid platform: {p}" for p in invalid]

    existing = dal(dal.community_leaderboard_config.community_id == community_id).select().first()
    fields = {
        k: v
        for k, v in {
            "enabled_platforms": enabled_platforms,
            "watch_time_enabled": payload.get("watch_time_enabled"),
            "messages_enabled": payload.get("messages_enabled"),
            "public_leaderboard": payload.get("public_leaderboard"),
            "min_watch_time_minutes": payload.get("min_watch_time_minutes"),
            "min_message_count": payload.get("min_message_count"),
            "display_limit": payload.get("display_limit"),
        }.items()
        if v is not None
    }
    if existing is None:
        dal.community_leaderboard_config.insert(community_id=community_id, **fields)
    else:
        dal(dal.community_leaderboard_config.id == existing.id).update(**fields)
    dal.commit()
    return []


# ---------------------------------------------------------------------------
# Internal (service-to-service) ingestion -- X-Service-Key auth, no tenant/JWT
# ---------------------------------------------------------------------------


def _find_hub_user_id(dal: Any, platform: Any, platform_user_id: Any) -> int | None:
    """Look up a linked hub user by platform identity.

    `platform`/`platform_user_id` are typed `Any` -- both come straight
    from an untyped request-body dict (`payload.get(...)`) at every call
    site; the SQL layer, not mypy, is the validation boundary here.
    """
    rows = execute_positional(
        dal,
        "SELECT hub_user_id FROM hub_user_identities WHERE platform = $1 AND platform_user_id = $2",
        placeholders=[platform, platform_user_id],
    )
    return int(rows[0][0]) if rows and rows[0][0] is not None else None


def _update_daily_stats(
    dal: Any,
    community_id: Any,
    hub_user_id: int | None,
    platform_user_id: str | None,
    platform_username: str | None,
    watch_seconds: int,
    message_count: int,
) -> None:
    execute_positional(
        dal,
        """
        INSERT INTO activity_stats_daily
          (community_id, hub_user_id, platform_user_id, platform_username, stat_date,
           watch_time_seconds, message_count)
        VALUES ($1, $2, $3, $4, CURRENT_DATE, $5, $6)
        ON CONFLICT (community_id, COALESCE(hub_user_id, -1),
                     COALESCE(platform_user_id, ''), stat_date)
        DO UPDATE SET
          watch_time_seconds = activity_stats_daily.watch_time_seconds + $5,
          message_count = activity_stats_daily.message_count + $6,
          platform_username = COALESCE($4, activity_stats_daily.platform_username),
          updated_at = NOW()
        """,
        placeholders=[
            community_id,
            hub_user_id,
            platform_user_id,
            platform_username,
            watch_seconds,
            message_count,
        ],
    )
    dal.commit()


def record_watch_session(dal: Any, payload: dict[str, Any]) -> str | None:
    """Handle a join/leave/heartbeat watch-session event. Returns an error message, or `None`."""
    ensure_community_tables(dal)
    event_type = payload.get("eventType")
    community_id = payload.get("communityId")
    platform = payload.get("platform")
    platform_user_id = payload.get("platformUserId")
    channel_id = payload.get("channelId")

    if not all([event_type, community_id, platform, platform_user_id, channel_id]):
        return (
            "Missing required fields: eventType, communityId, platform, platformUserId, channelId"
        )
    if event_type not in {"join", "leave", "heartbeat"}:
        return "eventType must be join, leave, or heartbeat"

    hub_user_id = _find_hub_user_id(dal, platform, platform_user_id)
    platform_username = payload.get("platformUsername")

    if event_type == "join":
        existing = (
            dal(
                (dal.activity_watch_sessions.community_id == community_id)
                & (dal.activity_watch_sessions.platform == platform)
                & (dal.activity_watch_sessions.platform_user_id == platform_user_id)
                & (dal.activity_watch_sessions.channel_id == channel_id)
                & (dal.activity_watch_sessions.is_active == True)  # noqa: E712 -- pydal comparison
            )
            .select()
            .first()
        )
        if existing:
            dal(dal.activity_watch_sessions.id == existing.id).update(updated_at=datetime.utcnow())
        else:
            dal.activity_watch_sessions.insert(
                community_id=community_id,
                hub_user_id=hub_user_id,
                platform=platform,
                platform_user_id=platform_user_id,
                platform_username=platform_username,
                channel_id=channel_id,
                session_start=datetime.utcnow(),
                is_active=True,
            )
        dal.commit()
    elif event_type == "leave":
        rows = execute_positional(
            dal,
            """
            UPDATE activity_watch_sessions
            SET is_active = false, session_end = NOW(),
                duration_seconds = EXTRACT(EPOCH FROM (NOW() - session_start))::INTEGER,
                updated_at = NOW()
            WHERE community_id = $1 AND platform = $2 AND platform_user_id = $3
              AND channel_id = $4 AND is_active = true
            RETURNING id, duration_seconds
            """,
            placeholders=[community_id, platform, platform_user_id, channel_id],
        )
        dal.commit()
        if rows:
            duration = rows[0][1] or 0
            _update_daily_stats(
                dal, community_id, hub_user_id, platform_user_id, platform_username, duration, 0
            )
    else:  # heartbeat
        dal(
            (dal.activity_watch_sessions.community_id == community_id)
            & (dal.activity_watch_sessions.platform == platform)
            & (dal.activity_watch_sessions.platform_user_id == platform_user_id)
            & (dal.activity_watch_sessions.channel_id == channel_id)
            & (dal.activity_watch_sessions.is_active == True)  # noqa: E712
        ).update(updated_at=datetime.utcnow())
        dal.commit()
    return None


def record_message(dal: Any, payload: dict[str, Any]) -> str | None:
    """Handle a single chat-message activity event. Returns an error message, or `None`."""
    ensure_community_tables(dal)
    community_id = payload.get("communityId")
    platform = payload.get("platform")
    platform_user_id = payload.get("platformUserId")
    if not all([community_id, platform, platform_user_id]):
        return "Missing required fields: communityId, platform, platformUserId"

    hub_user_id = _find_hub_user_id(dal, platform, platform_user_id)
    platform_username = payload.get("platformUsername")
    channel_id = payload.get("channelId")

    dal.activity_message_events.insert(
        community_id=community_id,
        hub_user_id=hub_user_id,
        platform=platform,
        platform_user_id=platform_user_id,
        platform_username=platform_username,
        channel_id=channel_id,
    )
    dal.commit()
    _update_daily_stats(dal, community_id, hub_user_id, platform_user_id, platform_username, 0, 1)
    return None


def record_activity_batch(dal: Any, events: list[dict[str, Any]]) -> tuple[int, int]:
    """Batch-ingest up to 100 `message`-type events. Returns (processed, failed)."""
    ensure_community_tables(dal)
    processed = 0
    failed = 0
    for event in events:
        try:
            if event.get("type") != "message":
                continue
            hub_user_id = _find_hub_user_id(dal, event.get("platform"), event.get("platformUserId"))
            dal.activity_message_events.insert(
                community_id=event.get("communityId"),
                hub_user_id=hub_user_id,
                platform=event.get("platform"),
                platform_user_id=event.get("platformUserId"),
                platform_username=event.get("platformUsername"),
                channel_id=event.get("channelId"),
            )
            dal.commit()
            _update_daily_stats(
                dal,
                event.get("communityId"),
                hub_user_id,
                event.get("platformUserId"),
                event.get("platformUsername"),
                0,
                1,
            )
            processed += 1
        except Exception:  # noqa: BLE001 -- one bad event must not abort the batch
            failed += 1
    return processed, failed


def close_stale_watch_sessions(dal: Any, stale_minutes: int) -> int:
    """Force-close watch sessions idle past `stale_minutes`; roll duration into daily stats."""
    ensure_community_tables(dal)
    rows = execute_positional(
        dal,
        """
        UPDATE activity_watch_sessions
        SET is_active = false,
            session_end = updated_at,
            duration_seconds = EXTRACT(EPOCH FROM (updated_at - session_start))::INTEGER
        WHERE is_active = true AND updated_at < NOW() - ($1 || ' minutes')::INTERVAL
        RETURNING community_id, hub_user_id, platform_user_id, platform_username, duration_seconds
        """,
        placeholders=[str(stale_minutes)],
    )
    dal.commit()
    for row in rows:
        _update_daily_stats(dal, row[0], row[1], row[2], row[3], row[4] or 0, 0)
    return len(rows)
