"""builtin_handlers/moderation_enforce_action.py -- Discord/Twitch moderation ENFORCEMENT tests.

Discord/Twitch APIs are always mocked (`httpx.MockTransport`); Twitch's
warn path goes through a real `fakeredis.FakeAsyncRedis` (genuine LPUSH
semantics), matching `test_builtin_twitch_send_action.py`'s own
convention. `api_base` is always overridden to a literal IP (`8.8.8.8`)
-- no real DNS resolution in unit tests, matching `test_builtin_discord_
send_action.py`'s own convention (`waddle_transports.url_guard.
validate_url` resolves the host via `socket.getaddrinfo` before every
real request).
"""

from __future__ import annotations

import json as _json
import logging
from typing import Any

import fakeredis
import httpx
import pytest
from flask_core import PlatformEvent, StageEnvelope
from waddle_transports import NonRetryableTransportError, RetryableTransportError
from waddle_transports.transports.irc_relay import outbound_queue_key

import builtin_handlers.moderation_enforce_action as mod_bundle
from builtin_handlers.moderation_enforce_action import _extract_enforcement, enforce

DISCORD_TOKEN_REF = "TEST_MOD_DISCORD_BOT_TOKEN"
TWITCH_MODERATOR_TOKEN_REF = "TEST_MOD_TWITCH_MODERATOR_TOKEN"


def _discord_envelope(
    payload: dict[str, Any] | None = None, *, actor: str | None = "flagged-user"
) -> StageEnvelope:
    default_payload: dict[str, Any] = {
        "text": "a flagged message",
        "guild_id": "111222333",
        "channel_id": "444555666",
        "author_id": "777888999",
        "moderation_enforcement": {
            "category": "hate_speech",
            "score": 0.91,
            "timeout_s": 600,
            "warn_text": "please keep it civil",
        },
    }
    return StageEnvelope(
        tenant="1",
        community="42",
        app_id="waddles.bot.discord.default",
        stage="action",
        event=PlatformEvent(
            platform="discord",
            event_type="message",
            actor=actor,
            payload=payload if payload is not None else default_payload,
            occurred_at="2026-09-11T12:00:00Z",
        ),
        ts="2026-09-11T12:00:00Z",
    )


def _discord_config(**overrides: object) -> dict[str, Any]:
    base: dict[str, Any] = {
        "bot_token_ref": DISCORD_TOKEN_REF,
        "api_base": "https://8.8.8.8/api/v10",
    }
    base.update(overrides)
    return base


def _twitch_envelope(
    payload: dict[str, Any] | None = None, *, actor: str | None = "flagged-user"
) -> StageEnvelope:
    default_payload: dict[str, Any] = {
        "text": "a flagged chat message",
        "channel_name": "somechannel",
        "broadcaster_id": "123456789",
        "user_id": "555666777",
        "moderation_enforcement": {
            "category": "harassment",
            "score": 0.85,
            "timeout_s": 300,
            "warn_text": "please keep it civil",
        },
    }
    return StageEnvelope(
        tenant="1",
        community="42",
        app_id="waddles.bot.twitch.default",
        stage="action",
        event=PlatformEvent(
            platform="twitch",
            event_type="message",
            actor=actor,
            payload=payload if payload is not None else default_payload,
            occurred_at="2026-09-11T12:00:00Z",
        ),
        ts="2026-09-11T12:00:00Z",
    )


def _twitch_config(**overrides: object) -> dict[str, Any]:
    base: dict[str, Any] = {
        "client_id": "test-client-id",
        "moderator_token_ref": TWITCH_MODERATOR_TOKEN_REF,
        "moderator_id": "999888777",
        "api_base": "https://8.8.8.8/helix",
    }
    base.update(overrides)
    return base


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


@pytest.fixture
async def fake_redis() -> Any:
    client = fakeredis.FakeAsyncRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture(autouse=True)
