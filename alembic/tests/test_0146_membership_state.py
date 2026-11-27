"""Tests for 0146_membership_state: state backfill + the per-tenant community-name unique index."""

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
_PATH = _ROOT / "alembic" / "versions" / "0146_membership_state.py"
_REV = "0146_membership_state"
_PREV = "0145_community_role_backfill"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0146", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_chain_and_id_length() -> None:
    module = _load()
    assert module.revision == _REV
    assert len(module.revision) <= 32
    assert module.down_revision == _PREV


def test_single_head_and_0146_in_history() -> None:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    assert len(script.get_heads()) == 1
    assert _REV in {r.revision for r in script.walk_revisions()}


def _scalar(cur: psycopg2.extensions.cursor, sql: str) -> object:
    cur.execute(sql)
    return cur.fetchone()[0]  # type: ignore[index]


@requires_docker
def test_backfills_state_and_scopes_name_uniqueness_to_tenant() -> None:
    with migrated_postgres("0146-membership-state") as db:
        alembic_cli("downgrade", _PREV, dsn=db.dsn)
        conn = psycopg2.connect(db.dsn)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO communities (name, tenant_id) VALUES ('c1', 1) RETURNING id"
                )
                cid = cur.fetchone()[0]  # type: ignore[index]
                for puid, role in (("bad", "Banned"), ("good", "member")):
                    cur.execute(
                        "INSERT INTO community_members "
                        "(community_id, platform, platform_user_id, role) "
                        "VALUES (%s, 'discord', %s, %s)",
                        (cid, puid, role),
                    )
                # pre-upgrade: the GLOBAL unique still blocks a same-name row in another tenant
                with pytest.raises(psycopg2.errors.UniqueViolation):
                    cur.execute(
                        "INSERT INTO communities (name, tenant_id) VALUES ('c1', 2)"
                    )

            alembic_cli("upgrade", _REV, dsn=db.dsn)

            with conn.cursor() as cur:
                cur.execute(
                    "SELECT platform_user_id, membership_state FROM community_members "
                    "WHERE community_id = %s",
                    (cid,),
                )
                assert dict(cur.fetchall()) == {"bad": 2, "good": 1}
                # composite replaces the global constraint: other tenant OK, same tenant blocked
                cur.execute(
                    "INSERT INTO communities (name, tenant_id) VALUES ('c1', 2)"
                )
                with pytest.raises(psycopg2.errors.UniqueViolation):
                    cur.execute(
                        "INSERT INTO communities (name, tenant_id) VALUES ('c1', 1)"
                    )
                assert (
                    _scalar(
                        cur,
                        "SELECT count(*) FROM pg_constraint WHERE conrelid = 'communities'::regclass "
                        "AND conname = 'communities_name_key'",
                    )
                    == 0
                )
                cur.execute(
                    "DELETE FROM communities WHERE name = 'c1' AND tenant_id = 2"
                )

            # idempotent re-run, then a clean round trip restores the global constraint
            alembic_cli("downgrade", _PREV, dsn=db.dsn)
            alembic_cli("upgrade", _REV, dsn=db.dsn)
            alembic_cli("downgrade", _PREV, dsn=db.dsn)
            with conn.cursor() as cur:
                assert (
                    _scalar(
                        cur,
                        "SELECT count(*) FROM pg_constraint WHERE conrelid = 'communities'::regclass "
                        "AND conname = 'communities_name_key'",
                    )
                    == 1
                )
                assert (
                    _scalar(
                        cur,
                        "SELECT to_regclass('public.uq_communities_tenant_name') IS NULL",
                    )
                    is True
                )
        finally:
            conn.close()
