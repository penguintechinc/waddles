"""v1 generic bundle-admin gateway -- the "site foreman" path every app bundle's admin UI uses.

Mount: `/api/v1/apps/community/<community_id>/bundles/<app_id>/...`. Nothing here knows any
bundle: config schema, admin tools and data views all come from the ACTIVE bundle's manifest
(`community_config_schema`, `admin_tools`) -- see docs/BUNDLE_ADMIN_GATEWAY.md.

| Route | Needs |
|---|---|
| `GET  /admin` | community moderator (descriptor: schema + tools the caller may use) |
| `GET  /config`, `GET /config/history` | community moderator |
| `PUT  /config` | community admin; body validated against `community_config_schema` |
| `POST /admin-actions/<tool>` | the scope the manifest declares for that tool |
| `GET  /data/<view>` | the scope the manifest declares; paginated read tool |

Authz order (security.md): `tenant_middleware` first, then DB-backed community membership
(`authorize_community`, tenant-ownership re-checked), then the manifest-declared community scope.
There is deliberately no flat JWT scope: a community admin never carries one at issuance (see
`services/community_authz.py`). Every route is rate limited per user/community/bundle/tool and
fails closed (503) when no limiter is configured on the app.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.tenancy import TenantContext, get_tenant_context, tenant_middleware
from penguin_dal import AsyncDB
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services import bundle_admin_gateway_service as svc
from services.bundle_tool_transport import ToolTransport, ValkeyToolTransport
from services.community_authz import CommunityMembership, authorize_community
from services.current_user import get_current_user_id
from services.errors import ApiError
from services.rate_limiting import RATE_LIMITER_CONFIG_KEY

logger = logging.getLogger(__name__)

bundle_admin_gateway_bp = Blueprint(
    "v1_bundle_admin_gateway",
    __name__,
    url_prefix="/api/v1/apps/community/<int:community_id>/bundles/<app_id>",
)

_APP_ID_RE = re.compile(r"^waddles\.[a-z0-9_-]+\.[a-z0-9_-]+\.[a-z0-9_-]+$")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")
#: Requests per minute per (user, community, bundle, tool).
ACTION_RATE_PER_MIN = 30
READ_RATE_PER_MIN = 120
_RATE_WINDOW_S = 60
_MAX_QUERY_ARGS = 4096


def _err(exc: ApiError) -> tuple[Any, int]:
    return cast(
        tuple[Any, int],
        error_response(exc.message, exc.status_code, exc.code, exc.details),
    )


def _install_dal() -> AsyncDB:
    return cast(AsyncDB, current_app.config["install_dal"])


def _transport() -> ToolTransport:
    """The configured transport, or the signed Valkey one (keys are read lazily, fail loud)."""
    configured = current_app.config.get("bundle_tool_transport")
    return cast(ToolTransport, configured) if configured is not None else ValkeyToolTransport()


async def _rate_limit(user_id: int, community_id: int, app_id: str, name: str, limit: int) -> None:
    """Per-tool sliding-window limit; a missing limiter is a 503, never an open door."""
    limiter = current_app.config.get(RATE_LIMITER_CONFIG_KEY)
    if limiter is None:
        raise ApiError("rate limiter unavailable", 503, "rate_limiter_unavailable")
    allowed = await limiter.check_rate_limit(
        f"bundle_admin:{user_id}:{community_id}:{app_id}:{name}", limit, _RATE_WINDOW_S
    )
    if not allowed:
        raise ApiError("rate limit exceeded", 429, "RATE_LIMIT_EXCEEDED")


@dataclass(slots=True, frozen=True)
class Gate:
    """Everything a route needs after the authz chain ran."""

    ctx: TenantContext
    membership: CommunityMembership
    bundle: svc.ActiveBundle
    user_id: int


async def _gate(community_id: int, app_id: str, *, name: str, limit: int) -> Gate:
    """Tenant -> membership -> rate limit -> active-bundle resolution (each fails closed)."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101 - tenant_middleware ran first
    if not _APP_ID_RE.fullmatch(app_id):
        raise ApiError("invalid app id", 400, "BAD_REQUEST")
    async_dal, dal = current_app.config["async_dal"], current_app.config["dal"]
    membership = await authorize_community(
        request, async_dal, dal, community_id=community_id, admin=False
    )
    user_id = get_current_user_id(request)
    await _rate_limit(user_id, community_id, app_id, name, limit)
    bundle = await svc.load_active_bundle(
        _install_dal(), tenant_id=ctx.tenant_id, community_id=community_id, app_id=app_id
    )
    return Gate(ctx=ctx, membership=membership, bundle=bundle, user_id=user_id)


