"""v1 `music` group -- ported from `musicController.js` (M7 Streaming module).

Mounted at `/api/v1/admin/<community_id>/music/*`, matching
`frontend/src/services/api.js`'s `musicApi`/inline `getMusicSettings`-family
calls byte-for-byte (see `hub_api/PORTING.md`'s checklist). Node's own
`routes/music.js` (which would mount this same contract) is never wired
into `routes/index.js` -- `services/music_service.py`'s module docstring
covers that gap and the resulting 5-endpoint contract-vs-implementation
mismatch in `api.js` in full; only the 8 real `musicController.js`
handlers are ported here.

Auth pattern deviation from `blueprints/v1/auth.py`'s `tenant_middleware`
+ `require_scope(...)` copy-me recipe (documented, not accidental): every
route here is gated by Node's `requireCommunityAdmin`, a DB-backed check
scoped to the SPECIFIC `:communityId` in the path, not a global JWT scope
claim -- `services.community_authz.authorize_community()`'s own module
docstring explains why `require_scope` cannot express this (no `teams`
claim in this codebase's JWT yet) and why skipping the DB check would be
an IDOR (any caller with an admin-flavored token could pass an arbitrary
communityId). `tenant_middleware` still runs first (security.md ordering
contract) -- `authorize_community()` reads the `TenantContext` it
publishes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services import music_service as svc
from services.community_authz import authorize_community
from services.current_user import get_optional_current_user_id
from services.dto_response import jsonify_dto
from services.errors import ApiError, bad_request, payment_required
from services.music_service import MusicProviderDTO, MusicSettingsDTO, RadioStationDTO
from services.pagination import parse_limit
from services.schema import bind_streaming_tables

music_bp = Blueprint("v1_music", __name__, url_prefix="/api/v1/admin")

#: Two-gate Feature flag -- `libs/streaming_module/features.py`'s
#: `streaming.music_station` Feature contract, free tier.
FEATURE_STREAMING_MUSIC_STATION = "waddles.streaming.music_station"


def _dal() -> tuple[Any, Any]:
    """Return `(async_dal, dal)` from app config, ensuring this group's tables are bound.

    `bind_streaming_tables()` is idempotent per-DAL-instance (see its own
    docstring) -- called on every request rather than at `app.py` startup
    because this port's explicit scope forbids editing `app.py`/
    `routers/*.py`/`blueprints/__init__.py` (blueprint auto-discovery is
    the only wiring point; table binding has no equivalent hook).
    """
    async_dal, dal = current_app.config["async_dal"], current_app.config["dal"]
    bind_streaming_tables(dal)
    return async_dal, dal


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    """Convert an `ApiError` into the flask_core `error_response()` JSON envelope."""
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


# ---------------------------------------------------------------------------
# DTOs -- camelCase field names deliberately break PEP8 snake_case
# convention: wire contracts pinned to `frontend/src/services/api.js`
# (security.md Output Validation), same rationale `blueprints/v1/auth.py`
# documents.
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class UpdateMusicSettingsRequest:
    """Request DTO for `PUT .../music/settings` -- every field optional (partial update)."""

    defaultProvider: str | None = None
    autoplayEnabled: bool | None = None
    volumeLimit: int | None = None
    allowedGenres: list[str] | None = None
    blockedArtists: list[str] | None = None
    requireDjApproval: bool | None = None
    isActive: bool | None = None


@dataclass(slots=True, frozen=True)
class MusicSettingsResponse:
    """Response DTO for the music-settings endpoints."""

    success: bool
    settings: MusicSettingsDTO


@dataclass(slots=True, frozen=True)
class MusicProvidersResponse:
    """Response DTO for `GET .../music/providers`."""

    success: bool
    providers: list[MusicProviderDTO]


@dataclass(slots=True, frozen=True)
class StartOAuthRequest:
    """Request DTO for `POST .../music/providers/<provider>/oauth`."""

    redirectUri: str


@dataclass(slots=True, frozen=True)
class StartOAuthResponse:
    """Response DTO for the OAuth-start endpoint."""

    success: bool
    authUrl: str
    stateToken: str


@dataclass(slots=True, frozen=True)
class MessageResponse:
    """Generic `{success, message}` response DTO."""

    success: bool
    message: str


@dataclass(slots=True, frozen=True)
class PaginationDTO:
    """Pagination metadata -- mirrors Node's `{page, limit, total, pages}`."""

    page: int
    limit: int
    total: int
    pages: int


@dataclass(slots=True, frozen=True)
class RadioStationsResponse:
    """Response DTO for `GET .../music/radio-stations`."""

    success: bool
    pagination: PaginationDTO
    stations: list[RadioStationDTO]


@dataclass(slots=True, frozen=True)
class AddRadioStationRequest:
    """Request DTO for `POST .../music/radio-stations`."""

    name: str
    url: str
    description: str | None = None
    genre: str | None = None
    isActive: bool | None = None


