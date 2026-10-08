"""Tests for `services.event_discord_sync_client` -- the real HTTP client to hub-api.

Mocks `httpx.AsyncClient.post` (the ONE call this module ever makes),
mirroring `core/svc_process/tests/test_reputation_gate_client.py`'s
established pattern for this exact shape of real-HTTP-client test: asserts
the exact URL/payload/headers built for a given call, and every branch of
the graceful-degradation outcome mapping (200 / non-2xx / unreachable /
malformed body / application-level sync_error).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from services.event_discord_sync_client import (
    EventDiscordSyncClient,
    get_event_discord_sync_client,
    reset_for_tests,
)


class _FakeResponse:
    def __init__(self, status_code: int, json_body: Any) -> None:
        self.status_code = status_code
        self._json_body = json_body
        self.text = str(json_body)

    def json(self) -> Any:
        if isinstance(self._json_body, Exception):
            raise self._json_body
        return self._json_body


class TestSyncEventSuccess:
    async def test_builds_correct_request_and_returns_ok(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> _FakeResponse:
            captured["url"] = url
            captured["headers"] = kwargs["headers"]
            captured["json"] = kwargs["json"]
            return _FakeResponse(
                200,
                {
                    "status": "success",
                    "data": {
                        "event_id": 7,
                        "discord_event_id": "d-123",
                        "sync_status": "synced",
                        "sync_error": None,
                    },
                },
            )

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        client = EventDiscordSyncClient(base_url="http://hub-api-test.invalid:8204", service_api_key="sekrit")
        result = await client.sync_event(7, "create")

        assert result.ok is True
        assert result.event_id == 7
        assert result.discord_event_id == "d-123"
        assert result.sync_status == "synced"
        assert result.sync_error is None
        assert captured["url"] == (
            "http://hub-api-test.invalid:8204/api/v1/internal/calendar/events/sync-discord"
        )
        assert captured["headers"] == {"X-Service-Key": "sekrit"}
        assert captured["json"] == {"event_id": 7, "action": "create"}

    async def test_application_level_sync_error_still_ok_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """hub-api itself is fail-closed -- a Discord-side failure is still a 200."""

        async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> _FakeResponse:
            return _FakeResponse(
                200,
                {
                    "status": "success",
                    "data": {
                        "event_id": 7,
                        "discord_event_id": None,
                        "sync_status": "sync_error",
                        "sync_error": "discord unreachable",
                    },
                },
            )

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        client = EventDiscordSyncClient(base_url="http://hub-api-test.invalid:8204")
        result = await client.sync_event(7, "update")

        assert result.ok is True
        assert result.sync_status == "sync_error"
        assert result.sync_error == "discord unreachable"

    async def test_invalid_action_never_calls_hub_api(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called = False

        async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> _FakeResponse:
            nonlocal called
            called = True
            return _FakeResponse(200, {"status": "success", "data": {}})

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        client = EventDiscordSyncClient(base_url="http://hub-api-test.invalid:8204")
        result = await client.sync_event(7, "delete-forever")

        assert result.ok is False
        assert called is False


class TestSyncEventGracefulDegradation:
    """Every branch degrades to `ok=False` -- never raise."""

    async def test_network_failure_never_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> _FakeResponse:
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        client = EventDiscordSyncClient(base_url="http://hub-api-test.invalid:8204")
        result = await client.sync_event(1, "create")

        assert result.ok is False
        assert result.sync_error is not None

    async def test_non_2xx_status_degrades_to_ok_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> _FakeResponse:
            return _FakeResponse(404, {"success": False, "error": "not found"})

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        client = EventDiscordSyncClient(base_url="http://hub-api-test.invalid:8204")
        result = await client.sync_event(1, "create")

        assert result.ok is False
        assert "404" in (result.sync_error or "")

    async def test_malformed_json_response_degrades_to_ok_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> _FakeResponse:
            return _FakeResponse(200, ValueError("not json"))

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        client = EventDiscordSyncClient(base_url="http://hub-api-test.invalid:8204")
        result = await client.sync_event(1, "create")

        assert result.ok is False

    async def test_missing_data_envelope_degrades_to_ok_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> _FakeResponse:
            return _FakeResponse(200, {"status": "success"})

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        client = EventDiscordSyncClient(base_url="http://hub-api-test.invalid:8204")
        result = await client.sync_event(1, "create")

        assert result.ok is False


class TestSetEventSyncEnabled:
    async def test_builds_correct_request_and_returns_ok(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> _FakeResponse:
            captured["url"] = url
            captured["json"] = kwargs["json"]
            return _FakeResponse(
                200,
                {
                    "status": "success",
                    "data": {
                        "community_id": 9,
                        "event_sync_enabled": True,
                        "pairings_updated": 2,
                    },
                },
            )

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        client = EventDiscordSyncClient(base_url="http://hub-api-test.invalid:8204", service_api_key="k")
        result = await client.set_event_sync_enabled(9, True)

        assert result.ok is True
        assert result.community_id == 9
        assert result.event_sync_enabled is True
        assert result.pairings_updated == 2
        assert captured["url"] == (
            "http://hub-api-test.invalid:8204/api/v1/internal/calendar/guild-pairings/event-sync"
        )
        assert captured["json"] == {"community_id": 9, "enabled": True}

    async def test_no_pairings_404_degrades_to_ok_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> _FakeResponse:
            return _FakeResponse(404, {"success": False, "error": {"message": "no pairings"}})

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        client = EventDiscordSyncClient(base_url="http://hub-api-test.invalid:8204")
        result = await client.set_event_sync_enabled(9, True)

        assert result.ok is False
        assert result.pairings_updated == 0


class TestGetEventDiscordSyncClient:
    def teardown_method(self) -> None:
        reset_for_tests()

    def test_returns_a_client(self) -> None:
        reset_for_tests()
        client = get_event_discord_sync_client()
        assert isinstance(client, EventDiscordSyncClient)

    def test_singleton_reused_across_calls(self) -> None:
        reset_for_tests()
        first = get_event_discord_sync_client()
        second = get_event_discord_sync_client()
        assert first is second

    def test_reset_for_tests_clears_singleton(self) -> None:
        reset_for_tests()
        first = get_event_discord_sync_client()
        reset_for_tests()
        second = get_event_discord_sync_client()
        assert first is not second


class TestConfigDefaults:
    def test_constructor_defaults_come_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HUB_API_URL", "http://from-env.invalid:9000")
        monkeypatch.setenv("SERVICE_API_KEY", "env-key")

        client = EventDiscordSyncClient()

        assert client._base_url == "http://from-env.invalid:9000"
        assert client._service_api_key == "env-key"

    def test_constructor_args_override_env_defaults(self) -> None:
        client = EventDiscordSyncClient(
            base_url="http://explicit.invalid:1234", service_api_key="explicit-key"
        )

        assert client._base_url == "http://explicit.invalid:1234"
        assert client._service_api_key == "explicit-key"
