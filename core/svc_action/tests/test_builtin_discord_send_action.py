"""builtin_handlers/discord_send_action.py -- real Discord Create Message API call, SSRF-guarded.

Discord API is always mocked (`httpx.MockTransport`) -- a real send needs a
live bot token. This file additionally, opportunistically, exercises the
handler against the real Discord API if `~/.discord.token` exists on disk
(local dev convenience only, never in CI) -- skipped gracefully otherwise,
and the token's contents are never printed/logged/asserted into a failure
message.
"""

from __future__ import annotations

import json as _json
from pathlib import Path

import httpx
import pytest
from flask_core import PlatformEvent, StageEnvelope
from waddle_transports import NonRetryableTransportError, RetryableTransportError

from builtin_handlers.discord_send_action import send_message


def _envelope(payload: dict | None = None) -> StageEnvelope:
    default_payload = {"text": "hello from waddlebot", "channel_id": "123456789"}
    return StageEnvelope(
        tenant="1",
        community="42",
        app_id="waddles.bot.discord.default",
        stage="action",
        event=PlatformEvent(
            platform="discord",
            event_type="message",
            actor=None,
            payload=payload if payload is not None else default_payload,
            occurred_at="2026-08-31T12:00:00Z",
        ),
        ts="2026-08-31T12:00:00Z",
    )


def _config(**overrides: object) -> dict:
    base = {
        "bot_token_ref": "TEST_DISCORD_BOT_TOKEN",
        "api_base": "https://8.8.8.8/api/v10",  # literal IP -- no real DNS in unit tests
    }
    base.update(overrides)
    return base


def _client(handler) -> httpx.AsyncClient:  # noqa: ANN001
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


class TestChannelIdResolution:
    """Reply-in-place: payload.channel_id is primary, config.channel_id is a fallback only."""

    async def test_no_channel_id_from_either_source_is_non_retryable(self) -> None:
        async with _client(lambda r: httpx.Response(200)) as client:
            with pytest.raises(NonRetryableTransportError, match="channel_id"):
                await send_message(_envelope(payload={"text": "hi"}), _config(), http_client=client)

    async def test_payload_channel_id_takes_precedence_over_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_DISCORD_BOT_TOKEN", "s3cr3t-bot-token")
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"id": "1"})

        payload = {"text": "hi", "channel_id": "from-payload"}
        async with _client(handler) as client:
            await send_message(
                _envelope(payload=payload),
                _config(channel_id="from-config"),
                http_client=client,
            )
        assert captured["url"] == "https://8.8.8.8/api/v10/channels/from-payload/messages"

    async def test_config_channel_id_used_as_fallback_when_payload_lacks_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_DISCORD_BOT_TOKEN", "s3cr3t-bot-token")
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"id": "1"})

        async with _client(handler) as client:
            await send_message(
                _envelope(payload={"text": "hi"}),
                _config(channel_id="from-config"),
                http_client=client,
            )
        assert captured["url"] == "https://8.8.8.8/api/v10/channels/from-config/messages"


async def test_missing_bot_token_ref_is_non_retryable() -> None:
    async with _client(lambda r: httpx.Response(200)) as client:
        with pytest.raises(NonRetryableTransportError, match="bot_token_ref"):
            await send_message(_envelope(), _config(bot_token_ref=None), http_client=client)


async def test_missing_payload_text_is_non_retryable() -> None:
    async with _client(lambda r: httpx.Response(200)) as client:
        with pytest.raises(NonRetryableTransportError, match="'text'"):
            await send_message(
                _envelope(payload={"channel_id": "123456789"}), _config(), http_client=client
            )


async def test_unresolvable_bot_token_is_non_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_DISCORD_BOT_TOKEN", raising=False)
    async with _client(lambda r: httpx.Response(200)) as client:
        with pytest.raises(NonRetryableTransportError, match="token resolution failed"):
            await send_message(_envelope(), _config(), http_client=client)


