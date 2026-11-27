"""CAT (`wdl_c_...`) bearer authentication tests for `tenant_middleware` and `require_scope`."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from pydal import DAL, Field
from quart import Quart, request

from flask_core.auth import DEFAULT_TENANT_SLUG
from flask_core.authz import get_authz_decision, require_scope
from flask_core.tenancy import get_tenant_context, tenant_middleware


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@pytest.fixture
def cat_db():
    dal = DAL("sqlite:memory")
    dal.define_table(
        "tenants",
        Field("slug", unique=True),
        Field("is_active", "boolean", default=True),
    )
    dal.define_table(
        "communities",
        Field("tenant_id", "reference tenants"),
        Field("name"),
        Field("is_active", "boolean", default=True),
    )
    dal.define_table(
        "community_access_tokens",
        Field("community_id", "integer", notnull=True),
        Field("created_by_user_id", "integer"),
        Field("name", "string"),
        Field("token_hash", "string", notnull=True),
        Field("scopes", "json"),
        Field("expires_at", "datetime"),
        Field("created_at", "datetime"),
        Field("is_revoked", "boolean", default=False),
    )

    t_default = dal.tenants.insert(slug=DEFAULT_TENANT_SLUG, is_active=True)
    t_acme = dal.tenants.insert(slug="acme", is_active=True)
    t_inactive = dal.tenants.insert(slug="dormant", is_active=False)

    c1 = dal.communities.insert(tenant_id=t_default, name="Default Community", is_active=True)
    c2 = dal.communities.insert(tenant_id=t_default, name="Other Same-Tenant", is_active=True)
    c3 = dal.communities.insert(tenant_id=t_acme, name="Acme Community", is_active=True)
    c4 = dal.communities.insert(tenant_id=t_inactive, name="Dormant Community", is_active=True)

    valid_tok = "wdl_c_" + ("a" * 48)
    revoked_tok = "wdl_c_" + ("b" * 48)
    expired_tok = "wdl_c_" + ("c" * 48)
    acme_tok = "wdl_c_" + ("d" * 48)
    dormant_tok = "wdl_c_" + ("e" * 48)
    wildcard_tok = "wdl_c_" + ("f" * 48)

    dal.community_access_tokens.insert(
        community_id=c1,
        created_by_user_id=10,
        name="valid",
        token_hash=_sha256(valid_tok),
        scopes=["community.music:read", "community.music:write"],
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        created_at=datetime.now(timezone.utc),
        is_revoked=False,
    )
    dal.community_access_tokens.insert(
        community_id=c1,
        created_by_user_id=10,
        name="revoked",
        token_hash=_sha256(revoked_tok),
        scopes=["community.music:read"],
        expires_at=None,
        created_at=datetime.now(timezone.utc),
        is_revoked=True,
    )
    dal.community_access_tokens.insert(
        community_id=c1,
        created_by_user_id=10,
        name="expired",
        token_hash=_sha256(expired_tok),
        scopes=["community.music:read"],
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        created_at=datetime.now(timezone.utc),
        is_revoked=False,
    )
    dal.community_access_tokens.insert(
        community_id=c3,
        created_by_user_id=20,
        name="acme",
        token_hash=_sha256(acme_tok),
        scopes=["community.music:read"],
        expires_at=None,
        created_at=datetime.now(timezone.utc),
        is_revoked=False,
    )
    dal.community_access_tokens.insert(
        community_id=c4,
        created_by_user_id=30,
        name="dormant",
        token_hash=_sha256(dormant_tok),
        scopes=["community.music:read"],
        expires_at=None,
        created_at=datetime.now(timezone.utc),
        is_revoked=False,
    )
    dal.community_access_tokens.insert(
        community_id=c1,
        created_by_user_id=10,
        name="wildcard",
        token_hash=_sha256(wildcard_tok),
        scopes=["*:read"],
        expires_at=None,
        created_at=datetime.now(timezone.utc),
        is_revoked=False,
    )
    dal.commit()

    return {
        "dal": dal,
        "c1": int(c1),
        "c2": int(c2),
        "c3": int(c3),
        "valid_tok": valid_tok,
        "revoked_tok": revoked_tok,
        "expired_tok": expired_tok,
        "acme_tok": acme_tok,
        "dormant_tok": dormant_tok,
        "wildcard_tok": wildcard_tok,
    }


def _make_app(dal) -> Quart:
    app = Quart("cat-auth-test")
    app.config["dal"] = dal

    @app.route("/communities/my")
    @tenant_middleware
    async def my_communities():
        ctx = get_tenant_context(request)
        return {
            "tenant_slug": ctx.tenant_slug,
            "cat_community_id": ctx.cat_community_id,
        }

    @app.route("/admin/<int:community_id>/music")
    @tenant_middleware
    @require_scope("community.music:read")
    async def get_music(community_id: int):
        ctx = get_tenant_context(request)
        decision = get_authz_decision(request)
        return {
            "community_id": community_id,
            "tenant_slug": ctx.tenant_slug,
            "subject": decision.subject if decision else None,
        }

    @app.route("/admin/<int:community_id>/tokens")
    @tenant_middleware
    @require_scope("community.tokens:admin")
    async def get_tokens(community_id: int):
        return {"community_id": community_id}

    return app


class TestCatBearerAuth:
    async def test_valid_cat_succeeds_on_bound_community(self, cat_db):
        app = _make_app(cat_db["dal"])
        client = app.test_client()
        resp = await client.get(
            f"/admin/{cat_db['c1']}/music",
            headers={"Authorization": f"Bearer {cat_db['valid_tok']}"},
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["community_id"] == cat_db["c1"]
        assert data["tenant_slug"] == DEFAULT_TENANT_SLUG
        assert data["subject"].startswith("cat:")

    async def test_valid_cat_populates_cat_community_id_on_non_community_route(self, cat_db):
        app = _make_app(cat_db["dal"])
        client = app.test_client()
        resp = await client.get(
            "/communities/my",
            headers={"Authorization": f"Bearer {cat_db['acme_tok']}"},
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["tenant_slug"] == "acme"
        assert data["cat_community_id"] == cat_db["c3"]

    async def test_wildcard_scope_covers_read_but_not_admin(self, cat_db):
        app = _make_app(cat_db["dal"])
        client = app.test_client()
        ok = await client.get(
            f"/admin/{cat_db['c1']}/music",
            headers={"Authorization": f"Bearer {cat_db['wildcard_tok']}"},
        )
        assert ok.status_code == 200

        denied = await client.get(
            f"/admin/{cat_db['c1']}/tokens",
            headers={"Authorization": f"Bearer {cat_db['wildcard_tok']}"},
        )
        assert denied.status_code == 403

    async def test_insufficient_scope_is_403(self, cat_db):
        app = _make_app(cat_db["dal"])
        client = app.test_client()
        resp = await client.get(
            f"/admin/{cat_db['c1']}/tokens",
            headers={"Authorization": f"Bearer {cat_db['valid_tok']}"},
        )
        assert resp.status_code == 403

    async def test_unknown_cat_is_401(self, cat_db):
        app = _make_app(cat_db["dal"])
        client = app.test_client()
        resp = await client.get(
            f"/admin/{cat_db['c1']}/music",
            headers={"Authorization": "Bearer wdl_c_" + ("0" * 48)},
        )
        assert resp.status_code == 401

    async def test_revoked_cat_is_401(self, cat_db):
        app = _make_app(cat_db["dal"])
        client = app.test_client()
        resp = await client.get(
            f"/admin/{cat_db['c1']}/music",
            headers={"Authorization": f"Bearer {cat_db['revoked_tok']}"},
        )
        assert resp.status_code == 401

    async def test_expired_cat_is_401(self, cat_db):
        app = _make_app(cat_db["dal"])
        client = app.test_client()
        resp = await client.get(
            f"/admin/{cat_db['c1']}/music",
            headers={"Authorization": f"Bearer {cat_db['expired_tok']}"},
        )
        assert resp.status_code == 401

    async def test_wrong_community_same_tenant_is_403(self, cat_db):
        app = _make_app(cat_db["dal"])
        client = app.test_client()
        resp = await client.get(
            f"/admin/{cat_db['c2']}/music",
            headers={"Authorization": f"Bearer {cat_db['valid_tok']}"},
        )
        assert resp.status_code == 403

    async def test_wrong_tenant_community_is_403(self, cat_db):
        app = _make_app(cat_db["dal"])
        client = app.test_client()
        resp = await client.get(
            f"/admin/{cat_db['c1']}/music",
            headers={"Authorization": f"Bearer {cat_db['acme_tok']}"},
        )
        assert resp.status_code == 403

    async def test_inactive_tenant_cat_is_403(self, cat_db):
        app = _make_app(cat_db["dal"])
        client = app.test_client()
        resp = await client.get(
            "/communities/my",
            headers={"Authorization": f"Bearer {cat_db['dormant_tok']}"},
        )
        assert resp.status_code == 403
