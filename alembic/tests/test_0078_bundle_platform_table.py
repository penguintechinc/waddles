"""Real-Postgres tests for 0078_bundle_platform_table: audit table + least-privilege role."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import closing

import psycopg2
import pytest
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, alembic_cli, empty_postgres

pytestmark = pytest.mark.skipif(not DOCKER_AVAILABLE, reason="docker CLI not available")


def _q(dsn: str, sql: str, params: Sequence[object] = ()) -> list[tuple[object, ...]]:
    with closing(psycopg2.connect(dsn)) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [tuple(r) for r in cur.fetchall()] if cur.description else []


@pytest.fixture(scope="module")
def fresh_db() -> Iterator[PgTestDatabase]:
    with empty_postgres("0078-fresh") as db:
        alembic_cli("upgrade", "head", dsn=db.dsn)
        yield db


def test_revision_id_within_limit() -> None:
    assert len("0078_bundle_platform_table") <= 32


def test_audit_table_and_inert_role_exist(fresh_db: PgTestDatabase) -> None:
    assert _q(
        fresh_db.dsn,
        "SELECT to_regclass('public.bundle_platform_table_audit') IS NOT NULL",
    )[0][0]
    # No DB_PLATFORM_TABLE_PASSWORD in the test env -> NOLOGIN (inert, fail-closed).
    row = _q(
        fresh_db.dsn,
        "SELECT rolcanlogin, rolsuper FROM pg_roles WHERE rolname = 'waddles_platform_table'",
    )
    assert row == [(False, False)]


def test_audit_op_is_write_only(fresh_db: PgTestDatabase) -> None:
    with pytest.raises(psycopg2.errors.CheckViolation):
        _q(
            fresh_db.dsn,
            "INSERT INTO bundle_platform_table_audit (tenant_id, community_id, app_id, "
            "app_version, user_uuid, table_name, column_name, op) VALUES "
            "(1, 1, 'a', 1, gen_random_uuid(), 't', 'c', 'delete')",
        )
