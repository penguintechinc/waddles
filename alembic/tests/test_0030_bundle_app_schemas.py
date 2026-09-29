"""Real-Postgres regression tests for 0030_bundle_app_schemas.

Privilege boundaries -- a role being outright denied a statement, a
role-scoped default-privilege grant firing correctly, a pinned search_path
-- cannot be verified against mocked SQL text; see `pg_docker.py`'s own
module docstring and `test_0028_bundle_active_set_changelog.py`'s identical
rationale for why this file is one of the two exceptions in this directory
that runs the full `alembic upgrade head` chain against a real container.

Both `waddles_bundle_migrator` and `waddles_bundle_runtime` are real LOGIN
roles (unlike the NOLOGIN RBAC-matrix roles `test_0020_ingest_sources_rbac.py`
-style tests exercise via `SET ROLE`), so these tests connect directly AS
each role with its own password to assert what it can and cannot do.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import psycopg2
import psycopg2.errors
import psycopg2.extensions
import pytest
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, alembic_cli, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_MIGRATOR_PASSWORD = "phase0-migrator-test-pw"
_RUNTIME_PASSWORD = "phase0-runtime-test-pw"


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

    `BUNDLE_MIGRATOR_PASSWORD`/`BUNDLE_RUNTIME_PASSWORD` are staged in this
    process's own `os.environ` before `migrated_postgres` spawns the
    `alembic upgrade head` subprocess -- that subprocess inherits via
    `env={**os.environ, ...}` (`pg_docker.py`), so this is the only place
    these two roles' real test passwords are set.
    """
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    os.environ["BUNDLE_MIGRATOR_PASSWORD"] = _MIGRATOR_PASSWORD
    os.environ["BUNDLE_RUNTIME_PASSWORD"] = _RUNTIME_PASSWORD
    try:
        with migrated_postgres("0030-bundle-app-schemas") as db:
            yield db
    finally:
        os.environ.pop("BUNDLE_MIGRATOR_PASSWORD", None)
        os.environ.pop("BUNDLE_RUNTIME_PASSWORD", None)


@requires_docker
class TestSchemasAndRolesProvisioned:
    """The two app schemas exist, owned by the migrator role, per spec Sec2."""

    def test_schemas_owned_by_migrator(self, pg_db: PgTestDatabase) -> None:
        conn = psycopg2.connect(pg_db.dsn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT nspname, pg_get_userbyid(nspowner) FROM pg_namespace "
                "WHERE nspname IN ('app_core', 'app_community') ORDER BY nspname"
            )
            rows = cur.fetchall()
        conn.close()
        assert rows == [
            ("app_community", "waddles_bundle_migrator"),
            ("app_core", "waddles_bundle_migrator"),
        ]

    def test_both_roles_are_login(self, pg_db: PgTestDatabase) -> None:
        conn = psycopg2.connect(pg_db.dsn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole FROM pg_roles "
                "WHERE rolname IN ('waddles_bundle_migrator', 'waddles_bundle_runtime') ORDER BY rolname"
            )
            rows = cur.fetchall()
        conn.close()
        assert rows == [
            ("waddles_bundle_migrator", True, False, False, False),
            ("waddles_bundle_runtime", True, False, False, False),
        ]


@requires_docker
class TestRuntimeRoleDenied:
    """`waddles_bundle_runtime` is DML-only on the two app schemas -- everything else is denied."""

    def test_cannot_create_table_in_app_core(self, pg_db: PgTestDatabase) -> None:
        conn = _connect_as(pg_db, "waddles_bundle_runtime", _RUNTIME_PASSWORD)
        try:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege), conn.cursor() as cur:
                cur.execute("CREATE TABLE app_core.phase0_should_fail (id uuid)")
        finally:
            conn.close()

    def test_cannot_read_public_schema_tables(self, pg_db: PgTestDatabase) -> None:
        """Fully-qualified (`public.ingest_sources`), not relying on the pinned search_path alone.

        `ingest_sources` is created with real RBAC-matrix grants by
        migration 0020 -- proves the denial is `waddles_bundle_runtime`'s
        own explicit REVOKE (Sec1 C3.1), not merely "table doesn't exist in
        my search_path".
        """
        conn = _connect_as(pg_db, "waddles_bundle_runtime", _RUNTIME_PASSWORD)
        try:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege), conn.cursor() as cur:
                cur.execute("SELECT 1 FROM public.ingest_sources LIMIT 1")
        finally:
            conn.close()

    def test_cannot_create_temp_table(self, pg_db: PgTestDatabase) -> None:
        conn = _connect_as(pg_db, "waddles_bundle_runtime", _RUNTIME_PASSWORD)
        try:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege), conn.cursor() as cur:
                cur.execute("CREATE TEMP TABLE phase0_should_fail (id uuid)")
        finally:
            conn.close()


