"""Real-Postgres regression tests for 0036_connector_pii_reader_role.

Column-level `GRANT`/`REVOKE` cannot be verified against mocked SQL text
(a `GRANT SELECT (col) ON t TO role` statement executing without error
proves nothing about which columns are actually reachable) -- this
migration's one real guarantee, "the role can read only those columns",
requires an actual Postgres session authenticated as the role attempting
both an allowed and a forbidden `SELECT`. See `pg_docker.py`'s own module
docstring for the harness and why it bootstraps a minimal schema.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg2
import psycopg2.errors
import psycopg2.extensions
import pytest
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_ROLE = "waddles_connector_pii_reader"


@pytest.fixture(scope="session")
def pg_db() -> Iterator[PgTestDatabase]:
    """One real Postgres 17 container, migrated to `head`, shared by every test in this module."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0036-pii-reader") as db:
        yield db


@pytest.fixture(scope="session")
def seeded(pg_db: PgTestDatabase) -> PgTestDatabase:
    """Seed one hub_user + identity + community_member row, and a login role that can `SET ROLE` into `_ROLE`.

    `_ROLE` is NOLOGIN by design (this migration's own docstring) --
    exactly like every other role in this schema, a real process
    authenticates as a distinct login role and is granted membership.
    `connector_pii_reader_login` here stands in for whatever login role
    the helm auto-provision Secret (PR #445) actually creates per
    environment; this migration doesn't create that login role itself
    (out of its scope, per `_role_exists_guard`'s own docstring), so the
    test provisions a throwaway one.
    """
    conn = psycopg2.connect(pg_db.dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'connector_pii_reader_login') THEN "
            "CREATE ROLE connector_pii_reader_login LOGIN PASSWORD 'testpass123'; "
            "END IF; END $$;"
        )
        cur.execute(f"GRANT {_ROLE} TO connector_pii_reader_login")
        cur.execute(
            "INSERT INTO hub_users (id, email, password_hash) VALUES (1, 'someone@example.com', 'x') "
            "ON CONFLICT (id) DO NOTHING"
        )
        cur.execute(
            "INSERT INTO hub_user_identities (hub_user_id, platform, platform_user_id, platform_username) "
            "VALUES (1, 'discord', 'plat-123', 'someone#0001') ON CONFLICT DO NOTHING"
        )
        cur.execute(
            "INSERT INTO communities (id, name) VALUES (1, 'c') ON CONFLICT (id) DO NOTHING"
        )
        cur.execute(
            "INSERT INTO community_members (community_id, platform, platform_user_id, display_name, bio) "
            "VALUES (1, 'discord', 'plat-123', 'Someone', 'a private bio') ON CONFLICT DO NOTHING"
        )
    conn.close()
    return pg_db


@pytest.fixture
def reader_cur(seeded: PgTestDatabase) -> Iterator[psycopg2.extensions.cursor]:
    """A cursor authenticated as the login role, `SET ROLE`'d into `_ROLE` for the duration of the test."""
    dsn = f"postgresql://connector_pii_reader_login:testpass123@{seeded.host}:{seeded.port}/{seeded.dbname}"
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(f"SET ROLE {_ROLE}")
    yield cur
    cur.close()
    conn.close()


@requires_docker
class TestRoleReadsOnlyGrantedColumns:
    def test_role_can_select_hub_users_id_and_uuid(
        self, reader_cur: psycopg2.extensions.cursor
    ) -> None:
        reader_cur.execute("SELECT id, uuid FROM hub_users WHERE id = 1")
        row = reader_cur.fetchone()
        assert row is not None
        assert row[0] == 1
        assert (
            row[1] is not None
        )  # backfilled by the migration's DEFAULT gen_random_uuid()

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
class TestUuidBackfill:
    def test_every_existing_hub_user_row_got_a_uuid(
        self, seeded: PgTestDatabase
    ) -> None:
        conn = psycopg2.connect(seeded.dsn)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM hub_users WHERE uuid IS NULL")
                assert cur.fetchone()[0] == 0
                cur.execute("SELECT COUNT(DISTINCT uuid) FROM hub_users")
                cur.execute("SELECT COUNT(*) FROM hub_users")
                total = cur.fetchone()[0]
                cur.execute("SELECT COUNT(DISTINCT uuid) FROM hub_users")
                distinct = cur.fetchone()[0]
                assert total == distinct  # UNIQUE constraint holds
        finally:
            conn.close()
