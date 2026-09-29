"""Real-Postgres tests for `services/guild_pairing_service.py` (#500/#501).

The exclusivity/active-pairing rules under test are enforced by migration
0038's own partial unique indexes and `BEFORE INSERT/UPDATE` triggers --
DB-level behavior a mocked/sqlite connection can't faithfully reproduce
(same rationale `test_0028_bundle_active_set_changelog.py`/
`test_bundle_active_set_watermark_job.py` give for their own real-Postgres
harnesses). Uses `alembic/tests/pg_docker.py`'s `migrated_postgres()` --
loaded by path since `alembic/` isn't a package `hub_api/` can import
normally (same idiom `test_bundle_active_set_watermark_job.py` uses).
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
from services.guild_pairing_service import (
    PendingRoleRegistration,
    create_binding,
    list_managed_roles,
    list_pairings,
    request_role_registration,
    unbind,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PG_DOCKER_PATH = _REPO_ROOT / "alembic" / "tests" / "pg_docker.py"


def _load_pg_docker() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "waddles_pg_docker_guild_pairing", _PG_DOCKER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["waddles_pg_docker_guild_pairing"] = module
    spec.loader.exec_module(module)
    return module


pg_docker = _load_pg_docker()

requires_docker = pytest.mark.skipif(
    not pg_docker.DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)


@pytest.fixture(scope="module")
def pg_db() -> Iterator[Any]:
    if not pg_docker.DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with pg_docker.migrated_postgres("guild-pairing-service") as db:
        yield db


#: `audit_log` is a pre-existing production table (pydal, `services/schema.
#: py::bind_admin_tables`), not part of the Alembic chain `pg_docker.py`'s
#: minimal bootstrap replays -- created here as its real-column mirror
#: (same columns `hub_api/tests/conftest.py::_create_bundle_install_tables`'s
#: own sqlite mirror uses) so `bundle_audit.record()` and the adopted-role
#: staging path (`guild_pairing_service`'s own docstring) have somewhere to
#: write in this real-Postgres harness.
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


#: `pg_docker.py`'s minimal bootstrap only creates the columns of `tenants`/
#: `communities` that migrations 0020+ reference by FK (`id` only) -- real
#: deployments have `tenants.display_name`/`is_global` and
#: `communities.tenant_id` from the pydal-managed `058_tenants_and_claims.sql`
#: baseline, which this module's service layer (`_validate_community_tenant`,
#: the `reject_global_tenant_credentials` trigger) both assume. Patched in
#: here, locally to this test module, rather than editing the shared
#: `pg_docker.py` harness every other migration test also uses.
_PATCH_TENANT_COLUMNS_SQL = """
ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS display_name TEXT,
    ADD COLUMN IF NOT EXISTS is_global BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE communities ADD COLUMN IF NOT EXISTS tenant_id INTEGER;
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


def _seed_active_pairing(pg_db: Any, *, tenant_id: int, guild_id: str = "111111") -> str:
    with psycopg2.connect(pg_db.dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO guild_tenant_pairings (platform, guild_id, tenant_id, status) "
                "VALUES ('discord', %s, %s, 'active') RETURNING id",
                (guild_id, tenant_id),
            )
            return str(cur.fetchone()[0])


def _seed_pending_pairing(pg_db: Any, *, tenant_id: int, guild_id: str = "222222") -> str:
    with psycopg2.connect(pg_db.dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO guild_tenant_pairings (platform, guild_id, tenant_id, status) "
                "VALUES ('discord', %s, %s, 'pending') RETURNING id",
                (guild_id, tenant_id),
            )
            return str(cur.fetchone()[0])


@requires_docker
class TestListPairings:
    async def test_lists_only_the_callers_tenant(self, pg_db: Any, dal: AsyncDB) -> None:
        tenant_a = _seed_tenant(pg_db, slug="tenant-a")
        tenant_b = _seed_tenant(pg_db, slug="tenant-b")
        _seed_active_pairing(pg_db, tenant_id=tenant_a, guild_id="1")
        _seed_active_pairing(pg_db, tenant_id=tenant_b, guild_id="2")

        rows = await list_pairings(dal, tenant_id=tenant_a)
        assert len(rows) == 1
        assert rows[0].tenant_id == tenant_a


