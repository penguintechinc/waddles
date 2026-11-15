"""Global identity derives from tenancy (the `system` tenant) -- there is no is_super_admin flag.

Covers the helper (`services.global_identity`), token issuance (scopes/roles claims granted iff
system-tenant admin), `auth/me` + login `isSuperAdmin` (the API field survives, derived), the
tenant-owner-of-another-tenant negative, fail-closed edges, and the `default` catch-all slug.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from flask_core.auth import DEFAULT_TENANT_SLUG, verify_jwt_token
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.auth import auth_bp
from services import auth_service
from services.global_identity import (
    SYSTEM_TENANT_SLUG,
    add_tenant_admin_sync,
    add_tenant_member_sync,
    ensure_system_tenant,
    global_admin_ids,
    grant_global_admin_sync,
    is_global_admin,
    is_global_admin_sync,
    is_global_user,
    is_global_user_sync,
    revoke_global_admin_sync,
)
from tests.conftest import TENANT_SLUG, make_user_token, seed_super_admin
from tests.test_v1_auth_blueprint import _seed_user, _test_config

SECRET = "change-me-in-production"


@pytest.fixture
def app(auth_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(auth_bp)
    quart_app.config["dal"] = auth_db.dal
    quart_app.config["async_dal"] = auth_db
    quart_app.config["HUB_API_CONFIG"] = _test_config()
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


async def _login(client: Any, email: str, tenant: str = TENANT_SLUG) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "hunter22", "tenantSlug": tenant},
    )
    assert response.status_code == 200, await response.get_json()
    body: dict[str, Any] = await response.get_json()
    return body


def _scopes(body: dict[str, Any]) -> set[str]:
    claims = verify_jwt_token(body["token"], SECRET)
    assert claims is not None
    return set(claims["scope"].split())


class TestHelper:
    def test_no_system_tenant_is_nobody_global(self, auth_db: Any) -> None:
        uid = _seed_user(auth_db, email="a@example.com")
        assert is_global_admin_sync(auth_db.dal, uid) is False
        assert is_global_user_sync(auth_db.dal, uid) is False
        assert global_admin_ids(auth_db.dal) == set()

    def test_member_is_global_user_not_admin(self, auth_db: Any) -> None:
        dal = auth_db.dal
        uid = _seed_user(auth_db, email="a@example.com")
        sys_id = ensure_system_tenant(dal)
        add_tenant_member_sync(dal, tenant_id=sys_id, user_id=uid)
        dal.commit()
        assert is_global_user_sync(dal, uid) is True
        assert is_global_admin_sync(dal, uid) is False

    def test_system_tenant_admin_is_global_admin_and_user(self, auth_db: Any) -> None:
        dal = auth_db.dal
        uid = _seed_user(auth_db, email="a@example.com")
        grant_global_admin_sync(dal, uid)
        dal.commit()
        assert is_global_admin_sync(dal, uid) is True
        assert is_global_user_sync(dal, uid) is True
        assert global_admin_ids(dal) == {uid}

    def test_admin_of_another_tenant_is_not_global(self, auth_db: Any) -> None:
        dal = auth_db.dal
        uid = _seed_user(auth_db, email="a@example.com")
        ensure_system_tenant(dal)
        acme = dal(dal.tenants.slug == TENANT_SLUG).select().first().id
        add_tenant_admin_sync(dal, tenant_id=acme, user_id=uid, role="tenant-owner")
        add_tenant_member_sync(dal, tenant_id=acme, user_id=uid)
        dal.commit()
        assert is_global_admin_sync(dal, uid) is False
        assert is_global_user_sync(dal, uid) is False

    def test_inactive_and_missing_users_fail_closed(self, auth_db: Any) -> None:
        dal = auth_db.dal
        uid = _seed_user(auth_db, email="a@example.com")
        grant_global_admin_sync(dal, uid)
        dal(dal.hub_users.id == uid).update(is_active=False)
        dal.commit()
        assert is_global_admin_sync(dal, uid) is False
        assert is_global_user_sync(dal, uid) is False
        assert is_global_admin_sync(dal, 999999) is False
        assert global_admin_ids(dal) == set()

    def test_grant_is_idempotent_and_revoke_keeps_membership(self, auth_db: Any) -> None:
        dal = auth_db.dal
        uid = _seed_user(auth_db, email="a@example.com")
        first = grant_global_admin_sync(dal, uid)
        assert grant_global_admin_sync(dal, uid) == first
        dal.commit()
        assert dal(dal.tenant_admins.user_id == uid).count() == 1
        assert dal(dal.tenant_members.user_id == uid).count() == 1
        revoke_global_admin_sync(dal, uid)
        dal.commit()
        assert is_global_admin_sync(dal, uid) is False
        assert is_global_user_sync(dal, uid) is True

    async def test_async_variants_match_sync(self, auth_db: Any) -> None:
        dal = auth_db.dal
        admin = _seed_user(auth_db, email="a@example.com")
        plain = _seed_user(auth_db, email="b@example.com")
        grant_global_admin_sync(dal, admin)
        dal.commit()
        assert await is_global_admin(auth_db, dal, user_id=admin) is True
        assert await is_global_admin(auth_db, dal, user_id=plain) is False
        assert await is_global_user(auth_db, dal, user_id=admin) is True
        assert await is_global_user(auth_db, dal, user_id=plain) is False

    def test_seed_super_admin_fixture_makes_system_admin(self, auth_db: Any) -> None:
        seed_super_admin(auth_db, user_id=1, bootstrap=True)
        assert is_global_admin_sync(auth_db.dal, 1) is True
        assert auth_db.dal(auth_db.dal.tenants.slug == SYSTEM_TENANT_SLUG).count() == 1


class TestTokenIssuance:
    async def test_system_admin_token_carries_super_admin_scopes_and_role(
        self, client: Any, auth_db: Any
    ) -> None:
        uid = _seed_user(auth_db, email="root@example.com", password="hunter22")
        grant_global_admin_sync(auth_db.dal, uid)
        auth_db.dal.commit()
        body = await _login(client, "root@example.com")
        scopes = _scopes(body)
        assert {"users:admin", "*:admin", "settings:write"} <= scopes
        claims = verify_jwt_token(body["token"], SECRET)
        assert claims is not None and "super_admin" in claims["roles"]
        assert body["user"]["isSuperAdmin"] is True

    async def test_non_system_user_token_has_no_super_admin_scopes(
        self, client: Any, auth_db: Any
    ) -> None:
        _seed_user(auth_db, email="plain@example.com")
        body = await _login(client, "plain@example.com")
        assert "users:admin" not in _scopes(body)
        assert "*:admin" not in _scopes(body)
        assert body["user"]["isSuperAdmin"] is False

    async def test_system_member_without_admin_gets_no_super_admin_scopes(
        self, client: Any, auth_db: Any
    ) -> None:
        uid = _seed_user(auth_db, email="member@example.com")
        add_tenant_member_sync(
            auth_db.dal, tenant_id=ensure_system_tenant(auth_db.dal), user_id=uid
        )
        auth_db.dal.commit()
        body = await _login(client, "member@example.com")
        assert "users:admin" not in _scopes(body)
        assert body["user"]["isSuperAdmin"] is False

    async def test_other_tenant_owner_gets_tenant_bundle_but_never_users_admin(
        self, client: Any, auth_db: Any
    ) -> None:
        dal = auth_db.dal
        uid = _seed_user(auth_db, email="owner@example.com")
        acme = dal(dal.tenants.slug == TENANT_SLUG).select().first().id
        dal.tenant_admins.insert(
            tenant_id=acme, user_id=uid, role="tenant-owner", created_at=datetime.now(UTC)
        )
        dal.commit()
        scopes = _scopes(await _login(client, "owner@example.com"))
        assert "tenant:admin" in scopes
        assert "users:admin" not in scopes

    async def test_demotion_lands_on_next_token_mint(self, auth_db: Any) -> None:
        """`_scopes_for` re-reads the database every mint: a revoked admin loses the bundle."""
        dal = auth_db.dal
        uid = _seed_user(auth_db, email="x@example.com")
        grant_global_admin_sync(dal, uid)
        dal.commit()
        user = auth_service.SessionUser(id=uid, email="x@example.com", username="x")
        _, scopes = await auth_service._scopes_for(auth_db, dal, user=user, tenant_id=None)
        assert "users:admin" in scopes
        revoke_global_admin_sync(dal, uid)
        dal.commit()
        _, scopes = await auth_service._scopes_for(auth_db, dal, user=user, tenant_id=None)
        assert "users:admin" not in scopes

    async def test_forged_session_user_flag_cannot_grant_scopes(self, auth_db: Any) -> None:
        """The `SessionUser.is_super_admin` display field is never trusted for scope expansion."""
        uid = _seed_user(auth_db, email="x@example.com")
        user = auth_service.SessionUser(
            id=uid, email="x@example.com", username="x", is_super_admin=True
        )
        _, scopes = await auth_service._scopes_for(auth_db, auth_db.dal, user=user, tenant_id=None)
        assert "users:admin" not in scopes


class TestMe:
    async def test_me_is_super_admin_true_for_bootstrap_system_admin(
        self, client: Any, auth_db: Any
    ) -> None:
        seed_super_admin(auth_db, user_id=1, bootstrap=True)
        token = make_user_token(user_id=1, tenant=TENANT_SLUG)
        response = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert (await response.get_json())["user"]["isSuperAdmin"] is True

    async def test_me_is_super_admin_false_for_plain_user(self, client: Any, auth_db: Any) -> None:
        uid = _seed_user(auth_db, email="p@example.com")
        token = make_user_token(user_id=uid, tenant=TENANT_SLUG)
        response = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert (await response.get_json())["user"]["isSuperAdmin"] is False


class TestDefaultTenantSlug:
    def test_catch_all_slug_is_default(self) -> None:
        assert DEFAULT_TENANT_SLUG == "default"

    def test_hub_api_config_default_and_login_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from config import HubAPIConfig

        monkeypatch.delenv("DEFAULT_TENANT_SLUG", raising=False)
        monkeypatch.setenv("SECRET_KEY", "x" * 40)
        cfg = HubAPIConfig.from_env()
        assert cfg.default_tenant_slug == "default"

    async def test_login_without_tenant_resolves_the_default_tenant(
        self, client: Any, auth_db: Any
    ) -> None:
        dal = auth_db.dal
        dal.tenants.insert(slug="default", display_name="Waddles", is_global=True, is_active=True)
        dal.commit()
        _seed_user(auth_db, email="d@example.com")
        response = await client.post(
            "/api/v1/auth/login", json={"email": "d@example.com", "password": "hunter22"}
        )
        assert response.status_code == 200
        claims = verify_jwt_token((await response.get_json())["token"], SECRET)
        assert claims is not None and claims["tenant"] == "default"
