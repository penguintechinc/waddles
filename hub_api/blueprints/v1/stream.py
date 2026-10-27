"""v1 `stream` group -- ported from `streamController.js` (M7 Streaming module).

Node mounts these 3 handlers under `routes/community.js`
(`requireMember`-gated), itself mounted at BOTH `/api/v1/community` and
`/api/v1/communities` (`routes/index.js`: `router.use('/community',
communityRoutes); router.use('/communities', communityRoutes);` -- a
plural alias, not a typo). `frontend/src/services/api.js`'s `streamApi`
uses the plural form exclusively; mounted at both here too, byte-faithful
to the Node route table, via two `Blueprint` objects sharing one
`_register()` helper (mirrors `blueprints/v1/event.py`'s two-blueprint
pattern for the same "one contract, two mount points" shape).

Auth pattern: `requireMember`, not `requireCommunityAdmin` -- any active
community member may read live-stream listings, ported via
`services.community_authz.authorize_community(..., admin=False)`. See
`blueprints/v1/music.py`'s module docstring for why this DB-backed check
replaces the usual `require_scope(...)` recipe step for this group.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_response

from services import stream_service as svc
from services.community_authz import authorize_community
from services.errors import ApiError, payment_required
from services.schema import bind_streaming_tables
from services.stream_service import LiveStreamDTO, StreamDetailsDTO

#: Two-gate Feature flag -- `libs/streaming_module/features.py`'s
#: `streaming.stream` Feature contract, free tier.
FEATURE_STREAMING_STREAM = "waddles.streaming.stream"


def _dal() -> tuple[Any, Any]:
    """Return `(async_dal, dal)` from app config, ensuring this group's tables are bound."""
    async_dal, dal = current_app.config["async_dal"], current_app.config["dal"]
    bind_streaming_tables(dal)
    return async_dal, dal


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    """Convert an `ApiError` into the flask_core `error_response()` JSON envelope."""
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


@dataclass(slots=True, frozen=True)
class LiveStreamsResponse:
    """Response DTO for `GET .../streams` and `.../streams/featured`."""

    success: bool
    streams: list[LiveStreamDTO]


@dataclass(slots=True, frozen=True)
class StreamDetailsResponse:
    """Response DTO for `GET .../streams/<entityId>`."""

    success: bool
    stream: StreamDetailsDTO


def _register(bp: Blueprint) -> None:
    """Wire the 3 `streamController.js` handlers onto `bp` -- shared by both mount points."""

    @bp.route("/<int:community_id>/streams", methods=["GET"])
    @tenant_middleware  # type: ignore[misc]
    @validate_response(LiveStreamsResponse)
    async def get_live_streams(
        community_id: int,
    ) -> LiveStreamsResponse | tuple[dict[str, object], int]:
        """Get live streams for a community."""
        async_dal, dal = _dal()
        try:
            await authorize_community(
                request, async_dal, dal, community_id=community_id, admin=False
            )
            ctx = get_tenant_context(request)
            assert ctx is not None  # nosec B101 -- tenant_middleware guarantees this
            if not await feature_enabled(FEATURE_STREAMING_STREAM, tenant=ctx.tenant_slug):
                raise payment_required("Live streams are not enabled for this plan")
            streams = await svc.get_live_streams(async_dal, dal, community_id=community_id)
        except ApiError as exc:
            return _err(exc)
        return LiveStreamsResponse(success=True, streams=streams)

    @bp.route("/<int:community_id>/streams/featured", methods=["GET"])
    @tenant_middleware  # type: ignore[misc]
    @validate_response(LiveStreamsResponse)
    async def get_featured_streams(
        community_id: int,
    ) -> LiveStreamsResponse | tuple[dict[str, object], int]:
        """Get featured/pinned streams for a community (top 5 by viewer count)."""
        async_dal, dal = _dal()
        try:
            await authorize_community(
                request, async_dal, dal, community_id=community_id, admin=False
            )
            streams = await svc.get_featured_streams(async_dal, dal, community_id=community_id)
        except ApiError as exc:
            return _err(exc)
        return LiveStreamsResponse(success=True, streams=streams)

    @bp.route("/<int:community_id>/streams/<entity_id>", methods=["GET"])
    @tenant_middleware  # type: ignore[misc]
    @validate_response(StreamDetailsResponse)
    async def get_stream_details(
        community_id: int, entity_id: str
    ) -> StreamDetailsResponse | tuple[dict[str, object], int]:
        """Get details for a specific stream."""
        async_dal, dal = _dal()
        try:
            await authorize_community(
                request, async_dal, dal, community_id=community_id, admin=False
            )
            stream = await svc.get_stream_details(
                async_dal, dal, community_id=community_id, entity_id=entity_id
            )
        except ApiError as exc:
            return _err(exc)
        return StreamDetailsResponse(success=True, stream=stream)


community_stream_bp = Blueprint("v1_community_stream", __name__, url_prefix="/api/v1/community")
communities_stream_bp = Blueprint(
    "v1_communities_stream", __name__, url_prefix="/api/v1/communities"
)

_register(community_stream_bp)
_register(communities_stream_bp)

BLUEPRINTS: list[Blueprint] = [community_stream_bp, communities_stream_bp]
