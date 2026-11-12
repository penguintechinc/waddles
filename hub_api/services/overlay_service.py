"""Overlay business logic -- ported from `overlayController.js` (Streaming module, M7).

Manages the per-community browser-source overlay token
`community_overlay_tokens` renders against (svc-presentation reads
`overlay_key` to resolve which community's `full_screen`/`media`/
`crawler` sections to serve -- see `docs/plans/2026-08-31-svc-streaming-
design.md`). Every function here assumes its caller (`blueprints/v1/
overlay.py`) has already run `services.community_access.
require_community_admin()` -- this module has no authz of its own, same
division of responsibility `auth_service.py`/`event_calendar_proxy.py`
already establish (blueprint owns auth + HTTP shape, service owns the
query).

Write-through to `overlay_view_credentials`: svc-presentation does NOT read
`community_overlay_tokens` -- it validates a presented `?key=` as
`SHA-256(key) == overlay_view_credentials.key_hash` (`core/overlay_auth`). Every
create/rotate/disable here therefore writes BOTH tables inside one executor job
(`_run_atomic`) so they never diverge, storing only the hash in the new table and
never logging a raw key. `communities.overlay_code` (alembic 0057) is ensured via
raw SQL on mint/rotate and tolerated-absent on a pre-0057 database.

Overlay key generation: `secrets.token_hex(32)` (64 hex chars) is the
direct Python equivalent of Node's `crypto.randomBytes(32).toString
('hex')` -- both cryptographically-random, unguessable, same length.
Never derived from `community_id` or any other predictable input
(security.md: URLs/tokens must be unguessable, not merely unlisted).

Write paths (`get_or_create_overlay`'s create branch, `update_overlay`,
`rotate_overlay_key`) deliberately build their return value from the
field values already known in Python rather than re-`select_async`-ing
immediately after an `insert_async`/`update_async` -- `hub_api/PORTING.md`
Gotcha #2/#3's uncommitted-write-visibility class of bug, sidestepped the
same way `auth_service.register()` already does (see that function's own
comment), not re-discovered here.

Timestamps are serialized to ISO-8601 strings (`_iso()`) at the DTO
boundary, not left as raw `datetime` objects: `blueprints/v1/overlay.py`
routes all use `services/dto_response.py::jsonify_dto()` (Gotcha #3, a
real `insert_async`/`update_async` + nested-dataclass response), which
bypasses quart-schema's pydantic `TypeAdapter` -- the ONE thing that
would otherwise give `datetime` fields consistent ISO formatting for
free. Left as raw `datetime`, Quart's own default JSON provider formats
dates as an RFC 822 string instead, which parses fine via JS `new
Date(...)` (`AdminStreamOverlays.jsx`) but isn't the ISO 8601 string
Node's `pg` driver + `JSON.stringify` produce today -- `_iso()` matches
that shape instead of leaving it to whichever serializer happens to run.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from config import HubAPIConfig
from services.errors import ApiError, not_found
from services.schema import bind_overlay_tables, bind_streaming_tables

logger = logging.getLogger(__name__)

_DEFAULT_ENABLED_SOURCES: tuple[str, ...] = ("alerts", "chat", "goals", "ticker")

#: Shape pinned by `alembic/versions/0057_communities_overlay_code.py`'s CHECK constraint.
_OVERLAY_CODE_RE = re.compile(r"[0-9a-f]{16}")


def _bind(dal: Any) -> None:
    """Bind every table this module touches (idempotent, per-DAL-instance)."""
    bind_streaming_tables(dal)
    bind_overlay_tables(dal)


def _hash_key(overlay_key: str) -> str:
    """SHA-256 hex of an overlay key -- byte-for-byte `overlay_auth::token::hash_token`.

    svc-presentation validates a presented `?key=` by hashing it with this exact function and
    comparing to `overlay_view_credentials.key_hash`, so the two MUST NOT diverge.
    """
    return hashlib.sha256(overlay_key.encode("utf-8")).hexdigest()


def _iso(value: datetime | None) -> str | None:
    """Format a `datetime` as an ISO-8601 UTC string (`...Z`), matching JS's `toISOString()`."""
    if value is None:
        return None
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(slots=True, frozen=True)
class OverlayRecord:
    """Row-shaped view of `community_overlay_tokens` -- mirrors Node's spread-plus-`overlayUrl`.

    Field names are DELIBERATELY mixed-case: every DB-column field stays
    snake_case (Node spreads the raw pg row verbatim), only `overlayUrl`
    is camelCase (Node's own added field) -- `AdminStreamOverlays.jsx`
    reads both conventions from the same object (`overlay.is_active`,
    `overlay.overlay_key`, `overlay.overlayUrl`), so this DTO matches
    the pinned wire contract byte-for-byte rather than "fixing" the
    casing on one side. See `hub_api/PORTING.md`'s DTO casing note.
    Timestamp fields are pre-formatted ISO strings -- see module
    docstring's `_iso()` note.
    """

    id: int
    community_id: int
    overlay_key: str
    previous_key: str | None
    is_active: bool
    theme_config: dict[str, Any]
    enabled_sources: list[str]
    last_accessed: str | None
    access_count: int
    created_at: str | None
    updated_at: str | None
    rotated_at: str | None
    overlayUrl: str | None


