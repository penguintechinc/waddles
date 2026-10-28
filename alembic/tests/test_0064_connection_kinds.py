"""Tests for 0064_connection_kinds (connection_kind + sealed_secret + extended resource_type).

Static checks pin the revision chain and that ``public_id`` is NOT re-added (0063 owns it). The
real-Postgres tests prove the columns/CHECKs, that existing rows are backfilled to kind 1
(``migration_backfills_kind_1_for_existing_rows``), that the DB itself rejects an out-of-range kind
or a mis-shaped row, and that ``downgrade`` refuses while a static-kind row exists, then round-trips.
"""

from __future__ import annotations

import importlib.util
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

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "versions" / "0064_connection_kinds.py"
)
_PREVIOUS_REVISION = "0063_seal_aad_version"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "migration_0064_connection_kinds", _MIGRATION_PATH
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
        assert module.revision == "0064_connection_kinds"
        assert module.down_revision == _PREVIOUS_REVISION
        assert len(module.revision) <= 32

    def test_upgrade_does_not_re_add_public_id(self) -> None:
        sql = _sql("upgrade")
        assert "public_id" not in sql.replace("public_id is", "").replace(
            "conn:public_id", ""
        )
        assert (
            "ADD COLUMN" in sql and "sealed_secret" in sql and "connection_kind" in sql
        )
        for provider in ("external_http", "aws_lambda", "gcp_function"):
            assert provider in sql

    def test_downgrade_guards_before_it_drops(self) -> None:
        down = _sql("downgrade")
        assert down.index("RAISE EXCEPTION") < down.index("DROP COLUMN")
        assert "'discord_guild', 'twitch_channel'))" in down


def _connect(db: PgTestDatabase) -> psycopg2.extensions.connection:
    conn = psycopg2.connect(db.dsn)
    conn.autocommit = True
    return conn


def _tenant(cur: psycopg2.extensions.cursor, slug: str) -> int:
    cur.execute("INSERT INTO tenants (slug) VALUES (%s) RETURNING id", (slug,))
    return int(cur.fetchone()[0])


def _oauth_row(cur: psycopg2.extensions.cursor, tenant: int, resource_id: str) -> int:
    """A row written exactly as a pre-0064 caller would (no ``connection_kind`` named)."""
    cur.execute(
        "INSERT INTO platform_connections (tenant_id, platform, resource_type, resource_id, "
        "access_token) VALUES (%s, 'twitch', 'twitch_channel', %s, 'ciphertext') RETURNING id",
        (tenant, resource_id),
    )
    return int(cur.fetchone()[0])