def _patch_redis_client(monkeypatch: pytest.MonkeyPatch, fake_redis: Any) -> None:
    monkeypatch.setattr(mod_bundle, "_get_redis_client", lambda config: fake_redis)  # noqa: ARG005


class TestContract:
    """`envelope.event.payload["moderation_enforcement"]` shape parsing."""

    def test_full_shape_parses(self) -> None:
        parsed = _extract_enforcement(
            {
                "moderation_enforcement": {
                    "category": "hate_speech",
                    "score": 0.9,
                    "timeout_s": 120,
                    "warn_text": "stop that",
                    "action": "timeout",
                }
            }
        )
        assert parsed == {
            "category": "hate_speech",
            "score": 0.9,
            "timeout_s": 120,
            "warn_text": "stop that",
            "action": "timeout",
        }

    def test_minimal_shape_parses_with_defaults(self) -> None:
        parsed = _extract_enforcement(
            {"moderation_enforcement": {"category": "spam", "warn_text": "no spam"}}
        )
        assert parsed == {
            "category": "spam",
            "score": 0.0,
            "timeout_s": 0,
            "warn_text": "no spam",
            "action": None,
        }

    def test_missing_key_is_none(self) -> None:
        assert _extract_enforcement({}) is None

    def test_not_a_dict_is_none(self) -> None:
        assert _extract_enforcement({"moderation_enforcement": "nope"}) is None

    @pytest.mark.parametrize(
        "raw",
        [
            {"warn_text": "hi"},  # missing category
            {"category": "spam"},  # missing warn_text
            {"category": "", "warn_text": "hi"},  # empty category
            {"category": "spam", "warn_text": ""},  # empty warn_text
        ],
    )
    def test_missing_required_fields_is_none(self, raw: dict[str, Any]) -> None:
        assert _extract_enforcement({"moderation_enforcement": raw}) is None


class TestSkipPaths:
    """No-op paths -- always a successful `TransportResult`, never an HTTP call."""

    async def test_no_moderation_enforcement_in_payload_is_skipped(self) -> None:
        async with _client(lambda r: httpx.Response(200)) as client:
            result = await enforce(
                _discord_envelope(payload={"text": "clean message"}),
                _discord_config(),
                http_client=client,
            )
        assert "applied=False" in result.detail
        assert "action=none" in result.detail

    async def test_explicit_action_none_is_skipped(self) -> None:
        payload = {
            "guild_id": "1",
            "channel_id": "2",
            "author_id": "3",
            "moderation_enforcement": {
                "category": "hate_speech",
                "warn_text": "hi",
                "action": "none",
            },
        }
        called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200)

        async with _client(handler) as client:
            result = await enforce(
                _discord_envelope(payload=payload), _discord_config(), http_client=client
            )
        assert called is False
        assert "action=none" in result.detail

    async def test_unsupported_platform_is_non_retryable(self) -> None:
        envelope = StageEnvelope(
            tenant="1",
            community="42",
            app_id="waddles.bot.x.default",
            stage="action",
            event=PlatformEvent(
                platform="mastodon",
                event_type="message",
                actor=None,
                payload={
                    "moderation_enforcement": {"category": "spam", "warn_text": "hi"},
                    "author_id": "1",
                },
                occurred_at="2026-09-11T12:00:00Z",
            ),
            ts="2026-09-11T12:00:00Z",
        )
        async with _client(lambda r: httpx.Response(200)) as client:
            with pytest.raises(NonRetryableTransportError, match="does not support platform"):
                await enforce(envelope, _discord_config(), http_client=client)

    async def test_unresolvable_target_user_id_is_non_retryable(self) -> None:
        payload = {
            "guild_id": "1",
            "channel_id": "2",
            "moderation_enforcement": {"category": "spam", "warn_text": "hi"},
        }
        async with _client(lambda r: httpx.Response(200)) as client:
            with pytest.raises(NonRetryableTransportError, match="target user id"):
                await enforce(
                    _discord_envelope(payload=payload), _discord_config(), http_client=client
                )

    async def test_skip_self_never_enforces_on_bots_own_message(self) -> None:
        called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200)

        async with _client(handler) as client:
            result = await enforce(
                _discord_envelope(),
                _discord_config(bot_user_id="777888999"),  # matches default author_id
                http_client=client,
            )
        assert called is False
        assert "target is the bot's own message" in result.detail


