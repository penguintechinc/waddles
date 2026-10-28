"""Tests for 0065_hub_users_is_bootstrap (explicit bootstrap-admin marker, free-tier admin cap).

Real Postgres: the column shape, the single-row partial unique index, and the backfill marking
exactly the lowest-id super admin present at upgrade time (never a later/second one).
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg2
import psycopg2.errors
import pytest
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, alembic_cli, migrated_postgres

_PREVIOUS = "0064_connection_kinds"


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """One real Postgres container migrated to head (includes 0065)."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0065-is-bootstrap") as db:
        yield db


def _connect(db: PgTestDatabase) -> psycopg2.extensions.connection:
    conn = psycopg2.connect(db.dsn)
    conn.autocommit = True
    return conn


def test_column_shape_and_default(pg_db: PgTestDatabase) -> None:
    conn = _connect(pg_db)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
                "WHERE table_name='hub_users' AND column_name='is_bootstrap'"
            )
            dtype, nullable, default = cur.fetchone()
    finally:
        conn.close()
    assert (dtype, nullable) == ("boolean", "NO")
    assert "false" in default


def test_only_one_bootstrap_row_can_exist(pg_db: PgTestDatabase) -> None:
    conn = _connect(pg_db)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO hub_users (username, is_bootstrap) VALUES ('b1', true)"
            )
            with pytest.raises(psycopg2.errors.UniqueViolation):
                cur.execute(
                    "INSERT INTO hub_users (username, is_bootstrap) VALUES ('b2', true)"
                )
            cur.execute("DELETE FROM hub_users WHERE username IN ('b1', 'b2')")
    finally:
        conn.close()


def test_backfill_marks_only_the_lowest_id_super_admin(pg_db: PgTestDatabase) -> None:
    alembic_cli("downgrade", _PREVIOUS, dsn=pg_db.dsn)
    conn = _connect(pg_db)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE hub_users ADD COLUMN IF NOT EXISTS is_super_admin BOOLEAN"
            )
            cur.execute(
                "INSERT INTO hub_users (username, is_super_admin) VALUES ('plain', false)"
            )
            cur.execute(
                "INSERT INTO hub_users (username, is_super_admin) VALUES ('seed', true)"
            )
            cur.execute(
                "INSERT INTO hub_users (username, is_super_admin) VALUES ('later', true)"
            )
        alembic_cli("upgrade", "head", dsn=pg_db.dsn)
        with conn.cursor() as cur:
            cur.execute("SELECT username FROM hub_users WHERE is_bootstrap ORDER BY id")
            assert cur.fetchall() == [("seed",)]
            cur.execute(
                "DELETE FROM hub_users WHERE username IN ('plain','seed','later')"
            )
    finally:
        conn.close()
