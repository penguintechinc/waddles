"""v1 `superadmin` (cross-tenant platform-admin) group -- ported from `superadminController.js`.

Mounted at `/api/v1/superadmin/*` (matches `routes/superadmin.js`, minus
`/users*` -- already owned by M1's `blueprints/v1/user_management.py`,
blueprint name `v1_superadmin_users`; this module uses the distinct name
`v1_superadmin_platform` to avoid a Quart blueprint-name collision, per
this PR's task brief). See `services/superadmin_service.py`'s module
docstring for the scope/auth rationale (`@tenant_middleware` +
`@require_scope(...)`, all three resource scopes covered by the
`global:admin` bundle's `*:admin` wildcard).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.tenancy import tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services import superadmin_service as svc
from services.current_user import get_current_user_id
from services.dto_response import jsonify_dto
from services.errors import ApiError
from services.pagination import parse_limit

superadmin_bp = Blueprint("v1_superadmin_platform", __name__, url_prefix="/api/v1/superadmin")


def _dal() -> tuple[Any, Any]:
    return current_app.config["async_dal"], current_app.config["dal"]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


# ── DTOs ─────────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class MessageResponse:
    """Response DTO for message endpoints."""

    success: bool
    message: str


@dataclass(slots=True, frozen=True)
class PlatformBreakdownDTO:
    """Per-platform community count DTO."""

    discord: int
    twitch: int
    slack: int


@dataclass(slots=True, frozen=True)
class DashboardStatsDTO:
    """Dashboard stats DTO."""

    totalCommunities: int
    activeCommunities: int
    platformBreakdown: PlatformBreakdownDTO
    totalMembers: int
    adminCount: int


@dataclass(slots=True, frozen=True)
class RecentCommunityDTO:
    """Recently-created community summary DTO."""

    id: int
    name: str
    displayName: str
    platform: str | None
    createdAt: str | None


@dataclass(slots=True, frozen=True)
class DashboardResponse:
    """Response DTO for dashboard stats."""

    success: bool
    stats: DashboardStatsDTO
    recentCommunities: list[RecentCommunityDTO]


@dataclass(slots=True, frozen=True)
class PaginationDTO:
    """Pagination DTO."""

    page: int
    limit: int
    total: int
    totalPages: int


@dataclass(slots=True, frozen=True)
class CommunityDTO:
    """Community DTO -- list-view shape (no `config`)."""

    id: int
    name: str
    displayName: str
    description: str | None
    platform: str | None
    platformServerId: str | None
    ownerId: str | None
    ownerName: str | None
    memberCount: int
    isActive: bool
    isPublic: bool
    communityType: str
    isPremium: bool
    seatLimit: int | None
    createdAt: str | None
    updatedAt: str | None


@dataclass(slots=True, frozen=True)
class CommunitiesResponse:
    """Response DTO for list-communities."""

    success: bool
    communities: list[CommunityDTO]
    pagination: PaginationDTO


@dataclass(slots=True, frozen=True)
class CommunityDetailDTO:
    """Community DTO -- detail-view shape (includes `config`)."""

    id: int
    name: str
    displayName: str
    description: str | None
    platform: str | None
    platformServerId: str | None
    ownerId: str | None
    ownerName: str | None
    memberCount: int
    isActive: bool
    isPublic: bool
    communityType: str
    config: dict[str, Any] | None
    createdAt: str | None
    updatedAt: str | None


@dataclass(slots=True, frozen=True)
class CommunityDetailResponse:
    """Response DTO for get-community."""

    success: bool
    community: CommunityDetailDTO


@dataclass(slots=True, frozen=True)
class CreateCommunityRequest:
    """Request DTO for create-community."""

    name: str
    platform: str
    displayName: str | None = None
    description: str | None = None
    platformServerId: str | None = None
    ownerId: str | None = None
    ownerName: str | None = None
    isPublic: bool | None = None
    communityType: str | None = None


@dataclass(slots=True, frozen=True)
class CreatedCommunityDTO:
    """Response DTO for a newly-created community."""

    id: int
    name: str
    displayName: str | None
    platform: str | None
    communityType: str
    createdAt: str | None


@dataclass(slots=True, frozen=True)
class CreateCommunityResponse:
    """Response DTO for create-community."""

    success: bool
    community: CreatedCommunityDTO


@dataclass(slots=True, frozen=True)
class UpdateCommunityRequest:
    """Request DTO for update-community."""

    displayName: str | None = None
    description: str | None = None
    ownerId: str | None = None
    ownerName: str | None = None
    isActive: bool | None = None
    isPublic: bool | None = None
    platform: str | None = None
    platformServerId: str | None = None
    communityType: str | None = None
    isPremium: bool | None = None
    seatLimit: int | None = None


@dataclass(slots=True, frozen=True)
class ReassignOwnerRequest:
    """Request DTO for reassign-owner."""

    newOwnerName: str
    newOwnerId: str | None = None


@dataclass(slots=True, frozen=True)
class ModuleDTO:
    """Marketplace module DTO -- list-view shape."""

    id: int
    name: str
    displayName: str
    description: str | None
    version: str | None
    author: str | None
    category: str | None
    iconUrl: str | None
    isPublished: bool
    isCore: bool
    avgRating: str
    reviewCount: int
    installCount: int
    createdAt: str | None


@dataclass(slots=True, frozen=True)
class ModulesResponse:
    """Response DTO for list-modules."""

    success: bool
    modules: list[ModuleDTO]
    pagination: PaginationDTO


@dataclass(slots=True, frozen=True)
class CreateModuleRequest:
    """Request DTO for create-module."""

    name: str
    displayName: str | None = None
    description: str | None = None
    version: str | None = None
    author: str | None = None
    category: str | None = None
    iconUrl: str | None = None
    isCore: bool | None = None
    configSchema: dict[str, Any] | None = None


@dataclass(slots=True, frozen=True)
class DbAccountDTO:
    """Module DB-account provisioning outcome DTO."""

    provisioned: bool
    username: str | None = None
    message: str | None = None


@dataclass(slots=True, frozen=True)
class CreatedModuleDTO:
    """Response DTO for a newly-created module."""

    id: int
    name: str
    displayName: str | None
    createdAt: str | None
    dbAccount: DbAccountDTO


@dataclass(slots=True, frozen=True)
class CreateModuleResponse:
    """Response DTO for create-module."""

    success: bool
    module: CreatedModuleDTO


@dataclass(slots=True, frozen=True)
class UpdateModuleRequest:
    """Request DTO for update-module."""

    displayName: str | None = None
    description: str | None = None
    version: str | None = None
    author: str | None = None
    category: str | None = None
    iconUrl: str | None = None
    isCore: bool | None = None
    configSchema: dict[str, Any] | None = None


@dataclass(slots=True, frozen=True)
class PublishModuleRequest:
    """Request DTO for publish-module."""

    isPublished: bool


@dataclass(slots=True, frozen=True)
class TenantDTO:
    """Tenant DTO."""

    id: int
    slug: str
    displayName: str
    description: str | None
    logoUrl: str | None
    isGlobal: bool
    isActive: bool
    allowedModuleIds: list[int] | None
    seatLimit: int | None
    createdAt: str | None
    updatedAt: str | None


@dataclass(slots=True, frozen=True)
class TenantsResponse:
    """Response DTO for list-tenants."""

    success: bool
    tenants: list[TenantDTO]
    pagination: PaginationDTO


@dataclass(slots=True, frozen=True)
class CreateTenantRequest:
    """Request DTO for create-tenant."""

    slug: str
    displayName: str
    description: str | None = None
    logoUrl: str | None = None
    seatLimit: int | None = None
    allowedModuleIds: list[int] | None = None


@dataclass(slots=True, frozen=True)
class CreatedTenantDTO:
    """Response DTO for a newly-created tenant."""

    id: int
    slug: str
    displayName: str
    createdAt: str | None


@dataclass(slots=True, frozen=True)
class CreateTenantResponse:
    """Response DTO for create-tenant."""

    success: bool
    tenant: CreatedTenantDTO


@dataclass(slots=True, frozen=True)
class UpdateTenantRequest:
    """Request DTO for update-tenant."""

    displayName: str | None = None
    description: str | None = None
    logoUrl: str | None = None
    isActive: bool | None = None
    seatLimit: int | None = None
    allowedModuleIds: list[int] | None = None
    config: dict[str, Any] | None = None


# ── Community mapping helpers ───────────────────────────────────────────


def _community_dto(row: Any) -> CommunityDTO:
    return CommunityDTO(
        id=row.id,
        name=row.name,
        displayName=row.display_name or row.name,
        description=row.description,
        platform=row.platform,
        platformServerId=row.platform_server_id,
        ownerId=row.owner_id,
        ownerName=row.owner_name,
        memberCount=row.member_count or 0,
        isActive=bool(row.is_active),
        isPublic=bool(row.is_public),
        communityType=row.community_type or "creator",
        isPremium=bool(row.is_premium),
        seatLimit=row.seat_limit,
        createdAt=_iso(row.created_at),
        updatedAt=_iso(row.updated_at),
    )


# ── Routes: dashboard ────────────────────────────────────────────────────


@superadmin_bp.route("/dashboard", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("communities:admin")  # type: ignore[misc]
@validate_response(DashboardResponse)
async def get_dashboard() -> DashboardResponse:
    """Get superadmin dashboard stats."""
    async_dal, dal = _dal()
    data = await svc.get_dashboard_stats(async_dal, dal)
    stats = data["stats"]
    return DashboardResponse(
        success=True,
        stats=DashboardStatsDTO(
            totalCommunities=stats["totalCommunities"],
            activeCommunities=stats["activeCommunities"],
            platformBreakdown=PlatformBreakdownDTO(**stats["platformBreakdown"]),
            totalMembers=stats["totalMembers"],
            adminCount=stats["adminCount"],
        ),
        recentCommunities=[RecentCommunityDTO(**c) for c in data["recentCommunities"]],
    )


# ── Routes: community management ────────────────────────────────────────


@superadmin_bp.route("/communities", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("communities:admin")  # type: ignore[misc]
@validate_response(CommunitiesResponse)
async def list_communities() -> CommunitiesResponse:
    """List all communities (cross-tenant)."""
    async_dal, dal = _dal()
    page = int(request.args.get("page", "1"))
    limit = parse_limit(request.args.get("limit"), default=25)
    search = request.args.get("search", "")
    platform = request.args.get("platform")
    is_active_param = request.args.get("isActive")
    is_active = is_active_param == "true" if is_active_param is not None else None

    rows, total, total_pages = await svc.list_communities(
        async_dal,
        dal,
        page=page,
        limit=limit,
        search=search,
        platform=platform,
        is_active=is_active,
    )
    return CommunitiesResponse(
        success=True,
        communities=[_community_dto(r) for r in rows],
        pagination=PaginationDTO(
            page=max(1, page), limit=min(100, max(1, limit)), total=total, totalPages=total_pages
        ),
    )


@superadmin_bp.route("/communities/<int:community_id>", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("communities:admin")  # type: ignore[misc]
@validate_response(CommunityDetailResponse)
async def get_community(
    community_id: int,
) -> CommunityDetailResponse | tuple[dict[str, object], int]:
    """Get a single community's details."""
    async_dal, dal = _dal()
    try:
        row = await svc.get_community(async_dal, dal, community_id=community_id)
    except ApiError as exc:
        return _err(exc)
    return CommunityDetailResponse(
        success=True,
        community=CommunityDetailDTO(
            id=row.id,
            name=row.name,
            displayName=row.display_name or row.name,
            description=row.description,
            platform=row.platform,
            platformServerId=row.platform_server_id,
            ownerId=row.owner_id,
            ownerName=row.owner_name,
            memberCount=row.member_count or 0,
            isActive=bool(row.is_active),
            isPublic=bool(row.is_public),
            communityType=row.community_type or "creator",
            config=row.config,
            createdAt=_iso(row.created_at),
            updatedAt=_iso(row.updated_at),
        ),
    )


