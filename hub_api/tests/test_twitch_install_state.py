"""Tests for `services/twitch_install_state.py` -- single-use Twitch-install CSRF state store.

Same `FakeRedis` + monkeypatched `_redis_client()` approach as
`test_oauth_connection_state.py` -- no real Redis dependency. Also covers
this module's own addition over that one: `client_secret` is encrypted at
rest in the stored payload.
"""

from __future__ import annotations

import time

import pytest
from quart import Quart

from config import HubAPIConfig
from services import twitch_install_state as state_module

_TEST_ENCRYPTION_KEY = "11" * 32  # 32 bytes hex -- matches platform_integrations_crypto's format


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
        value, _ = self.store[key]
        self.store[key] = (value, time.monotonic() - 1)


def _test_hub_config(*, ttl_s: int = 600) -> HubAPIConfig:
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
        connections_state_ttl_s=ttl_s,
    )


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", _TEST_ENCRYPTION_KEY)


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
                tenant_id=7,
                installed_by_user_id=42,
                client_id="tenant-client-id",
                client_secret="tenant-client-secret",  # noqa: S106
                redirect_uri="https://hub.example/api/v1/tenant/twitch-install/callback",
            )
            payload = await state_module.consume_state(token)

        assert payload == state_module.TwitchInstallStatePayload(
            tenant_id=7,
            installed_by_user_id=42,
            client_id="tenant-client-id",
            client_secret="tenant-client-secret",
            redirect_uri="https://hub.example/api/v1/tenant/twitch-install/callback",
        )

    async def test_client_secret_encrypted_at_rest(self, app: Quart, fake_redis: FakeRedis) -> None:
        async with app.app_context():
            token = await state_module.create_state(
                tenant_id=7,
                installed_by_user_id=42,
                client_id="tenant-client-id",
                client_secret="super-secret-app-value",  # noqa: S106
                redirect_uri="https://hub.example/cb",
            )

        raw_value, _ = fake_redis.store[f"{state_module._STATE_KEY_PREFIX}{token}"]
        assert "super-secret-app-value" not in raw_value

    async def test_state_is_single_use(self, app: Quart, fake_redis: FakeRedis) -> None:
        async with app.app_context():
            token = await state_module.create_state(
                tenant_id=1,
                installed_by_user_id=1,
                client_id="cid",
                client_secret="csecret",  # noqa: S106
                redirect_uri="https://hub.example/cb",
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
                installed_by_user_id=1,
                client_id="cid",
                client_secret="csecret",  # noqa: S106
                redirect_uri="https://hub.example/cb",
            )
            fake_redis.force_expire(f"{state_module._STATE_KEY_PREFIX}{token}")
            result = await state_module.consume_state(token)
        assert result is None

    async def test_malformed_payload_returns_none(self, app: Quart, fake_redis: FakeRedis) -> None:
        async with app.app_context():
            key = f"{state_module._STATE_KEY_PREFIX}bogus"
            await fake_redis.set(key, "not-json", ex=60)
            result = await state_module.consume_state("bogus")
        assert result is None

    async def test_undecryptable_secret_returns_none(
        self, app: Quart, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A key rotation between create and consume must fail closed, not raise/leak plaintext."""
        async with app.app_context():
            token = await state_module.create_state(
                tenant_id=1,
                installed_by_user_id=1,
                client_id="cid",
                client_secret="csecret",  # noqa: S106
                redirect_uri="https://hub.example/cb",
            )
            monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "22" * 32)
            result = await state_module.consume_state(token)
        assert result is None
