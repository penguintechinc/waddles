"""`services/analytics_proxy.py` -- unit tests for the real HTTP-forwarding logic.

`tests/test_v1_analytics_blueprint.py` monkeypatches `AnalyticsCoreProxyClient.
get` entirely (no real network I/O in route-level tests) -- which means the
actual `httpx` call, header construction, and success/failure/timeout
branching inside `get()` itself is exercised nowhere else. This file closes
that gap with `httpx.MockTransport` (real `httpx` request/response objects,
no real socket), mirroring `tests/test_event_calendar_proxy.py`'s own
pattern for `EventCalendarProxyClient`.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from services.analytics_proxy import AnalyticsCoreProxyClient, AnalyticsProxyConfig, ProxyResult

_CONFIG = AnalyticsProxyConfig(
    base_url="http://analytics-core:8040", api_key="test-svc-key", timeout_seconds=1.0
)


def _patch_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    """Force every `httpx.AsyncClient` built inside `analytics_proxy.py` onto a mock transport.

    Same technique `test_event_calendar_proxy.py::_patch_transport` uses --
    patch the class's own `__init__` for one test (auto-reverted by
    `monkeypatch`) rather than adding a test-only constructor seam to
    production code.
    """
    transport = httpx.MockTransport(handler)
    original_init = httpx.AsyncClient.__init__

    def patched_init(self: httpx.AsyncClient, *args: object, **kwargs: object) -> None:
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


class TestAnalyticsProxyConfigFromEnv:
    def test_defaults_with_no_env_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("ANALYTICS_CORE_API_URL", "SERVICE_API_KEY", "ANALYTICS_PROXY_TIMEOUT_MS"):
            monkeypatch.delenv(name, raising=False)
        cfg = AnalyticsProxyConfig.from_env()
        assert cfg.base_url == "http://analytics-core:8040"
        assert cfg.timeout_seconds == 10.0

    def test_reads_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANALYTICS_CORE_API_URL", "http://analytics-core.svc:9000")
        monkeypatch.setenv("SERVICE_API_KEY", "real-key")
        monkeypatch.setenv("ANALYTICS_PROXY_TIMEOUT_MS", "2500")
        cfg = AnalyticsProxyConfig.from_env()
        assert cfg.base_url == "http://analytics-core.svc:9000"
        assert cfg.api_key == "real-key"
        assert cfg.timeout_seconds == 2.5


class TestAnalyticsCoreProxyClientGet:
    async def test_success_relays_status_and_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"success": True, "data": {"total_users": 5}})

        _patch_transport(monkeypatch, handler)
        client = AnalyticsCoreProxyClient(_CONFIG)
        result = await client.get("/api/v1/analytics/platform/summary")
        assert result == ProxyResult(
            ok=True, status_code=200, body={"success": True, "data": {"total_users": 5}}
        )

    async def test_platform_call_omits_caller_headers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["headers"] = dict(request.headers)
            return httpx.Response(200, json={})

        _patch_transport(monkeypatch, handler)
        client = AnalyticsCoreProxyClient(_CONFIG)
        await client.get("/api/v1/analytics/platform/summary")
        headers = captured["headers"]
        assert isinstance(headers, dict)
        assert headers["x-service-key"] == "test-svc-key"
        assert "x-caller-user-id" not in headers
        assert "x-caller-role" not in headers

    async def test_user_scoped_call_sends_caller_headers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["url"] = str(request.url)
            captured["headers"] = dict(request.headers)
            return httpx.Response(200, json={})

        _patch_transport(monkeypatch, handler)
        client = AnalyticsCoreProxyClient(_CONFIG)
        await client.get("/api/v1/analytics/user/42/self", caller_user_id=42, caller_role="user")
        assert captured["method"] == "GET"
        assert captured["url"] == "http://analytics-core:8040/api/v1/analytics/user/42/self"
        headers = captured["headers"]
        assert isinstance(headers, dict)
        assert headers["x-service-key"] == "test-svc-key"
        assert headers["x-caller-user-id"] == "42"
        assert headers["x-caller-role"] == "user"

    async def test_caller_role_defaults_to_user_when_omitted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["headers"] = dict(request.headers)
            return httpx.Response(200, json={})

        _patch_transport(monkeypatch, handler)
        client = AnalyticsCoreProxyClient(_CONFIG)
        await client.get("/api/v1/analytics/user/42/self", caller_user_id=42)
        headers = captured["headers"]
        assert isinstance(headers, dict)
        assert headers["x-caller-role"] == "user"

    async def test_query_params_are_forwarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["query"] = dict(request.url.params)
            return httpx.Response(200, json={})

        _patch_transport(monkeypatch, handler)
        client = AnalyticsCoreProxyClient(_CONFIG)
        await client.get("/api/v1/analytics/platform/growth", query={"period": "30d"})
        assert captured["query"] == {"period": "30d"}

    async def test_downstream_4xx_is_ok_false_with_relayed_status(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "period must be 30d, 90d, or 1y"})

        _patch_transport(monkeypatch, handler)
        client = AnalyticsCoreProxyClient(_CONFIG)
        result = await client.get("/api/v1/analytics/platform/growth", query={"period": "bogus"})
        assert result.ok is False
        assert result.status_code == 400
        assert result.body == {"error": "period must be 30d, 90d, or 1y"}

    async def test_non_json_response_body_is_none_not_a_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not json")

        _patch_transport(monkeypatch, handler)
        client = AnalyticsCoreProxyClient(_CONFIG)
        result = await client.get("/api/v1/analytics/platform/summary")
        assert result.ok is True
        assert result.body is None

    async def test_transport_error_is_masked_as_502(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("connection timed out", request=request)

        _patch_transport(monkeypatch, handler)
        client = AnalyticsCoreProxyClient(_CONFIG)
        result = await client.get("/api/v1/analytics/platform/summary")
        assert result == ProxyResult(ok=False, status_code=502, body=None, transport_error=True)
