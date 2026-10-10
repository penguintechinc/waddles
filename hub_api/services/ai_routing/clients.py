"""Real (never stubbed) HTTP clients for every model-routing tier.

Free/premium both talk to Ollama (different endpoint+model, `OllamaConfig`);
BYOK talks directly to the community's own OpenAI/Anthropic account via
plain `httpx` REST calls (no official SDK -- avoids a new pinned dependency,
per this PR's own instructions; both APIs are simple enough that the direct
REST shape is barely more code than wrapping an SDK client would be). Every
`generate()` makes a real outbound call when reachable/credentialed -- there
is no mocked/fake code path here, only real requests; unit tests mock the
transport layer (`httpx.MockTransport`, `tests/test_analytics_proxy.py`'s
own established pattern), never this module's own logic. An unreachable
Ollama host, a rejected BYOK key, or a non-2xx provider response raises
`services.ai_routing.errors.provider_error()` -- graceful degradation (tier
fallback) happens one layer up, in `router.py`, never inside a client.

BYOK API keys are received already-decrypted (by `config_service.py`) and
used ONLY as an outbound header value here -- never logged, never echoed
into any response or exception message (`httpx.HTTPStatusError.__str__`
includes the request URL but not headers, so the default exception message
is safe to relay via `provider_error()`).

Capability-aware output mode: a model is either text-only or JSON-capable,
and that is CONFIG (`OllamaConfig.supports_json`, per tier, from
`AI_FREE_SUPPORTS_JSON`/`AI_PREMIUM_SUPPORTS_JSON`) -- never inferred from a
hardcoded tier->model map. `AIRequest.wants_json` is honoured (Ollama
`format: json`) only for a JSON-capable model; a text-only model always gets
a plain-text request and `AIResponse.json_mode` says which one the caller
got. Every client fails LOUD on an empty completion (a reasoning model that
burns its whole token budget "thinking" otherwise returns a successful,
metered, blank answer).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
from flask_core.ai_telemetry import AITelemetry
from flask_core.db_errors import describe_db_error, format_sanitized_traceback

from services.ai_routing.errors import invalid_byok_key, provider_error
from services.ai_routing.models import AIRequest, AIResponse, ByokProvider, Tier
from services.ai_routing.pii_redaction import redact_pii
from services.errors import ApiError

logger = logging.getLogger(__name__)

#: Spans + histogram/counters for every Ollama call (PII-free; no-op without an OTel provider).
telemetry = AITelemetry("waddles.hub_api.ai_routing")

_DEFAULT_TIMEOUT_SECONDS = 60.0
_ANTHROPIC_API_VERSION = "2023-06-01"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def _env_flag(name: str, *, default: bool) -> bool:
    """Parse a boolean env var strictly -- a typo'd value fails loud, never a silent default."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ValueError(f"{name}={raw!r} is not a boolean (use true/false/1/0/yes/no/on/off)")