@dataclass(slots=True, frozen=True)
class OverlayDailyStat:
    """One row of `overlay_access_log`'s per-day aggregation."""

    date: str
    access_count: int
    unique_ips: int


@dataclass(slots=True, frozen=True)
class OverlayStats:
    """Response shape for `getOverlayStats` -- `{total: {...}, daily: [...]}`."""

    total_access_count: int
    last_accessed: str | None
    daily: list[OverlayDailyStat] = field(default_factory=list)


#: Surface the all-in-one `overlayUrl` points at: the alert overlay, the primary beta surface.
#: The Rust svc-presentation router has no `/{key}` route -- it serves only
#: `/{overlay_code}/{surface}?key=` -- so the legacy bare-key form would 404.
OVERLAY_URL_SURFACE = "alert_box"


def _browser_source_url(cfg: HubAPIConfig, code: str, surface: str, overlay_key: str) -> str:
    """Build `{base}/{overlay_code}/{surface}?key={key}` (the only form svc-presentation serves).

    Single builder shared by the all-in-one `overlayUrl` (alert_box surface) and the
    browser-sources listing (one URL per `BROWSER_SOURCE_SURFACES` entry).
    """
    return f"{cfg.overlay_base_url}/{code}/{surface}?key={overlay_key}"


def _overlay_url(cfg: HubAPIConfig, code: str | None, overlay_key: str) -> str | None:
    """`overlayUrl` for the all-in-one record; the overlay code is resolved server-side.

    `None` (never a URL the Rust router cannot serve) when the code is unavailable -- only a
    pre-0057 database, already logged at WARNING by `_ensure_overlay_code_sync`. Key minting and
    rotation stay usable there; the dedicated browser-source listing fails loud (503) instead.
    """
    if code is None:
        return None
    return _browser_source_url(cfg, code, OVERLAY_URL_SURFACE, overlay_key)


def _to_record(row: Any, cfg: HubAPIConfig, code: str | None) -> OverlayRecord:
    """Build an `OverlayRecord` from a freshly-selected `community_overlay_tokens` row."""
    return OverlayRecord(
        id=row.id,
        community_id=row.community_id,
        overlay_key=row.overlay_key,
        previous_key=row.previous_key,
        is_active=bool(row.is_active),
        theme_config=row.theme_config or {},
        enabled_sources=row.enabled_sources or list(_DEFAULT_ENABLED_SOURCES),
        last_accessed=_iso(row.last_accessed),
        access_count=row.access_count or 0,
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
        rotated_at=_iso(row.rotated_at),
        overlayUrl=_overlay_url(cfg, code, row.overlay_key),
    )


async def _run_atomic[T](async_dal: Any, dal: Any, fn: Callable[[], T]) -> T:
    """Run `fn` as ONE executor job, committing on success and rolling back on failure.

    Both overlay tables must change together or not at all. `AsyncDAL`'s per-call
    `insert_async`/`update_async` each commit in their own job and `transaction_async()` gives no
    cross-call atomicity (see its docstring), so the multi-table write is bundled into a single
    synchronous function on one executor thread -- the same pattern
    `services/token_billing_service.py` documents.
    """

    def _job() -> T:
        try:
            result = fn()
            dal.commit()
            return result
        except Exception:
            dal.rollback()
            raise

    return await asyncio.get_running_loop().run_in_executor(async_dal.executor, _job)


def _upsert_credentials(
    dal: Any,
    *,
    community_id: int,
    key_hash: str,
    previous_key_hash: str | None,
    is_active: bool,
    rotated_at: datetime | None,
    now: datetime,
    overwrite: bool = True,
) -> None:
    """Atomically `INSERT ... ON CONFLICT (community_id) DO UPDATE` the credentials row.

    `overwrite=False` (used by `_heal_credentials`) turns the conflict action into DO NOTHING so
    a heal that loses a race to a concurrent mint/rotate never clobbers the newer row.

    Stores only hashes (never the plaintext key). A single-statement upsert (not
    check-then-insert) so two concurrent first GETs converge on one row instead of one raising
    `IntegrityError` on `community_id`'s UNIQUE constraint. Must run inside `_run_atomic`
    alongside the `community_overlay_tokens` write so the two tables commit/roll back together
    -- no savepoint or mid-transaction rollback is used. The INSERT text comes from pydal's own
    value escaping (`Table._insert`); `created_at` is deliberately absent from the update set so
    a rotate never rewrites it.
    """
    table = dal.overlay_view_credentials
    insert_sql = table._insert(  # noqa: SLF001 -- pydal's SQL builder, values escaped by pydal
        community_id=community_id,
        key_hash=key_hash,
        previous_key_hash=previous_key_hash,
        is_active=is_active,
        rotated_at=rotated_at,
        created_at=now,
        updated_at=now,
    ).rstrip("; \n")
    if not overwrite:
        dal.executesql(f'{insert_sql} ON CONFLICT ("community_id") DO NOTHING')  # noqa: S608
        return
    update_cols = ("key_hash", "previous_key_hash", "is_active", "rotated_at", "updated_at")
    set_clause = ", ".join(f'"{col}" = excluded."{col}"' for col in update_cols)
    dal.executesql(
        f'{insert_sql} ON CONFLICT ("community_id") DO UPDATE SET {set_clause}'  # noqa: S608
    )


