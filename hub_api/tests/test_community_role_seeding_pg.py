"""Community create / global auto-join on REAL Postgres: roles seeded, members linked, scopes OK.

Runs the production write paths (`free_tier_caps.create_community_capped`,
`auth_service.add_user_to_global_community`) against a migrated Postgres container that carries
the REAL `seed_community_system_roles()` function and role-kind triggers (alembic 0002/0117/0118,
installed by `tests/demo_pg_support.py`), then asks the REAL authz resolvers
(`community_authz.get_caller_community_role`, `community_authz.member_scopes_sync`) what the
member holds. Regression for: community create never seeded roles nor linked the creator, so the
creator resolved no scopes.

Skipped (never failed) where the `docker` CLI is unavailable, like every other PG test here.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from flask_core.database import AsyncDAL

from services import auth_service, community_authz, free_tier_caps, member_roles
from services.scope_registry import KIND_ADMIN, effective_scopes
from tests import demo_pg_support as pg

pytestmark = pytest.mark.skipif(
    not pg.DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

CREATOR = 4101
JOINER = 4102


@pytest.fixture(scope="module")
def pg_db() -> Iterator[pg.PgTestDatabase]:
    """One migrated Postgres 17 container (+ real role seeding objects) for the module."""
    if not pg.DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with pg.migrated_postgres("hub-api-role-seeding") as db:
        pg.prepare_database(db)
        yield db


@pytest.fixture
def adal(pg_db: pg.PgTestDatabase) -> Iterator[AsyncDAL]:
    """Per-test truncated, bound pydal `AsyncDAL` on the real container."""
    yield from pg.fresh_dal(pg_db)


@pytest.fixture(autouse=True)
def _caps_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier caps are out of scope here (covered by test_free_tier_caps); never block the insert."""

    async def _no_caps(_tenant_slug: str) -> bool:
        return False

    monkeypatch.setattr(free_tier_caps, "_caps_apply", _no_caps)


def _tenant(dal: Any) -> int:
    return int(
        pg.scalar(
            dal,
            "INSERT INTO tenants (slug, display_name, is_global, is_active) "
            "VALUES ('acme', 'Acme', false, true) RETURNING id",
        )
    )


async def _create(adal: AsyncDAL, *, name: str = "penguin-pals") -> tuple[int, int]:
    dal = adal.dal
    tenant_id = _tenant(dal)
    now = datetime.now(UTC)
    community_id = await free_tier_caps.create_community_capped(
        adal,
        dal,
        tenant_slug="acme",
        tenant_id=tenant_id,
        values=dict(
            name=name,
            display_name=name,
            platform="discord",
            tenant_id=tenant_id,
            created_by=str(CREATOR),
            created_at=now,
            updated_at=now,
        ),
        creator_id=CREATOR,
    )
    return community_id, tenant_id


def _role(dal: Any, community_id: int, name: str) -> Any:
    rows = pg.q(
        dal,
        "SELECT id, role_kind, base_claims FROM community_roles "
        "WHERE community_id = %s AND name = %s",
        [community_id, name],
    )
    assert rows, f"role {name!r} not seeded"
    return rows[0]


class TestCreateSeedsAndLinksCreator:
    async def test_creator_holds_owner_role_and_admin_scopes_via_real_authz(
        self, adal: AsyncDAL
    ) -> None:
        dal = adal.dal
        community_id, _ = await _create(adal)

        seeded = {
            r[0]
            for r in pg.q(
                dal,
                "SELECT name FROM community_roles WHERE community_id = %s AND is_system",
                [community_id],
            )
        }
        assert {"member", "moderator", "community-admin", "community-owner"} <= seeded, seeded
        owner_id, owner_kind, _ = _role(dal, community_id, member_roles.COMMUNITY_OWNER_ROLE)
        assert owner_kind == KIND_ADMIN  # set by the 0117 trigger, never by app code

        member = pg.q(
            dal,
            "SELECT role, community_role_id, is_active FROM community_members "
            "WHERE community_id = %s AND user_id = %s",
            [community_id, str(CREATOR)],
        )
        assert member == [(member_roles.COMMUNITY_OWNER_ROLE, owner_id, True)]

        full = effective_scopes(KIND_ADMIN, None)
        caller = await community_authz.get_caller_community_role(
            adal, dal, community_id=community_id, user_id=CREATOR
        )
        assert caller is not None
        assert caller.scopes == full
        assert community_authz.member_scopes_sync(dal, community_id, CREATOR) == full

    async def test_owner_id_is_not_written(self, adal: AsyncDAL) -> None:
        community_id, _ = await _create(adal)
        assert (
            pg.scalar(adal.dal, "SELECT owner_id FROM communities WHERE id = %s", [community_id])
            is None
        )

    async def test_seed_failure_rolls_back_the_whole_create(
        self, adal: AsyncDAL, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_dal: Any, _community_id: int) -> None:
            raise RuntimeError("seed failed")

        monkeypatch.setattr(member_roles, "seed_community_system_roles", boom)
        with pytest.raises(RuntimeError, match="seed failed"):
            await _create(adal, name="rolled-back")
        assert (
            pg.scalar(adal.dal, "SELECT count(*) FROM communities WHERE name = 'rolled-back'") == 0
        )
        assert pg.scalar(adal.dal, "SELECT count(*) FROM community_members") == 0


class TestGlobalAutoJoin:
    async def _global_community(self, adal: AsyncDAL) -> int:
        community_id, _ = await _create(adal, name="global")
        pg.q(
            adal.dal,
            'UPDATE communities SET config = \'{"is_global": "true"}\'::jsonb, is_active = true '
            "WHERE id = %s",
            [community_id],
        )
        return community_id

    async def test_auto_join_links_member_role_and_resolves_member_scopes(
        self, adal: AsyncDAL
    ) -> None:
        dal = adal.dal
        community_id = await self._global_community(adal)

        await auth_service.add_user_to_global_community(adal, dal, user_id=JOINER)

        member_id, _, base_claims = _role(dal, community_id, member_roles.COMMUNITY_MEMBER_ROLE)
        assert pg.q(
            dal,
            "SELECT role, community_role_id FROM community_members "
            "WHERE community_id = %s AND user_id = %s",
            [community_id, str(JOINER)],
        ) == [(member_roles.COMMUNITY_MEMBER_ROLE, member_id)]
        want = frozenset(base_claims["scopes"])
        assert "community:read" in want
        caller = await community_authz.get_caller_community_role(
            adal, dal, community_id=community_id, user_id=JOINER
        )
        assert caller is not None and caller.scopes == want
        assert community_authz.member_scopes_sync(dal, community_id, JOINER) == want
        assert not want >= effective_scopes(KIND_ADMIN, None)
        assert (
            pg.scalar(dal, "SELECT member_count FROM communities WHERE id = %s", [community_id])
            == 2
        )

    async def test_auto_join_never_demotes_an_existing_member(self, adal: AsyncDAL) -> None:
        dal = adal.dal
        community_id = await self._global_community(adal)

        await auth_service.add_user_to_global_community(adal, dal, user_id=CREATOR)

        owner_id, _, _ = _role(dal, community_id, member_roles.COMMUNITY_OWNER_ROLE)
        assert pg.q(
            dal,
            "SELECT role, community_role_id FROM community_members "
            "WHERE community_id = %s AND user_id = %s",
            [community_id, str(CREATOR)],
        ) == [(member_roles.COMMUNITY_OWNER_ROLE, owner_id)]
