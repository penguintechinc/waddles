"""v1 `public` group -- ported from `publicController.js` (migration plan M3).

PRE-AUTH surface (`routes/public.js`, mounted at `/api/v1/public*` +
one top-level alias `/api/v1/signup-settings` -- `routes/index.js`
re-dispatches that alias to the same handler; modeled here as a second,
un-prefixed blueprint rather than a runtime redispatch, see this file's
`BLUEPRINTS` list). NO `tenant_middleware`/`require_scope` on any route
-- there is no JWT (`hub_api/PORTING.md`'s Auth pattern table: "Pre-auth
... carry NO tenant_middleware/require_scope -- there is no JWT yet").

`getPublicProfile` (`/public/users/:userId/profile`) and
`getCommunityProfile` (`/public/communities/:id/profile`) are mounted in
Node's SAME `routes/public.js` file but belong to `profileController.js`/
`communityProfileController.js` respectively -- the migration plan's own
controller table lists them under Identity (M1, already ported -- see
`services/profile_service.py`'s own docstring excluding
`getPublicProfile` by name) and Tenancy (a different, not-yet-ported
group). Not ported here; scope is the three controllers this PR's task
names, not everything Node happens to mount under `/public`.

See `services/public_service.py`'s module docstring for the cross-tenant
fix applied to the `communities`-backed routes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from flask_core.api_utils import error_response
from quart import Blueprint, current_app, request
from quart_schema import validate_response

from config import HubAPIConfig
from services import landing_stats_cache
from services import public_service as svc
from services.default_tenant import DefaultTenantMissingError
from services.errors import ApiError
from services.pagination import parse_limit

logger = logging.getLogger("waddles.hub_api.public")

public_bp = Blueprint("v1_public", __name__, url_prefix="/api/v1/public")
#: `GET /api/v1/signup-settings` -- Node's `routes/index.js` top-level alias
#: for `publicController.getSignupSettings`, same handler, different path.
signup_settings_alias_bp = Blueprint("v1_signup_settings_alias", __name__, url_prefix="/api/v1")


def _dal() -> tuple[Any, Any]:
    """Return `(async_dal, dal)` from app config."""
    return current_app.config["async_dal"], current_app.config["dal"]


def _cfg() -> HubAPIConfig:
    """Return the app's `HubAPIConfig` (cast -- Quart's config storage is Any-typed)."""
    return cast(HubAPIConfig, current_app.config["HUB_API_CONFIG"])


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    """Convert an `ApiError` into the flask_core `error_response()` JSON envelope."""
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


async def _default_tenant_or_500() -> Any:
    """`public_service.resolve_default_tenant()`, converted to a 500 `ApiError` on failure.

    A missing/inactive default tenant is a deployment-configuration
    error (the seed migration always creates it) -- surfaced as a real
    500 via `ApiError`, never silently swallowed into an empty result
    that would look like "no communities exist".
    """
    async_dal, dal = _dal()
    try:
        return await svc.resolve_default_tenant(async_dal, dal, _cfg())
    except DefaultTenantMissingError as exc:
        # The ERROR log (slug + existing reserved tenants) was written by `require_default_tenant`;
        # the client gets a stable structured code, never the tenant inventory.
        raise ApiError(
            "The platform's default tenant is not configured", 500, "DEFAULT_TENANT_MISSING"
        ) from exc


# ---------------------------------------------------------------------------
# DTOs -- camelCase field names pinned to `frontend/src/services/api.js`'s
# `publicApi` (see hub_api/PORTING.md's DTO casing note).
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class DiscordStatsDTO:
    """Discord stats DTO."""

    servers: int
    channels: int


@dataclass(slots=True, frozen=True)
class TwitchStatsDTO:
    """Twitch stats DTO."""

    channels: int
    live: int
    viewers: int


@dataclass(slots=True, frozen=True)
class SlackStatsDTO:
    """Slack stats DTO."""

    workspaces: int
    channels: int


@dataclass(slots=True, frozen=True)
class PublicStatsDTO:
    """Public stats DTO."""

    communities: int
    discord: DiscordStatsDTO
    twitch: TwitchStatsDTO
    slack: SlackStatsDTO


@dataclass(slots=True, frozen=True)
class StatsResponse:
    """Response DTO for public stats."""

    success: bool
    stats: PublicStatsDTO
    computedAt: str


@dataclass(slots=True, frozen=True)
class StatsWarmingResponse:
    """503 body while no snapshot exists yet (CronJob has not populated the cache)."""

    status: str


@dataclass(slots=True, frozen=True)
class SpotlightedCommunityDTO:
    """Spotlighted community DTO."""

    id: int
    name: str | None
    displayName: str | None
    description: str | None
    logoUrl: str | None
    platform: str | None
    communityType: str
    memberCount: int


@dataclass(slots=True, frozen=True)
class SpotlightedCommunitiesResponse:
    """Response DTO for spotlighted communities."""

    success: bool
    communities: list[SpotlightedCommunityDTO]


@dataclass(slots=True, frozen=True)
class PaginationDTO:
    """Pagination DTO."""

    page: int
    limit: int
    total: int
    totalPages: int


@dataclass(slots=True, frozen=True)
class PublicCommunityDTO:
    """Public community DTO (list row)."""

    id: int
    name: str | None
    displayName: str | None
    description: str | None
    logoUrl: str | None
    platform: str | None
    memberCount: int
    createdAt: str | None


@dataclass(slots=True, frozen=True)
class ListCommunitiesResponse:
    """Response DTO for list communities."""

    success: bool
    communities: list[PublicCommunityDTO]
    pagination: PaginationDTO


@dataclass(slots=True, frozen=True)
class PublicCommunityDetailDTO:
    """Public community detail DTO."""

    id: int
    name: str | None
    displayName: str | None
    description: str | None
    logoUrl: str | None
    bannerUrl: str | None
    platform: str | None
    memberCount: int
    joinMode: str
    createdAt: str | None


@dataclass(slots=True, frozen=True)
class GetCommunityResponse:
    """Response DTO for get community."""

    success: bool
    community: PublicCommunityDetailDTO


@dataclass(slots=True, frozen=True)
class StreamDTO:
    """Stream DTO (list row)."""

    entityId: str
    channelName: str | None
    viewerCount: int
    liveSince: str | None
    title: str
    game: str
    thumbnailUrl: str


@dataclass(slots=True, frozen=True)
class LiveStreamsResponse:
    """Response DTO for live streams."""

    success: bool
    streams: list[StreamDTO]
    timestamp: str


@dataclass(slots=True, frozen=True)
class StreamDetailDTO:
    """Stream detail DTO."""

    entityId: str
    platform: str
    channelName: str | None
    isLive: bool
    viewerCount: int
    liveSince: str | None
    lastActivity: str | None
    title: str
    game: str
    thumbnailUrl: str


@dataclass(slots=True, frozen=True)
class StreamDetailsResponse:
    """Response DTO for stream details."""

    success: bool
    stream: StreamDetailDTO


@dataclass(slots=True, frozen=True)
class SignupSettingsResponse:
    """Response DTO for signup settings."""

    success: bool
    signupEnabled: bool
    hasAllowedDomains: bool
    allowedDomains: list[str] | None


@dataclass(slots=True, frozen=True)
class BannerResponse:
    """Response DTO for the banner -- Node's own shape has NO `success` envelope key."""

    enabled: bool
    text: str
    bgColor: str
    textColor: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@public_bp.route("/stats", methods=["GET"])