@dataclass(slots=True, frozen=True)
class RadioStationResponse:
    """Response DTO for the add-radio-station endpoint."""

    success: bool
    station: RadioStationDTO


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@music_bp.route("/<int:community_id>/music/settings", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@validate_response(MusicSettingsResponse)
async def get_music_settings(
    community_id: int,
) -> MusicSettingsResponse | tuple[dict[str, object], int]:
    """Get music settings for a community."""
    async_dal, dal = _dal()
    try:
        await authorize_community(request, async_dal, dal, community_id=community_id, admin=True)
        ctx = get_tenant_context(request)
        assert ctx is not None  # nosec B101 -- tenant_middleware guarantees this
        if not await feature_enabled(FEATURE_STREAMING_MUSIC_STATION, tenant=ctx.tenant_slug):
            raise payment_required("The Music Station is not enabled for this plan")
        settings = await svc.get_music_settings(async_dal, dal, community_id=community_id)
    except ApiError as exc:
        return _err(exc)
    return MusicSettingsResponse(success=True, settings=settings)


@music_bp.route("/<int:community_id>/music/settings", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@validate_request(UpdateMusicSettingsRequest)
# NOT @validate_response -- update_music_settings() calls update_async then
# returns a nested-dataclass response, the exact crash class
# services/dto_response.py documents. jsonify_dto() is the workaround.
async def update_music_settings(data: UpdateMusicSettingsRequest, community_id: int) -> Any:
    """Update music settings for a community."""
    async_dal, dal = _dal()
    try:
        await authorize_community(request, async_dal, dal, community_id=community_id, admin=True)
        fields = {
            "defaultProvider": data.defaultProvider,
            "autoplayEnabled": data.autoplayEnabled,
            "volumeLimit": data.volumeLimit,
            "allowedGenres": data.allowedGenres,
            "blockedArtists": data.blockedArtists,
            "requireDjApproval": data.requireDjApproval,
            "isActive": data.isActive,
        }
        settings = await svc.update_music_settings(
            async_dal, dal, community_id=community_id, fields=fields
        )
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(MusicSettingsResponse(success=True, settings=settings))


@music_bp.route("/<int:community_id>/music/providers", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@validate_response(MusicProvidersResponse)
async def get_providers(
    community_id: int,
) -> MusicProvidersResponse | tuple[dict[str, object], int]:
    """List configured music providers for a community."""
    async_dal, dal = _dal()
    try:
        await authorize_community(request, async_dal, dal, community_id=community_id, admin=True)
        providers = await svc.get_providers(async_dal, dal, community_id=community_id)
    except ApiError as exc:
        return _err(exc)
    return MusicProvidersResponse(success=True, providers=providers)


@music_bp.route("/<int:community_id>/music/providers/<provider>/oauth", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@validate_request(StartOAuthRequest)
@validate_response(StartOAuthResponse)
async def start_oauth(
    data: StartOAuthRequest, community_id: int, provider: str
) -> StartOAuthResponse | tuple[dict[str, object], int]:
    """Start OAuth flow for a music provider."""
    async_dal, dal = _dal()
    try:
        await authorize_community(request, async_dal, dal, community_id=community_id, admin=True)
        auth_url, state_token = await svc.start_oauth(
            async_dal,
            dal,
            community_id=community_id,
            provider=provider,
            redirect_uri=data.redirectUri,
        )
    except ApiError as exc:
        return _err(exc)
    return StartOAuthResponse(success=True, authUrl=auth_url, stateToken=state_token)


@music_bp.route("/<int:community_id>/music/providers/<provider>", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@validate_response(MessageResponse)
async def disconnect_provider(
    community_id: int, provider: str
) -> MessageResponse | tuple[dict[str, object], int]:
    """Disconnect a music provider."""
    async_dal, dal = _dal()
    try:
        await authorize_community(request, async_dal, dal, community_id=community_id, admin=True)
        await svc.disconnect_provider(async_dal, dal, community_id=community_id, provider=provider)
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message=f"{provider} provider disconnected")


@music_bp.route("/<int:community_id>/music/radio-stations", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@validate_response(RadioStationsResponse)
async def get_radio_stations(
    community_id: int,
) -> RadioStationsResponse | tuple[dict[str, object], int]:
    """Get radio stations for a community."""
    async_dal, dal = _dal()
    try:
        await authorize_community(request, async_dal, dal, community_id=community_id, admin=True)
        page = int(request.args.get("page", "1"))
        limit = parse_limit(request.args.get("limit"), default=25)
        stations, page, limit, total = await svc.get_radio_stations(
            async_dal, dal, community_id=community_id, page=page, limit=limit
        )
    except ApiError as exc:
        return _err(exc)
    except ValueError:
        return _err(bad_request("page/limit must be integers"))
    pages = (total + limit - 1) // limit if limit else 0
    return RadioStationsResponse(
        success=True,
        pagination=PaginationDTO(page=page, limit=limit, total=total, pages=pages),
        stations=stations,
    )


@music_bp.route("/<int:community_id>/music/radio-stations", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@validate_request(AddRadioStationRequest)
# NOT @validate_response -- add_radio_station() calls insert_async then
# returns a nested-dataclass response (services/dto_response.py's crash).
async def add_radio_station(data: AddRadioStationRequest, community_id: int) -> Any:
    """Add a radio station to a community."""
    async_dal, dal = _dal()
    try:
        await authorize_community(request, async_dal, dal, community_id=community_id, admin=True)
        created_by = get_optional_current_user_id(request)
        station = await svc.add_radio_station(
            async_dal,
            dal,
            community_id=community_id,
            name=data.name,
            url=data.url,
            description=data.description,
            genre=data.genre,
            is_active=data.isActive if data.isActive is not None else True,
            created_by=created_by,
        )
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(RadioStationResponse(success=True, station=station), 201)


@music_bp.route("/<int:community_id>/music/radio-stations/<int:station_id>", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@validate_response(MessageResponse)
async def remove_radio_station(
    community_id: int, station_id: int
) -> MessageResponse | tuple[dict[str, object], int]:
    """Remove a radio station from a community."""
    async_dal, dal = _dal()
    try:
        await authorize_community(request, async_dal, dal, community_id=community_id, admin=True)
        await svc.remove_radio_station(
            async_dal, dal, community_id=community_id, station_id=station_id
        )
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message="Radio station removed")


BLUEPRINTS: list[Blueprint] = [music_bp]
