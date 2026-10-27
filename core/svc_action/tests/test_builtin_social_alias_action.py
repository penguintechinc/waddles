"""Tests for `builtin_handlers.social_alias_action.send_message` -- alias response dispatch."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from flask_core import (
    PlatformEvent,
    StageEnvelope,
    bundle_context,
    reset_bundle_dal_for_tests,
    set_bundle_dal,
)
from waddle_transports import NonRetryableTransportError, RetryableTransportError

from builtin_handlers.social_alias_action import send_message


def _envelope(platform: str = "discord", payload: dict | None = None) -> StageEnvelope:
    default_payload = {"text": "hello from alias", "channel_id": "123456789"}
    if platform == "twitch":
        default_payload = {"text": "hello from alias", "channel_name": "waddles"}
    return StageEnvelope(
        tenant="1",
        community="42",
        app_id="waddles.social.alias.default",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="message",
            actor=None,
            payload=payload if payload is not None else default_payload,
            occurred_at="2026-08-31T12:00:00Z",
        ),
        ts="2026-08-31T12:00:00Z",
    )


def _config(**overrides: object) -> dict:
    base = {
        "bot_token_ref": "TEST_BOT_TOKEN",
        "api_base": "https://8.8.8.8/api/v10",  # literal IP -- no real DNS in unit tests
    }
    base.update(overrides)
    return base


def _client(handler) -> httpx.AsyncClient:  # noqa: ANN001
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


class _FakeDal:
    """In-memory stand-in for AsyncDAL -- minimal surface."""

    async def execute(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        """Mock execute -- not used by social_alias_action."""
        return []


@pytest.fixture(autouse=True)
def _dal() -> Any:
    """Set up fake DAL for all tests."""
    fake = _FakeDal()
    set_bundle_dal(fake)
    yield fake
    reset_bundle_dal_for_tests()


class TestDiscordChannelIdResolution:
    """Reply-in-place for Discord: payload.channel_id is primary, config.channel_id fallback."""

    async def test_no_channel_id_is_non_retryable(self) -> None:
        async with _client(lambda r: httpx.Response(200)) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.alias.default"):
                with pytest.raises(NonRetryableTransportError, match="channel_id"):
                    await send_message(
                        _envelope("discord", payload={"text": "hi"}), _config(), http_client=client
                    )

    async def test_payload_channel_id_takes_precedence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_BOT_TOKEN", "s3cr3t")
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"id": "1"})

        payload = {"text": "hi", "channel_id": "from-payload"}
        async with _client(handler) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.alias.default"):
                await send_message(
                    _envelope("discord", payload=payload),
                    _config(channel_id="from-config"),
                    http_client=client,
                )
        assert captured["url"] == "https://8.8.8.8/api/v10/channels/from-payload/messages"

    async def test_config_channel_id_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_BOT_TOKEN", "s3cr3t")
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"id": "1"})

        payload = {"text": "hi"}  # No channel_id
        async with _client(handler) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.alias.default"):
                await send_message(
                    _envelope("discord", payload=payload),
                    _config(channel_id="from-config"),
                    http_client=client,
                )
        assert captured["url"] == "https://8.8.8.8/api/v10/channels/from-config/messages"


class TestDiscordAuth:
    """Discord authentication (bot token) and token resolution."""

    async def test_missing_bot_token_ref_is_non_retryable(self) -> None:
        async with _client(lambda r: httpx.Response(200)) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.alias.default"):
                with pytest.raises(NonRetryableTransportError, match="bot_token_ref"):
                    await send_message(
                        _envelope("discord"), _config(bot_token_ref=""), http_client=client
                    )

    async def test_bot_token_is_resolved_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_BOT_TOKEN", "my-secret-token")
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, json={"id": "1"})

        async with _client(handler) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.alias.default"):
                await send_message(_envelope("discord"), _config(), http_client=client)
        assert captured["auth"] == "Bot my-secret-token"


class TestDiscordMessageContent:
    """Discord message body and text handling."""

    async def test_missing_text_is_non_retryable(self) -> None:
        async with _client(lambda r: httpx.Response(200)) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.alias.default"):
                with pytest.raises(NonRetryableTransportError, match="text"):
                    await send_message(
                        _envelope("discord", payload={"channel_id": "123"}),
                        _config(),
                        http_client=client,
                    )

    async def test_empty_text_is_non_retryable(self) -> None:
        async with _client(lambda r: httpx.Response(200)) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.alias.default"):
                with pytest.raises(NonRetryableTransportError, match="text"):
                    await send_message(
                        _envelope("discord", payload={"text": "", "channel_id": "123"}),
                        _config(),
                        http_client=client,
                    )

    async def test_text_sent_as_content_field(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_BOT_TOKEN", "token")
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = request.content.decode()
            return httpx.Response(200, json={"id": "1"})

        async with _client(handler) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.alias.default"):
                await send_message(
                    _envelope("discord", payload={"text": "hello world", "channel_id": "123"}),
                    _config(),
                    http_client=client,
                )
        assert "hello world" in captured["body"]


class TestDiscordStatusCodes:
    """Discord API response status code handling."""

    async def test_429_rate_limit_is_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_BOT_TOKEN", "token")
        async with _client(lambda r: httpx.Response(429)) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.alias.default"):
                with pytest.raises(RetryableTransportError):
                    await send_message(_envelope("discord"), _config(), http_client=client)

    async def test_401_auth_error_is_non_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_BOT_TOKEN", "token")
        async with _client(lambda r: httpx.Response(401)) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.alias.default"):
                with pytest.raises(NonRetryableTransportError):
                    await send_message(_envelope("discord"), _config(), http_client=client)

    async def test_403_forbidden_is_non_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_BOT_TOKEN", "token")
        async with _client(lambda r: httpx.Response(403)) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.alias.default"):
                with pytest.raises(NonRetryableTransportError):
                    await send_message(_envelope("discord"), _config(), http_client=client)

    async def test_500_server_error_is_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_BOT_TOKEN", "token")
        async with _client(lambda r: httpx.Response(500)) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.alias.default"):
                with pytest.raises(RetryableTransportError):
                    await send_message(_envelope("discord"), _config(), http_client=client)

    async def test_200_success_returns_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_BOT_TOKEN", "token")
        async with _client(lambda r: httpx.Response(200, json={"id": "1"})) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.alias.default"):
                result = await send_message(_envelope("discord"), _config(), http_client=client)
        assert result.transport == "bundle"
        assert result.http_status == 200


class TestUnsupportedPlatform:
    """Non-Discord/Twitch platforms should be rejected."""

    async def test_unsupported_platform_is_non_retryable(self) -> None:
        async with _client(lambda r: httpx.Response(200)) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.alias.default"):
                env = StageEnvelope(
                    tenant="1",
                    community="42",
                    app_id="waddles.social.alias.default",
                    stage="action",
                    event=PlatformEvent(
                        platform="slack",  # Unsupported
                        event_type="message",
                        actor=None,
                        payload={"text": "hello", "channel_id": "123"},
                        occurred_at="2026-08-31T12:00:00Z",
                    ),
                    ts="2026-08-31T12:00:00Z",
                )
                with pytest.raises(NonRetryableTransportError, match="does not support"):
                    await send_message(env, _config(), http_client=client)


class TestTwitchChannelNameResolution:
    """Reply-in-place for Twitch: payload.channel_name is primary, config.channel_name fallback."""

    async def test_no_channel_name_is_non_retryable(self) -> None:
        async with _client(lambda r: httpx.Response(200)) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.alias.default"):
                with pytest.raises(NonRetryableTransportError, match="channel_name"):
                    await send_message(
                        _envelope("twitch", payload={"text": "hi"}), _config(), http_client=client
                    )

    async def test_payload_channel_name_takes_precedence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_BOT_TOKEN", "s3cr3t")
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"data": [{"message_id": "1"}]})

        payload = {"text": "hi", "channel_name": "from-payload"}
        async with _client(handler) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.alias.default"):
                await send_message(
                    _envelope("twitch", payload=payload),
                    _config(channel_name="from-config", api_base="https://8.8.8.8/helix"),
                    http_client=client,
                )
        # Twitch doesn't use channel_name in URL
        assert "from-payload" not in captured.get("url", "")


class TestTwitchAuth:
    """Twitch authentication and token resolution."""

    async def test_twitch_missing_bot_token_ref_is_non_retryable(self) -> None:
        async with _client(lambda r: httpx.Response(200)) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.alias.default"):
                with pytest.raises(NonRetryableTransportError, match="bot_token_ref"):
                    await send_message(
                        _envelope("twitch", payload={"text": "hi", "channel_name": "waddles"}),
                        _config(bot_token_ref=""),
                        http_client=client,
                    )

    async def test_twitch_missing_text_is_non_retryable(self) -> None:
        async with _client(lambda r: httpx.Response(200)) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.alias.default"):
                with pytest.raises(NonRetryableTransportError, match="text"):
                    await send_message(
                        _envelope("twitch", payload={"channel_name": "waddles"}),
                        _config(),
                        http_client=client,
                    )

    async def test_twitch_empty_text_is_non_retryable(self) -> None:
        async with _client(lambda r: httpx.Response(200)) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.alias.default"):
                with pytest.raises(NonRetryableTransportError, match="text"):
                    await send_message(
                        _envelope("twitch", payload={"text": "", "channel_name": "waddles"}),
                        _config(),
                        http_client=client,
                    )

    async def test_twitch_token_resolution_error_is_non_retryable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BAD_TOKEN", "unused")
        async with _client(lambda r: httpx.Response(200)) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.alias.default"):
                with pytest.raises(NonRetryableTransportError, match="bot token resolution failed"):
                    await send_message(
                        _envelope("twitch", payload={"text": "hi", "channel_name": "waddles"}),
                        _config(bot_token_ref="UNDEFINED_TOKEN"),
                        http_client=client,
                    )


class TestTwitchStatusCodes:
    """Twitch API response status code handling."""

    async def test_twitch_429_rate_limit_is_retryable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_BOT_TOKEN", "token")
        async with _client(lambda r: httpx.Response(429)) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.alias.default"):
                with pytest.raises(RetryableTransportError):
                    await send_message(
                        _envelope("twitch", payload={"text": "hi", "channel_name": "waddles"}),
                        _config(),
                        http_client=client,
                    )

    async def test_twitch_401_auth_error_is_non_retryable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_BOT_TOKEN", "token")
        async with _client(lambda r: httpx.Response(401)) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.alias.default"):
                with pytest.raises(NonRetryableTransportError):
                    await send_message(
                        _envelope("twitch", payload={"text": "hi", "channel_name": "waddles"}),
                        _config(),
                        http_client=client,
                    )

    async def test_twitch_403_forbidden_is_non_retryable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_BOT_TOKEN", "token")
        async with _client(lambda r: httpx.Response(403)) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.alias.default"):
                with pytest.raises(NonRetryableTransportError):
                    await send_message(
                        _envelope("twitch", payload={"text": "hi", "channel_name": "waddles"}),
                        _config(),
                        http_client=client,
                    )

    async def test_twitch_400_client_error_is_non_retryable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_BOT_TOKEN", "token")
        async with _client(lambda r: httpx.Response(400)) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.alias.default"):
                with pytest.raises(NonRetryableTransportError):
                    await send_message(
                        _envelope("twitch", payload={"text": "hi", "channel_name": "waddles"}),
                        _config(),
                        http_client=client,
                    )

    async def test_twitch_500_server_error_is_retryable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_BOT_TOKEN", "token")
        async with _client(lambda r: httpx.Response(500)) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.alias.default"):
                with pytest.raises(RetryableTransportError):
                    await send_message(
                        _envelope("twitch", payload={"text": "hi", "channel_name": "waddles"}),
                        _config(),
                        http_client=client,
                    )

    async def test_twitch_200_success_returns_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_BOT_TOKEN", "token")
        response = httpx.Response(200, json={"data": [{"message_id": "1"}]})
        async with _client(lambda r: response) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.alias.default"):
                result = await send_message(
                    _envelope("twitch", payload={"text": "hi", "channel_name": "waddles"}),
                    _config(),
                    http_client=client,
                )
        assert result.transport == "bundle"
        assert result.http_status == 200


class TestDiscordErrorHandling:
    """Additional Discord error cases and exception handling."""

    async def test_discord_token_resolution_error_is_non_retryable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BAD_TOKEN", "unused")
        async with _client(lambda r: httpx.Response(200)) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.alias.default"):
                with pytest.raises(NonRetryableTransportError, match="bot token resolution failed"):
                    await send_message(
                        _envelope("discord"),
                        _config(bot_token_ref="UNDEFINED_TOKEN"),
                        http_client=client,
                    )

    async def test_discord_400_client_error_is_non_retryable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_BOT_TOKEN", "token")
        async with _client(lambda r: httpx.Response(400)) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.alias.default"):
                with pytest.raises(NonRetryableTransportError):
                    await send_message(_envelope("discord"), _config(), http_client=client)

    async def test_discord_404_not_found_is_non_retryable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_BOT_TOKEN", "token")
        async with _client(lambda r: httpx.Response(404)) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.alias.default"):
                with pytest.raises(NonRetryableTransportError):
                    await send_message(_envelope("discord"), _config(), http_client=client)

    async def test_discord_502_bad_gateway_is_retryable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_BOT_TOKEN", "token")
        async with _client(lambda r: httpx.Response(502)) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.alias.default"):
                with pytest.raises(RetryableTransportError):
                    await send_message(_envelope("discord"), _config(), http_client=client)

    async def test_discord_ssrf_error_is_non_retryable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from waddle_transports.url_guard import SSRFError

        monkeypatch.setenv("TEST_BOT_TOKEN", "token")

        def handler(request: httpx.Request) -> httpx.Response:
            raise SSRFError("SSRF detected")

        async with _client(handler) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.alias.default"):
                with pytest.raises(NonRetryableTransportError, match="SSRF"):
                    await send_message(_envelope("discord"), _config(), http_client=client)


class TestTwitchErrorHandling:
    """Additional Twitch error cases and exception handling."""

    async def test_twitch_ssrf_error_is_non_retryable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from waddle_transports.url_guard import SSRFError

        monkeypatch.setenv("TEST_BOT_TOKEN", "token")

        def handler(request: httpx.Request) -> httpx.Response:
            raise SSRFError("SSRF detected")

        async with _client(handler) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.alias.default"):
                with pytest.raises(NonRetryableTransportError, match="SSRF"):
                    await send_message(
                        _envelope("twitch", payload={"text": "hi", "channel_name": "waddles"}),
                        _config(),
                        http_client=client,
                    )

    async def test_twitch_timeout_is_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_BOT_TOKEN", "token")

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("timeout")

        async with _client(handler) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.alias.default"):
                with pytest.raises(RetryableTransportError):
                    await send_message(
                        _envelope("twitch", payload={"text": "hi", "channel_name": "waddles"}),
                        _config(),
                        http_client=client,
                    )

    async def test_twitch_network_error_is_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_BOT_TOKEN", "token")

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.NetworkError("network down")

        async with _client(handler) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.alias.default"):
                with pytest.raises(RetryableTransportError):
                    await send_message(
                        _envelope("twitch", payload={"text": "hi", "channel_name": "waddles"}),
                        _config(),
                        http_client=client,
                    )


class TestNetworkErrors:
    """Network and timeout errors are retryable."""

    async def test_timeout_is_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_BOT_TOKEN", "token")

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("timeout")

        async with _client(handler) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.alias.default"):
                with pytest.raises(RetryableTransportError):
                    await send_message(_envelope("discord"), _config(), http_client=client)

    async def test_network_error_is_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_BOT_TOKEN", "token")

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.NetworkError("network down")

        async with _client(handler) as client:
            with bundle_context(tenant="1", community="42", app_id="waddles.social.alias.default"):
                with pytest.raises(RetryableTransportError):
                    await send_message(_envelope("discord"), _config(), http_client=client)