def _describe_http_error(exc: httpx.HTTPError) -> str:
    """Non-sensitive one-liner for a self-hosted Ollama failure: status or class, never the URL.

    `str(httpx.HTTPError)` embeds the request URL -- for the free/premium tiers that is
    the internal Ollama address, which must not be relayed to API callers via
    `provider_error()`. The full exception is logged server-side instead.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


def _require_completion_text(text: str, *, provider: str, detail: str) -> str:
    """Return `text`, or raise `provider_error()` if the provider returned a blank completion.

    A blank answer is never a success: it would be returned to the caller as
    a valid reply (and, on the premium tier, metered). `detail` is the
    provider's own non-sensitive finish reason (`done_reason`/`finish_reason`).
    """
    if not text.strip():
        raise provider_error(f"{provider} returned an empty completion ({detail})")
    return text


@dataclass(slots=True, frozen=True)
class OllamaConfig:
    """One Ollama endpoint + model -- free and premium tiers each get their own."""

    base_url: str
    model: str
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    #: True only for a model that handles JSON/structured output. False (the
    #: default) = text-only: the request carries no `format`, the prompt is plain
    #: text and the caller parses text. Per-tier config, never inferred from the
    #: model name.
    supports_json: bool = False
    #: Send `think: false` so a reasoning model answers directly instead of
    #: spending its token budget on a hidden chain of thought (otherwise the
    #: visible completion is empty). Harmless for non-reasoning models.
    disable_thinking: bool = True


def free_ollama_config() -> OllamaConfig:
    """`OLLAMA_URL` + `AI_FREE_MODEL` -- the always-reachable floor tier (spec §1/§6).

    `AI_FREE_SUPPORTS_JSON` (default false -> text-only path) and
    `AI_FREE_DISABLE_THINKING` (default true) describe the configured model's
    capabilities; they are set per deployment alongside `AI_FREE_MODEL`.
    """
    return OllamaConfig(
        base_url=os.environ.get("OLLAMA_URL", "http://localhost:11434"),
        model=os.environ.get("AI_FREE_MODEL", "llama3.1:1b"),
        supports_json=_env_flag("AI_FREE_SUPPORTS_JSON", default=False),
        disable_thinking=_env_flag("AI_FREE_DISABLE_THINKING", default=True),
    )


def premium_ollama_config() -> OllamaConfig:
    """`OLLAMA_PREMIUM_URL` (falls back to `OLLAMA_URL`) + `AI_PREMIUM_MODEL`.

    The "beefy host" MoE endpoint (spec §6) -- a separate env var so it can
    point at a dedicated GPU node pool distinct from the ubiquitous free
    endpoint; defaults to the same host as free-local for environments
    (local/alpha) that don't run a separate beefy Ollama yet. Live calls
    against a real beefy host are deferred to a later phase (task scope);
    this client is fully real today against whatever `OLLAMA_PREMIUM_URL`
    points at.
    """
    return OllamaConfig(
        base_url=os.environ.get(
            "OLLAMA_PREMIUM_URL", os.environ.get("OLLAMA_URL", "http://localhost:11434")
        ),
        model=os.environ.get("AI_PREMIUM_MODEL", "gemma2:27b"),
        supports_json=_env_flag("AI_PREMIUM_SUPPORTS_JSON", default=False),
        disable_thinking=_env_flag("AI_PREMIUM_DISABLE_THINKING", default=True),
    )


class OllamaClient:
    """Real Ollama `/api/generate` client -- shared by the free and premium tiers."""

    def __init__(self, config: OllamaConfig) -> None:
        """Bind this client to one Ollama endpoint/model pair."""
        self._config = config

    async def generate(self, request: AIRequest, *, tier: Tier) -> AIResponse:
        """Call Ollama's non-streaming generate endpoint; normalize its own token counts.

        Output mode follows the model's configured capability, not the caller's
        wish: `format: json` is sent only when `request.wants_json` AND
        `config.supports_json`. The returned `AIResponse.json_mode` is True only
        in that case, and then `text` has been verified to parse as JSON. The
        call is spanned and timed (`flask_core.ai_telemetry`, PII-free).
        """
        model = self._config.model
        json_mode = request.wants_json and self._config.supports_json
        mode = "json" if json_mode else "text"
        started = time.perf_counter()
        with telemetry.span(provider="ollama", tier=tier, model=model, mode=mode):
            try:
                response = await self._generate(request, tier=tier, json_mode=json_mode)
            except ApiError as exc:
                telemetry.record_call(
                    provider="ollama",
                    tier=tier,
                    model=model,
                    mode=mode,
                    duration_ms=(time.perf_counter() - started) * 1000.0,
                    error_code=exc.code,
                )
                raise
        telemetry.record_call(
            provider="ollama",
            tier=tier,
            model=model,
            mode=mode,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        )
        return response

    async def _generate(self, request: AIRequest, *, tier: Tier, json_mode: bool) -> AIResponse:
        """The un-instrumented call: build the payload, POST, validate and normalize the reply."""
        model = self._config.model
        payload: dict[str, Any] = {
            "model": model,
            "prompt": request.prompt,
            "stream": False,
            "options": {"temperature": request.temperature, "num_predict": request.max_tokens},
        }
        if self._config.disable_thinking:
            payload["think"] = False
        if json_mode:
            payload["format"] = "json"
        elif request.wants_json:
            logger.info(
                "ollama_json_requested_but_model_text_only tier=%s model=%s -> plain-text mode",
                tier,
                model,
            )
        logger.debug(
            "ollama_generate_request tier=%s model=%s mode=%s prompt_chars=%d max_tokens=%d",
            tier,
            model,
            "json" if json_mode else "text",
            len(request.prompt),
            request.max_tokens,
        )
        try:
            async with httpx.AsyncClient(
                base_url=self._config.base_url, timeout=self._config.timeout_seconds
            ) as client:
                response = await client.post("/api/generate", json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            logger.error(
                "ollama_generate_failed tier=%s model=%s %s status=%s",
                tier,
                model,
                describe_db_error(exc),
                status,
            )
            frames = format_sanitized_traceback(exc)  # frames only -- no exception text
            logger.debug("ollama_generate_failed_frames %s", frames)
            raise provider_error(
                f"Ollama ({tier}) request failed: {_describe_http_error(exc)}"
            ) from exc

        data = self._parse_body(response, tier=tier)
        done_reason = data.get("done_reason")
        raw_text = data.get("response")
        text = _require_completion_text(
            raw_text if isinstance(raw_text, str) else "",
            provider=f"Ollama ({tier})",
            detail=(
                f"model={model!r}, done_reason={done_reason!r}, "
                f"thinking_present={bool(data.get('thinking'))}"
            ),
        )
        if json_mode:
            try:
                json.loads(text)
            except ValueError as exc:
                raise provider_error(
                    f"Ollama ({tier}) returned invalid JSON in JSON mode "
                    f"(model={model!r}, done_reason={done_reason!r})"
                ) from exc
        input_tokens = int(data.get("prompt_eval_count", 0) or 0)
        output_tokens = int(data.get("eval_count", 0) or 0)
        logger.debug(
            "ollama_generate_ok tier=%s model=%s mode=%s done_reason=%s in=%d out=%d",
            tier,
            model,
            "json" if json_mode else "text",
            done_reason,
            input_tokens,
            output_tokens,
        )
        return AIResponse(
            text=text,
            provider="ollama",
            model=model,
            tier_used=tier,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            json_mode=json_mode,
        )

    @staticmethod
    def _parse_body(response: httpx.Response, *, tier: Tier) -> dict[str, Any]:
        """Decode Ollama's JSON body; anything else is a loud `provider_error()`."""
        try:
            data = response.json()
        except ValueError as exc:
            raise provider_error(f"Ollama ({tier}) returned a non-JSON response body") from exc
        if not isinstance(data, dict):
            raise provider_error(f"Ollama ({tier}) returned an unexpected response shape")
        return data


