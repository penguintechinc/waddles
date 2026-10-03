"""`blueprints/v1/tenant_twitch_install.py` -- per-tenant Twitch bot-install OAuth flow.

Service-layer modules (`twitch_install_oauth`/`twitch_install_credentials`/
`twitch_install_state`) are monkeypatched at this blueprint module's own
imported references -- same pattern `test_community_connections_api.py`
uses for its sibling OAuth-install flow -- rather than exercising their
real HTTP/Redis/DB behavior, which belongs to their own test suites
(`test_twitch_install_oauth.py`, `test_twitch_install_credentials.py`,
`test_twitch_install_state.py`).
"""

from __future__ import annotations

import json as json_module
from typing import Any
from unittest.mock import AsyncMock

import pytest
from quart import Quart
from quart_schema import QuartSchema

import blueprints.v1.tenant_twitch_install as install_module
from blueprints.v1.tenant_twitch_install import tenant_twitch_install_bp
from config import HubAPIConfig
from services.twitch_install_oauth import TwitchOAuthError, TwitchTokenResult
from services.twitch_install_state import TwitchInstallStatePayload

CALLBACK_BASE = "http://localhost:19999"


def _test_hub_config() -> HubAPIConfig:
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
        connections_callback_base_url=CALLBACK_BASE,
    )


@pytest.fixture
def app(bar_citizen_db: Any) -> Quart:
    dal, _community_id, _tenant_id, _global_id = bar_citizen_db
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(tenant_twitch_install_bp)
    quart_app.config["dal"] = dal
    quart_app.config["HUB_API_CONFIG"] = _test_hub_config()
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


@pytest.fixture(autouse=True)
def _feature_enabled_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install_module, "feature_enabled", AsyncMock(return_value=True))


