"""v1 `user identities` group -- ported from `identityController.js`.

Mounted at `/api/v1/user/identities` (matches `routes/user.js`'s
`router.use(requireAuth)` + identity-linking block). Every route is
self-service (the caller's own linked platform identities) --
`tenant_middleware` only, no `require_scope`: see `blueprints/v1/auth.py`
module docstring and `services/current_user.py` for why. `getOrCreateHubUser`
(Node's pre-unified-auth fallback) is deliberately not ported --
`services/identity_service.py`'s own docstring covers why it's dead code
under the current login flow.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.tenancy import tenant_middleware
from quart import Blueprint, current_app, redirect, request
from quart_schema import validate_request, validate_response

from config import HubAPIConfig
from services import identity_service
from services.current_user import get_current_user_id
from services.errors import ApiError

identity_bp = Blueprint("v1_user_identities", __name__, url_prefix="/api/v1/user/identities")


def _cfg() -> HubAPIConfig:
    """Return the app's `HubAPIConfig`."""
    return cast(HubAPIConfig, current_app.config["HUB_API_CONFIG"])


def _dal() -> tuple[Any, Any]:
    """Return `(async_dal, dal)` from app config."""
    return current_app.config["async_dal"], current_app.config["dal"]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    """Convert an `ApiError` into the flask_core `error_response()` JSON envelope."""
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


@dataclass(slots=True, frozen=True)
class IdentityDTO:
    """Identity DTO."""

    id: int
    platform: str
    platformUserId: str
    platformUsername: str | None
    avatarUrl: str | None
    isPrimary: bool
    linkedAt: str | None
    lastUsed: str | None


@dataclass(slots=True, frozen=True)
class ListIdentitiesResponse:
    """Response DTO for list identities endpoints."""

    success: bool
    identities: list[IdentityDTO]


@dataclass(slots=True, frozen=True)
class PrimaryIdentityResponse:
    """Response DTO for primary identity endpoints."""

    success: bool
    identity: IdentityDTO | None


@dataclass(slots=True, frozen=True)
class SetPrimaryIdentityRequest:
    """Request DTO for set primary identity endpoints."""

    platform: str


@dataclass(slots=True, frozen=True)
class MessageResponse:
    """Response DTO for message endpoints."""

    success: bool
    message: str


@dataclass(slots=True, frozen=True)
class SimpleSuccessResponse:
    """Response DTO for simple success endpoints."""

    success: bool


@dataclass(slots=True, frozen=True)
class LinkStartResponse:
    """Response DTO for link start endpoints."""

    success: bool
    authorizeUrl: str
    state: str


def _dto(row: object) -> IdentityDTO:
    return IdentityDTO(
        id=row.id,  # type: ignore[attr-defined]
        platform=row.platform,  # type: ignore[attr-defined]
        platformUserId=row.platform_user_id,  # type: ignore[attr-defined]
        platformUsername=row.platform_username,  # type: ignore[attr-defined]
        avatarUrl=row.avatar_url,  # type: ignore[attr-defined]
        isPrimary=bool(row.is_primary),  # type: ignore[attr-defined]
        linkedAt=row.linked_at.isoformat() if row.linked_at else None,  # type: ignore[attr-defined]
        lastUsed=row.last_used.isoformat() if row.last_used else None,  # type: ignore[attr-defined]
    )


@identity_bp.route("", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@validate_response(ListIdentitiesResponse)
async def list_identities() -> ListIdentitiesResponse:
    """List identities."""
    async_dal, dal = _dal()
    user_id = get_current_user_id(request)
    rows = await identity_service.list_identities(async_dal, dal, user_id=user_id)
    return ListIdentitiesResponse(success=True, identities=[_dto(r) for r in rows])


@identity_bp.route("/primary", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@validate_response(PrimaryIdentityResponse)
async def get_primary_identity() -> PrimaryIdentityResponse:
    """Get primary identity."""
    async_dal, dal = _dal()
    user_id = get_current_user_id(request)
    row = await identity_service.get_primary_identity(async_dal, dal, user_id=user_id)
    return PrimaryIdentityResponse(success=True, identity=_dto(row) if row else None)


@identity_bp.route("/primary", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@validate_request(SetPrimaryIdentityRequest)
@validate_response(MessageResponse)
async def set_primary_identity(
    data: SetPrimaryIdentityRequest,
) -> MessageResponse | tuple[dict[str, object], int]:
    """Set primary identity."""
    async_dal, dal = _dal()
    try:
        user_id = get_current_user_id(request)
        await identity_service.set_primary_identity(
            async_dal, dal, user_id=user_id, platform=data.platform
        )
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message="Primary identity updated")


@identity_bp.route("/link/<platform>", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@validate_response(LinkStartResponse)
async def start_identity_link(platform: str) -> LinkStartResponse | tuple[dict[str, object], int]:
    """Start identity link."""
    async_dal, dal = _dal()
    try:
        user_id = get_current_user_id(request)
        authorize_url, state = await identity_service.start_identity_link(
            async_dal,
            dal,
            user_id=user_id,
            platform=platform,
            callback_base_url=_cfg().identity_callback_base_url,
        )
    except ApiError as exc:
        return _err(exc)
    return LinkStartResponse(success=True, authorizeUrl=authorize_url, state=state)


@identity_bp.route("/link/<platform>/callback", methods=["GET"])
async def identity_link_callback(platform: str):  # type: ignore[no-untyped-def]
    """Identity link callback."""
    async_dal, dal = _dal()
    code = request.args.get("code")
    state = request.args.get("state")
    frontend_origin = _cfg().frontend_origin

    if not code or not state:
        return redirect(f"{frontend_origin}/settings/identities?error=missing_params")
    try:
        await identity_service.identity_link_callback(
            async_dal,
            dal,
            platform=platform,
            code=code,
            state=state,
            callback_base_url=_cfg().identity_callback_base_url,
        )
    except ApiError:
        return redirect(f"{frontend_origin}/settings/identities?error=linking_failed")
    return redirect(f"{frontend_origin}/settings/identities?success=linked&platform={platform}")


@identity_bp.route("/<platform>", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@validate_response(MessageResponse)
async def unlink_identity(platform: str) -> MessageResponse | tuple[dict[str, object], int]:
    """Unlink identity."""
    async_dal, dal = _dal()
    try:
        user_id = get_current_user_id(request)
        await identity_service.unlink_identity(async_dal, dal, user_id=user_id, platform=platform)
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message="Identity unlinked successfully")


BLUEPRINTS: list[Blueprint] = [identity_bp]
