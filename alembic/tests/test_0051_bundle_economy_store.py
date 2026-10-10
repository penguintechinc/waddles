"""Real-Postgres tests for 0051_bundle_economy_store (issue #714).

Runs the actual Alembic chain to `head` in an ephemeral container (see
`pg_docker.py`) and asserts the schema, the non-negative balance backstop, the
append-only ledger grants and the `waddles_economy_runtime` role's privilege
boundary -- none of which a mocked `op.execute` could verify.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.errors
import pytest
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1] / "versions" / "0051_bundle_economy_store.py"
)
_ROLE = "waddles_economy_runtime"


def _load_migration():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("migration_0047", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMigrationMetadata:
    def test_chains_off_0050_reputation_store(self) -> None:
        migration = _load_migration()
        assert migration.revision == "0051_bundle_economy_store"
        assert migration.down_revision == "0050_bundle_reputation_store"

    def test_revision_id_fits_alembic_version_num_varchar32(self) -> None:
        assert len(_load_migration().revision) <= 32

    def test_ddl_file_the_migration_loads_exists(self) -> None:
        assert _load_migration()._SQL_PATH.is_file()

    def test_role_name_and_password_env_are_the_documented_ones(self) -> None:
        migration = _load_migration()
        assert migration._ROLE == "waddles_economy_runtime"
        assert migration._PASSWORD_ENV == "DB_ECONOMY_PASSWORD"


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0051-economy") as db:
        yield db


def _connect(db: PgTestDatabase, **overrides: str):  # type: ignore[no-untyped-def]
    conn = psycopg2.connect(
        host=db.host,
        port=db.port,
        user=overrides.get("user", db.user),
        password=overrides.get("password", db.password),
        dbname=db.dbname,
    )
    conn.autocommit = True
    return conn


@contextmanager
def _cursor(db: PgTestDatabase) -> Iterator[Any]:
    """One AUTOCOMMIT cursor (never `with conn`, which opens a transaction): a raised constraint error must not poison the next statement."""
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            yield cur
    finally:
        conn.close()


@requires_docker
class TestSchema:
    def test_balances_table_has_the_expected_columns_and_composite_pk(
        self, pg_db: PgTestDatabase
    ) -> None:
        with _cursor(pg_db) as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'economy_balances'"
            )
            assert {r[0] for r in cur.fetchall()} == {
                "tenant_id",
                "community_id",
                "user_uuid",
                "balance",
                "updated_at",
            }
            cur.execute(
                "SELECT a.attname FROM pg_index i "
                "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
                "WHERE i.indrelid = 'economy_balances'::regclass AND i.indisprimary "
                "ORDER BY array_position(i.indkey::int[], a.attnum)"
            )
            assert [r[0] for r in cur.fetchall()] == [
                "tenant_id",
                "community_id",
                "user_uuid",
            ]

    def test_ledger_table_has_the_expected_columns(self, pg_db: PgTestDatabase) -> None:
        with _cursor(pg_db) as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'economy_ledger'"
            )
            assert {r[0] for r in cur.fetchall()} == {
                "id",
                "tenant_id",
                "community_id",
                "app_id",
                "user_uuid",
                "counterparty_uuid",
                "kind",
                "delta",
                "stake",
                "payout",
                "balance_after",
                "occurred_at",
            }

    def test_balance_check_rejects_a_negative_balance(
        self, pg_db: PgTestDatabase
    ) -> None:
        with _cursor(pg_db) as cur:
            cur.execute("INSERT INTO tenants (slug) VALUES ('econ-t') RETURNING id")
            tid = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO communities (name, tenant_id) VALUES ('econ-c', %s) RETURNING id",
                (tid,),
            )
            cid = cur.fetchone()[0]
            uid = "11111111-2222-4333-8444-555555555555"
            cur.execute(
                "INSERT INTO economy_balances (tenant_id, community_id, user_uuid, balance) "
                "VALUES (%s, %s, %s, 5)",
                (tid, cid, uid),
            )
            with pytest.raises(psycopg2.errors.CheckViolation):
                cur.execute("UPDATE economy_balances SET balance = balance - 6")
            with pytest.raises(psycopg2.errors.UniqueViolation):
                cur.execute(
                    "INSERT INTO economy_balances (tenant_id, community_id, user_uuid) "
                    "VALUES (%s, %s, %s)",
                    (tid, cid, uid),
                )

    def test_ledger_kind_is_a_closed_set(self, pg_db: PgTestDatabase) -> None:
        with _cursor(pg_db) as cur:
            cur.execute("SELECT id FROM tenants LIMIT 1")
            tid = cur.fetchone()[0]
            cur.execute("SELECT id FROM communities LIMIT 1")
            cid = cur.fetchone()[0]
            with pytest.raises(psycopg2.errors.CheckViolation):
                cur.execute(
                    "INSERT INTO economy_ledger "
                    "(tenant_id, community_id, app_id, user_uuid, kind, delta, balance_after) "
                    "VALUES (%s, %s, 'a', '11111111-2222-4333-8444-555555555555', 'mint', 1, 1)",
                    (tid, cid),
                )

    def test_user_uuid_column_exists_and_is_shared_with_reputation_and_identity(
        self, pg_db: PgTestDatabase
    ) -> None:
        with _cursor(pg_db) as cur:
            cur.execute(
                "SELECT data_type, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'community_members' AND column_name = 'user_uuid'"
            )
            assert cur.fetchone() == ("uuid", "YES")

    def test_public_has_no_access_to_either_table(self, pg_db: PgTestDatabase) -> None:
        with _cursor(pg_db) as cur:
            for table in ("economy_balances", "economy_ledger"):
                cur.execute(
                    "SELECT has_table_privilege('public', %s, 'SELECT')", (table,)
                )
                assert cur.fetchone() == (False,), table

    def test_ddl_is_idempotent_a_second_application_succeeds(
        self, pg_db: PgTestDatabase
    ) -> None:
        sql = _load_migration()._SQL_PATH.read_text(encoding="utf-8")
        with _cursor(pg_db) as cur:
            cur.execute(sql)


@requires_docker
class TestRolePrivilegeBoundary:
    def test_role_exists_nologin_without_a_staged_password(
        self, pg_db: PgTestDatabase
    ) -> None:
        with _cursor(pg_db) as cur:
            cur.execute(
                "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication "
                "FROM pg_roles WHERE rolname = %s",
                (_ROLE,),
            )
            assert cur.fetchone() == (False, False, False, False, False)

    def test_role_privileges_are_exactly_the_least_privilege_set(
        self, pg_db: PgTestDatabase
    ) -> None:
        checks = {
            ("economy_balances", "SELECT"): True,
            # INSERT/UPDATE are COLUMN-scoped only (see the column test below):
            # no whole-table privilege, so a row can never be minted or re-keyed.
            ("economy_balances", "INSERT"): False,
            ("economy_balances", "UPDATE"): False,
            ("economy_balances", "DELETE"): False,
            ("economy_ledger", "SELECT"): True,
            ("economy_ledger", "INSERT"): True,
            # The ledger is append-only for this role: no rewrite, no delete.
            ("economy_ledger", "UPDATE"): False,
            ("economy_ledger", "DELETE"): False,
            # Membership tables: column-scoped SELECT only, never DML.
            ("community_members", "INSERT"): False,
            ("community_members", "UPDATE"): False,
            ("community_members", "DELETE"): False,
            ("communities", "INSERT"): False,
            ("communities", "UPDATE"): False,
            # Unrelated tables -- including the reputation store -- nothing.
            ("hub_users", "SELECT"): False,
            ("tenants", "SELECT"): False,
            ("bundle_reputation_scores", "SELECT"): False,
            ("bundle_reputation_adjustments", "SELECT"): False,
        }
        with _cursor(pg_db) as cur:
            for (table, priv), expected in checks.items():
                cur.execute(
                    "SELECT has_table_privilege(%s, %s, %s)", (_ROLE, table, priv)
                )
                assert cur.fetchone() == (expected,), f"{table} {priv}"

    def test_balances_writes_are_column_scoped_so_insert_cannot_mint(
        self, pg_db: PgTestDatabase
    ) -> None:
        checks = {
            # INSERT may name the identity columns only: balance takes its default 0.
            ("tenant_id", "INSERT"): True,
            ("community_id", "INSERT"): True,
            ("user_uuid", "INSERT"): True,
            ("balance", "INSERT"): False,
            # UPDATE may move balance/updated_at only: no re-keying a row.
            ("balance", "UPDATE"): True,
            ("updated_at", "UPDATE"): True,
            ("tenant_id", "UPDATE"): False,
            ("community_id", "UPDATE"): False,
            ("user_uuid", "UPDATE"): False,
        }
        with _cursor(pg_db) as cur:
            for (column, priv), expected in checks.items():
                cur.execute(
                    "SELECT has_column_privilege(%s, 'economy_balances', %s, %s)",
                    (_ROLE, column, priv),
                )
                assert cur.fetchone() == (expected,), f"{column} {priv}"

    def test_scope_window_index_exists_for_the_durable_daily_aggregate(
        self, pg_db: PgTestDatabase
    ) -> None:
        with _cursor(pg_db) as cur:
            cur.execute(
                "SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_economy_ledger_scope_window'"
            )
            row = cur.fetchone()
        assert row is not None
        assert "(tenant_id, community_id, app_id, kind, occurred_at)" in row[0]

    def test_member_table_select_is_column_scoped(self, pg_db: PgTestDatabase) -> None:
        with _cursor(pg_db) as cur:
            for column in (
                "community_id",
                "user_uuid",
                "is_active",
                "removed_at",
                "left_at",
            ):
                cur.execute(
                    "SELECT has_column_privilege(%s, 'community_members', %s, 'SELECT')",
                    (_ROLE, column),
                )
                assert cur.fetchone() == (True,), column
            # Whole-table SELECT (every column) must NOT be granted.
            cur.execute(
                "SELECT has_table_privilege(%s, 'community_members', 'SELECT')",
                (_ROLE,),
            )
            assert cur.fetchone() == (False,)
