"""Tests for penguind <-> waddles parity endpoints and auth flows.

Covers:
1. `GET /api/v1/communities/my` with both `wdl_c_` CAT and user JWT (public UUIDs).
2. `POST /api/v1/auth/refresh` with JSON body `{"refresh_token": ...}` + cookie fallback.
3. Desktop OAuth deep-link (`waddles://oauth/callback`) allowlist, open-redirect rejection,
   and single-use `POST /api/v1/auth/exchange` without browser cookie.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import pytest
from flask_core.tenancy import DEFAULT_TENANT_SLUG
from quart import Quart
from quart_schema import QuartSchema

from app import bridge_session_cookie_to_bearer
from blueprints.v1.auth import auth_bp
from blueprints.v1.community_profile import community_profile_bp
from config import HubAPIConfig
from services import access_token_service, oauth_service
from services.community_public_id import install_community_public_ids, lookup_public_id
from services.schema import bind_support_token_tables
from tests.conftest import TENANT_SLUG, make_user_token


def _test_config() -> HubAPIConfig:
    return HubAPIConfig(
        module_name="hub-api-test",
        module_version="0.0.0-test",
        module_port=8204,
        grpc_port=50204,
        database_url="sqlite:memory",
        database_read_replica_url=None,
        db_pool_size=1,
        db_max_retries=1,
        db_retry_delay=1,
        secret_key="change-me-in-production",
        default_tenant_slug=DEFAULT_TENANT_SLUG,
        posthog_api_key=None,
        posthog_host="https://license.penguintech.io",
        license_server_url="https://license.penguintech.io",
        identity_callback_base_url="http://localhost:8204",
        frontend_origin="https://waddles.app",
        log_level="INFO",
    )


def _make_app(auth_db: Any) -> Quart:
    app = Quart("penguind-parity-test")
    app.config["async_dal"] = auth_db
    app.config["dal"] = auth_db.dal
    app.config["HUB_API_CONFIG"] = _test_config()
    install_community_public_ids(app)
    app.before_request(bridge_session_cookie_to_bearer)
    QuartSchema(app)
    app.register_blueprint(auth_bp)
    app.register_blueprint(community_profile_bp)
    return app


def _seed_user(
    auth_db: Any, *, email: str = "desktop@example.com", password: str | None = None
) -> int:
    dal = auth_db.dal
    pw_hash = (
        bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode() if password else None
    )
    uid = dal.hub_users.insert(
        email=email,
        username="desktopuser",
        display_name="Desktop User",
        password_hash=pw_hash,
        email_verified=True,
        is_active=True,
        created_at=datetime.now(UTC),
    )
    dal.commit()
    return int(uid)


def _seed_community(auth_db: Any, *, name: str, tenant_slug: str = TENANT_SLUG) -> int:
    dal = auth_db.dal
    tenant = dal(dal.tenants.slug == tenant_slug).select().first()
    assert tenant is not None
    cid = dal.communities.insert(
        name=name,
        tenant_id=int(tenant.id),
    )
    dal.commit()
    return int(cid)


class TestMyCommunities:
    async def test_unauthenticated_is_401(self, auth_db: Any) -> None:
        app = _make_app(auth_db)
        client = app.test_client()
        resp = await client.get("/api/v1/communities/my")
        assert resp.status_code == 401

    async def test_cat_returns_bound_community_with_public_uuid(self, auth_db: Any) -> None:
        uid = _seed_user(auth_db)
        cid = _seed_community(auth_db, name="Streamers")
        _seed_community(auth_db, name="Other Community")
        dal = auth_db.dal
        bind_support_token_tables(dal, migrate=True)
        access_token_service._bind(dal)
        dal.permission_scopes.insert(
            scope_key="community.music:read",
            category="music",
            display_name="Music Read",
        )
        dal.commit()
        cat = access_token_service.create_cat(
            dal,
            cid,
            created_by_user_id=uid,
            name="penguind",
            scopes=["community.music:read"],
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
        app = _make_app(auth_db)
        async with app.app_context():
            expected_uuid = await lookup_public_id(cid)
        assert expected_uuid is not None

        client = app.test_client()
        resp = await client.get(
            "/api/v1/communities/my",
            headers={"Authorization": f"Bearer {cat}"},
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        assert len(data["communities"]) == 1
        assert data["communities"][0]["id"] == expected_uuid
        assert data["communities"][0]["name"] == "Streamers"
        assert data["communities"][0]["role"] == "token"

    async def test_jwt_returns_user_communities_in_tenant_only(self, auth_db: Any) -> None:
        uid = _seed_user(auth_db)
        dal = auth_db.dal
        bind_support_token_tables(dal, migrate=True)
        access_token_service._bind(dal)
        dal.tenants.insert(slug="other-tenant", is_active=True)
        dal.commit()
        c_mine = _seed_community(auth_db, name="My Guild", tenant_slug=TENANT_SLUG)
        c_other_tenant = _seed_community(auth_db, name="Foreign Guild", tenant_slug="other-tenant")
        dal.community_members.insert(community_id=c_mine, user_id=str(uid), is_active=True)
        dal.community_members.insert(community_id=c_other_tenant, user_id=str(uid), is_active=True)
        dal.commit()

        token = make_user_token(user_id=uid, tenant=TENANT_SLUG, scope="community:read")
        app = _make_app(auth_db)
        client = app.test_client()
        resp = await client.get(
            "/api/v1/communities/my",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        async with app.app_context():
            expected_uuid = await lookup_public_id(c_mine)
        assert [c["id"] for c in data["communities"]] == [expected_uuid]
        assert data["communities"][0]["name"] == "My Guild"


class TestRefreshBodySupport:
    async def test_refresh_with_json_body_rotates_and_returns_tokens(self, auth_db: Any) -> None:
        _seed_user(auth_db, email="refresh@example.com", password="correcthorse")
        app = _make_app(auth_db)
        browser_client = app.test_client()
        login_resp = await browser_client.post(
            "/api/v1/auth/login", json={"email": "refresh@example.com", "password": "correcthorse"}
        )
        assert login_resp.status_code == 200
        refresh_tok = next(
            (c.value for c in browser_client.cookie_jar if c.name == "wb_refresh"), None
        )
        assert refresh_tok is not None

        # Fresh client with no cookie jar — simulates desktop SessionProxy.
        desktop_client = app.test_client()
        resp = await desktop_client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refresh_tok},
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["success"] is True
        assert isinstance(data["token"], str) and data["token"]
        assert data["access_token"] == data["token"]
        assert isinstance(data["refresh_token"], str) and data["refresh_token"]
        assert data["refresh_token"] != refresh_tok

    async def test_refresh_with_invalid_json_token_is_401(self, auth_db: Any) -> None:
        app = _make_app(auth_db)
        client = app.test_client()
        resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": "invalid-refresh-token"},
        )
        assert resp.status_code == 401


class TestDesktopOAuthDeepLink:
    @pytest.mark.parametrize(
        "bad_redirect",
        [
            "https://evil.example.com/callback",
            "http://localhost:8080/callback",
            "waddles://evil/callback",
            "waddles://oauth/callback?extra=1",
            "javascript:alert(1)",
        ],
    )
    async def test_open_redirect_rejected_with_400(
        self, auth_db: Any, monkeypatch: Any, bad_redirect: str
    ) -> None:
        monkeypatch.setenv("DISCORD_CLIENT_ID", "123456789012345678")
        app = _make_app(auth_db)
        client = app.test_client()
        resp = await client.get(f"/api/v1/auth/oauth/discord?redirect_uri={bad_redirect}")
        assert resp.status_code == 400
        data = await resp.get_json()
        assert data["error"]["code"] == "INVALID_REDIRECT_URI"

    async def test_desktop_oauth_start_callback_and_exchange(
        self, auth_db: Any, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("DISCORD_CLIENT_ID", "123456789012345678")

        async def _fake_oauth_callback(*_args: Any, **_kwargs: Any) -> str:
            return "mock.jwt.token"

        monkeypatch.setattr(oauth_service, "oauth_callback", _fake_oauth_callback)

        app = _make_app(auth_db)
        client = app.test_client()

        start_resp = await client.get(
            "/api/v1/auth/oauth/discord?redirect_uri=waddles://oauth/callback"
        )
        assert start_resp.status_code == 200
        start_data = await start_resp.get_json()
        state = start_data["state"]
        assert state.startswith(oauth_service.DESKTOP_STATE_PREFIX)

        cb_resp = await client.get(
            f"/api/v1/auth/oauth/discord/callback?code=provider_code&state={state}"
        )
        assert cb_resp.status_code == 302
        location = cb_resp.headers["Location"]
        assert location.startswith("waddles://oauth/callback?code=")
        assert f"&state={state}" in location
        assert "mock.jwt.token" not in location

        exchange_code = location.split("code=")[1].split("&")[0]
        ex_resp = await client.post("/api/v1/auth/exchange", json={"code": exchange_code})
        assert ex_resp.status_code == 200
        ex_data = await ex_resp.get_json()
        assert ex_data["success"] is True
        assert ex_data["token"] == "mock.jwt.token"

        # Single-use: second redemption must fail.
        replay_resp = await client.post("/api/v1/auth/exchange", json={"code": exchange_code})
        assert replay_resp.status_code == 400
