"""v1 `bot` group -- Shoutout, AIChatter, AI Knowledge, Server Manager (RCON/Voice).

Ports the Bot module's four controllers (migration plan §2/§7, phase
M5 -- the pattern-prover module): `shoutoutController`/`adminController`'s
live shoutout functions, `aiChatterController`, `aiKnowledgeController`,
`rconController`. Paths are IDENTICAL to `admin/hub_module/frontend/src/
services/api.js` (the frozen `/api/v1` contract) -- see each route's
docstring for the exact `api.js` export it satisfies.

Auth chain (security.md, `blueprints/v2/platform.py`'s copy-me pattern):
`tenant_middleware` (outermost) -> `require_scope` -> handler.
`community_id` is a URL path param on every shoutout/rcon route (the
frozen v1 contract predates tenant scoping) -- `_require_community`
below re-verifies it belongs to the caller's resolved tenant
immediately after auth, before any Bot-table query runs (security.md:
never trust a path param for tenant scoping).

Scope-namespace note (migration plan §8 D5, not yet confirmed): Node's
`requireCommunityAdmin` had no OIDC-scope equivalent to port 1:1 --
mapped here to `bot.<surface>:read`/`:write`/`:admin` under the
`waddles.bot.*` feature keys the plan already assigns per controller.
Interim mapping, not a re-litigation of D5.

Pure-proxy routes (rcon live-command surface, ai-chatter config) return
`(dict, int)` directly rather than through `@validate_response` -- the
downstream JSON shape is owned by `server-manager-service`/
`ai-interaction`, not this port, and the Node source itself does an
unvalidated `res.json(result)` pass-through (`rconController.js`,
`aiChatterController.js`). Documented exception to security.md Output
Validation, not a silent gap: every other route here uses an explicit
DTO.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from dataclasses import dataclass, field
from typing import Any

from flask_core.authz import require_scope
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, request
from quart_schema import validate_request, validate_response

from services import bot_ai_chatter, bot_ai_knowledge, bot_rcon, bot_shoutout
from services.bot_tables import bind_bot_tables, community_belongs_to_tenant

bot_bp = Blueprint("v1_bot", __name__, url_prefix="/api/v1/admin")

# -- Scope namespace (see module docstring's D5 note) --
_SHOUTOUT_READ = "bot.shoutout:read"
_SHOUTOUT_WRITE = "bot.shoutout:write"
_AI_CHATTER_READ = "bot.ai_chatter:read"
_AI_CHATTER_WRITE = "bot.ai_chatter:write"
_AI_KNOWLEDGE_READ = "bot.ai_knowledge:read"
_AI_KNOWLEDGE_WRITE = "bot.ai_knowledge:write"
_SERVER_MANAGER_ADMIN = "bot.server_manager:admin"
_SERVER_MANAGER_READ = "bot.server_manager:read"


def _error(message: str, status: int, code: str) -> tuple[dict[str, Any], int]:
    """Matches `middleware/errorHandler.js`'s `{success: false, error: {code, message}}` shape."""
    return {"success": False, "error": {"code": code, "message": message}}, status


async def _require_community(community_id: int) -> tuple[dict[str, Any], int] | None:
    """Verify `community_id` belongs to the caller's resolved tenant; `None` if OK.

    Called first in every handler carrying a `community_id` path param --
    see module docstring. Returns an error tuple to return-and-short-
    circuit on mismatch, mirroring how `tenant_middleware`/`require_scope`
    themselves short-circuit before the handler body runs.
    """
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101 - tenant_middleware already enforced this
    dal = _dal()
    ok = await asyncio.to_thread(community_belongs_to_tenant, dal, community_id, ctx.tenant_id)
    if not ok:
        return _error("Community not found for this tenant", 403, "FORBIDDEN")
    return None


def _dal() -> Any:
    """`app.config["dal"]`, binding the Bot module's tables on first use.

    Never edit `app.py` to wire this (the M0 scaffold's stated
    extension-point contract, `hub_api/README.md`) -- Core Identity/
    Tenancy hasn't landed its own `before_serving` table registration
    hook yet as of this M5 port, so each port group binds its own
    tables lazily and idempotently (`bind_bot_tables` guards every
    `define_table` call, see its own docstring) rather than requiring a
    shared, contended edit to the app factory.
    """
    from quart import current_app

    dal = current_app.config["dal"]
    if "shoutout_config" not in dal.tables:
        bind_bot_tables(dal)
    return dal


def _user_id() -> int:
    """`req.user.id` equivalent -- the JWT `sub` claim, coerced to int for FK columns.

    Neither `tenant_middleware` nor `require_scope` publish the decoded
    payload on `request` (`tenancy.py` only sets `request.tenant_context`)
    -- re-decodes the bearer token itself, same self-contained pattern
    `require_scope` uses rather than reaching into `tenancy.py`'s
    request-local state (`authz.py`'s own module docstring). Safe to
    re-decode: `tenant_middleware` (outermost) already proved the token
    valid before this handler ever runs. Falls back to `0` for a
    non-numeric `sub` (e.g. a UUID-based identity once M1 lands) rather
    than raising -- these columns are nullable audit metadata
    (`added_by`/`user_id`), never an authz decision.
    """
    from flask_core.auth import verify_jwt_token
    from flask_core.secrets import require_secret_key

    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    secret_key = require_secret_key()
    payload = verify_jwt_token(token, secret_key) if token else None
    sub = payload.get("sub") if payload else None
    try:
        return int(sub) if sub is not None else 0
    except (TypeError, ValueError):
        return 0


