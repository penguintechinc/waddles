"""Ownership transfer (`superadmin_service.transfer_ownership`) on REAL Postgres.

Runs the production write path against a migrated container carrying the real
`seed_community_system_roles()` + role-kind triggers, then asks the REAL authz resolvers what the
old and new owner hold. Ownership is the member linked to the system `community-owner` role;
nothing here reads or writes a name or `communities.owner_id`.

Skipped (never failed) where the `docker` CLI is unavailable, like every other PG test here.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from flask_core.database import AsyncDAL

from services import community_authz, free_tier_caps, member_roles, superadmin_service
from services.errors import ApiError
from services.scope_registry import KIND_ADMIN, effective_scopes
from tests import demo_pg_support as pg

pytestmark = pytest.mark.skipif(
    not pg.DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

FULL = effective_scopes(KIND_ADMIN, None)


@pytest.fixture(scope="module")
def pg_db() -> Iterator[pg.PgTestDatabase]:
    """One migrated Postgres 17 container (+ real role seeding objects) for the module."""
    if not pg.DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with pg.migrated_postgres("hub-api-ownership-transfer") as db:
        pg.prepare_database(db)
        yield db


@pytest.fixture
def adal(pg_db: pg.PgTestDatabase) -> Iterator[AsyncDAL]:
    """Per-test truncated, bound pydal `AsyncDAL` on the real container."""
    yield from pg.fresh_dal(pg_db)


@pytest.fixture(autouse=True)
def _caps_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier caps are out of scope here; never block the create."""

    async def _no_caps(_tenant_slug: str) -> bool:
        return False

    monkeypatch.setattr(free_tier_caps, "_caps_apply", _no_caps)


def _user(dal: Any, name: str) -> tuple[int, str]:
    """A `hub_users` row -> (numeric id, public uuid)."""
    row = pg.q(
        dal,
        "INSERT INTO hub_users (username, email) VALUES (%s, %s) RETURNING id, uuid",
        [name, f"{name}@example.invalid"],
    )[0]
    return int(row[0]), str(row[1])


def _tenant(dal: Any, slug: str) -> int:
    return int(
        pg.scalar(
            dal,
            "INSERT INTO tenants (slug, display_name, is_global, is_active) "
            "VALUES (%s, %s, false, true) RETURNING id",
            [slug, slug],
        )
    )


class World:
    """A tenant + community created via the production path, with an owner and extra members."""

    def __init__(self, adal: AsyncDAL) -> None:
        """Bind the DAL; call :meth:`build` to seed."""
        self.adal, self.dal = adal, adal.dal

    async def build(self) -> World:
        """Seed tenants, users, the community (creator = owner), members, a banned member."""
        dal = self.dal
        pg.q(dal, "TRUNCATE tenant_admins RESTART IDENTITY CASCADE")
        self.tenant_id = _tenant(dal, "acme")
        self.other_tenant_id = _tenant(dal, "other")
        self.owner_id, self.owner_uuid = _user(dal, "olive")
        self.next_id, self.next_uuid = _user(dal, "nigel")
        self.banned_id, self.banned_uuid = _user(dal, "bruno")
        self.stranger_id, self.stranger_uuid = _user(dal, "sally")
        self.admin_caller_id, _ = _user(dal, "tenantadmin")
        self.community_id = await free_tier_caps.create_community_capped(
            self.adal,
            dal,
            tenant_slug="acme",
            tenant_id=self.tenant_id,
            values=dict(
                name="pals", display_name="pals", platform="discord", tenant_id=self.tenant_id
            ),
            creator_id=self.owner_id,
        )
        for uid in (self.next_id, self.banned_id):
            member_roles.set_member_role(
                dal,
                community_id=self.community_id,
                role=member_roles.COMMUNITY_MEMBER_ROLE,
                user_id=uid,
                add=True,
            )
        dal.commit()
        pg.q(
            dal,
            "UPDATE community_members SET membership_state = 2 "
            "WHERE community_id = %s AND user_id = %s",
            [self.community_id, str(self.banned_id)],
        )
        pg.q(
            dal,
            "INSERT INTO tenant_admins (user_id, tenant_id) VALUES (%s, %s)",
            [self.admin_caller_id, self.tenant_id],
        )
        return self

    async def transfer(self, target_uuid: str, **kw: Any) -> bool:
        """Run the service transfer as the seeded tenant admin unless overridden."""
        args: dict[str, Any] = dict(
            community_id=self.community_id,
            new_owner_user_uuid=target_uuid,
            caller_user_id=self.admin_caller_id,
            caller_tenant_id=self.tenant_id,
            is_global=False,
            audit=None,
        )
        args.update(kw)
        return await superadmin_service.transfer_ownership(self.adal, self.dal, **args)

    def role_name(self, user_id: int) -> str | None:
        """The name of the community role the member links (None: not a member)."""
        rows = pg.q(
            self.dal,
            "SELECT r.name FROM community_members m JOIN community_roles r "
            "ON r.id = m.community_role_id WHERE m.community_id = %s AND m.user_id = %s",
            [self.community_id, str(user_id)],
        )
        return rows[0][0] if rows else None


