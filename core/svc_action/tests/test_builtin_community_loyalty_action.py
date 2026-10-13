"""Tests for `builtin_handlers.community_loyalty_action.loyalty`.

Covers: the exact HTTP calls made to hub-api's internal Community Loyalty
routes, every reply text (balance/top/shop/redeem/adjust success, 409
relay, 5xx/timeout graceful degradation), reply-in-place via Twitch/
Discord, and input validation.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from flask_core import PlatformEvent, StageEnvelope
from waddle_transports import NonRetryableTransportError

from builtin_handlers.community_loyalty_action import (
    _LEADERBOARD_EMPTY_SUFFIX,
    _SHOP_EMPTY_REPLY,
    loyalty,
)

_BALANCE_URL_FRAGMENT = "/api/v1/internal/loyalty/balance"
_LEADERBOARD_URL_FRAGMENT = "/api/v1/internal/loyalty/leaderboard"
_ITEMS_URL_FRAGMENT = "/api/v1/internal/loyalty/items"
_ADJUST_URL_FRAGMENT = "/api/v1/internal/loyalty/adjust"
_REDEEM_URL_FRAGMENT = "/api/v1/internal/loyalty/redeem"


def _envelope(
    payload: dict[str, object],
    *,
    platform: str = "twitch",
    community: str | None = "42",
    actor: str | None = "penguin",
) -> StageEnvelope:
    return StageEnvelope(
        tenant="global",
        community=community,
        app_id="waddles.community.loyalty.default",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="message",
            actor=actor,
            payload=payload,
            occurred_at="2026-09-11T00:00:00Z",
        ),
        ts="2026-09-11T00:00:00Z",
    )


def _base_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "channel_id": "123",
        "channel_name": "testchannel",
        "author_id": "platform-user-1",
        **overrides,
    }
    return payload


def _config(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "bot_token_ref": "TEST_DISCORD_TOKEN",
        "api_base": "https://8.8.8.8/v1",
        "channel_id": "fallback-chan",
    }
    base.update(overrides)
    return base


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


def _relay_transport(sent: dict[str, Any]) -> AsyncMock:
    """A fake `RelayOutboundIrcTransport` recording the (target, message) it was sent."""
    transport = AsyncMock()

    async def _send(target: dict[str, object], message: dict[str, object]) -> MagicMock:
        sent["target"] = target
        sent["message"] = message
        return MagicMock(transport="relay", detail="sent", http_status=200)

    transport.send = _send
    return transport


async def _call(
    payload: dict[str, object], handler: Any, *, platform: str = "twitch"
) -> tuple[Any, dict[str, Any]]:
    sent: dict[str, Any] = {}
    async with _client(handler) as client:
        with patch(
            "builtin_handlers.community_loyalty_action.RelayOutboundIrcTransport",
            return_value=_relay_transport(sent),
        ):
            result = await loyalty(
                _envelope(payload, platform=platform), _config(), http_client=client
            )
    return result, sent


class TestBalanceOwn:
    """`!points` -- caller's own balance, no `target` in payload."""

    async def test_posts_correct_query_params_and_service_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SERVICE_API_KEY", "s3cr3t")
        monkeypatch.setenv("HUB_API_URL", "https://hub-api.internal")
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["method"] = request.method
            captured["service_key"] = request.headers.get("x-service-key")
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {"balance": 120, "currency_name": "points"},
                },
            )

        await _call(_base_payload(subcommand="balance"), handler)

        assert captured["method"] == "GET"
        assert captured["url"].startswith(f"https://hub-api.internal{_BALANCE_URL_FRAGMENT}")
        assert "community_id=42" in captured["url"]
        assert "platform=twitch" in captured["url"]
        assert "platform_user_id=platform-user-1" in captured["url"]
        assert captured["service_key"] == "s3cr3t"

    async def test_success_reply(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"status": "success", "data": {"balance": 120, "currency_name": "gems"}}
            )

        result, sent = await _call(_base_payload(subcommand="balance"), handler)
        assert sent["message"]["text"] == "you have 120 gems"
        assert result.http_status == 200

    async def test_currency_name_defaults_to_points_when_absent(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "success", "data": {"balance": 5}})

        _result, sent = await _call(_base_payload(subcommand="balance"), handler)
        assert sent["message"]["text"] == "you have 5 points"

    async def test_missing_target_and_author_id_raises(self) -> None:
        payload: dict[str, object] = {"channel_id": "123", "subcommand": "balance"}
        with pytest.raises(NonRetryableTransportError, match="platform_user_id"):
            async with _client(lambda r: httpx.Response(200)) as client:
                await loyalty(_envelope(payload), _config(), http_client=client)