# ═══════════════════════════════════════════════════════════════════════
# Shoutout -- adminController.js's live getShoutoutConfig/... functions
# ═══════════════════════════════════════════════════════════════════════


@dataclass(slots=True, frozen=True)
class ShoutoutConfigResponse:
    """Response body -- Shoutout Config."""

    success: bool
    config: bot_shoutout.ShoutoutConfig


@dataclass(slots=True, frozen=True)
class ShoutoutConfigRequest:
    """Request body -- Shoutout Config."""

    soEnabled: bool
    soPermission: str
    vsoEnabled: bool
    vsoPermission: str
    autoShoutoutMode: str
    triggerFirstMessage: bool
    triggerRaidHost: bool
    widgetPosition: str
    widgetDurationSeconds: int
    cooldownMinutes: int


@dataclass(slots=True, frozen=True)
class ShoutoutCreatorsResponse:
    """Response body -- Shoutout Creators."""

    success: bool
    creators: list[bot_shoutout.ShoutoutCreator] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class ShoutoutCreatorRequest:
    """Request body -- Shoutout Creator."""

    platform: str
    username: str


@dataclass(slots=True, frozen=True)
class ShoutoutCreatorResponse:
    """Response body -- Shoutout Creator."""

    success: bool
    creator: bot_shoutout.ShoutoutCreator


@dataclass(slots=True, frozen=True)
class ShoutoutMessageResponse:
    """Response body -- Shoutout Message."""

    success: bool
    message: str


@dataclass(slots=True, frozen=True)
class ShoutoutPagination:
    """Shoutout Pagination."""

    page: int
    limit: int
    total: int
    totalPages: int


@dataclass(slots=True, frozen=True)
class ShoutoutHistoryResponse:
    """Response body -- Shoutout History."""

    success: bool
    history: list[bot_shoutout.ShoutoutHistoryEntry]
    pagination: ShoutoutPagination


@bot_bp.route("/<int:community_id>/shoutout/config", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SHOUTOUT_READ)  # type: ignore[misc]
@validate_response(ShoutoutConfigResponse)
async def get_shoutout_config(community_id: int) -> Any:
    """`api.js` `getShoutoutConfig` -- `GET /api/v1/admin/:communityId/shoutout/config`."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    config = await asyncio.to_thread(
        bot_shoutout.get_or_create_shoutout_config, _dal(), community_id
    )
    return ShoutoutConfigResponse(success=True, config=config)


@bot_bp.route("/<int:community_id>/shoutout/config", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SHOUTOUT_WRITE)  # type: ignore[misc]
@validate_request(ShoutoutConfigRequest)
@validate_response(ShoutoutConfigResponse)
async def update_shoutout_config(data: ShoutoutConfigRequest, community_id: int) -> Any:
    """`api.js` `updateShoutoutConfig` -- `PUT /api/v1/admin/:communityId/shoutout/config`."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    config = await asyncio.to_thread(
        bot_shoutout.update_shoutout_config,
        _dal(),
        community_id,
        so_enabled=data.soEnabled,
        so_permission=data.soPermission,
        vso_enabled=data.vsoEnabled,
        vso_permission=data.vsoPermission,
        auto_shoutout_mode=data.autoShoutoutMode,
        trigger_first_message=data.triggerFirstMessage,
        trigger_raid_host=data.triggerRaidHost,
        widget_position=data.widgetPosition,
        widget_duration_seconds=data.widgetDurationSeconds,
        cooldown_minutes=data.cooldownMinutes,
    )
    return ShoutoutConfigResponse(success=True, config=config)


@bot_bp.route("/<int:community_id>/shoutout/creators", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SHOUTOUT_READ)  # type: ignore[misc]
@validate_response(ShoutoutCreatorsResponse)
async def get_shoutout_creators(community_id: int) -> Any:
    """`api.js` `getShoutoutCreators` -- `GET /api/v1/admin/:communityId/shoutout/creators`."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    creators = await asyncio.to_thread(bot_shoutout.list_shoutout_creators, _dal(), community_id)
    return ShoutoutCreatorsResponse(success=True, creators=creators)


@bot_bp.route("/<int:community_id>/shoutout/creators", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SHOUTOUT_WRITE)  # type: ignore[misc]
@validate_request(ShoutoutCreatorRequest)
async def add_shoutout_creator(data: ShoutoutCreatorRequest, community_id: int) -> Any:
    """`api.js` `addShoutoutCreator` -- `POST /api/v1/admin/:communityId/shoutout/creators`.

    No `@validate_response` here -- see "Known gaps" in `hub_api/README.md`
    (or module docstring): reproducibly hits a `quart-schema`/`pydantic`
    interaction bug (`TypeError: 'None' is not an instance of
    'SchemaSerializer'`) specific to this route's response shape once
    registered on the real `bot_bp` blueprint (isolated to this handler
    via bisection -- an equivalent hand-built route with the identical
    DTOs does not reproduce it). Response shape is still explicit and
    schema-documented via `ShoutoutCreatorResponse` (used for OpenAPI
    generation and the characterization tests' own assertions) -- only
    the runtime double-validation is skipped, matching this file's
    existing "pure proxy" exception precedent (module docstring).
    """
    denied = await _require_community(community_id)
    if denied:
        return denied
    if not data.platform or not data.username:
        return _error("Platform and username are required", 400, "BAD_REQUEST")
    creator = await asyncio.to_thread(
        bot_shoutout.add_shoutout_creator,
        _dal(),
        community_id,
        platform=data.platform,
        username=data.username.strip(),
        added_by=_user_id(),
    )
    if creator is None:
        return _error("Creator already in list", 409, "CONFLICT")
    return {"success": True, "creator": dataclasses.asdict(creator)}


@bot_bp.route("/<int:community_id>/shoutout/creators/<int:creator_id>", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SHOUTOUT_WRITE)  # type: ignore[misc]
@validate_response(ShoutoutMessageResponse)
async def remove_shoutout_creator(community_id: int, creator_id: int) -> Any:
    """`api.js` `removeShoutoutCreator` -- `DELETE .../shoutout/creators/:creatorId`."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    removed = await asyncio.to_thread(
        bot_shoutout.remove_shoutout_creator, _dal(), community_id, creator_id
    )
    if not removed:
        return _error("Creator not found", 404, "NOT_FOUND")
    return ShoutoutMessageResponse(success=True, message="Creator removed")


