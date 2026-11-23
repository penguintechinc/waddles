"""Shoutout service -- ports `adminController.js`'s shoutout functions.

Ports the *live* implementation wired to `/api/v1/admin/:communityId/
shoutout/*` via `routes/admin.js` (`adminController.getShoutoutConfig`
et al) -- not `controllers/shoutoutController.js`, which `grep -rl
shoutoutController routes/` shows is never `require`d by any route file
(dead code, reads a different, also-unrouted table pair). See
`bot_tables.py`'s module docstring for the table-name discrepancy this
resolves.

Every function takes a raw pydal `DAL` and runs synchronous queries --
callers (`blueprints/v1/bot.py`) wrap each call in `asyncio.to_thread`
per backend-python.md's "no sync DB in handler" rule; this module stays
framework-agnostic and unit-testable without an event loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .public_id_rows import row_public_id


@dataclass(slots=True)
class ShoutoutConfig:
    """Mirrors `adminController.js`'s `formatShoutoutConfig` field-for-field."""

    soEnabled: bool
    soPermission: str
    vsoEnabled: bool
    vsoPermission: str
    autoShoutoutMode: str
    triggerFirstMessage: bool
    triggerRaidHost: bool
    widgetPosition: str
    widgetDurationSeconds: int
    cooldownMinutes: int


@dataclass(slots=True)
class ShoutoutCreator:
    """A row from `shoutout_creators`, camelCased to match the JSON contract; `id` = public UUID."""

    id: str
    platform: str
    platformUsername: str
    addedBy: int | None = None
    createdAt: str | None = None


@dataclass(slots=True)
class ShoutoutHistoryEntry:
    """A row from `shoutout_history`, camelCased to match the JSON contract."""

    id: int
    platform: str
    targetUsername: str
    shoutoutType: str
    triggerType: str
    triggeredBy: str | None = None
    createdAt: str | None = None


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _format_config(row: Any) -> ShoutoutConfig:
    return ShoutoutConfig(
        soEnabled=bool(row.so_enabled),
        soPermission=row.so_permission,
        vsoEnabled=bool(row.vso_enabled),
        vsoPermission=row.vso_permission,
        autoShoutoutMode=row.auto_shoutout_mode,
        triggerFirstMessage=bool(row.trigger_first_message),
        triggerRaidHost=bool(row.trigger_raid_host),
        widgetPosition=row.widget_position,
        widgetDurationSeconds=row.widget_duration_seconds,
        cooldownMinutes=row.cooldown_minutes,
    )


def get_or_create_shoutout_config(dal: Any, community_id: int) -> ShoutoutConfig:
    """`GET .../shoutout/config` -- auto-creates a default row on first access."""
    row = dal(dal.shoutout_config.community_id == community_id).select().first()
    if row is None:
        new_id = dal.shoutout_config.insert(community_id=community_id)
        dal.commit()
        row = dal.shoutout_config[new_id]
    return _format_config(row)


def update_shoutout_config(
    dal: Any,
    community_id: int,
    *,
    so_enabled: bool,
    so_permission: str,
    vso_enabled: bool,
    vso_permission: str,
    auto_shoutout_mode: str,
    trigger_first_message: bool,
    trigger_raid_host: bool,
    widget_position: str,
    widget_duration_seconds: int,
    cooldown_minutes: int,
) -> ShoutoutConfig:
    """`PUT .../shoutout/config` -- full-replace upsert (Node's `ON CONFLICT ... = EXCLUDED`)."""
    fields = dict(
        so_enabled=so_enabled,
        so_permission=so_permission,
        vso_enabled=vso_enabled,
        vso_permission=vso_permission,
        auto_shoutout_mode=auto_shoutout_mode,
        trigger_first_message=trigger_first_message,
        trigger_raid_host=trigger_raid_host,
        widget_position=widget_position,
        widget_duration_seconds=widget_duration_seconds,
        cooldown_minutes=cooldown_minutes,
    )
    dal.shoutout_config.update_or_insert(
        dal.shoutout_config.community_id == community_id,
        community_id=community_id,
        **fields,
    )
    dal.commit()
    row = dal(dal.shoutout_config.community_id == community_id).select().first()
    return _format_config(row)


def list_shoutout_creators(dal: Any, community_id: int) -> list[ShoutoutCreator]:
    """`GET .../shoutout/creators`, newest first."""
    rows = dal(dal.shoutout_creators.community_id == community_id).select(
        orderby=~dal.shoutout_creators.created_at
    )
    return [
        ShoutoutCreator(
            id=row_public_id(row),
            platform=row.platform,
            platformUsername=row.platform_username,
            addedBy=row.added_by,
            createdAt=_iso(row.created_at),
        )
        for row in rows
    ]


def add_shoutout_creator(
    dal: Any, community_id: int, *, platform: str, username: str, added_by: int
) -> ShoutoutCreator | None:
    """`POST .../shoutout/creators` -- `None` on a `(community_id, platform, username)` conflict."""
    existing = (
        dal(
            (dal.shoutout_creators.community_id == community_id)
            & (dal.shoutout_creators.platform == platform)
            & (dal.shoutout_creators.platform_username == username)
        )
        .select()
        .first()
    )
    if existing is not None:
        return None
    new_id = dal.shoutout_creators.insert(
        community_id=community_id,
        platform=platform,
        platform_username=username,
        added_by=added_by,
    )
    dal.commit()
    row = dal.shoutout_creators[new_id]
    return ShoutoutCreator(
        id=row_public_id(row),
        platform=row.platform,
        platformUsername=row.platform_username,
        addedBy=row.added_by,
        createdAt=_iso(row.created_at),
    )


def remove_shoutout_creator(dal: Any, community_id: int, creator_id: int) -> bool:
    """`DELETE .../shoutout/creators/:id` -- `True` if a row was removed, `False` if not found."""
    row = (
        dal(
            (dal.shoutout_creators.id == creator_id)
            & (dal.shoutout_creators.community_id == community_id)
        )
        .select()
        .first()
    )
    if row is None:
        return False
    dal(dal.shoutout_creators.id == creator_id).delete()
    dal.commit()
    return True


def list_shoutout_history(
    dal: Any, community_id: int, *, page: int, limit: int
) -> tuple[list[ShoutoutHistoryEntry], int]:
    """`GET .../shoutout/history` -- `(entries, total_count)`; `page`/`limit` already clamped."""
    total = dal(dal.shoutout_history.community_id == community_id).count()
    offset = (page - 1) * limit
    rows = dal(dal.shoutout_history.community_id == community_id).select(
        orderby=~dal.shoutout_history.created_at,
        limitby=(offset, offset + limit),
    )
    entries = [
        ShoutoutHistoryEntry(
            id=row.id,
            platform=row.platform,
            targetUsername=row.target_username,
            shoutoutType=row.shoutout_type,
            triggeredBy=row.triggered_by_username,
            triggerType=row.trigger_type,
            createdAt=_iso(row.created_at),
        )
        for row in rows
    ]
    return entries, total
