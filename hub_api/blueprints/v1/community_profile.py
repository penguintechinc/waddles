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
from flask_core.cat_auth import CAT_SELF_ONLY, allow_cat, cat_bound_community, get_cat_principal
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services import community_profile_service as svc
from services.community_public_id import lookup_public_id
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

    id: str
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


async def _to_dto(profile: svc.CommunityProfile) -> CommunityProfileDTO:
    """Profile DTO; `id` is the community's PUBLIC UUID (never the numeric id)."""
    return CommunityProfileDTO(
        id=await lookup_public_id(profile.id),
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


@community_profile_bp.route("/public/communities/<cuuid:community_id>/profile", methods=["GET"])
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
        return cast("tuple[Any, int]", error_response("Community not found", 404, "NOT_FOUND"))
    return jsonify_dto(CommunityProfileResponse(success=True, community=await _to_dto(profile)))


@community_profile_bp.route("/admin/<cuuid:community_id>/profile", methods=["PUT"])
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
    return jsonify_dto(CommunityProfileResponse(success=True, community=await _to_dto(profile)))


@community_profile_bp.route("/admin/<cuuid:community_id>/logo", methods=["POST"])
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


@community_profile_bp.route("/admin/<cuuid:community_id>/logo", methods=["DELETE"])
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


@community_profile_bp.route("/admin/<cuuid:community_id>/banner", methods=["POST"])
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


@community_profile_bp.route("/admin/<cuuid:community_id>/banner", methods=["DELETE"])
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


@dataclass(slots=True, frozen=True)
class MyCommunityDTO:
    """Community summary returned by `GET /api/v1/communities/my`."""

    id: str
    name: str
    displayName: str
    role: str


@dataclass(slots=True, frozen=True)
class MyCommunitiesResponse:
    """Response DTO for `GET /api/v1/communities/my`."""

    communities: list[MyCommunityDTO]


@community_profile_bp.route("/communities/my", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@allow_cat(scopes=CAT_SELF_ONLY, community_from=cat_bound_community)  # type: ignore[misc]
@validate_response(MyCommunitiesResponse)
async def list_my_communities() -> MyCommunitiesResponse | tuple[dict[str, object], int]:
    """`GET /api/v1/communities/my` -- communities the caller (JWT user or CAT) can act on.

    A CAT (opted in as `CAT_SELF_ONLY`: token introspection, no capability scope) sees ONLY its
    own bound community; it never resolves to the human who minted it.
    """
    _, dal = _dal()
    ctx = get_tenant_context(request)
    if ctx is None:
        return cast(
            tuple[dict[str, object], int],
            error_response("Tenant context missing", 403, "FORBIDDEN"),
        )

    items: list[MyCommunityDTO] = []
    cat = get_cat_principal(request)
    if cat is not None:
        row = (
            dal(
                (dal.communities.id == cat.community_id)
                & (dal.communities.tenant_id == ctx.tenant_id)
            )
            .select()
            .first()
        )
        if row is not None:
            public_id = (await lookup_public_id(int(row.id))) or str(row.id)
            name = str(getattr(row, "name", "") or "")
            display_name = str(getattr(row, "display_name", None) or name)
            items.append(
                MyCommunityDTO(
                    id=str(public_id),
                    name=name,
                    displayName=display_name,
                    role="token",
                )
            )
        return MyCommunitiesResponse(communities=items)

    try:
        user_id = get_current_user_id(request)
    except ApiError as exc:
        current_app.logger.warning("communities/my: user auth failed (%s)", exc.code)
        return _err(exc)

    if "community_members" in getattr(dal, "tables", ()):
        member_rows = dal(
            (dal.community_members.user_id == str(user_id))
            & (dal.community_members.is_active == True)  # noqa: E712
        ).select()
        community_ids = [int(m.community_id) for m in member_rows]
    else:
        community_ids = []

    if community_ids:
        comm_rows = dal(
            (dal.communities.id.belongs(community_ids))
            & (dal.communities.tenant_id == ctx.tenant_id)
        ).select()
        for row in comm_rows:
            public_id = (await lookup_public_id(int(row.id))) or str(row.id)
            name = str(getattr(row, "name", "") or "")
            display_name = str(getattr(row, "display_name", None) or name)
            items.append(
                MyCommunityDTO(
                    id=str(public_id),
                    name=name,
                    displayName=display_name,
                    role="member",
                )
            )

    return MyCommunitiesResponse(communities=items)


BLUEPRINTS: list[Blueprint] = [community_profile_bp]