@requires_docker
class TestCreateBindingExclusivity:
    async def test_binding_without_active_pairing_is_rejected(
        self, pg_db: Any, dal: AsyncDB
    ) -> None:
        tenant_id = _seed_tenant(pg_db, slug="t1")
        community_id = _seed_community(pg_db, tenant_id=tenant_id, name="c1")
        pairing_id = _seed_pending_pairing(pg_db, tenant_id=tenant_id)

        with pytest.raises(ApiError) as exc:
            await create_binding(
                dal,
                tenant_id=tenant_id,
                pairing_id=pairing_id,
                community_id=community_id,
                channel_id=None,
                created_by=None,
            )
        assert exc.value.status_code == 422

    async def test_guild_default_binding_exclusive_across_tenants(
        self, pg_db: Any, dal: AsyncDB
    ) -> None:
        tenant_a = _seed_tenant(pg_db, slug="tenant-a2")
        tenant_b = _seed_tenant(pg_db, slug="tenant-b2")
        community_a = _seed_community(pg_db, tenant_id=tenant_a, name="ca")
        community_b = _seed_community(pg_db, tenant_id=tenant_b, name="cb")
        guild_id = "999999"
        pairing_a = _seed_active_pairing(pg_db, tenant_id=tenant_a, guild_id=guild_id)
        pairing_b = _seed_active_pairing(pg_db, tenant_id=tenant_b, guild_id=guild_id)

        await create_binding(
            dal,
            tenant_id=tenant_a,
            pairing_id=pairing_a,
            community_id=community_a,
            channel_id=None,
            created_by=None,
        )

        with pytest.raises(ApiError) as exc:
            await create_binding(
                dal,
                tenant_id=tenant_b,
                pairing_id=pairing_b,
                community_id=community_b,
                channel_id=None,
                created_by=None,
            )
        # Non-leaky: the 409 body never mentions tenant_a or community_a.
        assert exc.value.status_code == 409
        assert "tenant" not in exc.value.message.lower() or "tenant-a2" not in exc.value.message
        assert str(tenant_a) not in exc.value.message
        assert str(community_a) not in exc.value.message

    async def test_channel_binding_exclusive_across_tenants(self, pg_db: Any, dal: AsyncDB) -> None:
        tenant_a = _seed_tenant(pg_db, slug="tenant-a3")
        tenant_b = _seed_tenant(pg_db, slug="tenant-b3")
        community_a = _seed_community(pg_db, tenant_id=tenant_a, name="ca3")
        community_b = _seed_community(pg_db, tenant_id=tenant_b, name="cb3")
        guild_id = "888888"
        pairing_a = _seed_active_pairing(pg_db, tenant_id=tenant_a, guild_id=guild_id)
        pairing_b = _seed_active_pairing(pg_db, tenant_id=tenant_b, guild_id=guild_id)

        await create_binding(
            dal,
            tenant_id=tenant_a,
            pairing_id=pairing_a,
            community_id=community_a,
            channel_id="12345",
            created_by=None,
        )
        with pytest.raises(ApiError) as exc:
            await create_binding(
                dal,
                tenant_id=tenant_b,
                pairing_id=pairing_b,
                community_id=community_b,
                channel_id="12345",
                created_by=None,
            )
        assert exc.value.status_code == 409
        assert str(tenant_a) not in exc.value.message

    async def test_unbind_then_rebind_succeeds(self, pg_db: Any, dal: AsyncDB) -> None:
        tenant_id = _seed_tenant(pg_db, slug="t-unbind")
        community_id = _seed_community(pg_db, tenant_id=tenant_id, name="c-unbind")
        pairing_id = _seed_active_pairing(pg_db, tenant_id=tenant_id, guild_id="333333")

        binding = await create_binding(
            dal,
            tenant_id=tenant_id,
            pairing_id=pairing_id,
            community_id=community_id,
            channel_id=None,
            created_by=None,
        )
        assert await unbind(
            dal,
            tenant_id=tenant_id,
            pairing_id=pairing_id,
            binding_id=str(binding.id),
            actor_id=None,
        )
        # Re-binding the guild default now succeeds -- the revoked row no
        # longer counts against the partial unique index (`status='active'`).
        second = await create_binding(
            dal,
            tenant_id=tenant_id,
            pairing_id=pairing_id,
            community_id=community_id,
            channel_id=None,
            created_by=None,
        )
        assert second.id != binding.id


