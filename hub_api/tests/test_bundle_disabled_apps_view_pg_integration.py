"""`bundle_disabled_apps` (migration 0095) as the REAL restricted `waddles_bundle_reader` login.

The Rust active-set reads filter out globally disabled bundles via
`app_active_versions.app_id NOT IN (SELECT app_id FROM bundle_disabled_apps)`. The reader
role has no SELECT on `app_catalog`, so this proves, on a fully migrated Postgres 17, that
the exact predicate runs for the reader (privilege is checked at plan time even with zero
rows), excludes disabled apps, and that `app_catalog` itself stays denied. Skipped where the
`docker` CLI is unavailable.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg2
import pytest

_ALEMBIC_TESTS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "tests"
if str(_ALEMBIC_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_ALEMBIC_TESTS_DIR))

from pg_docker import (  # noqa: E402  # type: ignore[import-not-found]
    DOCKER_AVAILABLE,
    PgTestDatabase,
    migrated_postgres,
)

_READER = "waddles_bundle_reader"
_READER_DEFAULT_PW = "pg-docker-harness-default-reader-pw"

pytestmark = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """One real Postgres 17 container migrated to head (includes 0095)."""
    with migrated_postgres("hub-api-disabled-apps-view") as db:
        yield db


def _connect(db: PgTestDatabase, *, reader: bool) -> Any:
    """A genuine login: the reader role, or the harness owner for setup."""
    return psycopg2.connect(
        host=db.host,
        port=db.port,
        dbname=db.dbname,
        user=_READER if reader else db.user,
        password=os.environ.get("DB_READER_PASSWORD", _READER_DEFAULT_PW)
        if reader
        else db.password,
    )


def _seed(db: PgTestDatabase) -> None:
    """Two catalog rows: one active, one disabled."""
    conn = _connect(db, reader=False)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("DELETE FROM app_catalog WHERE app_id IN ('app.on', 'app.off')")
            cur.execute(
                "INSERT INTO app_catalog (app_id, status) VALUES "
                "('app.on', 'active'), ('app.off', 'disabled')"
            )
    finally:
        conn.close()


def test_reader_sees_only_disabled_app_ids_through_the_view(pg_db: PgTestDatabase) -> None:
    """The view lists disabled app_ids to the reader; app_catalog stays denied."""
    _seed(pg_db)
    conn = _connect(pg_db, reader=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT app_id FROM bundle_disabled_apps ORDER BY app_id")
            assert [r[0] for r in cur.fetchall()] == ["app.off"]
        conn.rollback()
        with conn.cursor() as cur, pytest.raises(psycopg2.errors.InsufficientPrivilege):
            cur.execute("SELECT app_id FROM app_catalog")
    finally:
        conn.rollback()
        conn.close()


def test_active_set_predicate_runs_as_reader_and_excludes_disabled(pg_db: PgTestDatabase) -> None:
    """The exact crate predicate succeeds as the reader and drops the disabled app."""
    _seed(pg_db)
    conn = _connect(pg_db, reader=True)
    try:
        with conn.cursor() as cur:
            # Against the real table: proves the reader may plan/run the query (0 rows is fine).
            cur.execute(
                "SELECT app_id FROM app_active_versions WHERE tenant_id = 1 AND community_id = 1 "
                "AND app_active_versions.app_id NOT IN (SELECT app_id FROM bundle_disabled_apps)"
            )
            assert cur.fetchall() == []
            # Same predicate over candidate rows: the disabled app is excluded.
            cur.execute(
                "SELECT v.app_id FROM (VALUES ('app.on'), ('app.off')) AS v(app_id) "
                "WHERE v.app_id NOT IN (SELECT app_id FROM bundle_disabled_apps)"
            )
            assert [r[0] for r in cur.fetchall()] == ["app.on"]
    finally:
        conn.rollback()
        conn.close()
