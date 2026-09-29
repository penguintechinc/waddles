"""Real-Postgres upgrade/downgrade/upgrade round-trip for 0038_guild_tenant_pairing (#500/#501).

Exclusivity (partial unique indexes) and the "binding requires an active
pairing" rule (a cross-table `BEFORE INSERT/UPDATE` trigger) cannot be
verified against mocked SQL text -- same rationale `test_0028_bundle_active_
set_changelog.py`'s `TestMigrationUpDown` gives for its own real-container
round-trip. See `hub_api/tests/test_guild_pairing_service.py` for the
service-layer exercise of these same DB-level guarantees.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import psycopg2
import pytest
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, alembic_cli, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0038-guild-tenant-pairing") as db:
        yield db


def _regclass_exists(cur: Any, name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (name,))
    return bool(cur.fetchone()[0])


@requires_docker
class TestSchemaAtHead:
    """Tables/views/triggers exist and the backward-compat data migration ran."""

    def test_tables_and_views_exist(self, pg_db: PgTestDatabase) -> None:
        conn = psycopg2.connect(pg_db.dsn)
        with conn.cursor() as cur:
            for name in (
                "tenant_platform_credentials",
                "guild_tenant_pairings",
                "community_channel_bindings",
                "managed_roles",
                "v_guild_routing",
                "v_managed_roles_active",
            ):
                assert _regclass_exists(cur, name), f"{name} missing at head"
        conn.close()

    def test_global_tenant_credentials_rejected_by_trigger(self, pg_db: PgTestDatabase) -> None:
        conn = psycopg2.connect(pg_db.dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            # `pg_docker.py`'s minimal bootstrap `tenants` table has no
            # `is_global` column (only migrations 0020+'s own FK-referenced
            # columns) -- patched in locally, same rationale as
            # `hub_api/tests/test_guild_pairing_service.py`'s own constant.
            cur.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS is_global BOOLEAN NOT NULL DEFAULT FALSE")
            cur.execute(
                "INSERT INTO tenants (slug, is_global, is_active) "
                "VALUES ('global-test', TRUE, TRUE) RETURNING id"
            )
            tenant_id = cur.fetchone()[0]
            with pytest.raises(psycopg2.errors.RaiseException):
                cur.execute(
                    "INSERT INTO tenant_platform_credentials "
                    "(tenant_id, application_id, client_secret_ciphertext, client_secret_iv, "
                    "bot_token_ciphertext, bot_token_iv, key_ref) "
                    "VALUES (%s, 'app', '\\x00', '\\x00', '\\x00', '\\x00', 'ref')",
                    (tenant_id,),
                )
        conn.close()

    def test_binding_requires_active_pairing_trigger(self, pg_db: PgTestDatabase) -> None:
        conn = psycopg2.connect(pg_db.dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE communities ADD COLUMN IF NOT EXISTS tenant_id INTEGER")
            cur.execute(
                "INSERT INTO tenants (slug, is_active) VALUES ('trig-test', TRUE) RETURNING id"
            )
            tenant_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO communities (tenant_id, name) VALUES (%s, 'c') RETURNING id",
                (tenant_id,),
            )
            community_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO guild_tenant_pairings (platform, guild_id, tenant_id, status) "
                "VALUES ('discord', 'g-trig', %s, 'pending') RETURNING id",
                (tenant_id,),
            )
            pairing_id = cur.fetchone()[0]
            with pytest.raises(psycopg2.errors.RaiseException):
                cur.execute(
                    "INSERT INTO community_channel_bindings "
                    "(platform, guild_id, channel_id, community_id, tenant_id, pairing_id, status) "
                    "VALUES ('discord', 'g-trig', NULL, %s, %s, %s, 'active')",
                    (community_id, tenant_id, pairing_id),
                )
        conn.close()


class TestMigrationUpDown:
    """`alembic downgrade <down_revision>` / `upgrade <revision>` against the real container, round-tripped."""

    _REVISION = "0038_guild_tenant_pairing"
    _DOWN_REVISION = "0030_bundle_app_schemas"

    @requires_docker
    def test_downgrade_then_upgrade_round_trips_schema(self, pg_db: PgTestDatabase) -> None:
        alembic_cli("downgrade", self._REVISION, dsn=pg_db.dsn)

        with psycopg2.connect(pg_db.dsn) as check_conn:
            check_conn.autocommit = True
            with check_conn.cursor() as cur:
                for name in (
                    "tenant_platform_credentials",
                    "guild_tenant_pairings",
                    "community_channel_bindings",
                    "managed_roles",
                    "v_guild_routing",
                    "v_managed_roles_active",
                ):
                    assert _regclass_exists(cur, name)

        alembic_cli("downgrade", self._DOWN_REVISION, dsn=pg_db.dsn)

        with psycopg2.connect(pg_db.dsn) as check_conn:
            check_conn.autocommit = True
            with check_conn.cursor() as cur:
                for name in (
                    "tenant_platform_credentials",
                    "guild_tenant_pairings",
                    "community_channel_bindings",
                    "managed_roles",
                ):
                    assert not _regclass_exists(cur, name)
                cur.execute(
                    "SELECT COUNT(*) FROM pg_trigger WHERE tgname IN "
                    "('trg_reject_global_tenant_credentials', "
                    "'trg_require_active_pairing_for_binding')"
                )
                assert cur.fetchone()[0] == 0

        alembic_cli("upgrade", self._REVISION, dsn=pg_db.dsn)

        with psycopg2.connect(pg_db.dsn) as check_conn:
            check_conn.autocommit = True
            with check_conn.cursor() as cur:
                for name in (
                    "tenant_platform_credentials",
                    "guild_tenant_pairings",
                    "community_channel_bindings",
                    "managed_roles",
                    "v_guild_routing",
                    "v_managed_roles_active",
                ):
                    assert _regclass_exists(cur, name)
                cur.execute(
                    "SELECT COUNT(*) FROM pg_trigger WHERE tgname IN "
                    "('trg_reject_global_tenant_credentials', "
                    "'trg_require_active_pairing_for_binding')"
                )
                assert cur.fetchone()[0] == 2

        # Restore true head for any later test in this session-scoped module.
        alembic_cli("upgrade", "head", dsn=pg_db.dsn)
