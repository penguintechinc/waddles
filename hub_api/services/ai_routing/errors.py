"""AI-routing error factories -- same `ApiError` factory convention as `services/errors.py`.

Every route/service function raises one of these; the blueprint catches
`ApiError` and converts it via `flask_core.api_utils.error_response`
(`services/errors.py`'s own module docstring). Kept in their own module
(rather than added to `services/errors.py` directly) since these are
domain-specific to AI routing/billing, not generic HTTP-status factories
every group needs.
"""

from __future__ import annotations

from dataclasses import dataclass

from services.errors import ApiError


def ai_routing_disabled(message: str = "AI routing is not enabled for this community") -> ApiError:
    """503 -- the base `waddles.ai.routing` flag is off."""
    return ApiError(message, 503, "AI_ROUTING_DISABLED")


def ai_disabled_by_deployment(
    message: str = "AI features are disabled in this deployment",
) -> ApiError:
    """503 -- the deploy-time `WADDLES_AI_ENABLED` kill-switch (`config.py`'s `ai_enabled`) is off.

    Distinct from `ai_routing_disabled()` (the per-community PostHog flag):
    this one is a whole-deployment, ops-controlled switch checked before ANY
    per-community flag/entitlement/DAL work happens or any outbound model
    call is attempted -- lets hub-api run on a machine with no Ollama/model
    backend reachable at all. `blueprints/v1/ai_routing.py`'s handlers raise
    this before touching community auth/config; `router.route_completion()`
    raises it as its own first line too, independent of the blueprint.
    """
    return ApiError(message, 503, "AI_DISABLED_DEPLOYMENT")


def not_entitled(message: str = "This AI tier requires a higher plan") -> ApiError:
    """403 -- flag-or-license-tier gate failed for a premium/BYOK tier request."""
    return ApiError(message, 403, "AI_TIER_NOT_ENTITLED")


def insufficient_balance(message: str = "Insufficient premium-AI token balance") -> ApiError:
    """402 -- `on_insufficient_balance == 'block'` and the community can't afford it."""
    return ApiError(message, 402, "AI_INSUFFICIENT_BALANCE")


def byok_key_missing(message: str = "No BYOK key configured for this provider") -> ApiError:
    """409 -- BYOK tier requested/configured but no active key is on file."""
    return ApiError(message, 409, "AI_BYOK_KEY_MISSING")


def provider_error(message: str) -> ApiError:
    """502 -- the upstream model provider (Ollama/OpenAI/Anthropic) call failed."""
    return ApiError(message, 502, "AI_PROVIDER_ERROR")


def invalid_byok_key(message: str = "BYOK API key failed validation") -> ApiError:
    """400 -- a rotate/set call's key was rejected by the provider's own API."""
    return ApiError(message, 400, "AI_BYOK_KEY_INVALID")


#: ``ApiError.code`` for a refused model-requested tool call. Routers key off this to make sure a
#: tool-call denial is never downgraded to a silent tier fallback.
TOOL_CALL_DENIED_CODE = "AI_TOOL_CALL_DENIED"


@dataclass(slots=True)
class ToolCallDeniedError(ApiError):
    """403 -- a model-requested tool call failed server-side re-authorisation (fail-closed).

    ``reason`` is the stable ``flask_core.ai_tool_authz`` denial code (e.g. ``scope_denied``,
    ``reserved_argument``); it never carries the model's tool name or any argument value.
    """

    reason: str = "denied"


def ai_tool_call_denied(reason: str) -> ToolCallDeniedError:
    """403 -- the model asked for an action the invoking user's tenant/scopes do not permit."""
    return ToolCallDeniedError(
        "The AI response requested an action that is not permitted",
        403,
        TOOL_CALL_DENIED_CODE,
        reason,
    )
