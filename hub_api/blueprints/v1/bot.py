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
import uuid
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from flask_core.authz import require_scope
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, request
from quart_schema import validate_request, validate_response

from services import bot_ai_chatter, bot_ai_knowledge, bot_rcon, bot_shoutout
from services.bot_tables import bind_bot_tables, community_belongs_to_tenant
from services.community_authz import resolve_caller_standing_sync
from services.community_common import api_error
from services.community_public_id import (
    lookup_public_ids,
    resolve_public_community_id,
    to_internal_community_id,
)
from services.errors import ApiError
from services.global_identity import is_global_admin_sync
from services.public_ids import (
    PublicIdTarget,
    Scope,
    build_view_arg_translator,
    parse_public_id,
    resolve_public_id,
)
from services.public_ids import lookup_public_ids as lookup_entity_public_ids
from services.route_guards import (
    GUARD_ADMIN,
    community_admin_guard,
    community_member_guard,
    mark_guarded,
)

bot_bp = Blueprint("v1_bot", __name__, url_prefix="/api/v1/admin")

# `<puuid:...>` path ids -> numeric, BEFORE the handler (the app-level `cuuid` hook already ran).
# Community-scoped targets resolve inside the URL community only; the ai-knowledge ids carry no
# URL community, so they resolve globally and `_load_authorized_source` / `_scope_allowed` stay
# the (unchanged) authz: a foreign or unknown UUID is the same 404 as before.
bot_bp.before_request(
    build_view_arg_translator(
        {
            "creator_id": PublicIdTarget("shoutout_creators"),
            "server_id": PublicIdTarget("server_status_configs"),
            "source_id": PublicIdTarget("ai_knowledge_sources", community_param=None),
            "suggestion_id": PublicIdTarget("ai_ticket_suggestions", community_param=None),
        }
    )
)

# -- Scope namespace (see module docstring's D5 note) --
_SHOUTOUT_READ = "bot.shoutout:read"
_SHOUTOUT_WRITE = "bot.shoutout:write"
_AI_CHATTER_READ = "bot.ai_chatter:read"
_AI_CHATTER_WRITE = "bot.ai_chatter:write"
logger = logging.getLogger(__name__)

_AI_KNOWLEDGE_READ = "bot.ai_knowledge:read"
_AI_KNOWLEDGE_WRITE = "bot.ai_knowledge:write"
_SERVER_MANAGER_ADMIN = "bot.server_manager:admin"
_SERVER_MANAGER_READ = "bot.server_manager:read"


def _error(message: str, status: int, code: str) -> tuple[dict[str, Any], int]:
    """Matches `middleware/errorHandler.js`'s `{success: false, error: {code, message}}` shape."""
    return {"success": False, "error": {"code": code, "message": message}}, status


