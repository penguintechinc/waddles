"""v1 `overlay` group -- ported from `overlayController.js` (Streaming module, M7).

Community browser-source overlay token management, mounted at
`/api/v1/admin/:communityId/overlay*` in Node (`admin.js`) -- rendered by
svc-presentation's `full_screen`/`media`/`crawler` overlay surfaces (see
`docs/plans/2026-08-31-svc-streaming-design.md` §3). All four routes
carried Node's `requireCommunityAdmin` middleware; ported here as
`services.community_access.require_community_admin()` (`hub_api/
PORTING.md`'s documented `tenant_middleware` + `require_scope` pattern
does NOT by itself verify the caller administers the specific
`community_id` in the path -- see that module's docstring for the full
rationale). This is this port's security fix, not a faithful bug
reproduction: an overlay URL/key is a per-community secret
(unguessable-but-still-authz'd, security.md), and `require_scope` alone
would let any caller holding a global `streaming.overlay:admin` grant
read/rotate/mutate ANY community's overlay token by supplying its id.

Response DTOs are deliberately mixed-case (`overlay_key` snake_case,
`overlayUrl` camelCase) -- `services/overlay_service.py::OverlayRecord`'s
own docstring explains why; `AdminStreamOverlays.jsx` is the pinned
contract source. `jsonify_dto()`, not `@validate_response`, on every
route: `get_overlay`/`update_overlay`/`rotate_overlay_key` all await a
real `insert_async`/`update_async` and then return a nested-dataclass
response (`{success, overlay: OverlayRecord}`) -- exactly `services/
dto_response.py`'s documented crash class (`hub_api/PORTING.md` Gotcha
#3). `get_overlay_stats` doesn't write, but uses the same helper for
consistency (every response in this group nests `OverlayRecord`/
`OverlayStats`, both regular slotted dataclasses `jsonify_dto()` handles
identically regardless of write history).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, cast

from flask_core.authz import require_scope
from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, jsonify, request
from quart_schema import validate_request

from config import HubAPIConfig
from services import community_access, overlay_service
from services.current_user import get_current_user_id
from services.dto_response import jsonify_dto
from services.errors import ApiError, bad_request, not_found, payment_required
from services.overlay_service import (
    BROWSER_SOURCE_SURFACES,
    BrowserSourceRecord,
    OverlayRecord,
    OverlayStats,
)

logger = logging.getLogger(__name__)

overlay_bp = Blueprint("v1_overlay", __name__, url_prefix="/api/v1/admin")

SCOPE_ADMIN = "streaming.overlay:admin"

#: Two-gate Feature flag -- `libs/streaming_module/features.py`'s
#: `streaming.overlays` Feature contract, free tier.
FEATURE_STREAMING_OVERLAYS = "waddles.streaming.overlays"


def _cfg() -> HubAPIConfig:
    return cast(HubAPIConfig, current_app.config["HUB_API_CONFIG"])


def _dal() -> tuple[Any, Any]:
    return current_app.config["async_dal"], current_app.config["dal"]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    from flask_core.api_utils import error_response

    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


async def _require_admin(community_id: int) -> int:
    """Resolve the caller + enforce `require_community_admin`; returns the caller's user id."""
    async_dal, dal = _dal()
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101 - tenant_middleware already ran (outermost decorator)
    user_id = get_current_user_id(request)
    await community_access.require_community_admin(
        async_dal, dal, request, ctx, community_id=community_id, user_id=user_id
    )
    return user_id


async def _require_admin_or_404(community_id: int) -> int:
    """`_require_admin`, but a refusal reads as 404 so a foreign community's id is not an oracle."""
    try:
        return await _require_admin(community_id)
    except ApiError as exc:
        if exc.status_code == 403:
            raise not_found("Community not found") from exc
        raise


@dataclass(slots=True, frozen=True)
class UpdateOverlayRequest:
    """Request DTO for `PUT /:communityId/overlay` -- camelCase, matches Node's `req.body`."""

    isActive: bool | None = None
    themeConfig: dict[str, Any] | None = None
    enabledSources: list[str] | None = None


@dataclass(slots=True, frozen=True)
class OverlayResponse:
    """Response DTO for `getOverlay`/`updateOverlay` -- `{success, overlay}`."""

    success: bool
    overlay: OverlayRecord


@dataclass(slots=True, frozen=True)
class RotateOverlayResponse:
    """Response DTO for `rotateKey` -- `{success, message, overlay}`."""

    success: bool
    message: str
    overlay: OverlayRecord


@dataclass(slots=True, frozen=True)
class OverlayStatsResponse:
    """Response DTO for `getOverlayStats` -- `{success, stats}`."""

    success: bool
    stats: OverlayStats


