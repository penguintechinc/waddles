"""`services/oauth_connection_state.py` -- single-use OAuth `state` token store (gh-320, chunk C3).

`_redis_client()` is monkeypatched to a small in-process fake (no real
Redis/Valkey dependency in this suite) for every test except
`TestRedisClientResolution`, which exercises that function's own two
resolution paths (reuse `RateLimiter`'s connection, or lazily open and
cache one against `HubAPIConfig.valkey_url`) directly.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from quart import Quart

from config import HubAPIConfig
from services import oauth_connection_state as state_module
from services.install_state_binding import ERR_STATE, InstallStateError, nonce_hash
from services.rate_limiting import RATE_LIMITER_CONFIG_KEY

NONCE = "browser-nonce-0123456789"


class FakeRedis:
    """Minimal async fake covering only the two commands this module calls."""

    def __init__(self) -> None:
        """Start with an empty in-memory key/value store."""
        self.store: dict[str, tuple[str, float | None]] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        expires_at = time.monotonic() + ex if ex is not None else None
        self.store[key] = (value, expires_at)

    async def get(self, key: str) -> str | None:
        entry = self.store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at is not None and time.monotonic() > expires_at:
            return None
        return value

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
        default_tenant_slug="default",
        posthog_api_key=None,
        posthog_host="https://license.penguintech.io",
        license_server_url="https://license.penguintech.io",
        identity_callback_base_url="http://localhost:8204",
        frontend_origin="http://localhost:5173",
        log_level="INFO",
        connections_state_ttl_s=ttl_s,
    )


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
                binding_nonce=NONCE,
                community_id=1,
                provider="youtube",
                user_id=42,
                redirect_uri="http://localhost/cb/youtube",
                code_verifier="verifier-123",
            )
            payload = await state_module.consume_state(token, NONCE)

        assert payload == state_module.StatePayload(
            community_id=1,
            provider="youtube",
            user_id=42,
            redirect_uri="http://localhost/cb/youtube",
            code_verifier="verifier-123",
        )

    async def test_code_verifier_none_roundtrips_as_none(
        self, app: Quart, fake_redis: FakeRedis
    ) -> None:
        async with app.app_context():
            token = await state_module.create_state(
                binding_nonce=NONCE,
                community_id=2,
                provider="spotify",
                user_id=1,
                redirect_uri="http://localhost/cb/spotify",
                code_verifier=None,
            )
            payload = await state_module.consume_state(token, NONCE)

        assert payload is not None
        assert payload.code_verifier is None

    async def test_state_is_single_use(self, app: Quart, fake_redis: FakeRedis) -> None:
        async with app.app_context():
            token = await state_module.create_state(
                binding_nonce=NONCE,
                community_id=1,
                provider="twitch",
                user_id=1,
                redirect_uri="http://localhost/cb/twitch",
                code_verifier=None,
            )
            first = await state_module.consume_state(token, NONCE)
            with pytest.raises(InstallStateError) as replay:
                await state_module.consume_state(token, NONCE)

        assert first is not None
        assert replay.value.code == ERR_STATE

    async def test_unknown_token_returns_none(self, app: Quart, fake_redis: FakeRedis) -> None:
        async with app.app_context():
            with pytest.raises(InstallStateError) as refused:
                await state_module.consume_state("never-issued-token", NONCE)
            assert refused.value.code == ERR_STATE

    async def test_empty_state_returns_none_without_touching_redis(
        self, app: Quart, fake_redis: FakeRedis
    ) -> None:
        async with app.app_context():
            with pytest.raises(InstallStateError) as refused:
                await state_module.consume_state("", NONCE)
            assert refused.value.code == ERR_STATE
        assert fake_redis.store == {}

    async def test_expired_state_returns_none(self, app: Quart, fake_redis: FakeRedis) -> None:
        async with app.app_context():
            token = await state_module.create_state(
                binding_nonce=NONCE,
                community_id=1,
                provider="discord",
                user_id=1,
                redirect_uri="http://localhost/cb/discord",
                code_verifier=None,
            )
            fake_redis.force_expire(f"oauth:conn:state:{token}")
            with pytest.raises(InstallStateError) as refused:
                await state_module.consume_state(token, NONCE)
            assert refused.value.code == ERR_STATE

    async def test_redis_error_on_consume_returns_none(
        self, app: Quart, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _ExplodingRedis:
            async def get(self, _key: str) -> str:
                raise ConnectionError("redis unreachable")

            async def getdel(self, _key: str) -> str:
                raise ConnectionError("redis unreachable")

        monkeypatch.setattr(state_module, "_redis_client", lambda: _ExplodingRedis())
        async with app.app_context():
            with pytest.raises(InstallStateError) as refused:
                await state_module.consume_state("some-token", NONCE)
            assert refused.value.code == ERR_STATE

    async def test_malformed_payload_returns_none(self, app: Quart, fake_redis: FakeRedis) -> None:
        async with app.app_context():
            await fake_redis.set("oauth:conn:state:bogus", "not-valid-json", ex=600)
            with pytest.raises(InstallStateError) as refused:
                await state_module.consume_state("bogus", NONCE)
            assert refused.value.code == ERR_STATE

    async def test_payload_missing_required_field_returns_none(
        self, app: Quart, fake_redis: FakeRedis
    ) -> None:
        async with app.app_context():
            await fake_redis.set(
                "oauth:conn:state:bogus2",
                f'{{"community_id": 1, "provider": "kick", "binding": "{nonce_hash(NONCE)}"}}',
                ex=600,
            )
            with pytest.raises(InstallStateError) as refused:
                await state_module.consume_state("bogus2", NONCE)
            assert refused.value.code == ERR_STATE

    async def test_create_stores_under_configured_ttl(
        self, app: Quart, fake_redis: FakeRedis
    ) -> None:
        app.config["HUB_API_CONFIG"] = _test_hub_config(ttl_s=42)
        async with app.app_context():
            token = await state_module.create_state(
                binding_nonce=NONCE,
                community_id=1,
                provider="slack",
                user_id=1,
                redirect_uri="http://localhost/cb/slack",
                code_verifier=None,
            )
        _value, expires_at = fake_redis.store[f"oauth:conn:state:{token}"]
        assert expires_at is not None
        # Roughly 42s out -- generous bound, just proving the configured TTL
        # (not some other default) was the one passed to `set(..., ex=...)`.
        assert 40 <= expires_at - time.monotonic() <= 42


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
