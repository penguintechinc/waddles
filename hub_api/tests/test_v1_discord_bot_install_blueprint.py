"""`blueprints/v1/discord_bot_install.py` -- PRE-AUTH, no DB/tenant fixture needed.

`services.discord_bot_install.build_install_url` reads `DISCORD_CLIENT_ID`
straight from the environment, so these tests exercise the real function
against `monkeypatch.setenv`/`delenv` rather than mocking the service --
same "no Discord call ever happens" posture as
`test_discord_bot_install_service.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.discord_bot_install import discord_bot_install_bp


@pytest.fixture
def app() -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(discord_bot_install_bp)
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


class TestGetBotInstallUrl:
    async def test_no_auth_required(self, client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """PRE-AUTH route -- a bare request with no Authorization header must still succeed."""
        monkeypatch.setenv("DISCORD_CLIENT_ID", "123456789012345678")
        response = await client.get("/api/v1/public/discord/bot-install")
        assert response.status_code == 200

    async def test_response_shape(self, client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DISCORD_CLIENT_ID", "123456789012345678")
        response = await client.get("/api/v1/public/discord/bot-install")
        body = await response.get_json()

        assert body["success"] is True
        assert body["installUrl"].startswith("https://discord.com/oauth2/authorize?")
        assert "client_id=123456789012345678" in body["installUrl"]
        assert "scope=bot" in body["installUrl"]

    async def test_missing_client_id_is_503_provider_not_configured(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DISCORD_CLIENT_ID", raising=False)
        response = await client.get("/api/v1/public/discord/bot-install")
        body = await response.get_json()

        assert response.status_code == 503
        assert body["error"] == "provider_not_configured"
        assert body["provider"] == "discord"
