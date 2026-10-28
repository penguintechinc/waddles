"""v1 `streaming` group -- ported from `streamingController.js` (M7 Streaming module).

Mounted at `/api/v1/admin/<community_id>/streams/*`, matching
`frontend/src/services/api.js`'s streaming-config calls byte-for-byte.
Every handler is a pure reverse-proxy to `video_proxy_module`
(`services.streaming_proxy_service.VideoProxyClient`) -- no owned data
model, so response bodies are relayed as plain dicts rather than typed
DTOs, matching `blueprints/v1/event.py`'s established precedent for
opaque-proxy bodies (security.md Output Validation guards against
over-serializing an OWNED model; a relayed downstream body was never this
service's row to begin with -- see that module's own docstring).

Auth: `requireCommunityAdmin` in Node, ported via `services.
community_authz.authorize_community(..., admin=True)` -- same rationale
as `blueprints/v1/music.py`'s module docstring.
"""

from __future__ import annotations

from typing import cast

from flask_core.api_utils import error_response
from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request

from services.community_authz import authorize_community
from services.errors import ApiError, bad_request, payment_required
from services.streaming_proxy_service import VideoProxyClient, validate_destination_input
from services.url_guard import validate_outbound_url

streaming_bp = Blueprint("v1_streaming", __name__, url_prefix="/api/v1/admin")

#: Two-gate Feature flag -- `libs/streaming_module/features.py`'s
#: `streaming.broadcast` Feature contract, Professional tier (forward/
#: record/transcode destination management).
FEATURE_STREAMING_BROADCAST = "waddles.streaming.broadcast"

_client = VideoProxyClient()


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    """Convert an `ApiError` into the flask_core `error_response()` JSON envelope."""
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


async def _authorize(community_id: int) -> None:
    async_dal, dal = current_app.config["async_dal"], current_app.config["dal"]
    await authorize_community(request, async_dal, dal, community_id=community_id, admin=True)


@streaming_bp.route("/<int:community_id>/streams", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
async def get_stream_config(community_id: int) -> tuple[dict[str, object], int]:
    """Get stream configuration for a community."""
    try:
        await _authorize(community_id)
        ctx = get_tenant_context(request)
        assert ctx is not None  # nosec B101 -- tenant_middleware guarantees this
        if not await feature_enabled(FEATURE_STREAMING_BROADCAST, tenant=ctx.tenant_slug):
            raise payment_required("Broadcast forwarding requires a Professional plan or higher")
        config = await _client.get_config(community_id)
    except ApiError as exc:
        return _err(exc)
    return {"success": True, "config": config}, 200


@streaming_bp.route("/<int:community_id>/streams", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
async def create_stream_config(community_id: int) -> tuple[dict[str, object], int]:
    """Create stream configuration for a community."""
    try:
        await _authorize(community_id)
        body = await request.get_json(force=True, silent=True) or {}
        rtmp_port = body.get("rtmpPort")
        http_port = body.get("httpPort")
        if rtmp_port is not None and not (1024 <= int(rtmp_port) <= 65535):
            raise bad_request("Invalid RTMP port")
        if http_port is not None and not (1024 <= int(http_port) <= 65535):
            raise bad_request("Invalid HTTP port")
        config = await _client.create_config(
            community_id,
            rtmp_port=rtmp_port,
            http_port=http_port,
            enabled=body.get("enabled") is not False,
        )
    except ApiError as exc:
        return _err(exc)
    return {"success": True, "config": config}, 201


@streaming_bp.route("/<int:community_id>/streams/key/regenerate", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
async def regenerate_stream_key(community_id: int) -> tuple[dict[str, object], int]:
    """Regenerate stream key for a community."""
    try:
        await _authorize(community_id)
        stream_key = await _client.regenerate_key(community_id)
    except ApiError as exc:
        return _err(exc)
    return {"success": True, "streamKey": stream_key}, 200


@streaming_bp.route("/<int:community_id>/streams/destinations", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
async def get_destinations(community_id: int) -> tuple[dict[str, object], int]:
    """Get streaming destinations for a community."""
    try:
        await _authorize(community_id)
        destinations = await _client.get_destinations(community_id)
    except ApiError as exc:
        return _err(exc)
    return {"success": True, "destinations": destinations}, 200


@streaming_bp.route("/<int:community_id>/streams/destinations", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
async def add_destination(community_id: int) -> tuple[dict[str, object], int]:
    """Add streaming destination for a community."""
    try:
        await _authorize(community_id)
        body = await request.get_json(force=True, silent=True) or {}
        rtmp_url = body.get("rtmpUrl") or ""
        platform = validate_destination_input(
            body.get("platform") or "", rtmp_url, body.get("streamKey") or ""
        )
        # SSRF guard -- must run BEFORE the proxy client call, in this same
        # route-handler frame (see streaming_proxy_service.py's module
        # docstring for why it can't live inside the mockable client
        # method itself).
        await validate_outbound_url(rtmp_url, allowed_schemes=("rtmp", "rtmps"))
        destination = await _client.add_destination(
            community_id,
            platform=platform,
            rtmp_url=rtmp_url,
            stream_key=body.get("streamKey") or "",
            enabled=body.get("enabled") is not False,
            force_cut=body.get("forceCut") is True,
        )
    except ApiError as exc:
        return _err(exc)
    return {"success": True, "destination": destination}, 201


@streaming_bp.route(
    "/<int:community_id>/streams/destinations/<int:destination_id>", methods=["DELETE"]
)
@tenant_middleware  # type: ignore[misc]
async def remove_destination(
    community_id: int, destination_id: int
) -> tuple[dict[str, object], int]:
    """Remove streaming destination."""
    try:
        await _authorize(community_id)
        await _client.remove_destination(community_id, destination_id)
    except ApiError as exc:
        return _err(exc)
    return {"success": True, "message": "Destination removed successfully"}, 200


@streaming_bp.route(
    "/<int:community_id>/streams/destinations/<int:destination_id>/force-cut", methods=["PUT"]
)
@tenant_middleware  # type: ignore[misc]
async def toggle_force_cut(community_id: int, destination_id: int) -> tuple[dict[str, object], int]:
    """Toggle force cut for a destination."""
    try:
        await _authorize(community_id)
        body = await request.get_json(force=True, silent=True) or {}
        force_cut = body.get("forceCut")
        if not isinstance(force_cut, bool):
            raise bad_request("forceCut must be a boolean")
        destination = await _client.toggle_force_cut(
            community_id, destination_id, force_cut=force_cut
        )
    except ApiError as exc:
        return _err(exc)
    return {"success": True, "destination": destination}, 200


@streaming_bp.route("/<int:community_id>/streams/status", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
async def get_stream_status(community_id: int) -> tuple[dict[str, object], int]:
    """Get streaming status for a community."""
    try:
        await _authorize(community_id)
        status = await _client.get_status(community_id)
    except ApiError as exc:
        return _err(exc)
    return {"success": True, "status": status}, 200


BLUEPRINTS: list[Blueprint] = [streaming_bp]