class OpenAIClient:
    """Real OpenAI Chat Completions client -- BYOK tier, community's own key."""

    BASE_URL = "https://api.openai.com/v1"

    def __init__(self, *, timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS) -> None:
        """Stateless besides the timeout -- the API key is passed per-call, never stored."""
        self._timeout_seconds = timeout_seconds

    async def generate(self, api_key: str, request: AIRequest) -> AIResponse:
        """POST `/chat/completions`; normalize OpenAI's `usage.{prompt,completion}_tokens`.

        `request.prompt` is redacted (`pii_redaction.redact_pii`) before it
        leaves this process -- this call crosses to a third-party API
        (the community's own OpenAI account), unlike the self-hosted Ollama
        tiers.
        """
        model = request.model_hint or os.environ.get("AI_BYOK_OPENAI_MODEL", "gpt-4o-mini")
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": redact_pii(request.prompt)}],
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        try:
            async with httpx.AsyncClient(
                base_url=self.BASE_URL, timeout=self._timeout_seconds
            ) as client:
                response = await client.post(
                    "/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=payload,
                )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # httpx's default str() omits request/response headers (never the api_key).
            raise provider_error(f"OpenAI request failed: {exc}") from exc

        data = response.json()
        choices = data.get("choices") or [{}]
        text = _require_completion_text(
            str((choices[0].get("message") or {}).get("content") or ""),
            provider="OpenAI",
            detail=f"model={model!r}, finish_reason={choices[0].get('finish_reason')!r}",
        )
        usage = data.get("usage") or {}
        return AIResponse(
            text=text,
            provider="openai",
            model=model,
            tier_used="byok",
            input_tokens=int(usage.get("prompt_tokens", 0) or 0),
            output_tokens=int(usage.get("completion_tokens", 0) or 0),
        )


