"""v1 `ai_routing` group -- premium-AI model-routing + BYOK config, completion proxy.

Greenfield feature (no Node controller to port) -- see
`docs/plans/2026-08-31-premium-ai-routing-design.md`. Two blueprints, same
split shape `community_activity.py`/`community_interaction.py` already
establish for this repo (admin-only config vs member-facing action):

  * `ai_config_bp` (`/api/v1/admin/<community_id>/ai/...`) -- tier choice +
    BYOK key set/rotate/list/delete. Admin-only: `services.community_access.
    require_community_admin()` (DB-backed owner/admin membership check, the
    IDOR-hardened primitive this repo's newer port groups standardize on
    for anything touching secrets -- BYOK keys are exactly that).
  * `ai_completions_bp` (`/api/v1/community/<community_id>/ai/completions`)
    -- the actual completion proxy, gated by `require_community_member()`
    (any active member, not just admins -- this is a usage endpoint, not a
    config one).

Both gate on `tenant_middleware` first (security.md ordering contract --
tenant resolution/auth always runs before any handler body). Every
handler's own body then checks `_ai_enabled()` as its very first line,
before `_require_admin()`/`_require_member()` or any DAL/config_service
call -- the deploy-time `WADDLES_AI_ENABLED` kill-switch (`config.py`)
that lets hub-api run on a machine with no Ollama/model backend reachable
at all. Disabled means a clean 503 (`ai_disabled_by_deployment()`), never
a crash, never a DAL query, never anything that gets close to an outbound
model call.

Response DTOs are flat (no nested-dataclass fields) throughout -- safe
under `@validate_response` even on routes that call `insert_async`
first (`hub_api/PORTING.md` Gotcha #3's own "flat responses ... were
empirically confirmed safe" note); `jsonify_dto()` is not needed here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast
from uuid import uuid4

from flask_core.api_utils import error_response
from flask_core.authz import AuthzDecision
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services.ai_routing import config_service
from services.ai_routing.errors import ToolCallDeniedError, ai_disabled_by_deployment
from services.ai_routing.models import AIRequest
from services.ai_routing.router import route_completion
from services.community_access import require_community_admin, require_community_member
from services.current_user import get_current_scopes, get_current_user_id
from services.errors import ApiError

ai_config_bp = Blueprint("v1_ai_config", __name__, url_prefix="/api/v1/admin")
ai_completions_bp = Blueprint("v1_ai_completions", __name__, url_prefix="/api/v1/community")


def _dal() -> tuple[Any, Any]:
    """Return `(async_dal, dal)` from app config -- same accessor shape as every other blueprint."""
    return current_app.config["async_dal"], current_app.config["dal"]


def _ai_enabled() -> bool:
    """Read the deploy-time `WADDLES_AI_ENABLED` kill-switch off `HubAPIConfig` (`config.py`).

    Every handler in this module calls this as the first line of its own
    body -- after `tenant_middleware` (security.md's mandatory ordering:
    tenant resolution/auth always runs first) but before
    `_require_admin()`/`_require_member()` or any DAL/config_service call.
    A deployment with AI disabled never touches a community's AI config row
    and never gets close to an outbound Ollama/OpenAI/Anthropic call.
    Defaults enabled if `HUB_API_CONFIG` is somehow unset (defensive only --
    `app.py::create_app` always sets it; every test fixture that registers
    these blueprints sets it too, see `tests/test_v1_ai_routing_blueprint.py`).
    """
    cfg = current_app.config.get("HUB_API_CONFIG")
    return bool(cfg is None or cfg.ai_enabled)


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    """Convert an `ApiError` into the flask_core `error_response()` JSON envelope."""
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


def _publish_tool_denial(exc: ApiError, user_id: int | None) -> None:
    """Hand a refused model tool call to the audit hook as a denied authz decision.

    `services.audit_http` records every authenticated 403 as `authz.denied`; publishing the
    decision (the same `request.authz_decision` channel `require_scope` uses) adds the closed-
    vocabulary reason code to that tamper-evident record. Nothing the model said -- tool name,
    arguments -- is in it.
    """
    if not isinstance(exc, ToolCallDeniedError):
        return
    request.authz_decision = AuthzDecision(  # type: ignore[attr-defined]
        required_scopes=(),
        allowed=False,
        reason=f"ai_tool_call_denied:{exc.reason}",
        subject=None if user_id is None else str(user_id),
    )


async def _require_admin(community_id: int) -> int:
    """Resolve `(ctx, user_id)` and enforce community-admin membership; returns `user_id`."""
    ctx = get_tenant_context(request)
    if ctx is None:
        raise ApiError("Tenant context not resolved", 403, "FORBIDDEN")
    async_dal, dal = _dal()
    user_id = get_current_user_id(request)
    await require_community_admin(
        async_dal, dal, request, ctx, community_id=community_id, user_id=user_id
    )
    return user_id


async def _require_member(community_id: int) -> int:
    """Resolve `(ctx, user_id)` and enforce active community membership; returns `user_id`."""
    ctx = get_tenant_context(request)
    if ctx is None:
        raise ApiError("Tenant context not resolved", 403, "FORBIDDEN")
    async_dal, dal = _dal()
    user_id = get_current_user_id(request)
    await require_community_member(
        async_dal, dal, request, ctx, community_id=community_id, user_id=user_id
    )
    return user_id


# ---------------------------------------------------------------------------
# DTOs -- flat, snake_case (no pre-existing JS wire contract to match; see
# `hub_api/PORTING.md`'s DTO-casing note, which only mandates camelCase
# where a real frontend contract needs it).
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class AIConfigResponse:
    """`GET`/`PUT` `.../ai/config` response."""

    success: bool
    community_id: int
    preferred_tier: str
    byok_provider: str | None
    on_insufficient_balance: str


@dataclass(slots=True, frozen=True)
class SetAIConfigRequest:
    """`PUT .../ai/config` request body."""

    preferred_tier: str
    byok_provider: str | None = None
    on_insufficient_balance: str = "fallback_free"


@dataclass(slots=True, frozen=True)
class ByokKeyDTO:
    """One masked BYOK key record -- never carries the real key value."""

    provider: str
    key_last4: str
    is_active: bool
    created_at: str | None
    updated_at: str | None
    rotated_at: str | None


@dataclass(slots=True, frozen=True)
class ListByokKeysResponse:
    """`GET .../ai/byok-keys` response."""

    success: bool
    keys: list[ByokKeyDTO] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class SetByokKeyRequest:
    """`PUT .../ai/byok-keys` request body -- set-or-rotate one provider's key."""

    provider: str
    api_key: str


