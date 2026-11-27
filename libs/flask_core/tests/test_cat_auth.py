"""CAT (`wdl_c_...`) bearer authentication tests for `tenant_middleware` and `require_scope`."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from pydal import DAL, Field
from quart import Quart, request

from flask_core.auth import DEFAULT_TENANT_SLUG
from flask_core.authz import get_authz_decision, require_scope
from flask_core.cat_auth import (
    CAT_SELF_ONLY,
    allow_cat,
    cat_bound_community,
    from_json_body,
    from_view_arg,
    get_cat_principal,
)
from flask_core.tenancy import CatPrincipal, get_tenant_context, tenant_middleware


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

    c1 = dal.communities.insert(
        tenant_id=t_default, name="Default Community", is_active=True
    )
    c2 = dal.communities.insert(
        tenant_id=t_default, name="Other Same-Tenant", is_active=True
    )
    c3 = dal.communities.insert(tenant_id=t_acme, name="Acme Community", is_active=True)
    c4 = dal.communities.insert(
        tenant_id=t_inactive, name="Dormant Community", is_active=True
    )

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
    @allow_cat(scopes=CAT_SELF_ONLY, community_from=cat_bound_community)
    async def my_communities():
        ctx = get_tenant_context(request)
        return {
            "tenant_slug": ctx.tenant_slug,
            "cat_community_id": ctx.cat_community_id,
        }

    @app.route("/admin/<int:community_id>/music")
    @tenant_middleware
    @allow_cat(
        scopes=["community.music:read"], community_from=from_view_arg("community_id")
    )
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
    @allow_cat(
        scopes=["community.tokens:admin"], community_from=from_view_arg("community_id")
    )
    @require_scope("community.tokens:admin")
    async def get_tokens(community_id: int):
        return {"community_id": community_id}

    # Community id under a NON-standard param name -- the old `community_id`-only check missed it.
    @app.route("/guilds/<int:cid>/music")
    @tenant_middleware
    @allow_cat(scopes=["community.music:read"], community_from=from_view_arg("cid"))
    async def guild_music(cid: int):
        return {"cid": cid}

    # Community id in the JSON body.
    @app.route("/music/queue", methods=["POST"])
    @tenant_middleware
    @allow_cat(
        scopes=["community.music:write"], community_from=from_json_body("community_id")
    )
    async def queue_song():
        cat = get_cat_principal(request)
        return {"community_id": cat.community_id if cat else None}

    # Opted in but forgot to declare where the community comes from -> default rejects.
    @app.route("/opted-in-no-extractor")
    @tenant_middleware
    @allow_cat(scopes=["community.music:read"])
    async def no_extractor():
        return {"ok": True}

    # NOT opted in: user-level route a CAT must never reach.
    @app.route("/user/account")
    @tenant_middleware
    async def user_account():
        return {"ok": True}

    # NOT opted in, require_scope only (no tenant_middleware): CAT still refused.
    @app.route("/scope-only")
    @require_scope("community.music:read")
    async def scope_only():
        return {"ok": True}

    # allow_cat placed ABOVE tenant_middleware never reaches it -> fail closed.
    @app.route("/misordered")
    @allow_cat(scopes=["community.music:read"], community_from=cat_bound_community)
    @tenant_middleware
    async def misordered():
        return {"ok": True}

    return app


def _bearer(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


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

    async def test_valid_cat_populates_cat_community_id_on_non_community_route(
        self, cat_db
    ):
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


class TestCatFailClosed:
    """Hotfix: CATs are opt-in per route, scope-checked, and community-bound whatever the source."""

    async def test_cat_on_non_opted_in_route_is_401(self, cat_db):
        client = _make_app(cat_db["dal"]).test_client()
        resp = await client.get("/user/account", headers=_bearer(cat_db["valid_tok"]))
        assert resp.status_code == 401

    async def test_cat_on_scope_only_route_is_403(self, cat_db):
        client = _make_app(cat_db["dal"]).test_client()
        resp = await client.get("/scope-only", headers=_bearer(cat_db["valid_tok"]))
        assert resp.status_code == 403

    async def test_misordered_opt_in_fails_closed(self, cat_db):
        client = _make_app(cat_db["dal"]).test_client()
        resp = await client.get("/misordered", headers=_bearer(cat_db["valid_tok"]))
        assert resp.status_code == 401

    async def test_opted_in_without_extractor_rejects(self, cat_db):
        client = _make_app(cat_db["dal"]).test_client()
        resp = await client.get(
            "/opted-in-no-extractor", headers=_bearer(cat_db["valid_tok"])
        )
        assert resp.status_code == 403

    async def test_opted_in_route_without_scope_is_403(self, cat_db):
        client = _make_app(cat_db["dal"]).test_client()
        # acme token holds only community.music:read; route needs community.music:write
        resp = await client.post(
            "/music/queue",
            json={"community_id": cat_db["c3"]},
            headers=_bearer(cat_db["acme_tok"]),
        )
        assert resp.status_code == 403

    async def test_alternate_path_param_other_community_is_403(self, cat_db):
        client = _make_app(cat_db["dal"]).test_client()
        bad = await client.get(
            f"/guilds/{cat_db['c2']}/music", headers=_bearer(cat_db["valid_tok"])
        )
        assert bad.status_code == 403
        ok = await client.get(
            f"/guilds/{cat_db['c1']}/music", headers=_bearer(cat_db["valid_tok"])
        )
        assert ok.status_code == 200

    async def test_body_other_community_is_403(self, cat_db):
        client = _make_app(cat_db["dal"]).test_client()
        bad = await client.post(
            "/music/queue",
            json={"community_id": cat_db["c2"]},
            headers=_bearer(cat_db["valid_tok"]),
        )
        assert bad.status_code == 403
        missing = await client.post(
            "/music/queue", json={}, headers=_bearer(cat_db["valid_tok"])
        )
        assert missing.status_code == 403
        stringy = await client.post(
            "/music/queue",
            json={"community_id": str(cat_db["c1"])},
            headers=_bearer(cat_db["valid_tok"]),
        )
        assert stringy.status_code == 403
        ok = await client.post(
            "/music/queue",
            json={"community_id": cat_db["c1"]},
            headers=_bearer(cat_db["valid_tok"]),
        )
        assert ok.status_code == 200
        assert (await ok.get_json())["community_id"] == cat_db["c1"]

    async def test_path_param_other_community_is_403(self, cat_db):
        client = _make_app(cat_db["dal"]).test_client()
        resp = await client.get(
            f"/admin/{cat_db['c2']}/music", headers=_bearer(cat_db["valid_tok"])
        )
        assert resp.status_code == 403

    def test_principal_carries_no_user_id(self):
        assert "created_by_user_id" not in CatPrincipal.__slots__
        assert "user_id" not in CatPrincipal.__slots__

    def test_allow_cat_refuses_empty_scopes(self):
        with pytest.raises(ValueError):
            allow_cat(scopes=[])
        with pytest.raises(ValueError):
            allow_cat(scopes=[" "])

    async def test_cat_decision_published_for_audit(self, cat_db):
        client = _make_app(cat_db["dal"]).test_client()
        resp = await client.get(
            f"/admin/{cat_db['c1']}/music", headers=_bearer(cat_db["valid_tok"])
        )
        assert resp.status_code == 200
        assert (await resp.get_json())["subject"].startswith("cat:")
