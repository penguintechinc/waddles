"""v1 `community profile` group -- ported from `communityProfileController.js`.

Core-tenancy entity (the community ENTITY, not the Community engagement
module -- see `hub_api/PORTING.md`). Mounted at `/api/v1` with full paths
matching Node's real routes exactly:
`GET /api/v1/public/communities/<id>/profile` (`routes/public.js`, pre-auth
-- optional viewer JWT for visibility personalization, not required) and
`PUT/POST/DELETE /api/v1/admin/<communityId>/{profile,logo,banner}`
(`routes/admin.js`, `tenant_middleware` + a DB-backed per-community admin
check -- see `services/community_authz.py`'s own docstring for why a flat
`require_scope` is not safe to use alone for a `communityId` path param).

SECURITY: `update_community_profile`/logo/banner are NOT gated by
`flask_core.authz.require_scope` -- `services.community_profile_service`
calls `services.community_authz.require_community_scope` internally,
resolving the caller's role from a live `community_members` DB row keyed
on `(community_id_from_url, user_id_from_JWT)`, never trusting the URL's
`communityId` alone (the IDOR class this module's own security review
flagged in the Node source: a flat community-shaped scope claim would let
any caller with that scope act on ANY community, not just ones they
actually administer).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.tenancy import tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services import community_profile_service as svc
from services.current_user import get_current_user_id, get_optional_current_user_id
from services.dto_response import jsonify_dto
from services.errors import ApiError, bad_request

community_profile_bp = Blueprint("v1_community_profile", __name__, url_prefix="/api/v1")


def _dal() -> tuple[Any, Any]:
    """Return `(async_dal, dal)` from app config."""
    return current_app.config["async_dal"], current_app.config["dal"]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    """Convert an `ApiError` into the flask_core `error_response()` JSON envelope."""
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


@dataclass(slots=True, frozen=True)
class CommunityProfileDTO:
    """Community profile DTO -- matches Node's `formatCommunity()` shape (camelCase)."""

    id: int
    name: str
    displayName: str | None
    description: str | None
    aboutExtended: str | None
    socialLinks: dict[str, Any]
    websiteUrl: str | None
    discordInviteUrl: str | None
    platform: str | None
    memberCount: int
    isPublic: bool
    joinMode: str | None
    visibility: str
    logoUrl: str | None
    bannerUrl: str | None
    ownerUsername: str | None
    createdAt: str | None
    restricted: bool


@dataclass(slots=True, frozen=True)
class CommunityProfileResponse:
    """Response DTO for community profile endpoints."""

    success: bool
    community: CommunityProfileDTO


@dataclass(slots=True, frozen=True)
class UpdateCommunityProfileRequest:
    """Request DTO for `PUT /api/v1/admin/<communityId>/profile`."""

    displayName: str | None = None
    description: str | None = None
    aboutExtended: str | None = None
    socialLinks: dict[str, Any] | None = None
    websiteUrl: str | None = None
    discordInviteUrl: str | None = None
    visibility: str | None = None


@dataclass(slots=True, frozen=True)
class LogoResponse:
    """Response DTO for logo endpoints."""

    success: bool
    logoUrl: str | None = None


@dataclass(slots=True, frozen=True)
class BannerResponse:
    """Response DTO for banner endpoints."""

    success: bool
    bannerUrl: str | None = None


@dataclass(slots=True, frozen=True)
class SimpleSuccessResponse:
    """Bare `{success: true}` response -- delete endpoints."""

    success: bool


def _to_dto(profile: svc.CommunityProfile) -> CommunityProfileDTO:
    return CommunityProfileDTO(
        id=profile.id,
        name=profile.name,
        displayName=profile.display_name,
        description=profile.description,
        aboutExtended=profile.about_extended,
        socialLinks=profile.social_links,
        websiteUrl=profile.website_url,
        discordInviteUrl=profile.discord_invite_url,
        platform=profile.platform,
        memberCount=profile.member_count,
        isPublic=profile.is_public,
        joinMode=profile.join_mode,
        visibility=profile.visibility,
        logoUrl=profile.logo_url,
        bannerUrl=profile.banner_url,
        ownerUsername=profile.owner_username,
        createdAt=profile.created_at,
        restricted=profile.restricted,
    )


