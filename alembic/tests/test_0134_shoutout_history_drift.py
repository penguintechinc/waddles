"""Real-Postgres regression tests for 0134_shoutout_history_drift + the code-vs-DB drift audit.

# regression: shoutout_history lacked shoutout_type/triggered_by_username/trigger_type
(`GET .../shoutout/history` -> UndefinedColumn 500 on alpha), because legacy 003 created the table
first and 046's `CREATE TABLE IF NOT EXISTS` was a no-op.

Also pins the whole class: `scripts/audit_schema_drift.py` (every literal pydal `define_table`
column under hub_api/ vs the migrated DB) must report zero drift. A SKIP means docker is absent --
treat as FAILURE.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path

import psycopg2
import pytest
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, alembic_cli, empty_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

PREVIOUS_REVISION = "0133_precompile_stale_reclaims"
_REPO = Path(__file__).resolve().parents[2]


def _load_audit():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(
        "audit_schema_drift", _REPO / "scripts" / "audit_schema_drift.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["audit_schema_drift"] = module
    spec.loader.exec_module(module)
    return module


def _columns(db: PgTestDatabase, table: str) -> dict[str, tuple[str, str | None]]:
    with psycopg2.connect(db.dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s",
            (table,),
        )
        return {name: (nullable, default) for name, nullable, default in cur.fetchall()}


@pytest.fixture(scope="module")
def db() -> Iterator[PgTestDatabase]:
    """One genuinely empty Postgres, fresh-replayed 0001 -> head (legacy SQL + 0134)."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with empty_postgres("0134-shoutout") as database:
        alembic_cli("upgrade", "head", dsn=database.dsn)
        yield database


@requires_docker
def test_shoutout_history_has_every_column_hub_api_reads(db: PgTestDatabase) -> None:
    cols = _columns(db, "shoutout_history")
    for needed in ("shoutout_type", "triggered_by_username", "trigger_type"):
        assert needed in cols, f"shoutout_history.{needed} missing"
    assert "'text'" in (cols["shoutout_type"][1] or "")
    assert "'manual'" in (cols["trigger_type"][1] or "")
    # legacy 003 `message NOT NULL` must not block rows written in the 046 shape
    assert cols["message"][0] == "YES"


@requires_docker
def test_hub_api_insert_and_select_roundtrip(db: PgTestDatabase) -> None:
    with psycopg2.connect(db.dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO communities (name, tenant_id) "
            "VALUES ('c-0134', (SELECT min(id) FROM tenants)) RETURNING id"
        )
        community_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO shoutout_history (community_id, platform, target_username) "
            "VALUES (%s, 'twitch', 'someone') RETURNING shoutout_type, trigger_type",
            (community_id,),
        )
        assert cur.fetchone() == ("text", "manual")


@requires_docker
def test_inventory_log_actor_column_is_user_id(db: PgTestDatabase) -> None:
    """regression: code joined `il.performed_by_user_id`; the real actor column is `user_id`."""
    cols = _columns(db, "inventory_log")
    assert "user_id" in cols
    assert "performed_by_user_id" not in cols


@requires_docker
def test_no_pydal_column_is_missing_from_the_migrated_schema(
    db: PgTestDatabase,
) -> None:
    audit = _load_audit()
    defined = audit.collect_defined()
    live = audit.live_columns(db.dsn)
    assert len(defined) > 100, "audit examined suspiciously few tables"
    missing_tables, missing_cols = audit.diff(defined, live)
    assert missing_tables == []
    assert missing_cols == []


@requires_docker
def test_downgrade_drops_columns_and_reupgrade_is_idempotent() -> None:
    with empty_postgres("0134-roundtrip") as database:
        alembic_cli("upgrade", "head", dsn=database.dsn)
        alembic_cli("downgrade", PREVIOUS_REVISION, dsn=database.dsn)
        cols = _columns(database, "shoutout_history")
        assert "shoutout_type" not in cols and "trigger_type" not in cols
        alembic_cli("upgrade", "head", dsn=database.dsn)
        assert "shoutout_type" in _columns(database, "shoutout_history")
        # second round trip: the ADD COLUMN IF NOT EXISTS path must be re-runnable
        alembic_cli("downgrade", PREVIOUS_REVISION, dsn=database.dsn)
        alembic_cli("upgrade", "head", dsn=database.dsn)
