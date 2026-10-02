"""Real-Postgres regression tests for 0032_bundle_reader_role.

# regression: RO reader role never provisioned, multi-app path stayed off (alpha 2026-10-02)

Privilege boundaries -- SELECT allowed on exactly the eight tables this
migration grants, denied on everything else, and a LOGIN role that can
actually authenticate with its staged password -- cannot be verified
against mocked SQL text; see `test_0030_bundle_app_schemas.py`'s identical
rationale for why this is one of the real-Postgres exceptions in this
directory.
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Iterator
from pathlib import Path

import psycopg2
import psycopg2.errors
import psycopg2.extensions
import pytest
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, alembic_cli, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_READER_PASSWORD = "phase1-reader-test-pw"
_READER_ROLE = "waddles_bundle_reader"

#: Exactly what the migration grants -- SELECT must succeed on every one of these.
_GRANTED_TABLES = (
    "app_active_versions",
    "app_versions",
    "app_install_approvals",
    "app_source_bindings",
    "tenants",
    "communities",
    "bundle_active_set_changes",
    "bundle_active_set_watermark",
)

#: Pre-existing tables NOT in the grant list -- SELECT must be denied, proving
#: the grant is scoped exactly to _GRANTED_TABLES, never a schema-wide blanket.
_UNGRANTED_TABLES = ("ingest_sources", "hub_users")

_MIGRATION_MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "versions" / "0032_bundle_reader_role.py"
)


def _load_migration_module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("waddles_0032_bundle_reader_role", _MIGRATION_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _connect_as(db: PgTestDatabase, user: str, password: str) -> psycopg2.extensions.connection:
    """One autocommit connection as `user` -- so a raised privilege error never leaves a dangling failed transaction."""
    conn = psycopg2.connect(
        host=db.host, port=db.port, user=user, password=password, dbname=db.dbname
    )
    conn.autocommit = True
    return conn


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """One real Postgres 17 container, migrated to `head`, shared by every non-round-trip test below.

    `DB_READER_PASSWORD` is staged in this process's own `os.environ` before
    `migrated_postgres` spawns the `alembic upgrade head` subprocess -- that
    subprocess inherits via `env={**os.environ, ...}` (`pg_docker.py`).
    """
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    os.environ["DB_READER_PASSWORD"] = _READER_PASSWORD
    try:
        with migrated_postgres("0032-bundle-reader-role") as db:
            yield db
    finally:
        os.environ.pop("DB_READER_PASSWORD", None)


@requires_docker
class TestRoleProvisioned:
    """The role exists, is LOGIN, non-superuser, and authenticates with the staged password."""

    def test_role_is_login_non_privileged(self, pg_db: PgTestDatabase) -> None:
        conn = psycopg2.connect(pg_db.dsn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication "
                "FROM pg_roles WHERE rolname = %s",
                (_READER_ROLE,),
            )
            row = cur.fetchone()
        conn.close()
        assert row == (True, False, False, False, False)

    def test_can_authenticate_with_staged_password(self, pg_db: PgTestDatabase) -> None:
        conn = _connect_as(pg_db, _READER_ROLE, _READER_PASSWORD)
        conn.close()


@requires_docker
class TestReaderGrants:
    """SELECT succeeds on exactly the eight granted tables, denied everywhere else."""

    @pytest.mark.parametrize("table", _GRANTED_TABLES)
    def test_select_allowed(self, pg_db: PgTestDatabase, table: str) -> None:
        conn = _connect_as(pg_db, _READER_ROLE, _READER_PASSWORD)
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {table} LIMIT 0")  # noqa: S608 -- fixed literal from test param list
        finally:
            conn.close()

    @pytest.mark.parametrize("table", _UNGRANTED_TABLES)
    def test_select_denied_on_ungranted_table(self, pg_db: PgTestDatabase, table: str) -> None:
        conn = _connect_as(pg_db, _READER_ROLE, _READER_PASSWORD)
        try:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege), conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {table} LIMIT 0")  # noqa: S608 -- fixed literal from test param list
        finally:
            conn.close()

    def test_write_denied_on_granted_table(self, pg_db: PgTestDatabase) -> None:
        """SELECT-only: no INSERT privilege even on a table this role CAN read."""
        conn = _connect_as(pg_db, _READER_ROLE, _READER_PASSWORD)
        try:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege), conn.cursor() as cur:
                cur.execute("INSERT INTO communities (name) VALUES ('should-fail')")
        finally:
            conn.close()


@requires_docker
class TestDowngradeThenUpgrade:
    """Symmetric downgrade: drops the role and its grants cleanly, then a fresh upgrade recreates it.

    Uses its own container (not the module-scoped `pg_db` above) so this
    test's outcome never depends on execution order against the other
    classes in this file.
    """

    def test_downgrade_then_upgrade_recreates_role_and_grants(self) -> None:
        os.environ["DB_READER_PASSWORD"] = _READER_PASSWORD
        try:
            with migrated_postgres("0032-bundle-reader-role-roundtrip") as db:
                alembic_cli("downgrade", "0031_upload_status_changed_at", dsn=db.dsn)

                conn = psycopg2.connect(db.dsn)
                with conn.cursor() as cur:
                    cur.execute("SELECT rolname FROM pg_roles WHERE rolname = %s", (_READER_ROLE,))
                    assert cur.fetchall() == []
                conn.close()

                alembic_cli("upgrade", "head", dsn=db.dsn)

                conn = psycopg2.connect(db.dsn)
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT rolcanlogin FROM pg_roles WHERE rolname = %s", (_READER_ROLE,)
                    )
                    assert cur.fetchone() == (True,)
                conn.close()

                reader_conn = _connect_as(db, _READER_ROLE, _READER_PASSWORD)
                try:
                    with reader_conn.cursor() as cur:
                        cur.execute("SELECT * FROM app_source_bindings LIMIT 0")
                finally:
                    reader_conn.close()
        finally:
            os.environ.pop("DB_READER_PASSWORD", None)


@requires_docker
class TestRefusesEmptyPassword:
    """fix/no-empty-kept-secrets -- 0032's `upgrade()` must FAIL LOUD on an empty
    DB_READER_PASSWORD, never silently provision the role with no usable password.

    # regression: lookup-keep preserved EMPTY reader password; multi-app path off (alpha 2026-10-02)
    """

    def test_upgrade_raises_on_empty_reader_password(self) -> None:
        os.environ.pop("DB_READER_PASSWORD", None)
        try:
            with pytest.raises(RuntimeError, match="DB_READER_PASSWORD"):
                with migrated_postgres("0032-bundle-reader-role-empty-pw"):
                    pass
        finally:
            os.environ.pop("DB_READER_PASSWORD", None)


@requires_docker
class TestIdempotentRoleCreation:
    """Re-running the role-creation DDL against an already-existing role is a safe password refresh, never an error."""

    def test_rerunning_create_or_update_updates_password(self, pg_db: PgTestDatabase) -> None:
        module = _load_migration_module()
        new_password = "phase1-reader-rotated-pw"  # noqa: S105 -- test fixture value, never a real secret

        conn = psycopg2.connect(pg_db.dsn)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config(%s, %s, false)",
                    (module._password_guc(_READER_ROLE), new_password),
                )
                # Must not raise -- the role already exists from the module-scoped
                # migration; this exercises the ELSIF ... ALTER ROLE branch, never
                # the IF NOT EXISTS ... CREATE ROLE branch.
                cur.execute(module._create_or_update_login_role(_READER_ROLE))
        finally:
            conn.close()

        # The OLD password from the module-scoped fixture must no longer work...
        with pytest.raises(psycopg2.OperationalError):
            _connect_as(pg_db, _READER_ROLE, _READER_PASSWORD)
        # ...the rotated one must.
        rotated_conn = _connect_as(pg_db, _READER_ROLE, new_password)
        rotated_conn.close()

        # Restore the module-scoped fixture's own password so later tests in
        # this file (which may run after this one in file order) still work.
        conn = psycopg2.connect(pg_db.dsn)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config(%s, %s, false)",
                    (module._password_guc(_READER_ROLE), _READER_PASSWORD),
                )
                cur.execute(module._create_or_update_login_role(_READER_ROLE))
        finally:
            conn.close()