@validate_response(StatsResponse)
@validate_response(StatsWarmingResponse, 503)
async def get_stats() -> StatsResponse | tuple[StatsWarmingResponse, int]:
    """Serve the cached landing stats snapshot (refreshed by the 5-minute CronJob).

    Never queries the DB: a cache miss or Valkey error returns a typed 503
    `{"status": "warming"}` and logs a WARN -- no on-request recompute fallback.
    """
    try:
        snapshot = await landing_stats_cache.read_snapshot(
            landing_stats_cache.get_client(current_app.config)
        )
    except Exception:  # noqa: BLE001 - cache outage must surface as typed 503, not 500
        logger.warning("landing stats cache read failed; returning warming", exc_info=True)
        return StatsWarmingResponse(status="warming"), 503
    if snapshot is None:
        logger.warning("landing stats snapshot missing; CronJob has not populated it")
        return StatsWarmingResponse(status="warming"), 503
    stats = snapshot["stats"]
    return StatsResponse(
        success=True,
        stats=PublicStatsDTO(
            communities=stats["communities"],
            discord=DiscordStatsDTO(**stats["discord"]),
            twitch=TwitchStatsDTO(**stats["twitch"]),
            slack=SlackStatsDTO(**stats["slack"]),
        ),
        computedAt=snapshot["computed_at"],
    )


