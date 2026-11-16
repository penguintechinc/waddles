"""Trusted one-shot import of the legacy Music Station queue into the music-requests service.

The legacy hub-api queue (`music_station_queue` joined to `music_tracks`, migration 072) is
retired; the single queue now lives in the `waddles.core.service.music-requests` bundle's
db-v2 tables. This control-plane job copies each community's still-queued/playing legacy
rows into the service's physical `requests` table exactly once:

* per `(tenant_id, community_id)`, in ONE transaction guarded by a Postgres advisory lock
  (concurrent replicas/CLI runs skip a community another run holds);
* the `settings.imported` marker is checked first and set last, so a finished community never
  re-runs and a non-empty queue/history is never overwritten (it is marked imported instead,
  the same rule the service's own `requests.import` applies);
* physical names come only from `bundle_data_v2.derive_v2_table_identity`, identifiers are
  quoted, every value is a bound parameter and every statement filters on `tenant_id` AND
  `community_id` (the RLS GUCs are also set on Postgres);
* a missing service table (bundle not installed yet) skips the community to retry later;
* imported rows carry `kind=3`, `requester=NULL`, an https-only URL and no requester id;
* only counts are logged -- never titles, URLs or user ids.

The legacy tables are left in place (data retention); nothing reads them any more.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from services.bundle_data_v2 import derive_v2_table_identity

logger = logging.getLogger(__name__)

#: The music-requests service bundle whose tables receive the import.
MUSIC_REQUESTS_APP_ID = "waddles.core.service.music-requests"
_PROVIDER = "builtin"

#: RLS scope GUCs the bundle tables' policy compares against (`bundle_data_ddl`).
_TENANT_GUC = "waddles.tenant_id"
_COMMUNITY_GUC = "waddles.community_id"
#: High half of the per-community advisory-lock key (ascii "MUSI"); low bits carry the ids.
_LOCK_NAMESPACE = 0x4D555349

#: Contract limits (docs/music-requests-service.md): kind 3 imported, status 1 queued.
KIND_IMPORTED = 3
STATUS_QUEUED = 1
MAX_IMPORT = 100  # keep in step with the LIMIT in `_legacy_rows`
_PROVIDER_UNKNOWN = "other"

_SAFE_IDENT = re.compile(r"^[A-Za-z0-9_]+$")


@dataclass(slots=True)
class MigrationSummary:
    """Counts-only outcome of one `migrate_all` run (safe to log: no PII)."""

    communities_seen: int = 0
    communities_imported: int = 0
    rows_imported: int = 0
    skipped_already_imported: int = 0
    skipped_nonempty: int = 0
    skipped_tables_missing: int = 0
    skipped_locked: int = 0
    failed: int = 0


def _quote(ident: str) -> str:
    """Double-quote a derived identifier, refusing anything outside `[A-Za-z0-9_]`."""
    if not _SAFE_IDENT.fullmatch(ident):
        raise ValueError("unsafe SQL identifier")
    return f'"{ident}"'


def _qualified_table(logical: str) -> str:
    """Return the quoted `schema.table` of one music-requests logical table."""
    identity = derive_v2_table_identity(MUSIC_REQUESTS_APP_ID, logical, provider=_PROVIDER)
    return f"{_quote(identity.schema)}.{_quote(identity.table)}"


def _is_missing_table(exc: Exception) -> bool:
    """True when `exc` is the driver's undefined-table error (Postgres 42P01 / sqlite)."""
    orig = getattr(exc, "orig", exc)
    code = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
    msg = str(exc).lower()
    return code == "42P01" or "no such table" in msg or "does not exist" in msg


def _clean_url(raw: Any) -> str:
    """Keep only an https URL that fits the column; anything else becomes `""`."""
    url = str(raw or "").strip()
    if not url.lower().startswith("https://") or len(url) > 500 or any(c.isspace() for c in url):
        return ""
    return url


def _clean_text(raw: Any, limit: int = 200) -> str:
    """Trim and bound a title/artist to the service column width."""
    return str(raw or "").strip()[:limit]


