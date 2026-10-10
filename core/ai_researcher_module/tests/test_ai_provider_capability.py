"""`AIProviderService` Ollama path: capability-aware text/JSON mode + fail-loud completions.

Offline (`httpx.MockTransport` injected as the service's own client) -- the live-endpoint twin is
`test_ai_researcher_ollama_realpath.py`. Pinned here:

* `OLLAMA_SUPPORTS_JSON` (env, default false) is the only thing that can put the model in JSON mode;
  a text-only model never receives `format`, however loudly the caller wants JSON.
* `temperature` lives under Ollama's `options` (a top-level key is silently ignored by Ollama).
* a reasoning model that burns its budget thinking -> empty `response` -> `EmptyCompletionError`,
  never a blank "success".
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from ai_fakes import FakeConfig
from flask_core.ai_guard import UNTRUSTED_DATA_NOTICE
from config import Config, _env_flag
from services.ai_provider import (
    AIProvider,
    AIProviderService,
    EmptyCompletionError,
    InvalidJSONError,
)


def _service(
    handler: Any, **overrides: Any
) -> tuple[AIProviderService, list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []

    def recording(request: httpx.Request) -> httpx.Response:
        if request.content:
            seen.append(json.loads(request.content))
        return handler(request)

    service = AIProviderService(FakeConfig(**overrides))  # type: ignore[arg-type]
    service._client = httpx.AsyncClient(transport=httpx.MockTransport(recording))
    return service, seen


def _ok(text: str = "hello", **extra: Any) -> Any:
    body = {"response": text, "done": True, "done_reason": "stop", "eval_count": 4}
    body.update(extra)
    return lambda request: httpx.Response(200, json=body)


class TestEnvFlag:
    @pytest.mark.parametrize(("raw", "expected"), [("true", True), (" On ", True), ("0", False)])
    def test_parses(self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool) -> None:
        monkeypatch.setenv("X_FLAG", raw)
        assert _env_flag("X_FLAG", not expected) is expected

    def test_unset_and_blank_use_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("X_FLAG", raising=False)
        assert _env_flag("X_FLAG", True) is True
        monkeypatch.setenv("X_FLAG", "  ")
        assert _env_flag("X_FLAG", False) is False

    def test_garbage_fails_loud(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("X_FLAG", "treu")
        with pytest.raises(ValueError, match="X_FLAG='treu' is not a boolean"):
            _env_flag("X_FLAG", False)

    def test_shipped_defaults_are_the_text_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Config, "AI_PROVIDER", "ollama")
        assert Config.OLLAMA_SUPPORTS_JSON is False
        assert Config.OLLAMA_DISABLE_THINKING is True
        provider_cfg = Config.get_provider_config()
        assert provider_cfg["supports_json"] is False
        assert provider_cfg["disable_thinking"] is True


class TestCapabilityFlags:
    def test_text_only_by_default(self) -> None:
        service, _ = _service(_ok())
        assert service.provider is AIProvider.OLLAMA
        assert service.supports_json is False
        assert service.disable_thinking is True

    def test_json_capable_when_configured(self) -> None:
        service, _ = _service(_ok(), OLLAMA_SUPPORTS_JSON=True)
        assert service.supports_json is True

    def test_missing_attrs_on_a_legacy_config_mean_text_only(self) -> None:
        class Legacy:
            AI_PROVIDER = "ollama"
            MAX_CONCURRENT_LLM_CALLS = 1
            OLLAMA_HOST = "h"
            OLLAMA_PORT = "1"
            OLLAMA_USE_TLS = False
            OLLAMA_TIMEOUT = 1

        service = AIProviderService(Legacy())  # type: ignore[arg-type]
        assert (service.supports_json, service.disable_thinking) == (False, True)

    def test_non_ollama_provider_is_never_json_capable(self) -> None:
        service = AIProviderService(
            FakeConfig(AI_PROVIDER="openai", OLLAMA_SUPPORTS_JSON=True)  # type: ignore[arg-type]
        )
        assert service.supports_json is False


class TestOllamaPayload:
    async def test_text_only_model_never_gets_format_even_when_json_wanted(self) -> None:
        service, seen = _service(_ok("plain"))

        response = await service.generate("hi", want_json=True, max_tokens=33, temperature=0.2)

        assert "format" not in seen[0]
        assert response.json_mode is False
        assert response.content == "plain"
        assert response.tokens_used == 4
        assert response.model == "text-model"
        assert response.processing_time_ms >= 0

    async def test_json_capable_model_gets_format_json_and_a_validated_reply(self) -> None:
        service, seen = _service(_ok('{"a": 1}'), OLLAMA_SUPPORTS_JSON=True)

        response = await service.generate("hi", want_json=True)

        assert seen[0]["format"] == "json"
        assert response.json_mode is True
        assert json.loads(response.content) == {"a": 1}

    async def test_json_capable_model_stays_text_unless_asked(self) -> None:
        service, seen = _service(_ok("plain"), OLLAMA_SUPPORTS_JSON=True)

        response = await service.generate("hi")

        assert "format" not in seen[0]
        assert response.json_mode is False

    async def test_json_mode_with_non_json_output_fails_loud(self) -> None:
        service, _ = _service(_ok('{"cut off": '), OLLAMA_SUPPORTS_JSON=True)

        with pytest.raises(InvalidJSONError, match="non-JSON"):
            await service.generate("hi", want_json=True)

    async def test_wire_shape_temperature_budget_system_prompt_and_thinking(self) -> None:
        service, seen = _service(_ok())

        await service.generate("the question", system_prompt="be brief", temperature=0.3,
                               max_tokens=77)

        body = seen[0]
        assert body["model"] == "text-model"
        # regression: sec-llm01-hardening -- a real system/user split (Ollama `system`), not
        # the instructions glued onto the prompt; the untrusted-data notice always rides along.
        assert body["prompt"] == "the question"
        assert body["system"].startswith("be brief\n\n")
        assert UNTRUSTED_DATA_NOTICE in body["system"]
        assert body["stream"] is False
        assert body["think"] is False
        assert body["options"] == {"temperature": 0.3, "num_predict": 77}
        assert "temperature" not in body  # regression: top-level temperature is ignored by Ollama

    async def test_thinking_can_be_left_on(self) -> None:
        service, seen = _service(_ok(), OLLAMA_DISABLE_THINKING=False)
        await service.generate("hi")
        assert "think" not in seen[0]


class TestFailLoud:
    async def test_reasoning_model_with_blank_response_raises(self) -> None:
        # regression: gemma4 spent the whole num_predict budget in `thinking`.
        service, _ = _service(_ok("", done_reason="length", thinking="Thinking Process: ..."))

        with pytest.raises(EmptyCompletionError) as exc_info:
            await service.generate("hi", max_tokens=16)

        message = str(exc_info.value)
        assert "done_reason='length'" in message
        assert "thinking_present=True" in message
        assert "Thinking Process" not in message

    @pytest.mark.parametrize("body", [{"done": True}, {"response": None}, {"response": " \n "}])
    async def test_missing_null_or_whitespace_response_raises(self, body: dict[str, Any]) -> None:
        service, _ = _service(lambda request: httpx.Response(200, json=body))
        with pytest.raises(EmptyCompletionError):
            await service.generate("hi")

    async def test_http_error_status_propagates(self) -> None:
        service, _ = _service(lambda request: httpx.Response(404, json={"error": "no model"}))
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await service.generate("hi")
        assert exc_info.value.response.status_code == 404

    async def test_timeout_propagates(self) -> None:
        def slow(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        service, _ = _service(slow)
        with pytest.raises(httpx.TimeoutException):
            await service.generate("hi")

    async def test_unexpected_error_propagates_after_logging(self) -> None:
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        service, _ = _service(boom)
        with pytest.raises(httpx.ConnectError):
            await service.generate("hi")

    async def test_unimplemented_providers_fail_loudly(self) -> None:
        service = AIProviderService(FakeConfig(AI_PROVIDER="anthropic"))  # type: ignore[arg-type]
        with pytest.raises(NotImplementedError, match="Anthropic provider not yet implemented"):
            await service.generate("hi")


class TestSingleConnection:
    async def test_semaphore_of_one_never_overlaps_calls(self) -> None:
        import asyncio

        active = 0
        peak = 0

        class Probe(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.02)
                active -= 1
                return httpx.Response(200, json={"response": "x", "eval_count": 1})

        service = AIProviderService(FakeConfig(MAX_CONCURRENT_LLM_CALLS=1))  # type: ignore[arg-type]
        service._client = httpx.AsyncClient(transport=Probe())

        await asyncio.gather(*(service.generate(f"q{i}") for i in range(4)))

        assert peak == 1
        await service.close()
        assert service._client is None
