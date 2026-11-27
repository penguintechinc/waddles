"""Tests for 0148_member_ban_sources: fail-closed MANUAL backfill, CHECKs, round trip."""

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
_PATH = _ROOT / "alembic" / "versions" / "0148_member_ban_sources.py"
_REV = "0148_member_ban_sources"
_PREV = "0147_temp_password_user_bind"


def _load() -> ModuleType:
    """Import the migration module from its file path."""
    spec = importlib.util.spec_from_file_location("migration_0148", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_chain_and_id_length() -> None:
    module = _load()
    assert module.revision == _REV
    assert len(module.revision) <= 32
    assert module.down_revision == _PREV


def test_single_head_and_0148_in_history() -> None:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    assert len(script.get_heads()) == 1
    assert _REV in {r.revision for r in script.walk_revisions()}


def test_bits_match_the_service_enum() -> None:
    """The migration's MANUAL / all-bits constants mirror `membership_state.BanSource`."""
    import sys

    sys.path.insert(0, str(_ROOT / "hub_api"))
    from services.membership_state import ALL_BAN_SOURCES, BanSource

    module = _load()
    assert module._MANUAL == int(BanSource.MANUAL)
    assert module._ALL == ALL_BAN_SOURCES


@requires_docker
def test_backfills_banned_as_manual_checks_and_round_trip() -> None:
    with migrated_postgres("0148-ban-sources") as db:
        alembic_cli("downgrade", _PREV, dsn=db.dsn)
        conn = psycopg2.connect(db.dsn)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO communities (name, tenant_id) VALUES ('c1', 1) RETURNING id"
                )
                cid = cur.fetchone()[0]  # type: ignore[index]
                for puid, state in (("bad", 2), ("good", 1)):
                    cur.execute(
                        "INSERT INTO community_members "
                        "(community_id, platform, platform_user_id, membership_state) "
                        "VALUES (%s, 'discord', %s, %s)",
                        (cid, puid, state),
                    )

            alembic_cli("upgrade", _REV, dsn=db.dsn)

            with conn.cursor() as cur:
                cur.execute(
                    "SELECT platform_user_id, ban_sources FROM community_members "
                    "WHERE community_id = %s",
                    (cid,),
                )
                assert dict(cur.fetchall()) == {"bad": 1, "good": 0}
                with pytest.raises(psycopg2.errors.CheckViolation):
                    cur.execute("UPDATE community_members SET ban_sources = 16")
                with pytest.raises(psycopg2.errors.CheckViolation):
                    cur.execute(
                        "UPDATE community_members SET ban_sources = 2 "
                        "WHERE platform_user_id = 'good'"
                    )

            # idempotent re-run, then a clean downgrade drops the column
            alembic_cli("downgrade", _PREV, dsn=db.dsn)
            alembic_cli("upgrade", _REV, dsn=db.dsn)
            alembic_cli("upgrade", _REV, dsn=db.dsn)
            alembic_cli("downgrade", _PREV, dsn=db.dsn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = 'community_members' AND column_name = 'ban_sources'"
                )
                assert cur.fetchone()[0] == 0  # type: ignore[index]
                cur.execute(
                    "SELECT membership_state FROM community_members "
                    "WHERE platform_user_id = 'bad'"
                )
                assert cur.fetchone()[0] == 2  # type: ignore[index]
        finally:
            conn.close()
