"""v1 `tenant` group -- ported from `tenantController.js` (migration plan phase M2).

Full port of `routes/tenant.js`'s contract: tenant entity CRUD, tenant
settings, tenant-scoped community listing, tenant module allowlist, and
tenant admin management. Every path below is IDENTICAL to
`admin/hub_module/backend/src/routes/tenant.js` mounted at
`/api/v1/tenant` (verified against `frontend/src/services/api.js`'s
`tenantApi` -- see `hub_api/PORTING.md`).

Every route: `tenant_middleware` (resolves `TenantContext` from the
caller's OWN JWT `tenant` claim) -> `require_scope("tenant:admin")`
(matches Node's blanket `requireTenantAdmin` gate on every route in
`tenant.js`, no read/write split in the source contract) ->
`services.tenant_service.require_matching_tenant` (403 if the URL's
`tenant_slug` doesn't match `ctx.tenant_slug`) -> the handler, which uses
`ctx.tenant_id` for every query, NEVER the URL's `tenant_slug`. See
`services/tenant_service.py`'s module docstring for why that last check
exists and what it closes off relative to Node's own (bugged) auth chain.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services import branding_service
from services import tenant_service as svc
from services.errors import ApiError, payment_required
from services.pagination import parse_limit
from services.schema import bind_tenant_tables

logger = logging.getLogger(__name__)

tenant_bp = Blueprint("v1_tenant", __name__, url_prefix="/api/v1/tenant")


def _dal() -> tuple[Any, Any]:
    """Return `(async_dal, dal)` from app config, extending `tenants`/`communities` on first use.

    See `services/schema.py::bind_tenant_tables`'s docstring for why this
    group binds its tables lazily here rather than from
    `app.py::_bind_reference_tables` -- this port's task scopes `app.py`
    as never-edit. Idempotent and cheap after the first call.
    """
    async_dal, dal = current_app.config["async_dal"], current_app.config["dal"]
    bind_tenant_tables(dal)
    return async_dal, dal


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    """Convert an `ApiError` into the flask_core `error_response()` JSON envelope."""
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


def _tenant_id(tenant_slug: str) -> int:
    """Validate the URL's `tenant_slug` against the caller's own `TenantContext`, return its id.

    Raises via `ApiError` (caught by each route the same way service-layer
    errors are) rather than returning an error tuple directly, so callers
    can use it as a one-line precondition before touching the DB.
    """
    ctx = get_tenant_context(request)
    # tenant_middleware already returned 401/403 and short-circuited before
    # this line if ctx were ever None -- a mypy type-narrowing aid, not a
    # runtime security control (worst case if stripped under -O: an
    # AttributeError -> 500, never an auth bypass). Same pattern as
    # blueprints/v2/platform.py.
    assert ctx is not None  # nosec B101
    svc.require_matching_tenant(tenant_slug, ctx.tenant_slug)
    return cast(int, ctx.tenant_id)


# ---------------------------------------------------------------------------
# DTOs -- camelCase field names are deliberate (wire contract pinned to
# frontend/src/services/api.js's tenantApi -- see blueprints/v1/auth.py's
# own DTO section docstring for the full rationale).
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class TenantDTO:
    """Tenant DTO."""

    id: int
    slug: str
    displayName: str | None
    description: str | None
    logoUrl: str | None
    isGlobal: bool
    isActive: bool
    config: dict[str, Any]
    allowedModuleIds: list[int] | None
    seatLimit: int | None
    createdAt: str | None


@dataclass(slots=True, frozen=True)
class TenantResponse:
    """Response DTO for `GET /<tenant_slug>`."""

    success: bool
    tenant: TenantDTO


@dataclass(slots=True, frozen=True)
class UpdateTenantRequest:
    """Request DTO for `PUT /<tenant_slug>`."""

    displayName: str | None = None
    description: str | None = None
    logoUrl: str | None = None
    config: dict[str, Any] | None = None


@dataclass(slots=True, frozen=True)
class MessageResponse:
    """Response DTO for message-only endpoints."""

    success: bool
    message: str


@dataclass(slots=True, frozen=True)
class SettingDTO:
    """Tenant setting DTO."""

    key: str
    value: str | None


@dataclass(slots=True, frozen=True)
class SettingsResponse:
    """Response DTO for `GET /<tenant_slug>/settings`."""

    success: bool
    settings: list[SettingDTO]


@dataclass(slots=True, frozen=True)
class SettingInput:
    """A single `{key, value}` pair in `UpdateSettingsRequest.settings`."""

    key: str
    value: str | None = None


@dataclass(slots=True, frozen=True)
class UpdateSettingsRequest:
    """Request DTO for `PUT /<tenant_slug>/settings`."""

    settings: list[SettingInput]


@dataclass(slots=True, frozen=True)
class CommunityDTO:
    """Community summary DTO."""

    id: int
    name: str
    displayName: str
    memberCount: int
    isActive: bool
    isPublic: bool
    communityType: str
    createdAt: str | None


@dataclass(slots=True, frozen=True)
class PaginationDTO:
    """Pagination DTO."""

    page: int
    limit: int
    total: int
    totalPages: int


@dataclass(slots=True, frozen=True)
class CommunitiesResponse:
    """Response DTO for `GET /<tenant_slug>/communities`."""

    success: bool
    communities: list[CommunityDTO]
    pagination: PaginationDTO


@dataclass(slots=True, frozen=True)
class ModuleDTO:
    """Module summary DTO."""

    id: int
    name: str
    displayName: str | None
    description: str | None
    category: str | None
    isCore: bool
    isPublished: bool
    version: str | None
    createdAt: str | None


@dataclass(slots=True, frozen=True)
class ModulesResponse:
    """Response DTO for `GET /<tenant_slug>/modules`."""

    success: bool
    allModulesAllowed: bool
    modules: list[ModuleDTO]


@dataclass(slots=True, frozen=True)
class UpdateModulesRequest:
    """Request DTO for `PUT /<tenant_slug>/modules`."""

    allowedModuleIds: list[int] | None


@dataclass(slots=True, frozen=True)
class TenantAdminDTO:
    """Tenant admin DTO."""

    userId: int
    displayName: str | None
    email: str | None
    role: str
    createdAt: str | None


@dataclass(slots=True, frozen=True)
class TenantAdminsResponse:
    """Response DTO for `GET /<tenant_slug>/admins`."""

    success: bool
    admins: list[TenantAdminDTO]


@dataclass(slots=True, frozen=True)
class AddTenantAdminRequest:
    """Request DTO for `POST /<tenant_slug>/admins`."""

    userId: int
    role: str = "tenant-admin"


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


def _tenant_dto(row: Any) -> TenantDTO:
    return TenantDTO(
        id=row.id,
        slug=row.slug,
        displayName=row.display_name,
        description=row.description,
        logoUrl=row.logo_url,
        isGlobal=bool(row.is_global),
        isActive=bool(row.is_active),
        config=row.config or {},
        allowedModuleIds=row.allowed_module_ids,
        seatLimit=row.seat_limit,
        createdAt=_iso(row.created_at),
    )


def _community_dto(row: Any) -> CommunityDTO:
    return CommunityDTO(
        id=row.id,
        name=row.name,
        displayName=row.display_name or row.name,
        memberCount=row.member_count or 0,
        isActive=bool(row.is_active),
        isPublic=bool(row.is_public),
        communityType=row.community_type or "other",
        createdAt=_iso(row.created_at),
    )


def _module_dto(row: Any) -> ModuleDTO:
    return ModuleDTO(
        id=row.id,
        name=row.name,
        displayName=row.display_name,
        description=row.description,
        category=row.category,
        isCore=bool(row.is_core),
        isPublished=bool(row.is_published),
        version=row.version,
        createdAt=_iso(row.created_at),
    )


def _admin_dto(row: Any) -> TenantAdminDTO:
    ta = row.tenant_admins
    user = row.hub_users
    return TenantAdminDTO(
        userId=ta.user_id,
        displayName=user.display_name or user.username,
        email=user.email,
        role=ta.role,
        createdAt=_iso(ta.created_at),
    )


@tenant_bp.route("/<tenant_slug>", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:admin")  # type: ignore[untyped-decorator]
@validate_response(TenantResponse)
async def get_tenant(tenant_slug: str) -> TenantResponse | tuple[dict[str, object], int]:
    """Get tenant."""
    async_dal, dal = _dal()
    try:
        tenant_id = _tenant_id(tenant_slug)
        row = await svc.get_tenant(async_dal, dal, tenant_id=tenant_id)
    except ApiError as exc:
        return _err(exc)
    return TenantResponse(success=True, tenant=_tenant_dto(row))


@tenant_bp.route("/<tenant_slug>", methods=["PUT"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:admin")  # type: ignore[untyped-decorator]
@validate_request(UpdateTenantRequest)
@validate_response(MessageResponse)
async def update_tenant(
    data: UpdateTenantRequest, tenant_slug: str
) -> MessageResponse | tuple[dict[str, object], int]:
    """Update tenant."""
    async_dal, dal = _dal()
    try:
        tenant_id = _tenant_id(tenant_slug)
        if data.logoUrl is not None or data.config is not None:
            # Whitelabel gate (Professional, `tenancy.whitelabel`): saving a
            # a new non-empty branding value (logo/theme/welcome message) needs
            # entitlement; unrelated edits and clearing never do. Fail-closed 402.
            current = await svc.get_tenant(async_dal, dal, tenant_id=tenant_id)
            if branding_service.sets_branding(
                current_logo_url=current.logo_url,
                current_config=current.config,
                new_logo_url=data.logoUrl,
                new_config=data.config,
            ) and not await branding_service.whitelabel_enabled(tenant_slug):
                raise payment_required(
                    "Custom tenant branding requires a Professional plan or higher"
                )
        await svc.update_tenant(
            async_dal,
            dal,
            tenant_id=tenant_id,
            display_name=data.displayName,
            description=data.description,
            logo_url=data.logoUrl,
            config=data.config,
        )
    except ApiError as exc:
        # Includes the whitelabel 402 (Professional gate): log the rejection (no
        # branding values -- they may embed customer-supplied URLs) before returning.
        logger.info(
            "tenant.update_rejected",
            extra={"status": exc.status_code, "code": exc.code},
        )
        return _err(exc)
    return MessageResponse(success=True, message="Tenant updated")


@tenant_bp.route("/<tenant_slug>/settings", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:admin")  # type: ignore[untyped-decorator]
@validate_response(SettingsResponse)
async def get_tenant_settings(
    tenant_slug: str,
) -> SettingsResponse | tuple[dict[str, object], int]:
    """Get tenant settings."""
    async_dal, dal = _dal()
    try:
        tenant_id = _tenant_id(tenant_slug)
        rows = await svc.get_tenant_settings(async_dal, dal, tenant_id=tenant_id)
    except ApiError as exc:
        return _err(exc)
    return SettingsResponse(
        success=True, settings=[SettingDTO(key=r.key, value=r.value) for r in rows]
    )


@tenant_bp.route("/<tenant_slug>/settings", methods=["PUT"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:admin")  # type: ignore[untyped-decorator]
@validate_request(UpdateSettingsRequest)
@validate_response(MessageResponse)
async def update_tenant_settings(
    data: UpdateSettingsRequest, tenant_slug: str
) -> MessageResponse | tuple[dict[str, object], int]:
    """Update tenant settings."""
    async_dal, dal = _dal()
    try:
        tenant_id = _tenant_id(tenant_slug)
        await svc.update_tenant_settings(
            async_dal,
            dal,
            tenant_id=tenant_id,
            settings=[(s.key, s.value) for s in data.settings],
        )
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message="Tenant settings updated")


@tenant_bp.route("/<tenant_slug>/communities", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:admin")  # type: ignore[untyped-decorator]
@validate_response(CommunitiesResponse)
async def get_tenant_communities(
    tenant_slug: str,
) -> CommunitiesResponse | tuple[dict[str, object], int]:
    """Get tenant communities."""
    async_dal, dal = _dal()
    page = int(request.args.get("page", "1"))
    limit = parse_limit(request.args.get("limit"), default=25)
    try:
        tenant_id = _tenant_id(tenant_slug)
        rows, total, total_pages = await svc.get_tenant_communities(
            async_dal, dal, tenant_id=tenant_id, page=page, limit=limit
        )
    except ApiError as exc:
        return _err(exc)
    return CommunitiesResponse(
        success=True,
        communities=[_community_dto(r) for r in rows],
        pagination=PaginationDTO(
            page=max(1, page), limit=min(100, max(1, limit)), total=total, totalPages=total_pages
        ),
    )


@tenant_bp.route("/<tenant_slug>/modules", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:admin")  # type: ignore[untyped-decorator]
@validate_response(ModulesResponse)
async def get_tenant_modules(tenant_slug: str) -> ModulesResponse | tuple[dict[str, object], int]:
    """Get tenant modules."""
    async_dal, dal = _dal()
    try:
        tenant_id = _tenant_id(tenant_slug)
        all_allowed, rows = await svc.get_tenant_modules(async_dal, dal, tenant_id=tenant_id)
    except ApiError as exc:
        return _err(exc)
    return ModulesResponse(
        success=True,
        allModulesAllowed=all_allowed,
        modules=[_module_dto(r) for r in rows],
    )


@tenant_bp.route("/<tenant_slug>/modules", methods=["PUT"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:admin")  # type: ignore[untyped-decorator]
@validate_request(UpdateModulesRequest)
@validate_response(MessageResponse)
async def update_tenant_modules(
    data: UpdateModulesRequest, tenant_slug: str
) -> MessageResponse | tuple[dict[str, object], int]:
    """Update tenant modules."""
    async_dal, dal = _dal()
    try:
        tenant_id = _tenant_id(tenant_slug)
        await svc.update_tenant_modules(
            async_dal, dal, tenant_id=tenant_id, allowed_module_ids=data.allowedModuleIds
        )
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message="Tenant modules updated")


@tenant_bp.route("/<tenant_slug>/admins", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:admin")  # type: ignore[untyped-decorator]
@validate_response(TenantAdminsResponse)
async def get_tenant_admins(
    tenant_slug: str,
) -> TenantAdminsResponse | tuple[dict[str, object], int]:
    """Get tenant admins."""
    async_dal, dal = _dal()
    try:
        tenant_id = _tenant_id(tenant_slug)
        rows = await svc.get_tenant_admins(async_dal, dal, tenant_id=tenant_id)
    except ApiError as exc:
        return _err(exc)
    return TenantAdminsResponse(success=True, admins=[_admin_dto(r) for r in rows])


@tenant_bp.route("/<tenant_slug>/admins", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:admin")  # type: ignore[untyped-decorator]
@validate_request(AddTenantAdminRequest)
@validate_response(MessageResponse)
async def add_tenant_admin(
    data: AddTenantAdminRequest, tenant_slug: str
) -> MessageResponse | tuple[dict[str, object], int]:
    """Add tenant admin."""
    async_dal, dal = _dal()
    try:
        tenant_id = _tenant_id(tenant_slug)
        await svc.add_tenant_admin(
            async_dal, dal, tenant_id=tenant_id, user_id=data.userId, role=data.role
        )
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message="Tenant admin added")


@tenant_bp.route("/<tenant_slug>/admins/<int:user_id>", methods=["DELETE"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:admin")  # type: ignore[untyped-decorator]
@validate_response(MessageResponse)
async def remove_tenant_admin(
    tenant_slug: str, user_id: int
) -> MessageResponse | tuple[dict[str, object], int]:
    """Remove tenant admin."""
    async_dal, dal = _dal()
    try:
        tenant_id = _tenant_id(tenant_slug)
        await svc.remove_tenant_admin(async_dal, dal, tenant_id=tenant_id, user_id=user_id)
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message="Tenant admin removed")


BLUEPRINTS: list[Blueprint] = [tenant_bp]