class TestAuthorize:
    async def test_success_builds_state_and_authorize_url(
        self, client: Any, user_auth_headers: Any, bar_citizen_db: Any, monkeypatch: Any
    ) -> None:
        _dal, _community_id, tenant_id, _global_id = bar_citizen_db
        mock_create_state = AsyncMock(return_value="state-token-abc")
        monkeypatch.setattr(install_module.state_svc, "create_state", mock_create_state)

        captured: dict[str, Any] = {}

        def _build_authorize_url(**kwargs: Any) -> str:
            captured.update(kwargs)
            return "https://id.twitch.tv/oauth2/authorize?client_id=tcid&state=state-token-abc"

        monkeypatch.setattr(install_module.oauth_svc, "build_authorize_url", _build_authorize_url)

        response = await client.post(
            "/api/v1/tenant/twitch-install/authorize",
            headers={
                **user_auth_headers(user_id=1, scope="tenant:admin"),
                "Content-Type": "application/json",
            },
            data=json_module.dumps({"client_id": "tcid", "client_secret": "tcsecret"}),
        )

        assert response.status_code == 200
        body = await response.get_json()
        assert body["authorize_url"].startswith("https://id.twitch.tv/oauth2/authorize")

        assert captured["client_id"] == "tcid"
        assert captured["redirect_uri"] == f"{CALLBACK_BASE}/api/v1/tenant/twitch-install/callback"

        create_kwargs = mock_create_state.await_args.kwargs
        assert create_kwargs["tenant_id"] == tenant_id
        assert create_kwargs["installed_by_user_id"] == 1
        assert create_kwargs["client_id"] == "tcid"
        assert create_kwargs["client_secret"] == "tcsecret"
        expected_redirect = f"{CALLBACK_BASE}/api/v1/tenant/twitch-install/callback"
        assert create_kwargs["redirect_uri"] == expected_redirect

    async def test_wrong_scope_is_403(self, client: Any, user_auth_headers: Any) -> None:
        response = await client.post(
            "/api/v1/tenant/twitch-install/authorize",
            headers={
                **user_auth_headers(user_id=1, scope="tenant:read"),
                "Content-Type": "application/json",
            },
            data=json_module.dumps({"client_id": "tcid", "client_secret": "tcsecret"}),
        )
        assert response.status_code == 403

    async def test_feature_disabled_is_402(
        self, client: Any, user_auth_headers: Any, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr(install_module, "feature_enabled", AsyncMock(return_value=False))
        response = await client.post(
            "/api/v1/tenant/twitch-install/authorize",
            headers={
                **user_auth_headers(user_id=1, scope="tenant:admin"),
                "Content-Type": "application/json",
            },
            data=json_module.dumps({"client_id": "tcid", "client_secret": "tcsecret"}),
        )
        assert response.status_code == 402

    async def test_global_tenant_is_rejected(self, client: Any, user_auth_headers: Any) -> None:
        from tests.conftest import GLOBAL_TENANT_SLUG

        response = await client.post(
            "/api/v1/tenant/twitch-install/authorize",
            headers={
                **user_auth_headers(user_id=1, scope="tenant:admin", tenant=GLOBAL_TENANT_SLUG),
                "Content-Type": "application/json",
            },
            data=json_module.dumps({"client_id": "tcid", "client_secret": "tcsecret"}),
        )
        assert response.status_code == 403

    async def test_missing_client_id_is_400(self, client: Any, user_auth_headers: Any) -> None:
        response = await client.post(
            "/api/v1/tenant/twitch-install/authorize",
            headers={
                **user_auth_headers(user_id=1, scope="tenant:admin"),
                "Content-Type": "application/json",
            },
            data=json_module.dumps({"client_secret": "tcsecret"}),
        )
        assert response.status_code == 400

    async def test_missing_client_secret_is_400(self, client: Any, user_auth_headers: Any) -> None:
        response = await client.post(
            "/api/v1/tenant/twitch-install/authorize",
            headers={
                **user_auth_headers(user_id=1, scope="tenant:admin"),
                "Content-Type": "application/json",
            },
            data=json_module.dumps({"client_id": "tcid"}),
        )
        assert response.status_code == 400

    async def test_no_bearer_token_is_401(self, client: Any) -> None:
        response = await client.post(
            "/api/v1/tenant/twitch-install/authorize",
            headers={"Content-Type": "application/json"},
            data=json_module.dumps({"client_id": "tcid", "client_secret": "tcsecret"}),
        )
        assert response.status_code == 401


class TestCallback:
    async def test_invalid_or_expired_state_is_400(self, client: Any, monkeypatch: Any) -> None:
        monkeypatch.setattr(install_module.state_svc, "consume_state", AsyncMock(return_value=None))
        response = await client.get("/api/v1/tenant/twitch-install/callback?state=bogus&code=abc")
        assert response.status_code == 400
        text = await response.get_data(as_text=True)
        assert "invalid_or_expired_state" in text
        assert "bogus" not in text

    async def test_provider_error_param_is_handled(self, client: Any, monkeypatch: Any) -> None:
        payload = TwitchInstallStatePayload(
            tenant_id=1,
            installed_by_user_id=1,
            client_id="tcid",
            client_secret="tcsecret",  # noqa: S106
            redirect_uri=f"{CALLBACK_BASE}/api/v1/tenant/twitch-install/callback",
        )
        monkeypatch.setattr(
            install_module.state_svc, "consume_state", AsyncMock(return_value=payload)
        )
        response = await client.get(
            "/api/v1/tenant/twitch-install/callback?state=tok&error=access_denied"
        )
        assert response.status_code == 200
        text = await response.get_data(as_text=True)
        assert "access_denied" in text

    async def test_missing_code_is_400(self, client: Any, monkeypatch: Any) -> None:
        payload = TwitchInstallStatePayload(
            tenant_id=1,
            installed_by_user_id=1,
            client_id="tcid",
            client_secret="tcsecret",  # noqa: S106
            redirect_uri=f"{CALLBACK_BASE}/api/v1/tenant/twitch-install/callback",
        )
        monkeypatch.setattr(
            install_module.state_svc, "consume_state", AsyncMock(return_value=payload)
        )
        response = await client.get("/api/v1/tenant/twitch-install/callback?state=tok")
        assert response.status_code == 400

    async def test_exchange_failure_is_handled(self, client: Any, monkeypatch: Any) -> None:
        payload = TwitchInstallStatePayload(
            tenant_id=1,
            installed_by_user_id=1,
            client_id="tcid",
            client_secret="tcsecret",  # noqa: S106
            redirect_uri=f"{CALLBACK_BASE}/api/v1/tenant/twitch-install/callback",
        )
        monkeypatch.setattr(
            install_module.state_svc, "consume_state", AsyncMock(return_value=payload)
        )
        monkeypatch.setattr(
            install_module.oauth_svc,
            "exchange_code",
            AsyncMock(side_effect=TwitchOAuthError("twitch: token endpoint returned HTTP 400")),
        )
        response = await client.get(
            "/api/v1/tenant/twitch-install/callback?state=tok&code=authcode"
        )
        assert response.status_code == 200
        text = await response.get_data(as_text=True)
        assert "exchange_failed" in text
        assert "authcode" not in text
        assert "tcsecret" not in text

    async def test_success_stores_credentials(
        self, client: Any, bar_citizen_db: Any, monkeypatch: Any
    ) -> None:
        _dal, _community_id, tenant_id, _global_id = bar_citizen_db
        payload = TwitchInstallStatePayload(
            tenant_id=tenant_id,
            installed_by_user_id=1,
            client_id="tcid",
            client_secret="tcsecret",  # noqa: S106
            redirect_uri=f"{CALLBACK_BASE}/api/v1/tenant/twitch-install/callback",
        )
        monkeypatch.setattr(
            install_module.state_svc, "consume_state", AsyncMock(return_value=payload)
        )
        token = TwitchTokenResult(
            access_token="tok_new",
            refresh_token="ref_new",
            expires_in=14400,
            scopes=["channel:read:subscriptions", "moderation:read", "channel:manage:moderators"],
            token_type="bearer",
        )
        monkeypatch.setattr(
            install_module.oauth_svc, "exchange_code", AsyncMock(return_value=token)
        )
        store_calls: list[dict[str, Any]] = []

        def _store(dal: Any, **kwargs: Any) -> None:
            store_calls.append(kwargs)

        monkeypatch.setattr(install_module.creds_svc, "store_initial_credentials", _store)

        response = await client.get(
            "/api/v1/tenant/twitch-install/callback?state=tok&code=authcode"
        )
        assert response.status_code == 200
        text = await response.get_data(as_text=True)
        assert '"ok": true' in text
        assert "authcode" not in text
        assert "tcsecret" not in text

        assert len(store_calls) == 1
        call = store_calls[0]
        assert call["tenant_id"] == tenant_id
        assert call["client_id"] == "tcid"
        assert call["client_secret"] == "tcsecret"
        assert call["token"] is token
        assert call["installed_by_user_id"] == 1
