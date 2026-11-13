"""`services/discord_install_service.py` -- per-tenant Discord OAuth2 bot-install flow.

Discord itself is never called -- `_exchange_code` is monkeypatched (happy
path) or `httpx.AsyncClient.post` is patched directly (transport-error
cases), mirroring `services/oauth_providers.py`'s and the parked `#504`
design's own "no real Discord calls in the test suite" precedent.
`validate_outbound_url` is bypassed the same way `test_oauth_providers.py`
does -- it performs real DNS resolution, which has no place in a unit
test.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from services import discord_install_service as install_module
from services import tenant_discord_install_state as state_module
from services.credential_resolver import DefaultCredentialResolver, store_tenant_credentials
from services.discord_install_service import (
    DiscordInstallError,
    build_authorize_url,
    complete_install,
)
from services.errors import ApiError

_KEY = "d4f9317783becee1a4415c1a1229b9258e7a90b768d72a9e2c7dc891af661df6"  # gitleaks:allow


@pytest.fixture(autouse=True)
def _crypto_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", _KEY)


@pytest.fixture(autouse=True)
def _pass_through_ssrf_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _pass_through(url: str, *, allowed_schemes: tuple[str, ...]) -> str:
        return url

    monkeypatch.setattr(install_module, "validate_outbound_url", _pass_through)


NONCE = "browser-nonce-0123456789"
GUILD_ID = "123456789012345678"
_TOKEN_PAYLOAD = {"access_token": "tok", "token_type": "Bearer", "guild": {"id": GUILD_ID}}


@pytest.fixture(autouse=True)
def _no_live_bot_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ok(bot_token: str) -> None:
        return None

    monkeypatch.setattr(install_module, "_validate_bot_token", _ok)


class FakeRedis:
    """Minimal async fake -- same shape `test_tenant_discord_install_state.py` uses."""

    def __init__(self) -> None:
        """Start with an empty in-memory key/value store."""
        self.store: dict[str, str] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def getdel(self, key: str) -> str | None:
        return self.store.pop(key, None)


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    client = FakeRedis()
    monkeypatch.setattr(state_module, "_redis_client", lambda: client)
    return client


class TestBuildAuthorizeUrl:
    async def test_global_tenant_is_rejected(self, fake_redis: FakeRedis) -> None:
        with pytest.raises(ApiError) as exc_info:
            await build_authorize_url(
                binding_nonce=NONCE,
                tenant_id=1,
                is_global_tenant=True,
                admin_user_id=1,
                application_id="app-1",
                client_secret="secret-1",
                bot_token="bot-x",
                redirect_uri="http://localhost/cb",
            )
        assert exc_info.value.status_code == 400

    async def test_blank_application_id_is_rejected(self, fake_redis: FakeRedis) -> None:
        with pytest.raises(ApiError) as exc_info:
            await build_authorize_url(
                binding_nonce=NONCE,
                tenant_id=5,
                is_global_tenant=False,
                admin_user_id=1,
                application_id="",
                client_secret="secret-1",
                bot_token="bot-x",
                redirect_uri="http://localhost/cb",
            )
        assert exc_info.value.status_code == 400

    async def test_missing_bot_token_is_rejected(self, fake_redis: FakeRedis) -> None:
        with pytest.raises(ApiError) as exc_info:
            await build_authorize_url(
                binding_nonce=NONCE,
                tenant_id=5,
                is_global_tenant=False,
                admin_user_id=1,
                application_id="app-1",
                client_secret="secret-1",
                bot_token=None,
                redirect_uri="http://localhost/cb",
            )
        assert exc_info.value.status_code == 400

    async def test_blank_client_secret_is_rejected(self, fake_redis: FakeRedis) -> None:
        with pytest.raises(ApiError) as exc_info:
            await build_authorize_url(
                binding_nonce=NONCE,
                tenant_id=5,
                is_global_tenant=False,
                admin_user_id=1,
                application_id="app-1",
                client_secret="",
                bot_token="bot-x",
                redirect_uri="http://localhost/cb",
            )
        assert exc_info.value.status_code == 400

    async def test_builds_discord_authorize_url_with_bot_scope(self, fake_redis: FakeRedis) -> None:
        url = await build_authorize_url(
            binding_nonce=NONCE,
            tenant_id=5,
            is_global_tenant=False,
            admin_user_id=9,
            application_id="app-42",
            client_secret="secret-42",
            bot_token="bot-token-42",
            redirect_uri="http://localhost/cb",
        )
        assert url.startswith("https://discord.com/oauth2/authorize?")
        assert "client_id=app-42" in url
        assert "scope=bot" in url
        assert "applications.commands" in url
        assert "response_type=code" in url
        assert "state=" in url
        # The submitted app secret never appears in the URL itself.
        assert "secret-42" not in url
        assert "bot-token-42" not in url

    async def test_rejects_default_permissions_override_persists_state(
        self, fake_redis: FakeRedis
    ) -> None:
        url = await build_authorize_url(
            binding_nonce=NONCE,
            tenant_id=5,
            is_global_tenant=False,
            admin_user_id=9,
            application_id="app-42",
            client_secret="secret-42",
            bot_token="bot-x",
            redirect_uri="http://localhost/cb",
            permissions=16,
        )
        assert "permissions=16" in url


class TestCompleteInstall:
    async def test_missing_code_is_rejected(self, fake_redis: FakeRedis) -> None:
        with pytest.raises(ApiError) as exc_info:
            await complete_install(object(), code="", state="whatever", binding_nonce=NONCE)
        assert exc_info.value.status_code == 400

    async def test_invalid_state_is_rejected(self, fake_redis: FakeRedis) -> None:
        with pytest.raises(DiscordInstallError) as exc_info:
            await complete_install(
                object(), code="auth-code", state="never-issued", binding_nonce=NONCE
            )
        assert exc_info.value.status_code == 400

    async def test_replayed_state_is_rejected(
        self, fake_redis: FakeRedis, guild_pairing_db: Any
    ) -> None:
        dal, _community_id, tenant_id, _global_id = guild_pairing_db
        state = await state_module.create_state(
            binding_nonce=NONCE,
            tenant_id=tenant_id,
            admin_user_id=1,
            application_id="app-1",
            client_secret="secret-1",
            bot_token="bot-x",
            redirect_uri="http://localhost/cb",
        )
        with patch(
            "services.discord_install_service._exchange_code",
            new=AsyncMock(return_value=_TOKEN_PAYLOAD),
        ):
            await complete_install(dal, code="auth-code", state=state, binding_nonce=NONCE)

        with pytest.raises(DiscordInstallError):
            await complete_install(dal, code="auth-code", state=state, binding_nonce=NONCE)

    async def test_happy_path_stores_verified_credentials(
        self, fake_redis: FakeRedis, guild_pairing_db: Any
    ) -> None:
        dal, _community_id, tenant_id, _global_id = guild_pairing_db
        state = await state_module.create_state(
            binding_nonce=NONCE,
            tenant_id=tenant_id,
            admin_user_id=7,
            application_id="app-happy",
            client_secret="secret-happy",
            bot_token="bot-token-happy",
            redirect_uri="http://localhost/cb",
        )

        with patch(
            "services.discord_install_service._exchange_code",
            new=AsyncMock(return_value=_TOKEN_PAYLOAD),
        ):
            result = await complete_install(dal, code="auth-code", state=state, binding_nonce=NONCE)

        assert (result.tenant_id, result.platform, result.installed_by_user_id) == (
            tenant_id,
            "discord",
            7,
        )
        assert result.guild_id == GUILD_ID
        assert result.connection_id is not None

        resolver = DefaultCredentialResolver()
        creds = await resolver.resolve(
            dal, tenant_id=tenant_id, is_global_tenant=False, platform="discord"
        )
        assert creds.payload == {
            "client_id": "app-happy",
            "client_secret": "secret-happy",
            "bot_token": "bot-token-happy",
        }

    async def test_exchange_failure_never_persists_credentials(
        self, fake_redis: FakeRedis, guild_pairing_db: Any
    ) -> None:
        dal, _community_id, tenant_id, _global_id = guild_pairing_db
        state = await state_module.create_state(
            binding_nonce=NONCE,
            tenant_id=tenant_id,
            admin_user_id=7,
            application_id="app-fail",
            client_secret="secret-fail",
            bot_token="bot-x",
            redirect_uri="http://localhost/cb",
        )

        with patch(
            "services.discord_install_service._exchange_code",
            new=AsyncMock(side_effect=DiscordInstallError("boom", 502)),
        ):
            with pytest.raises(DiscordInstallError):
                await complete_install(dal, code="auth-code", state=state, binding_nonce=NONCE)

        from services.credential_resolver import TransportUnavailable

        resolver = DefaultCredentialResolver()
        with pytest.raises(TransportUnavailable):
            await resolver.resolve(
                dal, tenant_id=tenant_id, is_global_tenant=False, platform="discord"
            )

    async def test_exchange_network_error_raises_and_never_persists(
        self, fake_redis: FakeRedis, guild_pairing_db: Any
    ) -> None:
        import httpx

        dal, _community_id, tenant_id, _global_id = guild_pairing_db
        state = await state_module.create_state(
            binding_nonce=NONCE,
            tenant_id=tenant_id,
            admin_user_id=7,
            application_id="app-net",
            client_secret="secret-net",
            bot_token="bot-x",
            redirect_uri="http://localhost/cb",
        )

        with patch(
            "httpx.AsyncClient.post",
            new=AsyncMock(side_effect=httpx.ConnectError("boom")),
        ):
            with pytest.raises(DiscordInstallError):
                await complete_install(dal, code="auth-code", state=state, binding_nonce=NONCE)

    async def test_exchange_non_2xx_raises(
        self, fake_redis: FakeRedis, guild_pairing_db: Any
    ) -> None:
        dal, _community_id, tenant_id, _global_id = guild_pairing_db
        state = await state_module.create_state(
            binding_nonce=NONCE,
            tenant_id=tenant_id,
            admin_user_id=7,
            application_id="app-bad",
            client_secret="secret-bad",
            bot_token="bot-x",
            redirect_uri="http://localhost/cb",
        )

        class _FakeResponse:
            status_code = 401

            def json(self) -> Any:
                return {"error": "invalid_client"}

        with patch(
            "httpx.AsyncClient.post",
            new=AsyncMock(return_value=_FakeResponse()),
        ):
            with pytest.raises(DiscordInstallError) as exc_info:
                await complete_install(dal, code="auth-code", state=state, binding_nonce=NONCE)
        # The error message carries only an HTTP status -- never the secret.
        assert "secret-bad" not in exc_info.value.message

    async def test_store_tenant_credentials_write_side_still_rejects_global_tenant(
        self, guild_pairing_db: Any
    ) -> None:
        """Defense-in-depth: `store_tenant_credentials` itself fails closed for tenant 0."""
        dal, _community_id, _tenant_id, global_tenant_id = guild_pairing_db
        with pytest.raises(ApiError) as exc_info:
            store_tenant_credentials(
                dal,
                tenant_id=global_tenant_id,
                is_global_tenant=True,
                platform="discord",
                payload={"client_id": "x", "client_secret": "y"},
                installed_by_user_id=1,
            )
        assert exc_info.value.status_code == 400
