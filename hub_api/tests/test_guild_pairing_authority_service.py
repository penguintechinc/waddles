"""Real-Postgres tests for `services/guild_pairing_authority_service.py` (#500/#501).

Same real-Postgres harness rationale as `test_guild_pairing_service.py`.
`FakeVerifier` stands in for the OAuth agent's real `GuildAuthorityVerifier`
implementation (deliberately not implemented here -- module docstring).
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import psycopg2
import pytest
from penguin_dal import AsyncDB

from services.bundle_install_dal import build_install_dal
from services.errors import ApiError
from services.guild_pairing_authority_service import (
    approve_adopted_role,
    guild_overview,
    reject_adopted_role,
    revoke_pairing,
)
from services.guild_pairing_service import create_binding, request_role_registration

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PG_DOCKER_PATH = _REPO_ROOT / "alembic" / "tests" / "pg_docker.py"


def _load_pg_docker() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "waddles_pg_docker_guild_authority", _PG_DOCKER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["waddles_pg_docker_guild_authority"] = module
    spec.loader.exec_module(module)
    return module


pg_docker = _load_pg_docker()

requires_docker = pytest.mark.skipif(
    not pg_docker.DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)


class FakeVerifier:
    """Test double for `GuildAuthorityVerifier` -- returns a fixed, configurable answer."""

    def __init__(self, *, allow: bool) -> None:
        """Fix the answer every `verify()` call returns; record every call's arguments."""
        self.allow = allow
        self.calls: list[tuple[str, str, int]] = []

    async def verify(self, *, platform: str, guild_id: str, hub_user_id: int) -> bool:
        self.calls.append((platform, guild_id, hub_user_id))
        return self.allow


@pytest.fixture(scope="module")
def pg_db() -> Iterator[Any]:
    if not pg_docker.DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with pg_docker.migrated_postgres("guild-pairing-authority") as db:
        yield db


#: See `test_guild_pairing_service.py`'s own identical constants' docstrings.
_PATCH_TENANT_COLUMNS_SQL = """
ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS display_name TEXT,
    ADD COLUMN IF NOT EXISTS is_global BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE communities ADD COLUMN IF NOT EXISTS tenant_id INTEGER;
"""

