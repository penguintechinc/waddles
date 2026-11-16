"""Legacy Music Station queue -> music-requests service import (trusted control-plane job).

Runs against a throwaway sqlite file through `penguin_dal.AsyncDB`; sqlite has no schemas, so
`_qualified_table` is patched to the bare derived table name (same approach as
`test_v1_community_music.py`). Also proves the retired legacy routes are gone (404).
"""

# ruff: noqa: S608
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from penguin_dal import AsyncDB
from quart import Quart
from sqlalchemy import text

from services import music_legacy_migration as mig
from services.bundle_data_v2 import derive_v2_table_identity

REQUESTER = 4242


def _table(logical: str) -> str:
    """Bare derived physical table name for one music-requests logical table."""
    return derive_v2_table_identity(mig.MUSIC_REQUESTS_APP_ID, logical, provider="builtin").table


async def _exec(db: AsyncDB, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
    """Run one statement in its own committed transaction and return any rows."""
    async with db.engine.begin() as conn:
        result = await conn.execute(text(sql), params or {})
        return list(result.all()) if result.returns_rows else []


async def _make_service_tables(db: AsyncDB) -> None:
    """Create sqlite stand-ins for the service's requests/history/settings tables."""
    await _exec(
        db,
        f'CREATE TABLE "{_table("requests")}" (id INTEGER PRIMARY KEY AUTOINCREMENT, '
        "tenant_id INT, community_id INT, title TEXT, artist TEXT, source_url TEXT, "
        "provider TEXT, duration_ms INT, requester TEXT, kind INT, status INT, "
        "queued_at TEXT, started_at TEXT, seq INT)",
    )
    await _exec(
        db,
        f'CREATE TABLE "{_table("history")}" (id INTEGER PRIMARY KEY AUTOINCREMENT, '
        "tenant_id INT, community_id INT, title TEXT)",
    )
    await _exec(
        db,
        f'CREATE TABLE "{_table("settings")}" (id INTEGER PRIMARY KEY AUTOINCREMENT, '
        "tenant_id INT, community_id INT, skey TEXT, ival INT)",
    )


async def _make_legacy(db: AsyncDB) -> None:
    """Create the legacy music_tracks / music_station_queue tables."""
    await _exec(
        db,
        "CREATE TABLE music_tracks (id INTEGER PRIMARY KEY, tenant_id INT, provider TEXT, "
        "external_id TEXT, title TEXT, artist TEXT, duration_ms INT, url TEXT)",
    )
    await _exec(
        db,
        "CREATE TABLE music_station_queue (id INTEGER PRIMARY KEY, tenant_id INT, "
        "community_id INT, track_id INT, position INT, status TEXT, requested_by INT)",
    )


async def _legacy_row(
    db: AsyncDB, qid: int, t: int, c: int, title: str, url: str, pos: int, status: str = "queued"
) -> None:
    """Insert one legacy track plus its queue row."""
    await _exec(
        db,
        "INSERT INTO music_tracks (id, tenant_id, provider, external_id, title, artist, "
        "duration_ms, url) VALUES (:i, :t, 'youtube', :e, :title, 'Art', 1000, :url)",
        {"i": qid, "t": t, "e": f"e{qid}", "title": title, "url": url},
    )
    await _exec(
        db,
        "INSERT INTO music_station_queue (id, tenant_id, community_id, track_id, position, "
        "status, requested_by) VALUES (:i, :t, :c, :i, :p, :s, :u)",
        {"i": qid, "t": t, "c": c, "p": pos, "s": status, "u": REQUESTER},
    )


@pytest.fixture(autouse=True)
def _patch_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sqlite has no schemas: use the bare derived table name."""
    monkeypatch.setattr(mig, "_qualified_table", lambda logical: f'"{_table(logical)}"')


@pytest.fixture
async def db(tmp_path: Path) -> Any:
    """AsyncDB over a fresh sqlite file that already holds the legacy tables."""
    database = AsyncDB(f"sqlite:///{tmp_path / 'legacy.db'}", pool_size=1, echo=False)
    await _make_legacy(database)
    yield database
    await database.close()


async def _requests(db: AsyncDB, t: int, c: int) -> list[Any]:
    """Imported rows for one community ordered by seq."""
    return await _exec(
        db,
        "SELECT title, source_url, requester, kind, status, seq, provider "
        f'FROM "{_table("requests")}" '
        "WHERE tenant_id = :t AND community_id = :c ORDER BY seq",
        {"t": t, "c": c},
    )


async def _setting(db: AsyncDB, t: int, c: int, key: str) -> int | None:
    """One settings value for a community (None when unset)."""
    rows = await _exec(
        db,
        f'SELECT ival FROM "{_table("settings")}" '
        "WHERE tenant_id=:t AND community_id=:c AND skey=:k",
        {"t": t, "c": c, "k": key},
    )
    return int(rows[0][0]) if rows else None


class TestImport:
    async def test_imports_once_and_only_once(self, db: AsyncDB) -> None:
        await _make_service_tables(db)
        await _legacy_row(db, 1, 1, 10, "queued-b", "https://a.example/b", 2)
        await _legacy_row(db, 2, 1, 10, "playing-a", "https://a.example/a", 1, "playing")
        first = await mig.migrate_all(db)
        assert (first.communities_imported, first.rows_imported) == (1, 2)
        rows = await _requests(db, 1, 10)
        assert [r[0] for r in rows] == ["playing-a", "queued-b"]
        assert [r[5] for r in rows] == [1, 2]
        assert all(r[2] is None and r[3] == 3 and r[4] == 1 for r in rows)
        assert await _setting(db, 1, 10, "imported") == 1
        assert await _setting(db, 1, 10, "seq") == 2
        second = await mig.migrate_all(db)
        assert (second.rows_imported, second.skipped_already_imported) == (0, 1)
        assert len(await _requests(db, 1, 10)) == 2

    async def test_non_empty_queue_not_overwritten(self, db: AsyncDB) -> None:
        await _make_service_tables(db)
        await _exec(
            db,
            f'INSERT INTO "{_table("requests")}" (tenant_id, community_id, title, seq) '
            "VALUES (1, 10, 'live', 1)",
        )
        await _legacy_row(db, 1, 1, 10, "legacy", "https://a.example/x", 1)
        summary = await mig.migrate_all(db)
        assert summary.skipped_nonempty == 1 and summary.rows_imported == 0
        assert [r[0] for r in await _requests(db, 1, 10)] == ["live"]
        assert await _setting(db, 1, 10, "imported") == 1

    async def test_cross_community_isolation(self, db: AsyncDB) -> None:
        await _make_service_tables(db)
        await _exec(
            db,
            f'INSERT INTO "{_table("requests")}" (tenant_id, community_id, title, seq) '
            "VALUES (1, 11, 'other-live', 1)",
        )
        await _legacy_row(db, 1, 1, 10, "mine", "https://a.example/m", 1)
        await _legacy_row(db, 2, 1, 11, "blocked", "https://a.example/n", 1)
        await _legacy_row(db, 3, 2, 10, "other-tenant", "https://a.example/o", 1)
        summary = await mig.migrate_all(db)
        assert (summary.communities_imported, summary.skipped_nonempty) == (2, 1)
        assert [r[0] for r in await _requests(db, 1, 10)] == ["mine"]
        assert [r[0] for r in await _requests(db, 1, 11)] == ["other-live"]
        assert [r[0] for r in await _requests(db, 2, 10)] == ["other-tenant"]
        assert await _setting(db, 2, 10, "imported") == 1

    async def test_missing_service_tables_skip_and_retry_later(self, db: AsyncDB) -> None:
        await _legacy_row(db, 1, 1, 10, "t", "https://a.example/t", 1)
        skipped = await mig.migrate_all(db)
        assert (skipped.skipped_tables_missing, skipped.failed) == (1, 0)
        await _make_service_tables(db)
        retried = await mig.migrate_all(db)
        assert retried.rows_imported == 1

    async def test_https_only_and_no_requester(self, db: AsyncDB) -> None:
        await _make_service_tables(db)
        await _legacy_row(db, 1, 1, 10, "plain", "http://a.example/p", 1)
        await _legacy_row(db, 2, 1, 10, "js", "javascript:alert(1)", 2)
        await _legacy_row(db, 3, 1, 10, "ok", "https://a.example/ok", 3)
        await mig.migrate_all(db)
        rows = {r[0]: r for r in await _requests(db, 1, 10)}
        assert rows["plain"][1] == "" and rows["js"][1] == ""
        assert rows["ok"][1] == "https://a.example/ok"
        assert all(r[2] is None for r in rows.values())
        assert not any(str(REQUESTER) in str(c) for r in rows.values() for c in r)

    async def test_logs_counts_only(self, db: AsyncDB, caplog: pytest.LogCaptureFixture) -> None:
        await _make_service_tables(db)
        await _legacy_row(db, 1, 1, 10, "SecretTitle", "https://a.example/s", 1)
        with caplog.at_level(logging.DEBUG):
            await mig.migrate_all(db)
        logged = " ".join(
            f"{r.getMessage()} {r.__dict__}"
            for r in caplog.records
            if r.name.startswith("services.music_legacy_migration")
        )
        assert "SecretTitle" not in logged and "a.example" not in logged

    async def test_no_legacy_tables_is_noop(self, tmp_path: Path) -> None:
        empty = AsyncDB(f"sqlite:///{tmp_path / 'empty.db'}", pool_size=1, echo=False)
        try:
            summary = await mig.migrate_all(empty)
        finally:
            await empty.close()
        assert summary.communities_seen == 0


class TestLegacyRoutesGone:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/api/v1/admin/1/music-station/queue"),
            ("POST", "/api/v1/admin/1/music-station/queue/requests"),
            ("PUT", "/api/v1/admin/1/music-station/policy"),
            ("GET", "/api/v1/public/communities/1/music-station/queue"),
        ],
    )
    async def test_404(self, method: str, path: str) -> None:
        from blueprints import register_blueprints

        app = Quart(__name__)
        register_blueprints(app)
        resp = await app.test_client().open(path, method=method)
        assert resp.status_code == 404
