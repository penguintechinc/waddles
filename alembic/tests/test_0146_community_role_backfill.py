"""Tests for 0146_community_role_backfill: chain checks and the real-Postgres seed + link repair."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import psycopg2
import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory
from pg_docker import DOCKER_AVAILABLE, alembic_cli, migrated_postgres
from role_seeding import install_community_role_seeding

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_ROOT = Path(__file__).resolve().parents[2]
_PATH = _ROOT / "alembic" / "versions" / "0146_community_role_backfill.py"
_REV = "0146_community_role_backfill"
_PREV = "0145_radio_station_default"


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


def _members(cur: psycopg2.extensions.cursor, cid: int) -> dict[str, int | None]:
    cur.execute(
        "SELECT platform_user_id, community_role_id FROM community_members "
        "WHERE community_id = %s",
        (cid,),
    )
    return {row[0]: row[1] for row in cur.fetchall()}


def _role_ids(cur: psycopg2.extensions.cursor, cid: int) -> dict[str, int]:
    cur.execute("SELECT name, id FROM community_roles WHERE community_id = %s", (cid,))
    return {row[0]: row[1] for row in cur.fetchall()}


@requires_docker
def test_seeds_missing_roles_and_backfills_links_idempotently() -> None:
    with migrated_postgres("0146-role-backfill") as db:
        alembic_cli("downgrade", _PREV, dsn=db.dsn)
        install_community_role_seeding(db.dsn)
        conn = psycopg2.connect(db.dsn)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                # created AFTER the seeding objects exist, the way the buggy create path did:
                # no role rows, members with only the legacy role string.
                cur.execute(
                    "INSERT INTO communities (name) VALUES ('broken') RETURNING id"
                )
                cid = cur.fetchone()[0]  # type: ignore[index]
                cur.execute(
                    "INSERT INTO communities (name) VALUES ('healthy') RETURNING id"
                )
                healthy = cur.fetchone()[0]  # type: ignore[index]
                cur.execute("SELECT seed_community_system_roles(%s)", (healthy,))
                cur.execute(
                    "SELECT id FROM community_roles WHERE community_id = %s AND name = 'moderator'",
                    (healthy,),
                )
                healthy_mod = cur.fetchone()[0]  # type: ignore[index]
                for puid, role in (
                    ("creator", "community-admin"),
                    ("joiner", "member"),
                    ("vip", "vip"),
                ):
                    cur.execute(
                        "INSERT INTO community_members "
                        "(community_id, platform, platform_user_id, role, claims_cache) "
                        "VALUES (%s, 'discord', %s, %s, '{\"scopes\": [\"stale\"]}')",
                        (cid, puid, role),
                    )
                # already linked (to a DIFFERENT role than its string): never rewritten
                cur.execute(
                    "INSERT INTO community_members "
                    "(community_id, platform, platform_user_id, role, community_role_id) "
                    "VALUES (%s, 'discord', 'mod', 'member', %s)",
                    (healthy, healthy_mod),
                )
                assert _role_ids(cur, cid) == {}

            alembic_cli("upgrade", _REV, dsn=db.dsn)

            with conn.cursor() as cur:
                roles = _role_ids(cur, cid)
                assert {"member", "moderator", "community-admin"} <= set(roles), roles
                assert _members(cur, cid) == {
                    "creator": roles["community-admin"],
                    "joiner": roles["member"],
                    "vip": None,  # names no role row: stays unlinked (fail closed)
                }
                cur.execute(
                    "SELECT count(*) FROM community_members "
                    "WHERE community_id = %s AND community_role_id IS NOT NULL "
                    "AND claims_cache IS NOT NULL",
                    (cid,),
                )
                assert cur.fetchone()[0] == 0  # type: ignore[index]
                assert _members(cur, healthy) == {"mod": healthy_mod}
                cur.execute(
                    "SELECT role_kind FROM community_roles WHERE id = %s",
                    (roles["community-admin"],),
                )
                assert cur.fetchone()[0] == 1  # type: ignore[index]  # admin kind via 0117 trigger
                before = (_role_ids(cur, cid), _members(cur, cid))

            # idempotent: a re-run adds no roles and changes no links
            alembic_cli("downgrade", _PREV, dsn=db.dsn)
            alembic_cli("upgrade", _REV, dsn=db.dsn)
            with conn.cursor() as cur:
                assert (_role_ids(cur, cid), _members(cur, cid)) == before
        finally:
            conn.close()


@requires_docker
def test_batched_backfill_links_owner_and_ignores_shadowing_custom_role() -> None:
    """Batch path (tiny batch size, many batches) + system-name rule, then a real upgrade."""
    with migrated_postgres("0146-role-batches") as db:
        alembic_cli("downgrade", _PREV, dsn=db.dsn)
        install_community_role_seeding(db.dsn)
        conn = psycopg2.connect(db.dsn)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO communities (name) VALUES ('legacy') RETURNING id"
                )
                cid = cur.fetchone()[0]  # type: ignore[index]
                cur.execute("SELECT seed_community_system_roles(%s)", (cid,))
                # A custom `member` predating seeding shadows the system name in this one.
                cur.execute(
                    "INSERT INTO communities (name) VALUES ('shadow') RETURNING id"
                )
                shadow_cid = cur.fetchone()[0]  # type: ignore[index]
                cur.execute(
                    "INSERT INTO community_roles (community_id, name, is_system, base_claims) "
                    "VALUES (%s, 'member', false, '{\"scopes\": [\"community.members:manage\"]}')",
                    (shadow_cid,),
                )
                cur.execute("SELECT seed_community_system_roles(%s)", (shadow_cid,))
                members = [("owner", "community-owner")] + [
                    (f"m{i}", "member") for i in range(6)
                ]
                for puid, role in members:
                    cur.execute(
                        "INSERT INTO community_members "
                        "(community_id, platform, platform_user_id, role) "
                        "VALUES (%s, 'discord', %s, %s)",
                        (cid, puid, role),
                    )
                cur.execute(
                    "INSERT INTO community_members (community_id, platform, platform_user_id, "
                    "role) VALUES (%s, 'discord', 'shadowed', 'member')",
                    (shadow_cid,),
                )

            engine = sa.create_engine(db.dsn)
            try:
                with engine.begin() as sa_conn:
                    linked, batches = _load().backfill_links(sa_conn, batch_size=2)
            finally:
                engine.dispose()
            # 8 member ids in ranges of 2 -> 4 batches; 7 linked, the shadowed one is not
            assert (linked, batches) == (7, 4)

            with conn.cursor() as cur:
                roles = _role_ids(cur, cid)
                got = _members(cur, cid)
                assert got["owner"] == roles["community-owner"]
                assert {got[f"m{i}"] for i in range(6)} == {roles["member"]}
                cur.execute(
                    "SELECT is_system FROM community_roles WHERE id = %s",
                    (roles["community-owner"],),
                )
                assert cur.fetchone()[0] is True  # type: ignore[index]
                assert _members(cur, shadow_cid) == {"shadowed": None}

            # the real upgrade (autocommit batches) is a no-op on top and still single-head
            alembic_cli("upgrade", _REV, dsn=db.dsn)
            with conn.cursor() as cur:
                assert _members(cur, shadow_cid) == {"shadowed": None}
                assert (
                    _members(cur, cid)["owner"]
                    == _role_ids(cur, cid)["community-owner"]
                )
        finally:
            conn.close()


def test_batch_size_is_bounded() -> None:
    assert _load().BATCH_SIZE == 5000


@requires_docker
def test_minimal_bootstrap_without_community_roles_is_a_noop() -> None:
    with migrated_postgres("0146-noop") as db:
        alembic_cli("downgrade", _PREV, dsn=db.dsn)
        alembic_cli("upgrade", _REV, dsn=db.dsn)
        conn = psycopg2.connect(db.dsn)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.community_roles') IS NULL")
                assert cur.fetchone()[0] is True  # type: ignore[index]
        finally:
            conn.close()
