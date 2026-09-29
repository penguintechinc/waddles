"""`services/license_service.py` -- PenguinTech license server integration, Redis-cached.

Complements `tests/test_license_service_examples.py` (dev-mode/basic
flows, pre-existing) with release-mode paths: `connect()`/`disconnect()`,
Redis cache hit/miss/error, `_validate_with_server` HTTP status branches,
and `invalidate_cache()`.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.license_service import (
    LicenseException,
    LicenseService,
    LicenseStatus,
    LicenseTier,
    LicenseValidationException,
)


def _service(**overrides: Any) -> LicenseService:
    defaults = dict(
        license_server_url="https://license.penguintech.io",
        redis_url=None,
        release_mode=True,
        logger_instance=MagicMock(),
    )
    defaults.update(overrides)
    return LicenseService(**defaults)


class _FakeResponse:
    def __init__(self, status: int, json_body: dict | None = None, text_body: str = "err") -> None:
        self.status = status
        self._json_body = json_body or {}
        self._text_body = text_body

    async def json(self) -> dict:
        return self._json_body

    async def text(self) -> str:
        return self._text_body

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class TestConnectDisconnect:
    @pytest.mark.asyncio
    async def test_connect_without_aiohttp_logs_and_returns(self) -> None:
        svc = _service()
        with patch("services.license_service.AIOHTTP_AVAILABLE", False):
            await svc.connect()
        svc.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_connect_creates_session_without_redis_url(self) -> None:
        svc = _service(redis_url=None)
        await svc.connect()
        assert svc._session is not None
        await svc.disconnect()

    @pytest.mark.asyncio
    async def test_connect_with_redis_success(self) -> None:
        svc = _service(redis_url="redis://localhost:6379/0")
        fake_redis = AsyncMock()
        with patch("services.license_service.REDIS_AVAILABLE", True), \
             patch("services.license_service.redis.from_url", return_value=fake_redis):
            await svc.connect()
        assert svc._redis_connected is True
        await svc.disconnect()

    @pytest.mark.asyncio
    async def test_connect_with_redis_failure(self) -> None:
        svc = _service(redis_url="redis://localhost:6379/0")
        with patch("services.license_service.REDIS_AVAILABLE", True), \
             patch("services.license_service.redis.from_url", side_effect=Exception("conn refused")):
            await svc.connect()
        assert svc._redis_connected is False
        await svc.disconnect()

    @pytest.mark.asyncio
    async def test_connect_session_creation_failure_logs(self) -> None:
        svc = _service()
        with patch("services.license_service.aiohttp.ClientSession", side_effect=Exception("boom")):
            await svc.connect()
        svc.logger.error.assert_called()

    @pytest.mark.asyncio
    async def test_disconnect_closes_session_and_redis(self) -> None:
        svc = _service()
        svc._session = AsyncMock()
        svc._redis = AsyncMock()
        svc._redis_connected = True
        await svc.disconnect()
        svc._session.close.assert_awaited_once()
        svc._redis.close.assert_awaited_once()
        assert svc._redis_connected is False

    @pytest.mark.asyncio
    async def test_disconnect_noop_when_nothing_connected(self) -> None:
        svc = _service()
        await svc.disconnect()  # must not raise


class TestCheckLicenseStatus:
    @pytest.mark.asyncio
    async def test_release_mode_missing_key_raises(self) -> None:
        svc = _service(release_mode=True)
        with pytest.raises(LicenseException, match="No license key"):
            await svc.check_license_status(1)

    @pytest.mark.asyncio
    async def test_release_mode_success(self) -> None:
        svc = _service(release_mode=True)
        svc._validate_with_server = AsyncMock(
            return_value={"status": "active", "tier": "premium", "features": {"workflows": True}}
        )
        result = await svc.check_license_status(1, license_key="PENG-KEY")
        assert result["status"] == "active"

    @pytest.mark.asyncio
    async def test_returns_cached_result(self) -> None:
        svc = _service(release_mode=True)
        svc._get_cached_license = AsyncMock(return_value={"status": "active"})
        result = await svc.check_license_status(1, license_key="key")
        assert result["cached"] is True

    @pytest.mark.asyncio
    async def test_generic_error_wrapped_in_license_exception(self) -> None:
        svc = _service(release_mode=True)
        svc._validate_with_server = AsyncMock(side_effect=ValueError("bad response"))
        with pytest.raises(LicenseException, match="License check failed"):
            await svc.check_license_status(1, license_key="key")

    @pytest.mark.asyncio
    async def test_dev_mode_returns_premium(self) -> None:
        svc = _service(release_mode=False)
        result = await svc.check_license_status(1)
        assert result["dev_mode"] is True
        assert result["tier"] == LicenseTier.PREMIUM.value


class TestValidateWorkflowCreation:
    @pytest.mark.asyncio
    async def test_inactive_license_raises(self) -> None:
        svc = _service()
        svc.check_license_status = AsyncMock(return_value={"status": "expired", "tier": "premium"})
        with pytest.raises(LicenseValidationException, match="not active"):
            await svc.validate_workflow_creation(1, "entity-1")

    @pytest.mark.asyncio
    async def test_free_tier_raises(self) -> None:
        svc = _service()
        svc.check_license_status = AsyncMock(
            return_value={"status": "active", "tier": "free"}
        )
        with pytest.raises(LicenseValidationException, match="Free tier"):
            await svc.validate_workflow_creation(1, "entity-1")

    @pytest.mark.asyncio
    async def test_premium_tier_succeeds(self) -> None:
        svc = _service()
        svc.check_license_status = AsyncMock(
            return_value={"status": "active", "tier": "premium"}
        )
        assert await svc.validate_workflow_creation(1, "entity-1") is True

    @pytest.mark.asyncio
    async def test_unexpected_error_wrapped(self) -> None:
        svc = _service()
        svc.check_license_status = AsyncMock(side_effect=RuntimeError("boom"))
        with pytest.raises(LicenseValidationException):
            await svc.validate_workflow_creation(1, "entity-1")


class TestValidateWorkflowExecution:
    @pytest.mark.asyncio
    async def test_inactive_license_raises(self) -> None:
        svc = _service()
        svc.check_license_status = AsyncMock(return_value={"status": "expired"})
        with pytest.raises(LicenseValidationException, match="not active"):
            await svc.validate_workflow_execution("wf-1", 1)

    @pytest.mark.asyncio
    async def test_workflows_feature_disabled_raises(self) -> None:
        svc = _service()
        svc.check_license_status = AsyncMock(
            return_value={"status": "active", "features": {"workflows": False}}
        )
        with pytest.raises(LicenseValidationException, match="not enabled"):
            await svc.validate_workflow_execution("wf-1", 1)

    @pytest.mark.asyncio
    async def test_success(self) -> None:
        svc = _service()
        svc.check_license_status = AsyncMock(
            return_value={"status": "active", "features": {"workflows": True}}
        )
        assert await svc.validate_workflow_execution("wf-1", 1) is True

    @pytest.mark.asyncio
    async def test_unexpected_error_wrapped(self) -> None:
        svc = _service()
        svc.check_license_status = AsyncMock(side_effect=RuntimeError("boom"))
        with pytest.raises(LicenseValidationException):
            await svc.validate_workflow_execution("wf-1", 1)


class TestGetLicenseInfo:
    @pytest.mark.asyncio
    async def test_free_tier_has_zero_workflow_limit(self) -> None:
        svc = _service()
        svc.check_license_status = AsyncMock(
            return_value={"status": "active", "tier": "free", "features": {}, "cached": False}
        )
        info = await svc.get_license_info(1)
        assert info["workflow_limit"] == 0

    @pytest.mark.asyncio
    async def test_premium_tier_has_unlimited_workflows(self) -> None:
        svc = _service()
        svc.check_license_status = AsyncMock(
            return_value={"status": "active", "tier": "premium", "features": {}, "cached": True}
        )
        info = await svc.get_license_info(1)
        assert info["workflow_limit"] is None

    @pytest.mark.asyncio
    async def test_exception_propagates(self) -> None:
        svc = _service()
        svc.check_license_status = AsyncMock(side_effect=LicenseException("boom"))
        with pytest.raises(LicenseException):
            await svc.get_license_info(1)


class TestCachePrivateMethods:
    @pytest.mark.asyncio
    async def test_get_cached_license_redis_hit(self) -> None:
        svc = _service()
        svc._redis_connected = True
        svc._redis = AsyncMock()
        svc._redis.get = AsyncMock(return_value='{"status": "active"}')
        result = await svc._get_cached_license(1)
        assert result == {"status": "active"}

    @pytest.mark.asyncio
    async def test_get_cached_license_redis_miss(self) -> None:
        svc = _service()
        svc._redis_connected = True
        svc._redis = AsyncMock()
        svc._redis.get = AsyncMock(return_value=None)
        assert await svc._get_cached_license(1) is None

    @pytest.mark.asyncio
    async def test_get_cached_license_in_memory(self) -> None:
        svc = _service()
        svc._cache["license:community:1"] = {"status": "active"}
        result = await svc._get_cached_license(1)
        assert result == {"status": "active"}

    @pytest.mark.asyncio
    async def test_get_cached_license_swallows_error(self) -> None:
        svc = _service()
        svc._redis_connected = True
        svc._redis = AsyncMock()
        svc._redis.get = AsyncMock(side_effect=Exception("redis down"))
        assert await svc._get_cached_license(1) is None

    @pytest.mark.asyncio
    async def test_cache_license_redis(self) -> None:
        svc = _service()
        svc._redis_connected = True
        svc._redis = AsyncMock()
        await svc._cache_license(1, {"status": "active"})
        svc._redis.setex.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cache_license_in_memory(self) -> None:
        svc = _service()
        await svc._cache_license(1, {"status": "active"})
        assert svc._cache["license:community:1"] == {"status": "active"}

    @pytest.mark.asyncio
    async def test_cache_license_swallows_error(self) -> None:
        svc = _service()
        svc._redis_connected = True
        svc._redis = AsyncMock()
        svc._redis.setex = AsyncMock(side_effect=Exception("redis down"))
        await svc._cache_license(1, {"status": "active"})  # must not raise


class TestValidateWithServer:
    @pytest.mark.asyncio
    async def test_no_session_raises(self) -> None:
        svc = _service()
        with pytest.raises(LicenseException, match="not initialized"):
            await svc._validate_with_server(1, "key")

    @pytest.mark.asyncio
    async def test_success(self) -> None:
        svc = _service()
        svc._session = MagicMock()
        svc._session.post = MagicMock(
            return_value=_FakeResponse(
                200, json_body={"status": "active", "tier": "premium", "features": {"workflows": True}}
            )
        )
        result = await svc._validate_with_server(1, "key")
        assert result["status"] == "active"

    @pytest.mark.asyncio
    async def test_not_found(self) -> None:
        svc = _service()
        svc._session = MagicMock()
        svc._session.post = MagicMock(return_value=_FakeResponse(404))
        with pytest.raises(LicenseException, match="not found"):
            await svc._validate_with_server(1, "key")

    @pytest.mark.asyncio
    async def test_unauthorized(self) -> None:
        svc = _service()
        svc._session = MagicMock()
        svc._session.post = MagicMock(return_value=_FakeResponse(401))
        with pytest.raises(LicenseException, match="Invalid license key"):
            await svc._validate_with_server(1, "key")

    @pytest.mark.asyncio
    async def test_other_error_status(self) -> None:
        svc = _service()
        svc._session = MagicMock()
        svc._session.post = MagicMock(return_value=_FakeResponse(500, text_body="server error"))
        with pytest.raises(LicenseException, match="License server error"):
            await svc._validate_with_server(1, "key")

    @pytest.mark.asyncio
    async def test_timeout(self) -> None:
        svc = _service()
        svc._session = MagicMock()

        class _Raiser:
            def __call__(self, *a: Any, **k: Any) -> "_Raiser":
                return self

            async def __aenter__(self) -> Any:
                raise asyncio.TimeoutError()

            async def __aexit__(self, *exc: Any) -> None:
                return None

        svc._session.post = _Raiser()
        with pytest.raises(LicenseException, match="timed out"):
            await svc._validate_with_server(1, "key")

    @pytest.mark.asyncio
    async def test_client_error(self) -> None:
        import aiohttp

        svc = _service()
        svc._session = MagicMock()

        class _Raiser:
            def __call__(self, *a: Any, **k: Any) -> "_Raiser":
                return self

            async def __aenter__(self) -> Any:
                raise aiohttp.ClientConnectionError("connection refused")

            async def __aexit__(self, *exc: Any) -> None:
                return None

        svc._session.post = _Raiser()
        with pytest.raises(LicenseException, match="connection failed"):
            await svc._validate_with_server(1, "key")


class TestInvalidateCache:
    @pytest.mark.asyncio
    async def test_invalidate_redis(self) -> None:
        svc = _service()
        svc._redis_connected = True
        svc._redis = AsyncMock()
        await svc.invalidate_cache(1)
        svc._redis.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_invalidate_in_memory(self) -> None:
        svc = _service()
        svc._cache["license:community:1"] = {"status": "active"}
        await svc.invalidate_cache(1)
        assert "license:community:1" not in svc._cache

    @pytest.mark.asyncio
    async def test_invalidate_swallows_error(self) -> None:
        svc = _service()
        svc._redis_connected = True
        svc._redis = AsyncMock()
        svc._redis.delete = AsyncMock(side_effect=Exception("redis down"))
        await svc.invalidate_cache(1)  # must not raise