@community_profile_bp.route("/public/communities/<int:community_id>/profile", methods=["GET"])
# Pre-auth-shaped (matches Node's `optionalAuth` middleware): an absent
# bearer token is a valid "anonymous viewer" state, not a 401 -- so no
# `@tenant_middleware` here. `get_optional_current_user_id` still resolves
# a *valid* token's subject when one is present, for visibility checks.
async def get_community_profile(community_id: int) -> tuple[Any, int]:
    """Get a community's public profile (visibility-restricted for non-privileged viewers)."""
    async_dal, dal = _dal()
    viewer_id = get_optional_current_user_id(request)
    profile = await svc.get_community_profile(
        async_dal, dal, community_id=community_id, viewer_id=viewer_id
    )
    if profile is None:
        return cast(
            "tuple[Any, int]", error_response("Community not found", 404, "NOT_FOUND")
        )
    return jsonify_dto(CommunityProfileResponse(success=True, community=_to_dto(profile)))


@community_profile_bp.route("/admin/<int:community_id>/profile", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@validate_request(UpdateCommunityProfileRequest)
# NOT @validate_response -- update_community_profile() awaits update_async()
# then returns a nested-dataclass response, hitting the crash documented in
# services/dto_response.py. jsonify_dto() is the equivalent-safety workaround.
async def update_community_profile(
    community_id: int, data: UpdateCommunityProfileRequest
) -> tuple[Any, int]:
    """Update a community's profile (owner/admin only)."""
    async_dal, dal = _dal()
    try:
        user_id = get_current_user_id(request)
        profile = await svc.update_community_profile(
            async_dal,
            dal,
            community_id=community_id,
            user_id=user_id,
            fields={
                "displayName": data.displayName,
                "description": data.description,
                "aboutExtended": data.aboutExtended,
                "socialLinks": data.socialLinks,
                "websiteUrl": data.websiteUrl,
                "discordInviteUrl": data.discordInviteUrl,
                "visibility": data.visibility,
            },
        )
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(CommunityProfileResponse(success=True, community=_to_dto(profile)))


@community_profile_bp.route("/admin/<int:community_id>/logo", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@validate_response(LogoResponse)
async def upload_community_logo(community_id: int) -> LogoResponse | tuple[dict[str, object], int]:
    """Upload a community logo (moderator+)."""
    async_dal, dal = _dal()
    try:
        user_id = get_current_user_id(request)
        files = await request.files
        upload = files.get("logo")
        if upload is None:
            raise bad_request("No file uploaded")
        data = upload.stream.read()
        logo_url = await svc.upload_community_logo(
            async_dal,
            dal,
            community_id=community_id,
            user_id=user_id,
            data=data,
            filename=upload.filename or "logo",
            content_type=upload.content_type or "application/octet-stream",
            size=len(data),
        )
    except ApiError as exc:
        return _err(exc)
    return LogoResponse(success=True, logoUrl=logo_url)


@community_profile_bp.route("/admin/<int:community_id>/logo", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@validate_response(SimpleSuccessResponse)
async def delete_community_logo(
    community_id: int,
) -> SimpleSuccessResponse | tuple[dict[str, object], int]:
    """Delete a community logo (moderator+)."""
    async_dal, dal = _dal()
    try:
        user_id = get_current_user_id(request)
        await svc.delete_community_logo(async_dal, dal, community_id=community_id, user_id=user_id)
    except ApiError as exc:
        return _err(exc)
    return SimpleSuccessResponse(success=True)


@community_profile_bp.route("/admin/<int:community_id>/banner", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@validate_response(BannerResponse)
async def upload_community_banner(
    community_id: int,
) -> BannerResponse | tuple[dict[str, object], int]:
    """Upload a community banner (moderator+)."""
    async_dal, dal = _dal()
    try:
        user_id = get_current_user_id(request)
        files = await request.files
        upload = files.get("banner")
        if upload is None:
            raise bad_request("No file uploaded")
        data = upload.stream.read()
        banner_url = await svc.upload_community_banner(
            async_dal,
            dal,
            community_id=community_id,
            user_id=user_id,
            data=data,
            filename=upload.filename or "banner",
            content_type=upload.content_type or "application/octet-stream",
            size=len(data),
        )
    except ApiError as exc:
        return _err(exc)
    return BannerResponse(success=True, bannerUrl=banner_url)


@community_profile_bp.route("/admin/<int:community_id>/banner", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@validate_response(SimpleSuccessResponse)
async def delete_community_banner(
    community_id: int,
) -> SimpleSuccessResponse | tuple[dict[str, object], int]:
    """Delete a community banner (moderator+)."""
    async_dal, dal = _dal()
    try:
        user_id = get_current_user_id(request)
        await svc.delete_community_banner(
            async_dal, dal, community_id=community_id, user_id=user_id
        )
    except ApiError as exc:
        return _err(exc)
    return SimpleSuccessResponse(success=True)


BLUEPRINTS: list[Blueprint] = [community_profile_bp]