class AnthropicClient:
    """Real Anthropic Messages API client -- BYOK tier, community's own key."""

    BASE_URL = "https://api.anthropic.com/v1"

    def __init__(self, *, timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS) -> None:
        """Stateless besides the timeout -- the API key is passed per-call, never stored."""
        self._timeout_seconds = timeout_seconds

    async def generate(self, api_key: str, request: AIRequest) -> AIResponse:
        """POST `/messages`; normalize Anthropic's `usage.{input,output}_tokens`.

        `request.prompt` is redacted (`pii_redaction.redact_pii`) before it
        leaves this process -- see `OpenAIClient.generate`'s docstring for
        why this tier redacts and the free/premium Ollama tiers don't.
        """
        model = request.model_hint or os.environ.get(
            "AI_BYOK_ANTHROPIC_MODEL", "claude-3-5-haiku-20241022"
        )
        payload = {
            "model": model,
            "max_tokens": request.max_tokens,
            "messages": [{"role": "user", "content": redact_pii(request.prompt)}],
        }
        try:
            async with httpx.AsyncClient(
                base_url=self.BASE_URL, timeout=self._timeout_seconds
            ) as client:
                response = await client.post(
                    "/messages",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": _ANTHROPIC_API_VERSION,
                        "content-type": "application/json",
                    },
                    json=payload,
                )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise provider_error(f"Anthropic request failed: {exc}") from exc

        data = response.json()
        content_blocks = data.get("content") or [{}]
        text = _require_completion_text(
            "".join(str(block.get("text", "")) for block in content_blocks),
            provider="Anthropic",
            detail=f"model={model!r}, stop_reason={data.get('stop_reason')!r}",
        )
        usage = data.get("usage") or {}
        return AIResponse(
            text=text,
            provider="anthropic",
            model=model,
            tier_used="byok",
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
        )


def byok_client_for(provider: ByokProvider) -> OpenAIClient | AnthropicClient:
    """Return the real client for `provider` -- the only branch point BYOK dispatch needs."""
    if provider == "openai":
        return OpenAIClient()
    return AnthropicClient()


async def validate_byok_key(
    provider: ByokProvider, api_key: str, *, timeout_seconds: float = 10.0
) -> None:
    """Real, cheap validation call against the provider's own `/models` endpoint (spec §3).

    Called by `config_service.set_byok_key()` before a new/rotated key is
    encrypted and committed -- never persist a key that doesn't work.
    Raises `errors.invalid_byok_key()` on a `401`/`403` (the provider
    rejected this specific key), `errors.provider_error()` on any other
    failure (network error, 5xx, unexpected response) -- both real,
    typed outcomes, never a silent pass.
    """
    if provider == "openai":
        url = f"{OpenAIClient.BASE_URL}/models"
        headers = {"Authorization": f"Bearer {api_key}"}
    else:
        url = f"{AnthropicClient.BASE_URL}/models"
        headers = {"x-api-key": api_key, "anthropic-version": _ANTHROPIC_API_VERSION}

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise provider_error(f"{provider} key validation request failed: {exc}") from exc

    if response.status_code in (401, 403):
        raise invalid_byok_key(f"{provider} rejected this API key")
    if response.is_error:
        raise provider_error(f"{provider} key validation returned HTTP {response.status_code}")
