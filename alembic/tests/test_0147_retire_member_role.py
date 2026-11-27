"""Tests for 0147_retire_member_role: final link backfill, contract swap, drop + downgrade restore."""

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
_PATH = _ROOT / "alembic" / "versions" / "0147_retire_member_role.py"
_REV = "0147_retire_member_role"
_PREV = "0146_membership_state"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0147", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_chain_and_id_length() -> None:
    """Revision id fits the 32-char alembic_version column and chains off #1542's head."""
    module = _load()
    assert module.revision == _REV
    assert len(module.revision) <= 32
    assert module.down_revision == _PREV


def test_single_head_and_0147_in_history() -> None:
    """The chain keeps exactly one head."""
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    assert len(script.get_heads()) == 1
    assert _REV in {r.revision for r in script.walk_revisions()}


def _scalar(cur: psycopg2.extensions.cursor, sql: str, params: tuple[object, ...] = ()) -> object:
    cur.execute(sql, params)
    return cur.fetchone()[0]  # type: ignore[index]


def _column_count(cur: psycopg2.extensions.cursor, table: str, column: str) -> int:
    return int(
        _scalar(  # type: ignore[arg-type]
            cur,
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
            (table, column),
        )
    )


def _seed(cur: psycopg2.extensions.cursor) -> dict[str, int]:
    """One community, its system owner/moderator/member rows, a SHADOW custom `member`, members."""
    cur.execute("INSERT INTO communities (name, tenant_id) VALUES ('c0147', 1) RETURNING id")
    cid = cur.fetchone()[0]  # type: ignore[index]
    roles: dict[str, int] = {}
    for name, is_system in (
        ("community-owner", True),
        ("moderator", True),
        ("member", True),
        ("vip", False),
    ):
        cur.execute(
            "INSERT INTO community_roles (community_id, name, is_system) "
            "VALUES (%s, %s, %s) RETURNING id",
            (cid, name, is_system),
        )
        roles[name] = cur.fetchone()[0]  # type: ignore[index]
    cur.execute("INSERT INTO hub_users DEFAULT VALUES RETURNING id")
    owner_uid = cur.fetchone()[0]  # type: ignore[index]
    rows = (
        # platform_user_id, user_id, legacy role string, linked role
        ("owner", str(owner_uid), "community-owner", roles["community-owner"]),
        ("mod", None, "moderator", roles["moderator"]),
        ("late", None, "vip", None),  # written between 0145 and the drop: still unlinked
        ("ghost", None, "no-such-role", None),  # names nothing: stays unlinked (no scopes)
    )
    for puid, user_id, role, role_id in rows:
        cur.execute(
            "INSERT INTO community_members "
            "(community_id, platform, platform_user_id, user_id, role, community_role_id) "
            "VALUES (%s, 'discord', %s, %s, %s, %s)",
            (cid, puid, user_id, role, role_id),
        )
    return {"cid": cid, "owner_uid": owner_uid, **roles}


@requires_docker
def test_upgrade_drops_columns_and_downgrade_restores_from_links() -> None:
    """Upgrade links late rows, swaps the view to the link, drops columns; downgrade restores."""
    with migrated_postgres("0147-retire-member-role") as db:
        alembic_cli("downgrade", _PREV, dsn=db.dsn)
        conn = psycopg2.connect(db.dsn)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                ids = _seed(cur)
                cid = ids["cid"]

            alembic_cli("upgrade", _REV, dsn=db.dsn)

            with conn.cursor() as cur:
                assert _column_count(cur, "community_members", "role") == 0
                assert _column_count(cur, "communities", "owner_id") == 0
                assert _column_count(cur, "communities", "owner_name") == 0
                assert (
                    _scalar(cur, "SELECT to_regclass('public.idx_community_members_role')")
                    is None
                )
                # the final backfill linked the late row; the unresolvable one stays NULL
                cur.execute(
                    "SELECT platform_user_id, community_role_id FROM community_members "
                    "WHERE community_id = %s",
                    (cid,),
                )
                links = dict(cur.fetchall())
                assert links["late"] == ids["vip"]
                assert links["ghost"] is None
                # the data-plane view reads the LINKED role name
                cur.execute(
                    "SELECT platform_user_id, role FROM community_member_identities "
                    "WHERE community_id = %s",
                    (cid,),
                )
                assert dict(cur.fetchall()) == {
                    "owner": "community-owner",
                    "mod": "moderator",
                    "late": "vip",
                    "ghost": None,
                }
                # a relink is visible through the view at once (no string to keep in sync)
                cur.execute(
                    "UPDATE community_members SET community_role_id = %s "
                    "WHERE community_id = %s AND platform_user_id = 'mod'",
                    (ids["member"], cid),
                )
                assert (
                    _scalar(
                        cur,
                        "SELECT role FROM community_member_identities "
                        "WHERE community_id = %s AND platform_user_id = 'mod'",
                        (cid,),
                    )
                    == "member"
                )

            # idempotent re-run, then the downgrade repopulates from the links
            alembic_cli("downgrade", _PREV, dsn=db.dsn)
            alembic_cli("upgrade", _REV, dsn=db.dsn)
            alembic_cli("downgrade", _PREV, dsn=db.dsn)

            with conn.cursor() as cur:
                cur.execute(
                    "SELECT platform_user_id, role FROM community_members WHERE community_id = %s",
                    (cid,),
                )
                assert dict(cur.fetchall()) == {
                    "owner": "community-owner",
                    "mod": "member",
                    "late": "vip",
                    "ghost": None,
                }
                assert (
                    _scalar(cur, "SELECT owner_id FROM communities WHERE id = %s", (cid,))
                    == str(ids["owner_uid"])
                )
                assert (
                    _scalar(cur, "SELECT to_regclass('public.idx_community_members_role')")
                    is not None
                )
                # the restored view reads the string column again
                assert (
                    _scalar(
                        cur,
                        "SELECT role FROM community_member_identities "
                        "WHERE community_id = %s AND platform_user_id = 'owner'",
                        (cid,),
                    )
                    == "community-owner"
                )
            alembic_cli("upgrade", _REV, dsn=db.dsn)
        finally:
            conn.close()


@requires_docker
def test_platform_table_role_granted_link_not_string() -> None:
    """`waddles_platform_table` reads/writes the role LINK; the string column is gone."""
    with migrated_postgres("0147-platform-table-grants") as db:
        conn = psycopg2.connect(db.dsn)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                if not _scalar(
                    cur, "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)",
                    ("waddles_platform_table",),
                ):
                    pytest.fail("service role waddles_platform_table missing from the harness")
                cur.execute(
                    "SELECT column_name, privilege_type FROM information_schema.column_privileges "
                    "WHERE grantee = 'waddles_platform_table' AND table_name = 'community_members' "
                    "AND column_name IN ('community_role_id', 'claims_cache')"
                )
                granted = set(cur.fetchall())
                assert ("community_role_id", "SELECT") in granted
                assert ("community_role_id", "UPDATE") in granted
                assert ("claims_cache", "UPDATE") in granted
                assert _scalar(
                    cur,
                    "SELECT has_column_privilege('waddles_platform_table', 'community_roles', "
                    "'name', 'SELECT')",
                )
                assert not _scalar(
                    cur,
                    "SELECT has_table_privilege('waddles_platform_table', 'community_roles', "
                    "'UPDATE')",
                )
        finally:
            conn.close()
