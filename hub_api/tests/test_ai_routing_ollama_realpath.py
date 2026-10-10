"""Real-endpoint integration tests: WaddleAI provider/router against a LIVE local Ollama.

Gated behind `WADDLE_TEST_OLLAMA_URL` -- unset (CI) means every test here SKIPS; set means they
run against that endpoint with NO mocked transport. How to run: `docs/testing/ollama-realpath.md`.

THE INTEGRATION IS UNDER TEST, NOT THE MODEL. The lab models are the smallest available and will
give weak/odd answers, so nothing here asserts "input X -> content Y". Asserted instead:
the request on the wire is well-formed, the response is parsed into the right shape, the router
consumes provider results / gate verdicts and acts on them (metering, fallback, refusal), errors
are typed and loud, and PII is redacted before it leaves the process.

HARD LIMIT -- one query at a time: every test uses the `single_flight` fixture, which serializes
all real transport traffic (cross-process lock) and refuses any host except the Ollama endpoint.
The lab Ollama shares one GPU with the live WaddleAI. Each test sends 0-2 requests; the whole
module sends well under 20.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from services import token_ledger
from services.ai_routing import router
from services.ai_routing.clients import (
    AnthropicClient,
    OllamaClient,
    OllamaConfig,
    OpenAIClient,
    free_ollama_config,
    premium_ollama_config,
    validate_byok_key,
)
from services.ai_routing.errors import ApiError
from services.ai_routing.models import AIRequest
from tests.ai_routing_helpers import (
    credit_premium,
    patch_feature_flags,
    seed_community,
    set_enterprise,
)
from tests.ollama_support import SingleFlightGuard

pytestmark = pytest.mark.ollama_realpath

#: Synthetic-only strings shaped like the secrets/PII `pii_redaction` targets. None are real.
SYNTHETIC_EMAIL = "jane.doe.synthetic@example.com"
SYNTHETIC_SK = "sk-" + "SYNTHETICKEY0123456789ab"  # assembled so secret scanners skip it
SYNTHETIC_WA = "wa-SYNTHETIC0123456789"
SYNTHETIC_BEARER = "Bearer SYNTHETICbearer0123456789"
SYNTHETIC_JWT = ".".join(("eyJhbGciOiJub25lIn0", "eyJzdWIiOiJzeW50aGV0aWMifQ", "c3ludGhldGljc2ln"))
SYNTHETIC_SECRETS = (
    SYNTHETIC_EMAIL,
    SYNTHETIC_SK,
    SYNTHETIC_WA,
    SYNTHETIC_BEARER.removeprefix("Bearer "),
    SYNTHETIC_JWT,
)
PII_PROMPT = (
    f"Support note: customer {SYNTHETIC_EMAIL} pasted key {SYNTHETIC_SK} and {SYNTHETIC_WA}, "
    f"header 'Authorization: {SYNTHETIC_BEARER}', token {SYNTHETIC_JWT}. "
    "Reply with one short sentence acknowledging the note."
)
SHORT_PROMPT = "Reply with one short sentence about penguins."


@pytest.fixture
def ollama_env(monkeypatch: pytest.MonkeyPatch, ollama_url: str, text_model: str) -> dict[str, str]:
    """Point both tiers at the lab endpoint with the baseline text-only model; no JSON flags set."""
    for name in (
        "OLLAMA_PREMIUM_URL",
        "AI_PREMIUM_MODEL",
        "AI_FREE_SUPPORTS_JSON",
        "AI_PREMIUM_SUPPORTS_JSON",
        "AI_FREE_DISABLE_THINKING",
        "AI_PREMIUM_DISABLE_THINKING",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OLLAMA_URL", ollama_url)
    monkeypatch.setenv("AI_FREE_MODEL", text_model)
    return {"url": ollama_url, "model": text_model}


def _generate_request(guard: SingleFlightGuard) -> dict[str, Any]:
    """The single `/api/generate` body that went over the wire."""
    sent = guard.requests_to("/api/generate")
    assert len(sent) == 1, f"expected exactly one generate call, saw {len(sent)}"
    assert sent[0].method == "POST"
    assert sent[0].headers["content-type"].startswith("application/json")
    body: dict[str, Any] = sent[0].json()
    return body


class TestOllamaClientRealPath:
    async def test_free_default_is_the_text_path_even_when_json_is_wanted(
        self, ollama_env: dict[str, str], single_flight: SingleFlightGuard
    ) -> None:
        config = free_ollama_config()
        assert config.model == ollama_env["model"]
        assert config.supports_json is False  # default free tier: TEXT path

        response = await OllamaClient(config).generate(
            AIRequest(prompt=SHORT_PROMPT, max_tokens=64, temperature=0.0, wants_json=True),
            tier="free",
        )

        wire = _generate_request(single_flight)
        assert wire["model"] == ollama_env["model"]
        assert wire["prompt"] == SHORT_PROMPT
        assert wire["stream"] is False
        assert wire["think"] is False  # reasoning model answers directly, no hidden CoT budget burn
        assert wire["options"] == {"temperature": 0.0, "num_predict": 64}
        assert "format" not in wire  # text-only model is never sent structured-output params
        assert response.json_mode is False
        assert isinstance(response.text, str) and response.text.strip()
        assert response.provider == "ollama"
        assert response.model == ollama_env["model"]
        assert response.tier_used == "free"
        assert response.input_tokens > 0
        assert response.output_tokens > 0
        assert response.total_tokens == response.input_tokens + response.output_tokens
        assert single_flight.max_in_flight == 1

    async def test_json_capable_model_is_put_in_json_mode(
        self, ollama_env: dict[str, str], json_model: str, single_flight: SingleFlightGuard
    ) -> None:
        config = OllamaConfig(base_url=ollama_env["url"], model=json_model, supports_json=True)

        try:
            response = await OllamaClient(config).generate(
                AIRequest(
                    prompt='Return a JSON object with one key "status" whose value is "ok".',
                    max_tokens=128,
                    temperature=0.0,
                    wants_json=True,
                ),
                tier="premium",
            )
        except ApiError as exc:
            # A weak model may still emit unparseable JSON; the contract is that the client
            # then fails LOUD and typed instead of handing text back flagged as JSON.
            assert exc.code == "AI_PROVIDER_ERROR"
            assert "invalid JSON" in exc.message
        else:
            assert response.json_mode is True
            assert response.model == json_model
            assert response.tier_used == "premium"
            json.loads(response.text)  # validated by the client; re-parse proves the shape
            assert response.output_tokens > 0

        wire = _generate_request(single_flight)
        assert wire["model"] == json_model
        assert wire["format"] == "json"

    async def test_thinking_left_on_never_yields_a_silent_blank_success(
        self, ollama_env: dict[str, str], single_flight: SingleFlightGuard
    ) -> None:
        # regression: gemma4 spends the whole num_predict budget in `thinking`, `response == ""`.
        config = OllamaConfig(
            base_url=ollama_env["url"], model=ollama_env["model"], disable_thinking=False
        )

        try:
            response = await OllamaClient(config).generate(
                AIRequest(prompt=SHORT_PROMPT, max_tokens=16, temperature=0.0), tier="free"
            )
        except ApiError as exc:
            assert exc.status_code == 502
            assert exc.code == "AI_PROVIDER_ERROR"
            assert "empty completion" in exc.message
            assert "done_reason" in exc.message
        else:
            assert response.text.strip(), "client returned a blank completion as a success"

        assert "think" not in _generate_request(single_flight)

    async def test_unknown_model_is_a_typed_error_without_leaking_the_endpoint(
        self, ollama_env: dict[str, str], single_flight: SingleFlightGuard
    ) -> None:
        config = OllamaConfig(base_url=ollama_env["url"], model="waddle-test-no-such-model:0")

        with pytest.raises(ApiError) as exc_info:
            await OllamaClient(config).generate(AIRequest(prompt="hi"), tier="free")

        assert exc_info.value.status_code == 502
        assert exc_info.value.code == "AI_PROVIDER_ERROR"
        assert "HTTP 404" in exc_info.value.message
        assert single_flight.requests[0].host not in exc_info.value.message
        assert len(single_flight.requests) == 1  # no retry storm against the shared GPU


class TestRouterRealPath:
    async def test_default_config_routes_to_free_and_returns_a_valid_completion(
        self,
        ai_routing_db: Any,
        ollama_env: dict[str, str],
        single_flight: SingleFlightGuard,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async_dal, community_id = seed_community(ai_routing_db)
        patch_feature_flags(monkeypatch)

        response = await router.route_completion(
            async_dal,
            async_dal.dal,
            tenant="acme-corp",
            community_id=community_id,
            actor_user_id=1,
            ai_request=AIRequest(prompt=SHORT_PROMPT, max_tokens=64, temperature=0.0),
            idempotency_key="real-free-1",
        )

        assert response.tier_used == "free"
        assert response.model == ollama_env["model"]
        assert response.text.strip()
        assert response.billed_tokens == 0  # free is never metered
        assert response.fallback_reason is None
        assert response.json_mode is False
        assert response.input_tokens > 0 and response.output_tokens > 0
        assert _generate_request(single_flight)["model"] == ollama_env["model"]

    async def test_premium_meters_the_real_usage_numbers_the_provider_reported(
        self,
        ai_routing_db: Any,
        ollama_env: dict[str, str],
        single_flight: SingleFlightGuard,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("AI_PREMIUM_MODEL", ollama_env["model"])  # any model proves metering
        assert premium_ollama_config().base_url == ollama_env["url"]
        async_dal, community_id = seed_community(ai_routing_db)
        await set_enterprise(async_dal, community_id)
        await credit_premium(async_dal, community_id, 100_000)
        patch_feature_flags(monkeypatch)

        response = await router.route_completion(
            async_dal,
            async_dal.dal,
            tenant="acme-corp",
            community_id=community_id,
            actor_user_id=7,
            ai_request=AIRequest(
                prompt=SHORT_PROMPT, max_tokens=64, temperature=0.0, requested_tier="premium"
            ),
            idempotency_key="real-premium-1",
        )

        assert response.tier_used == "premium"
        assert response.fallback_reason is None
        assert response.text.strip()
        assert response.total_tokens > 0
        assert response.billed_tokens == response.total_tokens  # real eval counts, not a stub
        balance = await token_ledger.get_balance(
            async_dal, async_dal.dal, community_id=community_id
        )
        assert balance == 100_000 - response.billed_tokens
        assert len(single_flight.requests_to("/api/generate")) == 1

    async def test_ambient_premium_without_entitlement_falls_back_to_the_text_only_free_tier(
        self,
        ai_routing_db: Any,
        ollama_env: dict[str, str],
        single_flight: SingleFlightGuard,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Premium is JSON-capable, free is not; the fallback must serve (and report) TEXT mode.
        monkeypatch.setenv("AI_PREMIUM_MODEL", "premium-json-capable-model")
        monkeypatch.setenv("AI_PREMIUM_SUPPORTS_JSON", "true")
        async_dal, community_id = seed_community(ai_routing_db)  # not enterprise -> not entitled
        patch_feature_flags(monkeypatch)

        response = await router.route_completion(
            async_dal,
            async_dal.dal,
            tenant="acme-corp",
            community_id=community_id,
            actor_user_id=1,
            ai_request=AIRequest(
                prompt=SHORT_PROMPT,
                max_tokens=64,
                temperature=0.0,
                requested_tier="premium",
                invocation="ambient",
                wants_json=True,
            ),
            idempotency_key="real-ambient-1",
        )

        assert response.tier_used == "free"
        assert response.fallback_reason == "not_entitled"
        assert response.json_mode is False
        assert response.text.strip()
        wire = _generate_request(single_flight)
        assert wire["model"] == ollama_env["model"]  # the premium model never reached the wire
        assert "format" not in wire

    async def test_closed_gates_never_reach_the_provider(
        self,
        ai_routing_db: Any,
        ollama_env: dict[str, str],
        single_flight: SingleFlightGuard,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async_dal, community_id = seed_community(ai_routing_db)
        kwargs: dict[str, Any] = {
            "tenant": "acme-corp",
            "community_id": community_id,
            "actor_user_id": 1,
            "idempotency_key": "real-gates-1",
        }

        patch_feature_flags(monkeypatch)
        with pytest.raises(ApiError) as killed:
            await router.route_completion(
                async_dal,
                async_dal.dal,
                ai_request=AIRequest(prompt="x"),
                ai_enabled=False,
                **kwargs,
            )
        assert killed.value.code == "AI_DISABLED_DEPLOYMENT"

        patch_feature_flags(monkeypatch, disabled=frozenset({router.FEATURE_AI_ROUTING}))
        with pytest.raises(ApiError) as flagged:
            await router.route_completion(
                async_dal, async_dal.dal, ai_request=AIRequest(prompt="x"), **kwargs
            )
        assert flagged.value.code == "AI_ROUTING_DISABLED"

        patch_feature_flags(monkeypatch)
        with pytest.raises(ApiError) as unentitled:
            await router.route_completion(
                async_dal,
                async_dal.dal,
                ai_request=AIRequest(prompt="x", requested_tier="premium"),
                **kwargs,
            )
        assert unentitled.value.code == "AI_TIER_NOT_ENTITLED"

        assert single_flight.requests == []  # the verdicts were acted on: zero model traffic

    async def test_provider_failure_propagates_loudly_with_no_silent_fallback(
        self,
        ai_routing_db: Any,
        ollama_env: dict[str, str],
        single_flight: SingleFlightGuard,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("AI_FREE_MODEL", "waddle-test-no-such-model:0")
        async_dal, community_id = seed_community(ai_routing_db)
        patch_feature_flags(monkeypatch)

        with pytest.raises(ApiError) as exc_info:
            await router.route_completion(
                async_dal,
                async_dal.dal,
                tenant="acme-corp",
                community_id=community_id,
                actor_user_id=1,
                ai_request=AIRequest(prompt="hi", invocation="ambient"),
                idempotency_key="real-fail-1",
            )

        assert exc_info.value.status_code == 502
        assert exc_info.value.code == "AI_PROVIDER_ERROR"
        assert len(single_flight.requests) == 1  # free is the floor: nothing to fall back to


class TestOpenAICompatibleLegsAndPiiRedaction:
    """The BYOK clients, pointed at Ollama's OpenAI/Anthropic-compatible `/v1` surface.

    The only way to exercise `OpenAIClient`/`AnthropicClient` end-to-end without a real third-party
    account: swap their class-level `BASE_URL`. The synthetic key never leaves the LAN, and the
    single-flight guard would refuse any non-Ollama host anyway.
    """

    SYNTHETIC_API_KEY = "synthetic-not-a-real-key-0000"

    def _assert_redacted_on_wire(self, raw_body: bytes) -> None:
        text = raw_body.decode("utf-8")
        for secret in SYNTHETIC_SECRETS:
            assert secret not in text, f"unredacted secret/PII reached the wire: {secret[:8]}..."
        assert "[REDACTED_EMAIL]" in text
        assert "[REDACTED_TOKEN]" in text

    async def test_openai_client_redacts_pii_before_the_llm_call(
        self,
        ollama_env: dict[str, str],
        single_flight: SingleFlightGuard,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(OpenAIClient, "BASE_URL", f"{ollama_env['url']}/v1")

        try:
            response = await OpenAIClient().generate(
                self.SYNTHETIC_API_KEY,
                AIRequest(
                    prompt=PII_PROMPT,
                    model_hint=ollama_env["model"],
                    max_tokens=2048,
                    temperature=0.0,
                ),
            )
        except ApiError as exc:
            # Ollama's /v1 surface cannot disable reasoning, so a small model may exhaust the
            # budget; that must surface as the typed empty-completion error, nothing else.
            assert exc.code == "AI_PROVIDER_ERROR"
            assert "empty completion" in exc.message
        else:
            assert response.provider == "openai"
            assert response.tier_used == "byok"
            assert response.model == ollama_env["model"]
            assert response.text.strip()
            assert response.input_tokens > 0 and response.output_tokens > 0

        sent = single_flight.requests_to("/v1/chat/completions")
        assert len(sent) == 1
        assert sent[0].headers["authorization"] == f"Bearer {self.SYNTHETIC_API_KEY}"
        body = sent[0].json()
        assert body["model"] == ollama_env["model"]
        assert body["max_tokens"] == 2048
        assert [m["role"] for m in body["messages"]] == ["user"]
        assert "pasted key" in body["messages"][0]["content"]  # non-PII context survives
        self._assert_redacted_on_wire(sent[0].body)
        assert self.SYNTHETIC_API_KEY not in sent[0].body.decode("utf-8")

    async def test_anthropic_client_parses_a_real_response(
        self,
        ollama_env: dict[str, str],
        single_flight: SingleFlightGuard,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(AnthropicClient, "BASE_URL", f"{ollama_env['url']}/v1")

        response = await AnthropicClient().generate(
            self.SYNTHETIC_API_KEY,
            AIRequest(
                prompt=SHORT_PROMPT,
                model_hint=ollama_env["model"],
                max_tokens=2048,
                temperature=0.0,
            ),
        )

        assert response.provider == "anthropic"
        assert response.tier_used == "byok"
        assert response.model == ollama_env["model"]
        assert response.text.strip()
        assert response.input_tokens > 0 and response.output_tokens > 0
        assert response.json_mode is False

        sent = single_flight.requests_to("/v1/messages")
        assert len(sent) == 1
        assert sent[0].headers["x-api-key"] == self.SYNTHETIC_API_KEY
        assert sent[0].headers["anthropic-version"] == "2023-06-01"
        assert sent[0].headers["content-type"].startswith("application/json")
        body = sent[0].json()
        assert body["model"] == ollama_env["model"]
        assert body["max_tokens"] == 2048
        assert body["messages"] == [{"role": "user", "content": SHORT_PROMPT}]

    async def test_anthropic_client_redacts_pii_before_the_llm_call(
        self,
        ollama_env: dict[str, str],
        single_flight: SingleFlightGuard,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(AnthropicClient, "BASE_URL", f"{ollama_env['url']}/v1")

        try:
            await AnthropicClient().generate(
                self.SYNTHETIC_API_KEY,
                AIRequest(
                    prompt=PII_PROMPT,
                    model_hint=ollama_env["model"],
                    max_tokens=2048,
                    temperature=0.0,
                ),
            )
        except ApiError as exc:
            # Reasoning models spend max_tokens thinking; a blank answer must be the typed error.
            assert exc.code == "AI_PROVIDER_ERROR"
            assert "empty completion" in exc.message

        sent = single_flight.requests_to("/v1/messages")
        assert len(sent) == 1
        body = sent[0].json()
        assert [m["role"] for m in body["messages"]] == ["user"]
        assert "customer" in body["messages"][0]["content"]  # non-PII context survives
        self._assert_redacted_on_wire(sent[0].body)
        assert self.SYNTHETIC_API_KEY not in sent[0].body.decode("utf-8")

    @pytest.mark.parametrize("provider", ["openai", "anthropic"])
    async def test_key_validation_round_trips_against_the_models_endpoint(
        self,
        provider: str,
        ollama_env: dict[str, str],
        single_flight: SingleFlightGuard,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(OpenAIClient, "BASE_URL", f"{ollama_env['url']}/v1")
        monkeypatch.setattr(AnthropicClient, "BASE_URL", f"{ollama_env['url']}/v1")

        await validate_byok_key(provider, self.SYNTHETIC_API_KEY)  # type: ignore[arg-type]

        sent = single_flight.requests_to("/v1/models")
        assert len(sent) == 1 and sent[0].method == "GET"
        header = "authorization" if provider == "openai" else "x-api-key"
        assert self.SYNTHETIC_API_KEY in sent[0].headers[header]

    async def test_key_validation_failure_is_typed_when_the_endpoint_is_wrong(
        self,
        ollama_env: dict[str, str],
        single_flight: SingleFlightGuard,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A path Ollama really does not serve -> real 404 -> typed provider error, not a pass.
        monkeypatch.setattr(OpenAIClient, "BASE_URL", f"{ollama_env['url']}/not-an-api")

        with pytest.raises(ApiError) as exc_info:
            await validate_byok_key("openai", self.SYNTHETIC_API_KEY)

        assert exc_info.value.code == "AI_PROVIDER_ERROR"
        assert "404" in exc_info.value.message