def _static_row(
    cur: psycopg2.extensions.cursor, tenant: int, resource_id: str, kind: int = 2
) -> int:
    cur.execute(
        "INSERT INTO platform_connections (tenant_id, platform, resource_type, resource_id, "
        "connection_kind, sealed_secret) VALUES (%s, 'external_http', 'external_http', %s, %s, 'sealed') "
        "RETURNING id",
        (tenant, resource_id, kind),
    )
    return int(cur.fetchone()[0])


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """One real Postgres container migrated to head (includes 0064)."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0064-connection-kinds") as db:
        yield db


@requires_docker
class TestColumnsAndChecks:
    def test_columns_have_the_documented_shape(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                    "WHERE table_name='platform_connections' AND column_name IN "
                    "('connection_kind','sealed_secret','access_token','public_id')"
                )
                cols = {c: (d, n) for c, d, n in cur.fetchall()}
        finally:
            conn.close()
        assert cols["connection_kind"] == ("smallint", "NO")
        assert cols["sealed_secret"] == ("text", "YES")
        assert cols["access_token"] == ("text", "YES")
        assert cols["public_id"] == ("uuid", "NO")

    def test_pre_0064_style_insert_is_kind_1(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                row = _oauth_row(cur, _tenant(cur, "t-0064-a"), "r1")
                cur.execute(
                    "SELECT connection_kind FROM platform_connections WHERE id=%s",
                    (row,),
                )
                assert cur.fetchone() == (1,)
        finally:
            conn.close()

    def test_static_kinds_and_resource_types_are_accepted(
        self, pg_db: PgTestDatabase
    ) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                tenant = _tenant(cur, "t-0064-b")
                for kind, rtype in (
                    (2, "external_http"),
                    (3, "aws_lambda"),
                    (4, "gcp_function"),
                ):
                    cur.execute(
                        "INSERT INTO platform_connections (tenant_id, platform, resource_type, "
                        "resource_id, connection_kind, sealed_secret) VALUES (%s,%s,%s,%s,%s,'s')",
                        (tenant, rtype, rtype, f"id-{kind}", kind),
                    )
        finally:
            conn.close()

    @pytest.mark.parametrize("kind", [0, 5, -1])
    def test_out_of_range_kind_is_rejected_by_the_database(
        self, pg_db: PgTestDatabase, kind: int
    ) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                tenant = _tenant(cur, f"t-0064-k{kind}".replace("-1", "n1"))
                with pytest.raises(psycopg2.errors.CheckViolation):
                    _static_row(cur, tenant, "bad", kind=kind)
        finally:
            conn.close()

    def test_secret_shape_check_rejects_mismatched_rows(
        self, pg_db: PgTestDatabase
    ) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                tenant = _tenant(cur, "t-0064-shape")
                with pytest.raises(
                    psycopg2.errors.CheckViolation
                ):  # static row with a token
                    cur.execute(
                        "INSERT INTO platform_connections (tenant_id, platform, resource_type, "
                        "resource_id, connection_kind, access_token, sealed_secret) "
                        "VALUES (%s,'external_http','external_http','x',2,'tok','s')",
                        (tenant,),
                    )
                with pytest.raises(
                    psycopg2.errors.CheckViolation
                ):  # oauth row with no token
                    cur.execute(
                        "INSERT INTO platform_connections (tenant_id, platform, resource_type, "
                        "resource_id, connection_kind) VALUES (%s,'twitch','twitch_channel','y',1)",
                        (tenant,),
                    )
        finally:
            conn.close()

    def test_unknown_resource_type_is_still_rejected(
        self, pg_db: PgTestDatabase
    ) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                tenant = _tenant(cur, "t-0064-rt")
                with pytest.raises(psycopg2.errors.CheckViolation):
                    cur.execute(
                        "INSERT INTO platform_connections (tenant_id, platform, resource_type, "
                        "resource_id, access_token) VALUES (%s,'x','carrier_pigeon','z','t')",
                        (tenant,),
                    )
        finally:
            conn.close()


@requires_docker
class TestRoundTrip:
    def test_migration_backfills_kind_1_for_existing_rows(self) -> None:
        with migrated_postgres("0064-backfill") as db:
            alembic_cli("downgrade", _PREVIOUS_REVISION, dsn=db.dsn)
            conn = _connect(db)
            try:
                with conn.cursor() as cur:
                    tenant = _tenant(cur, "t-bf")
                    cur.execute(
                        "INSERT INTO platform_connections (tenant_id, platform, resource_type, "
                        "resource_id, access_token) VALUES "
                        "(%s,'twitch','twitch_channel','a','c'),(%s,'discord','discord_guild','b','c')",
                        (tenant, tenant),
                    )
                alembic_cli("upgrade", "head", dsn=db.dsn)
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT connection_kind, sealed_secret FROM platform_connections"
                    )
                    assert cur.fetchall() == [(1, None), (1, None)]
            finally:
                conn.close()

    def test_downgrade_refuses_while_a_static_row_exists_then_round_trips(self) -> None:
        with migrated_postgres("0064-roundtrip") as db:
            conn = _connect(db)
            try:
                with conn.cursor() as cur:
                    tenant = _tenant(cur, "t-rt")
                    _oauth_row(cur, tenant, "keep")
                    static_id = _static_row(cur, tenant, "static")
                with pytest.raises(RuntimeError, match="0064 downgrade refused"):
                    alembic_cli("downgrade", _PREVIOUS_REVISION, dsn=db.dsn)
                with conn.cursor() as cur:  # the refusal left everything in place
                    cur.execute("SELECT count(*) FROM platform_connections")
                    assert cur.fetchone() == (2,)
                    cur.execute(
                        "DELETE FROM platform_connections WHERE id=%s", (static_id,)
                    )

                alembic_cli("downgrade", _PREVIOUS_REVISION, dsn=db.dsn)
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT column_name FROM information_schema.columns WHERE "
                        "table_name='platform_connections' AND column_name IN "
                        "('connection_kind','sealed_secret')"
                    )
                    assert cur.fetchall() == [], "downgrade left a column behind"
                    cur.execute("SELECT count(*) FROM platform_connections")
                    assert cur.fetchone() == (1,), (
                        "downgrade must never delete the OAuth row"
                    )
                alembic_cli("upgrade", "head", dsn=db.dsn)
                with conn.cursor() as cur:
                    cur.execute("SELECT connection_kind FROM platform_connections")
                    assert cur.fetchall() == [(1,)]
            finally:
                conn.close()
