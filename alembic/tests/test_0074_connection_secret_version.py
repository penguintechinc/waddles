"""Tests for 0074_connection_secret_version (credential-rotation counter on platform_connections).

Static checks pin the revision chain/id length; the real-Postgres tests prove the column shape and
default (existing rows read 1), the positivity CHECK, and a clean downgrade/upgrade round trip.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import psycopg2
import psycopg2.errors
import pytest
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, alembic_cli, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_PATH = (
    Path(__file__).resolve().parent.parent
    / "versions"
    / "0074_connection_secret_version.py"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0074", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sql(fn_name: str) -> str:
    with patch("alembic.op.execute") as mock_execute:
        getattr(_load(), fn_name)()
    return "\n".join(call.args[0] for call in mock_execute.call_args_list)


class TestStatic:
    def test_the_revision_chain(self) -> None:
        module = _load()
        assert module.revision == "0074_connection_secret_version"
        assert module.down_revision == "0073_youtube_connection_resource"
        assert len(module.revision) <= 32

    def test_upgrade_and_downgrade_sql(self) -> None:
        up, down = _sql("upgrade"), _sql("downgrade")
        assert "secret_version INTEGER NOT NULL DEFAULT 1" in up and "CHECK" in up
        assert "DROP COLUMN IF EXISTS secret_version" in down


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """One real Postgres container migrated to head (includes 0074)."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0074-secret-version") as db:
        yield db


def _one(
    db: PgTestDatabase, statement: str, params: tuple[object, ...] = ()
) -> tuple[object, ...]:
    conn = psycopg2.connect(db.dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(statement, params)
            row = cur.fetchone()
            assert row is not None
            return tuple(row)
    finally:
        conn.close()


@requires_docker
class TestOnPostgres:
    def test_column_shape_default_and_check(self, pg_db: PgTestDatabase) -> None:
        assert _one(
            pg_db,
            "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_name='platform_connections' AND column_name='secret_version'",
        ) == ("integer", "NO", "1")
        tenant = _one(
            pg_db, "INSERT INTO tenants (slug) VALUES ('t-0074') RETURNING id"
        )[0]
        row = _one(
            pg_db,
            "INSERT INTO platform_connections (tenant_id, platform, resource_type, resource_id, "
            "access_token) VALUES (%s, 'twitch', 'twitch_channel', 'r', 'c') RETURNING secret_version",
            (tenant,),
        )
        assert row == (1,)
        with pytest.raises(psycopg2.errors.CheckViolation):
            _one(
                pg_db, "UPDATE platform_connections SET secret_version = 0 RETURNING id"
            )

    def test_downgrade_then_upgrade_round_trips(self, pg_db: PgTestDatabase) -> None:
        alembic_cli("downgrade", "0073_youtube_connection_resource", dsn=pg_db.dsn)
        with pytest.raises(psycopg2.errors.UndefinedColumn):
            _one(pg_db, "SELECT secret_version FROM platform_connections LIMIT 1")
        alembic_cli("upgrade", "head", dsn=pg_db.dsn)
        assert _one(
            pg_db, "SELECT count(*) FROM platform_connections WHERE secret_version >= 1"
        )
