"""builtin_handlers/slack_send_action.py -- real Slack `chat.postMessage` API call, SSRF-guarded.

Slack API is always mocked (`httpx.MockTransport`) -- a real send needs a
live bot token. Mirrors `test_builtin_discord_send_action.py`'s structure
and conventions (channel_id resolution precedence, SSRF guard, retryable
vs non-retryable classification) plus Slack-specific behavior: thread
replies via `thread_ts`, and Slack's HTTP-200-with-`{"ok": false}` error
body instead of Discord's HTTP-status-only error signaling.
"""

from __future__ import annotations

import json as _json

import httpx
import pytest
from flask_core import PlatformEvent, StageEnvelope
from waddle_transports import NonRetryableTransportError, RetryableTransportError

from builtin_handlers.slack_send_action import send_message


def _envelope(payload: dict | None = None) -> StageEnvelope:
    default_payload = {"text": "hello from waddlebot", "channel_id": "C123456"}
    return StageEnvelope(
        tenant="1",
        community="42",
        app_id="waddles.bot.slack.default",
        stage="action",
        event=PlatformEvent(
            platform="slack",
            event_type="message",
            actor=None,
            payload=payload if payload is not None else default_payload,
            occurred_at="2026-08-31T12:00:00Z",
        ),
        ts="2026-08-31T12:00:00Z",
    )


def _config(**overrides: object) -> dict:
    base = {
        "bot_token_ref": "TEST_SLACK_BOT_TOKEN",
        "api_base": "https://8.8.8.8/api",  # literal IP -- no real DNS in unit tests
    }
    base.update(overrides)
    return base


def _client(handler) -> httpx.AsyncClient:  # noqa: ANN001
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


class TestChannelIdResolution:
    """Reply-in-place: payload.channel_id is primary, config.channel_id is a fallback only."""

    async def test_no_channel_id_from_either_source_is_non_retryable(self) -> None:
        async with _client(lambda r: httpx.Response(200, json={"ok": True})) as client:
            with pytest.raises(NonRetryableTransportError, match="channel_id"):
                await send_message(_envelope(payload={"text": "hi"}), _config(), http_client=client)

    async def test_payload_channel_id_takes_precedence_over_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_SLACK_BOT_TOKEN", "xoxb-s3cr3t")
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = request.content
            return httpx.Response(200, json={"ok": True, "ts": "1.1"})

        payload = {"text": "hi", "channel_id": "from-payload"}
        async with _client(handler) as client:
            await send_message(
                _envelope(payload=payload),
                _config(channel_id="from-config"),
                http_client=client,
            )
        assert _json.loads(captured["body"])["channel"] == "from-payload"

    async def test_config_channel_id_used_as_fallback_when_payload_lacks_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_SLACK_BOT_TOKEN", "xoxb-s3cr3t")
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = request.content
            return httpx.Response(200, json={"ok": True, "ts": "1.1"})

        async with _client(handler) as client:
            await send_message(
                _envelope(payload={"text": "hi"}),
                _config(channel_id="from-config"),
                http_client=client,
            )
        assert _json.loads(captured["body"])["channel"] == "from-config"


async def test_missing_bot_token_ref_is_non_retryable() -> None:
    async with _client(lambda r: httpx.Response(200, json={"ok": True})) as client:
        with pytest.raises(NonRetryableTransportError, match="bot_token_ref"):
            await send_message(_envelope(), _config(bot_token_ref=None), http_client=client)


async def test_missing_payload_text_is_non_retryable() -> None:
    async with _client(lambda r: httpx.Response(200, json={"ok": True})) as client:
        with pytest.raises(NonRetryableTransportError, match="'text'"):
            await send_message(
                _envelope(payload={"channel_id": "C123456"}), _config(), http_client=client
            )


async def test_unresolvable_bot_token_is_non_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_SLACK_BOT_TOKEN", raising=False)
    async with _client(lambda r: httpx.Response(200, json={"ok": True})) as client:
        with pytest.raises(NonRetryableTransportError, match="token resolution failed"):
            await send_message(_envelope(), _config(), http_client=client)


