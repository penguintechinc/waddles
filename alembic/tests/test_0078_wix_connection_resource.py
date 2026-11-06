"""Tests for 0078_wix_connection_resource: chain, CHECK extension, guarded downgrade.

Static checks pin the revision chain and single head; the real-Postgres tests (docker, skipped
where the CLI is absent -- same convention as 0064) prove ``wix_site`` is accepted after
``upgrade``, an unknown type is still refused, and ``downgrade`` refuses while a Wix row exists.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import psycopg2
import psycopg2.errors
import psycopg2.extensions
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pg_docker import DOCKER_AVAILABLE, alembic_cli, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION_PATH = _ROOT / "alembic" / "versions" / "0078_wix_connection_resource.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0078", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _script() -> ScriptDirectory:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    return ScriptDirectory.from_config(cfg)


# The parent is read from the migration itself, so re-chaining after a sibling 0078 merges
# (single head) never needs a test edit.
_PREVIOUS = _load().down_revision


def test_chain_and_id_length() -> None:
    module = _load()
    assert module.down_revision == _PREVIOUS
    assert module.down_revision in {r.revision for r in _script().walk_revisions()}
    assert len(module.revision) <= 32


def test_history_has_exactly_one_head_and_0078_is_in_it() -> None:
    script = _script()
    assert len(script.get_heads()) == 1
    assert "0078_wix_connection_resource" in {
        r.revision for r in script.walk_revisions()
    }


def _tenant(cur: psycopg2.extensions.cursor, slug: str) -> int:
    cur.execute("INSERT INTO tenants (slug) VALUES (%s) RETURNING id", (slug,))
    row = cur.fetchone()
    assert row is not None
    return int(row[0])


def _insert(cur: psycopg2.extensions.cursor, tenant: int, rtype: str, rid: str) -> None:
    cur.execute(
        "INSERT INTO platform_connections (tenant_id, platform, resource_type, resource_id, "
        "access_token) VALUES (%s, 'wix', %s, %s, 'sealed')",
        (tenant, rtype, rid),
    )


@requires_docker
def test_wix_site_accepted_unknown_rejected_and_downgrade_guarded() -> None:
    with migrated_postgres("0078-wix") as db:
        conn = psycopg2.connect(db.dsn)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                tenant = _tenant(cur, "t-0078")
                _insert(cur, tenant, "wix_site", "WIX1")
                with pytest.raises(psycopg2.errors.CheckViolation):
                    _insert(cur, tenant, "carrier_pigeon", "x")
            with pytest.raises(RuntimeError, match="0078 downgrade refused"):
                alembic_cli("downgrade", _PREVIOUS, dsn=db.dsn)
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM platform_connections WHERE resource_type='wix_site'"
                )
            alembic_cli("downgrade", _PREVIOUS, dsn=db.dsn)
            with conn.cursor() as cur, pytest.raises(psycopg2.errors.CheckViolation):
                _insert(cur, tenant, "wix_site", "WIX2")
            alembic_cli("upgrade", "head", dsn=db.dsn)
            with conn.cursor() as cur:
                _insert(cur, tenant, "wix_site", "WIX3")
        finally:
            conn.close()
