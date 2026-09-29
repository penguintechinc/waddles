"""Real-Postgres upgrade/downgrade/upgrade round-trip for 0039_managed_role_approval (#500/#501).

See `hub_api/services/guild_pairing_service.py`'s module docstring for why
this migration exists: an adopted-role registration is a real, pending
`managed_roles` row, not evidence staged in `audit_log`. Same real-Postgres
rationale as `test_0038_guild_tenant_pairing.py`.
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
    with migrated_postgres("0039-managed-role-approval") as db:
        yield db


def _column_exists(cur: Any, table: str, column: str) -> bool:
    cur.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = %s)",
        (table, column),
    )
    return bool(cur.fetchone()[0])


@requires_docker
class TestSchemaAtHead:
    def test_approval_status_column_exists_with_approved_default(
        self, pg_db: PgTestDatabase
    ) -> None:
        conn = psycopg2.connect(pg_db.dsn)
        with conn.cursor() as cur:
            assert _column_exists(cur, "managed_roles", "approval_status")
            cur.execute(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_name = 'managed_roles' AND column_name = 'approval_status'"
            )
            assert "approved" in cur.fetchone()[0]
        conn.close()

    def test_pending_adopted_row_allowed_without_approver(
        self, pg_db: PgTestDatabase
    ) -> None:
        """The whole point of this migration: an adopted row with no approver is now legal."""
        conn = psycopg2.connect(pg_db.dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE communities ADD COLUMN IF NOT EXISTS tenant_id INTEGER"
            )
            cur.execute(
                "INSERT INTO tenants (slug, is_active) VALUES ('0039-test', TRUE) RETURNING id"
            )
            tenant_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO communities (tenant_id, name) VALUES (%s, 'c') RETURNING id",
                (tenant_id,),
            )
            community_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO guild_tenant_pairings (platform, guild_id, tenant_id, status) "
                "VALUES ('discord', 'g-0039', %s, 'active') RETURNING id",
                (tenant_id,),
            )
            pairing_id = cur.fetchone()[0]
            # No approved_by_user_id -- would have violated the pre-0039
            # chk_managed_roles_adopted_approval unconditionally.
            cur.execute(
                "INSERT INTO managed_roles "
                "(platform, guild_id, role_id, tenant_id, pairing_id, owning_community_id, "
                "registered_via, approval_status, status) "
                "VALUES ('discord', 'g-0039', 'r-0039', %s, %s, %s, 'adopted', 'pending', "
                "'pending_approval')",
                (tenant_id, pairing_id, community_id),
            )
        conn.close()

    def test_approved_adopted_row_without_approver_still_rejected(
        self, pg_db: PgTestDatabase
    ) -> None:
        """approval_status='approved' still requires approved_by_user_id for an adopted row."""
        conn = psycopg2.connect(pg_db.dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE communities ADD COLUMN IF NOT EXISTS tenant_id INTEGER"
            )
            cur.execute(
                "INSERT INTO tenants (slug, is_active) VALUES ('0039-test-2', TRUE) RETURNING id"
            )
            tenant_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO communities (tenant_id, name) VALUES (%s, 'c2') RETURNING id",
                (tenant_id,),
            )
            community_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO guild_tenant_pairings (platform, guild_id, tenant_id, status) "
                "VALUES ('discord', 'g-0039b', %s, 'active') RETURNING id",
                (tenant_id,),
            )
            pairing_id = cur.fetchone()[0]
            with pytest.raises(psycopg2.errors.CheckViolation):
                cur.execute(
                    "INSERT INTO managed_roles "
                    "(platform, guild_id, role_id, tenant_id, pairing_id, owning_community_id, "
                    "registered_via, approval_status, status) "
                    "VALUES ('discord', 'g-0039b', 'r-0039b', %s, %s, %s, 'adopted', "
                    "'approved', 'active')",
                    (tenant_id, pairing_id, community_id),
                )
        conn.close()


class TestMigrationUpDown:
    """`alembic downgrade <down_revision>` / `upgrade <revision>` round-tripped."""

    _REVISION = "0039_managed_role_approval"
    _DOWN_REVISION = "0038_guild_tenant_pairing"

    @requires_docker
    def test_downgrade_then_upgrade_round_trips_schema(
        self, pg_db: PgTestDatabase
    ) -> None:
        # `TestSchemaAtHead` above leaves a pending (no approver) adopted row --
        # legal under 0039's relaxed CHECK but not under the pre-0039 one the
        # downgrade restores; clear it so re-adding that CHECK can validate.
        with psycopg2.connect(pg_db.dsn) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("DELETE FROM managed_roles")

        alembic_cli("downgrade", self._DOWN_REVISION, dsn=pg_db.dsn)

        with psycopg2.connect(pg_db.dsn) as check_conn:
            check_conn.autocommit = True
            with check_conn.cursor() as cur:
                assert not _column_exists(cur, "managed_roles", "approval_status")

        alembic_cli("upgrade", self._REVISION, dsn=pg_db.dsn)

        with psycopg2.connect(pg_db.dsn) as check_conn:
            check_conn.autocommit = True
            with check_conn.cursor() as cur:
                assert _column_exists(cur, "managed_roles", "approval_status")
                cur.execute(
                    "SELECT COUNT(*) FROM pg_constraint WHERE conname = "
                    "'chk_managed_roles_approval_status'"
                )
                assert cur.fetchone()[0] == 1

        # Restore true head for any later test in this session-scoped module.
        alembic_cli("upgrade", "head", dsn=pg_db.dsn)