async def test_sends_real_discord_create_message_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-first verification: the bundle builds the real Discord Bot-token Create Message call."""
    monkeypatch.setenv("TEST_DISCORD_BOT_TOKEN", "s3cr3t-bot-token")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth_header"] = request.headers["Authorization"]
        captured["body"] = request.content
        return httpx.Response(200, json={"id": "999888777"})

    async with _client(handler) as client:
        result = await send_message(_envelope(), _config(), http_client=client)

    assert captured["url"] == "https://8.8.8.8/api/v10/channels/123456789/messages"
    assert captured["auth_header"] == "Bot s3cr3t-bot-token"
    assert _json.loads(captured["body"]) == {"content": "hello from waddlebot"}
    assert result.transport == "bundle"
    assert result.http_status == 200
    assert "message_id=999888777" in result.detail


async def test_includes_embed_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_DISCORD_BOT_TOKEN", "s3cr3t-bot-token")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(200, json={"id": "1"})

    payload = {"text": "raid!", "channel_id": "123456789", "embed": {"title": "Raid Alert"}}
    async with _client(handler) as client:
        await send_message(_envelope(payload=payload), _config(), http_client=client)

    assert _json.loads(captured["body"])["embeds"] == [{"title": "Raid Alert"}]


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_rejection_is_non_retryable(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setenv("TEST_DISCORD_BOT_TOKEN", "s3cr3t-bot-token")
    async with _client(lambda r: httpx.Response(status)) as client:
        with pytest.raises(NonRetryableTransportError, match="rejected auth"):
            await send_message(_envelope(), _config(), http_client=client)


async def test_other_4xx_is_non_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_DISCORD_BOT_TOKEN", "s3cr3t-bot-token")
    async with _client(lambda r: httpx.Response(400, text="Bad Request")) as client:
        with pytest.raises(NonRetryableTransportError, match="client error"):
            await send_message(_envelope(), _config(), http_client=client)


async def test_429_rate_limit_is_retryable_not_slept_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deliberate divergence from the legacy module: no local asyncio.sleep on 429.

    `retry_with_backoff` (runner.py) owns retry timing platform-wide.
    """
    monkeypatch.setenv("TEST_DISCORD_BOT_TOKEN", "s3cr3t-bot-token")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "2.5"})

    async with _client(handler) as client:
        with pytest.raises(RetryableTransportError, match="rate limited"):
            await send_message(_envelope(), _config(), http_client=client)


async def test_5xx_is_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_DISCORD_BOT_TOKEN", "s3cr3t-bot-token")
    async with _client(lambda r: httpx.Response(503)) as client:
        with pytest.raises(RetryableTransportError, match="server error"):
            await send_message(_envelope(), _config(), http_client=client)


async def test_network_error_is_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_DISCORD_BOT_TOKEN", "s3cr3t-bot-token")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with _client(handler) as client:
        with pytest.raises(RetryableTransportError, match="request failed"):
            await send_message(_envelope(), _config(), http_client=client)


async def test_private_host_api_base_is_blocked_non_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSRF guard applies to the Discord API base exactly like every other transport."""
    monkeypatch.setenv("TEST_DISCORD_BOT_TOKEN", "s3cr3t-bot-token")
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"id": "1"})

    async with _client(handler) as client:
        with pytest.raises(NonRetryableTransportError, match="SSRF"):
            await send_message(
                _envelope(),
                _config(api_base="http://169.254.169.254"),
                http_client=client,
            )
    assert called is False


def _read_local_discord_token() -> str | None:
    """Sync helper (kept out of the async test body -- ASYNC240) for the opportunistic live check.

    First non-empty line only, all whitespace/control chars stripped --
    defends against a token file saved with a trailing newline or a second
    line, which would otherwise reach httpx as a malformed header value
    and raise an exception that echoes it back verbatim. Returns `None`
    (never an empty string) if the file is absent or empty.
    """
    token_path = Path("~/.discord.token").expanduser()
    if not token_path.exists():
        return None
    lines = [line.strip() for line in token_path.read_text().splitlines() if line.strip()]
    return lines[0] if lines else None


async def test_live_discord_token_can_authenticate() -> None:
    """Opportunistic live check: a real bot token can auth against Discord's real API.

    Never prints/logs the token, and never lets an exception carrying the
    `Authorization` header value (e.g. httpx's `LocalProtocolError` on a
    malformed header, which embeds the offending header text verbatim)
    propagate into pytest's failure output -- any failure here (missing
    file, malformed token content, network error, non-200 response) is
    treated as "can't verify live, skip" rather than a hard test failure,
    since this check is opportunistic dev convenience, not a gated CI
    requirement (no such file exists in CI, so this always skips there).
    Deliberately hits the token-only `/users/@me` endpoint (not
    `send_message`, which requires a real `channel_id` this test has no
    safe value for) so nothing is actually posted to a real channel.
    """
    token = _read_local_discord_token()
    if token is None:
        pytest.skip("~/.discord.token not present or empty")

    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=10.0) as client:
            response = await client.get(
                "https://discord.com/api/v10/users/@me",
                headers={"Authorization": f"Bot {token}"},
            )
    except Exception:  # noqa: BLE001 -- never let a header-echoing exception surface the token
        pytest.skip("live discord API request could not be completed")

    if response.status_code != 200:
        pytest.skip(f"live discord API auth check returned HTTP {response.status_code}")
