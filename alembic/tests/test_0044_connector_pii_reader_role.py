"""Real-Postgres regression tests for 0044_connector_pii_reader_role.

Column-level `GRANT`/`REVOKE` cannot be verified against mocked SQL text
(a `GRANT SELECT (col) ON t TO role` statement executing without error
proves nothing about which columns are actually reachable) -- this
migration's one real guarantee, "the role can read only those columns",
requires an actual Postgres session authenticated as the role attempting
both an allowed and a forbidden `SELECT`.

**Why a bare container.** Column-level grants need a live session as the role,
so the test spins an ephemeral Postgres directly, hand-bootstraps the minimal
state `0043_hub_users_identity_uuid` produces (`hub_users.uuid`,
`hub_user_identities`, `community_members`), then runs 0044's real `upgrade()`
by patching `alembic.op.execute` to a live cursor. Full-chain replay is covered
separately by the alembic-chain CI job.
"""

from __future__ import annotations

import importlib.util
import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import psycopg2
import psycopg2.errors
import psycopg2.extensions
import pytest
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, _free_port, _wait_ready

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "versions"
    / "0044_connector_pii_reader_role.py"
)
_ROLE = "waddles_connector_pii_reader"

#: The state `0043_hub_users_identity_uuid` (PR #434) would have already
#: produced by the time 0044 runs in the real chain -- `hub_users.uuid`
#: populated/unique/not-null, plus the two mapping tables 0044 grants
#: column-scoped SELECT on. `email`/`password_hash`/`bio` stand in for
#: "every other PII column that must stay unreachable".
_PRE_0043_STATE_SQL = """
CREATE TABLE hub_users (
    id SERIAL PRIMARY KEY,
    uuid UUID NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    email TEXT,
    password_hash TEXT
);
CREATE TABLE hub_user_identities (
    id SERIAL PRIMARY KEY,
    hub_user_id INTEGER NOT NULL REFERENCES hub_users(id) ON DELETE CASCADE,
    platform VARCHAR(50) NOT NULL,
    platform_user_id VARCHAR(255) NOT NULL,
    platform_username VARCHAR(255),
    linked_at TIMESTAMP
);
CREATE TABLE communities (
    id SERIAL PRIMARY KEY,
    name TEXT
);
CREATE TABLE community_members (
    id SERIAL PRIMARY KEY,
    community_id INTEGER REFERENCES communities(id) ON DELETE CASCADE,
    platform VARCHAR(50),
    platform_user_id VARCHAR(255),
    display_name VARCHAR(255),
    bio TEXT
);
CREATE TABLE app_catalog (
    app_id VARCHAR(255) PRIMARY KEY
);
INSERT INTO hub_users (id, email, password_hash) VALUES (1, 'someone@example.com', 'x');
INSERT INTO hub_user_identities (hub_user_id, platform, platform_user_id, platform_username)
    VALUES (1, 'discord', 'plat-123', 'someone#0001');
INSERT INTO communities (id, name) VALUES (1, 'c');
INSERT INTO community_members (community_id, platform, platform_user_id, display_name, bio)
    VALUES (1, 'discord', 'plat-123', 'Someone', 'a private bio');
"""


