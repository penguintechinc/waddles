"""Real-Postgres regression tests for 0035_connection_model_layers.

Proves the three-layer platform-connection model (see that migration's
own docstring, and `docs/CONNECTION_MODEL.local.md`) actually lands on a
real Postgres 17 container, round-trips upgrade -> downgrade -> upgrade
idempotently, and that the `tenant_platform_credentials` compatibility
view (kept for the still-open, stacked PR #563 chain) is read/write
transparent against the renamed `tenant_platform_apps` table.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg2
import psycopg2.errors
import psycopg2.extensions
import pytest
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, alembic_cli, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)


def _connect(db: PgTestDatabase) -> psycopg2.extensions.connection:
    conn = psycopg2.connect(db.dsn)
    conn.autocommit = True
    return conn


def _seed_tenant(cur: psycopg2.extensions.cursor, *, slug: str, is_global: bool) -> int:
    cur.execute(
        "INSERT INTO tenants (slug, is_global) VALUES (%s, %s) RETURNING id", (slug, is_global)
    )
    return int(cur.fetchone()[0])


def _seed_community(cur: psycopg2.extensions.cursor, *, name: str) -> int:
    cur.execute("INSERT INTO communities (name) VALUES (%s) RETURNING id", (name,))
    return int(cur.fetchone()[0])


def _seed_connection(
    cur: psycopg2.extensions.cursor,
    *,
    tenant_id: int,
    platform: str = "discord",
    resource_type: str = "discord_guild",
    resource_id: str,
) -> int:
    cur.execute(
        "INSERT INTO platform_connections "
        "(tenant_id, platform, resource_type, resource_id, access_token) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (tenant_id, platform, resource_type, resource_id, "encrypted-access-token-blob"),
    )
    return int(cur.fetchone()[0])


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """One real Postgres 17 container, migrated to `head`, shared by every non-round-trip test below."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0035-connection-model") as db:
        yield db


