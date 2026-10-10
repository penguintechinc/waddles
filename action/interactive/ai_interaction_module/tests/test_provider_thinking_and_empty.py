"""`OllamaProvider` / `WaddleAIProvider` / `AIService`: reasoning-model handling + fail-loud blank replies.

Offline (`httpx.MockTransport`); the live-endpoint twin is `test_ai_interaction_ollama_realpath.py`.

regression: a reasoning model (gemma4) spends the whole token budget in hidden "thinking" and returns
an EMPTY visible reply. Nothing said so: the provider returned "" and `AIService` quietly swapped in
a canned line. Now `think: false` is sent (Ollama), and a blank reply is logged with its
`done_reason`/`finish_reason` before the (deliberate, logged) canned fallback kicks in.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from config import _env_flag
from services.ai_service import AIService
from services.ollama_provider import OllamaProvider
from services.waddleai_provider import WaddleAIProvider


def _patch_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    transport = httpx.MockTransport(handler)
    original_init = httpx.AsyncClient.__init__

    def patched_init(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


def _chat_ok(text: str = "hello!", **message: Any) -> Any:
    body = {
        "message": {"role": "assistant", "content": text, **message},
        "done": True,
        "done_reason": "stop",
        "eval_count": 5,
    }
    return lambda request: httpx.Response(200, json=body)


class TestEnvFlag:
    @pytest.mark.parametrize(("raw", "expected"), [("TRUE", True), ("no", False)])
    def test_parses(self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool) -> None:
        monkeypatch.setenv("X_FLAG", raw)
        assert _env_flag("X_FLAG", not expected) is expected

    def test_unset_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("X_FLAG", raising=False)
        assert _env_flag("X_FLAG", True) is True

    def test_garbage_fails_loud(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("X_FLAG", "yep")
        with pytest.raises(ValueError, match="not a boolean"):
            _env_flag("X_FLAG", False)

    def test_shipped_default_disables_thinking(self) -> None:
        from config import Config

        assert Config.OLLAMA_DISABLE_THINKING is True


class TestOllamaProvider:
    async def test_thinking_is_disabled_by_default_and_no_format_is_ever_sent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return _chat_ok()(request)

        _patch_transport(monkeypatch, handler)
        provider = OllamaProvider()
        assert provider.disable_thinking is True

        result = await provider.generate_response("hi", "chatMessage", "u1", "twitch", {})

        assert result == "hello!"
        assert seen[0]["think"] is False
        assert "format" not in seen[0]  # chat replies are plain text on every model
        assert seen[0]["stream"] is False
        # regression: temperature must sit under `options` or Ollama ignores it
        assert seen[0]["options"] == {
            "temperature": provider.temperature,
            "num_predict": provider.max_tokens,
        }
        assert "temperature" not in seen[0]

    async def test_thinking_can_be_left_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return _chat_ok()(request)

        _patch_transport(monkeypatch, handler)
        await OllamaProvider(disable_thinking=False).generate_response(
            "hi", "chatMessage", "u1", "twitch", {}
        )
        assert "think" not in seen[0]

    async def test_blank_reply_is_logged_with_why_and_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        body = {
            "message": {"role": "assistant", "content": "", "thinking": "Thinking Process..."},
            "done_reason": "length",
        }
        _patch_transport(monkeypatch, lambda request: httpx.Response(200, json=body))

        with caplog.at_level(logging.ERROR):
            result = await OllamaProvider().generate_response("hi", "chatMessage", "u", "x", {})

        assert result is None
        text = caplog.text
        assert "empty completion" in text
        assert "done_reason='length'" in text
        assert "thinking_present=True" in text
        assert "Thinking Process" not in text  # reasoning text is never logged

    async def test_whitespace_only_reply_is_also_blank(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_transport(monkeypatch, _chat_ok("  \n "))
        assert await OllamaProvider().generate_response("hi", "chatMessage", "u", "x", {}) is None

    async def test_http_error_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_transport(monkeypatch, lambda request: httpx.Response(500, json={"error": "x"}))
        assert await OllamaProvider().generate_response("hi", "chatMessage", "u", "x", {}) is None

    async def test_timeout_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def slow(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        _patch_transport(monkeypatch, slow)
        assert await OllamaProvider().generate_response("hi", "chatMessage", "u", "x", {}) is None


class TestWaddleAIProvider:
    @staticmethod
    def _provider() -> WaddleAIProvider:
        return WaddleAIProvider(
            base_url="http://waddleai.test", api_key="synthetic", model="auto", preferred_model=""
        )

    async def test_reply_is_parsed_and_cleaned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = {
            "choices": [{"message": {"content": '**"hello"**'}, "finish_reason": "stop"}],
            "usage": {"waddleai_tokens": 3},
            "model": "m",
        }
        _patch_transport(monkeypatch, lambda request: httpx.Response(200, json=body))
        result = await self._provider().generate_response("hi", "chatMessage", "u", "x", {})
        assert result == "hello"

    @pytest.mark.parametrize("content", ["", None, "   "])
    async def test_blank_reply_is_logged_with_finish_reason_and_returns_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        content: str | None,
    ) -> None:
        body = {"choices": [{"message": {"content": content}, "finish_reason": "length"}]}
        _patch_transport(monkeypatch, lambda request: httpx.Response(200, json=body))

        with caplog.at_level(logging.ERROR):
            result = await self._provider().generate_response("hi", "chatMessage", "u", "x", {})

        assert result is None
        assert "empty completion" in caplog.text
        assert "finish_reason='length'" in caplog.text

    @pytest.mark.parametrize("status", [401, 429, 500])
    async def test_error_statuses_return_none(
        self, monkeypatch: pytest.MonkeyPatch, status: int
    ) -> None:
        _patch_transport(monkeypatch, lambda request: httpx.Response(status, text="no"))
        assert await self._provider().generate_response("hi", "chatMessage", "u", "x", {}) is None


class TestAIServiceConsumesProviderFailures:
    async def test_none_from_the_provider_becomes_the_logged_canned_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_transport(monkeypatch, _chat_ok(""))
        service = AIService(provider=OllamaProvider())

        reply = await service.generate_response("hi", "chatMessage", "u", "twitch", {})

        assert reply == service._get_fallback_response("chatMessage", {})
        assert reply  # a real, non-empty line the chat can show

    async def test_a_good_reply_is_passed_through_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_transport(monkeypatch, _chat_ok("Welcome aboard!"))
        service = AIService(provider=OllamaProvider())
        assert await service.generate_response("hi", "chatMessage", "u", "x", {}) == "Welcome aboard!"
