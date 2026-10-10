"""`route_completion()` -- the single entrypoint every AI-using caller in hub-api goes through.

Real dispatch, no stubs: resolves the community's AI config + entitlement
(PostHog flag AND Enterprise license tier -- `critical-rules.md`'s two-gate
model), picks a tier, calls the matching REAL client (`clients.py`), meters
premium-tier usage against the token ledger (`services.token_ledger`), and
falls back down the ladder (premium -> BYOK-configured-tier -> free) exactly
as specified in `docs/plans/2026-08-31-premium-ai-routing-design.md` §2:
`interactive` invocations BLOCK with a typed, upgrade-path error on a failed
gate; `ambient` invocations gracefully downgrade to free-local instead
(never a hard failure -- `client.md` graceful-degradation rule).

Free-local is always reachable once the base `waddles.ai.routing` flag is
on -- it is the floor every fallback path lands on.

`route_completion()`'s own `ai_enabled` parameter is a SEPARATE, deploy-time
kill-switch (`WADDLES_AI_ENABLED`, `config.py`) checked before all of the
above -- ONE-WAY (can only turn AI off), never a substitute for the
flag/license gates this module already enforces.
"""

from __future__ import annotations

from typing import Any

from flask_core.feature_flags import feature_enabled

from services import token_ledger
from services.ai_routing import config_service
from services.ai_routing.clients import (
    OllamaClient,
    byok_client_for,
    free_ollama_config,
    premium_ollama_config,
)
from services.ai_routing.errors import (
    ai_disabled_by_deployment,
    ai_routing_disabled,
    byok_key_missing,
    insufficient_balance,
    not_entitled,
)
from services.ai_routing.models import AIRequest, AIResponse, ByokProvider, Tier
from services.errors import ApiError

#: Base capability gate -- covers the whole endpoint, including free tier
#: (general.md: every feature behind its own flag, defaulted OFF).
FEATURE_AI_ROUTING = "waddles.ai.routing"
#: Premium-local-metered tier gate (spec §7) -- flag AND Enterprise tier.
FEATURE_AI_PREMIUM = "waddles.ai.premium_models"
#: BYOK tier gate (spec §7) -- flag AND Enterprise tier.
FEATURE_AI_BYOK = "waddles.ai.byok"


async def _is_enterprise_tier(async_dal: Any, dal: Any, *, community_id: int) -> bool:
    """Direct `communities.license_tier` read -- same pattern `workflow_service.validate_license()`.

    Not routed through the `FeatureContract`/`tier_requirements` catalog
    (`libs/core_platform_module/features.py`) -- that registration lives
    outside hub-api's own directory tree and isn't wired up for this
    brand-new capability yet; this direct check is the same
    self-contained fallback the pre-existing `workflow_service.py` port
    already established for exactly this situation (see that module's
    own docstring), so premium/BYOK entitlement is never silently
    unenforced just because the flag registry hasn't caught up.
    """
    rows = await async_dal.select_async(dal(dal.communities.id == community_id))
    if not rows:
        return False
    tier = (rows.first().license_tier or "").strip().lower()
    return tier == "enterprise"


async def _premium_entitled(async_dal: Any, dal: Any, *, tenant: str, community_id: int) -> bool:
    if not await feature_enabled(FEATURE_AI_PREMIUM, tenant=tenant, community=community_id):
        return False
    return await _is_enterprise_tier(async_dal, dal, community_id=community_id)


async def _byok_entitled(async_dal: Any, dal: Any, *, tenant: str, community_id: int) -> bool:
    if not await feature_enabled(FEATURE_AI_BYOK, tenant=tenant, community=community_id):
        return False
    return await _is_enterprise_tier(async_dal, dal, community_id=community_id)


async def _run_premium(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    actor_user_id: int | None,
    ai_request: AIRequest,
    idempotency_key: str,
) -> AIResponse:
    """Call the premium Ollama endpoint, then meter it -- assumes balance was already confirmed."""
    client = OllamaClient(premium_ollama_config())
    response = await client.generate(ai_request, tier="premium")
    debit = await token_ledger.debit_tokens(
        async_dal,
        dal,
        community_id,
        token_ledger.PREMIUM_AI_CONSUMABLE,
        response.total_tokens,
        idempotency_key=idempotency_key,
        source_ref=f"community:{community_id}",
        actor_user_id=actor_user_id,
    )
    if debit.success:
        return AIResponse(
            text=response.text,
            provider=response.provider,
            model=response.model,
            tier_used="premium",
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            billed_tokens=response.total_tokens,
            json_mode=response.json_mode,
        )
    # Balance dropped between the pre-check and this debit (concurrent
    # spend) -- compute already happened; bill 0 and say so explicitly
    # rather than silently eating the shortfall or discarding the
    # response the model already generated.
    return AIResponse(
        text=response.text,
        provider=response.provider,
        model=response.model,
        tier_used="premium",
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        billed_tokens=0,
        fallback_reason="metering_failed_insufficient_balance",
        json_mode=response.json_mode,
    )


