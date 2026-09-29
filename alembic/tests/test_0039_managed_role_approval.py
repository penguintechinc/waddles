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


def _seed_tenant_community_pairing(cur: Any, *, slug: str, guild_id: str) -> tuple[int, int, Any]:
    """Insert a tenant + community + active pairing; return `(tenant_id, community_id, pairing_id)`."""
    cur.execute("ALTER TABLE communities ADD COLUMN IF NOT EXISTS tenant_id INTEGER")
    cur.execute("INSERT INTO tenants (slug, is_active) VALUES (%s, TRUE) RETURNING id", (slug,))
    tenant_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO communities (tenant_id, name) VALUES (%s, %s) RETURNING id",
        (tenant_id, f"c-{slug}"),
    )
    community_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO guild_tenant_pairings (platform, guild_id, tenant_id, status) "
        "VALUES ('discord', %s, %s, 'active') RETURNING id",
        (guild_id, tenant_id),
    )
    pairing_id = cur.fetchone()[0]
    return tenant_id, community_id, pairing_id


def _insert_managed_role(
    cur: Any,
    *,
    guild_id: str,
    role_id: str,
    tenant_id: int,
    pairing_id: Any,
    community_id: int,
    approval_status: str,
    status: str,
) -> None:
    approved_by_user_id = None
    if approval_status == "approved":
        # chk_managed_roles_adopted_approval requires an approver for any
        # 'adopted' row once approval_status='approved' -- seed one.
        cur.execute("INSERT INTO hub_users DEFAULT VALUES RETURNING id")
        approved_by_user_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO managed_roles "
        "(platform, guild_id, role_id, tenant_id, pairing_id, owning_community_id, "
        "registered_via, approval_status, approved_by_user_id, status) "
        "VALUES ('discord', %s, %s, %s, %s, %s, 'adopted', %s, %s, %s)",
        (
            guild_id,
            role_id,
            tenant_id,
            pairing_id,
            community_id,
            approval_status,
            approved_by_user_id,
            status,
        ),
    )


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

    def test_old_unconditional_unique_constraint_is_gone(self, pg_db: PgTestDatabase) -> None:
        conn = psycopg2.connect(pg_db.dsn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM pg_constraint WHERE conname = "
                "'managed_roles_platform_guild_id_role_id_key'"
            )
            assert cur.fetchone()[0] == 0
            cur.execute(
                "SELECT COUNT(*) FROM pg_indexes WHERE tablename = 'managed_roles' "
                "AND indexname = 'uq_managed_roles_live_role'"
            )
            assert cur.fetchone()[0] == 1
        conn.close()


