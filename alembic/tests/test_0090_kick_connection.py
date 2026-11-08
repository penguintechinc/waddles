"""Tests for 0090_kick_connection: chain, CHECK extension, one-Kick-per-community trigger, guards.

Static checks pin the revision chain and single head; the real-Postgres tests (docker, skipped where
the CLI is absent) prove ``kick_channel`` is accepted, the trigger refuses a second live Kick grant
per community (revoked ones do not count), ``upgrade`` refuses pre-existing duplicates loudly, and
``downgrade`` refuses while a Kick row exists, then removes the trigger.
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
_MIGRATION_PATH = _ROOT / "alembic" / "versions" / "0090_kick_connection.py"
_PREVIOUS = "0089_gameserver_conn_resource"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0090", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_chain_and_id_length() -> None:
    module = _load()
    assert module.down_revision == _PREVIOUS
    assert len(module.revision) <= 32


def test_history_has_exactly_one_head_and_0090_is_in_it() -> None:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    assert len(script.get_heads()) == 1
    assert "0090_kick_connection" in {r.revision for r in script.walk_revisions()}


def _one(cur: psycopg2.extensions.cursor, sql: str, params: tuple[object, ...] = ()) -> int:
    cur.execute(sql, params)
    row = cur.fetchone()
    assert row is not None
    return int(row[0])


def _kick(
    cur: psycopg2.extensions.cursor, tenant: int, community: int, rid: str, status: str
) -> None:
    conn_id = _one(
        cur,
        "INSERT INTO platform_connections (tenant_id, platform, resource_type, resource_id, "
        "access_token) VALUES (%s, 'kick', %s, %s, 'sealed') RETURNING id",
        (tenant, "kick_channel", rid),
    )
    cur.execute(
        "INSERT INTO community_connection_access (community_id, connection_id, status) "
        "VALUES (%s, %s, %s)",
        (community, conn_id, status),
    )


@requires_docker
def test_trigger_check_and_guarded_downgrade() -> None:
    with migrated_postgres("0090-kick") as db:
        conn = psycopg2.connect(db.dsn)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute("ALTER TABLE communities ADD COLUMN IF NOT EXISTS tenant_id INTEGER")
                tenant = _one(cur, "INSERT INTO tenants (slug) VALUES ('t-0090') RETURNING id")
                insert = "INSERT INTO communities (name, tenant_id) VALUES (%s, %s) RETURNING id"
                c1 = _one(cur, insert, ("c1-0090", tenant))
                c2 = _one(cur, insert, ("c2-0090", tenant))
                _kick(cur, tenant, c1, "1001", "approved")
                with pytest.raises(psycopg2.errors.UniqueViolation) as caught:
                    _kick(cur, tenant, c1, "1002", "pending")
                assert caught.value.diag.constraint_name == "uq_one_kick_connection_per_community"
                _kick(cur, tenant, c1, "1003", "revoked")  # revoked never counts
                _kick(cur, tenant, c2, "1004", "approved")  # another community is free
            with pytest.raises(RuntimeError, match="0090 downgrade refused"):
                alembic_cli("downgrade", _PREVIOUS, dsn=db.dsn)
            with conn.cursor() as cur:
                cur.execute("DELETE FROM platform_connections WHERE resource_type='kick_channel'")
            alembic_cli("downgrade", _PREVIOUS, dsn=db.dsn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM pg_trigger WHERE tgname = "
                    "'trg_one_kick_connection_per_community'"
                )
                assert cur.fetchone() == (0,)
                with pytest.raises(psycopg2.errors.CheckViolation):
                    _kick(cur, tenant, c1, "1005", "approved")
            # Pre-existing duplicates: the upgrade refuses loudly instead of keeping ambiguity.
            with conn.cursor() as cur:
                cur.execute(
                    "ALTER TABLE platform_connections DROP CONSTRAINT "
                    "platform_connections_resource_type_check"
                )
                _kick(cur, tenant, c1, "2001", "approved")
                _kick(cur, tenant, c1, "2002", "approved")
            with pytest.raises(RuntimeError, match="0090 upgrade refused"):
                alembic_cli("upgrade", "head", dsn=db.dsn)
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM community_connection_access WHERE community_id=%s",
                    (c1,),
                )
                cur.execute("DELETE FROM platform_connections WHERE platform='kick'")
                cur.execute(
                    "ALTER TABLE platform_connections ADD CONSTRAINT "
                    "platform_connections_resource_type_check CHECK (resource_type IN "
                    "('discord_guild','twitch_channel','external_http','aws_lambda','gcp_function',"
                    "'youtube_channel','wix_site','spectrum_community'))"
                )
            alembic_cli("upgrade", "head", dsn=db.dsn)
            with conn.cursor() as cur:
                _kick(cur, tenant, c1, "3001", "approved")
        finally:
            conn.close()