def _load_migration():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(
        "migration_0044_connector_pii_reader_role", _MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def pg_db() -> Iterator[PgTestDatabase]:
    """One real, bare (non-alembic-managed) Postgres 17 container, bootstrapped to the pre-0044 state."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    container = "waddles-migtest-0044-pii-reader"
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
            input=_PRE_0043_STATE_SQL,
            text=True,
            capture_output=True,
            check=True,
        )
        yield db
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container], capture_output=True, check=False
        )


@pytest.fixture(scope="session")
def seeded(pg_db: PgTestDatabase) -> PgTestDatabase:
    """Run 0044's real `upgrade()` against `pg_db` (op.execute patched to a live cursor), then add a login role."""
    conn = psycopg2.connect(pg_db.dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        migration = _load_migration()
        with patch("alembic.op.execute", side_effect=cur.execute):
            migration.upgrade()

        # `_ROLE` is NOLOGIN by design (this migration's own docstring) --
        # exactly like every other role in this schema, a real process
        # authenticates as a distinct login role and is granted membership.
        # `connector_pii_reader_login` stands in for whatever login role the
        # helm auto-provision Secret (PR #445) actually creates per
        # environment.
        cur.execute(
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'connector_pii_reader_login') THEN "
            "CREATE ROLE connector_pii_reader_login LOGIN PASSWORD 'testpass123'; "
            "END IF; END $$;"
        )
        cur.execute(f"GRANT {_ROLE} TO connector_pii_reader_login")
    conn.close()
    return pg_db


@pytest.fixture
def reader_cur(seeded: PgTestDatabase) -> Iterator[psycopg2.extensions.cursor]:
    """A cursor authenticated as the login role, `SET ROLE`'d into `_ROLE` for the duration of the test."""
    dsn = (
        f"postgresql://connector_pii_reader_login:testpass123@"
        f"{seeded.host}:{seeded.port}/{seeded.dbname}"
    )
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(f"SET ROLE {_ROLE}")
    yield cur
    cur.close()
    conn.close()


@requires_docker
class TestMigrationMetadata:
    def test_chains_off_0043_hub_users_identity_uuid(self) -> None:
        migration = _load_migration()
        assert migration.revision == "0044_connector_pii_reader_role"
        assert migration.down_revision == "0043_hub_users_identity_uuid"

    def test_revision_id_fits_alembic_version_num_varchar32(self) -> None:
        migration = _load_migration()
        assert len(migration.revision) <= 32

    def test_upgrade_does_not_touch_hub_users_uuid_column(self) -> None:
        """0044 must not add/backfill hub_users.uuid -- that's 0043's job (PR #434)."""
        migration = _load_migration()
        with patch("alembic.op.execute") as mock_execute:
            migration.upgrade()
        upgrade_sql = "\n".join(call.args[0] for call in mock_execute.call_args_list)
        assert "ADD COLUMN" not in upgrade_sql
        assert "gen_random_uuid" not in upgrade_sql


@requires_docker
class TestRoleReadsOnlyGrantedColumns:
    def test_role_can_select_hub_users_id_and_uuid(
        self, reader_cur: psycopg2.extensions.cursor
    ) -> None:
        reader_cur.execute("SELECT id, uuid FROM hub_users WHERE id = 1")
        row = reader_cur.fetchone()
        assert row is not None
        assert row[0] == 1
        assert row[1] is not None

    def test_role_cannot_select_hub_users_email(
        self, reader_cur: psycopg2.extensions.cursor
    ) -> None:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            reader_cur.execute("SELECT email FROM hub_users WHERE id = 1")

    def test_role_cannot_select_hub_users_password_hash(
        self, reader_cur: psycopg2.extensions.cursor
    ) -> None:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            reader_cur.execute("SELECT password_hash FROM hub_users WHERE id = 1")

    def test_role_can_select_hub_user_identities_mapping_columns(
        self, reader_cur: psycopg2.extensions.cursor
    ) -> None:
        reader_cur.execute(
            "SELECT hub_user_id, platform, platform_user_id, platform_username "
            "FROM hub_user_identities WHERE platform_user_id = 'plat-123'"
        )
        row = reader_cur.fetchone()
        assert row == (1, "discord", "plat-123", "someone#0001")

    def test_role_cannot_select_hub_user_identities_linked_at(
        self, reader_cur: psycopg2.extensions.cursor
    ) -> None:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            reader_cur.execute(
                "SELECT linked_at FROM hub_user_identities WHERE hub_user_id = 1"
            )

    def test_role_can_select_community_members_identity_columns(
        self, reader_cur: psycopg2.extensions.cursor
    ) -> None:
        reader_cur.execute(
            "SELECT community_id, platform, platform_user_id, display_name "
            "FROM community_members WHERE platform_user_id = 'plat-123'"
        )
        row = reader_cur.fetchone()
        assert row == (1, "discord", "plat-123", "Someone")

    def test_role_cannot_select_community_members_bio(
        self, reader_cur: psycopg2.extensions.cursor
    ) -> None:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            reader_cur.execute(
                "SELECT bio FROM community_members WHERE community_id = 1"
            )

    def test_role_cannot_write_anywhere(
        self, reader_cur: psycopg2.extensions.cursor
    ) -> None:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            reader_cur.execute(
                "UPDATE hub_user_identities SET platform_username = 'x' WHERE hub_user_id = 1"
            )

    def test_role_has_no_access_to_unrelated_tables(
        self, reader_cur: psycopg2.extensions.cursor
    ) -> None:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            reader_cur.execute("SELECT app_id FROM app_catalog")


@requires_docker
class TestUpgradeIsIdempotentOnRerun:
    def test_rerunning_upgrade_against_a_live_db_does_not_error(
        self, seeded: PgTestDatabase
    ) -> None:
        """`seeded` already ran upgrade() once; running it again must be a safe no-op."""
        conn = psycopg2.connect(seeded.dsn)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                migration = _load_migration()
                with patch("alembic.op.execute", side_effect=cur.execute):
                    migration.upgrade()
        finally:
            conn.close()