class TestBalanceOther:
    """`!points <user>` -- `target` present in payload."""

    async def test_success_reply_uses_target_label(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"status": "success", "data": {"balance": 42, "currency_name": "points"}},
            )

        _result, sent = await _call(_base_payload(subcommand="balance", target="someone"), handler)
        assert sent["message"]["text"] == "someone has 42 points"

    async def test_uses_target_as_platform_user_id(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(
                200, json={"status": "success", "data": {"balance": 1, "currency_name": "points"}}
            )

        await _call(_base_payload(subcommand="balance", target="someone"), handler)
        assert "platform_user_id=someone" in captured["url"]


class TestTop:
    async def test_posts_limit_10(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {"entries": [], "currency_name": "points"},
                },
            )

        await _call(_base_payload(subcommand="top"), handler)
        assert "limit=10" in captured["url"]
        assert "community_id=42" in captured["url"]

    async def test_success_reply_numbered_with_display_name_fallback(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "entries": [
                            {"platform_user_id": "u1", "display_name": "Alice", "balance": 120},
                            {"platform_user_id": "u2", "balance": 90},
                        ],
                        "currency_name": "points",
                    },
                },
            )

        _result, sent = await _call(_base_payload(subcommand="top"), handler)
        assert sent["message"]["text"] == "top points: 1. Alice — 120, 2. u2 — 90"

    async def test_empty_leaderboard_reply(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"status": "success", "data": {"entries": [], "currency_name": "points"}},
            )

        _result, sent = await _call(_base_payload(subcommand="top"), handler)
        assert sent["message"]["text"] == f"top points: {_LEADERBOARD_EMPTY_SUFFIX}"


class TestShop:
    async def test_success_reply_renders_stock_and_unlimited(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "items": [
                            {
                                "sku": "hat",
                                "name": "Cool Hat",
                                "cost": 50,
                                "stock": 3,
                                "enabled": True,
                            },
                            {
                                "sku": "sticker",
                                "name": "Sticker",
                                "cost": 10,
                                "stock": None,
                                "enabled": True,
                            },
                            {
                                "sku": "hidden",
                                "name": "Hidden",
                                "cost": 5,
                                "stock": 1,
                                "enabled": False,
                            },
                        ]
                    },
                },
            )

        _result, sent = await _call(_base_payload(subcommand="shop"), handler)
        assert (
            sent["message"]["text"] == "shop: hat — Cool Hat (50) [x3 left], sticker — Sticker (10)"
        )

    async def test_empty_shop_reply(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "success", "data": {"items": []}})

        _result, sent = await _call(_base_payload(subcommand="shop"), handler)
        assert sent["message"]["text"] == _SHOP_EMPTY_REPLY


class TestRedeem:
    async def test_posts_correct_body(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "balance": 70,
                        "status": "fulfilled",
                        "item_name": "Cool Hat",
                        "cost": 50,
                    },
                },
            )

        await _call(_base_payload(subcommand="redeem", sku="hat"), handler)
        assert captured["body"] == {
            "community_id": 42,
            "platform": "twitch",
            "platform_user_id": "platform-user-1",
            "sku": "hat",
        }

    async def test_fulfilled_success_reply(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "balance": 70,
                        "status": "fulfilled",
                        "item_name": "Cool Hat",
                        "cost": 50,
                    },
                },
            )

        _result, sent = await _call(_base_payload(subcommand="redeem", sku="hat"), handler)
        assert sent["message"]["text"] == "redeemed Cool Hat for 50 — 70 left"

    async def test_pending_success_reply(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "balance": 70,
                        "status": "pending",
                        "item_name": "Signed Poster",
                        "cost": 500,
                    },
                },
            )

        _result, sent = await _call(_base_payload(subcommand="redeem", sku="poster"), handler)
        assert (
            sent["message"]["text"]
            == "redeemed Signed Poster for 500 — 70 left (awaiting mod approval)"
        )

    @pytest.mark.parametrize(
        "message",
        [
            "not enough points (have 10, need 50)",
            "item out of stock",
            "unknown item 'sku'",
            "loyalty is disabled here",
        ],
    )
    async def test_409_relays_message_verbatim(self, message: str) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(409, json={"status": "error", "error": {"message": message}})

        _result, sent = await _call(_base_payload(subcommand="redeem", sku="hat"), handler)
        assert sent["message"]["text"] == message

    async def test_missing_sku_raises(self) -> None:
        payload = _base_payload(subcommand="redeem")
        with pytest.raises(NonRetryableTransportError, match="sku"):
            async with _client(lambda r: httpx.Response(200)) as client:
                await loyalty(_envelope(payload), _config(), http_client=client)


