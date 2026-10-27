"""`blueprints/v1/auth.py` -- the M1 auth group, replacing the old login-only stub.

Standalone Quart app registering only `auth_bp` (mirrors
`test_platform_blueprint.py`'s own pattern) against the `auth_db`
fixture (`tests/conftest.py`) -- real JWTs via `flask_core.auth.
create_jwt_token`, real pydal queries against an in-memory schema, no
mocking of the auth chain itself.

Fail-first proof (executed, not narrated): temporarily removed
`get_current_user_id(request)`'s bearer-token check in `services/
current_user.py` (made it always return `1` regardless of the header) --
`test_set_password_without_token_is_401` went red (200 instead of 401,
an auth bypass); reverted, green again. Separately, temporarily swapped
`RequiresVerificationError` handling in `login()` to fall through to the
generic `except ApiError` branch -- `test_login_unverified_email_is_403_
requires_verification` went red (401 generic "Invalid credentials"
instead of 403 with `requiresVerification: true`, which `AuthContext.jsx`
depends on to route the user to the verification-resend flow); reverted,
green again.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import pytest
from quart import Quart
from quart_schema import QuartSchema

from app import bridge_session_cookie_to_bearer
from blueprints.v1.auth import auth_bp
from config import HubAPIConfig
from services import oauth_service
from tests.conftest import TENANT_SLUG, make_user_token


def _hash(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()


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
        jwt_algorithm="HS256",
        default_tenant_slug="global",
        posthog_api_key=None,
        posthog_host="https://license.penguintech.io",
        license_server_url="https://license.penguintech.io",
        identity_callback_base_url="http://localhost:8204",
        frontend_origin="http://localhost:5173",
        log_level="INFO",
    )


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


@pytest.fixture
def bridged_app(auth_db: Any) -> Quart:
    """Same as `app` above, plus `app.py`'s cookie->bearer bridge hook.

    Isolated into its own fixture rather than added to `app` directly --
    every other test class in this file authenticates purely via an
    explicit `Authorization` header and should keep proving that path
    works with nothing else in play; only `TestSessionCookieBridgeEndToEnd`
    below needs the hook registered.
    """
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(auth_bp)
    quart_app.config["dal"] = auth_db.dal
    quart_app.config["async_dal"] = auth_db
    quart_app.config["HUB_API_CONFIG"] = _test_config()
    quart_app.before_request(bridge_session_cookie_to_bearer)
    return quart_app


@pytest.fixture
def bridged_client(bridged_app: Quart) -> Any:
    return bridged_app.test_client()


def _seed_settings(auth_db: Any, **settings: str) -> None:
    for key, value in settings.items():
        auth_db.dal.hub_settings.insert(setting_key=key, setting_value=value)
    auth_db.dal.commit()


def _seed_user(
    auth_db: Any,
    *,
    email: str = "alice@example.com",
    username: str = "alice",
    password: str = "hunter22",
    is_active: bool = True,
    email_verified: bool = True,
    is_super_admin: bool = False,
    is_analytics_consumer: bool = False,
) -> int:
    user_id: int = auth_db.dal.hub_users.insert(
        email=email,
        username=username,
        password_hash=_hash(password),
        is_active=is_active,
        email_verified=email_verified,
        is_super_admin=is_super_admin,
        is_analytics_consumer=is_analytics_consumer,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    auth_db.dal.commit()
    return user_id


class TestRegister:
    async def test_register_disabled_by_default_is_403(self, client: Any) -> None:
        """No hub_settings rows -- signup_enabled defaults to "not true" -- forbidden."""
        response = await client.post(
            "/api/v1/auth/register",
            json={"email": "new@example.com", "password": "hunter22"},
        )
        assert response.status_code == 403

    async def test_register_success_returns_token_and_user(self, client: Any, auth_db: Any) -> None:
        _seed_settings(auth_db, signup_enabled="true", email_configured="true")
        response = await client.post(
            "/api/v1/auth/register",
            json={"email": "new@example.com", "password": "hunter22", "username": "newuser"},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["success"] is True
        assert body["token"]
        assert body["user"]["email"] == "new@example.com"
        assert body["user"]["isSuperAdmin"] is False

    async def test_register_duplicate_email_is_409(self, client: Any, auth_db: Any) -> None:
        _seed_settings(auth_db, signup_enabled="true", email_configured="true")
        _seed_user(auth_db, email="taken@example.com")
        response = await client.post(
            "/api/v1/auth/register",
            json={"email": "taken@example.com", "password": "hunter22"},
        )
        assert response.status_code == 409

    async def test_register_missing_fields_is_400(self, client: Any) -> None:
        """quart-schema @validate_request rejects a body missing required fields."""
        response = await client.post("/api/v1/auth/register", json={"email": "a@b.com"})
        assert response.status_code == 400


class TestLogin:
    async def test_login_success_returns_token(self, client: Any, auth_db: Any) -> None:
        _seed_user(auth_db, email="bob@example.com", password="correcthorse")
        response = await client.post(
            "/api/v1/auth/login", json={"email": "bob@example.com", "password": "correcthorse"}
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["token"]
        assert body["user"]["email"] == "bob@example.com"

    async def test_login_wrong_password_is_401(self, client: Any, auth_db: Any) -> None:
        _seed_user(auth_db, email="bob@example.com", password="correcthorse")
        response = await client.post(
            "/api/v1/auth/login", json={"email": "bob@example.com", "password": "wrong"}
        )
        assert response.status_code == 401

    async def test_login_unknown_email_is_401(self, client: Any) -> None:
        """Never reveals whether the account exists -- same 401 as a wrong password."""
        response = await client.post(
            "/api/v1/auth/login", json={"email": "nobody@example.com", "password": "x"}
        )
        assert response.status_code == 401

    async def test_login_inactive_account_is_401(self, client: Any, auth_db: Any) -> None:
        _seed_user(auth_db, email="gone@example.com", password="hunter22", is_active=False)
        response = await client.post(
            "/api/v1/auth/login", json={"email": "gone@example.com", "password": "hunter22"}
        )
        assert response.status_code == 401

    async def test_login_unverified_email_is_403_requires_verification(
        self, client: Any, auth_db: Any
    ) -> None:
        _seed_user(auth_db, email="new@example.com", password="hunter22", email_verified=False)
        response = await client.post(
            "/api/v1/auth/login", json={"email": "new@example.com", "password": "hunter22"}
        )
        assert response.status_code == 403
        body = await response.get_json()
        assert body["requiresVerification"] is True

    async def test_login_analytics_consumer_grants_analytics_read_scope(
        self, client: Any, auth_db: Any
    ) -> None:
        """Analytics module port (M9) -- see `services/auth_service.py::create_session_token`.

        Without this scope grant, no `is_analytics_consumer` user could
        ever satisfy `blueprints/v1/analytics.py`'s platform-overview
        `require_scope("analytics:read")` gate.
        """
        from flask_core.auth import verify_jwt_token

        _seed_user(
            auth_db,
            email="carol@example.com",
            password="hunter22",
            is_analytics_consumer=True,
        )
        response = await client.post(
            "/api/v1/auth/login", json={"email": "carol@example.com", "password": "hunter22"}
        )
        assert response.status_code == 200
        body = await response.get_json()
        payload = verify_jwt_token(body["token"], "change-me-in-production")
        assert payload is not None
        assert "analytics:read" in payload["scope"].split()

    async def test_login_non_consumer_has_no_analytics_read_scope(
        self, client: Any, auth_db: Any
    ) -> None:
        from flask_core.auth import verify_jwt_token

        _seed_user(auth_db, email="dave@example.com", password="hunter22")
        response = await client.post(
            "/api/v1/auth/login", json={"email": "dave@example.com", "password": "hunter22"}
        )
        assert response.status_code == 200
        body = await response.get_json()
        payload = verify_jwt_token(body["token"], "change-me-in-production")
        assert payload is not None
        assert "analytics:read" not in payload["scope"].split()


class TestMe:
    async def test_me_without_token_returns_null_user(self, client: Any) -> None:
        """Matches Node's optionalAuth -- absent token is "logged out", not an error."""
        response = await client.get("/api/v1/auth/me")
        assert response.status_code == 200
        body = await response.get_json()
        assert body == {"success": True, "user": None}

    async def test_me_with_valid_token_returns_user(self, client: Any, auth_db: Any) -> None:
        user_id = _seed_user(auth_db, email="carol@example.com")
        token = make_user_token(user_id=user_id, tenant=TENANT_SLUG)
        response = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        body = await response.get_json()
        assert body["user"]["email"] == "carol@example.com"
        assert body["user"]["hasPassword"] is True