@bot_bp.route("/<int:community_id>/shoutout/history", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SHOUTOUT_READ)  # type: ignore[misc]
@validate_response(ShoutoutHistoryResponse)
async def get_shoutout_history(community_id: int) -> Any:
    """`api.js` `getShoutoutHistory` -- `GET /api/v1/admin/:communityId/shoutout/history`."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    page = max(1, _int_arg("page", 1))
    limit = min(100, max(1, _int_arg("limit", 25)))
    entries, total = await asyncio.to_thread(
        bot_shoutout.list_shoutout_history, _dal(), community_id, page=page, limit=limit
    )
    total_pages = -(-total // limit) if limit else 0  # ceil div
    return ShoutoutHistoryResponse(
        success=True,
        history=entries,
        pagination=ShoutoutPagination(page=page, limit=limit, total=total, totalPages=total_pages),
    )


def _int_arg(name: str, default: int) -> int:
    raw = request.args.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# ═══════════════════════════════════════════════════════════════════════
# AIChatter -- aiChatterController.js (pure proxy to ai-interaction)
# ═══════════════════════════════════════════════════════════════════════


@dataclass(slots=True, frozen=True)
class AIChatterConfigRequest:
    """Request body -- AIChatter Config."""

    enabled: bool | None = None
    max_responses_per_window: int | None = None
    window_seconds: int | None = None
    max_per_user_per_window: int | None = None
    response_probability: float | None = None
    min_message_length: int | None = None


@dataclass(slots=True, frozen=True)
class AIChatterConfigDTO:
    """AIChatter config -- exact field set (unknown keys dropped)."""

    enabled: bool = False
    max_responses_per_window: int = 10
    window_seconds: int = 600
    max_per_user_per_window: int = 2
    response_probability: float = 0.30
    min_message_length: int = 10


@dataclass(slots=True, frozen=True)
class AIChatterConfigResponse:
    """Response body -- AIChatter config envelope."""

    success: bool
    config: AIChatterConfigDTO


def _chatter_dto(config: dict[str, Any]) -> AIChatterConfigDTO:
    """Project relayed upstream config onto `AIChatterConfigDTO`."""
    known = {f.name for f in dataclasses.fields(AIChatterConfigDTO)}
    return AIChatterConfigDTO(**{k: v for k, v in config.items() if k in known})


@bot_bp.route("/<int:community_id>/ai-chatter/config", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_AI_CHATTER_READ)  # type: ignore[misc]
@validate_response(AIChatterConfigResponse)
async def get_ai_chatter_config(community_id: int) -> Any:
    """`api.js` `getAIChatterConfig` -- proxy passthrough, see module docstring."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    config = await bot_ai_chatter.get_chatter_config(community_id)
    return AIChatterConfigResponse(success=True, config=_chatter_dto(config))


