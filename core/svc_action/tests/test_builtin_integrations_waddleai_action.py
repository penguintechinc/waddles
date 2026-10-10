"""Tests for `builtin_handlers.integrations_waddleai_action.waddleai_completion`."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from flask_core import PlatformEvent, StageEnvelope, bundle_context
from waddle_transports import NonRetryableTransportError, RetryableTransportError

from builtin_handlers.integrations_waddleai_action import waddleai_completion


def _envelope(payload: dict | None = None, community: str = "42") -> StageEnvelope:
    """Build a test StageEnvelope with sensible defaults."""
    default_payload = {"text": "What is the meaning of life?", "channel_id": "123"}
    return StageEnvelope(
        tenant="global",
        community=community,
        app_id="waddles.integrations.waddleai.default",
        stage="action",
        event=PlatformEvent(
            platform="discord",
            event_type="message",
            actor="penguin",
            payload=payload if payload is not None else default_payload,
            occurred_at="2026-08-31T12:00:00Z",
        ),
        ts="2026-08-31T12:00:00Z",
    )


def _config(**overrides: object) -> dict:
    """Build a test config dict with defaults."""
    base = {
        "hub_api_base": "https://8.8.8.8/v1",
        "max_tokens": 512,
        "temperature": 0.7,
    }
    base.update(overrides)
    return base


def _client(handler: object) -> httpx.AsyncClient:  # noqa: ANN001
    """Build an httpx.AsyncClient wired to a mock transport handler."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


def _ctx(community: str | None = "42") -> Any:
    """`bundle_context()` for this bundle's `app_id` -- shared to keep call sites short."""
    return bundle_context(
        tenant="global", community=community, app_id="waddles.integrations.waddleai.default"
    )