class TestSetPassword:
    async def test_set_password_without_token_is_401(self, client: Any) -> None:
        """The representative auth-bypass check: no bearer token, no access."""
        response = await client.post(
            "/api/v1/auth/password", json={"newPassword": "newpassword123"}
        )
        assert response.status_code == 401

    async def test_set_password_wrong_current_password_is_401(
        self, client: Any, auth_db: Any
    ) -> None:
        user_id = _seed_user(auth_db, password="original1")
        token = make_user_token(user_id=user_id, tenant=TENANT_SLUG)
        response = await client.post(
            "/api/v1/auth/password",
            headers={"Authorization": f"Bearer {token}"},
            json={"currentPassword": "wrongpass", "newPassword": "newpassword123"},
        )
        assert response.status_code == 401

    async def test_set_password_success(self, client: Any, auth_db: Any) -> None:
        user_id = _seed_user(auth_db, password="original1")
        token = make_user_token(user_id=user_id, tenant=TENANT_SLUG)
        response = await client.post(
            "/api/v1/auth/password",
            headers={"Authorization": f"Bearer {token}"},
            json={"currentPassword": "original1", "newPassword": "newpassword123"},
        )
        assert response.status_code == 200


class TestLogout:
    async def test_logout_always_returns_success(self, client: Any) -> None:
        """No token present -- matches Node's logout(), a no-op success, never an error."""
        response = await client.post("/api/v1/auth/logout")
        assert response.status_code == 200
        body = await response.get_json()
        assert body == {"success": True}