_CREATE_AUDIT_LOG_SQL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER,
    action VARCHAR(100) NOT NULL,
    target_type VARCHAR(50),
    target_id VARCHAR(255),
    details JSONB,
    ip_address VARCHAR(45),
    user_agent TEXT,
    created_at TIMESTAMPTZ
)
"""


@pytest.fixture
async def dal(pg_db: Any) -> AsyncIterator[AsyncDB]:
    """Fresh `install_dal`-equivalent `AsyncDB`, truncated to a clean slate per test."""
    with psycopg2.connect(pg_db.dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(_CREATE_AUDIT_LOG_SQL)
            cur.execute(_PATCH_TENANT_COLUMNS_SQL)

    pydal_style_dsn = pg_db.dsn.replace("postgresql://", "postgres://")
    install_dal = await build_install_dal(pydal_style_dsn, pool_size=2)
    with psycopg2.connect(pg_db.dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE managed_roles, community_channel_bindings, "
                "guild_tenant_pairings, communities, tenants, audit_log, hub_users "
                "RESTART IDENTITY CASCADE"
            )
    yield install_dal
    await install_dal.close()


def _seed_tenant(pg_db: Any, *, slug: str) -> int:
    with psycopg2.connect(pg_db.dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tenants (slug, display_name, is_global, is_active) "
                "VALUES (%s, %s, FALSE, TRUE) RETURNING id",
                (slug, slug),
            )
            return int(cur.fetchone()[0])


def _seed_community(pg_db: Any, *, tenant_id: int, name: str) -> int:
    with psycopg2.connect(pg_db.dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO communities (tenant_id, name) VALUES (%s, %s) RETURNING id",
                (tenant_id, name),
            )
            return int(cur.fetchone()[0])


def _seed_hub_user(pg_db: Any, *, user_id: int) -> None:
    """Insert a `hub_users` row with an explicit id.

    `managed_roles.approved_by_user_id`/`guild_tenant_pairings.revoked_by_user_id`
    are real FKs to it.
    """
    with psycopg2.connect(pg_db.dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("INSERT INTO hub_users (id) VALUES (%s)", (user_id,))


def _seed_active_pairing(pg_db: Any, *, tenant_id: int, guild_id: str) -> str:
    with psycopg2.connect(pg_db.dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO guild_tenant_pairings (platform, guild_id, tenant_id, status) "
                "VALUES ('discord', %s, %s, 'active') RETURNING id",
                (guild_id, tenant_id),
            )
            return str(cur.fetchone()[0])


@requires_docker
class TestRevocationCascade:
    async def test_revoke_deactivates_bindings_and_marks_roles_pending_cleanup(
        self, pg_db: Any, dal: AsyncDB
    ) -> None:
        tenant_id = _seed_tenant(pg_db, slug="t-revoke")
        community_id = _seed_community(pg_db, tenant_id=tenant_id, name="c-revoke")
        guild_id = "555000"
        pairing_id = _seed_active_pairing(pg_db, tenant_id=tenant_id, guild_id=guild_id)
        _seed_hub_user(pg_db, user_id=1)

        binding = await create_binding(
            dal,
            tenant_id=tenant_id,
            pairing_id=pairing_id,
            community_id=community_id,
            channel_id=None,
            created_by=None,
        )
        role = await request_role_registration(
            dal,
            tenant_id=tenant_id,
            pairing_id=pairing_id,
            community_id=community_id,
            role_id="909090",
            registered_via="created",
            requested_by=None,
        )

        verifier = FakeVerifier(allow=True)
        revoked = await revoke_pairing(dal, verifier, pairing_id=pairing_id, revoker_hub_user_id=1)
        assert revoked.status == "revoked"
        assert verifier.calls == [("discord", guild_id, 1)]

        binding_row = (await dal(dal.community_channel_bindings.id == binding.id).select()).first()
        assert binding_row.status == "revoked"

        role_row = (await dal(dal.managed_roles.id == role.id).select()).first()
        assert role_row.status == "pending_cleanup"

    async def test_revoke_denied_without_guild_authority(self, pg_db: Any, dal: AsyncDB) -> None:
        tenant_id = _seed_tenant(pg_db, slug="t-revoke-denied")
        pairing_id = _seed_active_pairing(pg_db, tenant_id=tenant_id, guild_id="556000")
        verifier = FakeVerifier(allow=False)
        with pytest.raises(ApiError) as exc:
            await revoke_pairing(dal, verifier, pairing_id=pairing_id, revoker_hub_user_id=1)
        assert exc.value.status_code == 403

    async def test_revoke_fails_closed_when_verifier_unconfigured(
        self, pg_db: Any, dal: AsyncDB
    ) -> None:
        tenant_id = _seed_tenant(pg_db, slug="t-revoke-unconfigured")
        pairing_id = _seed_active_pairing(pg_db, tenant_id=tenant_id, guild_id="557000")
        with pytest.raises(ApiError) as exc:
            await revoke_pairing(dal, None, pairing_id=pairing_id, revoker_hub_user_id=1)
        assert exc.value.status_code == 503


@requires_docker
class TestAdoptedRoleApproval:
    async def test_approve_inserts_managed_role_with_approver(
        self, pg_db: Any, dal: AsyncDB
    ) -> None:
        tenant_id = _seed_tenant(pg_db, slug="t-adopt-approve")
        community_id = _seed_community(pg_db, tenant_id=tenant_id, name="c-adopt")
        guild_id = "444555"
        pairing_id = _seed_active_pairing(pg_db, tenant_id=tenant_id, guild_id=guild_id)
        _seed_hub_user(pg_db, user_id=42)

        pending = await request_role_registration(
            dal,
            tenant_id=tenant_id,
            pairing_id=pairing_id,
            community_id=community_id,
            role_id="777888",
            registered_via="adopted",
            requested_by=None,
        )
        assert pending.approval_status == "pending"

        verifier = FakeVerifier(allow=True)
        row = await approve_adopted_role(
            dal, verifier, managed_role_id=pending.id, approver_hub_user_id=42
        )
        assert row.status == "active"
        assert row.approval_status == "approved"
        assert row.approved_by_user_id == 42
        assert row.registered_via == "adopted"
        assert verifier.calls == [("discord", guild_id, 42)]

    async def test_approve_denied_without_guild_authority(self, pg_db: Any, dal: AsyncDB) -> None:
        tenant_id = _seed_tenant(pg_db, slug="t-adopt-denied")
        community_id = _seed_community(pg_db, tenant_id=tenant_id, name="c-adopt-denied")
        pairing_id = _seed_active_pairing(pg_db, tenant_id=tenant_id, guild_id="444666")

        pending = await request_role_registration(
            dal,
            tenant_id=tenant_id,
            pairing_id=pairing_id,
            community_id=community_id,
            role_id="777999",
            registered_via="adopted",
            requested_by=None,
        )
        verifier = FakeVerifier(allow=False)
        with pytest.raises(ApiError) as exc:
            await approve_adopted_role(
                dal, verifier, managed_role_id=pending.id, approver_hub_user_id=42
            )
        assert exc.value.status_code == 403

    async def test_approve_unknown_request_is_404(self, pg_db: Any, dal: AsyncDB) -> None:
        verifier = FakeVerifier(allow=True)
        with pytest.raises(ApiError) as exc:
            await approve_adopted_role(
                dal,
                verifier,
                managed_role_id="00000000-0000-0000-0000-000000000000",
                approver_hub_user_id=1,
            )
        assert exc.value.status_code == 404

    async def test_approve_already_approved_row_is_rejected(self, pg_db: Any, dal: AsyncDB) -> None:
        """A `created` row (`approval_status='approved'` immediately) is not an adoption request."""
        tenant_id = _seed_tenant(pg_db, slug="t-adopt-already-approved")
        community_id = _seed_community(pg_db, tenant_id=tenant_id, name="c-already-approved")
        pairing_id = _seed_active_pairing(pg_db, tenant_id=tenant_id, guild_id="444777")

        created = await request_role_registration(
            dal,
            tenant_id=tenant_id,
            pairing_id=pairing_id,
            community_id=community_id,
            role_id="778000",
            registered_via="created",
            requested_by=None,
        )
        verifier = FakeVerifier(allow=True)
        with pytest.raises(ApiError) as exc:
            await approve_adopted_role(
                dal, verifier, managed_role_id=created.id, approver_hub_user_id=1
            )
        assert exc.value.status_code == 422


@requires_docker
class TestRejectAdoptedRole:
    async def test_reject_sets_approval_status_rejected(self, pg_db: Any, dal: AsyncDB) -> None:
        tenant_id = _seed_tenant(pg_db, slug="t-reject")
        community_id = _seed_community(pg_db, tenant_id=tenant_id, name="c-reject")
        guild_id = "800000"
        pairing_id = _seed_active_pairing(pg_db, tenant_id=tenant_id, guild_id=guild_id)

        pending = await request_role_registration(
            dal,
            tenant_id=tenant_id,
            pairing_id=pairing_id,
            community_id=community_id,
            role_id="800001",
            registered_via="adopted",
            requested_by=None,
        )
        verifier = FakeVerifier(allow=True)
        row = await reject_adopted_role(
            dal, verifier, managed_role_id=pending.id, rejecter_hub_user_id=42
        )
        assert row.approval_status == "rejected"
        assert row.approved_by_user_id is None
        assert verifier.calls == [("discord", guild_id, 42)]

    async def test_reject_denied_without_guild_authority(self, pg_db: Any, dal: AsyncDB) -> None:
        tenant_id = _seed_tenant(pg_db, slug="t-reject-denied")
        community_id = _seed_community(pg_db, tenant_id=tenant_id, name="c-reject-denied")
        pairing_id = _seed_active_pairing(pg_db, tenant_id=tenant_id, guild_id="800100")

        pending = await request_role_registration(
            dal,
            tenant_id=tenant_id,
            pairing_id=pairing_id,
            community_id=community_id,
            role_id="800101",
            registered_via="adopted",
            requested_by=None,
        )
        verifier = FakeVerifier(allow=False)
        with pytest.raises(ApiError) as exc:
            await reject_adopted_role(
                dal, verifier, managed_role_id=pending.id, rejecter_hub_user_id=42
            )
        assert exc.value.status_code == 403

    async def test_reject_unknown_request_is_404(self, pg_db: Any, dal: AsyncDB) -> None:
        verifier = FakeVerifier(allow=True)
        with pytest.raises(ApiError) as exc:
            await reject_adopted_role(
                dal,
                verifier,
                managed_role_id="00000000-0000-0000-0000-000000000000",
                rejecter_hub_user_id=1,
            )
        assert exc.value.status_code == 404

    async def test_reject_already_approved_row_is_rejected(self, pg_db: Any, dal: AsyncDB) -> None:
        tenant_id = _seed_tenant(pg_db, slug="t-reject-already-approved")
        community_id = _seed_community(pg_db, tenant_id=tenant_id, name="c-reject-approved")
        pairing_id = _seed_active_pairing(pg_db, tenant_id=tenant_id, guild_id="800200")

        created = await request_role_registration(
            dal,
            tenant_id=tenant_id,
            pairing_id=pairing_id,
            community_id=community_id,
            role_id="800201",
            registered_via="created",
            requested_by=None,
        )
        verifier = FakeVerifier(allow=True)
        with pytest.raises(ApiError) as exc:
            await reject_adopted_role(
                dal, verifier, managed_role_id=created.id, rejecter_hub_user_id=1
            )
        assert exc.value.status_code == 422

    async def test_reject_then_reregister_succeeds(self, pg_db: Any, dal: AsyncDB) -> None:
        """Migration 0039's partial unique index -- rejecting must not block re-registration."""
        tenant_id = _seed_tenant(pg_db, slug="t-reject-reregister")
        community_id = _seed_community(pg_db, tenant_id=tenant_id, name="c-reject-reregister")
        guild_id = "800300"
        pairing_id = _seed_active_pairing(pg_db, tenant_id=tenant_id, guild_id=guild_id)

        pending = await request_role_registration(
            dal,
            tenant_id=tenant_id,
            pairing_id=pairing_id,
            community_id=community_id,
            role_id="800301",
            registered_via="adopted",
            requested_by=None,
        )
        verifier = FakeVerifier(allow=True)
        await reject_adopted_role(dal, verifier, managed_role_id=pending.id, rejecter_hub_user_id=1)

        # Re-registration for the exact same role succeeds -- 0038's own
        # request_role_registration pre-check + the DB's own partial unique
        # index both agree the rejected row no longer counts as owned/pending.
        second = await request_role_registration(
            dal,
            tenant_id=tenant_id,
            pairing_id=pairing_id,
            community_id=community_id,
            role_id="800301",
            registered_via="adopted",
            requested_by=None,
        )
        assert second.id != pending.id
        assert second.approval_status == "pending"

    async def test_two_live_owners_of_the_same_role_still_conflict(
        self, pg_db: Any, dal: AsyncDB
    ) -> None:
        tenant_id = _seed_tenant(pg_db, slug="t-two-live-owners")
        community_id = _seed_community(pg_db, tenant_id=tenant_id, name="c-two-live")
        guild_id = "800400"
        pairing_id = _seed_active_pairing(pg_db, tenant_id=tenant_id, guild_id=guild_id)

        await request_role_registration(
            dal,
            tenant_id=tenant_id,
            pairing_id=pairing_id,
            community_id=community_id,
            role_id="800401",
            registered_via="created",
            requested_by=None,
        )
        with pytest.raises(ApiError) as exc:
            await request_role_registration(
                dal,
                tenant_id=tenant_id,
                pairing_id=pairing_id,
                community_id=community_id,
                role_id="800401",
                registered_via="adopted",
                requested_by=None,
            )
        assert exc.value.status_code == 409


