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

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import document_response, validate_request, validate_response

from services import admin_service as svc
from services import community_reputation_service as reputation_svc
from services.current_user import get_optional_current_user_id
from services.dto_response import jsonify_dto
from services.errors import ApiError
from services.pagination import parse_limit

logger = logging.getLogger(__name__)

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
class ReputationConfigRequest:
    """Request DTO for PUT reputation config -- only supplied fields change.

    Floors are independent, each with its own flag (both default OFF): score < `softBanFloor`
    -> soft ban (cannot WIN), score < `hardBanFloor` -> ban in this community. Must satisfy
    300 <= hardBanFloor < softBanFloor < atRiskThreshold <= 850. Enforcement is the accrual
    bundle's, behind the `waddles.reputation-auto-ban` flag.
    """

    softBanEnabled: bool | None = None
    softBanFloor: int | None = None
    hardBanEnabled: bool | None = None
    hardBanFloor: int | None = None
    atRiskThreshold: int | None = None


@dataclass(slots=True, frozen=True)
class ReputationConfigResponse:
    """Community reputation policy (the 300-850 scale is read-only product policy)."""

    success: bool
    softBanEnabled: bool
    softBanFloor: int
    hardBanEnabled: bool
    hardBanFloor: int
    atRiskThreshold: int
    scoreMin: int
    scoreMax: int
    scoreDefault: int
    updatedAt: str | None


@dataclass(slots=True, frozen=True)
class AtRiskResponse:
    """Members below the community's at-risk threshold, lowest first."""

    success: bool
    members: list[reputation_svc.AtRiskEntryDTO]


@dataclass(slots=True, frozen=True)
class HistoryResponse:
    """Reputation ledger entries, newest first."""

    success: bool
    entries: list[reputation_svc.HistoryEntryDTO]


@dataclass(slots=True, frozen=True)
class SoftBanRequest:
    """Request DTO for a manual soft ban; `expiresAt` (ISO-8601, future) omitted = until cleared."""

    expiresAt: str | None = None


@dataclass(slots=True, frozen=True)
class SoftBansResponse:
    """Active soft bans in the community."""

    success: bool
    softBans: list[reputation_svc.SoftBanEntryDTO]


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
            actor_user_id=get_optional_current_user_id(request),
        )
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(
        ReputationResponse(success=True, reputation=_reputation_dto(new_score), change=change)
    )


def _config_response(cfg: dict[str, Any]) -> ReputationConfigResponse:
    return ReputationConfigResponse(
        success=True,
        softBanEnabled=cfg["soft_ban_enabled"],
        softBanFloor=cfg["soft_ban_floor"],
        hardBanEnabled=cfg["hard_ban_enabled"],
        hardBanFloor=cfg["hard_ban_floor"],
        atRiskThreshold=cfg["at_risk_threshold"],
        scoreMin=cfg["score_min"],
        scoreMax=cfg["score_max"],
        scoreDefault=cfg["score_default"],
        updatedAt=cfg["updated_at"],
    )


async def _authorize_reputation_admin(community_id: int) -> Any:
    """Tenant-scope + community-admin gate shared by every reputation-admin route."""
    async_dal, dal = _dal()
    ctx = get_tenant_context(request)
    assert ctx is not None
    await svc.authorize_reputation_admin(
        async_dal, dal, request, community_id=community_id, ctx=ctx
    )
    return ctx