@public_bp.route("/communities/spotlighted", methods=["GET"])
@validate_response(SpotlightedCommunitiesResponse)
async def get_spotlighted_communities() -> (
    SpotlightedCommunitiesResponse | tuple[dict[str, object], int]
):
    """Get spotlighted communities."""
    async_dal, dal = _dal()
    try:
        ctx = await _default_tenant_or_500()
    except ApiError as exc:
        return _err(exc)
    rows = await svc.get_spotlighted_communities(async_dal, dal, ctx)
    return SpotlightedCommunitiesResponse(
        success=True,
        communities=[
            SpotlightedCommunityDTO(
                id=r.id,
                name=r.name,
                displayName=r.display_name or r.name,
                description=r.description,
                logoUrl=(r.config or {}).get("logo_url") if r.config else r.logo_url,
                platform=r.platform,
                communityType=r.community_type or "creator",
                memberCount=r.member_count or 0,
            )
            for r in rows
        ],
    )


@public_bp.route("/communities", methods=["GET"])
@validate_response(ListCommunitiesResponse)
async def list_communities() -> ListCommunitiesResponse | tuple[dict[str, object], int]:
    """List communities."""
    async_dal, dal = _dal()
    try:
        ctx = await _default_tenant_or_500()
    except ApiError as exc:
        return _err(exc)
    page = int(request.args.get("page", "1"))
    limit = parse_limit(request.args.get("limit"), default=12)
    rows, total, total_pages = await svc.list_communities(
        async_dal, dal, ctx, page=page, limit=limit
    )
    return ListCommunitiesResponse(
        success=True,
        communities=[
            PublicCommunityDTO(
                id=r.id,
                name=r.name,
                displayName=r.display_name or r.name,
                description=r.description,
                logoUrl=(r.config or {}).get("logo_url") if r.config else r.logo_url,
                platform=r.platform,
                memberCount=r.member_count or 0,
                createdAt=_iso(r.created_at),
            )
            for r in rows
        ],
        pagination=PaginationDTO(
            page=page, limit=min(50, max(1, limit)), total=total, totalPages=total_pages
        ),
    )


@public_bp.route("/communities/<int:community_id>", methods=["GET"])
@validate_response(GetCommunityResponse)
async def get_community(community_id: int) -> GetCommunityResponse | tuple[dict[str, object], int]:
    """Get community."""
    async_dal, dal = _dal()
    try:
        ctx = await _default_tenant_or_500()
        row = await svc.get_community(async_dal, dal, ctx, community_id=community_id)
    except ApiError as exc:
        return _err(exc)
    return GetCommunityResponse(
        success=True,
        community=PublicCommunityDetailDTO(
            id=row.id,
            name=row.name,
            displayName=row.display_name or row.name,
            description=row.description,
            logoUrl=(row.config or {}).get("logo_url") if row.config else row.logo_url,
            bannerUrl=(row.config or {}).get("banner_url") if row.config else row.banner_url,
            platform=row.platform,
            memberCount=row.member_count or 0,
            joinMode=row.join_mode or "open",
            createdAt=_iso(row.created_at),
        ),
    )


