"""Read model for the community music queue page (`waddles.core.service.music-requests`).

The music-requests service bundle owns three db-v2 tables per community
(`requests`, `history`, `settings` -- see `docs/music-requests-service.md`).
hub-api only READS them, from the read replica, to power the member-facing
queue page. Physical table names come exclusively from
`bundle_data_v2.derive_v2_table_identity` (never string-built); every query
filters on BOTH `tenant_id` and `community_id` as bound parameters; the
requester columns are never selected, so no requester identity can reach a
response. A missing table (bundle not installed) yields an empty result.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from services.bundle_data_v2 import derive_v2_table_identity
from services.bundle_install_dal import raw_sql_rows

logger = logging.getLogger(__name__)

#: The music-requests service bundle whose tables are read here.
MUSIC_REQUESTS_APP_ID = "waddles.core.service.music-requests"
#: The OPTIONAL AI DJ app layered on top of the music queue.
DJ_APP_ID = "waddles.core.example.dj"
#: Physical-table provider passed to `derive_v2_table_identity`.
_PROVIDER = "builtin"

QUEUE_LIMIT = 50
HISTORY_LIMIT = 5
TOP_LIMIT = 5

#: `requests.status` codes (contract: 1 queued, 2 playing).
_STATUS_QUEUED = 1
_STATUS_PLAYING = 2
#: `requests.kind` / `history.kind` codes -> wire strings.
_KIND_NAMES: Mapping[int, str] = {1: "viewer", 2: "dj", 3: "imported"}

#: Derived identifiers are `[a-z0-9_]` only; anything else is refused before quoting.
_SAFE_IDENT = re.compile(r"^[A-Za-z0-9_]+$")


@dataclass(slots=True, frozen=True)
class QueueItemDTO:
    """One upcoming or now-playing track -- deliberately carries no requester field."""

    title: str
    artist: str
    source_url: str
    duration_ms: int
    kind: str


@dataclass(slots=True, frozen=True)
class HistoryItemDTO:
    """One recently played track -- deliberately carries no requester field."""

    title: str
    artist: str
    source_url: str
    duration_ms: int
    played_at: str | None


@dataclass(slots=True, frozen=True)
class AiDjDTO:
    """Whether the optional AI DJ layer is currently running for the community."""

    enabled: bool


@dataclass(slots=True, frozen=True)
class TopArtistDTO:
    """One most-played artist for the community (aggregate counts only)."""

    artist: str
    plays: int


@dataclass(slots=True, frozen=True)
class TopSongDTO:
    """One most-played song for the community (aggregate counts only)."""

    title: str
    artist: str
    plays: int


@dataclass(slots=True, frozen=True)
class MusicPageDTO:
    """Full response of `GET /api/v1/communities/<id>/music`."""

    now_playing: QueueItemDTO | None
    queue: list[QueueItemDTO]
    history: list[HistoryItemDTO]
    ai_dj: AiDjDTO
    dmca_safe: bool
    top_artists: list[TopArtistDTO]
    top_songs: list[TopSongDTO]


def _quote(ident: str) -> str:
    """Double-quote a derived identifier, refusing anything outside `[A-Za-z0-9_]`."""
    if not _SAFE_IDENT.fullmatch(ident):
        raise ValueError("unsafe SQL identifier")
    return f'"{ident}"'


def _qualified_table(logical: str) -> str:
    """Return the quoted `schema.table` of one music-requests logical table."""
    identity = derive_v2_table_identity(MUSIC_REQUESTS_APP_ID, logical, provider=_PROVIDER)
    return f"{_quote(identity.schema)}.{_quote(identity.table)}"


def _placeholder(dal: Any) -> str:
    """DB-API placeholder token for `dal`'s driver (`?` for sqlite, `%s` for Postgres)."""
    return "?" if getattr(dal, "_dbname", "") == "sqlite" else "%s"


def _is_missing_table(exc: Exception) -> bool:
    """True when `exc` is the driver's undefined-table error (Postgres 42P01 / sqlite)."""
    return getattr(exc, "pgcode", None) == "42P01" or "no such table" in str(exc).lower()


async def _query(async_dal: Any, read_dal: Any, sql: str, params: list[Any]) -> list[Any]:
    """Run one parameterised SELECT on `read_dal` in the DAL executor; `[]` if the table is absent.

    Ends its transaction either way so the pooled replica connection is never left
    idle-in-transaction. Any error other than a missing table propagates.
    """
    loop = asyncio.get_event_loop()

    def _run() -> list[Any]:
        try:
            rows = read_dal.executesql(sql, placeholders=params)
            read_dal.commit()
            return list(rows or [])
        except Exception as exc:  # noqa: BLE001 - re-raised unless it is the missing-table case
            read_dal.rollback()
            if _is_missing_table(exc):
                return []
            raise

    runner = functools.partial(contextvars.copy_context().run, _run)
    result: list[Any] = await loop.run_in_executor(async_dal.executor, runner)
    return result


def _iso(value: Any) -> str | None:
    """Render a timestamp column as an ISO-8601 string (or `None`)."""
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _queue_item(row: Any) -> QueueItemDTO:
    """Map a `(title, artist, source_url, duration_ms, kind)` row to a `QueueItemDTO`."""
    return QueueItemDTO(
        title=str(row[0] or ""),
        artist=str(row[1] or ""),
        source_url=str(row[2] or ""),
        duration_ms=int(row[3] or 0),
        kind=_KIND_NAMES.get(int(row[4] or 1), "viewer"),
    )


