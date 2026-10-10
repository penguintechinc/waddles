"""Capability-aware output mode (text-only vs JSON) for the AI router + Ollama client.

Offline (`httpx.MockTransport`) -- always runs in CI. The real-endpoint twin is
`test_ai_routing_ollama_realpath.py`. What is pinned here:

* `supports_json` is per-tier CONFIG (env), default text-only -- no tier->model map.
* `format: json` reaches the wire only for a JSON-capable model; a text-only model
  always gets a plain-text request, and `AIResponse.json_mode` tells the caller which.
* the fallback ladder lands on the free tier's capability, not the requested tier's.
* an empty / non-JSON / malformed provider body is a LOUD `provider_error()`, never a
  silent blank success (regression: gemma4 reasoning model + small `num_predict`
  returned `response == ""` with the answer stuck in `thinking`).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from services.ai_routing import router
from services.ai_routing.clients import (
    AnthropicClient,
    OllamaClient,
    OllamaConfig,
    OpenAIClient,
    _env_flag,
    free_ollama_config,
    premium_ollama_config,
)
from services.ai_routing.errors import ApiError
from services.ai_routing.models import AIRequest
from tests.ai_routing_helpers import (
    credit_premium,
    ollama_body,
    patch_feature_flags,
    patch_transport,
    seed_community,
    set_enterprise,
)

_CONFIG_ENV = (
    "OLLAMA_URL",
    "OLLAMA_PREMIUM_URL",
    "AI_FREE_MODEL",
    "AI_PREMIUM_MODEL",
    "AI_FREE_SUPPORTS_JSON",
    "AI_PREMIUM_SUPPORTS_JSON",
    "AI_FREE_DISABLE_THINKING",
    "AI_PREMIUM_DISABLE_THINKING",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _CONFIG_ENV:
        monkeypatch.delenv(name, raising=False)


def _recording_handler(seen: list[dict[str, Any]], body: dict[str, Any]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=body)

    return handler


class TestEnvFlag:
    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", " yes ", "on"])
    def test_truthy(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv("X_FLAG", raw)
        assert _env_flag("X_FLAG", default=False) is True

    @pytest.mark.parametrize("raw", ["0", "false", "No", "off"])
    def test_falsy(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv("X_FLAG", raw)
        assert _env_flag("X_FLAG", default=True) is False

    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_unset_or_blank_uses_default(
        self, monkeypatch: pytest.MonkeyPatch, raw: str | None
    ) -> None:
        if raw is None:
            monkeypatch.delenv("X_FLAG", raising=False)
        else:
            monkeypatch.setenv("X_FLAG", raw)
        assert _env_flag("X_FLAG", default=True) is True
        assert _env_flag("X_FLAG", default=False) is False

    def test_garbage_fails_loud(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("X_FLAG", "tru")
        with pytest.raises(ValueError, match="X_FLAG='tru' is not a boolean"):
            _env_flag("X_FLAG", default=False)


class TestTierConfig:
    def test_free_defaults_to_text_only_path(self) -> None:
        cfg = free_ollama_config()
        assert cfg.supports_json is False
        assert cfg.disable_thinking is True

    def test_premium_defaults_to_text_only_path(self) -> None:
        cfg = premium_ollama_config()
        assert cfg.supports_json is False
        assert cfg.disable_thinking is True

    def test_each_tier_is_configured_independently(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AI_FREE_MODEL", "small-text-model")
        monkeypatch.setenv("AI_PREMIUM_MODEL", "big-json-model")
        monkeypatch.setenv("AI_PREMIUM_SUPPORTS_JSON", "true")
        monkeypatch.setenv("AI_FREE_DISABLE_THINKING", "false")
        free, premium = free_ollama_config(), premium_ollama_config()
        assert (free.model, free.supports_json, free.disable_thinking) == (
            "small-text-model",
            False,
            False,
        )
        assert (premium.model, premium.supports_json, premium.disable_thinking) == (
            "big-json-model",
            True,
            True,
        )

    def test_bad_flag_value_fails_loud(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AI_FREE_SUPPORTS_JSON", "maybe")
        with pytest.raises(ValueError, match="AI_FREE_SUPPORTS_JSON"):
            free_ollama_config()


class TestOllamaClientOutputMode:
    async def test_text_only_model_gets_no_format_even_when_json_requested(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[dict[str, Any]] = []
        patch_transport(monkeypatch, _recording_handler(seen, ollama_body("plain words")))
        client = OllamaClient(OllamaConfig(base_url="http://ollama.test", model="text-model"))

        response = await client.generate(AIRequest(prompt="hi", wants_json=True), tier="free")

        assert "format" not in seen[0]
        assert response.json_mode is False
        assert response.text == "plain words"

    async def test_json_capable_model_gets_format_json_and_validated_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[dict[str, Any]] = []
        patch_transport(monkeypatch, _recording_handler(seen, ollama_body('{"ok": true}')))
        client = OllamaClient(
            OllamaConfig(base_url="http://ollama.test", model="json-model", supports_json=True)
        )

        response = await client.generate(AIRequest(prompt="hi", wants_json=True), tier="premium")

        assert seen[0]["format"] == "json"
        assert response.json_mode is True
        assert json.loads(response.text) == {"ok": True}

    async def test_json_capable_model_stays_text_when_json_not_requested(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[dict[str, Any]] = []
        patch_transport(monkeypatch, _recording_handler(seen, ollama_body("hello")))
        client = OllamaClient(
            OllamaConfig(base_url="http://ollama.test", model="json-model", supports_json=True)
        )

        response = await client.generate(AIRequest(prompt="hi"), tier="free")

        assert "format" not in seen[0]
        assert response.json_mode is False

    async def test_invalid_json_in_json_mode_fails_loud(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        patch_transport(
            monkeypatch, _recording_handler([], ollama_body('{"truncated": ', done_reason="length"))
        )
        client = OllamaClient(
            OllamaConfig(base_url="http://ollama.test", model="json-model", supports_json=True)
        )

        with pytest.raises(ApiError) as exc_info:
            await client.generate(AIRequest(prompt="hi", wants_json=True), tier="premium")

        assert exc_info.value.status_code == 502
        assert "invalid JSON" in exc_info.value.message
        assert "length" in exc_info.value.message

    @pytest.mark.parametrize(("disable", "expected"), [(True, False), (False, None)])
    async def test_thinking_is_disabled_by_default_and_opt_out(
        self, monkeypatch: pytest.MonkeyPatch, disable: bool, expected: bool | None
    ) -> None:
        seen: list[dict[str, Any]] = []
        patch_transport(monkeypatch, _recording_handler(seen, ollama_body("hello")))
        client = OllamaClient(
            OllamaConfig(base_url="http://ollama.test", model="m", disable_thinking=disable)
        )

        await client.generate(AIRequest(prompt="hi"), tier="free")

        assert seen[0].get("think") is expected

    async def test_options_carry_temperature_and_token_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[dict[str, Any]] = []
        patch_transport(monkeypatch, _recording_handler(seen, ollama_body("hello")))
        client = OllamaClient(OllamaConfig(base_url="http://ollama.test", model="m"))

        await client.generate(AIRequest(prompt="hi", max_tokens=33, temperature=0.1), tier="free")

        assert seen[0]["options"] == {"temperature": 0.1, "num_predict": 33}
        assert seen[0]["stream"] is False
        assert seen[0]["prompt"] == "hi"


class TestOllamaClientFailsLoud:
    async def test_empty_completion_from_reasoning_model_is_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # regression: real gemma4 shape -- answer burned in `thinking`, `response` blank.
        body = ollama_body("", done_reason="length", thinking="Thinking Process: ...")
        patch_transport(monkeypatch, _recording_handler([], body))
        client = OllamaClient(OllamaConfig(base_url="http://ollama.test", model="gemma4:e2b"))

        with pytest.raises(ApiError) as exc_info:
            await client.generate(AIRequest(prompt="hi"), tier="free")

        message = exc_info.value.message
        assert exc_info.value.code == "AI_PROVIDER_ERROR"
        assert "empty completion" in message
        assert "done_reason='length'" in message
        assert "thinking_present=True" in message
        assert "Thinking Process" not in message  # reasoning text never leaks into the error

    @pytest.mark.parametrize("body", [{"done": True}, {"response": None}, {"response": "   "}])
    async def test_missing_null_or_blank_response_is_an_error(
        self, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
    ) -> None:
        patch_transport(monkeypatch, _recording_handler([], body))
        client = OllamaClient(OllamaConfig(base_url="http://ollama.test", model="m"))
        with pytest.raises(ApiError, match="empty completion"):
            await client.generate(AIRequest(prompt="hi"), tier="free")

    async def test_non_json_body_is_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        patch_transport(monkeypatch, lambda request: httpx.Response(200, text="<html>nope</html>"))
        client = OllamaClient(OllamaConfig(base_url="http://ollama.test", model="m"))
        with pytest.raises(ApiError, match="non-JSON response body"):
            await client.generate(AIRequest(prompt="hi"), tier="free")

    async def test_non_object_json_body_is_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        patch_transport(monkeypatch, lambda request: httpx.Response(200, json=["a", "b"]))
        client = OllamaClient(OllamaConfig(base_url="http://ollama.test", model="m"))
        with pytest.raises(ApiError, match="unexpected response shape"):
            await client.generate(AIRequest(prompt="hi"), tier="free")


class TestByokEmptyCompletion:
    async def test_openai_empty_content_is_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = {
            "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 200},
        }
        patch_transport(monkeypatch, lambda request: httpx.Response(200, json=body))
        with pytest.raises(ApiError, match="finish_reason='length'"):
            await OpenAIClient().generate("sk-test-key-0000", AIRequest(prompt="hi"))

    async def test_openai_null_content_is_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = {"choices": [{"message": {"content": None}, "finish_reason": "content_filter"}]}
        patch_transport(monkeypatch, lambda request: httpx.Response(200, json=body))
        with pytest.raises(ApiError, match="empty completion"):
            await OpenAIClient().generate("sk-test-key-0000", AIRequest(prompt="hi"))

    async def test_anthropic_empty_blocks_is_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = {"content": [{"type": "text", "text": ""}], "stop_reason": "max_tokens"}
        patch_transport(monkeypatch, lambda request: httpx.Response(200, json=body))
        with pytest.raises(ApiError, match="stop_reason='max_tokens'"):
            await AnthropicClient().generate("anthropic-test-key", AIRequest(prompt="hi"))

    async def test_nonempty_byok_replies_are_json_mode_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = {"choices": [{"message": {"content": "hello"}}], "usage": {}}
        patch_transport(monkeypatch, lambda request: httpx.Response(200, json=body))
        response = await OpenAIClient().generate(
            "sk-test-key-0000", AIRequest(prompt="hi", wants_json=True)
        )
        assert response.json_mode is False


class TestRouterCapabilityFollowsTierUsed:
    """The fallback ladder lands on the free tier's capability, not the requested tier's."""

    @pytest.fixture
    def tiers(self, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
        monkeypatch.setenv("OLLAMA_URL", "http://ollama.test")
        monkeypatch.setenv("AI_FREE_MODEL", "free-text-model")
        monkeypatch.setenv("AI_PREMIUM_MODEL", "premium-json-model")
        monkeypatch.setenv("AI_PREMIUM_SUPPORTS_JSON", "true")
        seen: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            seen.append(payload)
            text = '{"tier": "premium"}' if "format" in payload else "free plain text"
            return httpx.Response(200, json=ollama_body(text))

        patch_transport(monkeypatch, handler)
        patch_feature_flags(monkeypatch)
        return seen

    async def test_premium_json_capable_returns_json_mode(
        self, ai_routing_db: Any, tiers: list[dict[str, Any]]
    ) -> None:
        async_dal, community_id = seed_community(ai_routing_db)
        await set_enterprise(async_dal, community_id)
        await credit_premium(async_dal, community_id, 1000)

        response = await router.route_completion(
            async_dal,
            async_dal.dal,
            tenant="acme-corp",
            community_id=community_id,
            actor_user_id=1,
            ai_request=AIRequest(prompt="hi", requested_tier="premium", wants_json=True),
            idempotency_key="cap-1",
        )

        assert response.tier_used == "premium"
        assert response.json_mode is True
        assert tiers[0]["model"] == "premium-json-model"
        assert tiers[0]["format"] == "json"
        assert response.billed_tokens == 10  # 7 in + 3 out from the canned body

    async def test_ambient_fallback_to_free_drops_to_text_mode(
        self, ai_routing_db: Any, tiers: list[dict[str, Any]]
    ) -> None:
        async_dal, community_id = seed_community(ai_routing_db)
        await set_enterprise(async_dal, community_id)  # entitled, but zero balance

        response = await router.route_completion(
            async_dal,
            async_dal.dal,
            tenant="acme-corp",
            community_id=community_id,
            actor_user_id=1,
            ai_request=AIRequest(
                prompt="hi", requested_tier="premium", invocation="ambient", wants_json=True
            ),
            idempotency_key="cap-2",
        )

        assert response.tier_used == "free"
        assert response.fallback_reason == "insufficient_balance"
        assert response.json_mode is False
        assert tiers[0]["model"] == "free-text-model"
        assert "format" not in tiers[0]
        assert response.text == "free plain text"

    async def test_free_tier_text_only_by_default_even_when_json_wanted(
        self, ai_routing_db: Any, tiers: list[dict[str, Any]]
    ) -> None:
        async_dal, community_id = seed_community(ai_routing_db)

        response = await router.route_completion(
            async_dal,
            async_dal.dal,
            tenant="acme-corp",
            community_id=community_id,
            actor_user_id=1,
            ai_request=AIRequest(prompt="hi", wants_json=True),
            idempotency_key="cap-3",
        )

        assert response.tier_used == "free"
        assert response.json_mode is False
        assert response.fallback_reason is None
        assert "format" not in tiers[0]

    async def test_premium_metering_failure_keeps_json_mode(
        self, ai_routing_db: Any, tiers: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async_dal, community_id = seed_community(ai_routing_db)
        await set_enterprise(async_dal, community_id)
        await credit_premium(async_dal, community_id, 1000)

        class _Declined:
            success = False

        async def _decline(*args: Any, **kwargs: Any) -> _Declined:
            return _Declined()

        monkeypatch.setattr(router.token_ledger, "debit_tokens", _decline)

        response = await router.route_completion(
            async_dal,
            async_dal.dal,
            tenant="acme-corp",
            community_id=community_id,
            actor_user_id=1,
            ai_request=AIRequest(prompt="hi", requested_tier="premium", wants_json=True),
            idempotency_key="cap-4",
        )

        assert response.fallback_reason == "metering_failed_insufficient_balance"
        assert response.billed_tokens == 0
        assert response.json_mode is True
