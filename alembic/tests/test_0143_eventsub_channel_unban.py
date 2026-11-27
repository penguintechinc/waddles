"""Tests for 0143_eventsub_channel_unban against a real Postgres (`pg_docker.py` harness).

Covers chain position / single head and the `sub_type` CHECK widening (1..7 -> 1..8) with a
downgrade/upgrade round trip.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import psycopg2
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, alembic_cli, migrated_postgres

_ROOT = Path(__file__).resolve().parents[2]
_PATH = _ROOT / "alembic" / "versions" / "0143_eventsub_channel_unban.py"
_CONSTRAINT_DEF = (
    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
    "WHERE conname = 'twitch_eventsub_subscriptions_sub_type_check'"
)


def _load():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("migration_0143", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMigrationMetadata:
    def test_id_fits_varchar32_and_chains_after_the_ban_chain(self) -> None:
        m = _load()
        assert m.revision == "0143_eventsub_channel_unban"
        assert len(m.revision) <= 32
        assert m.down_revision == "0142_floor_ban_unban_observed"

    def test_single_head(self) -> None:
        cfg = Config(str(_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(_ROOT / "alembic"))
        heads = ScriptDirectory.from_config(cfg).get_heads()
        assert len(heads) == 1, f"diverged alembic heads: {heads}"


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0143-eventsub-unban") as db:
        yield db


def _constraint(db: PgTestDatabase) -> str:
    conn = psycopg2.connect(
        host=db.host, port=db.port, user=db.user, password=db.password, dbname=db.dbname
    )
    try:
        with conn.cursor() as cur:
            cur.execute(_CONSTRAINT_DEF)
            row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None, "sub_type CHECK missing"
    return str(row[0])


def test_the_check_allows_type_eight_and_the_round_trip_restores_it(pg_db: PgTestDatabase) -> None:
    assert "8" in _constraint(pg_db)
    alembic_cli("downgrade", "0142_floor_ban_unban_observed", dsn=pg_db.dsn)
    down = _constraint(pg_db)
    assert "7" in down and "8" not in down
    alembic_cli("upgrade", "head", dsn=pg_db.dsn)
    assert "8" in _constraint(pg_db)