@dataclass(slots=True, frozen=True)
class RegenerateBrowserSourcesRequest:
    """Request DTO for `POST /:communityId/browser-sources/regenerate`."""

    sourceType: str | None = None


@dataclass(slots=True, frozen=True)
class BrowserSourcesResponse:
    """Response DTO for `GET /:communityId/browser-sources` -- `{success, sources}`."""

    success: bool
    sources: list[BrowserSourceRecord]


@dataclass(slots=True, frozen=True)
class RegenerateBrowserSourcesResponse:
    """Response DTO for regenerate -- `{success, message}`; no key is echoed back."""

    success: bool
    message: str


@overlay_bp.errorhandler(ApiError)
async def _handle_api_error(exc: ApiError) -> tuple[dict[str, object], int]:
    """Convert a service `ApiError` into the JSON error shape, logging type + message."""
    logger.warning(
        "overlay_api_error",
        extra={
            "error_type": type(exc).__name__,
            "status": exc.status_code,
            "error_message": exc.message,
        },
    )
    return _err(exc)


@overlay_bp.route("/<int:community_id>/browser-sources", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
async def get_browser_sources(community_id: int) -> Any:
    """List browser-source URLs (ticker/media/general) for a community admin."""
    await _require_admin_or_404(community_id)
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101 -- tenant_middleware guarantees this
    if not await feature_enabled(FEATURE_STREAMING_OVERLAYS, tenant=ctx.tenant_slug):
        raise payment_required("Browser-source overlays are not enabled for this plan")
    async_dal, dal = _dal()
    sources = await overlay_service.get_browser_sources(
        async_dal, dal, _cfg(), community_id=community_id
    )
    return jsonify_dto(BrowserSourcesResponse(success=True, sources=sources))


@overlay_bp.route("/<int:community_id>/browser-sources/regenerate", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
@validate_request(RegenerateBrowserSourcesRequest)
async def regenerate_browser_sources(
    community_id: int, data: RegenerateBrowserSourcesRequest
) -> Any:
    """Rotate the view key behind every browser-source URL (one credential per community)."""
    if data.sourceType is not None and data.sourceType not in BROWSER_SOURCE_SURFACES:
        return _err(bad_request("Unknown sourceType"))
    await _require_admin_or_404(community_id)
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101 -- tenant_middleware guarantees this
    if not await feature_enabled(FEATURE_STREAMING_OVERLAYS, tenant=ctx.tenant_slug):
        raise payment_required("Browser-source overlays are not enabled for this plan")
    async_dal, dal = _dal()
    await overlay_service.get_or_create_overlay(async_dal, dal, _cfg(), community_id=community_id)
    await overlay_service.rotate_overlay_key(async_dal, dal, _cfg(), community_id=community_id)
    logger.info("browser_sources_regenerated", extra={"community_id": community_id})
    return jsonify_dto(
        RegenerateBrowserSourcesResponse(success=True, message="Browser source keys regenerated")
    )


@overlay_bp.route("/<int:community_id>/overlay", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
async def get_overlay(community_id: int) -> Any:
    """Get or create the overlay token for `community_id`."""
    try:
        await _require_admin(community_id)
        ctx = get_tenant_context(request)
        assert ctx is not None  # nosec B101 -- tenant_middleware guarantees this
        if not await feature_enabled(FEATURE_STREAMING_OVERLAYS, tenant=ctx.tenant_slug):
            raise payment_required("Browser-source overlays are not enabled for this plan")
        async_dal, dal = _dal()
        record = await overlay_service.get_or_create_overlay(
            async_dal, dal, _cfg(), community_id=community_id
        )
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(OverlayResponse(success=True, overlay=record))


@overlay_bp.route("/<int:community_id>/overlay", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
@validate_request(UpdateOverlayRequest)
async def update_overlay(community_id: int, data: UpdateOverlayRequest) -> Any:
    """Update overlay settings (active flag, theme, enabled sources)."""
    if data.isActive is None and data.themeConfig is None and data.enabledSources is None:
        return _err(bad_request("No fields to update"))
    try:
        await _require_admin(community_id)
        async_dal, dal = _dal()
        record = await overlay_service.update_overlay(
            async_dal,
            dal,
            _cfg(),
            community_id=community_id,
            is_active=data.isActive,
            theme_config=data.themeConfig,
            enabled_sources=data.enabledSources,
        )
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(OverlayResponse(success=True, overlay=record))


@overlay_bp.route("/<int:community_id>/overlay/rotate", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
async def rotate_overlay_key(community_id: int) -> Any:
    """Rotate the overlay key; the previous key stays valid for svc-presentation's grace period."""
    try:
        await _require_admin(community_id)
        async_dal, dal = _dal()
        record = await overlay_service.rotate_overlay_key(
            async_dal, dal, _cfg(), community_id=community_id
        )
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(
        RotateOverlayResponse(
            success=True,
            message="Overlay key rotated. Previous key valid for 5 more minutes.",
            overlay=record,
        )
    )


@overlay_bp.route("/<int:community_id>/overlay/stats", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
async def get_overlay_stats(community_id: int) -> Any:
    """Get daily + total overlay access statistics."""
    days_raw = request.args.get("days", "7")
    try:
        days = int(days_raw)
    except ValueError:
        days = 7
    try:
        await _require_admin(community_id)
        async_dal, dal = _dal()
        stats = await overlay_service.get_overlay_stats(
            async_dal, dal, community_id=community_id, days=days
        )
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(OverlayStatsResponse(success=True, stats=stats))


@overlay_bp.route("/<int:community_id>/overlay/widgets", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
async def get_available_widgets(community_id: int) -> Any:
    """List overlay widgets available from bundles active for this community."""
    try:
        await _require_admin(community_id)
        async_dal, dal = _dal()
        widgets = await overlay_service.list_available_widgets(
            async_dal, dal, community_id=community_id
        )
    except ApiError as exc:
        return _err(exc)
    return jsonify({"success": True, "widgets": widgets})


@overlay_bp.route("/<int:community_id>/overlay/layout", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
async def get_scene_layout(community_id: int) -> Any:
    """Get the full-screen composite overlay layout and active components."""
    try:
        await _require_admin(community_id)
        async_dal, dal = _dal()
        layout = await overlay_service.get_scene_layout(async_dal, dal, community_id=community_id)
    except ApiError as exc:
        return _err(exc)
    return jsonify({"success": True, "layout": layout})


@overlay_bp.route("/<int:community_id>/overlay/layout/components", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
async def add_scene_component(community_id: int) -> Any:
    """Add a component to the community's composite overlay scene."""
    try:
        await _require_admin(community_id)
        ctx = get_tenant_context(request)
        tenant_id = ctx.tenant_id if ctx else 1
        data = await request.get_json() or {}
        async_dal, dal = _dal()
        comp = await overlay_service.add_scene_component(
            async_dal, dal, community_id=community_id, tenant_id=tenant_id, data=data
        )
    except ApiError as exc:
        return _err(exc)
    return jsonify({"success": True, "component": comp}), 201


@overlay_bp.route(
    "/<int:community_id>/overlay/layout/components/<string:instance_id>", methods=["PATCH"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
async def update_scene_component(community_id: int, instance_id: str) -> Any:
    """Update position, size, visibility, or props of a placed scene component."""
    try:
        await _require_admin(community_id)
        data = await request.get_json() or {}
        async_dal, dal = _dal()
        comp = await overlay_service.update_scene_component(
            async_dal, dal, community_id=community_id, instance_id=instance_id, data=data
        )
    except ApiError as exc:
        return _err(exc)
    return jsonify({"success": True, "component": comp})


@overlay_bp.route(
    "/<int:community_id>/overlay/layout/components/<string:instance_id>", methods=["DELETE"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
async def remove_scene_component(community_id: int, instance_id: str) -> Any:
    """Remove a component from the composite overlay scene."""
    try:
        await _require_admin(community_id)
        async_dal, dal = _dal()
        removed = await overlay_service.remove_scene_component(
            async_dal, dal, community_id=community_id, instance_id=instance_id
        )
    except ApiError as exc:
        return _err(exc)
    return jsonify({"success": True, "removed": removed})


@overlay_bp.route("/<int:community_id>/overlay/layout/clear", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
async def clear_scene_layout(community_id: int) -> Any:
    """Remove all placed components from the composite overlay scene."""
    try:
        await _require_admin(community_id)
        async_dal, dal = _dal()
        count = await overlay_service.clear_scene_layout(async_dal, dal, community_id=community_id)
    except ApiError as exc:
        return _err(exc)
    return jsonify({"success": True, "removed_count": count})


@overlay_bp.route("/<int:community_id>/overlay/layout/theme", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
async def set_scene_canvas_theme(community_id: int) -> Any:
    """Configure canvas theme and resolution (1080p, 2k, 4k)."""
    try:
        await _require_admin(community_id)
        data = await request.get_json() or {}
        async_dal, dal = _dal()
        theme = await overlay_service.set_scene_canvas_theme(
            async_dal, dal, community_id=community_id, data=data
        )
    except ApiError as exc:
        return _err(exc)
    return jsonify({"success": True, "canvas": theme})


BLUEPRINTS: list[Blueprint] = [overlay_bp]
