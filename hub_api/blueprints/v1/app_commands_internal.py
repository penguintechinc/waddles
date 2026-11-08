"""v1 internal data-plane API: slash-command desired state + per-user active context.

Consumed by the Discord connector (reconcile) and the ingest router (context). Machine JWT only,
operator-plane ``system`` tenant (``services.machine_auth``); the tenant is derived server-side from
the source's pairing, never accepted from the caller. Entities are referenced by public_id UUID
only (never numeric PKs or names); no PII.

* ``GET    /api/v1/internal/dataplane/app-commands/sources?platform=&limit=&offset=``
* ``GET    /api/v1/internal/dataplane/app-commands/desired?platform=&source_id=[&if_version=]``
* ``GET    /api/v1/internal/dataplane/active-context?platform=&source_id=&user=``
* ``PUT    /api/v1/internal/dataplane/active-context``
  body ``{platform, sourceId, user, community}``
* ``DELETE /api/v1/internal/dataplane/active-context?platform=&source_id=&user=``

Scopes: ``dataplane:app-commands`` (first two), ``dataplane:active-context`` (the rest). Every
refusal is ``no-store``; a successful read is cacheable for the router's ~1 minute.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, LiteralString, cast

from flask_core.api_utils import error_response
from penguin_dal import AsyncDB
from quart import Blueprint, Response, current_app, request
from quart_schema import validate_request, validate_response

from services import app_command_registry as registry
from services.dataplane_scope import (
    AmbiguousScopeError,
    InvalidSourceError,
    ScopeError,
)
from services.machine_auth import machine_authenticated

logger = logging.getLogger(__name__)

app_commands_internal_bp = Blueprint(
    "v1_app_commands_internal", __name__, url_prefix="/api/v1/internal/dataplane"
)

SCOPE_APP_COMMANDS = "dataplane:app-commands"
SCOPE_ACTIVE_CONTEXT = "dataplane:active-context"
CACHE_TTL_SECONDS = 60
DEFAULT_LIMIT = 100


@dataclass(slots=True, frozen=True)
class SlashCommandDTO:
    """One command to register; ``name``/``description`` is the payload, the rest is context."""

    name: str
    description: str
    builtin: bool
    communityPublicIds: list[str] = field(default_factory=list)  # empty = tenant-wide
    appPublicIds: list[str] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class SlashDesiredDTO:
    """Desired state for one source. ``changed`` False (``if_version`` matched) carries no list."""

    success: bool
    version: str
    changed: bool
    tenantPublicId: str
    platform: str
    sourceId: str
    truncated: bool
    commands: list[SlashCommandDTO] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class SlashSourcesDTO:
    """One page of reconcile-eligible source ids."""

    success: bool
    items: list[str]
    total: int
    limit: int
    offset: int


@dataclass(slots=True, frozen=True)
class SlashCommunityDTO:
    """A community switchable on the source (``position`` 0 = first-registered default)."""

    publicId: str
    displayName: str
    position: int


@dataclass(slots=True, frozen=True)
class SlashContextDTO:
    """A user's active community for a source."""

    success: bool
    tenantPublicId: str
    communityPublicId: str
    communityName: str
    explicit: bool
    communities: list[SlashCommunityDTO] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class SlashSetContextRequest:
    """Body of the PUT: ``community`` is the community ``public_id`` UUID (names never resolve)."""

    platform: str
    sourceId: str
    user: str
    community: str


@dataclass(slots=True, frozen=True)
class SlashClearDTO:
    """Result of a DELETE (``cleared`` False = there was no explicit choice)."""

    success: bool
    cleared: bool