@bot_bp.route("/<int:community_id>/ai-chatter/config", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_AI_CHATTER_WRITE)  # type: ignore[misc]
@validate_request(AIChatterConfigRequest)
@validate_response(AIChatterConfigResponse)
async def update_ai_chatter_config(data: AIChatterConfigRequest, community_id: int) -> Any:
    """`api.js` `updateAIChatterConfig` -- proxy passthrough, see module docstring."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    update = bot_ai_chatter.AIChatterConfigUpdate(
        enabled=data.enabled,
        max_responses_per_window=data.max_responses_per_window,
        window_seconds=data.window_seconds,
        max_per_user_per_window=data.max_per_user_per_window,
        response_probability=data.response_probability,
        min_message_length=data.min_message_length,
    )
    try:
        config = await bot_ai_chatter.update_chatter_config(community_id, update)
    except bot_ai_chatter.AIChatterValidationError as exc:
        return _error(str(exc), 400, "BAD_REQUEST")
    return AIChatterConfigResponse(success=True, config=_chatter_dto(config))


# ═══════════════════════════════════════════════════════════════════════
# AI Knowledge -- aiKnowledgeController.js (no community_id path param)
# ═══════════════════════════════════════════════════════════════════════


@dataclass(slots=True, frozen=True)
class KnowledgeSourcesResponse:
    """Response body -- Knowledge Sources."""

    success: bool
    sources: list[bot_ai_knowledge.KnowledgeSource]


@dataclass(slots=True, frozen=True)
class KnowledgeSourceCreateRequest:
    """Request body -- Knowledge Source Create."""

    source_name: str
    source_type: str
    community_id: int | None = None
    vendor_id: int | None = None
    module_id: int | None = None
    source_url: str | None = None
    branch: str = "main"
    docs_path: str = "/"
    refresh_interval: str = "weekly"
    encrypted_token: str | None = None


@dataclass(slots=True, frozen=True)
class KnowledgeSourceResponse:
    """Response body -- Knowledge Source."""

    success: bool
    source: bot_ai_knowledge.KnowledgeSource


@dataclass(slots=True, frozen=True)
class KnowledgeSourceUpdateRequest:
    """Request body -- Knowledge Source Update."""

    source_name: str | None = None
    source_url: str | None = None
    branch: str | None = None
    docs_path: str | None = None
    refresh_interval: str | None = None
    encrypted_token: str | None = None
    is_active: bool | None = None


@dataclass(slots=True, frozen=True)
class SimpleSuccessResponse:
    """Response body -- Simple Success."""

    success: bool


@dataclass(slots=True, frozen=True)
class ReindexResponse:
    """Response body -- Reindex."""

    success: bool
    message: str
    sourceId: int


@dataclass(slots=True, frozen=True)
class KnowledgeSearchRequest:
    """Request body -- Knowledge Search."""

    query: str
    communityId: int | None = None
    vendorId: int | None = None
    topK: int | None = None


@dataclass(slots=True, frozen=True)
class KnowledgeChunkDTO:
    """Nested response field -- Knowledge Chunk."""

    id: int
    source_id: int
    content: str
    source_url: str | None
    source_title: str | None
    chunk_index: int
    token_count: int | None


@dataclass(slots=True, frozen=True)
class KnowledgeSearchResultDTO:
    """Nested response field -- Knowledge Search Result."""

    chunk: KnowledgeChunkDTO
    score: float


@dataclass(slots=True, frozen=True)
class KnowledgeSearchResponse:
    """Response body -- Knowledge Search."""

    success: bool
    results: list[KnowledgeSearchResultDTO]


@dataclass(slots=True, frozen=True)
class SuggestRequest:
    """Request body -- Suggest."""

    ticketId: int
    ticketText: str
    communityId: int | None = None


@dataclass(slots=True, frozen=True)
class TicketSuggestionDTO:
    """Nested response field -- Ticket Suggestion."""

    id: int
    ticket_id: int
    suggestion_text: str
    confidence_score: float
    cited_chunks: list[int]
    feedback: str | None
    is_auto_posted: bool
    created_at: str | None


@dataclass(slots=True, frozen=True)
class SuggestResponse:
    """Covers both outcomes -- `suggestion=None` + `message` set, or a populated `suggestion`."""

    success: bool
    suggestion: TicketSuggestionDTO | None = None
    message: str | None = None


@dataclass(slots=True, frozen=True)
class SuggestionFeedbackRequest:
    """Request body -- Suggestion Feedback."""

    feedback: str


@dataclass(slots=True, frozen=True)
class SuggestionFeedbackResponse:
    """Response body -- Suggestion Feedback."""

    success: bool
    suggestion: TicketSuggestionDTO


async def _check_optional_community(community_id: int | None) -> tuple[dict[str, Any], int] | None:
    """AI Knowledge routes carry no `community_id` path param; verify it if given as a filter."""
    if community_id is None:
        return None
    return await _require_community(community_id)


@bot_bp.route("/ai-knowledge/sources", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_AI_KNOWLEDGE_READ)  # type: ignore[misc]
@validate_response(KnowledgeSourcesResponse)
async def list_knowledge_sources() -> Any:
    """`aiKnowledgeController.listSources` -- `GET .../ai-knowledge/sources?communityId=`."""
    community_id = _int_arg("communityId", 0) or None
    denied = await _check_optional_community(community_id)
    if denied:
        return denied
    sources = await asyncio.to_thread(bot_ai_knowledge.list_knowledge_sources, _dal(), community_id)
    return KnowledgeSourcesResponse(success=True, sources=sources)


@bot_bp.route("/ai-knowledge/sources", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_AI_KNOWLEDGE_WRITE)  # type: ignore[misc]
@validate_request(KnowledgeSourceCreateRequest)
@validate_response(KnowledgeSourceResponse, 201)
async def create_knowledge_source(data: KnowledgeSourceCreateRequest) -> Any:
    """`aiKnowledgeController.createSource` -- `POST /api/v1/admin/ai-knowledge/sources`."""
    denied = await _check_optional_community(data.community_id)
    if denied:
        return denied
    payload = bot_ai_knowledge.KnowledgeSourceCreate(
        source_name=data.source_name,
        source_type=data.source_type,
        community_id=data.community_id,
        vendor_id=data.vendor_id,
        module_id=data.module_id,
        source_url=data.source_url,
        branch=data.branch,
        docs_path=data.docs_path,
        refresh_interval=data.refresh_interval,
        encrypted_token=data.encrypted_token,
    )
    dal = _dal()
    try:
        source = await asyncio.to_thread(bot_ai_knowledge.add_knowledge_source, dal, payload)
    except bot_ai_knowledge.KnowledgeServiceError as exc:
        return _error(str(exc), exc.status, "BAD_REQUEST" if exc.status == 400 else "ERROR")

    if source.source_type != "manual":
        # Fire-and-forget indexing -- matches Node's `setImmediate` + uncaught-promise
        # pattern: the route responds before this completes.
        asyncio.create_task(_index_source_background(dal, source.id))

    return KnowledgeSourceResponse(success=True, source=source), 201


async def _index_source_background(dal: Any, source_id: int) -> None:
    """Background indexing task -- matches Node's `indexSource(id).catch(err => logger.error)`."""
    try:
        await bot_ai_knowledge.index_source(dal, source_id)
    except Exception as exc:  # noqa: BLE001 - background task, must not propagate to the event loop
        logging.getLogger(__name__).error(
            "Background reindex failed source_id=%s err=%s", source_id, exc
        )