@requires_docker
class TestLiveRoleUniqueness:
    """Role ownership is unique only among LIVE rows (`uq_managed_roles_live_role`)."""

    def test_reject_then_reregister_succeeds(self, pg_db: PgTestDatabase) -> None:
        conn = psycopg2.connect(pg_db.dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            tenant_id, community_id, pairing_id = _seed_tenant_community_pairing(
                cur, slug="0039-reject-reregister", guild_id="g-reject-1"
            )
            _insert_managed_role(
                cur,
                guild_id="g-reject-1",
                role_id="r-reject-1",
                tenant_id=tenant_id,
                pairing_id=pairing_id,
                community_id=community_id,
                approval_status="rejected",
                status="pending_approval",
            )
            # Re-registration for the exact same (platform, guild_id, role_id)
            # succeeds -- the rejected row is excluded by the partial index.
            _insert_managed_role(
                cur,
                guild_id="g-reject-1",
                role_id="r-reject-1",
                tenant_id=tenant_id,
                pairing_id=pairing_id,
                community_id=community_id,
                approval_status="pending",
                status="pending_approval",
            )
            cur.execute(
                "SELECT COUNT(*) FROM managed_roles WHERE guild_id = 'g-reject-1' "
                "AND role_id = 'r-reject-1'"
            )
            assert cur.fetchone()[0] == 2
        conn.close()

    def test_removed_role_frees_the_slot(self, pg_db: PgTestDatabase) -> None:
        conn = psycopg2.connect(pg_db.dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            tenant_id, community_id, pairing_id = _seed_tenant_community_pairing(
                cur, slug="0039-removed-reregister", guild_id="g-removed-1"
            )
            _insert_managed_role(
                cur,
                guild_id="g-removed-1",
                role_id="r-removed-1",
                tenant_id=tenant_id,
                pairing_id=pairing_id,
                community_id=community_id,
                approval_status="approved",
                status="removed",
            )
            # approval_status is still 'approved' (never changed on removal --
            # only `status` does), but status='removed' alone frees the slot.
            _insert_managed_role(
                cur,
                guild_id="g-removed-1",
                role_id="r-removed-1",
                tenant_id=tenant_id,
                pairing_id=pairing_id,
                community_id=community_id,
                approval_status="approved",
                status="active",
            )
        conn.close()

    def test_two_live_owners_of_the_same_role_are_still_rejected(
        self, pg_db: PgTestDatabase
    ) -> None:
        conn = psycopg2.connect(pg_db.dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            tenant_id, community_id, pairing_id = _seed_tenant_community_pairing(
                cur, slug="0039-two-live-owners", guild_id="g-live-1"
            )
            _insert_managed_role(
                cur,
                guild_id="g-live-1",
                role_id="r-live-1",
                tenant_id=tenant_id,
                pairing_id=pairing_id,
                community_id=community_id,
                approval_status="approved",
                status="active",
            )
            with pytest.raises(psycopg2.errors.UniqueViolation):
                _insert_managed_role(
                    cur,
                    guild_id="g-live-1",
                    role_id="r-live-1",
                    tenant_id=tenant_id,
                    pairing_id=pairing_id,
                    community_id=community_id,
                    approval_status="pending",
                    status="pending_approval",
                )
        conn.close()

    def test_pending_cleanup_role_still_blocks_reregistration(
        self, pg_db: PgTestDatabase
    ) -> None:
        """`pending_cleanup` (revoke_pairing's cascade marker) is still LIVE -- Discord-side assignment may persist."""
        conn = psycopg2.connect(pg_db.dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            tenant_id, community_id, pairing_id = _seed_tenant_community_pairing(
                cur, slug="0039-pending-cleanup", guild_id="g-cleanup-1"
            )
            _insert_managed_role(
                cur,
                guild_id="g-cleanup-1",
                role_id="r-cleanup-1",
                tenant_id=tenant_id,
                pairing_id=pairing_id,
                community_id=community_id,
                approval_status="approved",
                status="pending_cleanup",
            )
            with pytest.raises(psycopg2.errors.UniqueViolation):
                _insert_managed_role(
                    cur,
                    guild_id="g-cleanup-1",
                    role_id="r-cleanup-1",
                    tenant_id=tenant_id,
                    pairing_id=pairing_id,
                    community_id=community_id,
                    approval_status="pending",
                    status="pending_approval",
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
                cur.execute(
                    "SELECT COUNT(*) FROM pg_constraint WHERE conname = "
                    "'managed_roles_platform_guild_id_role_id_key'"
                )
                assert cur.fetchone()[0] == 1
                cur.execute(
                    "SELECT COUNT(*) FROM pg_indexes WHERE tablename = 'managed_roles' "
                    "AND indexname = 'uq_managed_roles_live_role'"
                )
                assert cur.fetchone()[0] == 0

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
                cur.execute(
                    "SELECT COUNT(*) FROM pg_constraint WHERE conname = "
                    "'managed_roles_platform_guild_id_role_id_key'"
                )
                assert cur.fetchone()[0] == 0
                cur.execute(
                    "SELECT COUNT(*) FROM pg_indexes WHERE tablename = 'managed_roles' "
                    "AND indexname = 'uq_managed_roles_live_role'"
                )
                assert cur.fetchone()[0] == 1

        # Restore true head for any later test in this session-scoped module.
        alembic_cli("upgrade", "head", dsn=pg_db.dsn)
