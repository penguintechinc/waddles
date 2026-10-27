"""v1 `admin` (community platform-admin) group -- ported from `adminController.js`.

Mounted at `/api/v1/admin/<communityId>/*` (matches `routes/admin.js`).
Every route requires `@tenant_middleware` + `@require_scope("community:admin")`
(`SCOPE_BUNDLES["community"]["admin"]`, see `flask_core.auth`) -- the OIDC-
scope equivalent of Node's `requireCommunityAdmin` (super-admin/tenant-
admin/platform-admin bypass a per-community DB role lookup). See
`services/admin_service.py`'s module docstring for the tenant-isolation
(`_require_community`) and admin-promotion nuances this port preserves.

**Scope note (M3 Platform-admin, this PR's slice of `adminController.js`):**
ported here: community settings ("system settings"), per-community module
toggles, member/reputation management, temp-password issuance, connected-
platforms aggregation, commands reference. Deliberately NOT ported in this
PR (community-*feature* surfaces, not platform/system admin -- left for a
dedicated follow-up rather than rushed alongside this slice): browser
sources, custom domains, linked servers, join-requests, server-link-
requests, mirror groups, reputation *scoring config* (at-risk/leaderboard),
AI insights/researcher, bot detection/score, translation config, community
profile (logo/banner), overlay, loyalty, and OAuth credential management.
`getShoutoutConfig`/`updateShoutoutConfig`/creator-list/history were
already taken by the Bot module port (M5, per this PR's task brief).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services import admin_service as svc
from services.current_user import get_optional_current_user_id
from services.dto_response import jsonify_dto
from services.errors import ApiError
from services.pagination import parse_limit

admin_bp = Blueprint("v1_admin", __name__, url_prefix="/api/v1/admin")


def _dal() -> tuple[Any, Any]:
    return current_app.config["async_dal"], current_app.config["dal"]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


def _reputation_dto(score: int) -> ReputationDTO:
    score = max(300, min(850, int(score or 600)))
    if score >= 800:
        label, short = "Exceptional", "exceptional"
    elif score >= 740:
        label, short = "Very Good", "very_good"
    elif score >= 670:
        label, short = "Good", "good"
    elif score >= 580:
        label, short = "Fair", "fair"
    else:
        label, short = "Poor", "poor"
    tiers = [(800, 850), (740, 799), (670, 739), (580, 669), (300, 579)]
    tier_min, tier_max = next((mn, mx) for mn, mx in tiers if mn <= score <= mx)
    return ReputationDTO(
        score=score,
        label=label,
        shortLabel=short,
        tierMin=tier_min,
        tierMax=tier_max,
        systemMin=300,
        systemMax=850,
    )


# ── DTOs ─────────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class MessageResponse:
    """Response DTO for message endpoints."""

    success: bool
    message: str


@dataclass(slots=True, frozen=True)
class SettingsDTO:
    """Community settings DTO."""

    name: str | None
    displayName: str | None
    description: str | None
    platform: str | None
    isPublic: bool
    joinMode: str
    channelCreationPolicy: str
    config: dict[str, Any]


@dataclass(slots=True, frozen=True)
class SettingsResponse:
    """Response DTO for get-settings."""

    success: bool
    settings: SettingsDTO


@dataclass(slots=True, frozen=True)
class UpdateSettingsRequest:
    """Request DTO for update-settings."""

    displayName: str | None = None
    description: str | None = None
    isPublic: bool | None = None
    joinMode: str | None = None
    channelCreationPolicy: str | None = None
    config: dict[str, Any] | None = None


@dataclass(slots=True, frozen=True)
class ReputationDTO:
    """FICO-style reputation DTO."""

    score: int
    label: str
    shortLabel: str
    tierMin: int
    tierMax: int
    systemMin: int
    systemMax: int


@dataclass(slots=True, frozen=True)
class MemberDTO:
    """Community member DTO."""

    id: int
    userId: str | None
    username: str | None
    email: str | None
    avatarUrl: str | None
    role: str | None
    reputation: ReputationDTO
    joinedAt: str | None


@dataclass(slots=True, frozen=True)
class PaginationDTO:
    """Pagination DTO."""

    page: int
    limit: int
    total: int
    totalPages: int


@dataclass(slots=True, frozen=True)
class MembersResponse:
    """Response DTO for list-members."""

    success: bool
    members: list[MemberDTO]
    pagination: PaginationDTO


@dataclass(slots=True, frozen=True)
class UpdateRoleRequest:
    """Request DTO for update-member-role."""

    role: str


@dataclass(slots=True, frozen=True)
class AdjustReputationRequest:
    """Request DTO for adjust-reputation."""

    amount: float | None = None
    reason: str | None = None
    setTo: float | None = None


@dataclass(slots=True, frozen=True)
class ReputationResponse:
    """Response DTO for adjust-reputation."""

    success: bool
    reputation: ReputationDTO
    change: int | None = None


@dataclass(slots=True, frozen=True)
class RemoveMemberRequest:
    """Request DTO for remove-member."""

    reason: str | None = None


@dataclass(slots=True, frozen=True)
class ModuleDTO:
    """Per-community module installation DTO."""

    installationId: int
    moduleId: str
    name: str | None
    displayName: str | None
    description: str | None
    category: str | None
    isEnabled: bool
    config: dict[str, Any] | None
    installedAt: str | None


@dataclass(slots=True, frozen=True)
class ModulesResponse:
    """Response DTO for list-modules."""

    success: bool
    modules: list[ModuleDTO]


@dataclass(slots=True, frozen=True)
class UpdateModuleConfigRequest:
    """Request DTO for update-module-config."""

    config: dict[str, Any] | None = None
    isEnabled: bool | None = None


@dataclass(slots=True, frozen=True)
class TempPasswordRequest:
    """Request DTO for generate-temp-password."""

    userIdentifier: str
    forceOAuthLink: bool = True
    expiresInHours: int = 24


@dataclass(slots=True, frozen=True)
class TempPasswordResponse:
    """Response DTO for generate-temp-password."""

    success: bool
    tempPassword: str
    expiresAt: str
    instructions: str


@dataclass(slots=True, frozen=True)
class ConnectedPlatformDTO:
    """Connected platform aggregate DTO."""

    platform: str
    serverCount: int
    isActive: bool


@dataclass(slots=True, frozen=True)
class ConnectedPlatformsResponse:
    """Response DTO for connected-platforms."""

    success: bool
    connectedPlatforms: list[ConnectedPlatformDTO]


@dataclass(slots=True, frozen=True)
class CommandDTO:
    """Commands reference DTO -- snake_case verbatim, matching Node's raw row passthrough."""

    id: int
    command: str
    module_name: str
    description: str | None
    category: str | None
    permission_level: str | None
    platforms: Any
    is_enabled: bool