class TestDiscordEnforcement:
    """Discord timeout (member PATCH) + warn (Create Message)."""

    async def test_warn_and_timeout_success(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv(DISCORD_TOKEN_REF, "s3cr3t-mod-bot-token")
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "PATCH":
                return httpx.Response(200, json={"id": "777888999"})
            return httpx.Response(200, json={"id": "1"})

        with caplog.at_level(logging.DEBUG):
            async with _client(handler) as client:
                result = await enforce(_discord_envelope(), _discord_config(), http_client=client)

        assert len(requests) == 2
        warn_req, timeout_req = requests[0], requests[1]

        assert warn_req.method == "POST"
        assert str(warn_req.url) == "https://8.8.8.8/api/v10/channels/444555666/messages"
        assert warn_req.headers["Authorization"] == "Bot s3cr3t-mod-bot-token"
        assert _json.loads(warn_req.content) == {"content": "please keep it civil"}

        assert timeout_req.method == "PATCH"
        assert str(timeout_req.url) == "https://8.8.8.8/api/v10/guilds/111222333/members/777888999"
        assert timeout_req.headers["Authorization"] == "Bot s3cr3t-mod-bot-token"
        assert timeout_req.headers["X-Audit-Log-Reason"] == "hate_speech"
        body = _json.loads(timeout_req.content)
        assert "communication_disabled_until" in body

        assert "applied=True" in result.detail
        assert "action=warn+timeout" in result.detail

        # No secret leaked into any log line.
        assert "s3cr3t-mod-bot-token" not in caplog.text

    async def test_warn_only_when_timeout_s_is_non_positive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(DISCORD_TOKEN_REF, "s3cr3t-mod-bot-token")
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"id": "1"})

        payload = {
            "guild_id": "111222333",
            "channel_id": "444555666",
            "author_id": "777888999",
            "moderation_enforcement": {
                "category": "spam",
                "timeout_s": 0,
                "warn_text": "please don't spam",
            },
        }
        async with _client(handler) as client:
            result = await enforce(
                _discord_envelope(payload=payload), _discord_config(), http_client=client
            )

        assert len(requests) == 1
        assert requests[0].method == "POST"
        assert "action=warn" in result.detail
        assert "timeout" not in result.detail.split("reason=")[0]

    async def test_duration_is_capped_at_discord_max(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(DISCORD_TOKEN_REF, "s3cr3t-mod-bot-token")
        from services.platform_moderation import DISCORD_MAX_TIMEOUT_SECONDS

        payload = {
            "guild_id": "111222333",
            "channel_id": "444555666",
            "author_id": "777888999",
            "moderation_enforcement": {
                "category": "hate_speech",
                "timeout_s": DISCORD_MAX_TIMEOUT_SECONDS * 10,
                "warn_text": "final warning",
            },
        }
        async with _client(lambda r: httpx.Response(200, json={"id": "1"})) as client:
            result = await enforce(
                _discord_envelope(payload=payload), _discord_config(), http_client=client
            )
        assert f"timed out {DISCORD_MAX_TIMEOUT_SECONDS}s" in result.detail

    @pytest.mark.parametrize(
        ("status", "match"),
        [
            (401, "didn't work \\(401\\)"),
            (403, "MODERATE_MEMBERS permission \\(403\\)"),
        ],
    )
    async def test_auth_failures_are_specific_non_retryable(
        self, monkeypatch: pytest.MonkeyPatch, status: int, match: str
    ) -> None:
        monkeypatch.setenv(DISCORD_TOKEN_REF, "s3cr3t-mod-bot-token")
        async with _client(lambda r: httpx.Response(status)) as client:
            with pytest.raises(NonRetryableTransportError, match=match):
                await enforce(_discord_envelope(), _discord_config(), http_client=client)

    async def test_5xx_is_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(DISCORD_TOKEN_REF, "s3cr3t-mod-bot-token")
        async with _client(lambda r: httpx.Response(503)) as client:
            with pytest.raises(RetryableTransportError, match="server error"):
                await enforce(_discord_envelope(), _discord_config(), http_client=client)

    async def test_429_retries_once_then_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(DISCORD_TOKEN_REF, "s3cr3t-mod-bot-token")
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            # Warn: 1st call rate-limited, 2nd succeeds. Timeout: succeeds immediately.
            if request.method == "POST" and call_count == 1:
                return httpx.Response(429, headers={"Retry-After": "0"})
            return httpx.Response(200, json={"id": "1"})

        async with _client(handler) as client:
            result = await enforce(_discord_envelope(), _discord_config(), http_client=client)

        assert call_count == 3  # warn (429) + warn retry (200) + timeout (200)
        assert "applied=True" in result.detail

    async def test_429_exhausted_after_one_retry_is_retryable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(DISCORD_TOKEN_REF, "s3cr3t-mod-bot-token")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "0"})

        async with _client(handler) as client:
            with pytest.raises(RetryableTransportError, match="retry exhausted"):
                await enforce(_discord_envelope(), _discord_config(), http_client=client)

    async def test_missing_bot_token_ref_is_non_retryable(self) -> None:
        async with _client(lambda r: httpx.Response(200)) as client:
            with pytest.raises(NonRetryableTransportError, match="bot_token_ref"):
                await enforce(
                    _discord_envelope(), _discord_config(bot_token_ref=None), http_client=client
                )

    async def test_missing_guild_id_is_non_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(DISCORD_TOKEN_REF, "s3cr3t-mod-bot-token")
        payload = {
            "channel_id": "444555666",
            "author_id": "777888999",
            "moderation_enforcement": {
                "category": "hate_speech",
                "timeout_s": 60,
                "warn_text": "hi",
            },
        }
        async with _client(lambda r: httpx.Response(200, json={"id": "1"})) as client:
            with pytest.raises(NonRetryableTransportError, match="guild_id"):
                await enforce(
                    _discord_envelope(payload=payload), _discord_config(), http_client=client
                )