async def test_sends_real_slack_chat_post_message_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-first verification: the bundle builds the real Slack `chat.postMessage` call."""
    monkeypatch.setenv("TEST_SLACK_BOT_TOKEN", "xoxb-s3cr3t")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth_header"] = request.headers["Authorization"]
        captured["body"] = request.content
        return httpx.Response(200, json={"ok": True, "ts": "1234567890.000100"})

    async with _client(handler) as client:
        result = await send_message(_envelope(), _config(), http_client=client)

    assert captured["url"] == "https://8.8.8.8/api/chat.postMessage"
    assert captured["auth_header"] == "Bearer xoxb-s3cr3t"
    body = _json.loads(captured["body"])
    assert body["channel"] == "C123456"
    assert body["text"] == "hello from waddlebot"
    assert body["unfurl_links"] is False
    assert "thread_ts" not in body
    assert result.transport == "bundle"
    assert result.http_status == 200
    assert "ts=1234567890.000100" in result.detail


async def test_thread_reply_includes_thread_ts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_SLACK_BOT_TOKEN", "xoxb-s3cr3t")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(200, json={"ok": True, "ts": "2.2"})

    payload = {"text": "replying", "channel_id": "C123456", "thread_ts": "1111.2222"}
    async with _client(handler) as client:
        await send_message(_envelope(payload=payload), _config(), http_client=client)

    assert _json.loads(captured["body"])["thread_ts"] == "1111.2222"


async def test_no_thread_ts_when_absent_from_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_SLACK_BOT_TOKEN", "xoxb-s3cr3t")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(200, json={"ok": True, "ts": "2.2"})

    async with _client(handler) as client:
        await send_message(_envelope(), _config(), http_client=client)

    assert "thread_ts" not in _json.loads(captured["body"])


@pytest.mark.parametrize(
    ("error_code", "match"),
    [
        ("invalid_auth", "bot token didn't work"),
        ("not_authed", "bot token didn't work"),
        ("token_revoked", "bot token didn't work"),
    ],
)
async def test_ok_false_auth_errors_are_non_retryable(
    monkeypatch: pytest.MonkeyPatch, error_code: str, match: str
) -> None:
    monkeypatch.setenv("TEST_SLACK_BOT_TOKEN", "xoxb-s3cr3t")
    body = {"ok": False, "error": error_code}
    async with _client(lambda r: httpx.Response(200, json=body)) as client:
        with pytest.raises(NonRetryableTransportError, match=match):
            await send_message(_envelope(), _config(), http_client=client)


@pytest.mark.parametrize("error_code", ["channel_not_found", "not_in_channel"])
async def test_ok_false_channel_errors_are_non_retryable(
    monkeypatch: pytest.MonkeyPatch, error_code: str
) -> None:
    monkeypatch.setenv("TEST_SLACK_BOT_TOKEN", "xoxb-s3cr3t")
    body = {"ok": False, "error": error_code}
    async with _client(lambda r: httpx.Response(200, json=body)) as client:
        with pytest.raises(NonRetryableTransportError, match="isn't in that Slack channel"):
            await send_message(_envelope(), _config(), http_client=client)


async def test_ok_false_other_error_is_non_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_SLACK_BOT_TOKEN", "xoxb-s3cr3t")
    async with _client(
        lambda r: httpx.Response(200, json={"ok": False, "error": "msg_too_long"})
    ) as client:
        with pytest.raises(NonRetryableTransportError, match="slack API error: msg_too_long"):
            await send_message(_envelope(), _config(), http_client=client)


@pytest.mark.parametrize("status", [401, 403])
async def test_http_auth_rejection_is_non_retryable(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setenv("TEST_SLACK_BOT_TOKEN", "xoxb-s3cr3t")
    async with _client(lambda r: httpx.Response(status)) as client:
        with pytest.raises(NonRetryableTransportError, match="rejected auth"):
            await send_message(_envelope(), _config(), http_client=client)


async def test_other_4xx_is_non_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_SLACK_BOT_TOKEN", "xoxb-s3cr3t")
    async with _client(lambda r: httpx.Response(400, text="Bad Request")) as client:
        with pytest.raises(NonRetryableTransportError, match="client error"):
            await send_message(_envelope(), _config(), http_client=client)


async def test_429_retried_once_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_SLACK_BOT_TOKEN", "xoxb-s3cr3t")
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"ok": True, "ts": "3.3"})

    async with _client(handler) as client:
        result = await send_message(_envelope(), _config(), http_client=client)

    assert calls["count"] == 2
    assert result.http_status == 200


async def test_429_retried_once_then_still_rate_limited_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deliberate divergence from the legacy module: exactly one local retry, no sleep loop.

    `retry_with_backoff` (runner.py) owns retry timing platform-wide beyond
    this bundle's single immediate re-attempt.
    """
    monkeypatch.setenv("TEST_SLACK_BOT_TOKEN", "xoxb-s3cr3t")
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(429, headers={"Retry-After": "2.5"})

    async with _client(handler) as client:
        with pytest.raises(RetryableTransportError, match="rate limited"):
            await send_message(_envelope(), _config(), http_client=client)

    assert calls["count"] == 2


async def test_5xx_is_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_SLACK_BOT_TOKEN", "xoxb-s3cr3t")
    async with _client(lambda r: httpx.Response(503)) as client:
        with pytest.raises(RetryableTransportError, match="server error"):
            await send_message(_envelope(), _config(), http_client=client)


async def test_network_error_is_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_SLACK_BOT_TOKEN", "xoxb-s3cr3t")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with _client(handler) as client:
        with pytest.raises(RetryableTransportError, match="request failed"):
            await send_message(_envelope(), _config(), http_client=client)


async def test_private_host_api_base_is_blocked_non_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSRF guard applies to the Slack API base exactly like every other transport."""
    monkeypatch.setenv("TEST_SLACK_BOT_TOKEN", "xoxb-s3cr3t")
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"ok": True, "ts": "1.1"})

    async with _client(handler) as client:
        with pytest.raises(NonRetryableTransportError, match="SSRF"):
            await send_message(
                _envelope(),
                _config(api_base="http://169.254.169.254"),
                http_client=client,
            )
    assert called is False


async def test_bot_token_never_appears_in_error_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rejection errors name `bot_token_ref` (an env-var name), never the resolved token value."""
    monkeypatch.setenv("TEST_SLACK_BOT_TOKEN", "xoxb-s3cr3t-value-must-not-leak")
    async with _client(lambda r: httpx.Response(401)) as client:
        with pytest.raises(NonRetryableTransportError) as exc_info:
            await send_message(_envelope(), _config(), http_client=client)
    assert "xoxb-s3cr3t-value-must-not-leak" not in str(exc_info.value)


async def test_unparsable_response_body_is_non_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_SLACK_BOT_TOKEN", "xoxb-s3cr3t")
    async with _client(lambda r: httpx.Response(200, content=b"not json")) as client:
        with pytest.raises(NonRetryableTransportError, match="unparsable"):
            await send_message(_envelope(), _config(), http_client=client)