@public_bp.route("/live", methods=["GET"])
@validate_response(LiveStreamsResponse)
async def get_live_streams() -> LiveStreamsResponse:
    """Get live streams."""
    async_dal, dal = _dal()
    limit = parse_limit(request.args.get("limit"), default=20)
    rows = await svc.get_live_streams(async_dal, dal, limit=limit)
    return LiveStreamsResponse(
        success=True,
        streams=[
            StreamDTO(
                entityId=r.entity_id,
                channelName=r.channel_id or r.server_id,
                viewerCount=r.viewer_count or 0,
                liveSince=_iso(r.live_since),
                title=r.stream_title or "",
                game=r.game_name or "",
                thumbnailUrl=r.thumbnail_url or "",
            )
            for r in rows
        ],
        timestamp=datetime.now(UTC).isoformat(),
    )


@public_bp.route("/live/<entity_id>", methods=["GET"])
@validate_response(StreamDetailsResponse)
async def get_stream_details(
    entity_id: str,
) -> StreamDetailsResponse | tuple[dict[str, object], int]:
    """Get stream details."""
    async_dal, dal = _dal()
    try:
        row = await svc.get_stream_details(async_dal, dal, entity_id=entity_id)
    except ApiError as exc:
        return _err(exc)
    return StreamDetailsResponse(
        success=True,
        stream=StreamDetailDTO(
            entityId=row.entity_id,
            platform=row.platform,
            channelName=row.channel_id or row.server_id,
            isLive=bool(row.is_live),
            viewerCount=row.viewer_count or 0,
            liveSince=_iso(row.live_since),
            lastActivity=_iso(row.last_updated),
            title=row.stream_title or "",
            game=row.game_name or "",
            thumbnailUrl=row.thumbnail_url or "",
        ),
    )


async def _build_signup_settings_response() -> SignupSettingsResponse:
    """Shared body for `get_signup_settings()`/`get_signup_settings_alias()` below.

    Factored out rather than one route calling the other directly --
    calling a `@validate_response`-wrapped route function loses its
    precise return type from mypy's perspective (the decorator's own
    typing widens it to `Any`), which `--strict`'s `no-any-return`
    correctly flags.
    """
    async_dal, dal = _dal()
    signup_enabled, allowed_domains = await svc.get_signup_settings(async_dal, dal)
    return SignupSettingsResponse(
        success=True,
        signupEnabled=signup_enabled,
        hasAllowedDomains=len(allowed_domains) > 0,
        allowedDomains=allowed_domains or None,
    )


@public_bp.route("/signup-settings", methods=["GET"])
@validate_response(SignupSettingsResponse)
async def get_signup_settings() -> SignupSettingsResponse:
    """Get signup settings."""
    return await _build_signup_settings_response()


@signup_settings_alias_bp.route("/signup-settings", methods=["GET"])
@validate_response(SignupSettingsResponse)
async def get_signup_settings_alias() -> SignupSettingsResponse:
    """Get signup settings (top-level alias -- see this module's docstring)."""
    return await _build_signup_settings_response()


@public_bp.route("/banner", methods=["GET"])
@validate_response(BannerResponse)
async def get_banner() -> BannerResponse:
    """Get banner."""
    async_dal, dal = _dal()
    banner = await svc.get_banner(async_dal, dal)
    return BannerResponse(
        enabled=banner["enabled"],
        text=banner["text"],
        bgColor=banner["bg_color"],
        textColor=banner["text_color"],
    )


BLUEPRINTS: list[Blueprint] = [public_bp, signup_settings_alias_bp]
