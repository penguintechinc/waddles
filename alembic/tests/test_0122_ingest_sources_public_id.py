"""0122_ingest_sources_public_id: chain, emitted DDL, and real-Postgres backfill + up/down."""

from __future__ import annotations

import asyncio
import importlib.util
import uuid
from pathlib import Path
from types import ModuleType
from unittest import mock

import psycopg2
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pg_docker import DOCKER_AVAILABLE, alembic_cli, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_ROOT = Path(__file__).resolve().parents[2]
_PATH = _ROOT / "alembic" / "versions" / "0122_ingest_sources_public_id.py"
_PREV = "0121_dsar_requests"
_REV = "0122_ingest_sources_public_id"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0122", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_id_length_and_chain() -> None:
    module = _load()
    assert module.revision == _REV
    assert len(module.revision) <= 32
    assert module.down_revision == _PREV


def test_history_has_one_head_and_0122_is_in_its_ancestry() -> None:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1
    assert _REV in {
        r.revision for r in script.walk_revisions(base="base", head=heads[0])
    }


def test_upgrade_and_downgrade_emit_expected_ddl() -> None:
    module = _load()
    with mock.patch.object(module.op, "execute") as execute:
        module.upgrade()
    up = [c.args[0] for c in execute.call_args_list]
    assert len(up) == 5
    # atomic add: NOT NULL + volatile default in ONE statement (no NULL window for concurrent inserts)
    assert (
        "ADD COLUMN IF NOT EXISTS public_id UUID NOT NULL DEFAULT gen_random_uuid()"
        in up[0]
    )
    assert "gen_random_uuid() WHERE public_id IS NULL" in up[1]
    assert "SET DEFAULT gen_random_uuid()" in up[2]
    assert "SET NOT NULL" in up[3]
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_ingest_sources_public_id" in up[4]
    with mock.patch.object(module.op, "execute") as execute:
        module.downgrade()
    down = [c.args[0] for c in execute.call_args_list]
    assert down == [
        "DROP INDEX IF EXISTS uq_ingest_sources_public_id",
        "ALTER TABLE ingest_sources DROP COLUMN IF EXISTS public_id",
    ]


@requires_docker
def test_real_postgres_backfill_constraints_and_down_up() -> None:
    with migrated_postgres("0122-ingest-pid") as db:
        # Back to 0121 (column gone), seed rows the way pre-0122 code wrote them.
        alembic_cli("downgrade", _PREV, dsn=db.dsn)
        conn = psycopg2.connect(db.dsn)
        conn.autocommit = True
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'ingest_sources' AND column_name = 'public_id'"
            )
            assert cur.fetchone()[0] == 0
            cur.execute("INSERT INTO tenants (slug) VALUES ('t-pid') RETURNING id")
            tenant_id = cur.fetchone()[0]
            for i in range(3):
                cur.execute(
                    "INSERT INTO ingest_sources (tenant_id, platform, source_id, label) "
                    "VALUES (%s, 'discord', %s, 'l')",
                    (tenant_id, f"pre-{i}"),
                )
        finally:
            conn.close()

        alembic_cli("upgrade", _REV, dsn=db.dsn)
        conn = psycopg2.connect(db.dsn)
        conn.autocommit = True
        try:
            cur = conn.cursor()
            cur.execute("SELECT public_id FROM ingest_sources ORDER BY id")
            backfilled = [r[0] for r in cur.fetchall()]
            assert len(backfilled) == 3  # denominator: all 3 pre-existing rows examined
            assert all(v is not None for v in backfilled)
            assert len({str(v) for v in backfilled}) == 3  # distinct
            cur.execute(
                "SELECT is_nullable, column_default FROM information_schema.columns "
                "WHERE table_name = 'ingest_sources' AND column_name = 'public_id'"
            )
            nullable, default = cur.fetchone()
            assert nullable == "NO" and "gen_random_uuid" in default
            # new rows get a server-generated uuid; duplicates are refused
            cur.execute(
                "INSERT INTO ingest_sources (tenant_id, platform, source_id, label) "
                "VALUES (%s, 'twitch', 'new', 'l') RETURNING public_id",
                (tenant_id,),
            )
            assert cur.fetchone()[0] is not None
            with pytest.raises(psycopg2.errors.UniqueViolation):
                cur.execute(
                    "INSERT INTO ingest_sources (tenant_id, platform, source_id, label, public_id) "
                    "VALUES (%s, 'twitch', 'dupe', 'l', %s)",
                    (tenant_id, str(backfilled[0])),
                )
        finally:
            conn.close()

        _assert_reflected_uuid_column_binds_a_uuid(db.dsn, backfilled[0])

        alembic_cli("downgrade", _PREV, dsn=db.dsn)
        conn = psycopg2.connect(db.dsn)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'ingest_sources' AND column_name = 'public_id'"
            )
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT to_regclass('uq_ingest_sources_public_id')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT count(*) FROM ingest_sources")
            assert cur.fetchone()[0] == 4  # rows survive the downgrade
        finally:
            conn.close()
        alembic_cli("upgrade", _REV, dsn=db.dsn)  # up again: idempotent re-backfill


def _assert_reflected_uuid_column_binds_a_uuid(dsn: str, expected: uuid.UUID) -> None:
    """The registry service binds a `uuid.UUID` on Postgres: prove asyncpg reflection accepts it."""
    pytest.importorskip("asyncpg")
    from sqlalchemy import MetaData, select
    from sqlalchemy.ext.asyncio import create_async_engine

    async def run() -> int:
        engine = create_async_engine(
            dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
        )
        try:
            async with engine.connect() as conn:
                meta = MetaData()
                await conn.run_sync(lambda c: meta.reflect(c, only=["ingest_sources"]))
                t = meta.tables["ingest_sources"]
                rows = (
                    await conn.execute(select(t.c.id).where(t.c.public_id == expected))
                ).all()
                return len(rows)
        finally:
            await engine.dispose()

    assert asyncio.run(run()) == 1