@requires_docker
class TestGuildOverview:
    async def test_overview_lists_tenants_and_roles_no_member_data(
        self, pg_db: Any, dal: AsyncDB
    ) -> None:
        tenant_a = _seed_tenant(pg_db, slug="t-overview-a")
        tenant_b = _seed_tenant(pg_db, slug="t-overview-b")
        community_a = _seed_community(pg_db, tenant_id=tenant_a, name="ca-overview")
        community_b = _seed_community(pg_db, tenant_id=tenant_b, name="cb-overview")
        guild_id = "1000001"
        pairing_a = _seed_active_pairing(pg_db, tenant_id=tenant_a, guild_id=guild_id)
        pairing_b = _seed_active_pairing(pg_db, tenant_id=tenant_b, guild_id=guild_id)

        await create_binding(
            dal,
            tenant_id=tenant_a,
            pairing_id=pairing_a,
            community_id=community_a,
            channel_id="12121212",
            created_by=None,
        )
        await request_role_registration(
            dal,
            tenant_id=tenant_b,
            pairing_id=pairing_b,
            community_id=community_b,
            role_id="13131313",
            registered_via="created",
            requested_by=None,
        )

        verifier = FakeVerifier(allow=True)
        entries = await guild_overview(
            dal, verifier, platform="discord", guild_id=guild_id, requester_hub_user_id=1
        )
        assert {e.tenantId for e in entries} == {tenant_a, tenant_b}
        by_tenant = {e.tenantId: e for e in entries}
        assert by_tenant[tenant_a].boundCommunityIds == [community_a]
        assert by_tenant[tenant_b].ownedRoleIds == ["13131313"]
        # NO member data anywhere in the DTO (dataclass field set is exhaustive).
        for entry in entries:
            assert not hasattr(entry, "members")
