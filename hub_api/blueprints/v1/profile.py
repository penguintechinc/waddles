"""v1 `user profile` group -- ported from `profileController.js` (self-service subset).

Mounted at `/api/v1/user` (matches `routes/user.js`'s profile block:
`/profile`, `/profile/avatar`, `/linked-platforms`). Public/community
profile viewing (`getPublicProfile`/`getMemberProfile`) is out of scope
-- see `services/profile_service.py`'s module docstring. Avatar
upload/delete uses `services/storage_service.py` (S3-compatible only, no
local-disk fallback -- hub-api's rootless contract).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.tenancy import tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services import profile_service
from services.current_user import get_current_user_id
from services.dto_response import jsonify_dto
from services.errors import ApiError, bad_request, not_found

profile_bp = Blueprint("v1_user_profile", __name__, url_prefix="/api/v1/user")


def _dal() -> tuple[Any, Any]:
    """Return `(async_dal, dal)` from app config."""
    return current_app.config["async_dal"], current_app.config["dal"]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    """Convert an `ApiError` into the flask_core `error_response()` JSON envelope."""
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


@dataclass(slots=True, frozen=True)
class ProfileDTO:
    """Profile DTO."""

    userId: int
    username: str | None
    displayName: str | None
    avatarUrl: str | None
    bannerUrl: str | None
    bio: str | None
    location: str | None
    locationCity: str | None
    locationState: str | None
    locationCountry: str | None
    websiteUrl: str | None
    visibility: str
    showActivity: bool
    showCommunities: bool
    memberSince: str | None


@dataclass(slots=True, frozen=True)
class ProfileResponse:
    """Response DTO for profile endpoints."""

    success: bool
    profile: ProfileDTO


@dataclass(slots=True, frozen=True)
class UpdateProfileRequest:
    """Request DTO for update profile endpoints."""

    displayName: str | None = None
    bio: str | None = None
    location: str | None = None
    locationCity: str | None = None
    locationState: str | None = None
    locationCountry: str | None = None
    websiteUrl: str | None = None
    visibility: str | None = None
    showActivity: bool | None = None
    showCommunities: bool | None = None


@dataclass(slots=True, frozen=True)
class AvatarResponse:
    """Response DTO for avatar endpoints."""

    success: bool
    avatarUrl: str | None


@dataclass(slots=True, frozen=True)
class LinkedPlatformDTO:
    """Linked platform DTO."""

    platform: str
    username: str | None
    avatarUrl: str | None


@dataclass(slots=True, frozen=True)
class LinkedPlatformsResponse:
    """Response DTO for linked platforms endpoints."""

    success: bool
    linkedPlatforms: list[LinkedPlatformDTO]


def _profile_dto(profile: dict[str, Any]) -> ProfileDTO:
    member_since = profile["memberSince"]
    return ProfileDTO(
        userId=profile["userId"],
        username=profile["username"],
        displayName=profile["displayName"],
        avatarUrl=profile["avatarUrl"],
        bannerUrl=profile["bannerUrl"],
        bio=profile["bio"],
        location=profile["location"],
        locationCity=profile["locationCity"],
        locationState=profile["locationState"],
        locationCountry=profile["locationCountry"],
        websiteUrl=profile["websiteUrl"],
        visibility=profile["visibility"],
        showActivity=bool(profile["showActivity"]),
        showCommunities=bool(profile["showCommunities"]),
        memberSince=member_since.isoformat() if member_since else None,
    )


@profile_bp.route("/profile", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_response(ProfileResponse)
async def get_my_profile() -> ProfileResponse | tuple[dict[str, object], int]:
    """Get my profile."""
    async_dal, dal = _dal()
    try:
        user_id = get_current_user_id(request)
        profile = await profile_service.get_my_profile(async_dal, dal, user_id=user_id)
        if profile is None:
            raise not_found("User not found")
    except ApiError as exc:
        return _err(exc)
    return ProfileResponse(success=True, profile=_profile_dto(profile))


@profile_bp.route("/profile", methods=["PUT"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_request(UpdateProfileRequest)
# NOT @validate_response -- update_my_profile() inserts/updates
# hub_user_profiles then returns a nested-dataclass response, hitting the
# crash documented in services/dto_response.py. jsonify_dto() is the
# equivalent-safety workaround.
async def update_my_profile(data: UpdateProfileRequest) -> tuple[Any, int]:
    """Update my profile."""
    async_dal, dal = _dal()
    try:
        user_id = get_current_user_id(request)
        profile = await profile_service.update_my_profile(
            async_dal,
            dal,
            user_id=user_id,
            fields={
                "displayName": data.displayName,
                "bio": data.bio,
                "location": data.location,
                "locationCity": data.locationCity,
                "locationState": data.locationState,
                "locationCountry": data.locationCountry,
                "websiteUrl": data.websiteUrl,
                "visibility": data.visibility,
                "showActivity": data.showActivity,
                "showCommunities": data.showCommunities,
            },
        )
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(ProfileResponse(success=True, profile=_profile_dto(profile)))


@profile_bp.route("/profile/avatar", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_response(AvatarResponse)
async def upload_avatar() -> AvatarResponse | tuple[dict[str, object], int]:
    """Upload avatar."""
    async_dal, dal = _dal()
    try:
        user_id = get_current_user_id(request)
        files = await request.files
        upload = files.get("avatar")
        if upload is None:
            raise bad_request("No file uploaded")
        data = upload.stream.read()
        avatar_url = await profile_service.upload_my_avatar(
            async_dal,
            dal,
            user_id=user_id,
            data=data,
            filename=upload.filename or "avatar",
            content_type=upload.content_type or "application/octet-stream",
            size=len(data),
        )
    except ApiError as exc:
        return _err(exc)
    return AvatarResponse(success=True, avatarUrl=avatar_url)


@profile_bp.route("/profile/avatar", methods=["DELETE"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_response(AvatarResponse)
async def delete_avatar() -> AvatarResponse | tuple[dict[str, object], int]:
    """Delete avatar."""
    async_dal, dal = _dal()
    try:
        user_id = get_current_user_id(request)
        avatar_url = await profile_service.delete_my_avatar(async_dal, dal, user_id=user_id)
    except ApiError as exc:
        return _err(exc)
    return AvatarResponse(success=True, avatarUrl=avatar_url)


@profile_bp.route("/linked-platforms", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_response(LinkedPlatformsResponse)
async def get_my_linked_platforms() -> LinkedPlatformsResponse:
    """Get my linked platforms."""
    async_dal, dal = _dal()
    user_id = get_current_user_id(request)
    platforms = await profile_service.get_my_linked_platforms(async_dal, dal, user_id=user_id)
    return LinkedPlatformsResponse(
        success=True,
        linkedPlatforms=[
            LinkedPlatformDTO(platform=p.platform, username=p.username, avatarUrl=p.avatar_url)
            for p in platforms
        ],
    )


BLUEPRINTS: list[Blueprint] = [profile_bp]
