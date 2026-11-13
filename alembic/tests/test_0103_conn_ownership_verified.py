"""Real-Postgres tests for 0103_conn_ownership_verified (bind-time ownership flag).

Proves on a real Postgres that the column defaults FALSE (legacy rows unverified), that one channel
may be bound by several tenants -- verified or not (N:M, no global unique constraint) -- and that
downgrade/upgrade round-trips. A SKIP means docker is
absent -- treat as FAILURE.
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

PREVIOUS_REVISION = "0101_svc_ingest_display_names"


@pytest.fixture(scope="module")
def db() -> Iterator[PgTestDatabase]:
    """One real Postgres container migrated to head (includes 0103)."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0103-ownership") as database:
        yield database


def _connect(db: PgTestDatabase) -> psycopg2.extensions.connection:
    conn = psycopg2.connect(db.dsn)
    conn.autocommit = True
    return conn


def _tenant(cur: psycopg2.extensions.cursor) -> int:
    cur.execute(
        "INSERT INTO tenants (slug) VALUES (%s) RETURNING id", (f"t-{uuid.uuid4().hex[:8]}",)
    )
    return int(cur.fetchone()[0])


def _connection(
    cur: psycopg2.extensions.cursor, tenant_id: int, resource_id: str, verified: bool | None
) -> int:
    cols = "tenant_id, platform, resource_type, resource_id, public_id, access_token"
    vals = "%s, 'twitch', 'twitch_channel', %s, %s, 'sealed-placeholder'"
    params: list[object] = [tenant_id, resource_id, str(uuid.uuid4())]
    if verified is not None:
        cols += ", ownership_verified"
        vals += ", %s"
        params.append(verified)
    cur.execute(f"INSERT INTO platform_connections ({cols}) VALUES ({vals}) RETURNING id", params)  # noqa: S608 - fixed fragments
    return int(cur.fetchone()[0])


@requires_docker
def test_column_defaults_false(db: PgTestDatabase) -> None:
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            row_id = _connection(cur, _tenant(cur), f"r-{uuid.uuid4().hex[:6]}", None)
            cur.execute(
                "SELECT ownership_verified FROM platform_connections WHERE id=%s", (row_id,)
            )
            assert cur.fetchone()[0] is False
    finally:
        conn.close()


@requires_docker
def test_one_channel_many_tenants_verified_or_not(db: PgTestDatabase) -> None:
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            rid = f"r-{uuid.uuid4().hex[:6]}"
            _connection(cur, _tenant(cur), rid, False)
            _connection(cur, _tenant(cur), rid, True)
            _connection(cur, _tenant(cur), rid, True)
            cur.execute("SELECT count(*) FROM platform_connections WHERE resource_id=%s", (rid,))
            assert cur.fetchone()[0] == 3
    finally:
        conn.close()


@requires_docker
def test_same_tenant_duplicate_still_refused(db: PgTestDatabase) -> None:
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            rid = f"r-{uuid.uuid4().hex[:6]}"
            tenant = _tenant(cur)
            _connection(cur, tenant, rid, True)
            with pytest.raises(psycopg2.errors.UniqueViolation):
                _connection(cur, tenant, rid, True)
    finally:
        conn.close()


@requires_docker
def test_downgrade_drops_column_and_reupgrade_restores() -> None:
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0103-ownership-roundtrip") as database:
        alembic_cli("downgrade", PREVIOUS_REVISION, dsn=database.dsn)
        conn = _connect(database)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name='platform_connections' AND column_name='ownership_verified'"
                )
                assert cur.fetchone()[0] == 0
        finally:
            conn.close()
        alembic_cli("upgrade", "head", dsn=database.dsn)
        conn = _connect(database)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name='platform_connections' AND column_name='ownership_verified'"
                )
                assert cur.fetchone()[0] == 1
        finally:
            conn.close()
