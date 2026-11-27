"""A lifted ban restores normal access: floor-ban LIFT (status 8) and soft-ban clear/expiry.

User decision 2026-11-20: lifting a ban returns the user to normal access patterns. The route is
`DELETE /api/v1/admin/<community>/reputation/floor-bans/<user>` (tenant admins only). The
Postgres-only `reputation_floor_ban_lift()` function is faked with a table-writing stand-in here;
its real behaviour is covered against Postgres in `alembic/tests/test_0139_floor_ban_lift.py`.
The gate under test is the inventory member authz (`require_inventory_member`), which reads both
ban tables.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from flask_core.database import AsyncDAL
from pydal import Field
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.admin import admin_bp
from blueprints.v1.community_join import community_join_bp
from services import auth_service, ban_suppression, floor_ban_lift
from services.audit_events import (
    AuditAction,
    AuditCategory,
    AuditOutcome,
    RequestFacts,
    classify_request,
)
from services.community_inventory_authz import require_inventory_member
from services.errors import ApiError
from services.membership_state import MembershipState
from services.schema import (
    bind_admin_tables,
    bind_auth_tables,
    bind_community_authz_tables,
    bind_erasure_reputation_tables,
    bind_reputation_policy_tables,
)
from tests.community_ids import install_community_ids, public_id
from tests.conftest import TENANT_SLUG, make_user_token
from tests.test_v1_join_request_blueprint import _test_config
from tests.user_ids import uid

ADMIN, MEMBER, TENANT_ADMIN = 1, 2, 3
ACTIVE_STATUSES = (1, 2, 3, 4, 5, 7)


def _uuid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


@pytest.fixture
def db(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Sqlite AsyncDAL with auth/admin/reputation tables, one community and three users.

    User 1 is a community admin (NOT a tenant admin), 2 a plain member, 3 a tenant admin. The
    floor-ban SQL function is replaced by a faithful table-writing stand-in.
    """
    async_dal = AsyncDAL(f"sqlite://{tmp_path / 'lift.db'}", pool_size=1)
    dal = async_dal.dal
    dal.define_table(
        "tenants",
        Field("slug", unique=True),
        Field("display_name"),
        Field("logo_url"),
        Field("is_global", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("config", "json"),
    )
    bind_auth_tables(dal, migrate=True)
    bind_admin_tables(dal, migrate=True)
    bind_community_authz_tables(dal, migrate=True)
    bind_erasure_reputation_tables(dal, migrate=True)
    bind_reputation_policy_tables(dal, migrate=True)
    tenant = dal.tenants.insert(slug=TENANT_SLUG, display_name="Acme", is_active=True)
    cid = dal.communities.insert(name="c1", display_name="c1", tenant_id=tenant, is_active=True)
    for n in (ADMIN, MEMBER, TENANT_ADMIN):
        dal.hub_users.insert(
            id=n,
            username=f"user{n}",
            email=f"u{n}@example.com",
            is_active=True,
            is_super_admin=False,
        )
        role_id = dal.community_roles.insert(
            community_id=cid,
            name=f"role{n}",
            is_system=n == ADMIN,
            role_kind=1 if n == ADMIN else 0,
            base_claims={
                "scopes": ["community:manage_members" if n == ADMIN else "community:read"]
            },
        )
        dal.community_members.insert(
            community_id=cid,
            user_id=str(n),
            user_uuid=_uuid(n),
            display_name=f"user{n}",
            is_active=True,
            community_role_id=role_id,
            joined_at=datetime.now(UTC),
        )
    dal.tenant_admins.insert(user_id=TENANT_ADMIN, tenant_id=tenant)
    dal.commit()
    for name in dal.tables:
        dal(dal[name]).count()

    def fake_postgres_call(dal_: Any) -> floor_ban_lift.LiftCall:
        def call(tenant_id: int, user_uuids: list[str], lifted_by: str) -> int:
            bans = dal_.bundle_reputation_floor_bans
            query = (
                (bans.tenant_id == tenant_id)
                & bans.user_uuid.belongs(user_uuids)
                & bans.status.belongs(list(ACTIVE_STATUSES))
            )
            n = dal_(query).update(status=8, lifted_at=datetime.now(UTC), lifted_by=lifted_by)
            dal_.commit()
            return int(n or 0)

        return call

    monkeypatch.setattr(floor_ban_lift, "_postgres_call", fake_postgres_call)
    yield SimpleNamespace(async_dal=async_dal, dal=dal, tenant_id=tenant, community_id=cid)
    dal.close()


@pytest.fixture
def client(db: Any) -> Any:
    app = Quart(__name__)
    QuartSchema(app)
    app.config["dal"] = db.dal
    app.config["async_dal"] = db.async_dal
    install_community_ids(app, db.dal)
    app.register_blueprint(admin_bp)
    return app.test_client()


def _ban(db: Any, *, status: int = 2, user: int = MEMBER, key: int = 0, action: int = 1) -> None:
    """A tenant floor-ban outbox row for user `user` (banned in another community of the tenant)."""
    db.dal.bundle_reputation_floor_bans.insert(
        tenant_id=db.tenant_id,
        community_id=db.community_id + 100 + key,
        user_uuid=_uuid(user),
        platform="discord",
        status=status,
        action=action,
    )
    db.dal.commit()


def _soft_ban(db: Any, *, expires_at: datetime | None = None, user: int = MEMBER) -> None:
    db.dal.bundle_reputation_soft_bans.insert(
        tenant_id=db.tenant_id,
        community_id=db.community_id,
        user_uuid=_uuid(user),
        reason_code=2,
        set_by="admin",
        set_at=datetime.now(UTC),
        expires_at=expires_at.replace(tzinfo=None) if expires_at else None,
    )
    db.dal.commit()


def _allowed(db: Any, user: int = MEMBER) -> bool:
    """Whether the inventory member gate lets `user` through (True) or raises 403 (False)."""
    ctx = SimpleNamespace(tenant_id=db.tenant_id)
    try:
        require_inventory_member(db.dal, ctx, community_id=db.community_id, user_id=user)
    except ApiError as exc:
        assert exc.status_code == 403
        return False
    return True


def _url(db: Any, user: int = MEMBER) -> str:
    """The lift URL: community public id + the target's hub public UUID (never a numeric id)."""
    uuid_ = db.dal(db.dal.hub_users.id == user).select().first().uuid
    return f"/api/v1/admin/{public_id(db.async_dal, db.community_id)}/reputation/floor-bans/{uuid_}"


def _h(user: int, *, scope: str = "tenant:admin") -> dict[str, str]:
    """A bearer for `user`; `scope` is irrelevant to authorization (the DB role decides)."""
    token = make_user_token(user_id=user, scope=scope, tenant=TENANT_SLUG)
    return {"Authorization": f"Bearer {token}"}


class TestFloorBanLift:
    async def test_ban_denies_then_lift_allows_then_reban_denies(
        self, client: Any, db: Any
    ) -> None:
        assert _allowed(db)
        _ban(db)
        assert not _allowed(db)

        resp = await client.delete(_url(db), headers=_h(TENANT_ADMIN))
        assert resp.status_code == 200
        assert _allowed(db), "a lifted user regains exactly normal access"

        row = (
            db.dal(db.dal.bundle_reputation_floor_bans.user_uuid == _uuid(MEMBER)).select().first()
        )
        assert row.status == 8
        assert row.lifted_at is not None
        assert row.lifted_by and len(row.lifted_by) == 36, "lifted_by is the admin's UUID"

        _ban(db, key=1)  # a fresh enforcement after the lift
        assert not _allowed(db)

    @pytest.mark.parametrize("status", ACTIVE_STATUSES)
    def test_every_in_force_status_denies(self, db: Any, status: int) -> None:
        """Enforced, queued, retrying, dead, rate-limited and in-flight bans all still deny."""
        _ban(db, status=status)
        assert not _allowed(db)

    @pytest.mark.parametrize("status", (6, 8))
    def test_refused_and_lifted_rows_do_not_deny(self, db: Any, status: int) -> None:
        _ban(db, status=status)
        assert _allowed(db)

    @pytest.mark.parametrize("status", ACTIVE_STATUSES)
    async def test_lift_works_from_every_in_force_status(
        self, client: Any, db: Any, status: int
    ) -> None:
        _ban(db, status=status)
        assert (await client.delete(_url(db), headers=_h(TENANT_ADMIN))).status_code == 200
        assert _allowed(db)

    async def test_lift_is_tenant_wide_across_fanned_out_rows(self, client: Any, db: Any) -> None:
        _ban(db, key=0)
        _ban(db, key=1)
        assert (await client.delete(_url(db), headers=_h(TENANT_ADMIN))).status_code == 200
        assert _allowed(db)

    async def test_lift_does_not_touch_other_users(self, client: Any, db: Any) -> None:
        _ban(db, user=MEMBER)
        _ban(db, user=ADMIN)
        assert (await client.delete(_url(db), headers=_h(TENANT_ADMIN))).status_code == 200
        assert _allowed(db, MEMBER)
        assert not _allowed(db, ADMIN)

    async def test_tenant_admin_scope_claim_without_the_db_row_is_refused(
        self, client: Any, db: Any
    ) -> None:
        """Keyed deny test: a tenant-admin TOKEN without the `tenant_admins` DB row is refused."""
        _ban(db)
        resp = await client.delete(_url(db), headers=_h(MEMBER, scope="tenant:admin"))
        assert resp.status_code == 403
        assert not _allowed(db)

    async def test_authorization_is_the_db_row_never_a_token_scope(
        self, client: Any, db: Any
    ) -> None:
        """A tenant admin is admitted by the `tenant_admins` row whatever scopes the token has."""
        _ban(db)
        resp = await client.delete(_url(db), headers=_h(TENANT_ADMIN, scope=""))
        assert resp.status_code == 200
        assert _allowed(db)

    @pytest.mark.parametrize("caller", (MEMBER, ADMIN))
    async def test_non_tenant_admin_is_refused_and_ban_stays(
        self, client: Any, db: Any, caller: int
    ) -> None:
        """A plain member and even a community admin cannot reverse a tenant-wide ban."""
        _ban(db)
        assert (await client.delete(_url(db), headers=_h(caller))).status_code == 403
        assert not _allowed(db)

    async def test_numeric_user_id_in_the_url_is_not_a_route(self, client: Any, db: Any) -> None:
        """The numeric primary key never crosses the boundary: `/floor-bans/2` does not exist."""
        base = f"/api/v1/admin/{public_id(db.async_dal, db.community_id)}/reputation/floor-bans"
        assert (
            await client.delete(f"{base}/{MEMBER}", headers=_h(TENANT_ADMIN))
        ).status_code == 404

    async def test_unknown_uuid_is_404_like_no_ban(self, client: Any, db: Any) -> None:
        base = f"/api/v1/admin/{public_id(db.async_dal, db.community_id)}/reputation/floor-bans"
        url = f"{base}/{uuid.uuid4()}"
        assert (await client.delete(url, headers=_h(TENANT_ADMIN))).status_code == 404

    def test_active_statuses_are_the_documented_deny_set(self) -> None:
        from services import ban_suppression

        assert ban_suppression.FLOOR_BAN_ACTIVE_STATUSES == (1, 2, 3, 4, 5, 7)
        assert floor_ban_lift.FLOOR_BAN_LIFTED not in ban_suppression.FLOOR_BAN_ACTIVE_STATUSES

    async def test_unauthenticated_is_401(self, client: Any, db: Any) -> None:
        assert (await client.delete(_url(db))).status_code == 401

    async def test_nothing_in_force_is_404(self, client: Any, db: Any) -> None:
        assert (await client.delete(_url(db), headers=_h(TENANT_ADMIN))).status_code == 404
        _ban(db, status=6)  # refused: never enforced, nothing to lift
        assert (await client.delete(_url(db), headers=_h(TENANT_ADMIN))).status_code == 404

    async def test_second_lift_is_404_idempotent(self, client: Any, db: Any) -> None:
        _ban(db)
        assert (await client.delete(_url(db), headers=_h(TENANT_ADMIN))).status_code == 200
        assert (await client.delete(_url(db), headers=_h(TENANT_ADMIN))).status_code == 404
        assert _allowed(db)

    async def test_cross_tenant_admin_is_refused_and_ban_stays(self, client: Any, db: Any) -> None:
        """An admin of ANOTHER tenant is refused and never touches this tenant's ban.

        Without a `tenant_admins` row there the guard answers 403; with one, the foreign community
        is a 404.
        """
        other = db.dal.tenants.insert(slug="other-tenant", display_name="Other", is_active=True)
        db.dal.commit()
        _ban(db)
        headers = {
            "Authorization": "Bearer "
            + make_user_token(user_id=TENANT_ADMIN, scope="tenant:admin", tenant="other-tenant")
        }
        assert (await client.delete(_url(db), headers=headers)).status_code == 403
        db.dal.tenant_admins.insert(user_id=TENANT_ADMIN, tenant_id=other)
        db.dal.commit()
        assert (await client.delete(_url(db), headers=headers)).status_code == 404
        assert not _allowed(db)

    def test_counter_failure_never_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(floor_ban_lift, "get_meter", lambda: 1 / 0)
        monkeypatch.setattr(floor_ban_lift, "_COUNTER", None)
        floor_ban_lift._emit("lifted")


class TestUnbanDeliveryRowsNeverDeny:
    @pytest.mark.parametrize("status", (1, 2, 3, 4, 5, 7))
    def test_an_unban_row_in_any_status_does_not_deny(self, db: Any, status: int) -> None:
        """Rows with action 2 (alembic 0140) are platform UNBAN deliveries: they restore access."""
        _ban(db, status=status, action=2)
        assert _allowed(db)

    def test_a_ban_row_still_denies_next_to_an_unban_row(self, db: Any) -> None:
        _ban(db, status=2, action=2)
        _ban(db, status=2, action=1, key=1)
        assert not _allowed(db)


class TestSoftBanLiftAndExpiry:
    def test_active_soft_ban_denies(self, db: Any) -> None:
        _soft_ban(db)
        assert not _allowed(db)

    def test_expired_soft_ban_allows(self, db: Any) -> None:
        _soft_ban(db, expires_at=datetime.now(UTC) - timedelta(seconds=1))
        assert _allowed(db)

    def test_future_expiry_still_denies(self, db: Any) -> None:
        _soft_ban(db, expires_at=datetime.now(UTC) + timedelta(hours=1))
        assert not _allowed(db)

    def test_cleared_soft_ban_allows_and_reban_denies(self, db: Any) -> None:
        """Clearing deletes the row (the SQL `reputation_clear_soft_ban`), so access returns."""
        _soft_ban(db)
        assert not _allowed(db)
        bans = db.dal.bundle_reputation_soft_bans
        db.dal(bans.user_uuid == _uuid(MEMBER)).delete()
        db.dal.commit()
        assert _allowed(db)
        _soft_ban(db)
        assert not _allowed(db)


class TestLiftRestoresMembership:
    """Regression (PR #1542 review): a lift restores the BANNED membership in the SAME transaction.

    After the lift the member re-joins successfully and `/auth/me` (`get_current_user`) lists the
    community again, with its role link untouched by the ban.
    """

    @staticmethod
    def _ban_membership(db: Any) -> int:
        ban_suppression.ban_membership(
            db.dal, community_id=db.community_id, hub_user_id=MEMBER, now=datetime.now(UTC)
        )
        db.dal.commit()
        return int(_member_row(db).community_role_id)

    @staticmethod
    def _join_client(db: Any) -> Any:
        app = Quart(__name__)
        QuartSchema(app)
        app.config["dal"] = db.dal
        app.config["async_dal"] = db.async_dal
        app.config["HUB_API_CONFIG"] = _test_config()
        install_community_ids(app, db.dal)
        app.register_blueprint(community_join_bp)
        return app.test_client()

    async def _assert_restored(self, db: Any, role_id: int) -> None:
        row = _member_row(db)
        assert (int(row.membership_state), row.is_active) == (int(MembershipState.ACTIVE), True)
        assert row.community_role_id == role_id, "the ban never touched the role link"
        join = await self._join_client(db).post(
            f"/api/v1/communities/{public_id(db.async_dal, db.community_id)}/join",
            headers=_h(MEMBER, scope=""),
        )
        assert join.status_code in (200, 201), await join.get_json()
        me = await auth_service.get_current_user(db.async_dal, db.dal, user_id=MEMBER)
        assert me is not None
        listed = [
            c for c in me.communities if c.public_id == public_id(db.async_dal, db.community_id)
        ]
        assert len(listed) == 1, "the community is back on /auth/me"
        assert listed[0].scopes, "with the role's scopes"

    async def _assert_banned_out(self, db: Any) -> None:
        join = await self._join_client(db).post(
            f"/api/v1/communities/{public_id(db.async_dal, db.community_id)}/join",
            headers=_h(MEMBER, scope=""),
        )
        assert join.status_code == 403
        me = await auth_service.get_current_user(db.async_dal, db.dal, user_id=MEMBER)
        assert me is not None and not [
            c for c in me.communities if c.public_id == public_id(db.async_dal, db.community_id)
        ]

    async def test_floor_ban_lift_restores_membership(self, client: Any, db: Any) -> None:
        role_id = self._ban_membership(db)
        _ban(db)
        await self._assert_banned_out(db)
        resp = await client.delete(_url(db), headers=_h(TENANT_ADMIN))
        assert resp.status_code == 200
        await self._assert_restored(db, role_id)

    async def test_soft_ban_clear_restores_membership(
        self, client: Any, db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = db.dal.executesql

        def fake_sql(sql: str, placeholders: list[Any] | None = None, **kw: Any) -> Any:
            if "reputation_clear_soft_ban" not in sql:
                return real(sql, placeholders=placeholders, **kw)
            _tenant, community_id, user_uuid, _actor, _only_floor = placeholders or []
            bans = db.dal.bundle_reputation_soft_bans
            n = db.dal((bans.community_id == community_id) & (bans.user_uuid == user_uuid)).delete()
            return [[bool(n)]]

        monkeypatch.setattr(db.dal, "executesql", fake_sql)
        role_id = self._ban_membership(db)
        _soft_ban(db)
        await self._assert_banned_out(db)
        resp = await client.delete(
            f"/api/v1/admin/{public_id(db.async_dal, db.community_id)}/reputation/soft-bans/"
            f"{uid(MEMBER)}",
            headers=_h(ADMIN, scope="community:admin"),
        )
        assert resp.status_code == 200, await resp.get_json()
        await self._assert_restored(db, role_id)

    async def test_refused_lift_leaves_the_membership_banned(self, client: Any, db: Any) -> None:
        """Fail closed: a refused lift (non-tenant-admin) restores nothing."""
        self._ban_membership(db)
        _ban(db)
        resp = await client.delete(_url(db), headers=_h(ADMIN))
        assert resp.status_code == 403
        assert int(_member_row(db).membership_state) == int(MembershipState.BANNED)


def _member_row(db: Any) -> Any:
    cm = db.dal.community_members
    return (
        db.dal((cm.community_id == db.community_id) & (cm.user_id == str(MEMBER))).select().first()
    )


class TestAuditEvent:
    def test_lift_route_classifies_as_floor_ban_lifted(self) -> None:
        rule = "/api/v1/admin/<cuuid:community_id>/reputation/floor-bans/<uuid:user_uuid>"
        got = classify_request(
            RequestFacts(
                method="DELETE",
                rule=rule,
                status_code=200,
                scope_checked=False,
                denied=False,
                authenticated=True,
            )
        )
        assert got is not None
        assert got.category == AuditCategory.ADMIN
        assert got.action == AuditAction.ADMIN_FLOOR_BAN_LIFTED
        assert got.outcome == AuditOutcome.SUCCESS

    def test_refused_lift_is_an_authz_event(self) -> None:
        rule = "/api/v1/admin/<cuuid:community_id>/reputation/floor-bans/<uuid:user_uuid>"
        got = classify_request(
            RequestFacts(
                method="DELETE",
                rule=rule,
                status_code=403,
                scope_checked=False,
                denied=False,
                authenticated=True,
            )
        )
        assert got is not None
        assert got.action == AuditAction.AUTHZ_DENIED