async def _read_requests(
    async_dal: Any, read_dal: Any, *, tenant_id: int, community_id: int
) -> tuple[QueueItemDTO | None, list[QueueItemDTO]]:
    """Return `(now_playing, queue)` for one community from the `requests` table."""
    ph = _placeholder(read_dal)
    table = _qualified_table("requests")
    base = (
        "SELECT title, artist, source_url, duration_ms, kind FROM "  # nosec B608 # noqa: S608
        f"{table} WHERE tenant_id = {ph} AND community_id = {ph} AND status = {ph} "
    )
    playing = await _query(
        async_dal,
        read_dal,
        base + "ORDER BY started_at DESC LIMIT 1",
        [tenant_id, community_id, _STATUS_PLAYING],
    )
    queued = await _query(
        async_dal,
        read_dal,
        base + f"ORDER BY queued_at ASC LIMIT {int(QUEUE_LIMIT)}",
        [tenant_id, community_id, _STATUS_QUEUED],
    )
    return (_queue_item(playing[0]) if playing else None), [_queue_item(r) for r in queued]


async def _read_history(
    async_dal: Any, read_dal: Any, *, tenant_id: int, community_id: int
) -> list[HistoryItemDTO]:
    """Return the last `HISTORY_LIMIT` played tracks, newest first."""
    ph = _placeholder(read_dal)
    table = _qualified_table("history")
    rows = await _query(
        async_dal,
        read_dal,
        "SELECT title, artist, source_url, duration_ms, played_at FROM "  # nosec B608 # noqa: S608
        f"{table} WHERE tenant_id = {ph} AND community_id = {ph} "
        f"ORDER BY played_at DESC LIMIT {int(HISTORY_LIMIT)}",
        [tenant_id, community_id],
    )
    return [
        HistoryItemDTO(
            title=str(r[0] or ""),
            artist=str(r[1] or ""),
            source_url=str(r[2] or ""),
            duration_ms=int(r[3] or 0),
            played_at=_iso(r[4]),
        )
        for r in rows
    ]


async def _read_flags(
    async_dal: Any, read_dal: Any, *, tenant_id: int, community_id: int
) -> tuple[bool, bool]:
    """Return `(ai_dj_on, dmca_safe)` from `settings` (absent row/table = off)."""
    ph = _placeholder(read_dal)
    table = _qualified_table("settings")
    rows = await _query(
        async_dal,
        read_dal,
        f"SELECT skey, ival FROM {table} "  # nosec B608 # noqa: S608
        f"WHERE tenant_id = {ph} AND community_id = {ph} AND skey IN ({ph}, {ph})",
        [tenant_id, community_id, "ai_dj_on", "dmca_safe"],
    )
    values = {str(r[0]): int(r[1] or 0) for r in rows}
    return values.get("ai_dj_on", 0) == 1, values.get("dmca_safe", 0) == 1


async def _read_top(
    async_dal: Any, read_dal: Any, logical: str, columns: str, *, tenant_id: int, community_id: int
) -> list[Any]:
    """Top `TOP_LIMIT` rows of an aggregate play-count table, most played first."""
    ph = _placeholder(read_dal)
    table = _qualified_table(logical)
    return await _query(
        async_dal,
        read_dal,
        f"SELECT {columns}, plays FROM {table} "  # nosec B608 # noqa: S608
        f"WHERE tenant_id = {ph} AND community_id = {ph} "
        f"ORDER BY plays DESC LIMIT {int(TOP_LIMIT)}",
        [tenant_id, community_id],
    )


async def dj_bundle_active(install_dal: Any, *, tenant_id: int, community_id: int) -> bool:
    """True when the DJ bundle is activated for the community (or tenant-wide, community 0).

    Same activation lookup as `bundle_admin_gateway_service.load_active_bundle`.
    """
    rows = await raw_sql_rows(
        install_dal,
        "SELECT 1 AS one FROM app_active_versions "
        "WHERE app_id = :app_id AND tenant_id = :tenant_id "
        "AND community_id IN (:community_id, 0) LIMIT 1",
        {"app_id": DJ_APP_ID, "tenant_id": tenant_id, "community_id": community_id},
    )
    return bool(rows)


async def get_music_page(
    async_dal: Any,
    read_dal: Any,
    install_dal: Any,
    *,
    tenant_id: int,
    community_id: int,
) -> MusicPageDTO:
    """Assemble the member-facing music page from the replica (and the DJ activation lookup)."""
    now_playing, queue = await _read_requests(
        async_dal, read_dal, tenant_id=tenant_id, community_id=community_id
    )
    history = await _read_history(
        async_dal, read_dal, tenant_id=tenant_id, community_id=community_id
    )
    setting_on, dmca_safe = await _read_flags(
        async_dal, read_dal, tenant_id=tenant_id, community_id=community_id
    )
    dj_enabled = setting_on and await dj_bundle_active(
        install_dal, tenant_id=tenant_id, community_id=community_id
    )
    artists = await _read_top(
        async_dal,
        read_dal,
        "artist_plays",
        "artist",
        tenant_id=tenant_id,
        community_id=community_id,
    )
    songs = await _read_top(
        async_dal,
        read_dal,
        "song_plays",
        "title, artist",
        tenant_id=tenant_id,
        community_id=community_id,
    )
    logger.debug(
        "music_page.read",
        extra={"community_id": community_id, "queued": len(queue), "history": len(history)},
    )
    return MusicPageDTO(
        now_playing=now_playing,
        queue=queue,
        history=history,
        ai_dj=AiDjDTO(enabled=dj_enabled),
        dmca_safe=dmca_safe,
        top_artists=[TopArtistDTO(artist=str(r[0] or ""), plays=int(r[1] or 0)) for r in artists],
        top_songs=[
            TopSongDTO(title=str(r[0] or ""), artist=str(r[1] or ""), plays=int(r[2] or 0))
            for r in songs
        ],
    )
