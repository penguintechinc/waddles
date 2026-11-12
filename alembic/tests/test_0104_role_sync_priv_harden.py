"""Real-Postgres tests for 0104_role_sync_priv_harden (role-sync privilege hardening).

Proves: single head chained off 0099; the upgrade PURGES pre-existing privileged inbound bindings
and the CHECK then refuses new ones; the ledgers + `propagate_moderators` /
`ownership_verified_at` columns land; downgrade removes them cleanly. A SKIP means docker is
absent -- treat as FAILURE.
"""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg2
import psycopg2.errors
import psycopg2.extensions
import pytest
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, alembic_cli, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

REVISION = "0104_role_sync_priv_harden"
PREVIOUS_REVISION = "0099_discord_role_mirror"
_VERSIONS = Path(__file__).resolve().parent.parent / "versions"


def _load(name: str):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(
        f"mig_{name}", _VERSIONS / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_metadata_chain_and_id_length() -> None:
    mig = _load(REVISION)
    assert mig.revision == REVISION and len(REVISION) <= 32
    assert mig.down_revision == PREVIOUS_REVISION
    assert mig.INBOUND_COMMUNITY_ROLES == ("vip", "member")


def test_single_head() -> None:
    """No other migration revises 0099 or 0104 on this branch (heads == 1)."""
    downs: dict[str, list[str]] = {}
    for path in _VERSIONS.glob("*.py"):
        text = path.read_text()
        for line in text.splitlines():
            if line.startswith("down_revision = "):
                downs.setdefault(line.split("=", 1)[1].strip().strip("\"'"), []).append(
                    path.stem
                )
    assert downs.get(PREVIOUS_REVISION) == [REVISION]
    assert REVISION not in downs


@pytest.fixture(scope="module")
def db() -> Iterator[PgTestDatabase]:
    """Real Postgres migrated to head (includes 0104)."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0104-role-sync-harden") as database:
        yield database


def _connect(db: PgTestDatabase) -> psycopg2.extensions.connection:
    conn = psycopg2.connect(db.dsn)
    conn.autocommit = True
    return conn


def _pairing(cur: psycopg2.extensions.cursor) -> int:
    cur.execute(
        "INSERT INTO tenants (slug) VALUES (%s) RETURNING id",
        (f"t-{uuid.uuid4().hex[:8]}",),
    )
    tenant_id = int(cur.fetchone()[0])
    cur.execute(
        "INSERT INTO communities (name, tenant_id) VALUES (%s, %s) RETURNING id",
        (f"c-{uuid.uuid4().hex[:8]}", tenant_id),
    )
    cid = int(cur.fetchone()[0])
    cur.execute(
        "INSERT INTO guild_tenant_pairings (community_id, discord_guild_id, direction, "
        "role_name_prefix) VALUES (%s, %s, 'bidirectional', '[T]') RETURNING id",
        (cid, str(uuid.uuid4().int)[:18]),
    )
    return int(cur.fetchone()[0])


def _bind(cur: psycopg2.extensions.cursor, pid: int, role: str, rid: str) -> None:
    cur.execute(
        "INSERT INTO community_role_sync_bindings "
        "(pairing_id, sync_scope, community_role, discord_role_id) "
        "VALUES (%s, 'community_role', %s, %s)",
        (pid, role, rid),
    )


@requires_docker
def test_check_refuses_privileged_inbound_target(db: PgTestDatabase) -> None:
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            pid = _pairing(cur)
            _bind(cur, pid, "vip", "1")
            _bind(cur, pid, "member", "2")
        for role in ("community-admin", "moderator"):
            with pytest.raises(psycopg2.errors.CheckViolation), conn.cursor() as cur:
                _bind(cur, pid, role, "3")
        with conn.cursor() as cur:  # outbound scopes unaffected
            cur.execute(
                "INSERT INTO community_role_sync_bindings (pairing_id, sync_scope, discord_role_id) "
                "VALUES (%s, 'moderator', '9')",
                (pid,),
            )
            cur.execute(
                "SELECT propagate_moderators, ownership_verified_at FROM guild_tenant_pairings "
                "WHERE id = %s",
                (pid,),
            )
            assert cur.fetchone() == (False, None)
    finally:
        conn.close()


@requires_docker
def test_downgrade_purge_and_reupgrade(db: PgTestDatabase) -> None:
    """Privileged inbound bindings existing before 0104 are purged by its upgrade."""
    alembic_cli("downgrade", PREVIOUS_REVISION, dsn=db.dsn)
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_name IN "
                "('role_sync_community_grants', 'role_sync_discord_grants')"
            )
            assert cur.fetchone() == (0,)
            pid = _pairing(cur)
            _bind(cur, pid, "community-admin", "10")  # allowed pre-0104
            _bind(cur, pid, "moderator", "11")
            _bind(cur, pid, "vip", "12")
    finally:
        conn.close()
    alembic_cli("upgrade", "head", dsn=db.dsn)
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT community_role FROM community_role_sync_bindings WHERE pairing_id = %s",
                (pid,),
            )
            assert [r[0] for r in cur.fetchall()] == ["vip"]
            cur.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_name IN "
                "('role_sync_community_grants', 'role_sync_discord_grants')"
            )
            assert cur.fetchone() == (2,)
    finally:
        conn.close()


@requires_docker
def test_ledgers_cascade_and_refuse_privileged_grant(db: PgTestDatabase) -> None:
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            pid = _pairing(cur)
            cur.execute(
                "SELECT community_id FROM guild_tenant_pairings WHERE id = %s", (pid,)
            )
            cid = int(cur.fetchone()[0])
            cur.execute(
                "INSERT INTO hub_users (username) VALUES (%s) RETURNING id",
                (uuid.uuid4().hex,),
            )
            uid = int(cur.fetchone()[0])
            cur.execute(
                "INSERT INTO role_sync_community_grants (pairing_id, community_id, hub_user_id, "
                "granted_role, prior_role) VALUES (%s, %s, %s, 'vip', 'member')",
                (pid, cid, uid),
            )
            cur.execute(
                "INSERT INTO role_sync_discord_grants (pairing_id, discord_role_id, "
                "platform_user_id) VALUES (%s, '5', '77')",
                (pid,),
            )
        with pytest.raises(psycopg2.errors.CheckViolation), conn.cursor() as cur:
            cur.execute(
                "UPDATE role_sync_community_grants SET granted_role = 'community-admin' "
                "WHERE pairing_id = %s",
                (pid,),
            )
        with pytest.raises(psycopg2.errors.UniqueViolation), conn.cursor() as cur:
            cur.execute(
                "INSERT INTO role_sync_discord_grants (pairing_id, discord_role_id, "
                "platform_user_id) VALUES (%s, '5', '77')",
                (pid,),
            )
        with conn.cursor() as cur:
            cur.execute("DELETE FROM guild_tenant_pairings WHERE id = %s", (pid,))
            cur.execute(
                "SELECT (SELECT count(*) FROM role_sync_community_grants WHERE pairing_id = %s) + "
                "(SELECT count(*) FROM role_sync_discord_grants WHERE pairing_id = %s)",
                (pid, pid),
            )
            assert cur.fetchone() == (0,)
    finally:
        conn.close()
