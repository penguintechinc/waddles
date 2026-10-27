"""Edge cases for flask_core.community_access: super-admin bypass, identity decoding, exemptions.

Runs against a real sqlite pydal DB, real JWTs and a real Quart app (no mocked authz).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydal import DAL
from quart import Blueprint, Quart

from flask_core import community_access as ca
from flask_core.auth import create_jwt_token
from flask_core.community_access import (
    CallerIdentityError,
    CommunityAccessError,
    bind_shared_read_tables,
    decode_caller_user_id,
    install_community_scoped_auth,
    require_admin,
    require_member,
)
from flask_core.tenancy import TenantContext

SECRET = "test-secret-key-not-for-production-use-only"


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", SECRET)


def token(sub="1", roles=None, tenant="acme") -> str:
    return create_jwt_token(sub, "alice", "a@e.invalid", roles or [], SECRET, tenant=tenant)


def req(authorization: str | None = None):
    headers = {} if authorization is None else {"Authorization": authorization}
    return SimpleNamespace(headers=headers)


@pytest.fixture
def db():
    d = DAL("sqlite:memory")
    bind_shared_read_tables(d, migrate=True)
    yield d
    d.close()


class SyncAsyncDal:
    def __init__(self, dal):
        self.dal = dal

    async def select_async(self, query_set, *fields):
        return query_set.select(*fields) if fields else query_set.select()


@pytest.fixture
def world(db):
    t = db.tenants.insert(slug="acme", is_active=True)
    other = db.tenants.insert(slug="other", is_active=True)
    c = db.communities.insert(tenant_id=t)
    c_other = db.communities.insert(tenant_id=other)
    db.community_members.insert(community_id=c, user_id="1", role="member", is_active=True)
    db.community_members.insert(community_id=c, user_id="2", role="community-admin", is_active=True)
    db.community_members.insert(community_id=c, user_id="3", role="community-admin", is_active=False)
    db.commit()
    return SimpleNamespace(tenant=t, community=c, other_community=c_other,
                           ctx=TenantContext(tenant_id=t, tenant_slug="acme"), adal=SyncAsyncDal(db))


class TestBindSharedReadTables:
    def test_idempotent(self, db):
        before = set(db.tables)
        bind_shared_read_tables(db)  # already bound -> early return, no redefinition error
        assert set(db.tables) == before

    def test_binds_only_missing_tables(self):
        d = DAL("sqlite:memory")
        try:
            d.define_table("tenants", d.Field("slug"), d.Field("is_active", "boolean"), migrate=True)
            bind_shared_read_tables(d, migrate=True)
            assert {"tenants", "communities", "community_members"} <= set(d.tables)
        finally:
            d.close()


class TestSuperAdmin:
    def test_is_super_admin_cases(self):
        assert ca._is_super_admin(req(None)) is False
        assert ca._is_super_admin(req("Basic abc")) is False
        assert ca._is_super_admin(req("Bearer garbage")) is False
        assert ca._is_super_admin(req(f"Bearer {token(roles=['viewer'])}")) is False
        assert ca._is_super_admin(req(f"Bearer {token(roles=['super_admin'])}")) is True

    async def test_super_admin_bypasses_membership_and_tenant_checks(self, db, world):
        r = req(f"Bearer {token(sub='999', roles=['super_admin'])}")
        # not a member of anything, and even for a community in ANOTHER tenant
        await require_admin(world.adal, db, r, world.ctx, community_id=world.community, user_id=999)
        await require_member(world.adal, db, r, world.ctx, community_id=world.other_community, user_id=999)

    async def test_regular_roles_do_not_bypass(self, db, world):
        r = req(f"Bearer {token(sub='999', roles=['admin'])}")
        with pytest.raises(CommunityAccessError):
            await require_member(world.adal, db, r, world.ctx, community_id=world.community, user_id=999)


class TestMembershipRules:
    async def test_inactive_admin_row_is_not_admin(self, db, world):
        with pytest.raises(CommunityAccessError, match="admin access required"):
            await require_admin(world.adal, db, req(), world.ctx, community_id=world.community, user_id=3)

    async def test_cross_tenant_community_is_forbidden_not_not_found(self, db, world):
        with pytest.raises(CommunityAccessError, match="Community access required"):
            await require_member(world.adal, db, req(), world.ctx, community_id=world.other_community, user_id=1)

    async def test_admin_role_passes_both(self, db, world):
        await require_admin(world.adal, db, req(), world.ctx, community_id=world.community, user_id=2)
        await require_member(world.adal, db, req(), world.ctx, community_id=world.community, user_id=2)


class TestDecodeCallerUserId:
    def test_valid(self):
        assert decode_caller_user_id(req(f"Bearer {token(sub='42')}")) == 42

    @pytest.mark.parametrize("header,msg", [
        (None, "Authentication required"),
        ("Basic abc", "Authentication required"),
        ("Bearer garbage", "Invalid or expired token"),
    ])
    def test_rejections(self, header, msg):
        with pytest.raises(CallerIdentityError, match=msg):
            decode_caller_user_id(req(header))

    def test_non_integer_subject(self):
        with pytest.raises(CallerIdentityError, match="subject"):
            decode_caller_user_id(req(f"Bearer {token(sub='not-a-number')}"))


class TestInstalledHook:
    def _app(self, db, world, **kwargs):
        app = Quart("ca-edges")
        app.config["dal"] = db
        app.config["async_dal"] = world.adal
        bp = Blueprint("things", __name__)

        @bp.route("/health")
        async def health():
            return {"ok": True}

        @bp.route("/c/<int:community_id>/things", methods=["GET", "POST"])
        async def things(community_id):
            return {"community": community_id}

        @bp.route("/free")
        async def free():
            return {"free": True}

        install_community_scoped_auth(bp, **kwargs)
        app.register_blueprint(bp)
        return app

    def _hdr(self, sub="1", tenant="acme", roles=None):
        return {"Authorization": f"Bearer {token(sub=sub, tenant=tenant, roles=roles)}"}

    async def test_exempt_path_needs_no_token(self, db, world):
        app = self._app(db, world, exempt_paths=frozenset({"/health"}))
        assert (await app.test_client().get("/health")).status_code == 200
        assert (await app.test_client().get("/free")).status_code == 401  # others still gated

    async def test_member_reads_but_cannot_write(self, db, world):
        client = self._app(db, world).test_client()
        assert (await client.get(f"/c/{world.community}/things", headers=self._hdr("1"))).status_code == 200
        assert (await client.post(f"/c/{world.community}/things", headers=self._hdr("1"))).status_code == 403

    async def test_admin_can_write(self, db, world):
        client = self._app(db, world).test_client()
        assert (await client.post(f"/c/{world.community}/things", headers=self._hdr("2"))).status_code == 200

    async def test_non_member_forbidden(self, db, world):
        client = self._app(db, world).test_client()
        assert (await client.get(f"/c/{world.community}/things", headers=self._hdr("77"))).status_code == 403

    async def test_other_tenants_community_forbidden_even_for_its_member(self, db, world):
        db.community_members.insert(community_id=world.other_community, user_id="1", role="member", is_active=True)
        db.commit()
        client = self._app(db, world).test_client()
        resp = await client.get(f"/c/{world.other_community}/things", headers=self._hdr("1", tenant="acme"))
        assert resp.status_code == 403

    async def test_non_integer_subject_is_401(self, db, world):
        client = self._app(db, world).test_client()
        resp = await client.get(f"/c/{world.community}/things", headers=self._hdr("abc"))
        assert resp.status_code == 401

    async def test_route_without_community_param_only_needs_valid_tenant_token(self, db, world):
        client = self._app(db, world).test_client()
        assert (await client.get("/free", headers=self._hdr("12345"))).status_code == 200

    async def test_bad_token_and_bad_tenant(self, db, world):
        client = self._app(db, world).test_client()
        assert (await client.get("/free", headers={"Authorization": "Bearer nope"})).status_code == 401
        assert (await client.get("/free", headers={"Authorization": "Basic x"})).status_code == 401
        assert (await client.get("/free", headers=self._hdr("1", tenant="ghost"))).status_code == 403

    async def test_custom_admin_methods(self, db, world):
        client = self._app(db, world, admin_methods=frozenset({"GET"})).test_client()
        assert (await client.get(f"/c/{world.community}/things", headers=self._hdr("1"))).status_code == 403
        assert (await client.get(f"/c/{world.community}/things", headers=self._hdr("2"))).status_code == 200
