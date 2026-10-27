"""Tenant middleware + scoping edge cases (real Quart app, real JWTs, real pydal SQL).

Complements test_tenancy.py (cross-tenant isolation of ``tenant_scoped`` and
``resolve_tenant_context``) with the HTTP-layer contract: ``tenant_middleware`` must
resolve the tenant from the verified token BEFORE the handler runs, and every failure
mode must be a 401/403, never a handler call.
"""

from types import SimpleNamespace

import pytest
from pydal import DAL, Field
from quart import Quart, request

from flask_core.auth import DEFAULT_TENANT_SLUG, create_jwt_token
from flask_core.tenancy import (
    TenantContext,
    TenantIsolationError,
    get_tenant_context,
    resolve_tenant_context,
    tenant_middleware,
    tenant_scoped,
)

SECRET = "test-secret-key-not-for-production-use-only"


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", SECRET)


@pytest.fixture
def db():
    dal = DAL("sqlite:memory")
    dal.define_table("tenants", Field("slug", unique=True), Field("is_active", "boolean", default=True))
    dal.define_table("communities", Field("tenant_id", "reference tenants"), Field("name"))
    dal.define_table("notes", Field("tenant_id", "reference tenants"), Field("body"))
    dal.define_table("posts", Field("community_id", "reference communities"), Field("title"))
    dal.define_table("orphans", Field("label"))
    dal.tenants.insert(slug=DEFAULT_TENANT_SLUG, is_active=True)
    dal.tenants.insert(slug="acme", is_active=True)
    dal.tenants.insert(slug="dormant", is_active=False)
    return dal


def make_app(dal) -> Quart:
    app = Quart("tenancy-mw")
    app.config["dal"] = dal
    calls: list[str] = []
    app.config["calls"] = calls

    @app.route("/who")
    @tenant_middleware
    async def who():
        calls.append("handler")
        ctx = get_tenant_context(request)
        return {"id": ctx.tenant_id, "slug": ctx.tenant_slug, "default": ctx.is_default}

    return app


def bearer(tenant: str | None = "acme") -> dict[str, str]:
    tok = create_jwt_token("u1", "alice", "a@e.invalid", [], SECRET, tenant=tenant or "x")
    return {"Authorization": f"Bearer {tok}"}


class TestTenantMiddleware:
    async def test_resolves_tenant_and_publishes_context(self, db):
        app = make_app(db)
        resp = await app.test_client().get("/who", headers=bearer("acme"))
        body = await resp.get_json()
        assert resp.status_code == 200
        assert body["slug"] == "acme" and body["default"] is False
        assert body["id"] == db(db.tenants.slug == "acme").select().first().id
        assert app.config["calls"] == ["handler"]

    async def test_default_tenant_flagged(self, db):
        resp = await make_app(db).test_client().get("/who", headers=bearer(DEFAULT_TENANT_SLUG))
        assert (await resp.get_json())["default"] is True

    @pytest.mark.parametrize("headers", [{}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer"}])
    async def test_missing_or_non_bearer_auth_is_401(self, db, headers):
        app = make_app(db)
        resp = await app.test_client().get("/who", headers=headers)
        assert resp.status_code == 401
        assert app.config["calls"] == []

    async def test_invalid_token_is_401(self, db):
        app = make_app(db)
        resp = await app.test_client().get("/who", headers={"Authorization": "Bearer garbage"})
        assert resp.status_code == 401
        assert app.config["calls"] == []

    async def test_token_signed_with_wrong_key_is_401(self, db):
        bad = create_jwt_token("u1", "a", "a@e.invalid", [], "k" * 40, tenant="acme")
        app = make_app(db)
        resp = await app.test_client().get("/who", headers={"Authorization": f"Bearer {bad}"})
        assert resp.status_code == 401 and app.config["calls"] == []

    @pytest.mark.parametrize(
        "tenant,fragment",
        [("ghost", "does not exist"), ("dormant", "inactive")],
    )
    async def test_unknown_or_inactive_tenant_is_403_and_handler_never_runs(self, db, tenant, fragment):
        app = make_app(db)
        resp = await app.test_client().get("/who", headers=bearer(tenant))
        body = await resp.get_json()
        assert resp.status_code == 403
        assert fragment in body["error"]["message"]
        assert app.config["calls"] == []


class TestGetTenantContext:
    def test_present_and_absent(self):
        ctx = TenantContext(tenant_id=1, tenant_slug="a")
        assert get_tenant_context(SimpleNamespace(tenant_context=ctx)) is ctx
        assert get_tenant_context(SimpleNamespace()) is None

    def test_context_is_immutable(self):
        ctx = TenantContext(tenant_id=1, tenant_slug="a")
        with pytest.raises(Exception):
            ctx.tenant_id = 2  # type: ignore[misc]


class TestResolveTenantContext:
    async def test_missing_claim_is_hard_error(self, db):
        with pytest.raises(TenantIsolationError, match="no tenant claim"):
            await resolve_tenant_context({}, db)
        with pytest.raises(TenantIsolationError, match="no tenant claim"):
            await resolve_tenant_context({"tenant": ""}, db)


class TestTenantScopedShapes:
    def test_direct_tenant_id_column(self, db):
        acme = db(db.tenants.slug == "acme").select().first().id
        default = db(db.tenants.slug == DEFAULT_TENANT_SLUG).select().first().id
        db.notes.insert(tenant_id=acme, body="mine")
        db.notes.insert(tenant_id=default, body="theirs")
        ctx = TenantContext(tenant_id=acme, tenant_slug="acme")
        rows = db(tenant_scoped(db.notes.id > 0, ctx)).select()
        assert [r.body for r in rows] == ["mine"]

    def test_one_hop_via_community(self, db):
        acme = db(db.tenants.slug == "acme").select().first().id
        other = db(db.tenants.slug == DEFAULT_TENANT_SLUG).select().first().id
        c_mine = db.communities.insert(tenant_id=acme, name="m")
        c_other = db.communities.insert(tenant_id=other, name="o")
        db.posts.insert(community_id=c_mine, title="keep")
        db.posts.insert(community_id=c_other, title="hide")
        rows = db(tenant_scoped(db.posts.id > 0, TenantContext(acme, "acme"))).select()
        assert [r.title for r in rows] == ["keep"]

    def test_and_chained_query_resolves_leftmost_table(self, db):
        acme = db(db.tenants.slug == "acme").select().first().id
        db.notes.insert(tenant_id=acme, body="x")
        db.notes.insert(tenant_id=acme, body="y")
        chained = (db.notes.body == "x") & (db.notes.id > 0)
        rows = db(tenant_scoped(chained, TenantContext(acme, "acme"))).select()
        assert [r.body for r in rows] == ["x"]

    def test_table_without_tenant_path_raises_loudly(self, db):
        with pytest.raises(TenantIsolationError, match="neither tenant_id nor community_id"):
            tenant_scoped(db.orphans.id > 0, TenantContext(1, "a"))

    def test_unresolvable_query_raises(self):
        with pytest.raises(TenantIsolationError, match="could not resolve a table"):
            tenant_scoped("not-a-query", TenantContext(1, "a"))  # type: ignore[arg-type]