class TestAdjust:
    async def test_posts_correct_body(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200, json={"status": "success", "data": {"balance": 150, "currency_name": "points"}}
            )

        await _call(_base_payload(subcommand="adjust", target="someone", delta=50), handler)
        assert captured["body"] == {
            "community_id": 42,
            "platform": "twitch",
            "platform_user_id": "someone",
            "delta": 50,
            "actor_platform_user_id": "platform-user-1",
            "note": None,
        }

    async def test_success_reply(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"status": "success", "data": {"balance": 150, "currency_name": "points"}}
            )

        _result, sent = await _call(
            _base_payload(subcommand="adjust", target="someone", delta=50), handler
        )
        assert sent["message"]["text"] == "someone now has 150 points"

    async def test_negative_delta_removal_success_reply(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"status": "success", "data": {"balance": 30, "currency_name": "points"}}
            )

        _result, sent = await _call(
            _base_payload(subcommand="adjust", target="someone", delta=-20), handler
        )
        assert sent["message"]["text"] == "someone now has 30 points"

    async def test_currency_name_defaults_to_points_when_absent(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "success", "data": {"balance": 150}})

        _result, sent = await _call(
            _base_payload(subcommand="adjust", target="someone", delta=50), handler
        )
        assert sent["message"]["text"] == "someone now has 150 points"

    async def test_missing_target_raises(self) -> None:
        payload = _base_payload(subcommand="adjust", delta=50)
        with pytest.raises(NonRetryableTransportError, match="target"):
            async with _client(lambda r: httpx.Response(200)) as client:
                await loyalty(_envelope(payload), _config(), http_client=client)

    async def test_missing_delta_raises(self) -> None:
        payload = _base_payload(subcommand="adjust", target="someone")
        with pytest.raises(NonRetryableTransportError, match="delta"):
            async with _client(lambda r: httpx.Response(200)) as client:
                await loyalty(_envelope(payload), _config(), http_client=client)


class TestGracefulDegradation:
    """5xx / timeout -> the generic `loyalty unavailable` reply, every subcommand."""

    async def test_5xx_reply(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        _result, sent = await _call(_base_payload(subcommand="balance"), handler)
        assert sent["message"]["text"] == "loyalty unavailable (hub-api error 500)"

    async def test_unreachable_reply(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        _result, sent = await _call(_base_payload(subcommand="top"), handler)
        assert sent["message"]["text"] == "loyalty unavailable (hub-api unreachable)"

    async def test_malformed_2xx_body_reply(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "success", "data": {}})

        _result, sent = await _call(_base_payload(subcommand="shop"), handler)
        assert sent["message"]["text"] == "loyalty unavailable (hub-api error 200)"

    async def test_redeem_5xx_reply(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        _result, sent = await _call(_base_payload(subcommand="redeem", sku="hat"), handler)
        assert sent["message"]["text"] == "loyalty unavailable (hub-api error 503)"


class TestUnknownSubcommand:
    async def test_raises(self) -> None:
        payload = _base_payload(subcommand="bogus")
        with pytest.raises(NonRetryableTransportError, match="bogus"):
            async with _client(lambda r: httpx.Response(200)) as client:
                await loyalty(_envelope(payload), _config(), http_client=client)


class TestCommunityValidation:
    async def test_missing_community_raises(self) -> None:
        payload = _base_payload(subcommand="balance")
        with pytest.raises(NonRetryableTransportError, match="community"):
            async with _client(lambda r: httpx.Response(200)) as client:
                await loyalty(_envelope(payload, community=None), _config(), http_client=client)

    async def test_non_integer_community_raises(self) -> None:
        payload = _base_payload(subcommand="balance")
        with pytest.raises(NonRetryableTransportError, match="not a valid integer"):
            async with _client(lambda r: httpx.Response(200)) as client:
                await loyalty(
                    _envelope(payload, community="not-a-number"), _config(), http_client=client
                )


class TestReplyDispatch:
    async def test_success_reply_sent_via_discord(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_DISCORD_TOKEN", "tok")
        discord_call: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if _BALANCE_URL_FRAGMENT in str(request.url):
                return httpx.Response(
                    200,
                    json={"status": "success", "data": {"balance": 5, "currency_name": "points"}},
                )
            discord_call["body"] = json.loads(request.content)
            discord_call["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"id": "999"})

        async with _client(handler) as client:
            result = await loyalty(
                _envelope(_base_payload(subcommand="balance"), platform="discord"),
                _config(),
                http_client=client,
            )

        assert discord_call["body"] == {"content": "you have 5 points"}
        assert discord_call["auth"] == "Bot tok"
        assert result.http_status == 200

    async def test_missing_channel_raises(self) -> None:
        payload: dict[str, object] = {"subcommand": "top", "author_id": "platform-user-1"}

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"status": "success", "data": {"entries": [], "currency_name": "points"}}
            )

        with pytest.raises(NonRetryableTransportError, match="channel"):
            async with _client(handler) as client:
                await loyalty(
                    _envelope(payload, platform="twitch"),
                    {"bot_token_ref": "x"},
                    http_client=client,
                )