def _check_name(name: str) -> None:
    if not _NAME_RE.fullmatch(name):
        raise ApiError("invalid tool name", 400, "BAD_REQUEST")


@dataclass(slots=True, frozen=True)
class ToolDTO:
    """One admin tool the manifest declares, and whether this caller may use it."""

    name: str
    kind: str
    scope: str
    permitted: bool
    targetAppId: str


@dataclass(slots=True, frozen=True)
class DescriptorResponse:
    """Response DTO for `GET .../admin`."""

    success: bool
    appId: str
    version: str
    configSchema: dict[str, Any] | None
    tools: list[ToolDTO]


@dataclass(slots=True, frozen=True)
class ConfigResponse:
    """Response DTO for `GET/PUT .../config` (`version` 0 = never saved)."""

    success: bool
    appId: str
    version: int
    config: dict[str, Any]
    updatedBy: int | None
    createdAt: str | None


@dataclass(slots=True, frozen=True)
class ConfigHistoryEntryDTO:
    """One saved config version (metadata only)."""

    version: int
    updatedBy: int | None
    createdAt: str | None


@dataclass(slots=True, frozen=True)
class ConfigHistoryResponse:
    """Response DTO for `GET .../config/history`."""

    success: bool
    versions: list[ConfigHistoryEntryDTO]


@dataclass(slots=True, frozen=True)
class SaveConfigRequest:
    """Request DTO for `PUT .../config`; `expectedVersion` is the optimistic-concurrency token."""

    config: dict[str, Any]
    expectedVersion: int


@dataclass(slots=True, frozen=True)
class ActionRequest:
    """Request DTO for `POST .../admin-actions/<tool>`; `args` is checked against its schema."""

    args: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class ToolResponse:
    """Response DTO for admin actions and data views; `result` matches the tool `output_schema`."""

    success: bool
    result: dict[str, Any]
    limit: int | None = None
    nextCursor: str | None = None


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


def _config_dto(app_id: str, snap: svc.ConfigSnapshot) -> ConfigResponse:
    return ConfigResponse(
        success=True,
        appId=app_id,
        version=snap.version,
        config=snap.config,
        updatedBy=snap.updated_by,
        createdAt=_iso(snap.created_at),
    )