def _heal_credentials(dal: Any, *, community_id: int, token_row: Any, is_active: bool) -> None:
    """Insert the hashed credential row if a legacy token row has none (never overwrites one).

    Covers tokens minted before write-through existed whose one-time migration backfill never
    saw them. The legacy row still holds the plaintext key, so the hash is derivable.
    """
    exists = dal(dal.overlay_view_credentials.community_id == community_id).count()
    if exists:
        return
    now = datetime.now(UTC)
    previous = token_row.previous_key
    _upsert_credentials(
        dal,
        community_id=community_id,
        key_hash=_hash_key(token_row.overlay_key),
        previous_key_hash=_hash_key(previous) if previous else None,
        is_active=is_active,
        rotated_at=token_row.rotated_at,
        now=now,
        overwrite=False,
    )


def _overlay_code_column_exists(dal: Any) -> bool:
    """Return whether `communities.overlay_code` exists (alembic 0057 applied).

    Probes the catalog (`information_schema.columns`; SQLite's `PRAGMA table_info` for the
    file-backed test DB, which has no `information_schema`) instead of attempting the column
    read, so "column absent" is told apart from every other failure. Probe errors propagate.
    """
    try:
        if getattr(dal, "_dbname", "") == "sqlite":
            rows = dal.executesql("PRAGMA table_info(communities)")
            present = any(row[1] == "overlay_code" for row in rows or [])
        else:
            rows = dal.executesql(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = 'communities' AND column_name = 'overlay_code'"
            )
            present = bool(rows)
    except Exception:
        dal.rollback()
        raise
    dal.commit()
    return present


def _ensure_overlay_code_sync(dal: Any, community_id: int) -> str | None:
    """Make sure `communities.overlay_code` is set; return it (`None` if the column is absent).

    The column (alembic 0057) is `NOT NULL DEFAULT encode(gen_random_bytes(8), 'hex')` so a
    post-0057 row always has one; this only generates a code if it is somehow null/empty. It
    uses raw SQL because the column is deliberately NOT mapped on the shared `communities`
    pydal table (mapping it would break every community query on a pre-0057 database).

    Fails loud: ONLY a verified-absent column (pre-0057 database) degrades -- a warning and
    `None`, the mint still succeeds. Any other database error is rolled back and re-raised,
    never swallowed. Commits/rolls back its own work.
    """
    cid = int(community_id)
    if not _overlay_code_column_exists(dal):
        logger.warning(
            "communities.overlay_code unavailable (alembic 0057 not applied?); "
            "skipping overlay_code backfill",
            extra={"community_id": cid},
        )
        return None
    try:
        rows = dal.executesql(f"SELECT overlay_code FROM communities WHERE id = {cid}")  # noqa: S608 -- int-cast id
    except Exception:
        dal.rollback()
        raise
    dal.commit()
    if not rows:
        return None
    current = rows[0][0]
    if current:
        return str(current)

    for _ in range(5):
        code = secrets.token_hex(8)
        if not _OVERLAY_CODE_RE.fullmatch(code):  # defensive: the value is inlined below
            raise RuntimeError("generated overlay_code has an unexpected shape")
        try:
            # int-cast id + regex-validated hex literal -- nothing user-controlled is inlined
            dal.executesql(
                f"UPDATE communities SET overlay_code = '{code}' "  # noqa: S608
                f"WHERE id = {cid} AND (overlay_code IS NULL OR overlay_code = '')"
            )
            dal.commit()
        except Exception:  # noqa: BLE001 -- UNIQUE collision: re-draw
            dal.rollback()
            continue
        rows = dal.executesql(f"SELECT overlay_code FROM communities WHERE id = {cid}")  # noqa: S608
        dal.commit()
        return str(rows[0][0]) if rows and rows[0][0] else None
    logger.error(
        "could not assign communities.overlay_code after retries", extra={"community_id": cid}
    )
    return None


async def _ensure_overlay_code(async_dal: Any, dal: Any, community_id: int) -> str | None:
    """Async wrapper: run `_ensure_overlay_code_sync` on one executor job."""
    return await asyncio.get_running_loop().run_in_executor(
        async_dal.executor, _ensure_overlay_code_sync, dal, community_id
    )


