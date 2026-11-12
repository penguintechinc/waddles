"""`services/event_calendar_proxy.py` -- unit tests for the real HTTP-forwarding logic.

`tests/test_event_blueprint.py` monkeypatches `EventCalendarProxyClient.
request` entirely (no real network I/O in route-level tests, per
`implementing-database-patterns`'s "mock external APIs" convention) --
which means the actual `httpx` call, header construction, and success/
failure/timeout branching inside `request()` itself is exercised nowhere
else. This file closes that gap with `httpx.MockTransport` (real `httpx`
request/response objects, no real socket).
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from services.event_calendar_proxy import (
    EventCalendarProxyClient,
    EventCalendarProxyConfig,
    ProxyResult,
    UserContext,
)

_CONFIG = EventCalendarProxyConfig(
    base_url="http://calendar-interaction:8038", api_key="test-svc-key", timeout_seconds=1.0
)


def _patch_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    """Force every `httpx.AsyncClient` built inside `event_calendar_proxy.py` onto a mock transport.

    `event_calendar_proxy.py` constructs `httpx.AsyncClient(timeout=...)`
    inline with no injectable transport param -- rather than adding a
    test-only constructor seam to production code, patch the class's own
    `__init__` for the duration of one test (auto-reverted by
    `monkeypatch`) to always attach `transport`.
    """
    transport = httpx.MockTransport(handler)
    original_init = httpx.AsyncClient.__init__

    def patched_init(self: httpx.AsyncClient, *args: object, **kwargs: object) -> None:
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


class TestUserContextSerialization:
    def test_identified_caller(self) -> None:
        ctx = UserContext(user_id="u1", username="alice", role="admin")
        assert json.loads(ctx.to_header_json()) == {
            "user_id": "u1",
            "username": "alice",
            "platform": "hub",
            "platform_user_id": "u1",
            "role": "admin",
        }

    def test_anonymous_caller(self) -> None:
        ctx = UserContext(user_id=None, username=None, role="anonymous")
        assert json.loads(ctx.to_header_json()) == {
            "user_id": None,
            "username": None,
            "platform": "hub",
            "platform_user_id": "anonymous",
            "role": "anonymous",
        }


class TestEventCalendarProxyConfigFromEnv:
    def test_defaults_with_no_env_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("CALENDAR_API_URL", "SERVICE_API_KEY", "CALENDAR_PROXY_TIMEOUT_MS"):
            monkeypatch.delenv(name, raising=False)
        cfg = EventCalendarProxyConfig.from_env()
        assert cfg.base_url == ""
        assert cfg.timeout_seconds == 5.0

    def test_reads_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALENDAR_API_URL", "http://calendar-interaction.svc:9000")
        monkeypatch.setenv("SERVICE_API_KEY", "real-key")
        monkeypatch.setenv("CALENDAR_PROXY_TIMEOUT_MS", "2500")
        cfg = EventCalendarProxyConfig.from_env()
        assert cfg.base_url == "http://calendar-interaction.svc:9000"
        assert cfg.api_key == "real-key"
        assert cfg.timeout_seconds == 2.5


class TestEventCalendarProxyClientRequest:
    async def test_success_relays_status_and_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": 1, "name": "Standup"})

        _patch_transport(monkeypatch, handler)
        client = EventCalendarProxyClient(_CONFIG)
        result = await client.request(
            "GET",
            "/api/v1/calendar/booking-pages/1",
            user_context=UserContext(user_id="u1", username="alice", role="admin"),
        )
        assert result == ProxyResult(ok=True, status_code=200, body={"id": 1, "name": "Standup"})

    async def test_sends_expected_headers_method_and_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["url"] = str(request.url)
            captured["headers"] = dict(request.headers)
            return httpx.Response(200, json={})

        _patch_transport(monkeypatch, handler)
        client = EventCalendarProxyClient(_CONFIG)
        await client.request(
            "POST",
            "/api/v1/calendar/booking-pages",
            user_context=UserContext(user_id="u9", username="bob", role="admin"),
            json_body={"name": "New page"},
        )
        assert captured["method"] == "POST"
        assert captured["url"] == "http://calendar-interaction:8038/api/v1/calendar/booking-pages"
        headers = captured["headers"]
        assert isinstance(headers, dict)
        assert headers["x-api-key"] == "test-svc-key"
        assert headers["content-type"] == "application/json"
        assert json.loads(headers["x-user-context"])["username"] == "bob"

    async def test_query_params_are_forwarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["query"] = dict(request.url.params)
            return httpx.Response(200, json={})

        _patch_transport(monkeypatch, handler)
        client = EventCalendarProxyClient(_CONFIG)
        await client.request(
            "GET",
            "/api/v1/calendar/availability/7/slots",
            user_context=UserContext(user_id=None, username=None, role="anonymous"),
            query={"start": "2026-09-01", "end": "2026-09-02"},
        )
        assert captured["query"] == {"start": "2026-09-01", "end": "2026-09-02"}

    async def test_downstream_4xx_is_ok_false_with_relayed_status(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": "not found"})

        _patch_transport(monkeypatch, handler)
        client = EventCalendarProxyClient(_CONFIG)
        result = await client.request(
            "GET",
            "/api/v1/calendar/bookings/missing",
            user_context=UserContext(user_id="u1", username="alice", role="admin"),
        )
        assert result.ok is False
        assert result.status_code == 404
        assert result.body == {"error": "not found"}

    async def test_non_json_response_body_is_none_not_a_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not json")

        _patch_transport(monkeypatch, handler)
        client = EventCalendarProxyClient(_CONFIG)
        result = await client.request(
            "GET",
            "/api/v1/calendar/booking-pages",
            user_context=UserContext(user_id="u1", username="alice", role="admin"),
        )
        assert result.ok is True
        assert result.body is None

    async def test_transport_error_is_masked_as_502(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("connection timed out", request=request)

        _patch_transport(monkeypatch, handler)
        client = EventCalendarProxyClient(_CONFIG)
        result = await client.request(
            "GET",
            "/api/v1/calendar/my-bookings",
            user_context=UserContext(user_id="u1", username="alice", role="admin"),
        )
        assert result == ProxyResult(ok=False, status_code=502, body=None)


@pytest.mark.asyncio
async def test_unset_base_url_returns_typed_503_and_logs_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """regression: legacy calendar-interaction deleted -- unset URL is a loud typed 503."""
    from services.event_calendar_proxy import EventCalendarProxyClient, UserContext

    client = EventCalendarProxyClient(
        EventCalendarProxyConfig(base_url="", api_key="k", timeout_seconds=1.0)
    )
    with caplog.at_level("ERROR"):
        result = await client.request(
            "GET",
            "/api/v1/calendar/x",
            user_context=UserContext(user_id="u", username=None, role="user"),
        )
    assert (result.ok, result.status_code) == (False, 503)
    assert result.body == {"error": "service_unavailable", "service": "calendar-interaction"}
    assert any("event_calendar_proxy_service_unavailable" in r.message for r in caplog.records)