class TestWaddleaiCompletion:
    """Tests for the waddleai completion bundle."""

    async def test_sends_completion_request_and_returns_response(self) -> None:
        """Test successful completion request."""
        captured_request = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured_request["url"] = str(request.url)
            captured_request["method"] = request.method
            captured_request["body"] = request.content.decode("utf-8")
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "text": "The answer is 42.",
                    "provider": "ollama",
                    "model": "llama3.1:1b",
                    "tier_used": "free",
                    "input_tokens": 5,
                    "output_tokens": 4,
                    "billed_tokens": 0,
                    "fallback_reason": None,
                },
            )

        async with _client(handler) as client:
            with _ctx():
                result = await waddleai_completion(_envelope(), _config(), http_client=client)

        assert result.transport == "bundle"
        assert "community=42" in result.detail
        assert captured_request["url"] == "https://8.8.8.8/v1/api/v1/community/42/ai/completions"
        assert captured_request["method"] == "POST"

    async def test_missing_text_is_non_retryable(self) -> None:
        """Test that missing prompt text raises NonRetryableTransportError."""
        async with _client(lambda r: httpx.Response(200)) as client:
            with _ctx():
                with pytest.raises(NonRetryableTransportError, match="text"):
                    await waddleai_completion(
                        _envelope(payload={"channel_id": "1"}), _config(), http_client=client
                    )

    async def test_empty_text_is_non_retryable(self) -> None:
        """Test that empty prompt text raises NonRetryableTransportError."""
        async with _client(lambda r: httpx.Response(200)) as client:
            with _ctx():
                with pytest.raises(NonRetryableTransportError, match="text"):
                    await waddleai_completion(
                        _envelope(payload={"text": "", "channel_id": "1"}),
                        _config(),
                        http_client=client,
                    )

    async def test_missing_community_is_non_retryable(self) -> None:
        """Test that tenant-wide activation (no community) fails."""
        async with _client(lambda r: httpx.Response(200)) as client:
            with _ctx(None):
                with pytest.raises(NonRetryableTransportError, match="community is None"):
                    await waddleai_completion(
                        _envelope(community=None), _config(), http_client=client
                    )

    async def test_invalid_community_id_is_non_retryable(self) -> None:
        """Test that non-integer community ID raises NonRetryableTransportError."""
        async with _client(lambda r: httpx.Response(200)) as client:
            with _ctx("not-a-number"):
                with pytest.raises(NonRetryableTransportError, match="not a valid integer"):
                    await waddleai_completion(
                        _envelope(community="not-a-number"), _config(), http_client=client
                    )

    async def test_missing_hub_api_base_is_non_retryable(self) -> None:
        """Test that missing hub_api_base config raises NonRetryableTransportError."""
        async with _client(lambda r: httpx.Response(200)) as client:
            with _ctx():
                with pytest.raises(NonRetryableTransportError, match="hub_api_base"):
                    await waddleai_completion(
                        _envelope(),
                        _config(hub_api_base=None),
                        http_client=client,
                    )

    async def test_ai_disabled_503_is_retryable(self) -> None:
        """Test that 503 response (AI disabled) is retryable. regression: gh-11,gh-17,gh-12."""
        async with _client(lambda r: httpx.Response(503)) as client:
            with _ctx():
                with pytest.raises(RetryableTransportError, match="503"):
                    await waddleai_completion(_envelope(), _config(), http_client=client)

    async def test_auth_failure_401_is_non_retryable(self) -> None:
        """Test that 401 auth failure is non-retryable."""
        async with _client(lambda r: httpx.Response(401)) as client:
            with _ctx():
                with pytest.raises(NonRetryableTransportError, match="401"):
                    await waddleai_completion(_envelope(), _config(), http_client=client)

    async def test_auth_failure_403_is_non_retryable(self) -> None:
        """Test that 403 forbidden is non-retryable."""
        async with _client(lambda r: httpx.Response(403)) as client:
            with _ctx():
                with pytest.raises(NonRetryableTransportError, match="403"):
                    await waddleai_completion(_envelope(), _config(), http_client=client)

    async def test_client_error_400_is_non_retryable(self) -> None:
        """Test that 400 client error is non-retryable."""
        async with _client(lambda r: httpx.Response(400, json={"error": "bad request"})) as client:
            with _ctx():
                with pytest.raises(NonRetryableTransportError, match="400"):
                    await waddleai_completion(_envelope(), _config(), http_client=client)

    async def test_server_error_500_is_retryable(self) -> None:
        """Test that 500 server error is retryable."""
        async with _client(lambda r: httpx.Response(500)) as client:
            with _ctx():
                with pytest.raises(RetryableTransportError, match="500"):
                    await waddleai_completion(_envelope(), _config(), http_client=client)

    async def test_server_error_502_is_retryable(self) -> None:
        """Test that 502 bad gateway is retryable."""
        async with _client(lambda r: httpx.Response(502)) as client:
            with _ctx():
                with pytest.raises(RetryableTransportError, match="502"):
                    await waddleai_completion(_envelope(), _config(), http_client=client)

    async def test_network_error_is_retryable(self) -> None:
        """Test that network/timeout errors are retryable."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.NetworkError("Connection failed")

        async with _client(handler) as client:
            with _ctx():
                with pytest.raises(RetryableTransportError, match="request failed"):
                    await waddleai_completion(_envelope(), _config(), http_client=client)

    async def test_timeout_error_is_retryable(self) -> None:
        """Test that timeout errors are retryable."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("Request timed out")

        async with _client(handler) as client:
            with _ctx():
                with pytest.raises(RetryableTransportError, match="request failed"):
                    await waddleai_completion(_envelope(), _config(), http_client=client)

    async def test_invalid_json_response_is_non_retryable(self) -> None:
        """Test that invalid JSON response is non-retryable."""
        async with _client(lambda r: httpx.Response(200, text="not json")) as client:
            with _ctx():
                with pytest.raises(NonRetryableTransportError, match="not valid JSON"):
                    await waddleai_completion(_envelope(), _config(), http_client=client)

    async def test_missing_text_field_in_response_is_non_retryable(self) -> None:
        """Test that response missing 'text' field is non-retryable."""
        async with _client(
            lambda r: httpx.Response(200, json={"provider": "ollama", "model": "llama"})
        ) as client:
            with _ctx():
                with pytest.raises(NonRetryableTransportError, match="text"):
                    await waddleai_completion(_envelope(), _config(), http_client=client)

    async def test_non_string_text_in_response_is_non_retryable(self) -> None:
        """Test that non-string 'text' field in response is non-retryable."""
        async with _client(lambda r: httpx.Response(200, json={"text": 123})) as client:
            with _ctx():
                with pytest.raises(NonRetryableTransportError, match="not a string"):
                    await waddleai_completion(_envelope(), _config(), http_client=client)

    async def test_custom_max_tokens_passed_to_api(self) -> None:
        """Test that custom max_tokens config is passed through."""
        captured_request = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json as json_module

            captured_request["body"] = json_module.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json={"text": "response", "output_tokens": 0})

        async with _client(handler) as client:
            with _ctx():
                await waddleai_completion(
                    _envelope(),
                    _config(max_tokens=256),
                    http_client=client,
                )

        assert captured_request["body"]["max_tokens"] == 256

    async def test_custom_temperature_passed_to_api(self) -> None:
        """Test that custom temperature config is passed through."""
        captured_request = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json as json_module

            captured_request["body"] = json_module.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json={"text": "response", "output_tokens": 0})

        async with _client(handler) as client:
            with _ctx():
                await waddleai_completion(
                    _envelope(),
                    _config(temperature=0.3),
                    http_client=client,
                )

        assert captured_request["body"]["temperature"] == 0.3

    async def test_requested_tier_passed_to_api(self) -> None:
        """Test that requested_tier config is passed through."""
        captured_request = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json as json_module

            captured_request["body"] = json_module.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json={"text": "response", "output_tokens": 0})

        async with _client(handler) as client:
            with _ctx():
                await waddleai_completion(
                    _envelope(),
                    _config(requested_tier="premium"),
                    http_client=client,
                )

        assert captured_request["body"]["requested_tier"] == "premium"

    async def test_none_values_filtered_from_request(self) -> None:
        """Test that None config values are filtered out."""
        captured_request = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json as json_module

            captured_request["body"] = json_module.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json={"text": "response", "output_tokens": 0})

        async with _client(handler) as client:
            with _ctx():
                await waddleai_completion(
                    _envelope(),
                    _config(requested_tier=None, model_hint=None),
                    http_client=client,
                )

        # These keys should not be present in the request body
        assert "requested_tier" not in captured_request["body"]
        assert "model_hint" not in captured_request["body"]

    async def test_text_is_stripped(self) -> None:
        """Test that prompt text is stripped of whitespace."""
        captured_request = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json as json_module

            captured_request["body"] = json_module.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json={"text": "response", "output_tokens": 0})

        async with _client(handler) as client:
            with _ctx():
                await waddleai_completion(
                    _envelope(payload={"text": "  hello world  ", "channel_id": "1"}),
                    _config(),
                    http_client=client,
                )

        assert captured_request["body"]["prompt"] == "hello world"

    async def test_response_detail_includes_token_count(self) -> None:
        """Test that response detail includes output token count."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "text": "response",
                    "output_tokens": 42,
                    "input_tokens": 10,
                    "provider": "ollama",
                    "model": "llama",
                    "tier_used": "free",
                    "billed_tokens": 0,
                },
            )

        async with _client(handler) as client:
            with _ctx():
                result = await waddleai_completion(_envelope(), _config(), http_client=client)

        assert "tokens=42" in result.detail

    async def test_preserves_custom_config_values(self) -> None:
        """Test that various custom config values are passed through correctly."""
        captured_request = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json as json_module

            captured_request["body"] = json_module.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json={"text": "response", "output_tokens": 0})

        async with _client(handler) as client:
            with _ctx():
                await waddleai_completion(
                    _envelope(),
                    _config(
                        max_tokens=1024,
                        temperature=0.5,
                        requested_tier="byok",
                        model_hint="claude-3-sonnet",
                        byok_provider="anthropic",
                        invocation="ambient",
                    ),
                    http_client=client,
                )

        body = captured_request["body"]
        assert body["max_tokens"] == 1024
        assert body["temperature"] == 0.5
        assert body["requested_tier"] == "byok"
        assert body["model_hint"] == "claude-3-sonnet"
        assert body["byok_provider"] == "anthropic"
        assert body["invocation"] == "ambient"