@requires_docker
class TestTenantPlatformAppsRename:
    def test_renamed_table_has_expected_columns(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'tenant_platform_apps' ORDER BY column_name"
                )
                columns = {r[0] for r in cur.fetchall()}
                assert columns == {
                    "id",
                    "tenant_id",
                    "platform",
                    "credentials_ciphertext",
                    "key_ref",
                    "installed_by_user_id",
                    "created_at",
                    "updated_at",
                }
                # old name no longer a base table
                cur.execute(
                    "SELECT table_type FROM information_schema.tables "
                    "WHERE table_name = 'tenant_platform_credentials'"
                )
                assert cur.fetchone()[0] == "VIEW"
        finally:
            conn.close()

    def test_insert_into_renamed_table_and_unique(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                tenant_id = _seed_tenant(cur, slug="acme-conn-1", is_global=False)
                cur.execute(
                    "INSERT INTO tenant_platform_apps (tenant_id, platform, "
                    "credentials_ciphertext) VALUES (%s, %s, %s)",
                    (tenant_id, "discord", "app-blob-1"),
                )
            with (
                pytest.raises(psycopg2.errors.UniqueViolation),
                conn.cursor() as cur,
            ):
                cur.execute(
                    "INSERT INTO tenant_platform_apps (tenant_id, platform, "
                    "credentials_ciphertext) VALUES (%s, %s, %s)",
                    (tenant_id, "discord", "app-blob-2"),
                )
        finally:
            conn.close()

    def test_global_tenant_still_rejected_by_renamed_trigger(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                tenant_id = _seed_tenant(cur, slug="global-conn", is_global=True)
            with (
                pytest.raises(psycopg2.errors.RaiseException),
                conn.cursor() as cur,
            ):
                cur.execute(
                    "INSERT INTO tenant_platform_apps (tenant_id, platform, "
                    "credentials_ciphertext) VALUES (%s, %s, %s)",
                    (tenant_id, "discord", "should-fail"),
                )
        finally:
            conn.close()


@requires_docker
class TestCompatibilityViewWriteThrough:
    """Writes through the old `tenant_platform_credentials` name must land in `tenant_platform_apps`."""

    def test_insert_via_view_lands_in_base_table(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                tenant_id = _seed_tenant(cur, slug="acme-view-1", is_global=False)
                cur.execute(
                    "INSERT INTO tenant_platform_credentials (tenant_id, platform, "
                    "credentials_ciphertext) VALUES (%s, %s, %s) RETURNING id",
                    (tenant_id, "twitch", "view-insert-blob"),
                )
                new_id = cur.fetchone()[0]
                cur.execute(
                    "SELECT credentials_ciphertext FROM tenant_platform_apps WHERE id = %s",
                    (new_id,),
                )
                assert cur.fetchone()[0] == "view-insert-blob"
        finally:
            conn.close()

    def test_update_via_view_updates_base_table(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                tenant_id = _seed_tenant(cur, slug="acme-view-2", is_global=False)
                cur.execute(
                    "INSERT INTO tenant_platform_apps (tenant_id, platform, "
                    "credentials_ciphertext) VALUES (%s, %s, %s) RETURNING id",
                    (tenant_id, "discord", "before-update"),
                )
                row_id = cur.fetchone()[0]
                cur.execute(
                    "UPDATE tenant_platform_credentials SET credentials_ciphertext = %s "
                    "WHERE id = %s",
                    ("after-update", row_id),
                )
                cur.execute(
                    "SELECT credentials_ciphertext FROM tenant_platform_apps WHERE id = %s",
                    (row_id,),
                )
                assert cur.fetchone()[0] == "after-update"
        finally:
            conn.close()

    def test_delete_via_view_deletes_base_table_row(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                tenant_id = _seed_tenant(cur, slug="acme-view-3", is_global=False)
                cur.execute(
                    "INSERT INTO tenant_platform_apps (tenant_id, platform, "
                    "credentials_ciphertext) VALUES (%s, %s, %s) RETURNING id",
                    (tenant_id, "discord", "to-delete"),
                )
                row_id = cur.fetchone()[0]
                cur.execute(
                    "DELETE FROM tenant_platform_credentials WHERE id = %s", (row_id,)
                )
                cur.execute(
                    "SELECT 1 FROM tenant_platform_apps WHERE id = %s", (row_id,)
                )
                assert cur.fetchone() is None
        finally:
            conn.close()

    def test_global_tenant_rejected_through_view_too(self, pg_db: PgTestDatabase) -> None:
        """The view's INSTEAD OF trigger performs a real INSERT, so the base table's
        BEFORE INSERT tenant-0-reject trigger still fires."""
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                tenant_id = _seed_tenant(cur, slug="global-view", is_global=True)
            with (
                pytest.raises(psycopg2.errors.RaiseException),
                conn.cursor() as cur,
            ):
                cur.execute(
                    "INSERT INTO tenant_platform_credentials (tenant_id, platform, "
                    "credentials_ciphertext) VALUES (%s, %s, %s)",
                    (tenant_id, "discord", "should-fail-via-view"),
                )
        finally:
            conn.close()


@requires_docker
class TestPlatformConnections:
    def test_insert_and_unique_per_resource(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                tenant_id = _seed_tenant(cur, slug="acme-pc-1", is_global=False)
                _seed_connection(cur, tenant_id=tenant_id, resource_id="guild-pc-1")
            with (
                pytest.raises(psycopg2.errors.UniqueViolation),
                conn.cursor() as cur,
            ):
                _seed_connection(cur, tenant_id=tenant_id, resource_id="guild-pc-1")
        finally:
            conn.close()

    def test_tenant_zero_allowed_no_app_row_required(self, pg_db: PgTestDatabase) -> None:
        """Tenant 0 has no tenant_platform_apps row (trigger-rejected) but CAN have a
        platform_connections row -- no FK ties the two tables together."""
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                tenant_id = _seed_tenant(cur, slug="global-pc", is_global=True)
                connection_id = _seed_connection(cur, tenant_id=tenant_id, resource_id="guild-saas-1")
                assert connection_id > 0
        finally:
            conn.close()

    def test_invalid_resource_type_rejected(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                tenant_id = _seed_tenant(cur, slug="acme-pc-badtype", is_global=False)
            with (
                pytest.raises(psycopg2.errors.CheckViolation),
                conn.cursor() as cur,
            ):
                cur.execute(
                    "INSERT INTO platform_connections "
                    "(tenant_id, platform, resource_type, resource_id, access_token) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (tenant_id, "discord", "discord_server", "guild-badtype", "token"),
                )
        finally:
            conn.close()

    def test_invalid_status_rejected(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                tenant_id = _seed_tenant(cur, slug="acme-pc-badstatus", is_global=False)
            with (
                pytest.raises(psycopg2.errors.CheckViolation),
                conn.cursor() as cur,
            ):
                cur.execute(
                    "INSERT INTO platform_connections "
                    "(tenant_id, platform, resource_type, resource_id, access_token, status) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (tenant_id, "discord", "discord_guild", "guild-badstatus", "token", "pending"),
                )
        finally:
            conn.close()

    def test_status_defaults_active(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                tenant_id = _seed_tenant(cur, slug="acme-pc-default", is_global=False)
                connection_id = _seed_connection(
                    cur, tenant_id=tenant_id, resource_id="guild-default"
                )
                cur.execute(
                    "SELECT status FROM platform_connections WHERE id = %s", (connection_id,)
                )
                assert cur.fetchone()[0] == "active"
        finally:
            conn.close()


@requires_docker
class TestCommunityConnectionAccess:
    def test_pending_then_approved_lifecycle(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                tenant_id = _seed_tenant(cur, slug="acme-cca-1", is_global=False)
                connection_id = _seed_connection(cur, tenant_id=tenant_id, resource_id="guild-cca-1")
                community_id = _seed_community(cur, name="bc-cca-1")

                cur.execute(
                    "INSERT INTO community_connection_access (community_id, connection_id) "
                    "VALUES (%s, %s) RETURNING id, status",
                    (community_id, connection_id),
                )
                row_id, status = cur.fetchone()
                assert status == "pending"

                cur.execute(
                    "UPDATE community_connection_access SET status = 'approved' WHERE id = %s "
                    "RETURNING status",
                    (row_id,),
                )
                assert cur.fetchone()[0] == "approved"
        finally:
            conn.close()

    def test_invalid_status_rejected(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                tenant_id = _seed_tenant(cur, slug="acme-cca-badstatus", is_global=False)
                connection_id = _seed_connection(
                    cur, tenant_id=tenant_id, resource_id="guild-cca-badstatus"
                )
                community_id = _seed_community(cur, name="bc-cca-badstatus")
            with (
                pytest.raises(psycopg2.errors.CheckViolation),
                conn.cursor() as cur,
            ):
                cur.execute(
                    "INSERT INTO community_connection_access "
                    "(community_id, connection_id, status) VALUES (%s, %s, %s)",
                    (community_id, connection_id, "blocked"),
                )
        finally:
            conn.close()

    def test_unique_community_connection_pair(self, pg_db: PgTestDatabase) -> None:
        """Reuse: a second community connects via a NEW access row, not a duplicate
        of an existing (community, connection) pair."""
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                tenant_id = _seed_tenant(cur, slug="acme-cca-reuse", is_global=False)
                connection_id = _seed_connection(
                    cur, tenant_id=tenant_id, resource_id="guild-cca-reuse"
                )
                community_id = _seed_community(cur, name="bc-cca-reuse")
                cur.execute(
                    "INSERT INTO community_connection_access (community_id, connection_id) "
                    "VALUES (%s, %s)",
                    (community_id, connection_id),
                )
            with (
                pytest.raises(psycopg2.errors.UniqueViolation),
                conn.cursor() as cur,
            ):
                cur.execute(
                    "INSERT INTO community_connection_access (community_id, connection_id) "
                    "VALUES (%s, %s)",
                    (community_id, connection_id),
                )
        finally:
            conn.close()

    def test_second_community_reuses_same_connection(self, pg_db: PgTestDatabase) -> None:
        """Two different communities MAY both reference the same connection (reuse,
        not re-install) -- each gets its own access row."""
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                tenant_id = _seed_tenant(cur, slug="acme-cca-shared", is_global=False)
                connection_id = _seed_connection(
                    cur, tenant_id=tenant_id, resource_id="guild-cca-shared"
                )
                c1 = _seed_community(cur, name="bc-shared-1")
                c2 = _seed_community(cur, name="bc-shared-2")
                cur.execute(
                    "INSERT INTO community_connection_access (community_id, connection_id, status) "
                    "VALUES (%s, %s, 'approved')",
                    (c1, connection_id),
                )
                cur.execute(
                    "INSERT INTO community_connection_access (community_id, connection_id, status) "
                    "VALUES (%s, %s, 'pending')",
                    (c2, connection_id),
                )
                cur.execute(
                    "SELECT community_id, status FROM community_connection_access "
                    "WHERE connection_id = %s ORDER BY community_id",
                    (connection_id,),
                )
                rows = cur.fetchall()
                assert [r[0] for r in rows] == sorted([c1, c2])
                statuses = {r[0]: r[1] for r in rows}
                assert statuses[c1] == "approved"
                assert statuses[c2] == "pending"
        finally:
            conn.close()

    def test_connection_delete_cascades_to_access(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                tenant_id = _seed_tenant(cur, slug="acme-cca-cascade", is_global=False)
                connection_id = _seed_connection(
                    cur, tenant_id=tenant_id, resource_id="guild-cca-cascade"
                )
                community_id = _seed_community(cur, name="bc-cca-cascade")
                cur.execute(
                    "INSERT INTO community_connection_access (community_id, connection_id) "
                    "VALUES (%s, %s) RETURNING id",
                    (community_id, connection_id),
                )
                access_id = cur.fetchone()[0]

                cur.execute("DELETE FROM platform_connections WHERE id = %s", (connection_id,))

                cur.execute(
                    "SELECT 1 FROM community_connection_access WHERE id = %s", (access_id,)
                )
                assert cur.fetchone() is None
        finally:
            conn.close()


@requires_docker
class TestDowngradeThenUpgradeRoundTrip:
    """Downgrade restores the pre-0035 (0034) schema exactly, then re-upgrades cleanly."""

    def test_downgrade_then_upgrade_round_trip(self) -> None:
        with migrated_postgres("0035-connection-model-roundtrip") as db:
            alembic_cli("downgrade", "0034_bar_citizen_guild_pairing", dsn=db.dsn)

            conn = _connect(db)
            try:
                with conn.cursor() as cur:
                    # new tables gone
                    cur.execute(
                        "SELECT to_regclass('platform_connections'), "
                        "to_regclass('community_connection_access')"
                    )
                    assert cur.fetchone() == (None, None)

                    # old table name restored as a real base table, not a view
                    cur.execute(
                        "SELECT table_type FROM information_schema.tables "
                        "WHERE table_name = 'tenant_platform_credentials'"
                    )
                    assert cur.fetchone()[0] == "BASE TABLE"

                    cur.execute("SELECT to_regclass('tenant_platform_apps')")
                    assert cur.fetchone()[0] is None

                    # original trigger behavior restored
                    tenant_id = _seed_tenant(cur, slug="post-downgrade", is_global=True)
                with (
                    pytest.raises(psycopg2.errors.RaiseException),
                    conn.cursor() as cur,
                ):
                    cur.execute(
                        "INSERT INTO tenant_platform_credentials (tenant_id, platform, "
                        "credentials_ciphertext) VALUES (%s, %s, %s)",
                        (tenant_id, "discord", "should-fail"),
                    )
            finally:
                conn.close()

            alembic_cli("upgrade", "head", dsn=db.dsn)

            conn = _connect(db)
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT to_regclass('tenant_platform_apps')")
                    assert cur.fetchone()[0] is not None
                    cur.execute("SELECT to_regclass('platform_connections')")
                    assert cur.fetchone()[0] is not None
                    cur.execute("SELECT to_regclass('community_connection_access')")
                    assert cur.fetchone()[0] is not None
                    cur.execute(
                        "SELECT table_type FROM information_schema.tables "
                        "WHERE table_name = 'tenant_platform_credentials'"
                    )
                    assert cur.fetchone()[0] == "VIEW"
            finally:
                conn.close()