@requires_docker
class TestRoleRegistration:
    async def test_created_role_is_active_immediately(self, pg_db: Any, dal: AsyncDB) -> None:
        tenant_id = _seed_tenant(pg_db, slug="t-role-created")
        community_id = _seed_community(pg_db, tenant_id=tenant_id, name="c-role")
        pairing_id = _seed_active_pairing(pg_db, tenant_id=tenant_id, guild_id="444444")

        row = await request_role_registration(
            dal,
            tenant_id=tenant_id,
            pairing_id=pairing_id,
            community_id=community_id,
            role_id="555555",
            registered_via="created",
            requested_by=None,
        )
        assert row.status == "active"
        assert row.approved_by_user_id is None

        roles = await list_managed_roles(dal, tenant_id=tenant_id)
        assert len(roles) == 1

    async def test_adopted_role_is_staged_pending_not_inserted(
        self, pg_db: Any, dal: AsyncDB
    ) -> None:
        tenant_id = _seed_tenant(pg_db, slug="t-role-adopted")
        community_id = _seed_community(pg_db, tenant_id=tenant_id, name="c-role-adopted")
        pairing_id = _seed_active_pairing(pg_db, tenant_id=tenant_id, guild_id="666666")

        result = await request_role_registration(
            dal,
            tenant_id=tenant_id,
            pairing_id=pairing_id,
            community_id=community_id,
            role_id="777777",
            registered_via="adopted",
            requested_by=None,
        )
        assert isinstance(result, PendingRoleRegistration)
        assert await list_managed_roles(dal, tenant_id=tenant_id) == []

    async def test_registering_an_already_owned_role_is_a_conflict(
        self, pg_db: Any, dal: AsyncDB
    ) -> None:
        tenant_id = _seed_tenant(pg_db, slug="t-role-conflict")
        community_id = _seed_community(pg_db, tenant_id=tenant_id, name="c-role-conflict")
        pairing_id = _seed_active_pairing(pg_db, tenant_id=tenant_id, guild_id="121212")

        await request_role_registration(
            dal,
            tenant_id=tenant_id,
            pairing_id=pairing_id,
            community_id=community_id,
            role_id="343434",
            registered_via="created",
            requested_by=None,
        )
        with pytest.raises(ApiError) as exc:
            await request_role_registration(
                dal,
                tenant_id=tenant_id,
                pairing_id=pairing_id,
                community_id=community_id,
                role_id="343434",
                registered_via="created",
                requested_by=None,
            )
        assert exc.value.status_code == 409


@requires_docker
class TestCrossTenantInvisibility:
    async def test_binding_to_another_tenants_pairing_is_masked_404(
        self, pg_db: Any, dal: AsyncDB
    ) -> None:
        tenant_a = _seed_tenant(pg_db, slug="tenant-a4")
        tenant_b = _seed_tenant(pg_db, slug="tenant-b4")
        community_b = _seed_community(pg_db, tenant_id=tenant_b, name="cb4")
        pairing_a = _seed_active_pairing(pg_db, tenant_id=tenant_a, guild_id="131313")

        with pytest.raises(ApiError) as exc:
            await create_binding(
                dal,
                tenant_id=tenant_b,
                pairing_id=pairing_a,
                community_id=community_b,
                channel_id=None,
                created_by=None,
            )
        assert exc.value.status_code == 404
        assert str(tenant_a) not in exc.value.message