async def _lock_and_scope(conn: AsyncConnection, tenant_id: int, community_id: int) -> bool:
    """On Postgres take the per-community xact advisory lock and set the RLS GUCs.

    Returns False when another run holds the lock. A no-op `True` on other dialects (tests).
    """
    if conn.dialect.name != "postgresql":
        return True
    key = (_LOCK_NAMESPACE << 40) ^ (tenant_id << 20) ^ community_id
    got = (await conn.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": key})).scalar()
    if not got:
        return False
    for guc, value in ((_TENANT_GUC, tenant_id), (_COMMUNITY_GUC, community_id)):
        await conn.execute(
            text("SELECT set_config(:name, :value, true)"), {"name": guc, "value": str(value)}
        )
    return True


async def _scalar_int(conn: AsyncConnection, sql: str, params: dict[str, Any]) -> int | None:
    """Run a one-value SELECT and return it as int (or None when no row)."""
    row = (await conn.execute(text(sql), params)).first()
    return None if row is None or row[0] is None else int(row[0])


async def _read_setting(
    conn: AsyncConnection, settings: str, ids: dict[str, Any], key: str
) -> int | None:
    """Read one `settings.ival` for the community (None when the key is unset)."""
    return await _scalar_int(
        conn,
        f"SELECT ival FROM {settings} "  # nosec B608 # noqa: S608
        "WHERE tenant_id = :tenant_id AND community_id = :community_id AND skey = :skey",
        {**ids, "skey": key},
    )


async def _write_setting(
    conn: AsyncConnection, settings: str, ids: dict[str, Any], key: str, value: int
) -> None:
    """Set one `settings` key (update when present, else insert) inside the open transaction."""
    existing = await _read_setting(conn, settings, ids, key)
    params = {**ids, "skey": key, "ival": value}
    if existing is None:
        await conn.execute(
            text(
                f"INSERT INTO {settings} (tenant_id, community_id, skey, ival) "  # nosec B608 # noqa: S608
                "VALUES (:tenant_id, :community_id, :skey, :ival)"
            ),
            params,
        )
    else:
        await conn.execute(
            text(
                f"UPDATE {settings} SET ival = :ival "  # nosec B608 # noqa: S608
                "WHERE tenant_id = :tenant_id AND community_id = :community_id AND skey = :skey"
            ),
            params,
        )


async def _legacy_rows(conn: AsyncConnection, ids: dict[str, Any]) -> list[Any]:
    """Legacy queued/playing rows (playing first, then queue order) joined to their tracks."""
    result = await conn.execute(
        text(
            "SELECT t.title, t.artist, t.duration_ms, t.url "
            "FROM music_station_queue q "
            "JOIN music_tracks t ON t.id = q.track_id AND t.tenant_id = q.tenant_id "
            "WHERE q.tenant_id = :tenant_id AND q.community_id = :community_id "
            "AND q.status IN ('queued', 'playing') "
            "ORDER BY CASE q.status WHEN 'playing' THEN 0 ELSE 1 END, q.position, q.id "
            "LIMIT 100"
        ),
        ids,
    )
    return list(result.all())


async def _import_one(conn: AsyncConnection, tenant_id: int, community_id: int) -> str | int:
    """Import one community inside the caller's transaction.

    Returns the imported row count, or one of the skip codes `already`, `nonempty`, `locked`.
    Raises on a missing service table (the caller maps it to a retry-later skip).
    """
    ids = {"tenant_id": tenant_id, "community_id": community_id}
    if not await _lock_and_scope(conn, tenant_id, community_id):
        return "locked"
    settings = _qualified_table("settings")
    requests = _qualified_table("requests")
    history = _qualified_table("history")

    if await _read_setting(conn, settings, ids, "imported") == 1:
        return "already"
    existing = 0
    for table in (requests, history):
        existing += (
            await _scalar_int(
                conn,
                f"SELECT COUNT(*) FROM {table} "  # nosec B608 # noqa: S608
                "WHERE tenant_id = :tenant_id AND community_id = :community_id",
                ids,
            )
            or 0
        )
    if existing:
        await _write_setting(conn, settings, ids, "imported", 1)
        return "nonempty"

    seq = await _read_setting(conn, settings, ids, "seq") or 0
    base = datetime.now(UTC)
    sqlite = conn.dialect.name == "sqlite"
    imported = 0
    for row in await _legacy_rows(conn, ids):
        title = _clean_text(row[0])
        if not title:
            continue
        seq += 1
        queued_at = base + timedelta(milliseconds=imported)
        await conn.execute(
            text(
                f"INSERT INTO {requests} (tenant_id, community_id, title, artist, "  # nosec B608 # noqa: S608
                "source_url, provider, duration_ms, requester, kind, status, queued_at, seq) "
                "VALUES (:tenant_id, :community_id, :title, :artist, :source_url, :provider, "
                ":duration_ms, NULL, :kind, :status, :queued_at, :seq)"
            ),
            {
                **ids,
                "title": title,
                "artist": _clean_text(row[1]),
                "source_url": _clean_url(row[3]),
                "provider": _PROVIDER_UNKNOWN,
                "duration_ms": max(0, int(row[2] or 0)),
                "kind": KIND_IMPORTED,
                "status": STATUS_QUEUED,
                "queued_at": queued_at.isoformat() if sqlite else queued_at,
                "seq": seq,
            },
        )
        imported += 1
    if imported:
        await _write_setting(conn, settings, ids, "seq", seq)
    await _write_setting(conn, settings, ids, "imported", 1)
    return imported


async def _legacy_communities(install_dal: Any) -> list[tuple[int, int]]:
    """Distinct `(tenant_id, community_id)` that still hold queued/playing legacy rows."""
    async with install_dal.engine.connect() as conn:
        try:
            rows = (
                await conn.execute(
                    text(
                        "SELECT DISTINCT tenant_id, community_id FROM music_station_queue "
                        "WHERE status IN ('queued', 'playing') ORDER BY tenant_id, community_id"
                    )
                )
            ).all()
        except Exception as exc:  # noqa: BLE001 - only the legacy-table-absent case is tolerated
            if _is_missing_table(exc):
                return []
            raise
    return [(int(r[0]), int(r[1])) for r in rows]


async def migrate_all(install_dal: Any) -> MigrationSummary:
    """Import every legacy community once; idempotent and safe to run concurrently.

    A failing community is logged (counts only) and counted in `failed` so one bad community
    never blocks the rest; the next run retries it because `settings.imported` stays unset.
    """
    summary = MigrationSummary()
    for tenant_id, community_id in await _legacy_communities(install_dal):
        summary.communities_seen += 1
        try:
            async with install_dal.engine.begin() as conn:
                outcome = await _import_one(conn, tenant_id, community_id)
        except Exception as exc:  # noqa: BLE001 - classified below; transaction already rolled back
            if _is_missing_table(exc):
                summary.skipped_tables_missing += 1
            else:
                summary.failed += 1
                logger.exception(
                    "music_legacy_migration.failed",
                    extra={"tenant_id": tenant_id, "community_id": community_id},
                )
            continue
        if outcome == "already":
            summary.skipped_already_imported += 1
        elif outcome == "nonempty":
            summary.skipped_nonempty += 1
        elif outcome == "locked":
            summary.skipped_locked += 1
        else:
            summary.communities_imported += 1
            summary.rows_imported += int(outcome)
    logger.info(
        "music_legacy_migration.done",
        extra={
            "communities_seen": summary.communities_seen,
            "communities_imported": summary.communities_imported,
            "rows_imported": summary.rows_imported,
            "skipped_already_imported": summary.skipped_already_imported,
            "skipped_nonempty": summary.skipped_nonempty,
            "skipped_tables_missing": summary.skipped_tables_missing,
            "skipped_locked": summary.skipped_locked,
            "failed": summary.failed,
        },
    )
    return summary