@bot_bp.route("/ai-knowledge/sources/<int:source_id>", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_AI_KNOWLEDGE_WRITE)  # type: ignore[misc]
@validate_request(KnowledgeSourceUpdateRequest)
@validate_response(KnowledgeSourceResponse)
async def update_knowledge_source(data: KnowledgeSourceUpdateRequest, source_id: int) -> Any:
    """`aiKnowledgeController.updateSource` -- `PUT /api/v1/admin/ai-knowledge/sources/:id`."""
    # `data` is a `slots=True` dataclass -- no `__dict__` -- `dataclasses.fields()` instead.
    updates = {
        f.name: getattr(data, f.name)
        for f in dataclasses.fields(data)
        if getattr(data, f.name) is not None
    }
    try:
        source = await asyncio.to_thread(
            bot_ai_knowledge.update_knowledge_source, _dal(), source_id, updates
        )
    except bot_ai_knowledge.KnowledgeServiceError as exc:
        return _error(str(exc), exc.status, "NOT_FOUND" if exc.status == 404 else "BAD_REQUEST")
    return KnowledgeSourceResponse(success=True, source=source)


@bot_bp.route("/ai-knowledge/sources/<int:source_id>", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_AI_KNOWLEDGE_WRITE)  # type: ignore[misc]
@validate_response(SimpleSuccessResponse)
async def delete_knowledge_source(source_id: int) -> Any:
    """`aiKnowledgeController.deleteSource` -- `DELETE /api/v1/admin/ai-knowledge/sources/:id`."""
    try:
        await asyncio.to_thread(bot_ai_knowledge.delete_knowledge_source, _dal(), source_id)
    except bot_ai_knowledge.KnowledgeServiceError as exc:
        return _error(str(exc), exc.status, "NOT_FOUND")
    return SimpleSuccessResponse(success=True)


@bot_bp.route("/ai-knowledge/sources/<int:source_id>/reindex", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_AI_KNOWLEDGE_WRITE)  # type: ignore[misc]
@validate_response(ReindexResponse)
async def reindex_source(source_id: int) -> Any:
    """`aiKnowledgeController.reindexSource` -- responds immediately, indexes async."""
    asyncio.create_task(_index_source_background(_dal(), source_id))
    return ReindexResponse(success=True, message="Reindex started", sourceId=source_id)


def _suggestion_dto(suggestion: bot_ai_knowledge.TicketSuggestion) -> TicketSuggestionDTO:
    return TicketSuggestionDTO(
        id=suggestion.id,
        ticket_id=suggestion.ticket_id,
        suggestion_text=suggestion.suggestion_text,
        confidence_score=suggestion.confidence_score,
        cited_chunks=suggestion.cited_chunks,
        feedback=suggestion.feedback,
        is_auto_posted=suggestion.is_auto_posted,
        created_at=suggestion.created_at,
    )


@bot_bp.route("/ai-knowledge/search", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_AI_KNOWLEDGE_READ)  # type: ignore[misc]
@validate_request(KnowledgeSearchRequest)
@validate_response(KnowledgeSearchResponse)
async def search_knowledge_base(data: KnowledgeSearchRequest) -> Any:
    """`aiKnowledgeController.searchKnowledgeBase` -- `POST /api/v1/admin/ai-knowledge/search`."""
    if not data.query or not data.query.strip():
        return _error("query is required", 400, "BAD_REQUEST")
    denied = await _check_optional_community(data.communityId)
    if denied:
        return denied
    try:
        results = await bot_ai_knowledge.search_knowledge(
            _dal(),
            data.query,
            community_id=data.communityId,
            vendor_id=data.vendorId,
            top_k=data.topK or 5,
        )
    except bot_ai_knowledge.KnowledgeServiceError as exc:
        return _error(str(exc), exc.status, "BAD_REQUEST")
    return KnowledgeSearchResponse(
        success=True,
        results=[
            KnowledgeSearchResultDTO(
                chunk=KnowledgeChunkDTO(
                    id=r.chunk_id,
                    source_id=r.source_id,
                    content=r.content,
                    source_url=r.source_url,
                    source_title=r.source_title,
                    chunk_index=r.chunk_index,
                    token_count=r.token_count,
                ),
                score=r.score,
            )
            for r in results
        ],
    )