class RecordingAudit:
    """In-memory audit sink with the `AuditService.record` surface."""

    def __init__(self, fail: bool = False) -> None:
        """Start empty; ``fail`` makes every record raise."""
        self.events: list[Any] = []
        self.fail = fail

    async def record(self, event: Any) -> None:
        """Store the event or raise when configured to fail."""
        if self.fail:
            raise RuntimeError("sink down")
        self.events.append(event)


class TestTransferOwnership:
    async def test_old_owner_loses_owner_new_gains_it_via_real_authz(self, adal: AsyncDAL) -> None:
        w = await World(adal).build()
        audit = RecordingAudit()
        assert await w.transfer(w.next_uuid, audit=audit) is True

        assert w.role_name(w.owner_id) == member_roles.COMMUNITY_ADMIN_ROLE
        assert w.role_name(w.next_id) == member_roles.COMMUNITY_OWNER_ROLE
        for uid in (w.owner_id, w.next_id):  # both admin-kind: full scopes via the REAL resolver
            caller = await community_authz.get_caller_community_role(
                adal, w.dal, community_id=w.community_id, user_id=uid
            )
            assert caller is not None and caller.scopes == FULL
        owner = await community_authz.community_owner_member(
            adal, w.dal, community_id=w.community_id
        )
        assert owner is not None and owner.user_id == str(w.next_id)
        name = await community_authz.community_owner_username(
            adal, w.dal, community_id=w.community_id
        )
        assert name == "nigel"
        # the old owner is now an ordinary admin: the owner guard no longer protects them ...
        locked = community_authz.lock_member_for_change(
            w.dal,
            community_id=w.community_id,
            user_id=w.owner_id,
            editor_scopes=FULL,
            editor_bypass=False,
            owner_message="owner",
        )
        w.dal.rollback()
        assert locked.user_id == str(w.owner_id)
        # ... and the new owner IS protected.
        with pytest.raises(ApiError) as exc:
            community_authz.lock_member_for_change(
                w.dal,
                community_id=w.community_id,
                user_id=w.next_id,
                editor_scopes=FULL,
                editor_bypass=False,
                owner_message="owner",
            )
        w.dal.rollback()
        assert exc.value.status_code == 403

        [event] = audit.events
        assert event.action == "community.ownership_transferred"
        assert event.details == {
            "new_owner_user_uuid": w.next_uuid,
            "old_owner_user_uuid": w.owner_uuid,
        }
        assert event.actor_user_id == w.admin_caller_id and event.target_type == "community"

    async def test_owner_names_batch_is_display_only(self, adal: AsyncDAL) -> None:
        w = await World(adal).build()
        await w.transfer(w.next_uuid)
        names = await superadmin_service.community_owner_names(
            adal, w.dal, community_ids=[w.community_id, 999999]
        )
        assert names == {w.community_id: "nigel"}

    async def test_non_member_target_is_404_and_changes_nothing(self, adal: AsyncDAL) -> None:
        w = await World(adal).build()
        with pytest.raises(ApiError) as exc:
            await w.transfer(w.stranger_uuid)
        assert exc.value.status_code == 404
        assert w.role_name(w.owner_id) == member_roles.COMMUNITY_OWNER_ROLE

    async def test_unknown_uuid_is_404(self, adal: AsyncDAL) -> None:
        w = await World(adal).build()
        with pytest.raises(ApiError) as exc:
            await w.transfer("00000000-0000-4000-8000-000000000001")
        assert exc.value.status_code == 404

    async def test_banned_target_is_409_and_changes_nothing(self, adal: AsyncDAL) -> None:
        w = await World(adal).build()
        with pytest.raises(ApiError) as exc:
            await w.transfer(w.banned_uuid)
        assert exc.value.status_code == 409
        assert w.role_name(w.owner_id) == member_roles.COMMUNITY_OWNER_ROLE
        assert w.role_name(w.banned_id) == member_roles.COMMUNITY_MEMBER_ROLE

    async def test_already_owner_is_idempotent_noop(self, adal: AsyncDAL) -> None:
        w = await World(adal).build()
        audit = RecordingAudit()
        assert await w.transfer(w.owner_uuid, audit=audit) is False
        assert w.role_name(w.owner_id) == member_roles.COMMUNITY_OWNER_ROLE
        assert audit.events == []

    async def test_non_admin_caller_is_403(self, adal: AsyncDAL) -> None:
        w = await World(adal).build()
        with pytest.raises(ApiError) as exc:
            await w.transfer(w.next_uuid, caller_user_id=w.stranger_id)
        assert exc.value.status_code == 403
        assert w.role_name(w.next_id) == member_roles.COMMUNITY_MEMBER_ROLE

    async def test_tenant_admin_of_another_tenant_is_403(self, adal: AsyncDAL) -> None:
        w = await World(adal).build()
        with pytest.raises(ApiError) as exc:
            await w.transfer(w.next_uuid, caller_tenant_id=w.other_tenant_id)
        assert exc.value.status_code == 403

    async def test_other_tenant_community_is_404_for_tenant_admin_not_global(
        self, adal: AsyncDAL
    ) -> None:
        w = await World(adal).build()
        pg.q(
            w.dal,
            "INSERT INTO tenant_admins (user_id, tenant_id) VALUES (%s, %s)",
            [w.admin_caller_id, w.other_tenant_id],
        )
        with pytest.raises(ApiError) as exc:
            await w.transfer(w.next_uuid, caller_tenant_id=w.other_tenant_id)
        assert exc.value.status_code == 404
        # a global (system-tenant) admin needs no tenant match
        assert await w.transfer(w.next_uuid, caller_user_id=w.stranger_id, is_global=True) is True
        assert w.role_name(w.next_id) == member_roles.COMMUNITY_OWNER_ROLE

    async def test_second_write_failure_rolls_both_back(
        self, adal: AsyncDAL, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        w = await World(adal).build()
        real = member_roles.set_member_role
        calls: list[int] = []

        def flaky(dal: Any, **kwargs: Any) -> Any:
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("boom on the second write")
            return real(dal, **kwargs)

        monkeypatch.setattr(member_roles, "set_member_role", flaky)
        with pytest.raises(RuntimeError):
            await w.transfer(w.next_uuid)
        monkeypatch.undo()
        assert len(calls) == 2  # the demote DID run, then the promote blew up
        assert w.role_name(w.owner_id) == member_roles.COMMUNITY_OWNER_ROLE
        assert w.role_name(w.next_id) == member_roles.COMMUNITY_MEMBER_ROLE

    async def test_audit_failure_is_loud_after_commit(self, adal: AsyncDAL) -> None:
        w = await World(adal).build()
        with pytest.raises(ApiError) as exc:
            await w.transfer(w.next_uuid, audit=RecordingAudit(fail=True))
        assert (exc.value.status_code, exc.value.code) == (500, "OWNERSHIP_AUDIT_FAILED")
        assert w.role_name(w.next_id) == member_roles.COMMUNITY_OWNER_ROLE