@dataclass(slots=True, frozen=True)
class CommandsResponse:
    """Response DTO for get-commands -- no `success` wrapper, matches Node exactly."""

    commands: list[CommandDTO]


# ── Routes ───────────────────────────────────────────────────────────────


@admin_bp.route("/<int:community_id>/settings", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community:admin")  # type: ignore[misc]
@validate_response(SettingsResponse)
async def get_settings(community_id: int) -> SettingsResponse | tuple[dict[str, object], int]:
    """Get community settings."""
    async_dal, dal = _dal()
    ctx = get_tenant_context(request)
    assert ctx is not None  # tenant_middleware guarantees this on the success path
    try:
        row = await svc.get_community_settings(async_dal, dal, community_id=community_id, ctx=ctx)
    except ApiError as exc:
        return _err(exc)
    config = dict(row.config or {})
    return SettingsResponse(
        success=True,
        settings=SettingsDTO(
            name=row.name,
            displayName=row.display_name,
            description=row.description,
            platform=row.platform,
            isPublic=bool(row.is_public),
            joinMode=row.join_mode or "open",
            channelCreationPolicy=config.get("channel_creation_policy", "admin_only"),
            config=config,
        ),
    )


@admin_bp.route("/<int:community_id>/settings", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community:admin")  # type: ignore[misc]
@validate_request(UpdateSettingsRequest)
@validate_response(MessageResponse)
async def update_settings(
    data: UpdateSettingsRequest, community_id: int
) -> MessageResponse | tuple[dict[str, object], int]:
    """Update community settings."""
    async_dal, dal = _dal()
    ctx = get_tenant_context(request)
    assert ctx is not None
    try:
        await svc.update_community_settings(
            async_dal,
            dal,
            community_id=community_id,
            ctx=ctx,
            display_name=data.displayName,
            description=data.description,
            is_public=data.isPublic,
            join_mode=data.joinMode,
            channel_creation_policy=data.channelCreationPolicy,
            config=data.config,
        )
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message="Settings updated")


@admin_bp.route("/<int:community_id>/members", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community:admin")  # type: ignore[misc]
@validate_response(MembersResponse)
async def get_members(community_id: int) -> MembersResponse | tuple[dict[str, object], int]:
    """Get community members."""
    async_dal, dal = _dal()
    ctx = get_tenant_context(request)
    assert ctx is not None
    page = int(request.args.get("page", "1"))
    limit = parse_limit(request.args.get("limit"), default=25)
    search = request.args.get("search", "")
    role = request.args.get("role")
    try:
        rows, total, total_pages = await svc.get_members(
            async_dal,
            dal,
            community_id=community_id,
            ctx=ctx,
            page=page,
            limit=limit,
            search=search,
            role=role,
        )
    except ApiError as exc:
        return _err(exc)
    members = [
        MemberDTO(
            id=member.id,
            userId=member.user_id,
            username=getattr(user, "username", None),
            email=getattr(user, "email", None),
            avatarUrl=getattr(user, "avatar_url", None),
            role=member.role,
            reputation=_reputation_dto(member.reputation),
            joinedAt=_iso(member.joined_at),
        )
        for member, user in rows
    ]
    return MembersResponse(
        success=True,
        members=members,
        pagination=PaginationDTO(
            page=max(1, page), limit=min(100, max(1, limit)), total=total, totalPages=total_pages
        ),
    )


@admin_bp.route("/<int:community_id>/members/<int:user_id>/role", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community:admin")  # type: ignore[misc]
@validate_request(UpdateRoleRequest)
@validate_response(MessageResponse)
async def update_member_role(
    data: UpdateRoleRequest, community_id: int, user_id: int
) -> MessageResponse | tuple[dict[str, object], int]:
    """Update a member's role."""
    async_dal, dal = _dal()
    ctx = get_tenant_context(request)
    assert ctx is not None
    caller_id = get_optional_current_user_id(request)
    try:
        await svc.update_member_role(
            async_dal,
            dal,
            community_id=community_id,
            user_id=user_id,
            ctx=ctx,
            role=data.role,
            caller_id=caller_id,
        )
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message="Role updated")


@admin_bp.route("/<int:community_id>/members/<int:user_id>/reputation", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community:admin")  # type: ignore[misc]
@validate_request(AdjustReputationRequest)
# NOT @validate_response -- update_async + response returned to caller
# hits the crash class documented in services/dto_response.py.
async def adjust_reputation(
    data: AdjustReputationRequest, community_id: int, user_id: int
) -> tuple[Any, int]:
    """Adjust or set a member's reputation score."""
    async_dal, dal = _dal()
    ctx = get_tenant_context(request)
    assert ctx is not None
    try:
        new_score, change = await svc.adjust_reputation(
            async_dal,
            dal,
            community_id=community_id,
            user_id=user_id,
            ctx=ctx,
            amount=data.amount,
            set_to=data.setTo,
        )
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(
        ReputationResponse(success=True, reputation=_reputation_dto(new_score), change=change)
    )


@admin_bp.route("/<int:community_id>/members/<int:user_id>", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community:admin")  # type: ignore[misc]
@validate_request(RemoveMemberRequest)
@validate_response(MessageResponse)
async def remove_member(
    data: RemoveMemberRequest, community_id: int, user_id: int
) -> MessageResponse | tuple[dict[str, object], int]:
    """Remove (deactivate) a community member."""
    async_dal, dal = _dal()
    ctx = get_tenant_context(request)
    assert ctx is not None
    caller_id = get_optional_current_user_id(request)
    try:
        await svc.remove_member(
            async_dal,
            dal,
            community_id=community_id,
            user_id=user_id,
            ctx=ctx,
            caller_id=caller_id,
            reason=data.reason,
        )
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message="Member removed")


@admin_bp.route("/<int:community_id>/temp-password", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community:admin")  # type: ignore[misc]
@validate_request(TempPasswordRequest)
# NOT @validate_response -- insert_async + response hits the crash class
# documented in services/dto_response.py.
async def generate_temp_password(data: TempPasswordRequest, community_id: int) -> tuple[Any, int]:
    """Generate a one-time temp password for a community member.

    **Contract note:** mounted at `/<communityId>/temp-password` (no
    `/members/<userId>` segment) to match `api.js`'s
    `adminApi.generateTempPassword(communityId, data)` exactly --
    `routes/admin.js`'s own Express route
    (`/:communityId/members/:userId/temp-password`) is itself inconsistent
    with both the frontend call AND `generateTempPassword()`'s own body
    (which reads only `req.params.communityId`, never `userId`); the
    controller function's actual `req.params` usage is the tie-breaker.
    """
    async_dal, dal = _dal()
    ctx = get_tenant_context(request)
    assert ctx is not None
    caller_id = get_optional_current_user_id(request)
    try:
        temp_password, expires_at = await svc.generate_temp_password(
            async_dal,
            dal,
            community_id=community_id,
            ctx=ctx,
            user_identifier=data.userIdentifier,
            force_oauth_link=data.forceOAuthLink,
            expires_in_hours=data.expiresInHours,
            caller_id=caller_id,
        )
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(
        TempPasswordResponse(
            success=True,
            tempPassword=temp_password,
            expiresAt=expires_at.isoformat(),
            instructions=(
                f"Share this password with the user. It expires in "
                f"{data.expiresInHours} hours and can only be used once."
            ),
        ),
        201,
    )


@admin_bp.route("/<int:community_id>/modules", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community:admin")  # type: ignore[misc]
@validate_response(ModulesResponse)
async def get_modules(community_id: int) -> ModulesResponse | tuple[dict[str, object], int]:
    """Get installed modules for a community."""
    async_dal, dal = _dal()
    ctx = get_tenant_context(request)
    assert ctx is not None
    try:
        rows = await svc.get_modules(async_dal, dal, community_id=community_id, ctx=ctx)
    except ApiError as exc:
        return _err(exc)
    modules = [
        ModuleDTO(
            installationId=install.id,
            moduleId=install.module_id,
            name=getattr(mod, "name", None),
            displayName=getattr(mod, "display_name", None) or getattr(mod, "name", None),
            description=getattr(mod, "description", None),
            category=getattr(mod, "category", None),
            isEnabled=bool(install.is_enabled),
            config=install.config,
            installedAt=_iso(install.installed_at),
        )
        for install, mod in rows
    ]
    return ModulesResponse(success=True, modules=modules)


@admin_bp.route("/<int:community_id>/modules/<int:module_id>/config", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community:admin")  # type: ignore[misc]
@validate_request(UpdateModuleConfigRequest)
@validate_response(MessageResponse)
async def update_module_config(
    data: UpdateModuleConfigRequest, community_id: int, module_id: int
) -> MessageResponse | tuple[dict[str, object], int]:
    """Update a community's module configuration."""
    async_dal, dal = _dal()
    ctx = get_tenant_context(request)
    assert ctx is not None
    try:
        await svc.update_module_config(
            async_dal,
            dal,
            community_id=community_id,
            module_id=module_id,
            ctx=ctx,
            config=data.config,
            is_enabled=data.isEnabled,
        )
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message="Module configuration updated")


@admin_bp.route("/<int:community_id>/connected-platforms", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community:admin")  # type: ignore[misc]
@validate_response(ConnectedPlatformsResponse)
async def get_connected_platforms(
    community_id: int,
) -> ConnectedPlatformsResponse | tuple[dict[str, object], int]:
    """Get connected platforms aggregate for a community."""
    async_dal, dal = _dal()
    ctx = get_tenant_context(request)
    assert ctx is not None
    try:
        platforms = await svc.get_connected_platforms(
            async_dal, dal, community_id=community_id, ctx=ctx
        )
    except ApiError as exc:
        return _err(exc)
    return ConnectedPlatformsResponse(
        success=True, connectedPlatforms=[ConnectedPlatformDTO(**p) for p in platforms]
    )


@admin_bp.route("/<int:community_id>/commands", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community:admin")  # type: ignore[misc]
@validate_response(CommandsResponse)
async def get_commands(community_id: int) -> CommandsResponse | tuple[dict[str, object], int]:
    """Get the commands reference list."""
    async_dal, dal = _dal()
    ctx = get_tenant_context(request)
    assert ctx is not None
    try:
        commands = await svc.get_commands(async_dal, dal, community_id=community_id, ctx=ctx)
    except ApiError as exc:
        return _err(exc)
    return CommandsResponse(commands=[CommandDTO(**c) for c in commands])


BLUEPRINTS: list[Blueprint] = [admin_bp]