class TestRefresh:
    async def test_refresh_without_token_is_400(self, client: Any) -> None:
        response = await client.post("/api/v1/auth/refresh")
        assert response.status_code == 400

    async def test_refresh_valid_session_returns_new_token(self, client: Any, auth_db: Any) -> None:
        """The refresh cookie `login` set rides in the client's jar; no Authorization header."""
        _seed_user(auth_db, email="rita@example.com", password="correcthorse")
        login = await client.post(
            "/api/v1/auth/login", json={"email": "rita@example.com", "password": "correcthorse"}
        )
        token = (await login.get_json())["token"]
        response = await client.post("/api/v1/auth/refresh")
        assert response.status_code == 200
        body = await response.get_json()
        assert body["token"] and body["token"] != token

    async def test_an_access_token_is_not_a_refresh_credential(
        self, client: Any, auth_db: Any
    ) -> None:
        """The old contract refreshed from the (long-lived) access token; that door is closed."""
        user_id = _seed_user(auth_db)
        token = make_user_token(user_id=user_id, tenant=TENANT_SLUG)
        response = await client.post(
            "/api/v1/auth/refresh", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 400  # no refresh cookie -> "Refresh token required"


class TestSessionCookie:
    """security.md C4 fix -- the session JWT is no longer localStorage-only.

    Every token-issuing route also sets the `wb_session` HttpOnly cookie
    (`services/session_cookie.py`); `logout` clears it. The cookie-as-
    bearer-token bridge (`app.py`'s `before_request` hook) is NOT exercised
    here -- this file's `app` fixture registers only `auth_bp`, not the
    full `create_app()` -- see `tests/test_app_factory.py::
    TestSessionCookieBridgesToBearerAuth` for that end-to-end path.
    """

    @staticmethod
    def _cookie_attrs(response: Any, name: str = "wb_session") -> str:
        for raw in response.headers.getlist("Set-Cookie"):
            if raw.startswith(f"{name}="):
                return raw
        raise AssertionError(f"{name} cookie not set")

    async def test_login_sets_httponly_secure_samesite_lax_cookie(
        self, client: Any, auth_db: Any
    ) -> None:
        _seed_user(auth_db, email="dana@example.com", password="correcthorse")
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": "dana@example.com", "password": "correcthorse"},
        )
        assert response.status_code == 200
        cookie = self._cookie_attrs(response)
        assert "HttpOnly" in cookie
        assert "Secure" in cookie
        assert "SameSite=Lax" in cookie
        assert "Path=/" in cookie
        body = await response.get_json()
        # Cookie value carries the same session JWT the body still returns
        # (see services/session_cookie.py's docstring for why the body
        # field is kept rather than ripped out of the wire contract).
        assert body["token"] in cookie

    async def test_refresh_rotates_both_cookies(self, client: Any, auth_db: Any) -> None:
        _seed_user(auth_db, email="ria@example.com", password="correcthorse")
        login = await client.post(
            "/api/v1/auth/login", json={"email": "ria@example.com", "password": "correcthorse"}
        )
        old_access = (await login.get_json())["token"]
        old_refresh = self._cookie_attrs(login, "wb_refresh")
        response = await client.post("/api/v1/auth/refresh")
        assert response.status_code == 200
        body = await response.get_json()
        cookie = self._cookie_attrs(response)
        assert body["token"] in cookie
        assert old_access not in cookie
        assert self._cookie_attrs(response, "wb_refresh") != old_refresh

    async def test_logout_clears_the_cookie(self, client: Any) -> None:
        response = await client.post("/api/v1/auth/logout")
        assert response.status_code == 200
        cookie = self._cookie_attrs(response)
        # delete_cookie() -- empty value, immediately-expired Max-Age.
        assert cookie.startswith("wb_session=;") or "wb_session=" in cookie.split(";")[0]
        assert "Max-Age=0" in cookie or "expires=" in cookie.lower()


