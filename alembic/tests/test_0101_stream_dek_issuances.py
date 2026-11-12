"""Real-Postgres tests for 0101_stream_dek_issuances (GetStreamDek audit trail).

Head replay creates the table with its constraints; downgrade to the previous revision drops it
and leaves the 0054 key store intact; upgrade re-applies cleanly. A SKIP means docker is absent
-- treat as FAILURE.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg2
import pytest
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, alembic_cli, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

REVISION = "0101_stream_dek_issuances"


@pytest.fixture(scope="module")
def db() -> Iterator[PgTestDatabase]:
    """One real Postgres container migrated to head (includes 0101)."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0101-stream-dek") as database:
        yield database


def _scalar(db: PgTestDatabase, query: str) -> object:
    conn = psycopg2.connect(db.dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(query)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


@requires_docker
def test_table_columns_and_comment(db: PgTestDatabase) -> None:
    cols = _scalar(
        db,
        "SELECT string_agg(column_name, ',' ORDER BY ordinal_position) "
        "FROM information_schema.columns "
        "WHERE table_schema = 'keystore' AND table_name = 'stream_dek_issuances'",
    )
    assert cols == "id,tenant_id,purpose,dek_version,caller_service,outcome,created_at"
    assert "No key material" in str(
        _scalar(db, "SELECT obj_description('keystore.stream_dek_issuances'::regclass)")
    )


@requires_docker
def test_downgrade_drops_only_the_audit_table_then_upgrade_restores(db: PgTestDatabase) -> None:
    alembic_cli("downgrade", _down_revision(), dsn=db.dsn)
    assert _scalar(db, "SELECT to_regclass('keystore.stream_dek_issuances')") is None
    assert _scalar(db, "SELECT to_regclass('keystore.tenant_encryption_keys')") is not None
    alembic_cli("upgrade", "head", dsn=db.dsn)
    assert _scalar(db, "SELECT to_regclass('keystore.stream_dek_issuances')") is not None


def _down_revision() -> str:
    """Read `down_revision` from the migration file itself (single source of truth)."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "versions" / f"{REVISION}.py"
    spec = importlib.util.spec_from_file_location("m0101", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module.down_revision)
