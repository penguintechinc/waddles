"""Real-Postgres tests for 0099_discord_role_mirror (cross-guild role mirror + grant ledger).

Proves the tables land on a real Postgres (`pg_docker.migrated_postgres` replays to head), the
constraints the sync engine relies on hold (distinct guilds, unique mapping, unique grant, cascade
delete), and the downgrade drops both tables. A SKIP means docker is absent -- treat as FAILURE.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import psycopg2
import psycopg2.errors
import psycopg2.extensions
import pytest
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, alembic_cli, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

PREVIOUS_REVISION = "0098_system_tenant_global_admin"


@pytest.fixture(scope="module")
def db() -> Iterator[PgTestDatabase]:
    """One real Postgres container migrated to head (includes 0099)."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0099-role-mirror") as database:
        yield database


def _connect(db: PgTestDatabase) -> psycopg2.extensions.connection:
    conn = psycopg2.connect(db.dsn)
    conn.autocommit = True
    return conn


def _community(cur: psycopg2.extensions.cursor) -> int:
    cur.execute(
        "INSERT INTO tenants (slug) VALUES (%s) RETURNING id",
        (f"t-{uuid.uuid4().hex[:8]}",),
    )
    tenant_id = int(cur.fetchone()[0])
    cur.execute(
        "INSERT INTO communities (name, tenant_id) VALUES (%s, %s) RETURNING id",
        (f"c-{uuid.uuid4().hex[:8]}", tenant_id),
    )
    return int(cur.fetchone()[0])


def _mapping(
    cur: psycopg2.extensions.cursor, cid: int, sg: str = "1", tg: str = "2"
) -> int:
    cur.execute(
        "INSERT INTO discord_role_mirror_mappings "
        "(community_id, source_guild_id, source_role_id, target_guild_id, target_role_id) "
        "VALUES (%s, %s, '10', %s, '20') RETURNING id",
        (cid, sg, tg),
    )
    return int(cur.fetchone()[0])


@requires_docker
def test_tables_and_columns_exist(db: PgTestDatabase) -> None:
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name, count(*) FROM information_schema.columns "
                "WHERE table_name IN ('discord_role_mirror_mappings', 'discord_role_mirror_grants') "
                "GROUP BY table_name ORDER BY table_name"
            )
            assert cur.fetchall() == [
                ("discord_role_mirror_grants", 5),
                ("discord_role_mirror_mappings", 12),
            ]
    finally:
        conn.close()


@requires_docker
def test_same_guild_and_duplicate_rejected(db: PgTestDatabase) -> None:
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            cid = _community(cur)
            _mapping(cur, cid)
        with pytest.raises(psycopg2.errors.UniqueViolation), conn.cursor() as cur:
            _mapping(cur, cid)
        with pytest.raises(psycopg2.errors.CheckViolation), conn.cursor() as cur:
            _mapping(cur, cid, sg="5", tg="5")
    finally:
        conn.close()


@requires_docker
def test_grant_unique_and_cascade(db: PgTestDatabase) -> None:
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            cid = _community(cur)
            mid = _mapping(cur, cid)
            cur.execute(
                "INSERT INTO discord_role_mirror_grants (mapping_id, platform_user_id) "
                "VALUES (%s, '77')",
                (mid,),
            )
        with pytest.raises(psycopg2.errors.UniqueViolation), conn.cursor() as cur:
            cur.execute(
                "INSERT INTO discord_role_mirror_grants (mapping_id, platform_user_id) "
                "VALUES (%s, '77')",
                (mid,),
            )
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM discord_role_mirror_mappings WHERE id = %s", (mid,)
            )
            cur.execute(
                "SELECT count(*) FROM discord_role_mirror_grants WHERE mapping_id = %s",
                (mid,),
            )
            assert cur.fetchone() == (0,)
    finally:
        conn.close()


@requires_docker
def test_downgrade_drops_tables_and_reupgrade_restores() -> None:
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0099-roundtrip") as database:
        alembic_cli("downgrade", PREVIOUS_REVISION, dsn=database.dsn)
        conn = _connect(database)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('discord_role_mirror_mappings')")
                assert cur.fetchone() == (None,)
        finally:
            conn.close()
        alembic_cli("upgrade", "head", dsn=database.dsn)
        conn = _connect(database)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('discord_role_mirror_grants')")
                assert cur.fetchone()[0] is not None
        finally:
            conn.close()