class TestTenantLoginInfo:
    async def test_unknown_tenant_is_404(self, client: Any) -> None:
        response = await client.get("/api/v1/auth/tenant/no-such-tenant")
        assert response.status_code == 404

    async def test_known_tenant_returns_info(self, client: Any) -> None:
        response = await client.get(f"/api/v1/auth/tenant/{TENANT_SLUG}")
        assert response.status_code == 200
        body = await response.get_json()
        assert body["tenant"]["slug"] == TENANT_SLUG


class TestLegacyLinkOAuth:
    async def test_legacy_link_oauth_is_501(self, client: Any) -> None:
        """Dead code under the unified session model -- see blueprints/v1/auth.py docstring."""
        response = await client.post("/api/v1/auth/link-oauth")
        assert response.status_code == 501


def _patch_oauth_callback(monkeypatch: Any, token: str) -> None:
    """Stub `oauth_service.oauth_callback` -- avoids real network calls to Discord."""

    async def fake_oauth_callback(
        async_dal: Any,
        dal: Any,
        cfg: Any,
        *,
        platform: str,
        code: str,
        state: str,
        callback_base_url: str,
    ) -> str:
        return token

    monkeypatch.setattr(oauth_service, "oauth_callback", fake_oauth_callback)


class TestOAuthExchangeCodeHandoff:
    """Security hotfix: `oauth_callback` must never put the session JWT in a redirect URL.

    Query strings leak into proxy/access logs, browser history, and the
    `Referer` header of any outbound request the callback landing page
    happens to make -- see `blueprints/v1/auth.py::oauth_callback`'s
    docstring and `hub_api/PORTING.md` Gotcha #8.

    Fail-first proof (executed, not narrated): temporarily reverted
    `oauth_callback`'s final line to its original
    `redirect(f"{frontend_origin}/auth/callback?token={token}")` shape --
    `test_callback_redirect_never_contains_the_jwt` went red (the raw JWT
    appeared verbatim in the redirect's `Location` header); reverted,
    green again.
    """

    async def test_provider_error_redirects_without_calling_the_service(
        self, client: Any, monkeypatch: Any
    ) -> None:
        called = False

        async def fail_if_called(*args: Any, **kwargs: Any) -> str:
            nonlocal called
            called = True
            return "unreachable"

        monkeypatch.setattr(oauth_service, "oauth_callback", fail_if_called)
        response = await client.get(
            "/api/v1/auth/oauth/discord/callback",
            query_string={"error": "access_denied"},
        )
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/login?error=oauth_denied")
        assert not called

    async def test_missing_code_or_state_redirects_to_login(self, client: Any) -> None:
        response = await client.get("/api/v1/auth/oauth/discord/callback")
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/login?error=oauth_failed")

    async def test_service_api_error_redirects_to_login_without_minting_a_code(
        self, client: Any, monkeypatch: Any
    ) -> None:
        from services.errors import bad_request

        async def fake_oauth_callback_raises(*args: Any, **kwargs: Any) -> str:
            raise bad_request("Invalid or expired OAuth state")

        monkeypatch.setattr(oauth_service, "oauth_callback", fake_oauth_callback_raises)
        response = await client.get(
            "/api/v1/auth/oauth/discord/callback",
            query_string={"code": "platform-auth-code", "state": "s"},
        )
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/login?error=oauth_failed")

    async def test_callback_redirect_never_contains_the_jwt(
        self, client: Any, monkeypatch: Any
    ) -> None:
        _patch_oauth_callback(monkeypatch, token="super-secret-session-jwt")
        response = await client.get(
            "/api/v1/auth/oauth/discord/callback",
            query_string={"code": "platform-auth-code", "state": "s"},
        )
        assert response.status_code == 302
        location = response.headers["Location"]
        assert "token=" not in location
        assert "super-secret-session-jwt" not in location
        assert "code=" in location

    async def test_callback_then_exchange_returns_the_real_jwt(
        self, client: Any, monkeypatch: Any
    ) -> None:
        _patch_oauth_callback(monkeypatch, token="real-jwt-value")
        callback_response = await client.get(
            "/api/v1/auth/oauth/discord/callback",
            query_string={"code": "platform-auth-code", "state": "s"},
        )
        code = callback_response.headers["Location"].split("code=")[1]

        exchange_response = await client.post("/api/v1/auth/exchange", json={"code": code})
        assert exchange_response.status_code == 200
        body = await exchange_response.get_json()
        assert body == {"success": True, "token": "real-jwt-value"}

    async def test_exchange_also_sets_the_httponly_session_cookie(
        self, client: Any, monkeypatch: Any
    ) -> None:
        """security.md C4: `/exchange` must also deliver the cookie, not just the body.

        The frontend no longer persists the session JWT to localStorage,
        so the OAuth flow's `/exchange` response has to carry it as the
        HttpOnly cookie too.
        """
        _patch_oauth_callback(monkeypatch, token="cookie-jwt-value")
        callback_response = await client.get(
            "/api/v1/auth/oauth/discord/callback",
            query_string={"code": "platform-auth-code", "state": "s"},
        )
        code = callback_response.headers["Location"].split("code=")[1]

        exchange_response = await client.post("/api/v1/auth/exchange", json={"code": code})
        cookies = [
            raw
            for raw in exchange_response.headers.getlist("Set-Cookie")
            if raw.startswith("wb_session=")
        ]
        assert len(cookies) == 1
        assert "cookie-jwt-value" in cookies[0]
        assert "HttpOnly" in cookies[0]
        assert "Secure" in cookies[0]
        assert "SameSite=Lax" in cookies[0]

    async def test_exchange_code_is_single_use(self, client: Any, monkeypatch: Any) -> None:
        _patch_oauth_callback(monkeypatch, token="one-time-jwt")
        callback_response = await client.get(
            "/api/v1/auth/oauth/discord/callback",
            query_string={"code": "platform-auth-code", "state": "s"},
        )
        code = callback_response.headers["Location"].split("code=")[1]

        first = await client.post("/api/v1/auth/exchange", json={"code": code})
        assert first.status_code == 200

        second = await client.post("/api/v1/auth/exchange", json={"code": code})
        assert second.status_code == 400

    async def test_unknown_exchange_code_is_rejected(self, client: Any) -> None:
        response = await client.post("/api/v1/auth/exchange", json={"code": "never-issued-code"})
        assert response.status_code == 400

    async def test_expired_exchange_code_is_rejected(self, client: Any, auth_db: Any) -> None:
        auth_db.dal.hub_oauth_exchange_codes.insert(
            code="expired-exchange-code",
            token="whatever-it-was",
            platform="discord",
            used=False,
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
            created_at=datetime.now(UTC) - timedelta(minutes=2),
        )
        auth_db.dal.commit()
        response = await client.post(
            "/api/v1/auth/exchange", json={"code": "expired-exchange-code"}
        )
        assert response.status_code == 400


