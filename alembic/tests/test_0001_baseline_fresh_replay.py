"""Real-Postgres regression test: a TRUE fresh-DB replay applies every legacy SQL file.

# regression: fresh-DB replay silently skipped ~18 of 112 legacy migrations

The baseline (0001) used to savepoint-and-skip any legacy `config/postgres/migrations/*.sql`
file that failed (duplicate numeric prefixes sorting a dependent file before its
dependency, forward references to later/phantom tables, missing pgvector, ...) and carry
on, so `alembic upgrade head` exited 0 on an empty database while shipping an incomplete
schema (no RLS policies, no cookie/loyalty/calendar objects, ...). The baseline now
retries deferred files and raises if any can never apply; this test pins both halves:
every file lands in `schema_migrations`, and a few objects that used to be silently
missing exist.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg2
import pytest
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, alembic_cli, empty_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_SQL_DIR = Path(__file__).resolve().parents[2] / "config" / "postgres" / "migrations"


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """One genuinely empty Postgres 17 container (no stamp), migrated 0001 -> `head`."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with empty_postgres("0001-fresh-replay") as db:
        alembic_cli("upgrade", "head", dsn=db.dsn)
        yield db


@requires_docker
def test_every_legacy_sql_file_is_recorded_applied(pg_db: PgTestDatabase) -> None:
    """No legacy file may be skipped on a fresh replay (denominator = files on disk)."""
    expected = {p.stem for p in _SQL_DIR.glob("*.sql")}
    assert expected, f"no legacy SQL files found under {_SQL_DIR}"
    with psycopg2.connect(pg_db.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_migrations")
        applied = {row[0] for row in cur.fetchall()}
    assert expected - applied == set()


@requires_docker
def test_formerly_skipped_objects_exist(pg_db: PgTestDatabase) -> None:
    """Objects owned by files that used to be silently skipped are present."""
    with psycopg2.connect(pg_db.dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT to_regclass('public.credential_access_log') IS NOT NULL, "
            "to_regclass('public.calendar_rsvps') IS NOT NULL, "
            "to_regclass('public.ai_knowledge_chunks') IS NOT NULL, "
            "to_regclass('public.server_ban_sync') IS NOT NULL"
        )
        assert cur.fetchone() == (True, True, True, True)
        cur.execute("SELECT count(*) FROM pg_policies WHERE tablename = 'platform_integrations'")
        row = cur.fetchone()
        assert row is not None and row[0] > 0, "031/032 RLS policies were not applied"
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'hub_admin'")
        assert cur.fetchone() is not None
