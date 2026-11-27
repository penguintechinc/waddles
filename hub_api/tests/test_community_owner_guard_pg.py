"""Owner protection + system-role lookups on REAL Postgres (PR #1539 security review).

Regressions for: (1) the creator was linked to `community-admin`, so an admin they appointed
could demote/kick them -- the creator now holds the seeded system owner role and the guard
(`community_authz.lock_member_for_change`) decides on the LINKED role row, under a row lock;
(2) the platform view resolved the owner from the role string; (3) a custom role could shadow a
system role name; (4) an unlinked legacy role string still produced a role name / cached scopes.

Skipped (never failed) where the `docker` CLI is unavailable, like every other PG test here.
"""

from __future__ import annotations

import importlib.util
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from flask_core.database import AsyncDAL

from services import (
    community_authz,
    community_interaction,
    free_tier_caps,
    member_roles,
    platform_service,
)
from services.errors import ApiError
from services.scope_registry import KIND_ADMIN, effective_scopes
from tests import demo_pg_support as pg

pytestmark = pytest.mark.skipif(
    not pg.DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

CREATOR, APPOINTEE, JOINER, LEGACY = 5101, 5102, 5103, 5104
FULL = effective_scopes(KIND_ADMIN, None)


@pytest.fixture(scope="module")
def pg_db() -> Iterator[pg.PgTestDatabase]:
    """One migrated Postgres 17 container (+ real role seeding objects) for the module."""
    if not pg.DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with pg.migrated_postgres("hub-api-owner-guard") as db:
        pg.prepare_database(db)
        yield db


@pytest.fixture
def adal(pg_db: pg.PgTestDatabase) -> Iterator[AsyncDAL]:
    """Per-test truncated, bound pydal `AsyncDAL` on the real container."""
    yield from pg.fresh_dal(pg_db)


@pytest.fixture(autouse=True)
def _caps_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier caps are out of scope here (covered by test_free_tier_caps)."""

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


def _bare_community(dal: Any, tenant_id: int, name: str) -> int:
    """A community row with NO roles seeded yet (the legacy / pre-fix shape)."""
    return int(
        pg.scalar(
            dal,
            "INSERT INTO communities (name, tenant_id) VALUES (%s, %s) RETURNING id",
            [name, tenant_id],
        )
    )


async def _create_with_appointee(adal: AsyncDAL) -> tuple[int, int]:
    """Production create path (creator = owner), then the creator appoints an admin."""
    dal = adal.dal
    tenant_id = _tenant(dal)
    community_id = await free_tier_caps.create_community_capped(
        adal,
        dal,
        tenant_slug="acme",
        tenant_id=tenant_id,
        values=dict(name="pals", display_name="pals", platform="discord", tenant_id=tenant_id),
        creator_id=CREATOR,
    )
    for user_id, role in (
        (APPOINTEE, member_roles.COMMUNITY_ADMIN_ROLE),
        (JOINER, member_roles.COMMUNITY_MEMBER_ROLE),
    ):
        member_roles.set_member_role(
            dal, community_id=community_id, role=role, user_id=user_id, add=True
        )
    dal.commit()
    return community_id, tenant_id


def _guard(dal: Any, community_id: int, user_id: int, **kw: Any) -> Any:
    """Run the shared guard in its own (rolled back) transaction."""
    try:
        return community_authz.lock_member_for_change(
            dal, community_id=community_id, user_id=user_id, owner_message="owner", **kw
        )
    finally:
        dal.rollback()


def _load_migration_0145() -> Any:
    """The 0145 revision module (its `backfill_links` is the batched data-repair path)."""
    path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "0145_community_role_backfill.py"
    )
    spec = importlib.util.spec_from_file_location("m0145_owner_guard", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestOwnerProtection:
    async def test_creator_links_the_system_owner_role(self, adal: AsyncDAL) -> None:
        community_id, _ = await _create_with_appointee(adal)
        row = pg.q(
            adal.dal,
            "SELECT r.name, r.is_system, r.role_kind FROM community_members m "
            "JOIN community_roles r ON r.id = m.community_role_id "
            "WHERE m.community_id = %s AND m.user_id = %s",
            [community_id, str(CREATOR)],
        )
        assert row == [(member_roles.COMMUNITY_OWNER_ROLE, True, KIND_ADMIN)]

    async def test_appointed_admin_cannot_demote_or_remove_the_creator(
        self, adal: AsyncDAL
    ) -> None:
        dal = adal.dal
        community_id, _ = await _create_with_appointee(adal)
        caller = await community_authz.get_caller_community_role(
            adal, dal, community_id=community_id, user_id=APPOINTEE
        )
        assert caller is not None and caller.scopes == FULL  # holds EVERY scope
        with pytest.raises(ApiError) as exc:
            _guard(dal, community_id, CREATOR, editor_scopes=FULL, editor_bypass=False)
        assert (exc.value.status_code, exc.value.message) == (403, "owner")
        # control: the same editor may act on a member it out-scopes
        locked = _guard(dal, community_id, JOINER, editor_scopes=FULL, editor_bypass=False)
        assert locked.user_id == str(JOINER)

    async def test_global_or_tenant_admin_may_change_the_owner(self, adal: AsyncDAL) -> None:
        community_id, _ = await _create_with_appointee(adal)
        locked = _guard(
            adal.dal, community_id, CREATOR, editor_scopes=frozenset(), editor_bypass=True
        )
        assert locked.user_id == str(CREATOR)

    async def test_editor_must_hold_every_target_scope(self, adal: AsyncDAL) -> None:
        community_id, _ = await _create_with_appointee(adal)
        with pytest.raises(ApiError) as exc:
            _guard(
                adal.dal,
                community_id,
                APPOINTEE,
                editor_scopes=frozenset({"community:read"}),
                editor_bypass=False,
            )
        assert exc.value.status_code == 403
        assert "scopes you do not hold" in exc.value.message

    async def test_concurrent_removals_serialize_on_the_row_lock(
        self, adal: AsyncDAL, pg_db: pg.PgTestDatabase
    ) -> None:
        """The guard reads `FOR UPDATE`: a racing second removal waits, then sees 404."""
        community_id, _ = await _create_with_appointee(adal)
        locked, release = threading.Event(), threading.Event()
        outcome: dict[str, Any] = {}

        def _remove(name: str, hold: bool) -> None:
            conn = pg.open_dal(pg_db)
            d = conn.dal
            try:
                member = community_authz.lock_member_for_change(
                    d,
                    community_id=community_id,
                    user_id=APPOINTEE,
                    editor_scopes=FULL,
                    editor_bypass=False,
                    owner_message="owner",
                )
                if hold:
                    locked.set()
                    release.wait(10)
                d(d.community_members.id == member.id).update(is_active=False)
                d.commit()
                outcome[name] = "removed"
            except ApiError as err:
                d.rollback()
                outcome[name] = err.status_code
            finally:
                d.close()

        first = threading.Thread(target=_remove, args=("first", True))
        first.start()
        assert locked.wait(10)
        second = threading.Thread(target=_remove, args=("second", False))
        second.start()
        second.join(0.5)
        assert second.is_alive()  # blocked on the first transaction's row lock
        release.set()
        first.join(10)
        second.join(10)
        assert outcome == {"first": "removed", "second": 404}


class TestOwnerResolution:
    async def test_new_community_owner_resolves(self, adal: AsyncDAL) -> None:
        community_id, _ = await _create_with_appointee(adal)
        owner = await platform_service.community_owner_member(
            adal, adal.dal, community_id=community_id
        )
        assert owner is not None and owner.user_id == str(CREATOR)

    async def test_backfilled_legacy_owner_resolves(
        self, adal: AsyncDAL, pg_db: pg.PgTestDatabase
    ) -> None:
        dal = adal.dal
        community_id = _bare_community(dal, _tenant(dal), "legacy")
        pg.q(dal, "SELECT seed_community_system_roles(%s)", [community_id])
        pg.q(
            dal,
            "INSERT INTO community_members (community_id, user_id, role, is_active) "
            "VALUES (%s, %s, 'community-owner', true)",
            [community_id, str(LEGACY)],
        )
        # the role string alone identifies no owner
        assert (
            await platform_service.community_owner_member(adal, dal, community_id=community_id)
            is None
        )
        engine = sa.create_engine(pg_db.dsn)
        try:
            with engine.begin() as conn:
                linked, batches = _load_migration_0145().backfill_links(conn)
        finally:
            engine.dispose()
        assert (linked, batches) == (1, 1)
        owner = await platform_service.community_owner_member(adal, dal, community_id=community_id)
        assert owner is not None and owner.user_id == str(LEGACY)


class TestSystemNameLookups:
    async def test_reserved_names_rejected_on_custom_create(self, adal: AsyncDAL) -> None:
        community_id, _ = await _create_with_appointee(adal)
        names = [*sorted(member_roles.SYSTEM_ROLE_NAMES), "Community-Owner"]
        for name in names:
            with pytest.raises(ApiError) as exc:
                community_interaction.create_community_role(
                    adal.dal, community_id, {"name": name}, editor_scopes=FULL
                )
            assert (exc.value.status_code, exc.value.code) == (400, "ROLE_NAME_RESERVED")
        assert len(names) == 7

    async def test_shadowing_custom_role_is_ignored_by_system_lookups(self, adal: AsyncDAL) -> None:
        dal = adal.dal
        community_id = _bare_community(dal, _tenant(dal), "shadow")
        # A custom `member` row that predates seeding: the seed's ON CONFLICT then skips the
        # system `member`, so only the shadow row carries that name.
        pg.q(
            dal,
            "INSERT INTO community_roles (community_id, name, is_system, role_kind, base_claims) "
            "VALUES (%s, 'member', false, 0, '{\"scopes\": [\"community.members:manage\"]}')",
            [community_id],
        )
        pg.q(dal, "SELECT seed_community_system_roles(%s)", [community_id])
        with pytest.raises(LookupError):
            member_roles.set_member_role(
                dal,
                community_id=community_id,
                role=member_roles.COMMUNITY_MEMBER_ROLE,
                user_id=JOINER,
                add=True,
            )
        dal.rollback()
        linked = member_roles.set_member_role(
            dal,
            community_id=community_id,
            role=member_roles.COMMUNITY_MEMBER_ROLE,
            user_id=JOINER,
            add=True,
            strict=False,
        )
        dal.commit()
        assert linked is None
        assert community_authz.member_scopes_sync(dal, community_id, JOINER) == frozenset()


class TestUnlinkedLegacyString:
    async def test_unlinked_owner_string_grants_no_scopes_and_no_role_name(
        self, adal: AsyncDAL
    ) -> None:
        dal = adal.dal
        community_id, tenant_id = await _create_with_appointee(adal)
        pg.q(
            dal,
            "INSERT INTO community_members (community_id, user_id, role, is_active, claims_cache) "
            "VALUES (%s, %s, 'community-owner', true, %s)",
            [community_id, str(LEGACY), '{"scopes": ["community.members:manage"]}'],
        )
        caller = await community_authz.get_caller_community_role(
            adal, dal, community_id=community_id, user_id=LEGACY
        )
        assert caller is not None
        assert (caller.name, caller.scopes) == (None, frozenset())
        membership = await community_authz.resolve_community_membership_scoped(
            adal,
            dal,
            community_id=community_id,
            user_id=LEGACY,
            tenant_id=tenant_id,
            scopes_claim=frozenset(),
        )
        assert membership is not None
        assert (membership.role, membership.scopes, membership.is_admin) == (
            None,
            frozenset(),
            False,
        )
        assert community_authz.member_scopes_sync(dal, community_id, LEGACY) == frozenset()
        # and the unlinked string is never treated as the owner by the guard
        locked = _guard(dal, community_id, LEGACY, editor_scopes=frozenset(), editor_bypass=False)
        assert locked.user_id == str(LEGACY)