async def get_or_create_overlay(
    async_dal: Any, dal: Any, cfg: HubAPIConfig, *, community_id: int
) -> OverlayRecord:
    """Get the overlay token for `community_id`, creating one on first access.

    Port of `getOverlay` (`GET /:communityId/overlay`) -- Node's
    `SELECT ... ; if no rows, INSERT ... RETURNING`.
    """
    _bind(dal)
    code = await _ensure_overlay_code(async_dal, dal, community_id)
    rows = await async_dal.select_async(
        dal(dal.community_overlay_tokens.community_id == community_id)
    )
    if rows:
        existing = rows.first()
        await _run_atomic(
            async_dal,
            dal,
            lambda: _heal_credentials(
                dal,
                community_id=community_id,
                token_row=existing,
                is_active=bool(existing.is_active),
            ),
        )
        return _to_record(existing, cfg, code)

    overlay_key = secrets.token_hex(32)
    now = datetime.now(UTC)

    def _create() -> int:
        new = dal.community_overlay_tokens.insert(
            community_id=community_id,
            overlay_key=overlay_key,
            previous_key=None,
            is_active=True,
            theme_config={},
            enabled_sources=list(_DEFAULT_ENABLED_SOURCES),
            access_count=0,
            created_at=now,
            updated_at=now,
        )
        _upsert_credentials(
            dal,
            community_id=community_id,
            key_hash=_hash_key(overlay_key),
            previous_key_hash=None,
            is_active=True,
            rotated_at=None,
            now=now,
        )
        return int(new)

    new_id = await _run_atomic(async_dal, dal, _create)
    return OverlayRecord(
        id=new_id,
        community_id=community_id,
        overlay_key=overlay_key,
        previous_key=None,
        is_active=True,
        theme_config={},
        enabled_sources=list(_DEFAULT_ENABLED_SOURCES),
        last_accessed=None,
        access_count=0,
        created_at=_iso(now),
        updated_at=_iso(now),
        rotated_at=None,
        overlayUrl=_overlay_url(cfg, code, overlay_key),
    )


async def update_overlay(
    async_dal: Any,
    dal: Any,
    cfg: HubAPIConfig,
    *,
    community_id: int,
    is_active: bool | None,
    theme_config: dict[str, Any] | None,
    enabled_sources: list[str] | None,
) -> OverlayRecord:
    """Update overlay settings. Port of `updateOverlay` (`PUT /:communityId/overlay`)."""
    _bind(dal)
    code = await _ensure_overlay_code(async_dal, dal, community_id)
    rows = await async_dal.select_async(
        dal(dal.community_overlay_tokens.community_id == community_id)
    )
    if not rows:
        raise not_found("Overlay not found")
    existing = rows.first()

    updates: dict[str, Any] = {"updated_at": datetime.now(UTC)}
    if is_active is not None:
        updates["is_active"] = is_active
    if theme_config is not None:
        updates["theme_config"] = theme_config
    if enabled_sources is not None:
        updates["enabled_sources"] = enabled_sources

    def _apply() -> None:
        dal(dal.community_overlay_tokens.community_id == community_id).update(**updates)
        _heal_credentials(
            dal,
            community_id=community_id,
            token_row=existing,
            is_active=bool(updates.get("is_active", existing.is_active)),
        )
        if is_active is not None:
            # svc-presentation rejects an inactive credential, so the flag must follow.
            dal(dal.overlay_view_credentials.community_id == community_id).update(
                is_active=is_active, updated_at=updates["updated_at"]
            )

    await _run_atomic(async_dal, dal, _apply)

    return OverlayRecord(
        id=existing.id,
        community_id=community_id,
        overlay_key=existing.overlay_key,
        previous_key=existing.previous_key,
        is_active=bool(updates.get("is_active", existing.is_active)),
        theme_config=updates.get("theme_config", existing.theme_config or {}),
        enabled_sources=updates.get(
            "enabled_sources", existing.enabled_sources or list(_DEFAULT_ENABLED_SOURCES)
        ),
        last_accessed=_iso(existing.last_accessed),
        access_count=existing.access_count or 0,
        created_at=_iso(existing.created_at),
        updated_at=_iso(updates["updated_at"]),
        rotated_at=_iso(existing.rotated_at),
        overlayUrl=_overlay_url(cfg, code, existing.overlay_key),
    )