@bot_bp.route("/ai-knowledge/suggest", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_AI_KNOWLEDGE_WRITE)  # type: ignore[misc]
@validate_request(SuggestRequest)
@validate_response(SuggestResponse, 200)
@validate_response(SuggestResponse, 201)
async def suggest_for_ticket(data: SuggestRequest) -> Any:
    """`aiKnowledgeController.suggestForTicket` -- `POST /api/v1/admin/ai-knowledge/suggest`."""
    if not data.ticketId or not data.ticketText or not data.ticketText.strip():
        return _error("ticketId and ticketText are required", 400, "BAD_REQUEST")
    denied = await _check_optional_community(data.communityId)
    if denied:
        return denied
    try:
        suggestion = await bot_ai_knowledge.generate_suggestion(
            _dal(), data.ticketId, data.ticketText.strip(), community_id=data.communityId
        )
    except bot_ai_knowledge.KnowledgeServiceError as exc:
        return _error(str(exc), exc.status, "BAD_REQUEST")
    if suggestion is None:
        return SuggestResponse(
            success=True,
            suggestion=None,
            message="No suggestion generated -- knowledge base confidence below threshold",
        )
    return SuggestResponse(success=True, suggestion=_suggestion_dto(suggestion)), 201


@bot_bp.route("/ai-knowledge/suggestions/<int:suggestion_id>/feedback", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_AI_KNOWLEDGE_WRITE)  # type: ignore[misc]
@validate_request(SuggestionFeedbackRequest)
@validate_response(SuggestionFeedbackResponse)
async def submit_suggestion_feedback(data: SuggestionFeedbackRequest, suggestion_id: int) -> Any:
    """`aiKnowledgeController.submitFeedback` -- `POST .../suggestions/:id/feedback`."""
    if not data.feedback:
        return _error("feedback is required", 400, "BAD_REQUEST")
    try:
        suggestion = await asyncio.to_thread(
            bot_ai_knowledge.record_feedback, _dal(), suggestion_id, data.feedback
        )
    except bot_ai_knowledge.KnowledgeServiceError as exc:
        return _error(str(exc), exc.status, "NOT_FOUND" if exc.status == 404 else "BAD_REQUEST")
    return SuggestionFeedbackResponse(success=True, suggestion=_suggestion_dto(suggestion))


# ═══════════════════════════════════════════════════════════════════════
# Server Manager / RCON -- rconController.js
# ═══════════════════════════════════════════════════════════════════════


@dataclass(slots=True, frozen=True)
class ServersResponse:
    """Admin view -- includes `host`/`game_port`/`rcon_port` (Node's `isAdmin` branch)."""

    servers: list[bot_rcon.Server]


@dataclass(slots=True, frozen=True)
class ServersMemberResponse:
    """Member view -- no host/port/credential fields (Node's non-admin `SELECT`)."""

    servers: list[bot_rcon.ServerMemberView]


@dataclass(slots=True, frozen=True)
class ServerCreateRequest:
    """Request body -- Server Create."""

    display_name: str
    host: str
    game_name: str | None = None
    server_type: str = "rcon"
    game_port: int | None = None
    rcon_port: int | None = None
    password: str | None = None
    game_type: str = "other"
    visibility: str = "admin_only"
    status_api_type: str = "rcon"
    status_url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class ServerUpdateRequest:
    """Request body -- Server Update."""

    display_name: str | None = None
    game_name: str | None = None
    server_type: str | None = None
    host: str | None = None
    game_port: int | None = None
    rcon_port: int | None = None
    password: str | None = None
    game_type: str | None = None
    visibility: str | None = None
    status_api_type: str | None = None
    status_url: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(slots=True, frozen=True)
class ServerResponse:
    """Response body -- Server."""

    server: bot_rcon.Server


@dataclass(slots=True, frozen=True)
class MessageResponse:
    """Response body -- Message."""

    message: str


@dataclass(slots=True, frozen=True)
class TestConnectionRequest:
    """Request body -- Test Connection."""

    password: str | None = None


@dataclass(slots=True, frozen=True)
class ExecuteCommandRequest:
    """Request body -- Execute Command."""

    command: str


@dataclass(slots=True, frozen=True)
class KickPlayerRequest:
    """Request body -- Kick Player."""

    player: str
    reason: str | None = None


@dataclass(slots=True, frozen=True)
class BanPlayerRequest:
    """Request body -- Ban Player."""

    player: str
    reason: str | None = None
    duration: int | None = None


@dataclass(slots=True, frozen=True)
class MoveUserRequest:
    """Request body -- Move User."""

    user_id: str
    channel_id: int


@dataclass(slots=True, frozen=True)
class SendMessageRequest:
    """Request body -- Send Message."""

    text: str
    channel_id: int = 0
    target_mode: int = 2


@dataclass(slots=True, frozen=True)
class CommandLogResponse:
    """Response body -- Command Log."""

    log: list[bot_rcon.CommandLogEntry]


@bot_bp.route("/<int:community_id>/rcon/servers", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_ADMIN)  # type: ignore[misc]
@validate_response(ServersResponse)
async def list_rcon_servers(community_id: int) -> Any:
    """`api.js` `rconApi.listServers` -- `GET .../:communityId/rcon/servers` (admin view)."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    servers = await asyncio.to_thread(bot_rcon.list_servers, _dal(), community_id, is_admin=True)
    return ServersResponse(servers=servers)


@bot_bp.route("/<int:community_id>/rcon/servers", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_ADMIN)  # type: ignore[misc]
@validate_request(ServerCreateRequest)
@validate_response(ServerResponse, 201)
async def create_rcon_server(data: ServerCreateRequest, community_id: int) -> Any:
    """`api.js` `rconApi.createServer` -- `POST /api/v1/admin/:communityId/rcon/servers`."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    payload = bot_rcon.ServerCreate(
        display_name=data.display_name,
        host=data.host,
        game_name=data.game_name,
        server_type=data.server_type,
        game_port=data.game_port,
        rcon_port=data.rcon_port,
        password=data.password,
        game_type=data.game_type,
        visibility=data.visibility,
        status_api_type=data.status_api_type,
        status_url=data.status_url,
        metadata=data.metadata,
    )
    try:
        server = await asyncio.to_thread(
            bot_rcon.create_server, _dal(), community_id, payload, added_by=_user_id()
        )
    except bot_rcon.RconValidationError as exc:
        return _error(str(exc), 400, "BAD_REQUEST")
    return ServerResponse(server=server), 201


