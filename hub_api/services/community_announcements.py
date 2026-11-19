"""Announcements service -- port of Node's `announcementController.js`.

CRUD + lifecycle (publish/pin/unpin/archive) against the `announcements`
table, plus a simplified `broadcast_to_all_platforms` (port of Node's
378-line `services/broadcastService.js`). The full Node service builds
per-platform embeds and resolves an announcement channel per server; this
port preserves the public contract -- fan out to every active
`community_servers` row matching the requested platforms, record one
`announcement_broadcasts` row per attempt, return the same
`{success, summary, results}` envelope -- without reproducing every
platform-specific embed-formatting branch. TODO(M6-followup): port the
remaining Discord/Slack/Twitch/YouTube embed formatting
(`broadcastService.js` `broadcastToPlatform`/`findAnnouncementChannel`) if
a caller depends on channel auto-discovery rather than a generic POST.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from .community_common import ensure_community_tables
from .community_public_id import lookup_public_ids_sync

_VALID_TYPES = {"general", "important", "event", "update"}
_VALID_STATUSES = {"draft", "published", "archived"}
_TIMEOUT_SECONDS = 10.0

_PLATFORM_ENDPOINTS = {
    "discord": os.getenv("DISCORD_ACTION_URL", "http://localhost:8070"),
    "slack": os.getenv("SLACK_ACTION_URL", "http://localhost:8071"),
    "twitch": os.getenv("TWITCH_ACTION_URL", "http://localhost:8072"),
    "youtube": os.getenv("YOUTUBE_ACTION_URL", "http://localhost:8073"),
}


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else value


def _public_id(dal: Any, community_id: int) -> str:
    """Public UUID for one community id (fails loud when the row has none)."""
    return lookup_public_ids_sync(dal, [community_id])[int(community_id)]


@dataclass(slots=True, frozen=True)
class Announcement:
    """One `announcements` row, camelCase-in-Node fields kept snake_case here (ruff N815)."""

    id: int
    community_id: str
    title: str
    content: str
    announcement_type: str
    status: str
    is_pinned: bool
    created_by: int | None
    created_by_name: str | None
    created_at: str | None
    updated_by: int | None
    updated_at: str | None
    published_at: str | None
    archived_at: str | None


def _to_dto(row: Any, public_community_id: str) -> Announcement:
    """Announcement DTO; `community_id` is the community's PUBLIC UUID (never the numeric id)."""
    return Announcement(
        id=row.id,
        community_id=public_community_id,
        title=row.title,
        content=row.content,
        announcement_type=row.announcement_type,
        status=row.status,
        is_pinned=bool(row.is_pinned),
        created_by=row.created_by,
        created_by_name=row.created_by_name,
        created_at=_iso(row.created_at),
        updated_by=row.updated_by,
        updated_at=_iso(row.updated_at),
        published_at=_iso(row.published_at),
        archived_at=_iso(row.archived_at),
    )


@dataclass(slots=True, frozen=True)
class AnnouncementPagination:
    """Page metadata for `GET .../announcements`."""

    page: int
    limit: int
    total: int
    total_pages: int


@dataclass(slots=True, frozen=True)
class AnnouncementListResponse:
    """Response DTO for `GET .../announcements`."""

    success: bool
    data: list[Announcement]
    pagination: AnnouncementPagination


@dataclass(slots=True, frozen=True)
class AnnouncementResponse:
    """Response DTO for single-announcement endpoints (get/create/update/moderate)."""

    success: bool
    data: Announcement


