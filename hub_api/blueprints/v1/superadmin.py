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

import logging
from dataclasses import dataclass
from typing import Annotated, Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import get_authz_decision, has_required_scopes, require_scope
from flask_core.tenancy import get_tenant_context, tenant_middleware
from pydantic import Field
from quart import Blueprint, current_app, request
from quart_schema import RequestSchemaValidationError, validate_request, validate_response

from services import community_tier_service
from services import superadmin_service as svc
from services.audit_http import get_service as get_audit_service
from services.community_public_id import lookup_public_id, lookup_public_ids
from services.current_user import get_current_user_id
from services.dto_response import jsonify_dto
from services.errors import ApiError
from services.pagination import parse_limit
from services.pool_provider import get_pool_provider
from services.public_ids import PublicIdTarget, build_view_arg_translator
from services.system_tenant_guard import is_system_tenant_request, system_tenant_denied
from services.tenant_key_provisioning import TenantKeyProvisioningError, provision_new_tenant

logger = logging.getLogger(__name__)

superadmin_bp = Blueprint("v1_superadmin_platform", __name__, url_prefix="/api/v1/superadmin")
# `<puuid:tenant_id>` -> numeric tenants.id (global table: unscoped; the system-tenant gate and
# `tenants:admin` scope still decide who may call). Unknown/malformed UUID -> the handler's 404.
superadmin_bp.before_request(
    build_view_arg_translator({"tenant_id": PublicIdTarget("tenants", community_param=None)})
)


def _dal() -> tuple[Any, Any]:
    return current_app.config["async_dal"], current_app.config["dal"]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(
        tuple[dict[str, object], int],
        error_response(exc.message, exc.status_code, exc.code, exc.details),
    )


_OWNER_KEYS = ("ownerId", "ownerName", "owner_id", "owner_name")
_TRANSFER_NAME_KEYS = ("newOwnerName", "newOwnerId", "ownerName", "ownerId")


async def _reject_owner_keys(
    keys: tuple[str, ...] = _OWNER_KEYS,
) -> tuple[dict[str, object], int] | None:
    """400 when the body carries any owner key: ownership only changes via the transfer endpoint."""
    raw = await request.get_json(silent=True)
    if isinstance(raw, dict) and any(k in raw for k in keys):
        return _err(
            ApiError(
                "Owner cannot be set here; transfer ownership with "
                "POST /superadmin/communities/<id>/reassign {newOwnerUserId}",
                400,
                "VALIDATION_ERROR",
            )
        )
    return None


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

    id: str
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

    id: str
    name: str
    displayName: str
    description: str | None
    platform: str | None
    platformServerId: str | None
    #: Display only: the username of the member linked to the system owner role (never authority).
    ownerName: str | None
    memberCount: int
    isActive: bool
    isPublic: bool
    communityType: str
    isPremium: bool
    #: 1 free / 2 premium (`isPremium` is derived: tier == 2).
    licenseTier: int
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

    id: str
    name: str
    displayName: str
    description: str | None
    platform: str | None
    platformServerId: str | None
    #: Display only: the username of the member linked to the system owner role (never authority).
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
    # The creator becomes the owner through the seeded owner role; there is no owner field. An
    # old client still sending one gets a 400 from `_reject_owner_keys` (reads the raw body).
    isPublic: bool | None = None
    communityType: str | None = None


@dataclass(slots=True, frozen=True)
class CreatedCommunityDTO:
    """Response DTO for a newly-created community."""

    id: str
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
    # No owner fields: ownership changes only via POST .../reassign; an owner key in the raw
    # body is a 400 (`_reject_owner_keys`).
    isActive: bool | None = None
    isPublic: bool | None = None
    platform: str | None = None
    platformServerId: str | None = None
    communityType: str | None = None
    # REJECTED (400) whenever the key is present: premium is derived from `licenseTier`.
    isPremium: bool | None = None
    # Contract: ABSENT = unchanged; explicit null = unlimited (NULL); a blank string is
    # rejected, not coerced; only a strict non-negative JSON integer is accepted.
    seatLimit: Annotated[int | None, Field(strict=True, ge=0)] = None
    # Operator tier override: 1 free / 2 premium (3 or other -> 400). Requires `reasonCode`
    # and the global-admin scope; never blocked by a full pool (counted + audited instead).
    licenseTier: Annotated[int | None, Field(strict=True)] = None
    reasonCode: Annotated[str | None, Field(max_length=64)] = None


