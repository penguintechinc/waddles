"""Tests for 0063_seal_aad_version (M4: row-bound AAD sealing -- `seal_version` + `public_id`).

Static checks (no database) pin the revision chain and that downgrade mirrors upgrade; the
real-Postgres tests (`pg_docker.migrated_postgres`, replays 0020 -> head) prove the columns,
defaults, uniqueness, that legacy rows land as `seal_version = 0` with a distinct `public_id`, and
that `downgrade` -> `upgrade` round-trips -- and refuses while an AAD-bound row exists.
"""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import psycopg2
import psycopg2.errors
import psycopg2.extensions
import pytest
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, alembic_cli, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_MIGRATION_PATH = Path(__file__).resolve().parent.parent / "versions" / "0063_seal_aad_version.py"
_PREVIOUS_REVISION = "0062_app_commands_registry"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "migration_0063_seal_aad_version", _MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sql(fn_name: str) -> str:
    with patch("alembic.op.execute") as mock_execute:
        getattr(_load_migration(), fn_name)()
    return "\n".join(call.args[0] for call in mock_execute.call_args_list)


class TestStatic:
    def test_the_revision_chain(self) -> None:
        module = _load_migration()
        assert module.revision == "0063_seal_aad_version"
        assert module.down_revision == _PREVIOUS_REVISION
        assert len(module.revision) <= 32

    def test_upgrade_is_additive_and_idempotent(self) -> None:
        sql = _sql("upgrade")
        assert sql.count("ADD COLUMN IF NOT EXISTS seal_version SMALLINT NOT NULL DEFAULT 0") == 2
        assert "ADD COLUMN IF NOT EXISTS public_id UUID NOT NULL DEFAULT gen_random_uuid()" in sql
        assert "DROP" not in sql and "UPDATE" not in sql

    def test_downgrade_drops_everything_upgrade_adds_and_guards_first(self) -> None:
        down = _sql("downgrade")
        assert down.index("RAISE EXCEPTION") < down.index("DROP COLUMN")
        for needle in (
            "platform_integrations DROP COLUMN IF EXISTS seal_version",
            "platform_connections DROP COLUMN IF EXISTS seal_version",
            "platform_connections DROP COLUMN IF EXISTS public_id",
            "uq_platform_connections_public_id",
        ):
            assert needle in down, needle


def _connect(db: PgTestDatabase) -> psycopg2.extensions.connection:
    conn = psycopg2.connect(db.dsn)
    conn.autocommit = True
    return conn


def _connection(
    cur: psycopg2.extensions.cursor, tenant_id: int, resource_id: str
) -> tuple[int, str]:
    """A row written as a pre-0063 caller would (no `seal_version`/`public_id` supplied)."""
    cur.execute(
        "INSERT INTO platform_connections (tenant_id, platform, resource_type, resource_id, "
        "access_token) VALUES (%s, 'twitch', 'twitch_channel', %s, 'ciphertext') "
        "RETURNING id, public_id::text",
        (tenant_id, resource_id),
    )
    row = cur.fetchone()
    return int(row[0]), str(row[1])


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """One real Postgres container migrated to head (includes 0063)."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0063-seal-aad-version") as db:
        yield db


@requires_docker
class TestColumnsAndDefaults:
    def test_columns_exist_with_the_documented_types(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT table_name, column_name, data_type, is_nullable, column_default "
                    "FROM information_schema.columns WHERE "
                    "(table_name IN ('platform_integrations','platform_connections') "
                    "AND column_name = 'seal_version') "
                    "OR (table_name = 'platform_connections' AND column_name = 'public_id')"
                )
                cols = {(t, c): (d, n, default) for t, c, d, n, default in cur.fetchall()}
        finally:
            conn.close()
        assert len(cols) == 3
        for table in ("platform_integrations", "platform_connections"):
            data_type, nullable, default = cols[(table, "seal_version")]
            assert (data_type, nullable, default) == ("smallint", "NO", "0")
        data_type, nullable, default = cols[("platform_connections", "public_id")]
        assert (data_type, nullable) == ("uuid", "NO") and "gen_random_uuid" in default

    def test_a_pre_0063_style_insert_is_legacy_with_a_distinct_public_id(
        self, pg_db: PgTestDatabase
    ) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO tenants (slug) VALUES ('t-0063') RETURNING id")
                tenant = int(cur.fetchone()[0])
                first_id, first_public = _connection(cur, tenant, "r1")
                _, second_public = _connection(cur, tenant, "r2")
                assert uuid.UUID(first_public) != uuid.UUID(second_public)
                cur.execute(
                    "SELECT seal_version FROM platform_connections WHERE id=%s", (first_id,)
                )
                assert cur.fetchone() == (0,)
        finally:
            conn.close()

    def test_public_id_is_unique(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO tenants (slug) VALUES ('t-0063-uq') RETURNING id")
                tenant = int(cur.fetchone()[0])
                _, public_id = _connection(cur, tenant, "r1")
                with pytest.raises(psycopg2.errors.UniqueViolation):
                    cur.execute(
                        "INSERT INTO platform_connections (tenant_id, platform, resource_type, "
                        "resource_id, access_token, public_id) "
                        "VALUES (%s, 'twitch', 'twitch_channel', 'r2', 'c', %s)",
                        (tenant, public_id),
                    )
        finally:
            conn.close()


@requires_docker
class TestRoundTrip:
    def test_downgrade_refuses_while_an_aad_bound_row_exists_then_round_trips(self) -> None:
        with migrated_postgres("0063-roundtrip") as db:
            conn = _connect(db)
            try:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO tenants (slug) VALUES ('t-rt') RETURNING id")
                    tenant = int(cur.fetchone()[0])
                    row_id, _ = _connection(cur, tenant, "r1")
                    cur.execute(
                        "UPDATE platform_connections SET seal_version = 1 WHERE id=%s", (row_id,)
                    )
                with pytest.raises(RuntimeError, match="0063 downgrade refused"):
                    alembic_cli("downgrade", _PREVIOUS_REVISION, dsn=db.dsn)
                with conn.cursor() as cur:  # the refusal left everything in place
                    cur.execute(
                        "SELECT seal_version FROM platform_connections WHERE id=%s", (row_id,)
                    )
                    assert cur.fetchone() == (1,)
                    cur.execute("UPDATE platform_connections SET seal_version = 0")

                alembic_cli("downgrade", _PREVIOUS_REVISION, dsn=db.dsn)
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT column_name FROM information_schema.columns WHERE "
                        "column_name IN ('seal_version','public_id') "
                        "AND table_name IN ('platform_connections','platform_integrations')"
                    )
                    assert cur.fetchall() == [], "downgrade left a column behind"
                    cur.execute("SELECT count(*) FROM platform_connections")
                    assert cur.fetchone() == (1,), "downgrade must never delete rows"
                alembic_cli("upgrade", "head", dsn=db.dsn)
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT seal_version, public_id IS NOT NULL FROM platform_connections"
                    )
                    assert cur.fetchall() == [(0, True)]
            finally:
                conn.close()