@superadmin_bp.route("/communities", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("communities:admin")  # type: ignore[misc]
@validate_request(CreateCommunityRequest)
# NOT @validate_response -- insert_async + nested-dataclass response hits
# the crash class documented in services/dto_response.py.
async def create_community(data: CreateCommunityRequest) -> tuple[Any, int]:
    """Create a new community."""
    async_dal, dal = _dal()
    caller_id = get_current_user_id(request)
    try:
        community = await svc.create_community(
            async_dal,
            dal,
            name=data.name,
            display_name=data.displayName,
            description=data.description,
            platform=data.platform,
            platform_server_id=data.platformServerId,
            owner_id=data.ownerId,
            owner_name=data.ownerName,
            is_public=data.isPublic,
            community_type=data.communityType,
            caller_id=caller_id,
        )
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(
        CreateCommunityResponse(
            success=True,
            community=CreatedCommunityDTO(
                id=community.id,
                name=community.name,
                displayName=community.display_name,
                platform=community.platform,
                communityType=community.community_type,
                createdAt=_iso(community.created_at),
            ),
        ),
        201,
    )


@superadmin_bp.route("/communities/<int:community_id>", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope("communities:admin")  # type: ignore[misc]
@validate_request(UpdateCommunityRequest)
@validate_response(MessageResponse)
async def update_community(
    data: UpdateCommunityRequest, community_id: int
) -> MessageResponse | tuple[dict[str, object], int]:
    """Update community details."""
    async_dal, dal = _dal()
    field_map = {
        "display_name": data.displayName,
        "description": data.description,
        "owner_id": data.ownerId,
        "owner_name": data.ownerName,
        "is_active": data.isActive,
        "is_public": data.isPublic,
        "platform": data.platform,
        "platform_server_id": data.platformServerId,
        "community_type": data.communityType,
        "is_premium": data.isPremium,
        "seat_limit": data.seatLimit,
    }
    fields = {k: v for k, v in field_map.items() if v is not None}
    try:
        await svc.update_community(async_dal, dal, community_id=community_id, fields=fields)
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message="Community updated")


@superadmin_bp.route("/communities/<int:community_id>", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@require_scope("communities:admin")  # type: ignore[misc]
@validate_response(MessageResponse)
async def delete_community(
    community_id: int,
) -> MessageResponse | tuple[dict[str, object], int]:
    """Deactivate a community."""
    async_dal, dal = _dal()
    caller_id = get_current_user_id(request)
    try:
        await svc.delete_community(async_dal, dal, community_id=community_id, caller_id=caller_id)
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message="Community deleted")


@superadmin_bp.route("/communities/<int:community_id>/reassign", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("communities:admin")  # type: ignore[misc]
@validate_request(ReassignOwnerRequest)
@validate_response(MessageResponse)
async def reassign_owner(
    data: ReassignOwnerRequest, community_id: int
) -> MessageResponse | tuple[dict[str, object], int]:
    """Reassign community ownership."""
    async_dal, dal = _dal()
    try:
        await svc.reassign_owner(
            async_dal,
            dal,
            community_id=community_id,
            new_owner_id=data.newOwnerId,
            new_owner_name=data.newOwnerName,
        )
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message="Ownership reassigned")


# ── Routes: marketplace module registry ─────────────────────────────────


@superadmin_bp.route("/marketplace/modules", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("modules:admin")  # type: ignore[misc]
@validate_response(ModulesResponse)
async def get_all_modules() -> ModulesResponse:
    """List all modules (including unpublished)."""
    async_dal, dal = _dal()
    page = int(request.args.get("page", "1"))
    limit = parse_limit(request.args.get("limit"), default=25)
    search = request.args.get("search", "")
    category = request.args.get("category")
    is_published_param = request.args.get("isPublished")
    is_published = is_published_param == "true" if is_published_param is not None else None

    rows, total, total_pages = await svc.get_all_modules(
        async_dal,
        dal,
        page=page,
        limit=limit,
        search=search,
        category=category,
        is_published=is_published,
    )
    modules = [
        ModuleDTO(
            id=m.id,
            name=m.name,
            displayName=m.display_name or m.name,
            description=m.description,
            version=m.version,
            author=m.author,
            category=m.category,
            iconUrl=m.icon_url,
            isPublished=bool(m.is_published),
            isCore=bool(m.is_core),
            avgRating=f"{avg_rating:.1f}",
            reviewCount=review_count,
            installCount=install_count,
            createdAt=_iso(m.created_at),
        )
        for m, avg_rating, review_count, install_count in rows
    ]
    return ModulesResponse(
        success=True,
        modules=modules,
        pagination=PaginationDTO(
            page=max(1, page), limit=min(100, max(1, limit)), total=total, totalPages=total_pages
        ),
    )


@superadmin_bp.route("/marketplace/modules", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("modules:admin")  # type: ignore[misc]
@validate_request(CreateModuleRequest)
# NOT @validate_response -- insert_async + nested-dataclass response hits
# the crash class documented in services/dto_response.py.
async def create_module(data: CreateModuleRequest) -> tuple[Any, int]:
    """Create a new marketplace module."""
    async_dal, dal = _dal()
    caller_id = get_current_user_id(request)
    try:
        module, db_account = await svc.create_module(
            async_dal,
            dal,
            name=data.name,
            display_name=data.displayName,
            description=data.description,
            version=data.version,
            author=data.author,
            category=data.category,
            icon_url=data.iconUrl,
            is_core=data.isCore,
            config_schema=data.configSchema,
            caller_id=caller_id,
        )
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(
        CreateModuleResponse(
            success=True,
            module=CreatedModuleDTO(
                id=module.id,
                name=module.name,
                displayName=module.display_name,
                createdAt=_iso(module.created_at),
                dbAccount=DbAccountDTO(**db_account),
            ),
        ),
        201,
    )


@superadmin_bp.route("/marketplace/modules/<int:module_id>", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope("modules:admin")  # type: ignore[misc]
@validate_request(UpdateModuleRequest)
@validate_response(MessageResponse)
async def update_module(
    data: UpdateModuleRequest, module_id: int
) -> MessageResponse | tuple[dict[str, object], int]:
    """Update a marketplace module."""
    async_dal, dal = _dal()
    field_map = {
        "display_name": data.displayName,
        "description": data.description,
        "version": data.version,
        "author": data.author,
        "category": data.category,
        "icon_url": data.iconUrl,
        "is_core": data.isCore,
        "config_schema": data.configSchema,
    }
    fields = {k: v for k, v in field_map.items() if v is not None}
    try:
        await svc.update_module(async_dal, dal, module_id=module_id, fields=fields)
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message="Module updated")


@superadmin_bp.route("/marketplace/modules/<int:module_id>/publish", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope("modules:admin")  # type: ignore[misc]
@validate_request(PublishModuleRequest)
@validate_response(MessageResponse)
async def publish_module(
    data: PublishModuleRequest, module_id: int
) -> MessageResponse | tuple[dict[str, object], int]:
    """Publish/unpublish a module."""
    async_dal, dal = _dal()
    try:
        await svc.publish_module(async_dal, dal, module_id=module_id, is_published=data.isPublished)
    except ApiError as exc:
        return _err(exc)
    message = "Module published" if data.isPublished else "Module unpublished"
    return MessageResponse(success=True, message=message)


@superadmin_bp.route("/marketplace/modules/<int:module_id>", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@require_scope("modules:admin")  # type: ignore[misc]
@validate_response(MessageResponse)
async def delete_module(module_id: int) -> MessageResponse | tuple[dict[str, object], int]:
    """Delete a module (blocked if any installations exist)."""
    async_dal, dal = _dal()
    try:
        await svc.delete_module(async_dal, dal, module_id=module_id)
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message="Module deleted")


# ── Routes: tenant management ───────────────────────────────────────────


@superadmin_bp.route("/tenants", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("tenants:admin")  # type: ignore[misc]
@validate_response(TenantsResponse)
async def list_tenants() -> TenantsResponse:
    """List all tenants."""
    async_dal, dal = _dal()
    page = int(request.args.get("page", "1"))
    limit = parse_limit(request.args.get("limit"), default=25)
    search = request.args.get("search", "")
    rows, total, total_pages = await svc.list_tenants(
        async_dal, dal, page=page, limit=limit, search=search
    )
    tenants = [
        TenantDTO(
            id=t.id,
            slug=t.slug,
            displayName=t.display_name,
            description=t.description,
            logoUrl=t.logo_url,
            isGlobal=bool(t.is_global),
            isActive=bool(t.is_active),
            allowedModuleIds=list(t.allowed_module_ids) if t.allowed_module_ids else None,
            seatLimit=t.seat_limit,
            createdAt=_iso(t.created_at),
            updatedAt=_iso(t.updated_at),
        )
        for t in rows
    ]
    return TenantsResponse(
        success=True,
        tenants=tenants,
        pagination=PaginationDTO(
            page=max(1, page), limit=min(100, max(1, limit)), total=total, totalPages=total_pages
        ),
    )


@superadmin_bp.route("/tenants", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("tenants:admin")  # type: ignore[misc]
@validate_request(CreateTenantRequest)
# NOT @validate_response -- insert_async + nested-dataclass response hits
# the crash class documented in services/dto_response.py.
async def create_tenant(data: CreateTenantRequest) -> tuple[Any, int]:
    """Create a new tenant."""
    async_dal, dal = _dal()
    try:
        tenant = await svc.create_tenant(
            async_dal,
            dal,
            slug=data.slug,
            display_name=data.displayName,
            description=data.description,
            logo_url=data.logoUrl,
            seat_limit=data.seatLimit,
            allowed_module_ids=data.allowedModuleIds,
        )
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(
        CreateTenantResponse(
            success=True,
            tenant=CreatedTenantDTO(
                id=tenant.id,
                slug=tenant.slug,
                displayName=tenant.display_name,
                createdAt=_iso(tenant.created_at),
            ),
        ),
        201,
    )


@superadmin_bp.route("/tenants/<int:tenant_id>", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope("tenants:admin")  # type: ignore[misc]
@validate_request(UpdateTenantRequest)
@validate_response(MessageResponse)
async def update_tenant(
    data: UpdateTenantRequest, tenant_id: int
) -> MessageResponse | tuple[dict[str, object], int]:
    """Update a tenant."""
    async_dal, dal = _dal()
    field_map = {
        "display_name": data.displayName,
        "description": data.description,
        "logo_url": data.logoUrl,
        "is_active": data.isActive,
        "seat_limit": data.seatLimit,
        "allowed_module_ids": data.allowedModuleIds,
        "config": data.config,
    }
    fields = {k: v for k, v in field_map.items() if v is not None}
    try:
        await svc.update_tenant(async_dal, dal, tenant_id=tenant_id, fields=fields)
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message="Tenant updated")


@superadmin_bp.route("/tenants/<int:tenant_id>", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@require_scope("tenants:admin")  # type: ignore[misc]
@validate_response(MessageResponse)
async def delete_tenant(tenant_id: int) -> MessageResponse | tuple[dict[str, object], int]:
    """Deactivate a tenant."""
    async_dal, dal = _dal()
    try:
        await svc.delete_tenant(async_dal, dal, tenant_id=tenant_id)
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message="Tenant deactivated")


BLUEPRINTS: list[Blueprint] = [superadmin_bp]