async def _run_byok(
    async_dal: Any, dal: Any, *, provider: ByokProvider, community_id: int, ai_request: AIRequest
) -> AIResponse:
    api_key = await config_service.get_active_byok_key_plaintext(
        async_dal, dal, community_id=community_id, provider=provider
    )
    if api_key is None:
        raise byok_key_missing(f"No active {provider} key configured for this community")
    client = byok_client_for(provider)
    return await client.generate(api_key, ai_request)


async def _run_free(ai_request: AIRequest, *, fallback_reason: str | None) -> AIResponse:
    client = OllamaClient(free_ollama_config())
    response = await client.generate(ai_request, tier="free")
    if fallback_reason is None:
        return response
    return AIResponse(
        text=response.text,
        provider=response.provider,
        model=response.model,
        tier_used="free",
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        billed_tokens=0,
        fallback_reason=fallback_reason,
        json_mode=response.json_mode,
    )


async def route_completion(
    async_dal: Any,
    dal: Any,
    *,
    tenant: str,
    community_id: int,
    actor_user_id: int | None,
    ai_request: AIRequest,
    idempotency_key: str,
    ai_enabled: bool = True,
) -> AIResponse:
    """Route one completion request through the free/premium/BYOK ladder. Real dispatch throughout.

    `idempotency_key` must be unique per logical attempt (a caller retry
    after a network timeout should reuse the same key so a premium debit
    is never double-charged -- see `token_ledger.debit_tokens()`).

    `ai_enabled` is the deploy-time `WADDLES_AI_ENABLED` kill-switch
    (`config.py`'s `HubAPIConfig.ai_enabled`), threaded in by the caller
    (`blueprints/v1/ai_routing.py` reads it off `HUB_API_CONFIG`) rather
    than read from the environment here -- keeps this a pure function of
    its arguments and matches every other config value this module already
    receives as a parameter instead of reaching into `os.environ` itself.
    Defaults `True` (current full-feature behavior) so existing callers/
    tests that don't pass it are unaffected. Checked BEFORE the
    `waddles.ai.routing` PostHog flag and before any DAL/config_service
    call -- a deploy with `ai_enabled=False` never touches the community's
    AI config row and never attempts an outbound Ollama/OpenAI/Anthropic
    call.
    """
    if not ai_enabled:
        raise ai_disabled_by_deployment()

    if not await feature_enabled(FEATURE_AI_ROUTING, tenant=tenant, community=community_id):
        raise ai_routing_disabled()

    config = await config_service.get_ai_config(async_dal, dal, community_id=community_id)
    tier: Tier = ai_request.requested_tier or config.preferred_tier
    ambient = ai_request.invocation == "ambient"
    fallback_reason: str | None = None

    if tier == "premium":
        entitled = await _premium_entitled(async_dal, dal, tenant=tenant, community_id=community_id)
        if entitled:
            balance = await token_ledger.get_balance(
                async_dal,
                dal,
                community_id=community_id,
                consumable_type=token_ledger.PREMIUM_AI_CONSUMABLE,
            )
            if balance > 0:
                return await _run_premium(
                    async_dal,
                    dal,
                    community_id=community_id,
                    actor_user_id=actor_user_id,
                    ai_request=ai_request,
                    idempotency_key=idempotency_key,
                )
            if ambient or config.on_insufficient_balance == "fallback_free":
                tier, fallback_reason = "free", "insufficient_balance"
            else:
                raise insufficient_balance()
        elif ambient:
            tier, fallback_reason = "free", "not_entitled"
        else:
            raise not_entitled("Premium AI models require an Enterprise plan")

    if tier == "byok":
        entitled = await _byok_entitled(async_dal, dal, tenant=tenant, community_id=community_id)
        provider = ai_request.byok_provider or config.byok_provider
        if entitled and provider is not None:
            try:
                return await _run_byok(
                    async_dal,
                    dal,
                    provider=provider,
                    community_id=community_id,
                    ai_request=ai_request,
                )
            except ApiError:
                # Covers both "no active key on file" (`_run_byok` itself)
                # and a real provider-call failure (`clients.py`'s
                # `provider_error()`, bad-request-401 or connection
                # error) -- spec §2: BYOK failures fall back to
                # free-local only for ambient invocations, never charge
                # platform tokens either way (BYOK is never metered).
                if not ambient:
                    raise
                fallback_reason = "byok_call_failed"
                tier = "free"
        elif ambient:
            tier = "free"
            fallback_reason = "not_entitled" if not entitled else "byok_provider_not_configured"
        else:
            if not entitled:
                raise not_entitled("BYOK requires an Enterprise plan")
            raise byok_key_missing("No BYOK provider configured for this community")

    return await _run_free(ai_request, fallback_reason=fallback_reason)