async def rotate_overlay_key(
    async_dal: Any, dal: Any, cfg: HubAPIConfig, *, community_id: int
) -> OverlayRecord:
    """Rotate the overlay key, keeping the old one as `previous_key` for a grace period.

    Port of `rotateKey` (`POST /:communityId/overlay/rotate`) -- Node's
    comment ("Previous key valid for 5 more minutes") describes
    svc-presentation's own grace-period enforcement when resolving an
    overlay_key against BOTH `overlay_key` and `previous_key` within
    `rotated_at + 5m`; out of scope for hub-api's own write path (this
    function's job is only to rotate the columns).

    Writes through to `overlay_view_credentials` in the SAME transaction (current hash ->
    `previous_key_hash`, new hash -> `key_hash`, `rotated_at` = now) so svc-presentation, which
    only validates against that table, accepts the new key and honours the grace window.
    """
    _bind(dal)
    code = await _ensure_overlay_code(async_dal, dal, community_id)
    rows = await async_dal.select_async(
        dal(dal.community_overlay_tokens.community_id == community_id)
    )
    if not rows:
        raise not_found("Overlay not found")
    existing = rows.first()

    new_key = secrets.token_hex(32)
    now = datetime.now(UTC)

    def _rotate() -> None:
        dal(dal.community_overlay_tokens.community_id == community_id).update(
            previous_key=existing.overlay_key,
            overlay_key=new_key,
            rotated_at=now,
            updated_at=now,
        )
        current = dal(dal.overlay_view_credentials.community_id == community_id).select().first()
        # Demote whatever svc-presentation currently accepts; fall back to hashing the legacy
        # plaintext when no credential row exists yet.
        previous_hash = current.key_hash if current is not None else _hash_key(existing.overlay_key)
        _upsert_credentials(
            dal,
            community_id=community_id,
            key_hash=_hash_key(new_key),
            previous_key_hash=previous_hash,
            is_active=bool(existing.is_active),
            rotated_at=now,
            now=now,
        )

    await _run_atomic(async_dal, dal, _rotate)

    return OverlayRecord(
        id=existing.id,
        community_id=community_id,
        overlay_key=new_key,
        previous_key=existing.overlay_key,
        is_active=bool(existing.is_active),
        theme_config=existing.theme_config or {},
        enabled_sources=existing.enabled_sources or list(_DEFAULT_ENABLED_SOURCES),
        last_accessed=_iso(existing.last_accessed),
        access_count=existing.access_count or 0,
        created_at=_iso(existing.created_at),
        updated_at=_iso(now),
        rotated_at=_iso(now),
        overlayUrl=_overlay_url(cfg, code, new_key),
    )


#: Webui source type -> svc-presentation surface (`docs/guides/overlay-browser-source.md`).
BROWSER_SOURCE_SURFACES: dict[str, str] = {
    "ticker": "ticker",
    "media": "media",
    "general": "full_screen",
    "scene": "scene",
}


@dataclass(slots=True, frozen=True)
class BrowserSourceRecord:
    """One OBS browser source -- `{sourceType, url, isActive, createdAt}`, camelCase.

    `url` embeds the VIEW key (`?key=`), so this DTO is only ever built for a
    community admin and the key is never logged.
    """

    sourceType: str
    url: str
    isActive: bool
    createdAt: str | None


async def get_browser_sources(
    async_dal: Any, dal: Any, cfg: HubAPIConfig, *, community_id: int
) -> list[BrowserSourceRecord]:
    """List the community's browser-source URLs, minting the view credential on first use.

    Backed by the single overlay VIEW key + `communities.overlay_code`; fails loud (503) when the
    overlay code is unavailable (alembic 0057 unapplied) rather than emitting a dead URL.
    """
    record = await get_or_create_overlay(async_dal, dal, cfg, community_id=community_id)
    code = await _ensure_overlay_code(async_dal, dal, community_id)
    if code is None:
        raise ApiError(
            "Overlay code unavailable; browser sources cannot be built",
            status_code=503,
            code="SERVICE_UNAVAILABLE",
        )
    return [
        BrowserSourceRecord(
            sourceType=source_type,
            url=_browser_source_url(cfg, code, surface, record.overlay_key),
            isActive=record.is_active,
            createdAt=record.created_at,
        )
        for source_type, surface in BROWSER_SOURCE_SURFACES.items()
    ]


async def get_overlay_stats(
    async_dal: Any, dal: Any, *, community_id: int, days: int
) -> OverlayStats:
    """Get overlay access statistics. Port of `getOverlayStats` (`GET .../overlay/stats`)."""
    _bind(dal)
    cutoff = datetime.now(UTC) - timedelta(days=days)

    log_rows = await async_dal.select_async(
        dal(
            (dal.overlay_access_log.community_id == community_id)
            & (dal.overlay_access_log.accessed_at > cutoff)
        )
    )
    daily: dict[str, dict[str, Any]] = {}
    for row in log_rows:
        accessed_at = row.accessed_at
        day_key = accessed_at.date().isoformat() if accessed_at else "unknown"
        bucket = daily.setdefault(day_key, {"access_count": 0, "ips": set()})
        bucket["access_count"] += 1
        if row.ip_address:
            bucket["ips"].add(row.ip_address)

    daily_stats = [
        OverlayDailyStat(date=day, access_count=data["access_count"], unique_ips=len(data["ips"]))
        for day, data in sorted(daily.items(), reverse=True)
    ]

    total_rows = await async_dal.select_async(
        dal(dal.community_overlay_tokens.community_id == community_id)
    )
    total = total_rows.first() if total_rows else None

    return OverlayStats(
        total_access_count=(total.access_count or 0) if total is not None else 0,
        last_accessed=_iso(total.last_accessed) if total is not None else None,
        daily=daily_stats,
    )


