"""`services/tenant_discord_install_state.py` -- single-use bot-install `state` token store.

`_redis_client()` is monkeypatched to a small in-process fake (no real
Redis/Valkey dependency), same style `tests/test_oauth_connection_state.py`
uses for the sibling C3 module.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from quart import Quart

from config import HubAPIConfig
from services import tenant_discord_install_state as state_module
from services.rate_limiting import RATE_LIMITER_CONFIG_KEY

_KEY = "d4f9317783becee1a4415c1a1229b9258e7a90b768d72a9e2c7dc891af661df6"  # gitleaks:allow


class FakeRedis:
    """Minimal async fake covering only the two commands this module calls."""

    def __init__(self) -> None:
        """Start with an empty in-memory key/value store."""
        self.store: dict[str, tuple[str, float | None]] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        expires_at = time.monotonic() + ex if ex is not None else None
        self.store[key] = (value, expires_at)

    async def getdel(self, key: str) -> str | None:
        entry = self.store.pop(key, None)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at is not None and time.monotonic() > expires_at:
            return None
        return value

    def force_expire(self, key: str) -> None:
        """Test-only helper: make a stored entry already expired without sleeping."""
        value, _ = self.store[key]
        self.store[key] = (value, time.monotonic() - 1)


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


@pytest.fixture(autouse=True)
def _crypto_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", _KEY)


@pytest.fixture
def app() -> Quart:
    quart_app = Quart(__name__)
    quart_app.config["HUB_API_CONFIG"] = _test_hub_config()
    return quart_app


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    client = FakeRedis()
    monkeypatch.setattr(state_module, "_redis_client", lambda: client)
    return client


class TestCreateAndConsumeState:
    async def test_roundtrip_returns_the_stored_payload(
        self, app: Quart, fake_redis: FakeRedis
    ) -> None:
        async with app.app_context():
            token = await state_module.create_state(
                tenant_id=5,
                admin_user_id=42,
                application_id="app-123",
                client_secret="super-secret",
                bot_token="bot-token-xyz",
                redirect_uri="http://localhost/cb/discord",
            )
            pending = await state_module.consume_state(token)

        assert pending == state_module.PendingInstall(
            tenant_id=5,
            admin_user_id=42,
            application_id="app-123",
            client_secret="super-secret",
            bot_token="bot-token-xyz",
            redirect_uri="http://localhost/cb/discord",
        )

    async def test_bot_token_none_roundtrips_as_none(
        self, app: Quart, fake_redis: FakeRedis
    ) -> None:
        async with app.app_context():
            token = await state_module.create_state(
                tenant_id=1,
                admin_user_id=1,
                application_id="app-1",
                client_secret="secret-1",
                bot_token=None,
                redirect_uri="http://localhost/cb",
            )
            pending = await state_module.consume_state(token)

        assert pending is not None
        assert pending.bot_token is None

    async def test_secrets_never_stored_in_plaintext(
        self, app: Quart, fake_redis: FakeRedis
    ) -> None:
        """security.md Token & Secret Hygiene -- a Redis dump must never reveal the raw secret."""
        async with app.app_context():
            token = await state_module.create_state(
                tenant_id=1,
                admin_user_id=1,
                application_id="app-1",
                client_secret="super-secret-value",
                bot_token="super-secret-bot-token",
                redirect_uri="http://localhost/cb",
            )

        raw_value, _ = fake_redis.store[f"oauth:tenant_discord_install:state:{token}"]
        assert "super-secret-value" not in raw_value
        assert "super-secret-bot-token" not in raw_value

    async def test_state_is_single_use(self, app: Quart, fake_redis: FakeRedis) -> None:
        async with app.app_context():
            token = await state_module.create_state(
                tenant_id=1,
                admin_user_id=1,
                application_id="app-1",
                client_secret="secret-1",
                bot_token=None,
                redirect_uri="http://localhost/cb",
            )
            first = await state_module.consume_state(token)
            second = await state_module.consume_state(token)

        assert first is not None
        assert second is None

    async def test_unknown_token_returns_none(self, app: Quart, fake_redis: FakeRedis) -> None:
        async with app.app_context():
            result = await state_module.consume_state("never-issued-token")
        assert result is None

    async def test_empty_state_returns_none_without_touching_redis(
        self, app: Quart, fake_redis: FakeRedis
    ) -> None:
        async with app.app_context():
            result = await state_module.consume_state("")
        assert result is None
        assert fake_redis.store == {}

    async def test_expired_state_returns_none(self, app: Quart, fake_redis: FakeRedis) -> None:
        async with app.app_context():
            token = await state_module.create_state(
                tenant_id=1,
                admin_user_id=1,
                application_id="app-1",
                client_secret="secret-1",
                bot_token=None,
                redirect_uri="http://localhost/cb",
            )
            fake_redis.force_expire(f"oauth:tenant_discord_install:state:{token}")
            result = await state_module.consume_state(token)

        assert result is None

    async def test_redis_error_on_consume_returns_none(
        self, app: Quart, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _ExplodingRedis:
            async def getdel(self, _key: str) -> str:
                raise ConnectionError("redis unreachable")

        monkeypatch.setattr(state_module, "_redis_client", lambda: _ExplodingRedis())
        async with app.app_context():
            result = await state_module.consume_state("some-token")
        assert result is None

    async def test_malformed_payload_returns_none(self, app: Quart, fake_redis: FakeRedis) -> None:
        async with app.app_context():
            await fake_redis.set(
                "oauth:tenant_discord_install:state:bogus", "not-valid-json", ex=600
            )
            result = await state_module.consume_state("bogus")
        assert result is None

    async def test_payload_missing_required_field_returns_none(
        self, app: Quart, fake_redis: FakeRedis
    ) -> None:
        async with app.app_context():
            await fake_redis.set(
                "oauth:tenant_discord_install:state:bogus2",
                '{"tenant_id": 1, "admin_user_id": 1}',
                ex=600,
            )
            result = await state_module.consume_state("bogus2")
        assert result is None

    async def test_corrupt_ciphertext_returns_none(self, app: Quart, fake_redis: FakeRedis) -> None:
        """A tampered/corrupt ciphertext fails closed -- never leaks a decrypt exception."""
        async with app.app_context():
            await fake_redis.set(
                "oauth:tenant_discord_install:state:bogus3",
                (
                    '{"tenant_id": 1, "admin_user_id": 1, "application_id": "a",'
                    ' "client_secret": "not-valid-base64!!", "bot_token": null,'
                    ' "redirect_uri": "http://localhost/cb"}'
                ),
                ex=600,
            )
            result = await state_module.consume_state("bogus3")
        assert result is None


class TestRedisClientResolution:
    """Exercises `_redis_client()` itself -- bypassed by the `fake_redis` fixture elsewhere."""

    async def test_reuses_rate_limiter_connection_when_present(self, app: Quart) -> None:
        sentinel = object()

        class _FakeLimiter:
            _redis = sentinel

        app.config[RATE_LIMITER_CONFIG_KEY] = _FakeLimiter()
        async with app.app_context():
            client = state_module._redis_client()

        assert client is sentinel

    async def test_lazily_opens_and_caches_its_own_client(
        self, app: Quart, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sentinel = object()
        calls: list[str] = []

        def _fake_from_url(url: str, **_kwargs: Any) -> Any:
            calls.append(url)
            return sentinel

        monkeypatch.setattr("redis.asyncio.from_url", _fake_from_url)

        async with app.app_context():
            first = state_module._redis_client()
            second = state_module._redis_client()

        assert first is sentinel
        assert second is sentinel
        assert len(calls) == 1, "second call must hit the app.config cache, not open a new client"