@dataclass(slots=True, frozen=True)
class MessageResponse:
    """Generic `{success, message}` response."""

    success: bool
    message: str


@dataclass(slots=True, frozen=True)
class CompletionRequestDTO:
    """`POST .../ai/completions` request body -- maps 1:1 onto `AIRequest`."""

    prompt: str
    max_tokens: int = 512
    temperature: float = 0.7
    requested_tier: str | None = None
    model_hint: str | None = None
    byok_provider: str | None = None
    invocation: str = "interactive"


@dataclass(slots=True, frozen=True)
class CompletionResponseDTO:
    """`POST .../ai/completions` response -- flat, mirrors `AIResponse` field-for-field."""

    success: bool
    text: str
    provider: str
    model: str
    tier_used: str
    input_tokens: int
    output_tokens: int
    billed_tokens: int
    fallback_reason: str | None


def _config_dto(cfg: config_service.AIConfig) -> AIConfigResponse:
    return AIConfigResponse(
        success=True,
        community_id=cfg.community_id,
        preferred_tier=cfg.preferred_tier,
        byok_provider=cfg.byok_provider,
        on_insufficient_balance=cfg.on_insufficient_balance,
    )


def _key_dto(info: config_service.ByokKeyInfo) -> ByokKeyDTO:
    return ByokKeyDTO(
        provider=info.provider,
        key_last4=info.key_last4,
        is_active=info.is_active,
        created_at=_iso(info.created_at),
        updated_at=_iso(info.updated_at),
        rotated_at=_iso(info.rotated_at),
    )


# ---------------------------------------------------------------------------
# Config + BYOK key management -- community-admin only.
# ---------------------------------------------------------------------------