RESOLUTION_DIMENSIONS: dict[str, tuple[int, int]] = {
    "1080p": (1920, 1080),
    "2k": (2560, 1440),
    "4k": (3840, 2160),
}


async def list_available_widgets(
    async_dal: Any, dal: Any, *, community_id: int
) -> list[dict[str, Any]]:
    """Discover overlay widgets available for this community.

    Gathers declared widgets from active/enabled app bundles for `community_id`
    plus any dynamically registered widgets in `overlay_widget_registry`.
    """
    _bind(dal)
    from services.schema import bind_lifecycle_tables

    bind_lifecycle_tables(dal)

    widgets_by_id: dict[str, dict[str, Any]] = {}

    # 1. Query active bundles for this community
    def _query_active_bundles() -> list[dict[str, Any]]:
        if "app_activations" not in dal.tables:
            return []
        try:
            query = (dal.app_activations.community_id == community_id) & (
                dal.app_activations.enabled == True  # noqa: E712
            )
            fields = [dal.app_activations.app_id]
            has_catalog = "app_catalog" in dal.tables
            has_stages = has_catalog and hasattr(dal.app_catalog, "stages")
            if has_stages:
                fields.append(dal.app_catalog.stages)
            left = (
                dal.app_catalog.on(dal.app_activations.app_id == dal.app_catalog.app_id)
                if has_catalog
                else None
            )

            rows = dal(query).select(*fields, left=left) if left else dal(query).select(*fields)
            found = []
            for r in rows:
                stages_obj = (
                    r.app_catalog.stages
                    if hasattr(r, "app_catalog") and hasattr(r.app_catalog, "stages")
                    else {}
                )
                found.append({"app_id": r.app_activations.app_id, "stages": stages_obj or {}})
            return found
        except Exception:
            return []

    active_bundles = await asyncio.get_running_loop().run_in_executor(
        async_dal.executor, _query_active_bundles
    )

    for item in active_bundles:
        app_id = item["app_id"]
        stages = item.get("stages") or {}
        pres_stage = stages.get("presentation") or {}
        declared_widgets = pres_stage.get("overlay_widgets") or []
        for w in declared_widgets:
            if isinstance(w, dict) and "widget_id" in w:
                wid = w["widget_id"]
                widgets_by_id[wid] = {
                    "widget_id": wid,
                    "bundle_app_id": app_id,
                    "display_name": w.get("display_name", wid),
                    "description": w.get("description", ""),
                    "category": w.get("category", "custom"),
                    "output_kind": w.get("output_kind", "text"),
                    "surface_id": w.get("surface_id"),
                    "allow_standalone_output": w.get("allow_standalone_output", True),
                    "default_x": w.get("default_x", 48),
                    "default_y": w.get("default_y", 48),
                    "default_width": w.get("default_width", 400),
                    "default_height": w.get("default_height", 200),
                    "default_z_index": w.get("default_z_index", 10),
                    "props_schema": w.get("props_schema", {}),
                }

    # 2. Query overlay_widget_registry for dynamic widgets
    def _query_registry() -> list[dict[str, Any]]:
        rows = dal(dal.overlay_widget_registry.id > 0).select()
        return [
            {
                "widget_id": r.widget_id,
                "bundle_app_id": r.bundle_app_id,
                "display_name": r.display_name,
                "description": r.description or "",
                "category": r.category or "custom",
                "output_kind": {1: "text", 2: "image", 3: "sanitized_html", 4: "surface"}.get(
                    r.output_kind_code, "text"
                ),
                "surface_id": r.surface_id,
                "allow_standalone_output": r.allow_standalone_output,
                "default_x": r.default_x,
                "default_y": r.default_y,
                "default_width": r.default_width,
                "default_height": r.default_height,
                "default_z_index": r.default_z_index,
                "props_schema": r.props_schema or {},
            }
            for r in rows
        ]

    registry_widgets = await asyncio.get_running_loop().run_in_executor(
        async_dal.executor, _query_registry
    )
    for rw in registry_widgets:
        widgets_by_id[rw["widget_id"]] = rw

    return list(widgets_by_id.values())


