"""Tests for 0121_community_owner_role_fix: chain checks and the real-Postgres role repair."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import psycopg2
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pg_docker import DOCKER_AVAILABLE, alembic_cli, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_ROOT = Path(__file__).resolve().parents[2]
_PATH = _ROOT / "alembic" / "versions" / "0121_community_owner_role_fix.py"
_PREV = "0118_room_scopes_moderator"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0121", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_chain_and_id_length() -> None:
    module = _load()
    assert len(module.revision) <= 32
    assert module.down_revision == _PREV


def test_single_head_and_0121_in_history() -> None:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    assert len(script.get_heads()) == 1
    assert "0121_community_owner_role_fix" in {
        r.revision for r in script.walk_revisions()
    }


@requires_docker
def test_only_exact_owner_is_rewritten_and_role_id_repaired() -> None:
    with migrated_postgres("0121-owner-role") as db:
        alembic_cli("downgrade", _PREV, dsn=db.dsn)
        conn = psycopg2.connect(db.dsn)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO communities (name) VALUES ('c-0121') RETURNING id"
                )
                cid = cur.fetchone()[0]  # type: ignore[index]
                # the minimal CI bootstrap lacks 058's objects; add just what 0121 repairs
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS community_roles "
                    "(id serial PRIMARY KEY, community_id int, name text)"
                )
                cur.execute(
                    "ALTER TABLE community_members ADD COLUMN IF NOT EXISTS community_role_id int"
                )
                cur.execute(
                    "INSERT INTO community_roles (community_id, name) "
                    "VALUES (%s, 'community-owner') RETURNING id",
                    (cid,),
                )
                owner_role_id = cur.fetchone()[0]  # type: ignore[index]
                roles = [
                    "owner",
                    "community-owner",
                    "community-admin",
                    "moderator",
                    "member",
                ]
                for i, role in enumerate(roles):
                    cur.execute(
                        "INSERT INTO community_members (community_id, platform, platform_user_id, role) "
                        "VALUES (%s, 'discord', %s, %s)",
                        (cid, f"u{i}", role),
                    )
            alembic_cli("upgrade", "head", dsn=db.dsn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT platform_user_id, role, community_role_id "
                    "FROM community_members WHERE community_id = %s ORDER BY platform_user_id",
                    (cid,),
                )
                rows = cur.fetchall()
            assert [r[1] for r in rows] == [
                "community-owner",
                "community-owner",
                "community-admin",
                "moderator",
                "member",
            ], rows
            assert rows[0][2] == owner_role_id
            # untouched rows keep their (NULL) role id
            assert all(r[2] is None for r in rows[1:]), rows
            # idempotent: re-running changes nothing and never raises
            alembic_cli("downgrade", _PREV, dsn=db.dsn)
            alembic_cli("upgrade", "head", dsn=db.dsn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM community_members WHERE role = 'owner'"
                )
                assert cur.fetchone()[0] == 0  # type: ignore[index]
        finally:
            conn.close()


@requires_docker
def test_empty_table_upgrade_is_safe() -> None:
    with migrated_postgres("0121-empty") as db:
        alembic_cli("downgrade", _PREV, dsn=db.dsn)
        alembic_cli("upgrade", "head", dsn=db.dsn)
