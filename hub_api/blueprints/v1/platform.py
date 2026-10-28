"""v1 `platform` group -- ported from `platformController.js` (migration plan M3).

Mounted at `/api/v1/platform*`, matching `admin/hub_module/backend/src/
routes/platform.js` (verified against `frontend/src/services/api.js`'s
`platformApi` -- every path below is IDENTICAL). Cross-tenant by design
(Node's own comment: "Platform-wide admin features") -- see
`services/platform_service.py`'s module docstring for why that's safe
here and NOT a tenant-isolation gap.

Authz decision (fixes a genuinely-broken Node authz chain, not a faithful
reproduction of it): Node gates this whole router with `requireAuth` +
`requirePlatformAdmin`, which checks `req.user.roles.includes(
'platform-admin')`. `authController.js`'s `createSession()` NEVER pushes
that literal string into the JWT's `roles` array (only `'admin'`/
`'super_admin'`/`'vendor'`, driven by `isSuperAdmin`/`isVendor`) -- so
every caller, including a real super admin, gets 403 on every route in
this group, in Node, today. Combined with `platform_admins` (the table
`updateUserRole()` writes to) not existing in any migration either (see
`services/schema.py::bind_platform_tables()`'s own docstring), this
group is both unreachable AND partially non-functional in Node's current
deployment -- not a security control worth preserving byte-for-byte.

This port gates the group with `require_scope("platform:admin")`
instead. No `auth_service.py` change is needed: `is_super_admin` already
grants `SCOPE_BUNDLES["global"]["admin"]`, which includes the `*:admin`
wildcard -- `flask_core.authz._scope_covers`'s own wildcard rule
(resource `*` + exact action match) means `*:admin` already satisfies
`platform:admin` today, with zero changes to the shared auth chain. This
restores the group to reachable, matching the product intent visible in
Node's own code (a global admin was clearly meant to reach a
"platform-wide admin" panel). A `platform_admins`-table-only admin (role
='platform-admin', NOT also `is_super_admin`) still cannot reach this
group -- `createSession()` never merges that table into the granted
scope set either. Extending `auth_service.py` to do so belongs to the
M1 Core Identity/Auth group (this PR does not touch that file) and is
flagged here as a natural follow-up, not silently invented.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.tenancy import tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services import platform_service as svc
from services.current_user import get_current_user_id
from services.dto_response import jsonify_dto
from services.errors import ApiError
from services.pagination import parse_limit

platform_bp = Blueprint("v1_platform", __name__, url_prefix="/api/v1/platform")


def _dal() -> tuple[Any, Any]:
    """Return `(async_dal, dal)` from app config."""
    return current_app.config["async_dal"], current_app.config["dal"]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    """Convert an `ApiError` into the flask_core `error_response()` JSON envelope."""
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


# ---------------------------------------------------------------------------
# DTOs -- camelCase field names pinned to `frontend/src/services/api.js`'s
# `platformApi` (see hub_api/PORTING.md's DTO casing note).
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class PaginationDTO:
    """Pagination DTO."""

    page: int
    limit: int
    total: int
    totalPages: int


@dataclass(slots=True, frozen=True)
class PlatformUserDTO:
    """Platform user DTO (list row)."""

    userId: str
    displayName: str | None
    platform: str | None
    platformUserId: str | None
    createdAt: str | None
    lastActivity: str | None


@dataclass(slots=True, frozen=True)
class ListUsersResponse:
    """Response DTO for list users."""

    success: bool
    users: list[PlatformUserDTO]
    pagination: PaginationDTO


@dataclass(slots=True, frozen=True)
class CommunityMembershipDTO:
    """Community membership DTO (nested in get-user detail)."""

    communityId: int | None
    communityName: str | None
    role: str | None
    reputationScore: int
    joinedAt: str | None
    lastActivity: str | None


@dataclass(slots=True, frozen=True)
class PlatformUserDetailDTO:
    """Platform user detail DTO."""

    userId: str
    displayName: str | None
    platform: str | None
    platformUserId: str | None
    isPlatformAdmin: bool
    platformRole: str | None
    memberships: list[CommunityMembershipDTO]


@dataclass(slots=True, frozen=True)
class GetUserResponse:
    """Response DTO for get user."""

    success: bool
    user: PlatformUserDetailDTO


@dataclass(slots=True, frozen=True)
class UpdateUserRoleRequest:
    """Request DTO for update user role."""

    role: str | None = None


@dataclass(slots=True, frozen=True)
class DeactivateUserRequest:
    """Request DTO for deactivate user."""

    reason: str | None = None


@dataclass(slots=True, frozen=True)
class MessageResponse:
    """Response DTO for `{success, message}` endpoints."""

    success: bool
    message: str


@dataclass(slots=True, frozen=True)
class CommunityDTO:
    """Community DTO (list row)."""

    id: int
    name: str | None
    displayName: str | None
    description: str | None
    primaryPlatform: str | None
    memberCount: int
    isPublic: bool
    isActive: bool
    createdAt: str | None


@dataclass(slots=True, frozen=True)
class ListCommunitiesResponse:
    """Response DTO for list communities."""

    success: bool
    communities: list[CommunityDTO]
    pagination: PaginationDTO


@dataclass(slots=True, frozen=True)
class CommunityOwnerDTO:
    """Community owner DTO."""

    userId: str
    displayName: str | None
    platform: str | None


@dataclass(slots=True, frozen=True)
class CommunityDetailDTO:
    """Community detail DTO."""

    id: int
    name: str | None
    displayName: str | None
    description: str | None
    logoUrl: str | None
    bannerUrl: str | None
    primaryPlatform: str | None
    memberCount: int
    isPublic: bool
    isActive: bool
    createdAt: str | None
    owner: CommunityOwnerDTO | None
    moduleCount: int
    domainCount: int


@dataclass(slots=True, frozen=True)
class GetCommunityResponse:
    """Response DTO for get community."""

    success: bool
    community: CommunityDetailDTO


@dataclass(slots=True, frozen=True)
class UpdateCommunityRequest:
    """Request DTO for update community."""

    displayName: str | None = None
    description: str | None = None
    isPublic: bool | None = None
    isActive: bool | None = None


@dataclass(slots=True, frozen=True)
class HealthChecksDTO:
    """Health checks DTO."""

    database: bool
    timestamp: str


@dataclass(slots=True, frozen=True)
class SystemHealthResponse:
    """Response DTO for system health."""

    success: bool
    status: str
    checks: HealthChecksDTO


@dataclass(slots=True, frozen=True)
class ModuleDTO:
    """Collector module DTO."""

    moduleName: str
    moduleVersion: str | None
    platform: str | None
    endpointUrl: str | None
    status: str | None
    lastHeartbeat: str | None
    createdAt: str | None


@dataclass(slots=True, frozen=True)
class ModuleRegistryResponse:
    """Response DTO for module registry."""

    success: bool
    modules: list[ModuleDTO]


@dataclass(slots=True, frozen=True)
class AuditEntryDTO:
    """Audit log entry DTO."""

    id: int
    userId: int | None
    action: str
    targetType: str | None
    targetId: str | None
    details: dict[str, Any] | None
    ipAddress: str | None
    createdAt: str | None


@dataclass(slots=True, frozen=True)
class AuditLogResponse:
    """Response DTO for audit log."""

    success: bool
    entries: list[AuditEntryDTO]
    pagination: PaginationDTO


@dataclass(slots=True, frozen=True)
class StatsUsersDTO:
    """Stats users DTO."""

    total: int
    active7d: int
    active30d: int


@dataclass(slots=True, frozen=True)
class StatsCommunitiesDTO:
    """Stats communities DTO."""

    total: int
    public: int
    totalMembers: int


@dataclass(slots=True, frozen=True)
class StatsSessionsDTO:
    """Stats sessions DTO."""

    last24h: int
    active: int


@dataclass(slots=True, frozen=True)
class PlatformStatsDTO:
    """Platform stats DTO."""

    users: StatsUsersDTO
    communities: StatsCommunitiesDTO
    platforms: dict[str, int]
    sessions: StatsSessionsDTO
    timestamp: str


@dataclass(slots=True, frozen=True)
class PlatformStatsResponse:
    """Response DTO for platform stats."""

    success: bool
    stats: PlatformStatsDTO


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------


@platform_bp.route("/users", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("platform:admin")  # type: ignore[misc]
@validate_response(ListUsersResponse)
async def list_users() -> ListUsersResponse:
    """List users."""
    async_dal, dal = _dal()
    page = int(request.args.get("page", "1"))
    limit = parse_limit(request.args.get("limit"), default=25)
    search = request.args.get("search", "")
    platform = request.args.get("platform")

    rows, total, total_pages = await svc.list_users(
        async_dal, dal, page=page, limit=limit, search=search, platform=platform
    )
    return ListUsersResponse(
        success=True,
        users=[
            PlatformUserDTO(
                userId=r.user_id,
                displayName=r.display_name,
                platform=r.platform,
                platformUserId=r.platform_user_id,
                createdAt=_iso(r.created_at),
                lastActivity=_iso(r.last_activity),
            )
            for r in rows
        ],
        pagination=PaginationDTO(
            page=page, limit=min(100, max(1, limit)), total=total, totalPages=total_pages
        ),
    )


@platform_bp.route("/users/<int:user_id>", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("platform:admin")  # type: ignore[misc]
# NOT @validate_response -- `user: PlatformUserDetailDTO` is a nested
# dataclass field; jsonify_dto() is the established workaround (see
# services/dto_response.py's module docstring).
async def get_user(user_id: int) -> tuple[Any, int]:
    """Get user."""
    async_dal, dal = _dal()
    try:
        result = await svc.get_user(async_dal, dal, user_id=user_id)
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(
        GetUserResponse(
            success=True,
            user=PlatformUserDetailDTO(
                userId=str(result["user_id"]),
                displayName=result["display_name"],
                platform=result["platform"],
                platformUserId=result["platform_user_id"],
                isPlatformAdmin=result["is_platform_admin"],
                platformRole=result["platform_role"],
                memberships=[
                    CommunityMembershipDTO(
                        communityId=m["community_id"],
                        communityName=m["community_name"],
                        role=m["role"],
                        reputationScore=m["reputation_score"],
                        joinedAt=_iso(m["joined_at"]),
                        lastActivity=_iso(m["last_activity"]),
                    )
                    for m in result["memberships"]
                ],
            ),
        )
    )


@platform_bp.route("/users/<int:user_id>/role", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope("platform:admin")  # type: ignore[misc]
@validate_request(UpdateUserRoleRequest)
@validate_response(MessageResponse)
async def update_user_role(
    data: UpdateUserRoleRequest, user_id: int
) -> MessageResponse | tuple[dict[str, object], int]:
    """Update user role."""
    async_dal, dal = _dal()
    try:
        await svc.update_user_role(async_dal, dal, user_id=user_id, role=data.role)
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message="User role updated")


@platform_bp.route("/users/<int:user_id>", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@require_scope("platform:admin")  # type: ignore[misc]
@validate_request(DeactivateUserRequest)
@validate_response(MessageResponse)
async def deactivate_user(
    data: DeactivateUserRequest, user_id: int
) -> MessageResponse | tuple[dict[str, object], int]:
    """Deactivate user."""
    async_dal, dal = _dal()
    try:
        actor_id = get_current_user_id(request)
        await svc.deactivate_user(
            async_dal, dal, user_id=user_id, reason=data.reason, actor_id=actor_id
        )
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message="User deactivated")


# ---------------------------------------------------------------------------
# Community management
# ---------------------------------------------------------------------------


@platform_bp.route("/communities", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("platform:admin")  # type: ignore[misc]
@validate_response(ListCommunitiesResponse)
async def list_communities() -> ListCommunitiesResponse:
    """List communities."""
    async_dal, dal = _dal()
    page = int(request.args.get("page", "1"))
    limit = parse_limit(request.args.get("limit"), default=25)
    search = request.args.get("search", "")
    is_active = request.args.get("isActive", "true") != "false"

    rows, total, total_pages = await svc.list_communities(
        async_dal, dal, page=page, limit=limit, search=search, is_active=is_active
    )
    return ListCommunitiesResponse(
        success=True,
        communities=[
            CommunityDTO(
                id=r.id,
                name=r.name,
                displayName=r.display_name or r.name,
                description=r.description,
                primaryPlatform=r.primary_platform,
                memberCount=r.member_count or 0,
                isPublic=bool(r.is_public),
                isActive=bool(r.is_active),
                createdAt=_iso(r.created_at),
            )
            for r in rows
        ],
        pagination=PaginationDTO(
            page=page, limit=min(100, max(1, limit)), total=total, totalPages=total_pages
        ),
    )


@platform_bp.route("/communities/<int:community_id>", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("platform:admin")  # type: ignore[misc]
# NOT @validate_response -- nested `community: CommunityDetailDTO` +
# nested `owner: CommunityOwnerDTO`; see services/dto_response.py.
async def get_community(community_id: int) -> tuple[Any, int]:
    """Get community."""
    async_dal, dal = _dal()
    try:
        result = await svc.get_community(async_dal, dal, community_id=community_id)
    except ApiError as exc:
        return _err(exc)
    c = result["community"]
    owner = result["owner"]
    return jsonify_dto(
        GetCommunityResponse(
            success=True,
            community=CommunityDetailDTO(
                id=c.id,
                name=c.name,
                displayName=c.display_name or c.name,
                description=c.description,
                logoUrl=c.logo_url,
                bannerUrl=c.banner_url,
                primaryPlatform=c.primary_platform,
                memberCount=c.member_count or 0,
                isPublic=bool(c.is_public),
                isActive=bool(c.is_active),
                createdAt=_iso(c.created_at),
                owner=CommunityOwnerDTO(
                    userId=owner.user_id, displayName=owner.display_name, platform=owner.platform
                )
                if owner
                else None,
                moduleCount=result["module_count"],
                domainCount=result["domain_count"],
            ),
        )
    )


@platform_bp.route("/communities/<int:community_id>", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope("platform:admin")  # type: ignore[misc]
@validate_request(UpdateCommunityRequest)
@validate_response(MessageResponse)
async def update_community(
    data: UpdateCommunityRequest, community_id: int
) -> MessageResponse | tuple[dict[str, object], int]:
    """Update community."""
    async_dal, dal = _dal()
    try:
        await svc.update_community(
            async_dal,
            dal,
            community_id=community_id,
            display_name=data.displayName,
            description=data.description,
            is_public=data.isPublic,
            is_active=data.isActive,
        )
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message="Community updated")


@platform_bp.route("/communities/<int:community_id>", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@require_scope("platform:admin")  # type: ignore[misc]
@validate_request(DeactivateUserRequest)
@validate_response(MessageResponse)
async def deactivate_community(
    data: DeactivateUserRequest, community_id: int
) -> MessageResponse | tuple[dict[str, object], int]:
    """Deactivate community."""
    async_dal, dal = _dal()
    try:
        await svc.deactivate_community(
            async_dal, dal, community_id=community_id, reason=data.reason
        )
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message="Community deactivated")


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------


@platform_bp.route("/health", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("platform:admin")  # type: ignore[misc]
@validate_response(SystemHealthResponse, 200)
@validate_response(SystemHealthResponse, 503)
async def get_system_health() -> tuple[SystemHealthResponse, int]:
    """Get system health."""
    async_dal, dal = _dal()
    database_ok, healthy = await svc.get_system_health(async_dal, dal)
    response = SystemHealthResponse(
        success=healthy,
        status="healthy" if healthy else "degraded",
        checks=HealthChecksDTO(
            database=database_ok, timestamp=datetime.now(UTC).isoformat()
        ),
    )
    return response, 200 if healthy else 503


@platform_bp.route("/modules", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("platform:admin")  # type: ignore[misc]
@validate_response(ModuleRegistryResponse)
async def get_module_registry() -> ModuleRegistryResponse:
    """Get module registry."""
    async_dal, dal = _dal()
    rows = await svc.get_module_registry(async_dal, dal)
    return ModuleRegistryResponse(
        success=True,
        modules=[
            ModuleDTO(
                moduleName=r.module_name,
                moduleVersion=r.module_version,
                platform=r.platform,
                endpointUrl=r.endpoint_url,
                status=r.status,
                lastHeartbeat=_iso(r.last_heartbeat),
                createdAt=_iso(r.created_at),
            )
            for r in rows
        ],
    )


@platform_bp.route("/audit-log", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("platform:admin")  # type: ignore[misc]
@validate_response(AuditLogResponse)
async def get_audit_log() -> AuditLogResponse:
    """Get audit log."""
    async_dal, dal = _dal()
    page = int(request.args.get("page", "1"))
    limit = parse_limit(request.args.get("limit"), default=50)
    action = request.args.get("action")
    user_id_param = request.args.get("userId")
    user_id = int(user_id_param) if user_id_param else None

    rows, total, total_pages = await svc.get_audit_log(
        async_dal, dal, page=page, limit=limit, action=action, user_id=user_id
    )
    return AuditLogResponse(
        success=True,
        entries=[
            AuditEntryDTO(
                id=r.id,
                userId=r.user_id,
                action=r.action,
                targetType=r.target_type,
                targetId=r.target_id,
                details=r.details,
                ipAddress=r.ip_address,
                createdAt=_iso(r.created_at),
            )
            for r in rows
        ],
        pagination=PaginationDTO(
            page=page, limit=min(100, max(1, limit)), total=total, totalPages=total_pages
        ),
    )


@platform_bp.route("/stats", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("platform:admin")  # type: ignore[misc]
@validate_response(PlatformStatsResponse)
async def get_stats() -> PlatformStatsResponse:
    """Get platform stats."""
    async_dal, dal = _dal()
    result = await svc.get_stats(async_dal, dal)
    return PlatformStatsResponse(
        success=True,
        stats=PlatformStatsDTO(
            users=StatsUsersDTO(
                total=result["users"]["total"],
                active7d=result["users"]["active_7d"],
                active30d=result["users"]["active_30d"],
            ),
            communities=StatsCommunitiesDTO(
                total=result["communities"]["total"],
                public=result["communities"]["public"],
                totalMembers=result["communities"]["total_members"],
            ),
            platforms=result["platforms"],
            sessions=StatsSessionsDTO(
                last24h=result["sessions"]["last_24h"], active=result["sessions"]["active"]
            ),
            timestamp=datetime.now(UTC).isoformat(),
        ),
    )


BLUEPRINTS: list[Blueprint] = [platform_bp]