@dataclass(slots=True, frozen=True)
class ReassignOwnerRequest:
    """Request DTO for the ownership transfer (by user UUID only; any name key is a 400)."""

    #: The target member's `hub_users.uuid` (the user UUID the members API exposes as `userId`).
    newOwnerUserId: str


@dataclass(slots=True, frozen=True)
class TenantDTO:
    """Tenant DTO (`id` is the tenant's public UUID, never the numeric key)."""

    id: str
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
    publicId: str | None = None


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

    id: str
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


#: Tier code by `license_tier` column string. The ONE mapping both list and get serialization use:
#: the canonical names from `TIER_NAMES` plus the legacy strings older rows may still hold.
_TIER_CODE_BY_NAME: dict[str, int] = {
    **{name: code for code, name in community_tier_service.TIER_NAMES.items()},
    "pro": community_tier_service.TIER_CODE_PREMIUM,
    "professional": community_tier_service.TIER_CODE_PREMIUM,
    "enterprise": community_tier_service.TIER_CODE_PREMIUM,
}


def _license_tier_code(row: Any) -> int:
    """Tier code (1 free / 2 premium) from the denormalized `license_tier` string (legacy mapped).

    pro/professional/premium/enterprise (legacy strings) -> 2, anything else -> 1 (Free).
    A row with no `license_tier` that predates the column but is `is_premium` stays Premium.
    """
    name = str(row.license_tier or "").strip().lower()
    if not name:
        return (
            community_tier_service.TIER_CODE_PREMIUM
            if row.is_premium
            else community_tier_service.TIER_CODE_FREE
        )
    return _TIER_CODE_BY_NAME.get(name, community_tier_service.TIER_CODE_FREE)


