"""`blueprints/v1/tenant_discord_install.py` -- auth, tenant-0 rejection, response shape.

`discord_install_service.build_authorize_url`/`complete_install` are
monkeypatched at this blueprint module's own imported references, same
pattern `test_v1_guild_pairing_blueprint.py` uses for its service-layer
calls -- real HTTP/Discord/Redis behavior belongs to
`test_discord_install_service.py`'s own suite.
"""

from __future__ import annotations

import json as json_module
import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest
from quart import Quart
from quart_schema import QuartSchema

import blueprints.v1.tenant_discord_install as install_bp_module
from blueprints.v1.tenant_discord_install import tenant_discord_install_bp
from config import HubAPIConfig
from services.discord_install_service import DiscordInstallError, InstallResult
from services.errors import bad_request


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
    )


@pytest.fixture
def app(bar_citizen_db: Any) -> Quart:
    dal, _community_id, _tenant_id, _global_id = bar_citizen_db
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(tenant_discord_install_bp)
    quart_app.config["dal"] = dal
    quart_app.config["HUB_API_CONFIG"] = _test_hub_config()
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


@pytest.fixture(autouse=True)
def _feature_enabled_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install_bp_module, "feature_enabled", AsyncMock(return_value=True))


def _auth_body(**overrides: Any) -> dict[str, Any]:
    body = {
        "application_id": "app-123",
        "client_secret": "secret-123",  # gitleaks:allow - test fixture value, not a credential
        "bot_token": "bot-token-123",  # gitleaks:allow - test fixture value, not a credential
    }
    body.update(overrides)
    return body


class TestStartInstall:
    async def test_missing_bearer_token_is_401(self, client: Any) -> None:
        response = await client.post(
            "/api/v1/tenants/discord/install/authorize",
            headers={"Content-Type": "application/json"},
            data=json_module.dumps(_auth_body()),
        )
        assert response.status_code == 401

    async def test_wrong_scope_is_403(self, client: Any, auth_headers: Any) -> None:
        response = await client.post(
            "/api/v1/tenants/discord/install/authorize",
            headers={
                **auth_headers(scope="community.guild_pairing:write", user_id="1"),
                "Content-Type": "application/json",
            },
            data=json_module.dumps(_auth_body()),
        )
        assert response.status_code == 403

    async def test_feature_flag_off_is_402(
        self, client: Any, auth_headers: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(install_bp_module, "feature_enabled", AsyncMock(return_value=False))
        response = await client.post(
            "/api/v1/tenants/discord/install/authorize",
            headers={
                **auth_headers(scope="tenant.discord_install:write", user_id="1"),
                "Content-Type": "application/json",
            },
            data=json_module.dumps(_auth_body()),
        )
        assert response.status_code == 402

    async def test_global_tenant_is_rejected(self, client: Any, auth_headers: Any) -> None:
        from tests.conftest import GLOBAL_TENANT_SLUG

        response = await client.post(
            "/api/v1/tenants/discord/install/authorize",
            headers={
                **auth_headers(
                    scope="tenant.discord_install:write",
                    tenant=GLOBAL_TENANT_SLUG,
                    user_id="1",
                ),
                "Content-Type": "application/json",
            },
            data=json_module.dumps(_auth_body()),
        )
        assert response.status_code == 400
        body = await response.get_json()
        assert "global tenant" in body["error"]["message"].lower()

    async def test_missing_required_fields_is_400(self, client: Any, auth_headers: Any) -> None:
        response = await client.post(
            "/api/v1/tenants/discord/install/authorize",
            headers={
                **auth_headers(scope="tenant.discord_install:write", user_id="1"),
                "Content-Type": "application/json",
            },
            data=json_module.dumps({"application_id": "app-123"}),
        )
        assert response.status_code == 400

    async def test_happy_path_returns_authorize_url(
        self, client: Any, auth_headers: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            install_bp_module,
            "build_authorize_url",
            AsyncMock(return_value="https://discord.com/oauth2/authorize?client_id=app-123"),
        )
        response = await client.post(
            "/api/v1/tenants/discord/install/authorize",
            headers={
                **auth_headers(scope="tenant.discord_install:write", user_id="1"),
                "Content-Type": "application/json",
            },
            data=json_module.dumps(_auth_body()),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body == {
            "success": True,
            "authorize_url": "https://discord.com/oauth2/authorize?client_id=app-123",
        }

    async def test_service_error_is_propagated(
        self, client: Any, auth_headers: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            install_bp_module,
            "build_authorize_url",
            AsyncMock(side_effect=bad_request("application_id and client_secret are required")),
        )
        response = await client.post(
            "/api/v1/tenants/discord/install/authorize",
            headers={
                **auth_headers(scope="tenant.discord_install:write", user_id="1"),
                "Content-Type": "application/json",
            },
            data=json_module.dumps(_auth_body()),
        )
        assert response.status_code == 400


class TestInstallCallback:
    async def test_happy_path_returns_tenant_and_platform(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            install_bp_module,
            "complete_install",
            AsyncMock(
                return_value=InstallResult(tenant_id=5, platform="discord", installed_by_user_id=7)
            ),
        )
        response = await client.get(
            "/api/v1/discord/install/callback?code=auth-code&state=some-state"
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body == {"success": True, "tenant_id": 5, "platform": "discord"}

    async def test_invalid_state_is_rejected(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            install_bp_module,
            "complete_install",
            AsyncMock(
                side_effect=DiscordInstallError(
                    "invalid, expired, or already-used install state", 400
                )
            ),
        )
        response = await client.get("/api/v1/discord/install/callback?code=auth-code&state=forged")
        assert response.status_code == 400

    async def test_missing_code_is_rejected(self, client: Any) -> None:
        response = await client.get("/api/v1/discord/install/callback?state=some-state")
        assert response.status_code == 400

    async def test_requires_no_bearer_token(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The public callback must work with zero auth headers -- Discord sends none."""
        monkeypatch.setattr(
            install_bp_module,
            "complete_install",
            AsyncMock(
                return_value=InstallResult(tenant_id=1, platform="discord", installed_by_user_id=1)
            ),
        )
        response = await client.get(
            "/api/v1/discord/install/callback?code=auth-code&state=some-state"
        )
        assert response.status_code == 200


class TestNoSecretsInLogs:
    async def test_authorize_body_secrets_never_logged(
        self,
        client: Any,
        auth_headers: Any,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(
            install_bp_module,
            "build_authorize_url",
            AsyncMock(return_value="https://discord.com/oauth2/authorize?client_id=app-123"),
        )
        with caplog.at_level(logging.DEBUG):
            await client.post(
                "/api/v1/tenants/discord/install/authorize",
                headers={
                    **auth_headers(scope="tenant.discord_install:write", user_id="1"),
                    "Content-Type": "application/json",
                },
                data=json_module.dumps(_auth_body()),
            )
        for record in caplog.records:
            assert "secret-123" not in record.getMessage()
            assert "bot-token-123" not in record.getMessage()
