"""`services/calls_proxy.py` -- unit tests for the real HTTP-forwarding logic.

`tests/test_v1_calls_blueprint.py` monkeypatches `CallsProxyClient.request`
entirely (no real network I/O in route-level tests) -- which means the
actual `httpx` call, header construction, and success/failure/timeout
branching inside `request()` itself is exercised nowhere else. This file
closes that gap with `httpx.MockTransport`, mirroring `tests/
test_event_calendar_proxy.py`'s own pattern exactly (real `httpx`
request/response objects, no real socket).
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from services.calls_proxy import (
    CallsProxyClient,
    CallsProxyConfig,
    StreamingUrlNotConfiguredError,
)
from services.event_calendar_proxy import ProxyResult

_CONFIG = CallsProxyConfig(base_url="http://svc-streaming:8093", timeout_seconds=1.0)


def _patch_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    """Force every `httpx.AsyncClient` built inside `calls_proxy.py` onto a mock transport."""
    transport = httpx.MockTransport(handler)
    original_init = httpx.AsyncClient.__init__

    def patched_init(self: httpx.AsyncClient, *args: object, **kwargs: object) -> None:
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


class TestCallsProxyConfigFromEnv:
    def test_unset_url_fails_loud_instead_of_guessing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in ("STREAMING_RTC_URL", "MODULE_RTC_URL"):
            monkeypatch.delenv(name, raising=False)
        with pytest.raises(StreamingUrlNotConfiguredError, match="STREAMING_RTC_URL"):
            CallsProxyConfig.from_env()

    def test_blank_url_is_treated_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STREAMING_RTC_URL", "  ")
        monkeypatch.delenv("MODULE_RTC_URL", raising=False)
        with pytest.raises(StreamingUrlNotConfiguredError):
            CallsProxyConfig.from_env()

    def test_helm_rendered_url_and_default_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STREAMING_RTC_URL", "http://waddlebot-svc-streaming:8208/")
        monkeypatch.delenv("STREAMING_RTC_TIMEOUT_SECONDS", raising=False)
        cfg = CallsProxyConfig.from_env()
        assert cfg.base_url == "http://waddlebot-svc-streaming:8208"
        assert cfg.timeout_seconds == 10.0

    async def test_client_without_url_raises_on_first_request_not_at_construction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in ("STREAMING_RTC_URL", "MODULE_RTC_URL"):
            monkeypatch.delenv(name, raising=False)
        client = CallsProxyClient()  # import-time construction must stay safe
        with pytest.raises(StreamingUrlNotConfiguredError):
            await client.request("GET", "/api/v1/rooms", authorization=None)

    def test_falls_back_to_module_rtc_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("STREAMING_RTC_URL", raising=False)
        monkeypatch.setenv("MODULE_RTC_URL", "http://core-module-rtc:8093")
        cfg = CallsProxyConfig.from_env()
        assert cfg.base_url == "http://core-module-rtc:8093"

    def test_streaming_rtc_url_takes_priority(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STREAMING_RTC_URL", "http://svc-streaming.svc:9000")
        monkeypatch.setenv("MODULE_RTC_URL", "http://core-module-rtc:8093")
        cfg = CallsProxyConfig.from_env()
        assert cfg.base_url == "http://svc-streaming.svc:9000"


class TestCallsProxyClientRequest:
    async def test_success_relays_status_and_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"rooms": []})

        _patch_transport(monkeypatch, handler)
        client = CallsProxyClient(_CONFIG)
        result = await client.request(
            "GET", "/api/v1/rooms", authorization="Bearer tok", params={"community_id": "1"}
        )
        assert result == ProxyResult(ok=True, status_code=200, body={"rooms": []})

    async def test_forwards_authorization_header_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["headers"] = dict(request.headers)
            captured["method"] = request.method
            captured["url"] = str(request.url)
            return httpx.Response(200, json={})

        _patch_transport(monkeypatch, handler)
        client = CallsProxyClient(_CONFIG)
        await client.request(
            "POST",
            "/api/v1/rooms",
            authorization="Bearer abc123",
            json_body={"room_name": "room1"},
        )
        headers = captured["headers"]
        assert isinstance(headers, dict)
        assert headers["authorization"] == "Bearer abc123"
        assert headers["content-type"] == "application/json"
        assert captured["method"] == "POST"
        assert captured["url"] == "http://svc-streaming:8093/api/v1/rooms"

    async def test_no_authorization_header_when_none_given(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["headers"] = dict(request.headers)
            return httpx.Response(200, json={})

        _patch_transport(monkeypatch, handler)
        client = CallsProxyClient(_CONFIG)
        await client.request("GET", "/api/v1/rooms", authorization=None)
        headers = captured["headers"]
        assert isinstance(headers, dict)
        assert "authorization" not in headers

    async def test_downstream_4xx_is_ok_false_with_relayed_status(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": "room not found"})

        _patch_transport(monkeypatch, handler)
        client = CallsProxyClient(_CONFIG)
        result = await client.request("GET", "/api/v1/rooms/missing", authorization="Bearer t")
        assert result.ok is False
        assert result.status_code == 404
        assert result.body == {"error": "room not found"}

    async def test_non_json_response_body_is_none_not_a_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not json")

        _patch_transport(monkeypatch, handler)
        client = CallsProxyClient(_CONFIG)
        result = await client.request("GET", "/api/v1/rooms", authorization="Bearer t")
        assert result.ok is True
        assert result.body is None

    async def test_transport_error_is_masked_as_502(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("connection timed out", request=request)

        _patch_transport(monkeypatch, handler)
        client = CallsProxyClient(_CONFIG)
        result = await client.request("GET", "/api/v1/rooms", authorization="Bearer t")
        assert result == ProxyResult(ok=False, status_code=502, body=None)