class TestTwitchEnforcement:
    """Twitch warn (IRC relay) + timeout (Helix timed ban)."""

    async def test_warn_only_relays_via_irc(self, fake_redis: Any) -> None:
        payload = {
            "channel_name": "somechannel",
            "user_id": "555666777",
            "moderation_enforcement": {
                "category": "spam",
                "timeout_s": 0,
                "warn_text": "please don't spam",
            },
        }
        async with _client(lambda r: httpx.Response(200)) as client:
            result = await enforce(
                _twitch_envelope(payload=payload), _twitch_config(), http_client=client
            )

        assert "action=warn" in result.detail
        raw = await fake_redis.rpop(outbound_queue_key("twitch"))
        assert _json.loads(raw) == {"channel": "somechannel", "text": "please don't spam"}

    async def test_warn_and_timeout_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(TWITCH_MODERATOR_TOKEN_REF, "s3cr3t-moderator-token")
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["auth"] = request.headers["Authorization"]
            captured["client_id"] = request.headers["Client-Id"]
            captured["body"] = _json.loads(request.content)
            return httpx.Response(200, json={"data": [{"user_id": "555666777"}]})

        async with _client(handler) as client:
            result = await enforce(_twitch_envelope(), _twitch_config(), http_client=client)

        assert captured["url"].startswith("https://8.8.8.8/helix/moderation/bans?")
        assert "broadcaster_id=123456789" in captured["url"]
        assert "moderator_id=999888777" in captured["url"]
        assert captured["auth"] == "Bearer s3cr3t-moderator-token"
        assert captured["client_id"] == "test-client-id"
        assert captured["body"] == {
            "data": {"user_id": "555666777", "duration": 300, "reason": "harassment"}
        }
        assert "action=warn+timeout" in result.detail

    async def test_app_token_only_is_refused_without_attempting_a_call(self) -> None:
        called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200)

        config = _twitch_config()
        del config["moderator_token_ref"]

        async with _client(handler) as client:
            with pytest.raises(
                NonRetryableTransportError,
                match="requires a user token with moderator:manage:banned_users",
            ):
                await enforce(_twitch_envelope(), config, http_client=client)
        assert called is False

    async def test_duration_is_capped_at_twitch_max(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(TWITCH_MODERATOR_TOKEN_REF, "s3cr3t-moderator-token")
        from services.platform_moderation import TWITCH_MAX_TIMEOUT_SECONDS

        payload = {
            "channel_name": "somechannel",
            "broadcaster_id": "123456789",
            "user_id": "555666777",
            "moderation_enforcement": {
                "category": "harassment",
                "timeout_s": TWITCH_MAX_TIMEOUT_SECONDS * 10,
                "warn_text": "final warning",
            },
        }
        async with _client(lambda r: httpx.Response(200, json={"data": []})) as client:
            result = await enforce(
                _twitch_envelope(payload=payload), _twitch_config(), http_client=client
            )
        assert f"timed out {TWITCH_MAX_TIMEOUT_SECONDS}s" in result.detail

    async def test_missing_broadcaster_id_is_non_retryable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(TWITCH_MODERATOR_TOKEN_REF, "s3cr3t-moderator-token")
        payload = {
            "channel_name": "somechannel",
            "user_id": "555666777",
            "moderation_enforcement": {
                "category": "harassment",
                "timeout_s": 300,
                "warn_text": "hi",
            },
        }
        async with _client(lambda r: httpx.Response(200)) as client:
            with pytest.raises(NonRetryableTransportError, match="broadcaster_id"):
                await enforce(
                    _twitch_envelope(payload=payload), _twitch_config(), http_client=client
                )

    @pytest.mark.parametrize(
        ("status", "match"),
        [
            (401, "oauth token didn't work \\(401\\)"),
            (403, "moderator:manage:banned_users scope \\(403\\)"),
            (429, "rate limited"),
            (503, "server error"),
        ],
    )
    async def test_helix_failures_are_specific(
        self, monkeypatch: pytest.MonkeyPatch, status: int, match: str
    ) -> None:
        monkeypatch.setenv(TWITCH_MODERATOR_TOKEN_REF, "s3cr3t-moderator-token")
        expected_error = (
            RetryableTransportError if status in (429, 503) else NonRetryableTransportError
        )
        async with _client(lambda r: httpx.Response(status)) as client:
            with pytest.raises(expected_error, match=match):
                await enforce(_twitch_envelope(), _twitch_config(), http_client=client)


class TestTwitchCommunityModeratorToken:
    """gh-320: a per-community Twitch connection takes priority over `moderator_token_ref`."""

    async def test_community_token_used_and_config_ref_never_resolved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`moderator_token_ref` unset entirely -- community token alone must satisfy the call."""

        async def fake_resolve_community_moderator_token(community_id: int | None) -> str | None:
            assert community_id == 42
            return "community-moderator-token"  # noqa: S106 -- test fixture value

        monkeypatch.setattr(
            mod_bundle,
            "resolve_community_moderator_token",
            fake_resolve_community_moderator_token,
        )
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers["Authorization"]
            return httpx.Response(200, json={"data": [{"user_id": "555666777"}]})

        config = _twitch_config()
        del config["moderator_token_ref"]

        async with _client(handler) as client:
            result = await enforce(_twitch_envelope(), config, http_client=client)

        assert captured["auth"] == "Bearer community-moderator-token"
        assert "action=warn+timeout" in result.detail

    async def test_community_token_takes_priority_over_configured_ref(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(TWITCH_MODERATOR_TOKEN_REF, "s3cr3t-moderator-token")

        async def fake_resolve_community_moderator_token(community_id: int | None) -> str | None:
            return "community-moderator-token"  # noqa: S106 -- test fixture value

        monkeypatch.setattr(
            mod_bundle,
            "resolve_community_moderator_token",
            fake_resolve_community_moderator_token,
        )
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers["Authorization"]
            return httpx.Response(200, json={"data": [{"user_id": "555666777"}]})

        async with _client(handler) as client:
            await enforce(_twitch_envelope(), _twitch_config(), http_client=client)

        assert captured["auth"] == "Bearer community-moderator-token"

    async def test_no_community_connection_falls_back_to_configured_ref(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Resolver returns `None` -- falls back to `moderator_token_ref`, unchanged."""
        monkeypatch.setenv(TWITCH_MODERATOR_TOKEN_REF, "s3cr3t-moderator-token")

        async def fake_resolve_community_moderator_token(community_id: int | None) -> str | None:
            return None

        monkeypatch.setattr(
            mod_bundle,
            "resolve_community_moderator_token",
            fake_resolve_community_moderator_token,
        )
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers["Authorization"]
            return httpx.Response(200, json={"data": [{"user_id": "555666777"}]})

        async with _client(handler) as client:
            await enforce(_twitch_envelope(), _twitch_config(), http_client=client)

        assert captured["auth"] == "Bearer s3cr3t-moderator-token"

    async def test_no_community_connection_and_no_configured_ref_is_non_retryable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Neither source available -- same specific error as the pre-gh-320 refusal path."""

        async def fake_resolve_community_moderator_token(community_id: int | None) -> str | None:
            return None

        monkeypatch.setattr(
            mod_bundle,
            "resolve_community_moderator_token",
            fake_resolve_community_moderator_token,
        )
        config = _twitch_config()
        del config["moderator_token_ref"]

        async with _client(lambda r: httpx.Response(200)) as client:
            with pytest.raises(
                NonRetryableTransportError,
                match="requires a user token with moderator:manage:banned_users",
            ):
                await enforce(_twitch_envelope(), config, http_client=client)

    async def test_malformed_community_id_still_falls_back_to_configured_ref(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`envelope.community` not an integer -- `community_id` becomes `None`, never raises."""
        monkeypatch.setenv(TWITCH_MODERATOR_TOKEN_REF, "s3cr3t-moderator-token")

        async def fake_resolve_community_moderator_token(community_id: int | None) -> str | None:
            assert community_id is None
            return None

        monkeypatch.setattr(
            mod_bundle,
            "resolve_community_moderator_token",
            fake_resolve_community_moderator_token,
        )
        envelope = StageEnvelope(
            tenant="1",
            community="not-an-int",
            app_id="waddles.bot.twitch.default",
            stage="action",
            event=PlatformEvent(
                platform="twitch",
                event_type="message",
                actor="flagged-user",
                payload={
                    "channel_name": "somechannel",
                    "broadcaster_id": "123456789",
                    "user_id": "555666777",
                    "moderation_enforcement": {
                        "category": "harassment",
                        "timeout_s": 300,
                        "warn_text": "please keep it civil",
                    },
                },
                occurred_at="2026-09-11T12:00:00Z",
            ),
            ts="2026-09-11T12:00:00Z",
        )
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers["Authorization"]
            return httpx.Response(200, json={"data": [{"user_id": "555666777"}]})

        async with _client(handler) as client:
            await enforce(envelope, _twitch_config(), http_client=client)

        assert captured["auth"] == "Bearer s3cr3t-moderator-token"