def _community_dto(row: Any, public_id: str, owner_name: str | None) -> CommunityDTO:
    """Community DTO; `id` is the community's PUBLIC UUID (never the numeric id)."""
    tier_code = _license_tier_code(row)
    return CommunityDTO(
        id=public_id,
        name=row.name,
        displayName=row.display_name or row.name,
        description=row.description,
        platform=row.platform,
        platformServerId=row.platform_server_id,
        ownerName=owner_name,
        memberCount=row.member_count or 0,
        isActive=bool(row.is_active),
        isPublic=bool(row.is_public),
        communityType=row.community_type or "creator",
        isPremium=tier_code > community_tier_service.TIER_CODE_FREE,
        licenseTier=tier_code,
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
    recent_public_ids = await lookup_public_ids(int(c["id"]) for c in data["recentCommunities"])
    return DashboardResponse(
        success=True,
        stats=DashboardStatsDTO(
            totalCommunities=stats["totalCommunities"],
            activeCommunities=stats["activeCommunities"],
            platformBreakdown=PlatformBreakdownDTO(**stats["platformBreakdown"]),
            totalMembers=stats["totalMembers"],
            adminCount=stats["adminCount"],
        ),
        recentCommunities=[
            RecentCommunityDTO(**{**c, "id": recent_public_ids[int(c["id"])]})
            for c in data["recentCommunities"]
        ],
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
    public_ids = await lookup_public_ids(int(r.id) for r in rows)
    owner_names = await svc.community_owner_names(
        async_dal, dal, community_ids=[int(r.id) for r in rows]
    )
    return CommunitiesResponse(
        success=True,
        communities=[
            _community_dto(r, public_ids[int(r.id)], owner_names.get(int(r.id))) for r in rows
        ],
        pagination=PaginationDTO(
            page=max(1, page), limit=min(100, max(1, limit)), total=total, totalPages=total_pages
        ),
    )


@superadmin_bp.route("/communities/<cuuid:community_id>", methods=["GET"])
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
        logger.debug("superadmin.rejected code=%s status=%s", exc.code, exc.status_code)
        return _err(exc)
    owner_name = (await svc.community_owner_names(async_dal, dal, community_ids=[int(row.id)])).get(
        int(row.id)
    )
    return CommunityDetailResponse(
        success=True,
        community=CommunityDetailDTO(
            id=await lookup_public_id(int(row.id)),
            name=row.name,
            displayName=row.display_name or row.name,
            description=row.description,
            platform=row.platform,
            platformServerId=row.platform_server_id,
            ownerName=owner_name,
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
    if (rejected := await _reject_owner_keys()) is not None:
        return rejected
    caller_id = get_current_user_id(request)
    ctx = get_tenant_context(request)
    if ctx is None:
        return _err(ApiError("Tenant context required", 403, "FORBIDDEN"))
    try:
        community = await svc.create_community(
            async_dal,
            dal,
            name=data.name,
            display_name=data.displayName,
            description=data.description,
            platform=data.platform,
            platform_server_id=data.platformServerId,
            is_public=data.isPublic,
            community_type=data.communityType,
            caller_id=caller_id,
            tenant_slug=ctx.tenant_slug,
            tenant_id=ctx.tenant_id,
        )
    except ApiError as exc:
        logger.info("api_error_rejected", extra={"code": exc.code, "status": exc.status_code})
        return _err(exc)
    return jsonify_dto(
        CreateCommunityResponse(
            success=True,
            community=CreatedCommunityDTO(
                id=await lookup_public_id(int(community.id)),
                name=community.name,
                displayName=community.display_name,
                platform=community.platform,
                communityType=community.community_type,
                createdAt=_iso(community.created_at),
            ),
        ),
        201,
    )


@superadmin_bp.errorhandler(RequestSchemaValidationError)  # type: ignore[misc]
async def _request_validation_error(exc: RequestSchemaValidationError) -> Any:
    """Return the JSON error envelope naming each invalid field (never a bare empty 400)."""
    pydantic_err = getattr(exc, "validation_error", None)
    issues = pydantic_err.errors() if pydantic_err is not None else []
    parts = [
        f"{'.'.join(str(p) for p in i.get('loc', ())) or 'body'}: {i.get('msg', 'invalid')}"
        for i in issues
    ]
    message = "Invalid request: " + "; ".join(parts) if parts else "Invalid request body"
    body, status = error_response(message, 400, "VALIDATION_ERROR")
    return body, status


@superadmin_bp.route("/communities/<cuuid:community_id>", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope("communities:admin")  # type: ignore[misc]
@validate_request(UpdateCommunityRequest)
@validate_response(MessageResponse)
async def update_community(
    data: UpdateCommunityRequest, community_id: int
) -> MessageResponse | tuple[dict[str, object], int]:
    """Update community details; `licenseTier` is an operator tier override.

    Absent key = unchanged. Explicit `null` clears the nullable `seatLimit` (unlimited) and
    `description`; the non-nullable `displayName`/`isActive`/`isPublic` reject null with a 400.
    """
    async_dal, dal = _dal()
    if (rejected := await _reject_owner_keys()) is not None:
        return rejected
    raw = await request.get_json(silent=True)
    present = set(raw) if isinstance(raw, dict) else set()
    if "isPremium" in present:
        return _err(
            ApiError(
                "isPremium is derived from the community's tier and cannot be set; "
                "send licenseTier (1 free, 2 premium) with a reasonCode",
                400,
                "VALIDATION_ERROR",
            )
        )
    for key in ("displayName", "isActive", "isPublic"):
        if key in present and getattr(data, key) is None:
            return _err(ApiError(f"{key} cannot be null", 400, "VALIDATION_ERROR"))
    field_map = {
        "display_name": data.displayName,
        "is_active": data.isActive,
        "is_public": data.isPublic,
        "platform": data.platform,
        "platform_server_id": data.platformServerId,
        "community_type": data.communityType,
    }
    fields: dict[str, Any] = {k: v for k, v in field_map.items() if v is not None}
    # Nullable columns: presence decides, so an explicit null clears them.
    if "description" in present:
        fields["description"] = data.description
    if "seatLimit" in present:
        fields["seat_limit"] = data.seatLimit
    license_tier = data.licenseTier
    if "licenseTier" in present and license_tier is None:
        return _err(ApiError("licenseTier must be 1 or 2 when present", 400, "VALIDATION_ERROR"))
    if license_tier is not None:
        if not is_system_tenant_request():
            return system_tenant_denied()
        decision = get_authz_decision(request)
        if decision is None or not has_required_scopes(decision.granted, ("users:admin",)):
            return _err(
                ApiError(
                    "Changing a community's tier requires platform administration", 403, "FORBIDDEN"
                )
            )
    try:
        await svc.update_community(
            async_dal,
            dal,
            community_id=community_id,
            fields=fields,
            license_tier=license_tier,
            reason_code=data.reasonCode,
            actor=get_current_user_id(request) if license_tier is not None else None,
            pool=get_pool_provider(current_app) if license_tier is not None else None,
            audit=get_audit_service() if license_tier is not None else None,
        )
    except ApiError as exc:
        logger.debug("superadmin.rejected code=%s status=%s", exc.code, exc.status_code)
        return _err(exc)
    return MessageResponse(success=True, message="Community updated")


@superadmin_bp.route("/communities/<cuuid:community_id>", methods=["DELETE"])
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
        logger.debug("superadmin.rejected code=%s status=%s", exc.code, exc.status_code)
        return _err(exc)
    return MessageResponse(success=True, message="Community deleted")


@superadmin_bp.route("/communities/<cuuid:community_id>/reassign", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("communities:admin")  # type: ignore[misc]
@validate_request(ReassignOwnerRequest)
@validate_response(MessageResponse)
async def reassign_owner(
    data: ReassignOwnerRequest, community_id: int
) -> MessageResponse | tuple[dict[str, object], int]:
    """Transfer community ownership to an existing active member, by user UUID only.

    403 non-admin caller (tenant admin or global/system admin required); 404 community outside
    the caller's tenant or target not a member; 409 banned target; 200 when already the owner
    (idempotent no-op, `changed: false` is not exposed -- the message says so).
    """
    async_dal, dal = _dal()
    if (rejected := await _reject_owner_keys(_TRANSFER_NAME_KEYS)) is not None:
        return rejected
    ctx = get_tenant_context(request)
    if ctx is None:
        return _err(ApiError("Tenant context required", 403, "FORBIDDEN"))
    try:
        changed = await svc.transfer_ownership(
            async_dal,
            dal,
            community_id=community_id,
            new_owner_user_uuid=data.newOwnerUserId.strip().lower(),
            caller_user_id=get_current_user_id(request),
            caller_tenant_id=ctx.tenant_id,
            is_global=is_system_tenant_request(),
            audit=get_audit_service(),
        )
    except ApiError as exc:
        logger.debug("superadmin.rejected code=%s status=%s", exc.code, exc.status_code)
        return _err(exc)
    return MessageResponse(
        success=True,
        message="Ownership transferred" if changed else "Member is already the owner",
    )


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
            id=str(t.public_id),
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
            publicId=getattr(t, "public_id", None),
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
    ctx = get_tenant_context(request)
    if ctx is None:
        return _err(ApiError("Tenant context required", 403, "FORBIDDEN"))
    runtime = current_app.config.get("envelope_runtime")
    if runtime is None:
        logger.error("create_tenant.envelope_runtime_missing")
        return _err(ApiError("Encryption service is not available", 503, "TENANT_KEY_UNAVAILABLE"))
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
            caller_tenant_slug=ctx.tenant_slug,
        )
    except ApiError as exc:
        logger.info("api_error_rejected", extra={"code": exc.code, "status": exc.status_code})
        return _err(exc)
    # No tenant without an at-rest key: on failure the new row is removed and the create fails.
    try:
        await provision_new_tenant(
            current_app.config["install_dal"],
            runtime.service,
            tenant_id=tenant.id,
            slug=tenant.slug,
        )
    except TenantKeyProvisioningError:
        logger.error("create_tenant.key_provisioning_failed", extra={"tenant_id": tenant.id})
        return _err(
            ApiError(
                "Tenant was not created: its encryption key could not be provisioned",
                503,
                "TENANT_KEY_UNAVAILABLE",
            )
        )
    return jsonify_dto(
        CreateTenantResponse(
            success=True,
            tenant=CreatedTenantDTO(
                id=str(tenant.public_id),
                slug=tenant.slug,
                displayName=tenant.display_name,
                createdAt=_iso(tenant.created_at),
            ),
        ),
        201,
    )


@superadmin_bp.route("/tenants/<puuid:tenant_id>", methods=["PUT"])
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
        logger.debug("superadmin.rejected code=%s status=%s", exc.code, exc.status_code)
        return _err(exc)
    return MessageResponse(success=True, message="Tenant updated")


@superadmin_bp.route("/tenants/<puuid:tenant_id>", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@require_scope("tenants:admin")  # type: ignore[misc]
@validate_response(MessageResponse)
async def delete_tenant(tenant_id: int) -> MessageResponse | tuple[dict[str, object], int]:
    """Deactivate a tenant."""
    async_dal, dal = _dal()
    try:
        await svc.delete_tenant(async_dal, dal, tenant_id=tenant_id)
    except ApiError as exc:
        logger.debug("superadmin.rejected code=%s status=%s", exc.code, exc.status_code)
        return _err(exc)
    return MessageResponse(success=True, message="Tenant deactivated")


BLUEPRINTS: list[Blueprint] = [superadmin_bp]