def list_announcements(
    dal: Any, community_id: int, *, page: int, limit: int, status: str | None, pinned: str | None
) -> AnnouncementListResponse:
    """Paginated announcements, pinned-first then newest-first."""
    ensure_community_tables(dal)
    query = dal.announcements.community_id == community_id
    if status in _VALID_STATUSES:
        query &= dal.announcements.status == status
    if pinned == "true":
        query &= dal.announcements.is_pinned == True  # noqa: E712
    elif pinned == "false":
        query &= dal.announcements.is_pinned == False  # noqa: E712

    total = dal(query).count()
    offset = (page - 1) * limit
    rows = dal(query).select(
        orderby=(~dal.announcements.is_pinned, ~dal.announcements.created_at),
        limitby=(offset, offset + limit),
    )
    public_ids = lookup_public_ids_sync(dal, [community_id])
    return AnnouncementListResponse(
        success=True,
        data=[_to_dto(r, public_ids[int(r.community_id)]) for r in rows],
        pagination=AnnouncementPagination(
            page=page, limit=limit, total=total, total_pages=-(-total // limit) if limit else 0
        ),
    )


def get_announcement(dal: Any, community_id: int, announcement_id: int) -> Announcement | None:
    """Fetch one announcement scoped to its community."""
    ensure_community_tables(dal)
    row = (
        dal(
            (dal.announcements.id == announcement_id)
            & (dal.announcements.community_id == community_id)
        )
        .select()
        .first()
    )
    return _to_dto(row, _public_id(dal, row.community_id)) if row else None


def create_announcement(
    dal: Any, community_id: int, payload: dict[str, Any], user_id: int, username: str
) -> tuple[Announcement | None, str | None]:
    """Validate + insert. Returns `(dto, None)` or `(None, error_message)`."""
    ensure_community_tables(dal)
    title = (payload.get("title") or "").strip()
    content = (payload.get("content") or "").strip()
    if not title:
        return None, "Title is required"
    if len(title) > 255:
        return None, "Title must be 255 characters or less"
    if not content:
        return None, "Content is required"
    if len(content) > 2000:
        return None, "Content must be 2000 characters or less"

    announcement_type = payload.get("announcement_type") or "general"
    if announcement_type not in _VALID_TYPES:
        return None, f"Invalid announcement_type. Must be one of: {', '.join(sorted(_VALID_TYPES))}"

    status = payload.get("status") or "draft"
    if status not in _VALID_STATUSES:
        return None, f"Invalid status. Must be one of: {', '.join(sorted(_VALID_STATUSES))}"

    is_pinned = payload.get("is_pinned") is True
    published_at = datetime.utcnow() if status == "published" else None

    new_id = dal.announcements.insert(
        community_id=community_id,
        title=title,
        content=content,
        announcement_type=announcement_type,
        status=status,
        is_pinned=is_pinned,
        created_by=user_id,
        created_by_name=username or "",
        created_at=datetime.utcnow(),
        published_at=published_at,
    )
    dal.commit()
    row = dal.announcements[new_id]
    return _to_dto(row, _public_id(dal, row.community_id)), None


def update_announcement(
    dal: Any, community_id: int, announcement_id: int, payload: dict[str, Any], user_id: int
) -> tuple[Announcement | None, str | None]:
    """Partial update; only fields present in `payload` change."""
    ensure_community_tables(dal)
    current = (
        dal(
            (dal.announcements.id == announcement_id)
            & (dal.announcements.community_id == community_id)
        )
        .select()
        .first()
    )
    if current is None:
        return None, "__not_found__"

    fields: dict[str, Any] = {}
    if "title" in payload:
        title = (payload["title"] or "").strip()
        if not title:
            return None, "Title must be a non-empty string"
        if len(title) > 255:
            return None, "Title must be 255 characters or less"
        fields["title"] = title
    if "content" in payload:
        content = (payload["content"] or "").strip()
        if not content:
            return None, "Content must be a non-empty string"
        if len(content) > 2000:
            return None, "Content must be 2000 characters or less"
        fields["content"] = content
    if "announcement_type" in payload:
        if payload["announcement_type"] not in _VALID_TYPES:
            return (
                None,
                f"Invalid announcement_type. Must be one of: {', '.join(sorted(_VALID_TYPES))}",
            )
        fields["announcement_type"] = payload["announcement_type"]
    if "is_pinned" in payload:
        fields["is_pinned"] = payload["is_pinned"] is True
    status = payload.get("status")
    if status is not None:
        if status not in _VALID_STATUSES:
            return None, f"Invalid status. Must be one of: {', '.join(sorted(_VALID_STATUSES))}"
        fields["status"] = status
        if status == "published" and current.published_at is None:
            fields["published_at"] = datetime.utcnow()

    fields["updated_by"] = user_id
    fields["updated_at"] = datetime.utcnow()

    dal(dal.announcements.id == announcement_id).update(**fields)
    dal.commit()
    row = dal.announcements[announcement_id]
    return _to_dto(row, _public_id(dal, row.community_id)), None


def _transition(
    dal: Any, community_id: int, announcement_id: int, user_id: int, **fields: Any
) -> Announcement | None:
    ensure_community_tables(dal)
    existing = (
        dal(
            (dal.announcements.id == announcement_id)
            & (dal.announcements.community_id == community_id)
        )
        .select()
        .first()
    )
    if existing is None:
        return None
    fields.setdefault("updated_by", user_id)
    fields.setdefault("updated_at", datetime.utcnow())
    dal(dal.announcements.id == announcement_id).update(**fields)
    dal.commit()
    updated = dal.announcements[announcement_id]
    return _to_dto(updated, _public_id(dal, updated.community_id))


def delete_announcement(
    dal: Any, community_id: int, announcement_id: int, user_id: int
) -> Announcement | None:
    """Soft-delete -- archives rather than removing the row."""
    return _transition(
        dal,
        community_id,
        announcement_id,
        user_id,
        status="archived",
        archived_at=datetime.utcnow(),
    )


def publish_announcement(
    dal: Any, community_id: int, announcement_id: int, user_id: int
) -> Announcement | None:
    """Move a draft announcement to `published`."""
    return _transition(
        dal,
        community_id,
        announcement_id,
        user_id,
        status="published",
        published_at=datetime.utcnow(),
    )


def pin_announcement(
    dal: Any, community_id: int, announcement_id: int, user_id: int
) -> Announcement | None:
    """Toggle `is_pinned`."""
    ensure_community_tables(dal)
    existing = (
        dal(
            (dal.announcements.id == announcement_id)
            & (dal.announcements.community_id == community_id)
        )
        .select()
        .first()
    )
    if existing is None:
        return None
    return _transition(
        dal, community_id, announcement_id, user_id, is_pinned=not existing.is_pinned
    )


def unpin_announcement(
    dal: Any, community_id: int, announcement_id: int, user_id: int
) -> Announcement | None:
    """Force `is_pinned = false`."""
    return _transition(dal, community_id, announcement_id, user_id, is_pinned=False)


def archive_announcement(
    dal: Any, community_id: int, announcement_id: int, user_id: int
) -> Announcement | None:
    """Archive without the soft-delete framing (same effect as `delete_announcement`)."""
    return _transition(
        dal,
        community_id,
        announcement_id,
        user_id,
        status="archived",
        archived_at=datetime.utcnow(),
    )


# ---------------------------------------------------------------------------
# Broadcast (simplified port of broadcastService.js -- see module docstring)
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class BroadcastSummary:
    """Aggregate counts for one broadcast fan-out."""

    total_servers: int
    successful: int
    failed: int


@dataclass(slots=True, frozen=True)
class BroadcastResult:
    """Per-server broadcast outcome."""

    platform: str
    server_id: int
    success: bool
    error: str | None = None


@dataclass(slots=True, frozen=True)
class BroadcastOutcome:
    """Return value of `broadcast_to_all_platforms`."""

    success: bool
    summary: BroadcastSummary
    results: list[BroadcastResult] = field(default_factory=list)
    error: str | None = None


async def _post_to_platform(platform: str, announcement: dict[str, Any]) -> tuple[bool, str | None]:
    endpoint = _PLATFORM_ENDPOINTS.get(platform)
    if endpoint is None:
        return False, f"No action endpoint configured for platform {platform!r}"
    async with httpx.AsyncClient(base_url=endpoint, timeout=_TIMEOUT_SECONDS) as client:
        try:
            resp = await client.post("/internal/announce", json=announcement)
        except httpx.RequestError as exc:
            return False, str(exc)
    return resp.status_code < 400, None if resp.status_code < 400 else f"HTTP {resp.status_code}"


async def broadcast_to_all_platforms(
    dal: Any,
    community_id: int,
    announcement_id: int,
    announcement: dict[str, Any],
    platforms: list[str],
) -> BroadcastOutcome:
    """Fan out to every active `community_servers` row matching `platforms`."""
    ensure_community_tables(dal)
    servers = dal(
        (dal.community_servers.community_id == community_id)
        & (dal.community_servers.platform.belongs(platforms))
    ).select()

    if not servers:
        return BroadcastOutcome(
            success=False,
            summary=BroadcastSummary(total_servers=0, successful=0, failed=0),
            error="No active servers found for target platforms",
        )

    results: list[BroadcastResult] = []
    successful = 0
    for server in servers:
        ok, err = await _post_to_platform(server.platform, announcement)
        results.append(
            BroadcastResult(platform=server.platform, server_id=server.id, success=ok, error=err)
        )
        status = "sent" if ok else "failed"
        dal.announcement_broadcasts.insert(
            announcement_id=announcement_id,
            community_server_id=server.id,
            platform=server.platform,
            status=status,
            error_message=err,
            broadcast_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        )
        if ok:
            successful += 1
    dal.commit()

    failed = len(results) - successful
    return BroadcastOutcome(
        success=failed == 0,
        summary=BroadcastSummary(total_servers=len(servers), successful=successful, failed=failed),
        results=results,
    )


@dataclass(slots=True, frozen=True)
class BroadcastRecord:
    """One `announcement_broadcasts` row, for the broadcast-status endpoint."""

    id: int
    announcement_id: int
    platform: str
    status: str
    broadcast_at: str | None
    completed_at: str | None
    error_message: str | None


def get_broadcast_status(dal: Any, announcement_id: int) -> list[BroadcastRecord]:
    """All broadcast attempts for one announcement, newest first.

    `completed_at` has no backing column (Node's own query referenced one
    that does not exist in the schema -- `config/postgres/migrations/
    000_create_base_schema.sql` + `004_add_missing_tables.sql`) so it is
    always `None` here; kept in the DTO to preserve the response shape.
    """
    ensure_community_tables(dal)
    rows = dal(dal.announcement_broadcasts.announcement_id == announcement_id).select(
        orderby=~dal.announcement_broadcasts.broadcast_at
    )
    return [
        BroadcastRecord(
            id=r.id,
            announcement_id=r.announcement_id,
            platform=r.platform,
            status=r.status,
            broadcast_at=_iso(r.broadcast_at),
            completed_at=None,
            error_message=r.error_message,
        )
        for r in rows
    ]