@admin_bp.route("/<int:community_id>/reputation/config", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@document_response(ReputationConfigResponse)
async def get_reputation_config(community_id: int) -> tuple[Any, int]:
    """Community reputation policy -- community admins of THIS community only."""
    async_dal, dal = _dal()
    try:
        await _authorize_reputation_admin(community_id)
        cfg = await reputation_svc.get_community_config(async_dal, dal, community_id=community_id)
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(_config_response(cfg))


@admin_bp.route("/<int:community_id>/reputation/config", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@validate_request(ReputationConfigRequest)
@document_response(ReputationConfigResponse)
async def put_reputation_config(
    data: ReputationConfigRequest, community_id: int
) -> tuple[Any, int]:
    """Set the community's soft/hard ban floors + flags and at-risk threshold (400 if invalid)."""
    async_dal, dal = _dal()
    try:
        ctx = await _authorize_reputation_admin(community_id)
        cfg = await reputation_svc.set_community_config(
            async_dal,
            dal,
            tenant_id=ctx.tenant_id,
            community_id=community_id,
            soft_ban_enabled=data.softBanEnabled,
            soft_ban_floor=data.softBanFloor,
            hard_ban_enabled=data.hardBanEnabled,
            hard_ban_floor=data.hardBanFloor,
            at_risk_threshold=data.atRiskThreshold,
        )
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(_config_response(cfg))


@admin_bp.route("/<int:community_id>/reputation/at-risk", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@document_response(AtRiskResponse)
async def get_reputation_at_risk(community_id: int) -> tuple[Any, int]:
    """Members below the community's at-risk threshold (`?limit=`, default 50)."""
    async_dal, dal = _dal()
    try:
        await _authorize_reputation_admin(community_id)
        members = await reputation_svc.get_at_risk(
            async_dal,
            dal,
            community_id=community_id,
            limit=parse_limit(request.args.get("limit"), default=50),
        )
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(AtRiskResponse(success=True, members=members))


@admin_bp.route("/<int:community_id>/reputation/history", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@document_response(HistoryResponse)
async def get_reputation_history(community_id: int) -> tuple[Any, int]:
    """Reputation ledger, newest first (`?userId=` hub user id, `?limit=`, `?offset=`)."""
    async_dal, dal = _dal()
    raw_user = request.args.get("userId")
    if raw_user is not None and not raw_user.isdigit():
        return _err(ApiError("Invalid userId", 400, "BAD_REQUEST"))
    raw_offset = request.args.get("offset", "0")
    if not raw_offset.isdigit():
        return _err(ApiError("Invalid offset", 400, "BAD_REQUEST"))
    try:
        await _authorize_reputation_admin(community_id)
        entries = await reputation_svc.get_history(
            async_dal,
            dal,
            community_id=community_id,
            user_id=int(raw_user) if raw_user is not None else None,
            limit=parse_limit(request.args.get("limit"), default=50),
            offset=int(raw_offset),
        )
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(HistoryResponse(success=True, entries=entries))


@admin_bp.route("/<int:community_id>/reputation/soft-bans", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@document_response(SoftBansResponse)
async def list_reputation_soft_bans(community_id: int) -> tuple[Any, int]:
    """Active soft bans (members who cannot WIN in this community)."""
    async_dal, dal = _dal()
    try:
        await _authorize_reputation_admin(community_id)
        bans = await reputation_svc.list_soft_bans(
            async_dal,
            dal,
            community_id=community_id,
            limit=parse_limit(request.args.get("limit"), default=50),
        )
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(SoftBansResponse(success=True, softBans=bans))


@admin_bp.route("/<int:community_id>/reputation/soft-bans/<int:user_id>", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@validate_request(SoftBanRequest)
@document_response(MessageResponse)
async def put_reputation_soft_ban(
    data: SoftBanRequest, community_id: int, user_id: int
) -> tuple[Any, int]:
    """Manually soft-ban a member of this community (audited)."""
    async_dal, dal = _dal()
    try:
        expires = datetime.fromisoformat(data.expiresAt) if data.expiresAt else None
    except ValueError:
        # The raw value is caller input and is deliberately not logged.
        logger.info("reputation.soft_ban.invalid_expires_at community_id=%s", community_id)
        return _err(ApiError("Invalid expiresAt", 400, "BAD_REQUEST"))
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    try:
        ctx = await _authorize_reputation_admin(community_id)
        await reputation_svc.set_soft_ban(
            async_dal,
            dal,
            tenant_id=ctx.tenant_id,
            community_id=community_id,
            user_id=user_id,
            expires_at=expires,
            actor_user_id=get_optional_current_user_id(request),
        )
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(MessageResponse(success=True, message="Soft ban set"))


@admin_bp.route("/<int:community_id>/reputation/soft-bans/<int:user_id>", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@document_response(MessageResponse)
async def delete_reputation_soft_ban(community_id: int, user_id: int) -> tuple[Any, int]:
    """Clear a member's soft ban (audited)."""
    async_dal, dal = _dal()
    try:
        ctx = await _authorize_reputation_admin(community_id)
        cleared = await reputation_svc.clear_soft_ban(
            async_dal,
            dal,
            tenant_id=ctx.tenant_id,
            community_id=community_id,
            user_id=user_id,
            actor_user_id=get_optional_current_user_id(request),
        )
    except ApiError as exc:
        return _err(exc)
    if not cleared:
        return _err(ApiError("Member is not soft-banned", 404, "NOT_FOUND"))
    return jsonify_dto(MessageResponse(success=True, message="Soft ban cleared"))


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
