"""Tests for 0145_radio_station_default.

Metadata (id length, chained off 0144, single head) runs everywhere; the real-Postgres
round-trip (columns present, one-default-per-community index enforced, downgrade drops
exactly the two columns) runs where docker is available.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import psycopg2
import psycopg2.errors
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pg_docker import (
    DOCKER_AVAILABLE,
    REPO_ROOT,
    PgTestDatabase,
    alembic_cli,
    migrated_postgres,
)

_MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "versions"
    / "0145_radio_station_default.py"
)
_PRIOR = "0144_ingest_owner_public_ids"


def _load() -> ModuleType:
    """Import the migration module by path."""
    spec = importlib.util.spec_from_file_location("migration_0145", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _owner(db: PgTestDatabase) -> Any:
    """Autocommit owner connection."""
    conn = psycopg2.connect(
        host=db.host,
        port=db.port,
        user=db.user,
        password=db.password,
        dbname=db.dbname,
        connect_timeout=10,
    )
    conn.autocommit = True
    return conn


def _columns(owner: Any) -> set[str]:
    """Column names currently on `community_radio_stations`."""
    with owner.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'community_radio_stations'"
        )
        return {row[0] for row in cur.fetchall()}


def test_metadata_single_head_off_0144() -> None:
    """Revision id <= 32 chars, chained off 0144, and the graph has exactly one head."""
    m = _load()
    assert (m.revision, m.down_revision) == ("0145_radio_station_default", _PRIOR)
    assert len(m.revision) <= 32
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    assert script.get_heads() == ["0145_radio_station_default"]


#: 0012's DDL for the table (+ 0138's public_id). The pg harness stamps 0019 on a minimal
#: bootstrap, so 0012 never runs there -- the test creates the table itself.
_CREATE_0012_TABLE = """
CREATE TABLE community_radio_stations (
  id SERIAL PRIMARY KEY,
  community_id INTEGER,
  name VARCHAR(255),
  url VARCHAR(2048),
  genre VARCHAR(100),
  description TEXT,
  is_active BOOLEAN,
  created_by INTEGER,
  created_at TIMESTAMP,
  updated_at TIMESTAMP,
  public_id UUID NOT NULL DEFAULT gen_random_uuid()
)
"""


@pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)
def test_upgrade_enforces_one_default_and_downgrade_drops_columns() -> None:
    """Missing table = no-op; with the table, head adds both columns + the partial unique
    index; downgrade removes exactly them."""
    with migrated_postgres("0145-radio-default") as db:
        owner = _owner(db)
        # Reached head without the table: no-op, no error.
        assert _columns(owner) == set()
        alembic_cli("downgrade", _PRIOR, dsn=db.dsn)
        with owner.cursor() as cur:
            cur.execute(_CREATE_0012_TABLE)
        alembic_cli("upgrade", "head", dsn=db.dsn)
        assert {"is_default", "last_tested_at"} <= _columns(owner)
        with owner.cursor() as cur:
            cur.execute(
                "INSERT INTO community_radio_stations (community_id, name, url, is_active, "
                "is_default) VALUES (7, 'a', 'https://a.example', TRUE, TRUE), "
                "(7, 'b', 'https://b.example', TRUE, FALSE), "
                "(8, 'c', 'https://c.example', TRUE, TRUE)"
            )
            with pytest.raises(psycopg2.errors.UniqueViolation):
                cur.execute(
                    "UPDATE community_radio_stations SET is_default = TRUE WHERE name = 'b'"
                )

        alembic_cli("downgrade", _PRIOR, dsn=db.dsn)
        cols = _columns(owner)
        assert "is_default" not in cols and "last_tested_at" not in cols
        assert {"name", "url", "public_id"} <= cols

        alembic_cli("upgrade", "head", dsn=db.dsn)
        assert {"is_default", "last_tested_at"} <= _columns(owner)
        owner.close()
