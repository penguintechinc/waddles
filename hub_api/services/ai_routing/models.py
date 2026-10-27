"""Normalized, provider-agnostic request/response shapes for the AI router.

Every provider adapter (`clients.py`'s `OllamaClient`/`OpenAIClient`/
`AnthropicClient`) normalizes its native usage reporting into `AIResponse`'s
flat `input_tokens`/`output_tokens` -- deliberately NOT a nested dataclass
field (`hub_api/PORTING.md` Gotcha #3: a nested-dataclass response after an
`insert_async` call crashes quart-schema's response serializer in this
repo's pinned dependency versions; the completion endpoint debits the token
ledger, an `insert_async` call, before returning, so this response shape
stays flat on purpose rather than needing the `jsonify_dto()` workaround).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from flask_core.ai_guard import RetrievedItem
from flask_core.ai_tool_authz import AuthorizedToolCall, ToolCallRequest, ToolRegistry

#: Model-selection tiers, in fallback-ladder order (spec §2): premium-local
#: falls back to BYOK falls back to free-local, which is always reachable.
Tier = Literal["free", "premium", "byok"]
Invocation = Literal["interactive", "ambient"]
ByokProvider = Literal["openai", "anthropic"]

#: Node's "on insufficient balance" policy split (spec §2): `block` returns
#: an upgrade-path error for a deliberate user action; `fallback_free`
#: silently downgrades for proactive/automatic call-sites.
OnInsufficientBalance = Literal["block", "fallback_free"]


@dataclass(slots=True, frozen=True)
class AIRequest:
    """One normalized completion request -- provider-agnostic, tier-agnostic.

    `wants_json=True` asks for structured (JSON) output, but it is a REQUEST,
    not a guarantee: only a model whose tier is configured `supports_json`
    (`clients.OllamaConfig`) is sent Ollama's `format: json`; a text-only
    model (the free-tier default) is sent a plain-text prompt instead. Callers
    MUST check `AIResponse.json_mode` to learn which one they got -- the
    router's fallback ladder can land on a text-only tier even when the
    requested tier is JSON-capable.

    `requested_tier=None` means "use the community's configured default"
    (`ai_model_config.preferred_tier`) -- callers only set it to force a
    specific tier (e.g. an explicit "use my premium model" UI action).
    Likewise `byok_provider=None` means "use `ai_model_config.
    byok_provider`" -- set only to call a specific provider even though
    the community has keys on file for more than one.

    Prompt-injection posture (OWASP LLM01): `prompt` is the invoking user's own instruction.
    Anything the SERVER retrieved on their behalf (documents, chat history, memories, web
    results) goes in `untrusted_context`, never concatenated into `prompt` -- the clients render
    it as a labelled, delimited, defanged block next to a standing system notice, drop items
    that trip the injection scan, and the router treats such a request as TAINTED (side-effecting
    tools are refused). `system_prompt` is server-owned standing instructions and is never
    settable through the REST DTO. `tools` is the server-side registry of tools this request may
    execute if the model asks for them; `None` (the default) exposes none, so every
    model-requested tool call is denied.
    """

    prompt: str
    max_tokens: int = 512
    temperature: float = 0.7
    requested_tier: Tier | None = None
    model_hint: str | None = None
    byok_provider: ByokProvider | None = None
    invocation: Invocation = "interactive"
    wants_json: bool = False
    system_prompt: str | None = None
    untrusted_context: tuple[RetrievedItem, ...] = ()
    tools: ToolRegistry | None = None


@dataclass(slots=True, frozen=True)
class AIResponse:
    """One normalized completion response -- usage flattened, see module docstring."""

    text: str
    provider: str
    model: str
    tier_used: Tier
    input_tokens: int
    output_tokens: int
    billed_tokens: int = 0
    fallback_reason: str | None = None
    #: True only when the provider was actually put in JSON mode for this call
    #: and the returned `text` was validated as JSON. False = plain text.
    json_mode: bool = False
    #: Tool calls the model asked for, exactly as asked -- UNAUTHORISED. Set by the provider
    #: clients; `router.route_completion()` always empties it (authorising or raising), so a
    #: caller of the router never sees a raw model-chosen call.
    requested_tool_calls: tuple[ToolCallRequest, ...] = ()
    #: Tool calls that passed server-side re-authorisation against the invoking user's tenant and
    #: scopes. The only tool-call shape an executor may act on.
    tool_calls: tuple[AuthorizedToolCall, ...] = ()

    @property
    def total_tokens(self) -> int:
        """Sum of `input_tokens` + `output_tokens` -- what a metered tier bills."""
        return self.input_tokens + self.output_tokens