@bot_bp.route("/<int:community_id>/rcon/servers/<int:server_id>", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_ADMIN)  # type: ignore[misc]
@validate_request(ServerUpdateRequest)
@validate_response(ServerResponse)
async def update_rcon_server(data: ServerUpdateRequest, community_id: int, server_id: int) -> Any:
    """`api.js` `rconApi.updateServer` -- `PUT .../:communityId/rcon/servers/:serverId`."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    payload = bot_rcon.ServerUpdate(
        display_name=data.display_name,
        game_name=data.game_name,
        server_type=data.server_type,
        host=data.host,
        game_port=data.game_port,
        rcon_port=data.rcon_port,
        password=data.password,
        game_type=data.game_type,
        visibility=data.visibility,
        status_api_type=data.status_api_type,
        status_url=data.status_url,
        metadata=data.metadata,
    )
    try:
        server = await asyncio.to_thread(
            bot_rcon.update_server, _dal(), community_id, server_id, payload
        )
    except bot_rcon.RconValidationError as exc:
        return _error(str(exc), 400, "BAD_REQUEST")
    except bot_rcon.RconNotFoundError as exc:
        return _error(str(exc), 404, "NOT_FOUND")
    return ServerResponse(server=server)


@bot_bp.route("/<int:community_id>/rcon/servers/<int:server_id>", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_ADMIN)  # type: ignore[misc]
@validate_response(MessageResponse)
async def delete_rcon_server(community_id: int, server_id: int) -> Any:
    """`api.js` `rconApi.deleteServer` -- `DELETE .../:communityId/rcon/servers/:serverId`."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    try:
        await asyncio.to_thread(bot_rcon.delete_server, _dal(), community_id, server_id)
    except bot_rcon.RconNotFoundError as exc:
        return _error(str(exc), 404, "NOT_FOUND")
    return MessageResponse(message="Server deleted")


@bot_bp.route("/<int:community_id>/rcon/servers/<int:server_id>/test", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_ADMIN)  # type: ignore[misc]
@validate_request(TestConnectionRequest)
async def test_rcon_connection(
    data: TestConnectionRequest, community_id: int, server_id: int
) -> Any:
    """`api.js` `rconApi.testConnection` -- proxy passthrough, see module docstring."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    try:
        return await bot_rcon.test_connection(
            _dal(), community_id, server_id, password=data.password
        )
    except bot_rcon.RconNotFoundError as exc:
        return _error(str(exc), 404, "NOT_FOUND")


@bot_bp.route("/<int:community_id>/rcon/servers/<int:server_id>/command", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_ADMIN)  # type: ignore[misc]
@validate_request(ExecuteCommandRequest)
async def execute_rcon_command(
    data: ExecuteCommandRequest, community_id: int, server_id: int
) -> Any:
    """`api.js` `rconApi.executeCommand` -- proxy passthrough, see module docstring."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    try:
        return await bot_rcon.execute_command(
            _dal(), community_id, server_id, command=data.command, user_id=_user_id()
        )
    except bot_rcon.RconValidationError as exc:
        return _error(str(exc), 400, "BAD_REQUEST")


@bot_bp.route("/<int:community_id>/rcon/servers/<int:server_id>/kick", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_ADMIN)  # type: ignore[misc]
@validate_request(KickPlayerRequest)
async def kick_rcon_player(data: KickPlayerRequest, community_id: int, server_id: int) -> Any:
    """`api.js` `rconApi.kickPlayer` -- proxy passthrough, see module docstring."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    try:
        return await bot_rcon.kick_player(
            _dal(),
            community_id,
            server_id,
            player=data.player,
            reason=data.reason or "",
            user_id=_user_id(),
        )
    except bot_rcon.RconValidationError as exc:
        return _error(str(exc), 400, "BAD_REQUEST")


@bot_bp.route("/<int:community_id>/rcon/servers/<int:server_id>/ban", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_ADMIN)  # type: ignore[misc]
@validate_request(BanPlayerRequest)
async def ban_rcon_player(data: BanPlayerRequest, community_id: int, server_id: int) -> Any:
    """`api.js` `rconApi.banPlayer` -- proxy passthrough, see module docstring."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    try:
        return await bot_rcon.ban_player(
            _dal(),
            community_id,
            server_id,
            player=data.player,
            reason=data.reason or "",
            duration=data.duration or 0,
            user_id=_user_id(),
        )
    except bot_rcon.RconValidationError as exc:
        return _error(str(exc), 400, "BAD_REQUEST")