@bundle_admin_gateway_bp.route("/admin", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@validate_response(DescriptorResponse)
async def get_descriptor(community_id: int, app_id: str) -> DescriptorResponse | tuple[Any, int]:
    """Config schema + admin tools for the bundle's admin UI (community moderator+)."""
    try:
        gate = await _gate(community_id, app_id, name="_admin", limit=READ_RATE_PER_MIN)
        svc_scope = "community:moderate"
        if not svc.caller_holds(gate.membership, svc_scope):
            return _err(ApiError("community:moderate required", 403, "FORBIDDEN"))
        d = svc.describe(gate.bundle, gate.membership)
    except ApiError as exc:
        logger.warning(
            "bundle_admin_refused op=%s status=%s code=%s", "descriptor", exc.status_code, exc.code
        )
        return _err(exc)
    return DescriptorResponse(
        success=True,
        appId=d["appId"],
        version=d["version"],
        configSchema=d["configSchema"],
        tools=[ToolDTO(**t) for t in d["tools"]],
    )


@bundle_admin_gateway_bp.route("/config", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@validate_response(ConfigResponse)
async def get_config(community_id: int, app_id: str) -> ConfigResponse | tuple[Any, int]:
    """Latest saved per-community config (community moderator+)."""
    try:
        gate = await _gate(community_id, app_id, name="_config", limit=READ_RATE_PER_MIN)
        snap = await svc.get_config(
            _install_dal(),
            gate.bundle,
            gate.membership,
            tenant_id=gate.ctx.tenant_id,
            community_id=community_id,
        )
    except ApiError as exc:
        logger.warning(
            "bundle_admin_refused op=%s status=%s code=%s", "get_config", exc.status_code, exc.code
        )
        return _err(exc)
    return _config_dto(app_id, snap)


@bundle_admin_gateway_bp.route("/config", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@validate_request(SaveConfigRequest)
@validate_response(ConfigResponse)
async def put_config(
    data: SaveConfigRequest, community_id: int, app_id: str
) -> ConfigResponse | tuple[Any, int]:
    """Validate against the manifest `community_config_schema`; append a new version (admin)."""
    try:
        gate = await _gate(community_id, app_id, name="_config_put", limit=ACTION_RATE_PER_MIN)
        snap = await svc.save_config(
            _install_dal(),
            gate.bundle,
            gate.membership,
            tenant_id=gate.ctx.tenant_id,
            community_id=community_id,
            actor_id=gate.user_id,
            config=data.config,
            expected_version=data.expectedVersion,
        )
    except ApiError as exc:
        logger.warning(
            "bundle_admin_refused op=%s status=%s code=%s", "put_config", exc.status_code, exc.code
        )
        return _err(exc)
    return _config_dto(app_id, snap)


@bundle_admin_gateway_bp.route("/config/history", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@validate_response(ConfigHistoryResponse)
async def get_config_history(
    community_id: int, app_id: str
) -> ConfigHistoryResponse | tuple[Any, int]:
    """Saved config versions, newest first (metadata only; `?limit=` 1..100)."""
    try:
        limit = int(request.args.get("limit", str(svc.DEFAULT_PAGE_LIMIT)))
    except ValueError:
        logger.warning("bundle_admin_bad_limit op=config_history")
        return _err(ApiError("limit must be an integer", 400, "BAD_REQUEST"))
    try:
        gate = await _gate(community_id, app_id, name="_config_history", limit=READ_RATE_PER_MIN)
        entries = await svc.list_config_history(
            _install_dal(),
            gate.bundle,
            gate.membership,
            tenant_id=gate.ctx.tenant_id,
            community_id=community_id,
            limit=limit,
        )
    except ApiError as exc:
        logger.warning(
            "bundle_admin_refused op=%s status=%s code=%s",
            "config_history",
            exc.status_code,
            exc.code,
        )
        return _err(exc)
    return ConfigHistoryResponse(
        success=True,
        versions=[
            ConfigHistoryEntryDTO(e.version, e.updated_by, _iso(e.created_at)) for e in entries
        ],
    )


@bundle_admin_gateway_bp.route("/admin-actions/<tool_name>", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@validate_request(ActionRequest)
@validate_response(ToolResponse)
async def invoke_action(
    data: ActionRequest, community_id: int, app_id: str, tool_name: str
) -> ToolResponse | tuple[Any, int]:
    """Invoke a manifest-declared admin action tool (scope per the manifest)."""
    try:
        _check_name(tool_name)
        gate = await _gate(community_id, app_id, name=tool_name, limit=ACTION_RATE_PER_MIN)
        out = await svc.invoke_admin_action(
            _install_dal(),
            _transport(),
            gate.bundle,
            gate.membership,
            tool_name=tool_name,
            tenant_id=gate.ctx.tenant_id,
            community_id=community_id,
            actor_id=gate.user_id,
            args=data.args,
        )
    except ApiError as exc:
        logger.warning(
            "bundle_admin_refused op=%s status=%s code=%s",
            "admin_action",
            exc.status_code,
            exc.code,
        )
        return _err(exc)
    return ToolResponse(success=True, result=out.result)


@bundle_admin_gateway_bp.route("/data/<view_name>", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@validate_response(ToolResponse)
async def read_view(
    community_id: int, app_id: str, view_name: str
) -> ToolResponse | tuple[Any, int]:
    """Paginated bundle data view: `?limit=&cursor=&args=<url-encoded JSON object>`."""
    try:
        _check_name(view_name)
        raw_args = request.args.get("args", "{}")
        if len(raw_args) > _MAX_QUERY_ARGS:
            raise ApiError("args too large", 413, "args_too_large")
        try:
            args = json.loads(raw_args)
            raw_limit = request.args.get("limit")
            limit = int(raw_limit) if raw_limit is not None else None
        except ValueError as exc:
            raise ApiError(
                "args must be a JSON object, limit an integer", 400, "BAD_REQUEST"
            ) from exc
        if not isinstance(args, dict):
            raise ApiError("args must be a JSON object", 400, "BAD_REQUEST")
        gate = await _gate(community_id, app_id, name=view_name, limit=READ_RATE_PER_MIN)
        out = await svc.read_data_view(
            _install_dal(),
            _transport(),
            gate.bundle,
            gate.membership,
            view_name=view_name,
            tenant_id=gate.ctx.tenant_id,
            community_id=community_id,
            actor_id=gate.user_id,
            args=args,
            limit=limit,
            cursor=request.args.get("cursor"),
        )
    except ApiError as exc:
        logger.warning(
            "bundle_admin_refused op=%s status=%s code=%s", "data_view", exc.status_code, exc.code
        )
        return _err(exc)
    return ToolResponse(
        success=True, result=out.result, limit=out.limit, nextCursor=out.next_cursor
    )


BLUEPRINTS: list[Blueprint] = [bundle_admin_gateway_bp]