class TestSessionCookieBridgeEndToEnd:
    """Full round-trip proof of security.md's C4 fix, browser-SPA-shaped.

    Login purely through the wire contract the frontend actually uses --
    the session JWT is read only from the cookie the server set, an
    `Authorization` header is never sent by hand -- see `bridged_client`'s
    own docstring for why this needs its own app/client fixtures rather
    than the file's default `app`/`client`.
    """

    async def test_login_then_me_authenticates_via_cookie_alone(
        self, bridged_client: Any, auth_db: Any
    ) -> None:
        _seed_user(auth_db, email="frank@example.com", password="correcthorse")
        login_response = await bridged_client.post(
            "/api/v1/auth/login",
            json={"email": "frank@example.com", "password": "correcthorse"},
        )
        assert login_response.status_code == 200

        # No Authorization header set below -- only whatever cookie jar
        # `bridged_client` already carries from the login response above.
        me_response = await bridged_client.get("/api/v1/auth/me")
        assert me_response.status_code == 200
        body = await me_response.get_json()
        assert body["user"]["email"] == "frank@example.com"

    async def test_logout_then_me_is_unauthenticated_again(
        self, bridged_client: Any, auth_db: Any
    ) -> None:
        _seed_user(auth_db, email="grace@example.com", password="correcthorse")
        await bridged_client.post(
            "/api/v1/auth/login",
            json={"email": "grace@example.com", "password": "correcthorse"},
        )
        await bridged_client.post("/api/v1/auth/logout")

        me_response = await bridged_client.get("/api/v1/auth/me")
        body = await me_response.get_json()
        assert body["user"] is None