@bot_bp.route("/<int:community_id>/rcon/servers/<int:server_id>/channels", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_ADMIN)  # type: ignore[misc]
async def get_rcon_channels(community_id: int, server_id: int) -> Any:
    """`api.js` `rconApi.getChannels` -- proxy passthrough, see module docstring."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    return await bot_rcon.get_channels(_dal(), community_id, server_id)


@bot_bp.route("/<int:community_id>/rcon/servers/<int:server_id>/move", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_ADMIN)  # type: ignore[misc]
@validate_request(MoveUserRequest)
async def move_rcon_user(data: MoveUserRequest, community_id: int, server_id: int) -> Any:
    """`api.js` `rconApi.moveUser` -- proxy passthrough, see module docstring."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    try:
        return await bot_rcon.move_user(
            _dal(), community_id, server_id, target_user_id=data.user_id, channel_id=data.channel_id
        )
    except bot_rcon.RconValidationError as exc:
        return _error(str(exc), 400, "BAD_REQUEST")


@bot_bp.route("/<int:community_id>/rcon/servers/<int:server_id>/message", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_ADMIN)  # type: ignore[misc]
@validate_request(SendMessageRequest)
async def send_rcon_message(data: SendMessageRequest, community_id: int, server_id: int) -> Any:
    """`api.js` `rconApi.sendMessage` -- proxy passthrough, see module docstring."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    try:
        return await bot_rcon.send_message(
            _dal(),
            community_id,
            server_id,
            text=data.text,
            channel_id=data.channel_id,
            target_mode=data.target_mode,
        )
    except bot_rcon.RconValidationError as exc:
        return _error(str(exc), 400, "BAD_REQUEST")


@bot_bp.route("/<int:community_id>/rcon/log", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_ADMIN)  # type: ignore[misc]
@validate_response(CommandLogResponse)
async def get_rcon_command_log(community_id: int) -> Any:
    """`api.js` `rconApi.getCommandLog` -- `GET /api/v1/admin/:communityId/rcon/log`."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    limit = min(200, max(1, _int_arg("limit", 50)))
    offset = max(0, _int_arg("offset", 0))
    server_id = _int_arg("server_id", 0) or None
    entries = await asyncio.to_thread(
        bot_rcon.get_command_log,
        _dal(),
        community_id,
        limit=limit,
        offset=offset,
        server_id=server_id,
    )
    return CommandLogResponse(log=entries)


@bot_bp.route("/<int:community_id>/rcon/servers/<int:server_id>/policy", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_ADMIN)  # type: ignore[misc]
async def get_rcon_access_policy(community_id: int, server_id: int) -> Any:
    """`api.js` `rconApi.getAccessPolicy` -- proxy passthrough, see module docstring."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    return await bot_rcon.get_access_policy(_dal(), community_id, server_id)


@bot_bp.route("/<int:community_id>/rcon/servers/<int:server_id>/policy", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_ADMIN)  # type: ignore[misc]
async def update_rcon_access_policy(community_id: int, server_id: int) -> Any:
    """`api.js` `rconApi.updateAccessPolicy` -- proxy passthrough, see module docstring.

    No `@validate_request` DTO: Node forwards `req.body` to the downstream
    module verbatim (`proxyToModule(..., req.body)`), and the policy shape
    is owned by `server-manager-service`, not this port.
    """
    denied = await _require_community(community_id)
    if denied:
        return denied
    policy = await request.get_json(force=True, silent=True) or {}
    return await bot_rcon.update_access_policy(_dal(), community_id, server_id, policy)


@bot_bp.route("/<int:community_id>/rcon/servers/<int:server_id>/enforce", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_ADMIN)  # type: ignore[misc]
async def trigger_rcon_enforcement(community_id: int, server_id: int) -> Any:
    """`api.js` `rconApi.triggerEnforcement` -- proxy passthrough, see module docstring."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    return await bot_rcon.trigger_enforcement(_dal(), community_id, server_id)


@bot_bp.route("/<int:community_id>/rcon/servers/<int:server_id>/access-log", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_ADMIN)  # type: ignore[misc]
async def get_rcon_access_log(community_id: int, server_id: int) -> Any:
    """`api.js` `rconApi.getAccessLog` -- proxy passthrough, see module docstring."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    limit = min(200, max(1, _int_arg("limit", 50)))
    offset = max(0, _int_arg("offset", 0))
    return await bot_rcon.get_access_log(
        _dal(), community_id, server_id, limit=limit, offset=offset
    )


# -- Member routes (requireAuth only in Node -- no requireCommunityAdmin) --


@bot_bp.route("/<int:community_id>/rcon/info", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_READ)  # type: ignore[misc]
@validate_response(ServersMemberResponse)
async def list_rcon_info(community_id: int) -> Any:
    """`api.js` `rconApi.listInfo` -- `GET .../rcon/info` (member view, reuses `listServers`)."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    servers = await asyncio.to_thread(bot_rcon.list_servers, _dal(), community_id, is_admin=False)
    return ServersMemberResponse(servers=servers)


@bot_bp.route("/<int:community_id>/rcon/info/<int:server_id>/status", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_READ)  # type: ignore[misc]
async def get_rcon_server_status(community_id: int, server_id: int) -> Any:
    """`api.js` `rconApi.getServerStatus` -- proxy passthrough, see module docstring."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    return await bot_rcon.get_server_status(_dal(), community_id, server_id)


@bot_bp.route("/<int:community_id>/rcon/info/<int:server_id>/players", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_READ)  # type: ignore[misc]
async def get_rcon_player_list(community_id: int, server_id: int) -> Any:
    """`api.js` `rconApi.getPlayerList` -- proxy passthrough, see module docstring."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    return await bot_rcon.get_player_list(_dal(), community_id, server_id)


BLUEPRINTS: list[Blueprint] = [bot_bp]
