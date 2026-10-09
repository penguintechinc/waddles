"""Real-Postgres tests for 0046_bundle_reputation_store (issue #726).

Runs the actual Alembic chain to `head` in an ephemeral container (see
`pg_docker.py`) and asserts the schema, the fail-closed identity column, and
the `waddles_bundle_reputation` role's privilege boundary -- none of which a
mocked `op.execute` could verify.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import psycopg2
import psycopg2.errors
import pytest
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1] / "versions" / "0046_bundle_reputation_store.py"
)
_ROLE = "waddles_bundle_reputation"


def _load_migration():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("migration_0046", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMigrationMetadata:
    def test_chains_off_0045(self) -> None:
        migration = _load_migration()
        assert migration.revision == "0046_bundle_reputation_store"
        assert migration.down_revision == "0045_identity_resolution"

    def test_revision_id_fits_alembic_version_num_varchar32(self) -> None:
        assert len(_load_migration().revision) <= 32

    def test_ddl_file_the_migration_loads_exists(self) -> None:
        assert _load_migration()._SQL_PATH.is_file()


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0046-reputation") as db:
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


@requires_docker
class TestSchema:
    def test_scores_table_has_the_expected_columns(self, pg_db: PgTestDatabase) -> None:
        with _connect(pg_db) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'bundle_reputation_scores'"
            )
            cols = {r[0] for r in cur.fetchall()}
        assert cols == {
            "tenant_id",
            "community_id",
            "user_uuid",
            "balance",
            "adjustment_count",
            "created_at",
            "updated_at",
        }

    def test_community_members_gained_a_nullable_user_uuid(self, pg_db: PgTestDatabase) -> None:
        with _connect(pg_db) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT data_type, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'community_members' AND column_name = 'user_uuid'"
            )
            assert cur.fetchone() == ("uuid", "YES")

    def test_user_uuid_is_unique_per_community_but_null_is_unconstrained(
        self, pg_db: PgTestDatabase
    ) -> None:
        with _connect(pg_db) as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO communities (name, tenant_id) VALUES ('c', 1) RETURNING id")
            cid = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO community_members (community_id) VALUES (%s), (%s)", (cid, cid)
            )  # two NULL user_uuid rows are fine
            cur.execute(
                "INSERT INTO community_members (community_id, user_uuid) "
                "VALUES (%s, '11111111-2222-4333-8444-555555555555')",
                (cid,),
            )
            with pytest.raises(psycopg2.errors.UniqueViolation):
                cur.execute(
                    "INSERT INTO community_members (community_id, user_uuid) "
                    "VALUES (%s, '11111111-2222-4333-8444-555555555555')",
                    (cid,),
                )

    def test_public_has_no_access_to_the_scores_table(self, pg_db: PgTestDatabase) -> None:
        with _connect(pg_db) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT has_table_privilege('public', 'bundle_reputation_scores', 'SELECT')"
            )
            assert cur.fetchone() == (False,)


@requires_docker
class TestRolePrivilegeBoundary:
    def test_role_exists_nologin_without_a_staged_password(self, pg_db: PgTestDatabase) -> None:
        with _connect(pg_db) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole "
                "FROM pg_roles WHERE rolname = %s",
                (_ROLE,),
            )
            assert cur.fetchone() == (False, False, False, False)

    def test_role_privileges_are_exactly_the_least_privilege_set(
        self, pg_db: PgTestDatabase
    ) -> None:
        checks = {
            ("bundle_reputation_scores", "SELECT"): True,
            ("bundle_reputation_scores", "INSERT"): True,
            ("bundle_reputation_scores", "UPDATE"): True,
            ("bundle_reputation_scores", "DELETE"): False,
            ("bundle_reputation_adjustments", "SELECT"): True,
            ("bundle_reputation_adjustments", "INSERT"): True,
            # The ledger is append-only for this role: no rewrite, no delete.
            ("bundle_reputation_adjustments", "UPDATE"): False,
            ("bundle_reputation_adjustments", "DELETE"): False,
            # Membership tables: column-scoped SELECT only, never DML.
            ("community_members", "INSERT"): False,
            ("community_members", "UPDATE"): False,
            ("community_members", "DELETE"): False,
            ("communities", "INSERT"): False,
            ("communities", "UPDATE"): False,
            # Unrelated tables: nothing.
            ("hub_users", "SELECT"): False,
            ("tenants", "SELECT"): False,
        }
        with _connect(pg_db) as conn, conn.cursor() as cur:
            for (table, priv), expected in checks.items():
                cur.execute("SELECT has_table_privilege(%s, %s, %s)", (_ROLE, table, priv))
                assert cur.fetchone() == (expected,), f"{table} {priv}"

    def test_member_table_select_is_column_scoped(self, pg_db: PgTestDatabase) -> None:
        with _connect(pg_db) as conn, conn.cursor() as cur:
            for column, expected in (
                ("community_id", True),
                ("user_uuid", True),
                ("is_active", True),
                ("removed_at", True),
                ("left_at", True),
            ):
                cur.execute(
                    "SELECT has_column_privilege(%s, 'community_members', %s, 'SELECT')",
                    (_ROLE, column),
                )
                assert cur.fetchone() == (expected,), column
            # Whole-table SELECT (every column) must NOT be granted.
            cur.execute("SELECT has_table_privilege(%s, 'community_members', 'SELECT')", (_ROLE,))
            assert cur.fetchone() == (False,)