def _auth(scope: str, tag: LiteralString) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Machine-JWT guard requiring ``scope`` (event names are bounded-literal log keys)."""
    return machine_authenticated(
        scope,
        unconfigured_event=f"app_commands.{tag}_auth_unconfigured",
        rejected_event=f"app_commands.{tag}_auth_rejected",
        wrong_tenant_event=f"app_commands.{tag}_auth_wrong_tenant",
    )


_commands_auth = _auth(SCOPE_APP_COMMANDS, "desired")
_context_auth = _auth(SCOPE_ACTIVE_CONTEXT, "context")


@app_commands_internal_bp.after_request
async def _cache_policy(response: Response) -> Response:
    """Short private TTL on a successful GET; ``no-store`` on everything else."""
    if response.status_code == 200 and request.method == "GET":
        response.headers["Cache-Control"] = f"private, max-age={CACHE_TTL_SECONDS}"
    else:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    return response


def _dal() -> AsyncDB:
    return cast(AsyncDB, current_app.config["install_dal"])


def _refusal(exc: Exception, op: str) -> tuple[dict[str, object], int]:
    """Map a registry refusal to the JSON error envelope (fixed, application-authored text).

    Logs only the static op, exception class and stable code -- never ``exc_info`` or the
    exception text, so no user/source/community value reaches the logs via a traceback.
    """
    if isinstance(exc, InvalidSourceError):
        status, code, message = (
            400,
            "BAD_REQUEST",
            "platform, source_id, user or community is not valid",
        )
    elif isinstance(exc, AmbiguousScopeError):
        status, code, message = 409, "SCOPE_AMBIGUOUS", "source is paired across tenants"
    elif isinstance(exc, registry.ContextNotFoundError):
        status, code, message = 404, "CONTEXT_NOT_FOUND", "community is not paired to this source"
    else:
        status, code, message = 404, "SCOPE_NOT_FOUND", "no community is linked to this source"
    logger.warning("slash_refused op=%s type=%s code=%s", op, type(exc).__name__, code)
    return error_response(message, status, code)  # type: ignore[no-any-return]


def _int_arg(name: str, default: int) -> int:
    """Parse a non-negative integer query arg; garbage raises :class:`InvalidSourceError`."""
    raw = request.args.get(name)
    if raw is None:
        return default
    if not (raw.isascii() and raw.isdigit()) or len(raw) > 9:
        raise InvalidSourceError(f"{name} is not a non-negative integer")
    return int(raw)


@app_commands_internal_bp.route("/app-commands/sources", methods=["GET"])
@_commands_auth
@validate_response(SlashSourcesDTO)
async def list_sources_route() -> Any:
    """``GET`` -- one page of sources of a platform that have a paired community."""
    try:
        page = await registry.list_sources(
            _dal(),
            request.args.get("platform", ""),
            limit=_int_arg("limit", DEFAULT_LIMIT),
            offset=_int_arg("offset", 0),
        )
    except ScopeError as exc:
        return _refusal(exc, "list_sources")
    return SlashSourcesDTO(True, list(page.items), page.total, page.limit, page.offset)


@app_commands_internal_bp.route("/app-commands/desired", methods=["GET"])
@_commands_auth
@validate_response(SlashDesiredDTO)
async def desired_route() -> Any:
    """``GET`` -- the desired slash registration of one source, with its idempotency version."""
    try:
        state = await registry.desired_commands(
            _dal(), request.args.get("platform", ""), request.args.get("source_id", "")
        )
    except ScopeError as exc:
        return _refusal(exc, "desired")
    unchanged = request.args.get("if_version") == state.version
    return SlashDesiredDTO(
        success=True,
        version=state.version,
        changed=not unchanged,
        tenantPublicId=state.tenant_public_id,
        platform=state.platform,
        sourceId=state.source_id,
        truncated=state.truncated,
        commands=[]
        if unchanged
        else [
            SlashCommandDTO(
                c.name,
                c.description,
                c.builtin,
                list(c.community_public_ids),
                list(c.app_public_ids),
            )
            for c in state.commands
        ],
    )


def _context_dto(ctx: registry.ActiveContext) -> SlashContextDTO:
    return SlashContextDTO(
        success=True,
        tenantPublicId=ctx.tenant_public_id,
        communityPublicId=ctx.community_public_id,
        communityName=ctx.community_name,
        explicit=ctx.explicit,
        communities=[SlashCommunityDTO(c.public_id, c.name, c.position) for c in ctx.communities],
    )


@app_commands_internal_bp.route("/active-context", methods=["GET"])
@_context_auth
@validate_response(SlashContextDTO)
async def get_context_route() -> Any:
    """``GET`` -- the user's active community (default: first-registered) + switchable list."""
    try:
        ctx = await registry.get_active_context(
            _dal(),
            request.args.get("platform", ""),
            request.args.get("source_id", ""),
            request.args.get("user", ""),
        )
    except (ScopeError, registry.ContextNotFoundError) as exc:
        return _refusal(exc, "get_context")
    return _context_dto(ctx)


@app_commands_internal_bp.route("/active-context", methods=["PUT"])
@_context_auth
@validate_request(SlashSetContextRequest)
@validate_response(SlashContextDTO)
async def set_context_route(data: SlashSetContextRequest) -> Any:
    """``PUT`` -- switch the user's active community (community public_id UUID)."""
    try:
        ctx = await registry.set_active_context(
            _dal(), data.platform, data.sourceId, data.user, data.community
        )
    except (ScopeError, registry.ContextNotFoundError) as exc:
        return _refusal(exc, "set_context")
    return _context_dto(ctx)


@app_commands_internal_bp.route("/active-context", methods=["DELETE"])
@_context_auth
@validate_response(SlashClearDTO)
async def clear_context_route() -> Any:
    """``DELETE`` -- drop the user's explicit choice (revert to the source default)."""
    try:
        cleared = await registry.clear_active_context(
            _dal(),
            request.args.get("platform", ""),
            request.args.get("source_id", ""),
            request.args.get("user", ""),
        )
    except ScopeError as exc:
        return _refusal(exc, "clear_context")
    return SlashClearDTO(True, cleared)


BLUEPRINTS = [app_commands_internal_bp]