async def get_scene_layout(async_dal: Any, dal: Any, *, community_id: int) -> dict[str, Any]:
    """Get the full-screen composite scene layout (canvas theme + active components)."""
    _bind(dal)

    def _query() -> tuple[dict[str, Any], list[dict[str, Any]]]:
        tok = dal(dal.community_overlay_tokens.community_id == community_id).select().first()
        theme_cfg = (tok.theme_config if tok and tok.theme_config else {}) or {}

        # Prefer the dedicated canvas columns (migration 105 / 0083); fall back to
        # the legacy theme_config JSON blob for rows not yet migrated.
        if tok and getattr(tok, "canvas_resolution", None):
            res = tok.canvas_resolution
            w = tok.canvas_width or RESOLUTION_DIMENSIONS.get(res, (1920, 1080))[0]
            h = tok.canvas_height or RESOLUTION_DIMENSIONS.get(res, (1920, 1080))[1]
        else:
            res = theme_cfg.get("resolution", "1080p")
            dims = RESOLUTION_DIMENSIONS.get(res, (1920, 1080))
            w, h = dims[0], dims[1]

        canvas = {
            "theme": theme_cfg.get("theme", "default"),
            "resolution": res,
            "width": w,
            "height": h,
            "primary_color": theme_cfg.get("primary_color", "#1db954"),
            "secondary_color": theme_cfg.get("secondary_color", "#1ed760"),
            "font_family": theme_cfg.get("font_family", "sans-serif"),
        }

        comp_rows = dal(dal.overlay_scene_components.community_id == community_id).select(
            orderby=dal.overlay_scene_components.z_index | dal.overlay_scene_components.id
        )
        components = [
            {
                "instance_id": r.public_id,
                "bundle_app_id": r.bundle_app_id,
                "widget_id": r.widget_id,
                "surface_id": r.surface_id,
                "output_kind": {1: "text", 2: "image", 3: "sanitized_html", 4: "surface"}.get(
                    r.output_kind_code, "text"
                ),
                "display_name": r.display_name,
                "x": r.x_px,
                "y": r.y_px,
                "width": r.width_px,
                "height": r.height_px,
                "z_index": r.z_index,
                "status": "active" if r.status_code == 1 else "hidden",
                "props": r.props or {},
            }
            for r in comp_rows
        ]
        return canvas, components

    canvas, components = await asyncio.get_running_loop().run_in_executor(
        async_dal.executor, _query
    )
    return {"canvas": canvas, "components": components}


async def add_scene_component(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    tenant_id: int,
    data: dict[str, Any],
) -> dict[str, Any]:
    """Add a component to the community's composite overlay scene."""
    _bind(dal)
    widget_id = data.get("widget_id")
    if not widget_id or not isinstance(widget_id, str):
        raise ApiError("widget_id is required", status_code=400, code="BAD_REQUEST")

    x = int(data.get("x", 48))
    y = int(data.get("y", 48))
    width = int(data.get("width", 400))
    height = int(data.get("height", 200))
    z_index = int(data.get("z_index", 10))

    if not (0 <= x <= 3840 and 0 <= y <= 2160):
        raise ApiError(
            "coordinates out of canvas bounds (max 3840x2160)", status_code=400, code="BAD_REQUEST"
        )
    if not (1 <= width <= 3840 and 1 <= height <= 2160):
        raise ApiError(
            "component dimensions out of canvas bounds", status_code=400, code="BAD_REQUEST"
        )
    if not (-1000 <= z_index <= 1000):
        raise ApiError("z_index must be in -1000..=1000", status_code=400, code="BAD_REQUEST")

    public_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    def _insert() -> dict[str, Any]:
        count = dal(dal.overlay_scene_components.community_id == community_id).count()
        if count >= 128:
            raise ApiError(
                "Maximum of 128 scene components reached", status_code=400, code="CAPACITY_REACHED"
            )

        bundle_app_id = data.get("bundle_app_id", "custom")
        surface_id = data.get("surface_id", "custom")
        output_kind = data.get("output_kind", "text")
        output_kind_code = {"text": 1, "image": 2, "sanitized_html": 3, "surface": 4}.get(
            output_kind, 1
        )
        display_name = data.get("display_name", widget_id)

        dal.overlay_scene_components.insert(
            public_id=public_id,
            tenant_id=tenant_id,
            community_id=community_id,
            bundle_app_id=bundle_app_id,
            widget_id=widget_id,
            surface_id=surface_id,
            output_kind_code=output_kind_code,
            display_name=display_name,
            x_px=x,
            y_px=y,
            width_px=width,
            height_px=height,
            z_index=z_index,
            status_code=1,
            props=data.get("props", {}),
            created_at=now,
            updated_at=now,
        )
        return {
            "instance_id": public_id,
            "bundle_app_id": bundle_app_id,
            "widget_id": widget_id,
            "surface_id": surface_id,
            "output_kind": output_kind,
            "display_name": display_name,
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "z_index": z_index,
            "status": "active",
            "props": data.get("props", {}),
        }

    return await _run_atomic(async_dal, dal, _insert)