@ai_config_bp.route("/<int:community_id>/ai/config", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_response(AIConfigResponse)
async def get_config(community_id: int) -> AIConfigResponse | tuple[dict[str, object], int]:
    """`GET /api/v1/admin/<community_id>/ai/config`."""
    if not _ai_enabled():
        return _err(ai_disabled_by_deployment())
    async_dal, dal = _dal()
    try:
        await _require_admin(community_id)
    except ApiError as exc:
        return _err(exc)
    cfg = await config_service.get_ai_config(async_dal, dal, community_id=community_id)
    return _config_dto(cfg)


@ai_config_bp.route("/<int:community_id>/ai/config", methods=["PUT"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_request(SetAIConfigRequest)
@validate_response(AIConfigResponse)
async def set_config(
    data: SetAIConfigRequest, community_id: int
) -> AIConfigResponse | tuple[dict[str, object], int]:
    """`PUT /api/v1/admin/<community_id>/ai/config`."""
    if not _ai_enabled():
        return _err(ai_disabled_by_deployment())
    async_dal, dal = _dal()
    try:
        user_id = await _require_admin(community_id)
        cfg = await config_service.set_ai_config(
            async_dal,
            dal,
            community_id=community_id,
            preferred_tier=data.preferred_tier,
            byok_provider=data.byok_provider,
            on_insufficient_balance=data.on_insufficient_balance,
            updated_by_user_id=user_id,
        )
    except ApiError as exc:
        return _err(exc)
    return _config_dto(cfg)


@ai_config_bp.route("/<int:community_id>/ai/byok-keys", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_response(ListByokKeysResponse)
async def list_byok_keys(community_id: int) -> ListByokKeysResponse | tuple[dict[str, object], int]:
    """`GET /api/v1/admin/<community_id>/ai/byok-keys` -- masked, never the real key."""
    if not _ai_enabled():
        return _err(ai_disabled_by_deployment())
    async_dal, dal = _dal()
    try:
        await _require_admin(community_id)
    except ApiError as exc:
        return _err(exc)
    keys = await config_service.list_byok_keys(async_dal, dal, community_id=community_id)
    return ListByokKeysResponse(success=True, keys=[_key_dto(k) for k in keys])


@ai_config_bp.route("/<int:community_id>/ai/byok-keys", methods=["PUT"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_request(SetByokKeyRequest)
@validate_response(ByokKeyDTO)
async def set_byok_key(
    data: SetByokKeyRequest, community_id: int
) -> ByokKeyDTO | tuple[dict[str, object], int]:
    """`PUT /api/v1/admin/<community_id>/ai/byok-keys` -- validates against the real provider first.

    Never logs or echoes `data.api_key` -- the response is the masked
    `ByokKeyDTO` (`key_last4` only), same object `list_byok_keys()` returns.
    """
    if not _ai_enabled():
        return _err(ai_disabled_by_deployment())
    async_dal, dal = _dal()
    try:
        user_id = await _require_admin(community_id)
        info = await config_service.set_byok_key(
            async_dal,
            dal,
            community_id=community_id,
            provider=data.provider,
            plaintext_key=data.api_key,
            created_by_user_id=user_id,
        )
    except ApiError as exc:
        return _err(exc)
    return _key_dto(info)


@ai_config_bp.route("/<int:community_id>/ai/byok-keys/<provider>", methods=["DELETE"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_response(MessageResponse)
async def delete_byok_key(
    community_id: int, provider: str
) -> MessageResponse | tuple[dict[str, object], int]:
    """`DELETE .../ai/byok-keys/<provider>` -- deactivates, not a hard delete."""
    if not _ai_enabled():
        return _err(ai_disabled_by_deployment())
    async_dal, dal = _dal()
    try:
        await _require_admin(community_id)
        await config_service.delete_byok_key(
            async_dal, dal, community_id=community_id, provider=provider
        )
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message=f"{provider} key deactivated")


# ---------------------------------------------------------------------------
# Completion proxy -- any active community member.
# ---------------------------------------------------------------------------


@ai_completions_bp.route("/<int:community_id>/ai/completions", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_request(CompletionRequestDTO)
@validate_response(CompletionResponseDTO)
async def create_completion(
    data: CompletionRequestDTO, community_id: int
) -> CompletionResponseDTO | tuple[dict[str, object], int]:
    """`POST .../ai/completions` -- routes through `route_completion()`."""
    if not _ai_enabled():
        return _err(ai_disabled_by_deployment())
    async_dal, dal = _dal()
    user_id: int | None = None
    try:
        user_id = await _require_member(community_id)
        ctx = get_tenant_context(request)
        if ctx is None:  # pragma: no cover -- `_require_member` already raises if this is None
            raise ApiError("Tenant context not resolved", 403, "FORBIDDEN")

        if data.requested_tier not in (None, "free", "premium", "byok"):
            raise ApiError("invalid requested_tier", 400, "BAD_REQUEST")
        if data.byok_provider not in (None, "openai", "anthropic"):
            raise ApiError("invalid byok_provider", 400, "BAD_REQUEST")
        if data.invocation not in ("interactive", "ambient"):
            raise ApiError("invalid invocation", 400, "BAD_REQUEST")

        ai_request = AIRequest(
            prompt=data.prompt,
            max_tokens=data.max_tokens,
            temperature=data.temperature,
            requested_tier=data.requested_tier,  # type: ignore[arg-type]
            model_hint=data.model_hint,
            byok_provider=data.byok_provider,  # type: ignore[arg-type]
            invocation=data.invocation,  # type: ignore[arg-type]
        )
        idempotency_key = request.headers.get("X-Idempotency-Key") or str(uuid4())
        response = await route_completion(
            async_dal,
            dal,
            tenant=ctx.tenant_slug,
            community_id=community_id,
            actor_user_id=user_id,
            ai_request=ai_request,
            idempotency_key=idempotency_key,
            # Belt-and-suspenders: this handler already 503'd above when
            # disabled, so this is always True by the time we get here --
            # threaded through anyway so `route_completion()`'s own guard
            # is exercised on every real call path, not just its direct
            # unit tests (`test_ai_routing_router.py`).
            ai_enabled=_ai_enabled(),
            # The invoking user's verified scopes -- the only authority a model-requested tool
            # call is re-checked against (tenant comes from `ctx`, never from the request body).
            granted_scopes=get_current_scopes(request),
        )
    except ApiError as exc:
        _publish_tool_denial(exc, user_id)
        return _err(exc)

    return CompletionResponseDTO(
        success=True,
        text=response.text,
        provider=response.provider,
        model=response.model,
        tier_used=response.tier_used,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        billed_tokens=response.billed_tokens,
        fallback_reason=response.fallback_reason,
    )


BLUEPRINTS: list[Blueprint] = [ai_config_bp, ai_completions_bp]