async def _to_thread(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a sync-DAL `fn` on a pool thread, rolling the thread's connection back on failure.

    pydal connections are thread-local and the default executor reuses threads. A failed
    statement leaves that connection's transaction aborted; without a rollback every later
    request landing on the same thread dies with `InFailedSqlTransaction` (the alpha 500s on
    `shoutout/creators` and `rcon/servers`). Rollback runs ON the failing thread, then the
    original exception is re-raised unchanged -- no error is swallowed.
    """
    from quart import current_app

    dal = current_app.config["dal"]

    def _run() -> Any:
        try:
            return fn(*args, **kwargs)
        except BaseException:
            try:
                dal.rollback()
            except Exception:  # nosec B110 - rollback is best-effort; the original error is re-raised
                logging.getLogger(__name__).warning("bot_dal_rollback_failed", exc_info=True)
            raise

    return await asyncio.to_thread(_run)


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
    ok = await _to_thread(community_belongs_to_tenant, dal, community_id, ctx.tenant_id)
    if not ok:
        # 404, identical to `route_guards._guard`: a foreign-tenant id must not be an oracle.
        return api_error("Community not found", status_code=404)
    return None


async def _require_community_admin_inline(
    community_id: int,
) -> tuple[dict[str, Any], int] | None:
    """403 unless the caller administers `community_id` (run after `_require_community`)."""
    if not await _to_thread(_caller_administers, community_id):
        return _error("Community admin access required", 403, "FORBIDDEN")
    return None


def _caller_administers(community_id: int) -> bool:
    """True when the caller administers `community_id` (DB-backed standing, never token scope)."""
    from quart import current_app

    standing = resolve_caller_standing_sync(
        request, current_app.config["dal"], community_id=community_id
    )
    return bool(standing.is_admin)


async def _require_server(community_id: int, server_id: int) -> None:
    """404 unless `server_id` is an active server of `community_id` (no cross-community ids).

    Run after `_require_community` on every `<server_id>` route so a foreign server id is
    indistinguishable from a missing one, independent of the downstream module's own checks.
    """
    try:
        await _to_thread(bot_rcon.get_active_server, _dal(), community_id, server_id)
    except bot_rcon.RconNotFoundError as exc:
        raise ApiError(str(exc), 404, "NOT_FOUND") from exc


async def _require_member_server(community_id: int, server_id: int) -> Any:
    """Row for a member-facing `<server_id>` route; hidden servers 404 for non-admins.

    Admins (DB-backed standing) may read any active server of the community; everyone else only
    those in `bot_rcon.MEMBER_VISIBLE_VISIBILITIES` -- the same set `list_servers` uses. A hidden
    server returns the identical 404 as a missing id. DB lookups run via `_to_thread`.
    """
    is_admin = await _to_thread(_caller_administers, community_id)
    try:
        return await _to_thread(
            bot_rcon.get_active_server, _dal(), community_id, server_id, member_only=not is_admin
        )
    except bot_rcon.RconNotFoundError as exc:
        raise ApiError(str(exc), 404, "NOT_FOUND") from exc


@bot_bp.errorhandler(ApiError)
async def _handle_api_error(exc: ApiError) -> tuple[dict[str, Any], int]:
    """Convert a raised `ApiError` into the Bot JSON error shape, logging type + message."""
    logging.getLogger(__name__).warning(
        "bot_api_error",
        extra={
            "error_type": type(exc).__name__,
            "status": exc.status_code,
            "error_message": exc.message,
        },
    )
    return _error(exc.message, exc.status_code, exc.code)


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

    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    payload = verify_jwt_token(token) if token else None
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


@bot_bp.route("/<cuuid:community_id>/shoutout/config", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SHOUTOUT_READ)  # type: ignore[misc]
@community_admin_guard  # type: ignore[misc]
@validate_response(ShoutoutConfigResponse)
async def get_shoutout_config(community_id: int) -> Any:
    """`api.js` `getShoutoutConfig` -- `GET /api/v1/admin/:communityId/shoutout/config`."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    config = await _to_thread(bot_shoutout.get_or_create_shoutout_config, _dal(), community_id)
    return ShoutoutConfigResponse(success=True, config=config)


@bot_bp.route("/<cuuid:community_id>/shoutout/config", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SHOUTOUT_WRITE)  # type: ignore[misc]
@community_admin_guard  # type: ignore[misc]
@validate_request(ShoutoutConfigRequest)
@validate_response(ShoutoutConfigResponse)
async def update_shoutout_config(data: ShoutoutConfigRequest, community_id: int) -> Any:
    """`api.js` `updateShoutoutConfig` -- `PUT /api/v1/admin/:communityId/shoutout/config`."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    config = await _to_thread(
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


@bot_bp.route("/<cuuid:community_id>/shoutout/creators", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SHOUTOUT_READ)  # type: ignore[misc]
@community_admin_guard  # type: ignore[misc]
@validate_response(ShoutoutCreatorsResponse)
async def get_shoutout_creators(community_id: int) -> Any:
    """`api.js` `getShoutoutCreators` -- `GET /api/v1/admin/:communityId/shoutout/creators`."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    creators = await _to_thread(bot_shoutout.list_shoutout_creators, _dal(), community_id)
    return ShoutoutCreatorsResponse(success=True, creators=creators)


@bot_bp.route("/<cuuid:community_id>/shoutout/creators", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SHOUTOUT_WRITE)  # type: ignore[misc]
@community_admin_guard  # type: ignore[misc]
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
    creator = await _to_thread(
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


@bot_bp.route("/<cuuid:community_id>/shoutout/creators/<puuid:creator_id>", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SHOUTOUT_WRITE)  # type: ignore[misc]
@community_admin_guard  # type: ignore[misc]
@validate_response(ShoutoutMessageResponse)
async def remove_shoutout_creator(community_id: int, creator_id: int) -> Any:
    """`api.js` `removeShoutoutCreator` -- `DELETE .../shoutout/creators/:creatorId`."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    removed = await _to_thread(
        bot_shoutout.remove_shoutout_creator, _dal(), community_id, creator_id
    )
    if not removed:
        return _error("Creator not found", 404, "NOT_FOUND")
    return ShoutoutMessageResponse(success=True, message="Creator removed")


@bot_bp.route("/<cuuid:community_id>/shoutout/history", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SHOUTOUT_READ)  # type: ignore[misc]
@community_admin_guard  # type: ignore[misc]
@validate_response(ShoutoutHistoryResponse)
async def get_shoutout_history(community_id: int) -> Any:
    """`api.js` `getShoutoutHistory` -- `GET /api/v1/admin/:communityId/shoutout/history`."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    page = max(1, _int_arg("page", 1))
    limit = min(100, max(1, _int_arg("limit", 25)))
    entries, total = await _to_thread(
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


#: Stable machine-readable code the webui classifies on (neutral "not available" panel).
CODE_AI_CHATTER_UNAVAILABLE = "AI_CHATTER_UNAVAILABLE"


def _chatter_unavailable() -> tuple[dict[str, Any], int]:
    """503 standard envelope when `AI_INTERACTION_API_URL` is unset (backend not deployed).

    Same shape as the analytics unavailable state (`{success, error:{code,message}}`) so the
    webui renders a neutral panel instead of the raw `service_unavailable` token.
    """
    return _error(
        "AI Chatter is not available on this deployment yet", 503, CODE_AI_CHATTER_UNAVAILABLE
    )


def _chatter_dto(config: dict[str, Any]) -> AIChatterConfigDTO:
    """Project relayed upstream config onto `AIChatterConfigDTO`."""
    known = {f.name for f in dataclasses.fields(AIChatterConfigDTO)}
    return AIChatterConfigDTO(**{k: v for k, v in config.items() if k in known})


@bot_bp.route("/<cuuid:community_id>/ai-chatter/config", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_AI_CHATTER_READ)  # type: ignore[misc]
@community_admin_guard  # type: ignore[misc]
@validate_response(AIChatterConfigResponse)
async def get_ai_chatter_config(community_id: int) -> Any:
    """`api.js` `getAIChatterConfig` -- proxy passthrough, see module docstring."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    if not bot_ai_chatter.is_configured():
        return _chatter_unavailable()
    config = await bot_ai_chatter.get_chatter_config(community_id)
    return AIChatterConfigResponse(success=True, config=_chatter_dto(config))


@bot_bp.route("/<cuuid:community_id>/ai-chatter/config", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_AI_CHATTER_WRITE)  # type: ignore[misc]
@community_admin_guard  # type: ignore[misc]
@validate_request(AIChatterConfigRequest)
@validate_response(AIChatterConfigResponse)
async def update_ai_chatter_config(data: AIChatterConfigRequest, community_id: int) -> Any:
    """`api.js` `updateAIChatterConfig` -- proxy passthrough, see module docstring."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    if not bot_ai_chatter.is_configured():
        return _chatter_unavailable()
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
class KnowledgeSourceDTO:
    """One knowledge source as exposed over the API: `id`/`community_id` are PUBLIC UUIDs."""

    id: str
    community_id: str | None
    vendor_id: int | None
    module_id: int | None
    source_name: str
    source_type: str
    source_url: str | None
    branch: str
    docs_path: str
    refresh_interval: str
    is_active: bool
    last_indexed_at: str | None
    indexed_page_count: int
    index_errors: str | None
    created_at: str | None
    updated_at: str | None


async def _entity_uuids(table: str, ids: Any) -> dict[int, str]:
    """ONE batched numeric -> public UUID lookup for response DTOs (fails loud on a gap)."""
    wanted = list(ids)
    if not wanted:
        return {}
    return await _to_thread(lookup_entity_public_ids, _dal(), table, wanted)


async def _source_dtos(
    sources: list[bot_ai_knowledge.KnowledgeSource],
) -> list[KnowledgeSourceDTO]:
    """Map service sources to DTOs with ONE batched public-id lookup (fails loud if missing)."""
    public = await lookup_public_ids(s.community_id for s in sources if s.community_id is not None)
    source_uuids = await _entity_uuids("ai_knowledge_sources", (s.id for s in sources))
    out: list[KnowledgeSourceDTO] = []
    for src in sources:
        values = {f.name: getattr(src, f.name) for f in dataclasses.fields(src)}
        values["id"] = source_uuids[src.id]
        cid = values["community_id"]
        values["community_id"] = None if cid is None else public[cid]
        out.append(KnowledgeSourceDTO(**values))
    return out


@dataclass(slots=True, frozen=True)
class KnowledgeSourcesResponse:
    """Response body -- Knowledge Sources."""

    success: bool
    sources: list[KnowledgeSourceDTO]


@dataclass(slots=True, frozen=True)
class KnowledgeSourceCreateRequest:
    """Request body -- Knowledge Source Create."""

    source_name: str
    source_type: str
    #: Community public UUID (never the numeric id).
    community_id: uuid.UUID | None = None
    vendor_id: int | None = None
    module_id: int | None = None
    source_url: str | None = None
    branch: str = "main"
    docs_path: str = "/"
    refresh_interval: str = "weekly"
    #: Plaintext access token -- encrypted server-side before storage, never returned.
    token: str | None = None
    #: DEPRECATED alias of `token` (legacy clients); also treated as plaintext.
    encrypted_token: str | None = None


@dataclass(slots=True, frozen=True)
class KnowledgeSourceResponse:
    """Response body -- Knowledge Source."""

    success: bool
    source: KnowledgeSourceDTO


@dataclass(slots=True, frozen=True)
class KnowledgeSourceUpdateRequest:
    """Request body -- Knowledge Source Update."""

    source_name: str | None = None
    source_url: str | None = None
    branch: str | None = None
    docs_path: str | None = None
    refresh_interval: str | None = None
    #: Plaintext access token -- encrypted server-side before storage, never returned.
    token: str | None = None
    #: DEPRECATED alias of `token` (legacy clients); also treated as plaintext.
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
    sourceId: str


@dataclass(slots=True, frozen=True)
class KnowledgeSearchRequest:
    """Request body -- Knowledge Search."""

    query: str
    #: Community public UUID (never the numeric id).
    communityId: uuid.UUID | None = None
    vendorId: int | None = None
    topK: int | None = None


@dataclass(slots=True, frozen=True)
class KnowledgeChunkDTO:
    """Nested response field -- Knowledge Chunk."""

    id: int
    source_id: str
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
    #: Community public UUID (never the numeric id).
    communityId: uuid.UUID | None = None


@dataclass(slots=True, frozen=True)
class TicketSuggestionDTO:
    """Nested response field -- Ticket Suggestion; `id` = public UUID."""

    id: str
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


def _is_super_admin_sync(dal: Any, user_id: int) -> bool:
    """DB-authoritative system-tenant-admin lookup (never the JWT `roles` claim)."""
    if user_id <= 0:
        return False
    return is_global_admin_sync(dal, user_id)


async def _caller_is_super_admin() -> bool:
    """True when the authenticated caller is a global super admin."""
    return bool(await _to_thread(_is_super_admin_sync, _dal(), _user_id()))


async def _scope_allowed(community_id: int | None, vendor_id: int | None) -> bool:
    """Can the caller act on a knowledge source with this (community, vendor) scope?

    Community-scoped: the community must belong to the caller's tenant.
    Vendor-scoped (no community): the caller must be that vendor. Global
    (neither): super admin only. Super admins pass every scope.
    """
    if await _caller_is_super_admin():
        return True
    if community_id is not None:
        ctx = get_tenant_context(request)
        assert ctx is not None  # nosec B101 - tenant_middleware already enforced this
        # Tenant fence AND community admin: `bot.ai_knowledge:write` is a tenant-wide token scope,
        # so on its own it would let a maintainer of another community drive this one's sources.
        return bool(
            await _to_thread(community_belongs_to_tenant, _dal(), community_id, ctx.tenant_id)
        ) and bool(await _to_thread(_caller_administers, community_id))
    uid = _user_id()
    return vendor_id is not None and uid > 0 and vendor_id == uid


_SOURCE_NOT_FOUND = "Knowledge source not found"


async def _load_authorized_source(source_id: int) -> bool:
    """True iff the source exists AND the caller may act on it.

    Callers answer 404 (never 403) on False so a foreign source id is
    indistinguishable from a missing one -- no cross-tenant existence oracle.
    """
    # Query form, not `table[id]`: pydal raises KeyError for a negative key, and an unknown/foreign
    # UUID arrives as the negative `UNKNOWN_ID` sentinel -- it must be the plain 404 miss.
    dal = _dal()
    row = await _to_thread(lambda: dal(dal.ai_knowledge_sources.id == source_id).select().first())
    if row is None:
        return False
    if not await _scope_allowed(row.community_id, row.vendor_id):
        return False
    # Vendor-owned sources stay vendor-controlled even when also community-scoped
    # (confused deputy: a community admin must not drive the vendor's stored credential).
    return row.vendor_id is None or await _scope_allowed(None, row.vendor_id)


async def _require_query_scope(community_id: int | None) -> tuple[dict[str, Any], int] | None:
    """Gate for list/search/suggest: community admin of a tenant-checked community, or super admin.

    Without it a tenant admin could vector-search every community's knowledge chunks. With no
    community only a global super admin passes (400 otherwise, as before).
    """
    if community_id is not None:
        not_in_tenant = await _require_community(community_id)
        if not_in_tenant:
            return not_in_tenant
    is_admin = community_id is not None and await _to_thread(_caller_administers, community_id)
    if is_admin:
        return None
    if community_id is None:
        if await _caller_is_super_admin():
            return None
        return _error("communityId is required", 400, "BAD_REQUEST")
    return _error("Community admin access required", 403, "FORBIDDEN")


@bot_bp.route("/ai-knowledge/sources", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_AI_KNOWLEDGE_READ)  # type: ignore[misc]
@mark_guarded(GUARD_ADMIN)  # type: ignore[misc]
@validate_response(KnowledgeSourcesResponse)
async def list_knowledge_sources() -> Any:
    """`aiKnowledgeController.listSources` -- `GET .../ai-knowledge/sources?communityId=`.

    `communityId` (legacy `community_id` accepted) is REQUIRED for non-super-admins;
    only a global super admin may list unfiltered.
    """
    raw_community = request.args.get("communityId") or request.args.get("community_id")
    community_id: int | None = None
    if raw_community:
        community_id = await resolve_public_community_id(raw_community)
        if community_id is None:
            # Same answer as a foreign-tenant community: no existence oracle.
            return api_error("Community not found", status_code=404)
    denied = await _require_query_scope(community_id)
    if denied:
        return denied
    sources = await _to_thread(bot_ai_knowledge.list_knowledge_sources, _dal(), community_id)
    return KnowledgeSourcesResponse(success=True, sources=await _source_dtos(sources))


@bot_bp.route("/ai-knowledge/sources", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_AI_KNOWLEDGE_WRITE)  # type: ignore[misc]
@validate_request(KnowledgeSourceCreateRequest)
@validate_response(KnowledgeSourceResponse, 201)
async def create_knowledge_source(data: KnowledgeSourceCreateRequest) -> Any:
    """`aiKnowledgeController.createSource` -- `POST /api/v1/admin/ai-knowledge/sources`."""
    community_id = await to_internal_community_id(data.community_id)
    denied = await _check_optional_community(community_id)
    if denied:
        return denied
    if not await _scope_allowed(community_id, data.vendor_id) or (
        community_id is not None
        and data.vendor_id is not None
        and not await _scope_allowed(None, data.vendor_id)
    ):
        return _error("Not permitted for this knowledge source scope", 403, "FORBIDDEN")
    payload = bot_ai_knowledge.KnowledgeSourceCreate(
        source_name=data.source_name,
        source_type=data.source_type,
        community_id=community_id,
        vendor_id=data.vendor_id,
        module_id=data.module_id,
        source_url=data.source_url,
        branch=data.branch,
        docs_path=data.docs_path,
        refresh_interval=data.refresh_interval,
        token=data.token or data.encrypted_token,
    )
    dal = _dal()
    try:
        source = await _to_thread(bot_ai_knowledge.add_knowledge_source, dal, payload)
    except bot_ai_knowledge.KnowledgeServiceError as exc:
        logger.warning(
            "bot_request_rejected",
            extra={"error_type": type(exc).__name__, "status": exc.status},
        )
        return _error(str(exc), exc.status, "BAD_REQUEST" if exc.status == 400 else "ERROR")

    if source.source_type != "manual":
        # Fire-and-forget indexing -- matches Node's `setImmediate` + uncaught-promise
        # pattern: the route responds before this completes.
        asyncio.create_task(_index_source_background(dal, source.id))

    return KnowledgeSourceResponse(success=True, source=(await _source_dtos([source]))[0]), 201


async def _index_source_background(dal: Any, source_id: int) -> None:
    """Background indexing task -- matches Node's `indexSource(id).catch(err => logger.error)`."""
    try:
        await bot_ai_knowledge.index_source(dal, source_id)
    except Exception as exc:  # noqa: BLE001 - background task, must not propagate to the event loop
        logging.getLogger(__name__).error(
            "Background reindex failed source_id=%s err=%s", source_id, exc
        )


@bot_bp.route("/ai-knowledge/sources/<puuid:source_id>", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_AI_KNOWLEDGE_WRITE)  # type: ignore[misc]
@validate_request(KnowledgeSourceUpdateRequest)
@validate_response(KnowledgeSourceResponse)
async def update_knowledge_source(data: KnowledgeSourceUpdateRequest, source_id: int) -> Any:
    """`aiKnowledgeController.updateSource` -- `PUT /api/v1/admin/ai-knowledge/sources/:id`."""
    if not await _load_authorized_source(source_id):
        return _error(_SOURCE_NOT_FOUND, 404, "NOT_FOUND")
    # `data` is a `slots=True` dataclass -- no `__dict__` -- `dataclasses.fields()` instead.
    updates = {
        f.name: getattr(data, f.name)
        for f in dataclasses.fields(data)
        if f.name != "encrypted_token" and getattr(data, f.name) is not None
    }
    if "token" not in updates and data.encrypted_token is not None:
        updates["token"] = data.encrypted_token  # deprecated alias
    try:
        source = await _to_thread(
            bot_ai_knowledge.update_knowledge_source, _dal(), source_id, updates
        )
    except bot_ai_knowledge.KnowledgeServiceError as exc:
        logger.warning(
            "bot_request_rejected",
            extra={"error_type": type(exc).__name__, "status": exc.status},
        )
        return _error(str(exc), exc.status, "NOT_FOUND" if exc.status == 404 else "BAD_REQUEST")
    return KnowledgeSourceResponse(success=True, source=(await _source_dtos([source]))[0])


@bot_bp.route("/ai-knowledge/sources/<puuid:source_id>", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_AI_KNOWLEDGE_WRITE)  # type: ignore[misc]
@validate_response(SimpleSuccessResponse)
async def delete_knowledge_source(source_id: int) -> Any:
    """`aiKnowledgeController.deleteSource` -- `DELETE /api/v1/admin/ai-knowledge/sources/:id`."""
    if not await _load_authorized_source(source_id):
        return _error(_SOURCE_NOT_FOUND, 404, "NOT_FOUND")
    try:
        await _to_thread(bot_ai_knowledge.delete_knowledge_source, _dal(), source_id)
    except bot_ai_knowledge.KnowledgeServiceError as exc:
        logger.warning(
            "bot_request_rejected",
            extra={"error_type": type(exc).__name__, "status": exc.status},
        )
        return _error(str(exc), exc.status, "NOT_FOUND")
    return SimpleSuccessResponse(success=True)


@bot_bp.route("/ai-knowledge/sources/<puuid:source_id>/reindex", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_AI_KNOWLEDGE_WRITE)  # type: ignore[misc]
@validate_response(ReindexResponse)
async def reindex_source(source_id: int) -> Any:
    """`aiKnowledgeController.reindexSource` -- responds immediately, indexes async."""
    if not await _load_authorized_source(source_id):
        return _error(_SOURCE_NOT_FOUND, 404, "NOT_FOUND")
    asyncio.create_task(_index_source_background(_dal(), source_id))
    source_uuid = (await _entity_uuids("ai_knowledge_sources", [source_id]))[source_id]
    return ReindexResponse(success=True, message="Reindex started", sourceId=source_uuid)


async def _suggestion_dto(suggestion: bot_ai_knowledge.TicketSuggestion) -> TicketSuggestionDTO:
    uuids = await _entity_uuids("ai_ticket_suggestions", [suggestion.id])
    return TicketSuggestionDTO(
        id=uuids[suggestion.id],
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
@mark_guarded(GUARD_ADMIN)  # type: ignore[misc]
@validate_request(KnowledgeSearchRequest)
@validate_response(KnowledgeSearchResponse)
async def search_knowledge_base(data: KnowledgeSearchRequest) -> Any:
    """`aiKnowledgeController.searchKnowledgeBase` -- `POST /api/v1/admin/ai-knowledge/search`."""
    if not data.query or not data.query.strip():
        return _error("query is required", 400, "BAD_REQUEST")
    community_id = await to_internal_community_id(data.communityId)
    denied = await _require_query_scope(community_id)
    if denied:
        return denied
    try:
        results = await bot_ai_knowledge.search_knowledge(
            _dal(),
            data.query,
            community_id=community_id,
            vendor_id=data.vendorId,
            top_k=data.topK or 5,
        )
    except bot_ai_knowledge.KnowledgeServiceError as exc:
        logger.warning("ai-knowledge request refused: status=%s", exc.status)
        return _error(str(exc), exc.status, "BAD_REQUEST")
    source_uuids = await _entity_uuids("ai_knowledge_sources", {r.source_id for r in results})
    return KnowledgeSearchResponse(
        success=True,
        results=[
            KnowledgeSearchResultDTO(
                chunk=KnowledgeChunkDTO(
                    id=r.chunk_id,
                    source_id=source_uuids[r.source_id],
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
    community_id = await to_internal_community_id(data.communityId)
    denied = await _require_query_scope(community_id)
    if denied:
        return denied
    try:
        suggestion = await bot_ai_knowledge.generate_suggestion(
            _dal(), data.ticketId, data.ticketText.strip(), community_id=community_id
        )
    except bot_ai_knowledge.KnowledgeServiceError as exc:
        logger.warning("ai-knowledge request refused: status=%s", exc.status)
        return _error(str(exc), exc.status, "BAD_REQUEST")
    if suggestion is None:
        return SuggestResponse(
            success=True,
            suggestion=None,
            message="No suggestion generated -- knowledge base confidence below threshold",
        )
    return SuggestResponse(success=True, suggestion=await _suggestion_dto(suggestion)), 201


@bot_bp.route("/ai-knowledge/suggestions/<puuid:suggestion_id>/feedback", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_AI_KNOWLEDGE_WRITE)  # type: ignore[misc]
@validate_request(SuggestionFeedbackRequest)
@validate_response(SuggestionFeedbackResponse)
async def submit_suggestion_feedback(data: SuggestionFeedbackRequest, suggestion_id: int) -> Any:
    """`aiKnowledgeController.submitFeedback` -- `POST .../suggestions/:id/feedback`."""
    if not data.feedback:
        return _error("feedback is required", 400, "BAD_REQUEST")
    try:
        bot_ai_knowledge.validate_feedback(data.feedback)
    except bot_ai_knowledge.KnowledgeServiceError as exc:
        raise ApiError(str(exc), exc.status, "BAD_REQUEST") from exc
    exists, owner = await _to_thread(bot_ai_knowledge.get_suggestion_scope, _dal(), suggestion_id)
    if not exists or not await _scope_allowed(owner, None):
        return _error("Suggestion not found", 404, "NOT_FOUND")
    try:
        suggestion = await _to_thread(
            partial(
                bot_ai_knowledge.record_feedback,
                _dal(),
                suggestion_id,
                data.feedback,
                community_id=owner,
            )
        )
    except bot_ai_knowledge.KnowledgeServiceError as exc:
        code = "NOT_FOUND" if exc.status == 404 else "BAD_REQUEST"
        raise ApiError(str(exc), exc.status, code) from exc
    return SuggestionFeedbackResponse(success=True, suggestion=await _suggestion_dto(suggestion))


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


@bot_bp.route("/<cuuid:community_id>/rcon/servers", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_ADMIN)  # type: ignore[misc]
@community_admin_guard  # type: ignore[misc]
@validate_response(ServersResponse)
async def list_rcon_servers(community_id: int) -> Any:
    """`api.js` `rconApi.listServers` -- `GET .../:communityId/rcon/servers` (admin view)."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    servers = await _to_thread(bot_rcon.list_servers, _dal(), community_id, is_admin=True)
    return ServersResponse(servers=servers)


@bot_bp.route("/<cuuid:community_id>/rcon/servers", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_ADMIN)  # type: ignore[misc]
@community_admin_guard  # type: ignore[misc]
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
        server = await _to_thread(
            bot_rcon.create_server, _dal(), community_id, payload, added_by=_user_id()
        )
    except bot_rcon.RconValidationError as exc:
        logger.warning(
            "bot_request_rejected",
            extra={"error_type": type(exc).__name__, "error_message": str(exc)},
            exc_info=True,
        )
        return _error(str(exc), 400, "BAD_REQUEST")
    return ServerResponse(server=server), 201


@bot_bp.route("/<cuuid:community_id>/rcon/servers/<puuid:server_id>", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_ADMIN)  # type: ignore[misc]
@community_admin_guard  # type: ignore[misc]
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
        server = await _to_thread(bot_rcon.update_server, _dal(), community_id, server_id, payload)
    except bot_rcon.RconValidationError as exc:
        logger.warning(
            "bot_request_rejected",
            extra={"error_type": type(exc).__name__, "error_message": str(exc)},
            exc_info=True,
        )
        return _error(str(exc), 400, "BAD_REQUEST")
    except bot_rcon.RconNotFoundError as exc:
        logger.warning(
            "bot_request_rejected",
            extra={"error_type": type(exc).__name__, "error_message": str(exc)},
            exc_info=True,
        )
        return _error(str(exc), 404, "NOT_FOUND")
    return ServerResponse(server=server)


@bot_bp.route("/<cuuid:community_id>/rcon/servers/<puuid:server_id>", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_ADMIN)  # type: ignore[misc]
@community_admin_guard  # type: ignore[misc]
@validate_response(MessageResponse)
async def delete_rcon_server(community_id: int, server_id: int) -> Any:
    """`api.js` `rconApi.deleteServer` -- `DELETE .../:communityId/rcon/servers/:serverId`."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    try:
        await _to_thread(bot_rcon.delete_server, _dal(), community_id, server_id)
    except bot_rcon.RconNotFoundError as exc:
        logger.warning(
            "bot_request_rejected",
            extra={"error_type": type(exc).__name__, "error_message": str(exc)},
            exc_info=True,
        )
        return _error(str(exc), 404, "NOT_FOUND")
    return MessageResponse(message="Server deleted")


@bot_bp.route("/<cuuid:community_id>/rcon/servers/<puuid:server_id>/test", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_ADMIN)  # type: ignore[misc]
@community_admin_guard  # type: ignore[misc]
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
        logger.warning(
            "bot_request_rejected",
            extra={"error_type": type(exc).__name__, "error_message": str(exc)},
            exc_info=True,
        )
        return _error(str(exc), 404, "NOT_FOUND")


@bot_bp.route("/<cuuid:community_id>/rcon/servers/<puuid:server_id>/command", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_ADMIN)  # type: ignore[misc]
@community_admin_guard  # type: ignore[misc]
@validate_request(ExecuteCommandRequest)
async def execute_rcon_command(
    data: ExecuteCommandRequest, community_id: int, server_id: int
) -> Any:
    """`api.js` `rconApi.executeCommand` -- proxy passthrough, see module docstring."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    await _require_server(community_id, server_id)
    try:
        return await bot_rcon.execute_command(
            _dal(), community_id, server_id, command=data.command, user_id=_user_id()
        )
    except bot_rcon.RconValidationError as exc:
        logger.warning(
            "bot_request_rejected",
            extra={"error_type": type(exc).__name__, "error_message": str(exc)},
            exc_info=True,
        )
        return _error(str(exc), 400, "BAD_REQUEST")


@bot_bp.route("/<cuuid:community_id>/rcon/servers/<puuid:server_id>/kick", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_ADMIN)  # type: ignore[misc]
@community_admin_guard  # type: ignore[misc]
@validate_request(KickPlayerRequest)
async def kick_rcon_player(data: KickPlayerRequest, community_id: int, server_id: int) -> Any:
    """`api.js` `rconApi.kickPlayer` -- proxy passthrough, see module docstring."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    await _require_server(community_id, server_id)
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
        logger.warning(
            "bot_request_rejected",
            extra={"error_type": type(exc).__name__, "error_message": str(exc)},
            exc_info=True,
        )
        return _error(str(exc), 400, "BAD_REQUEST")


@bot_bp.route("/<cuuid:community_id>/rcon/servers/<puuid:server_id>/ban", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_ADMIN)  # type: ignore[misc]
@community_admin_guard  # type: ignore[misc]
@validate_request(BanPlayerRequest)
async def ban_rcon_player(data: BanPlayerRequest, community_id: int, server_id: int) -> Any:
    """`api.js` `rconApi.banPlayer` -- proxy passthrough, see module docstring."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    await _require_server(community_id, server_id)
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
        logger.warning(
            "bot_request_rejected",
            extra={"error_type": type(exc).__name__, "error_message": str(exc)},
            exc_info=True,
        )
        return _error(str(exc), 400, "BAD_REQUEST")


@bot_bp.route("/<cuuid:community_id>/rcon/servers/<puuid:server_id>/channels", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_ADMIN)  # type: ignore[misc]
@community_admin_guard  # type: ignore[misc]
async def get_rcon_channels(community_id: int, server_id: int) -> Any:
    """`api.js` `rconApi.getChannels` -- proxy passthrough, see module docstring."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    await _require_server(community_id, server_id)
    return await bot_rcon.get_channels(_dal(), community_id, server_id)


@bot_bp.route("/<cuuid:community_id>/rcon/servers/<puuid:server_id>/move", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_ADMIN)  # type: ignore[misc]
@community_admin_guard  # type: ignore[misc]
@validate_request(MoveUserRequest)
async def move_rcon_user(data: MoveUserRequest, community_id: int, server_id: int) -> Any:
    """`api.js` `rconApi.moveUser` -- proxy passthrough, see module docstring."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    await _require_server(community_id, server_id)
    try:
        return await bot_rcon.move_user(
            _dal(), community_id, server_id, target_user_id=data.user_id, channel_id=data.channel_id
        )
    except bot_rcon.RconValidationError as exc:
        logger.warning(
            "bot_request_rejected",
            extra={"error_type": type(exc).__name__, "error_message": str(exc)},
            exc_info=True,
        )
        return _error(str(exc), 400, "BAD_REQUEST")


@bot_bp.route("/<cuuid:community_id>/rcon/servers/<puuid:server_id>/message", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_ADMIN)  # type: ignore[misc]
@community_admin_guard  # type: ignore[misc]
@validate_request(SendMessageRequest)
async def send_rcon_message(data: SendMessageRequest, community_id: int, server_id: int) -> Any:
    """`api.js` `rconApi.sendMessage` -- proxy passthrough, see module docstring."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    await _require_server(community_id, server_id)
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
        logger.warning(
            "bot_request_rejected",
            extra={"error_type": type(exc).__name__, "error_message": str(exc)},
            exc_info=True,
        )
        return _error(str(exc), 400, "BAD_REQUEST")


@bot_bp.route("/<cuuid:community_id>/rcon/log", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_ADMIN)  # type: ignore[misc]
@community_admin_guard  # type: ignore[misc]
@validate_response(CommandLogResponse)
async def get_rcon_command_log(community_id: int) -> Any:
    """`api.js` `rconApi.getCommandLog` -- `GET /api/v1/admin/:communityId/rcon/log`."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    limit = min(200, max(1, _int_arg("limit", 50)))
    offset = max(0, _int_arg("offset", 0))
    server_id: int | None = None
    raw_server = request.args.get("server_id")
    if raw_server:
        # The filter is the server's public UUID, resolved inside this community only.
        server_id = await resolve_public_id(
            _dal(),
            "server_status_configs",
            parse_public_id(raw_server) or "",
            scope=Scope(community_id=community_id),
        )
        if server_id is None:
            return api_error("Server not found", status_code=404)
    entries = await _to_thread(
        bot_rcon.get_command_log,
        _dal(),
        community_id,
        limit=limit,
        offset=offset,
        server_id=server_id,
    )
    return CommandLogResponse(log=entries)


@bot_bp.route("/<cuuid:community_id>/rcon/servers/<puuid:server_id>/policy", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_ADMIN)  # type: ignore[misc]
@community_admin_guard  # type: ignore[misc]
async def get_rcon_access_policy(community_id: int, server_id: int) -> Any:
    """`api.js` `rconApi.getAccessPolicy` -- proxy passthrough, see module docstring."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    await _require_server(community_id, server_id)
    return await bot_rcon.get_access_policy(_dal(), community_id, server_id)


@bot_bp.route("/<cuuid:community_id>/rcon/servers/<puuid:server_id>/policy", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_ADMIN)  # type: ignore[misc]
@community_admin_guard  # type: ignore[misc]
async def update_rcon_access_policy(community_id: int, server_id: int) -> Any:
    """`api.js` `rconApi.updateAccessPolicy` -- proxy passthrough, see module docstring.

    No `@validate_request` DTO: Node forwards `req.body` to the downstream
    module verbatim (`proxyToModule(..., req.body)`), and the policy shape
    is owned by `server-manager-service`, not this port.
    """
    denied = await _require_community(community_id)
    if denied:
        return denied
    await _require_server(community_id, server_id)
    policy = await request.get_json(force=True, silent=True) or {}
    return await bot_rcon.update_access_policy(_dal(), community_id, server_id, policy)


@bot_bp.route("/<cuuid:community_id>/rcon/servers/<puuid:server_id>/enforce", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_ADMIN)  # type: ignore[misc]
@community_admin_guard  # type: ignore[misc]
async def trigger_rcon_enforcement(community_id: int, server_id: int) -> Any:
    """`api.js` `rconApi.triggerEnforcement` -- proxy passthrough, see module docstring."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    await _require_server(community_id, server_id)
    return await bot_rcon.trigger_enforcement(_dal(), community_id, server_id)


@bot_bp.route("/<cuuid:community_id>/rcon/servers/<puuid:server_id>/access-log", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_ADMIN)  # type: ignore[misc]
@community_admin_guard  # type: ignore[misc]
async def get_rcon_access_log(community_id: int, server_id: int) -> Any:
    """`api.js` `rconApi.getAccessLog` -- proxy passthrough, see module docstring."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    await _require_server(community_id, server_id)
    limit = min(200, max(1, _int_arg("limit", 50)))
    offset = max(0, _int_arg("offset", 0))
    return await bot_rcon.get_access_log(
        _dal(), community_id, server_id, limit=limit, offset=offset
    )


# -- Member routes (requireAuth only in Node -- no requireCommunityAdmin) --


@bot_bp.route("/<cuuid:community_id>/rcon/info", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_READ)  # type: ignore[misc]
@community_member_guard  # type: ignore[misc]
@validate_response(ServersMemberResponse)
async def list_rcon_info(community_id: int) -> Any:
    """`api.js` `rconApi.listInfo` -- `GET .../rcon/info` (member view, reuses `listServers`)."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    servers = await _to_thread(bot_rcon.list_servers, _dal(), community_id, is_admin=False)
    return ServersMemberResponse(servers=servers)


@bot_bp.route("/<cuuid:community_id>/rcon/info/<puuid:server_id>/status", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_READ)  # type: ignore[misc]
@community_member_guard  # type: ignore[misc]
@validate_response(bot_rcon.MemberStatusResponse)
async def get_rcon_server_status(community_id: int, server_id: int) -> Any:
    """`api.js` `rconApi.getServerStatus` -- member allowlist DTO, never a passthrough."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    row = await _require_member_server(community_id, server_id)
    return await bot_rcon.get_server_status(_dal(), community_id, server_id, row=row)


@bot_bp.route("/<cuuid:community_id>/rcon/info/<puuid:server_id>/players", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(_SERVER_MANAGER_READ)  # type: ignore[misc]
@community_member_guard  # type: ignore[misc]
@validate_response(bot_rcon.MemberPlayersResponse)
async def get_rcon_player_list(community_id: int, server_id: int) -> Any:
    """`api.js` `rconApi.getPlayerList` -- member allowlist DTO, never a passthrough."""
    denied = await _require_community(community_id)
    if denied:
        return denied
    row = await _require_member_server(community_id, server_id)
    return await bot_rcon.get_player_list(_dal(), community_id, server_id, row=row)


BLUEPRINTS: list[Blueprint] = [bot_bp]