async def update_scene_component(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    instance_id: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    """Update a scene component's position, size, visibility, or props."""
    _bind(dal)
    now = datetime.now(UTC)

    def _update() -> dict[str, Any]:
        row = (
            dal(
                (dal.overlay_scene_components.public_id == instance_id)
                & (dal.overlay_scene_components.community_id == community_id)
            )
            .select()
            .first()
        )
        if not row:
            raise not_found("Component not found")

        updates: dict[str, Any] = {"updated_at": now}
        if "x" in data and data["x"] is not None:
            updates["x_px"] = int(data["x"])
        if "y" in data and data["y"] is not None:
            updates["y_px"] = int(data["y"])
        if "width" in data and data["width"] is not None:
            updates["width_px"] = int(data["width"])
        if "height" in data and data["height"] is not None:
            updates["height_px"] = int(data["height"])
        if "z_index" in data and data["z_index"] is not None:
            updates["z_index"] = int(data["z_index"])
        if "visible" in data and data["visible"] is not None:
            updates["status_code"] = 1 if data["visible"] else 2
        if "props" in data and data["props"] is not None:
            updates["props"] = data["props"]

        row.update_record(**updates)
        return {
            "instance_id": row.public_id,
            "bundle_app_id": row.bundle_app_id,
            "widget_id": row.widget_id,
            "surface_id": row.surface_id,
            "output_kind": {1: "text", 2: "image", 3: "sanitized_html", 4: "surface"}.get(
                row.output_kind_code, "text"
            ),
            "display_name": row.display_name,
            "x": row.x_px,
            "y": row.y_px,
            "width": row.width_px,
            "height": row.height_px,
            "z_index": row.z_index,
            "status": "active" if row.status_code == 1 else "hidden",
            "props": row.props or {},
        }

    return await _run_atomic(async_dal, dal, _update)


async def remove_scene_component(
    async_dal: Any, dal: Any, *, community_id: int, instance_id: str
) -> bool:
    """Remove a scene component by its instance UUID."""
    _bind(dal)

    def _delete() -> bool:
        count = dal(
            (dal.overlay_scene_components.public_id == instance_id)
            & (dal.overlay_scene_components.community_id == community_id)
        ).delete()
        return bool(count > 0)

    return await _run_atomic(async_dal, dal, _delete)


async def clear_scene_layout(async_dal: Any, dal: Any, *, community_id: int) -> int:
    """Clear all scene components for a community."""
    _bind(dal)

    def _clear() -> int:
        return dal(dal.overlay_scene_components.community_id == community_id).delete()

    return await _run_atomic(async_dal, dal, _clear)


async def set_scene_canvas_theme(
    async_dal: Any, dal: Any, *, community_id: int, data: dict[str, Any]
) -> dict[str, Any]:
    """Configure the canvas theme, resolution (1080p, 2k, 4k), and colors.

    Canvas size (resolution / width / height) is persisted in the dedicated
    ``canvas_resolution``, ``canvas_width``, ``canvas_height`` columns on
    ``community_overlay_tokens`` (migration 105 / alembic 0083).  The legacy
    ``theme_config`` JSON blob is kept in sync for backward compatibility with
    svc-presentation rendering that may still read it.
    """
    _bind(dal)
    res = data.get("resolution", "1080p")
    if res not in RESOLUTION_DIMENSIONS:
        raise ApiError(
            f"Unsupported resolution '{res}', must be '1080p', '2k', or '4k'",
            status_code=400,
            code="BAD_REQUEST",
        )

    dims = RESOLUTION_DIMENSIONS[res]
    canvas_w, canvas_h = dims[0], dims[1]

    def _set() -> dict[str, Any]:
        tok = dal(dal.community_overlay_tokens.community_id == community_id).select().first()
        theme_cfg = (tok.theme_config if tok and tok.theme_config else {}) or {}

        # Dedicated columns — primary source of truth from migration 105 onward.
        theme_cfg["resolution"] = res  # kept in sync for svc-presentation back-compat
        theme_cfg["width"] = canvas_w
        theme_cfg["height"] = canvas_h
        if "theme" in data and data["theme"] is not None:
            theme_cfg["theme"] = data["theme"]
        if "primary_color" in data and data["primary_color"] is not None:
            theme_cfg["primary_color"] = data["primary_color"]
        if "secondary_color" in data and data["secondary_color"] is not None:
            theme_cfg["secondary_color"] = data["secondary_color"]
        if "font_family" in data and data["font_family"] is not None:
            theme_cfg["font_family"] = data["font_family"]

        if tok:
            tok.update_record(
                theme_config=theme_cfg,
                canvas_resolution=res,
                canvas_width=canvas_w,
                canvas_height=canvas_h,
                updated_at=datetime.now(UTC),
            )

        return {
            "theme": theme_cfg.get("theme", "default"),
            "resolution": res,
            "width": canvas_w,
            "height": canvas_h,
            "primary_color": theme_cfg.get("primary_color", "#1db954"),
            "secondary_color": theme_cfg.get("secondary_color", "#1ed760"),
            "font_family": theme_cfg.get("font_family", "sans-serif"),
        }

    return await _run_atomic(async_dal, dal, _set)
