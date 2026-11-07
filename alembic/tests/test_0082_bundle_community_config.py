"""Tests for 0082_bundle_community_config: chain, single head, real-PG constraints, downgrade."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import psycopg2
import psycopg2.errors
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pg_docker import DOCKER_AVAILABLE, alembic_cli, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_ROOT = Path(__file__).resolve().parents[2]
_PATH = _ROOT / "alembic" / "versions" / "0082_bundle_community_config.py"
_PREVIOUS = "0081_spectrum_conn_resource"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0082", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_chain_and_id_length() -> None:
    module = _load()
    assert module.down_revision == _PREVIOUS
    assert len(module.revision) <= 32


def test_single_head_contains_0082() -> None:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    assert len(script.get_heads()) == 1
    assert "0082_bundle_community_config" in {r.revision for r in script.walk_revisions()}


@requires_docker
def test_table_constraints_and_downgrade() -> None:
    with migrated_postgres("0082-bundle-config") as db:
        conn = psycopg2.connect(db.dsn)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO tenants (slug) VALUES ('t-0082') RETURNING id")
                tenant = cur.fetchone()[0]
                cur.execute("INSERT INTO communities (tenant_id, name) VALUES (%s, 'c') RETURNING id", (tenant,)) if _has_tenant_col(cur) else cur.execute("INSERT INTO communities (name) VALUES ('c') RETURNING id")
                community = cur.fetchone()[0]
                cur.execute("INSERT INTO app_catalog (app_id) VALUES ('waddles.a.b.c')")
                row = (tenant, community, "waddles.a.b.c")
                sql = (
                    "INSERT INTO bundle_community_config "
                    "(tenant_id, community_id, app_id, version, config, schema_sha256) "
                    "VALUES (%s, %s, %s, %s, '{\"k\": 1}', %s)"
                )
                cur.execute(sql, (*row, 1, "a" * 64))
                with pytest.raises(psycopg2.errors.UniqueViolation):
                    cur.execute(sql, (*row, 1, "a" * 64))
                with pytest.raises(psycopg2.errors.CheckViolation):
                    cur.execute(sql, (*row, 0, "a" * 64))
                with pytest.raises(psycopg2.errors.ForeignKeyViolation):
                    cur.execute(sql, (tenant, community, "waddles.no.such.app", 1, "a" * 64))
            alembic_cli("downgrade", _PREVIOUS, dsn=db.dsn)
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('bundle_community_config')")
                assert cur.fetchone()[0] is None
            alembic_cli("upgrade", "head", dsn=db.dsn)
        finally:
            conn.close()


def _has_tenant_col(cur: psycopg2.extensions.cursor) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name='communities' AND column_name='tenant_id'"
    )
    return cur.fetchone() is not None
