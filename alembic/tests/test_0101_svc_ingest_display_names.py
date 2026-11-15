"""Real-Postgres tests for 0101_svc_ingest_display_names (egress detokenizer read role).

Column grants can only be proven by a live session as the role: it must be able to run the
exact resolver queries (`core/svc_ingest/src/display_names.rs`) and must NOT read any other
column (email, password hash). Downgrade must revoke exactly what upgrade granted.
"""

from __future__ import annotations

import importlib.util
import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import psycopg2
import psycopg2.errors
import pytest
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, _free_port, _wait_ready

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "versions"
    / "0101_svc_ingest_display_name_reader.py"
)
_ROLE = "waddles_svc_ingest"

_STATE_SQL = f"""
CREATE ROLE {_ROLE} LOGIN PASSWORD 'testpass123';
CREATE TABLE communities (id BIGINT PRIMARY KEY, tenant_id BIGINT NOT NULL, name TEXT);
CREATE TABLE hub_users (id BIGINT PRIMARY KEY, uuid UUID UNIQUE NOT NULL, display_name TEXT,
    email TEXT, password_hash TEXT);
CREATE TABLE community_members (id BIGSERIAL PRIMARY KEY, community_id BIGINT NOT NULL,
    user_uuid UUID, user_id TEXT, display_name TEXT, bio TEXT);
CREATE TABLE ephemeral_pseudonyms (tenant_id BIGINT NOT NULL, pseudonym UUID NOT NULL,
    handle TEXT, platform_user_id TEXT);
INSERT INTO communities VALUES (70, 7, 'a');
INSERT INTO hub_users VALUES (1, '0b9f2c1e-7d3a-4c58-9a1e-5f6d7e8a9b0c', 'Alice', 'a@x', 'h');
INSERT INTO community_members (community_id, user_uuid, display_name)
    VALUES (70, '0b9f2c1e-7d3a-4c58-9a1e-5f6d7e8a9b0c', 'nick');
"""

#: The resolver's hub-user query, verbatim shape (psycopg2 placeholders).
_HUB_SQL = """
SELECT hu.uuid::text, COALESCE(NULLIF(btrim(hu.display_name), ''),
  (SELECT NULLIF(btrim(cm.display_name), '') FROM community_members cm
     JOIN communities c ON c.id = cm.community_id
    WHERE c.tenant_id = %s AND (cm.user_uuid = hu.uuid OR cm.user_id = hu.id::text)
    ORDER BY cm.id DESC LIMIT 1))
  FROM hub_users hu WHERE hu.uuid = ANY(%s::uuid[]) AND EXISTS (
    SELECT 1 FROM community_members cm JOIN communities c ON c.id = cm.community_id
     WHERE c.tenant_id = %s AND (cm.user_uuid = hu.uuid OR cm.user_id = hu.id::text))
"""
_PSEUDO_SQL = (
    "SELECT ep.pseudonym::text, ep.handle FROM ephemeral_pseudonyms ep "
    "WHERE ep.tenant_id = %s AND ep.pseudonym = ANY(%s::uuid[])"
)
_ALICE = "0b9f2c1e-7d3a-4c58-9a1e-5f6d7e8a9b0c"


def _load() -> ModuleType:
    """Import the migration module by path."""
    spec = importlib.util.spec_from_file_location("migration_0100", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """Bare Postgres 17 bootstrapped to the minimal pre-0100 state."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    container = "waddles-migtest-0100-ingest-names"
    port = _free_port()
    db = PgTestDatabase(
        host="127.0.0.1",
        port=port,
        user="waddlebot",
        password="testpass123",
        dbname="waddlebot",
    )
    subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container,
            "-e",
            f"POSTGRES_USER={db.user}",
            "-e",
            f"POSTGRES_PASSWORD={db.password}",
            "-e",
            f"POSTGRES_DB={db.dbname}",
            "-p",
            f"{port}:5432",
            "postgres:17-bookworm",
        ],
        capture_output=True,
        check=True,
    )
    try:
        _wait_ready(container, db.user, db.dbname)
        subprocess.run(
            ["docker", "exec", "-i", container, "psql", "-U", db.user, "-d", db.dbname],
            input=_STATE_SQL,
            text=True,
            capture_output=True,
            check=True,
        )
        yield db
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container], capture_output=True, check=False
        )


def _run(db: PgTestDatabase, fn_name: str) -> None:
    """Run the migration's upgrade/downgrade with op.execute bound to a live cursor."""
    conn = psycopg2.connect(db.dsn)
    conn.autocommit = True
    with conn.cursor() as cur, patch("alembic.op.execute", side_effect=cur.execute):
        getattr(_load(), fn_name)()
    conn.close()


def _role_conn(db: PgTestDatabase) -> psycopg2.extensions.connection:
    """A session authenticated as the svc-ingest role."""
    conn = psycopg2.connect(
        f"postgresql://{_ROLE}:testpass123@{db.host}:{db.port}/{db.dbname}"
    )
    conn.autocommit = True
    return conn


@requires_docker
def test_metadata_is_single_head_off_0099() -> None:
    m = _load()
    assert m.revision == "0101_svc_ingest_display_names"
    assert m.down_revision == "0100_stream_dek_issuances"
    assert len(m.revision) <= 32


@requires_docker
def test_upgrade_allows_resolver_queries_and_nothing_else_then_downgrade_revokes(
    pg_db: PgTestDatabase,
) -> None:
    _run(pg_db, "upgrade")
    _run(pg_db, "upgrade")  # idempotent
    conn = _role_conn(pg_db)
    with conn.cursor() as cur:
        cur.execute(_HUB_SQL, [7, [_ALICE], 7])
        assert cur.fetchall() == [(_ALICE, "Alice")]
        cur.execute(_HUB_SQL, [8, [_ALICE], 8])
        assert cur.fetchall() == []  # other tenant: no membership, no name
        cur.execute(_PSEUDO_SQL, [7, [_ALICE]])
        assert cur.fetchall() == []
        for forbidden in (
            "SELECT email FROM hub_users",
            "SELECT password_hash FROM hub_users",
            "SELECT bio FROM community_members",
            "SELECT platform_user_id FROM ephemeral_pseudonyms",
            "UPDATE hub_users SET display_name = 'x'",
        ):
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute(forbidden)
    _run(pg_db, "downgrade")
    with conn.cursor() as cur, pytest.raises(psycopg2.errors.InsufficientPrivilege):
        cur.execute(_HUB_SQL, [7, [_ALICE], 7])
    conn.close()