@requires_docker
class TestRuntimeRoleAllowed:
    """`waddles_bundle_runtime` CAN do full DML on a table the migrator created in `app_core`."""

    def test_dml_on_migrator_created_table(self, pg_db: PgTestDatabase) -> None:
        migrator_conn = _connect_as(pg_db, "waddles_bundle_migrator", _MIGRATOR_PASSWORD)
        try:
            with migrator_conn.cursor() as cur:
                cur.execute(
                    "CREATE TABLE app_core.phase0_probe ("
                    "  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),"
                    "  note text"
                    ")"
                )
        finally:
            migrator_conn.close()

        runtime_conn = _connect_as(pg_db, "waddles_bundle_runtime", _RUNTIME_PASSWORD)
        try:
            with runtime_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app_core.phase0_probe (note) VALUES ('hello') RETURNING id"
                )
                row_id = cur.fetchone()[0]

                cur.execute("SELECT note FROM app_core.phase0_probe WHERE id = %s", (row_id,))
                assert cur.fetchone() == ("hello",)

                cur.execute(
                    "UPDATE app_core.phase0_probe SET note = 'updated' WHERE id = %s", (row_id,)
                )
                cur.execute("SELECT note FROM app_core.phase0_probe WHERE id = %s", (row_id,))
                assert cur.fetchone() == ("updated",)

                cur.execute("DELETE FROM app_core.phase0_probe WHERE id = %s", (row_id,))
                cur.execute("SELECT COUNT(*) FROM app_core.phase0_probe WHERE id = %s", (row_id,))
                assert cur.fetchone() == (0,)
        finally:
            runtime_conn.close()

        # Restore app_core to empty so a later downgrade (this module's own
        # round-trip test, or a developer running `alembic downgrade` by
        # hand) hits DROP SCHEMA ... RESTRICT cleanly.
        migrator_conn = _connect_as(pg_db, "waddles_bundle_migrator", _MIGRATOR_PASSWORD)
        try:
            with migrator_conn.cursor() as cur:
                cur.execute("DROP TABLE app_core.phase0_probe")
        finally:
            migrator_conn.close()


@requires_docker
class TestDowngradeThenUpgrade:
    """Symmetric downgrade: drops both roles/schemas cleanly, then a fresh upgrade recreates them.

    Uses its own container (not the module-scoped `pg_db` above) so this
    test's outcome never depends on execution order against the other
    classes in this file.
    """

    def test_downgrade_then_upgrade_recreates_everything(self) -> None:
        os.environ["BUNDLE_MIGRATOR_PASSWORD"] = _MIGRATOR_PASSWORD
        os.environ["BUNDLE_RUNTIME_PASSWORD"] = _RUNTIME_PASSWORD
        try:
            with migrated_postgres("0030-bundle-app-schemas-roundtrip") as db:
                alembic_cli("downgrade", "0029_bundle_attribution_metadata", dsn=db.dsn)

                conn = psycopg2.connect(db.dsn)
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT rolname FROM pg_roles "
                        "WHERE rolname IN ('waddles_bundle_migrator', 'waddles_bundle_runtime')"
                    )
                    assert cur.fetchall() == []
                    cur.execute(
                        "SELECT nspname FROM pg_namespace WHERE nspname IN ('app_core', 'app_community')"
                    )
                    assert cur.fetchall() == []
                    # `has_schema_privilege` needs a real role, not the
                    # 'PUBLIC' keyword -- a throwaway role with zero
                    # explicit grants of its own only sees USAGE here if
                    # the PUBLIC-wide default was actually restored.
                    cur.execute("CREATE ROLE phase0_pub_probe LOGIN PASSWORD 'probe'")
                    cur.execute(
                        "SELECT has_schema_privilege('phase0_pub_probe', 'public', 'USAGE')"
                    )
                    assert cur.fetchone() == (True,)
                    cur.execute(
                        "SELECT has_database_privilege("
                        "'phase0_pub_probe', current_database(), 'TEMPORARY')"
                    )
                    assert cur.fetchone() == (True,)
                    cur.execute("DROP ROLE phase0_pub_probe")
                conn.close()

                alembic_cli("upgrade", "head", dsn=db.dsn)

                conn = psycopg2.connect(db.dsn)
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT nspname, pg_get_userbyid(nspowner) FROM pg_namespace "
                        "WHERE nspname IN ('app_core', 'app_community') ORDER BY nspname"
                    )
                    rows = cur.fetchall()
                conn.close()
                assert rows == [
                    ("app_community", "waddles_bundle_migrator"),
                    ("app_core", "waddles_bundle_migrator"),
                ]
        finally:
            os.environ.pop("BUNDLE_MIGRATOR_PASSWORD", None)
            os.environ.pop("BUNDLE_RUNTIME_PASSWORD", None)
