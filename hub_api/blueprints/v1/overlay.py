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
from typing import Annotated, Any, cast

from flask_core.authz import require_scope
from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import get_tenant_context, tenant_middleware
from pydantic import Field
from quart import Blueprint, current_app, request
from quart_schema import validate_request

from config import HubAPIConfig
from services import community_access, overlay_scene_service, overlay_service
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

#: Flag for the composite-scene Overlay Designer (defaults OFF until validated).
FEATURE_OVERLAY_BUILDER = "waddles.streaming.overlay-builder"

_Px = Annotated[int, Field(ge=0, le=3840)]
_PxY = Annotated[int, Field(ge=0, le=2160)]
_SizeW = Annotated[int, Field(ge=32, le=3840)]
_SizeH = Annotated[int, Field(ge=32, le=2160)]
_Z = Annotated[int, Field(ge=-1000, le=1000)]
_WidgetId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,127}$")]


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


@dataclass(slots=True, frozen=True)
class AddComponentRequest:
    """Request DTO for placing a widget; omitted geometry falls back to the widget defaults."""

    widget_id: _WidgetId
    x: _Px | None = None
    y: _PxY | None = None
    width: _SizeW | None = None
    height: _SizeH | None = None
    z_index: _Z | None = None
    props: dict[str, Any] | None = None


@dataclass(slots=True, frozen=True)
class UpdateComponentRequest:
    """Request DTO for a partial component update."""

    x: _Px | None = None
    y: _PxY | None = None
    width: _SizeW | None = None
    height: _SizeH | None = None
    z_index: _Z | None = None
    visible: bool | None = None
    props: dict[str, Any] | None = None


@dataclass(slots=True, frozen=True)
class SetCanvasRequest:
    """Request DTO for the scene canvas resolution (`1080p` | `2k` | `4k`)."""

    resolution: Annotated[str, Field(pattern=r"^(1080p|2k|4k)$")]


@dataclass(slots=True, frozen=True)
class WidgetsResponse:
    """Response DTO for the widget palette."""

    success: bool
    widgets: list[overlay_scene_service.WidgetRecord]


@dataclass(slots=True, frozen=True)
class LayoutResponse:
    """Response DTO for the full scene layout."""

    success: bool
    layout: overlay_scene_service.SceneLayout


@dataclass(slots=True, frozen=True)
class ComponentResponse:
    """Response DTO for one placed component."""

    success: bool
    component: overlay_scene_service.ComponentRecord


@dataclass(slots=True, frozen=True)
class RemovedResponse:
    """Response DTO for deleting one component."""

    success: bool
    removed: bool


@dataclass(slots=True, frozen=True)
class ClearedResponse:
    """Response DTO for clearing the scene."""

    success: bool
    removed_count: int


@dataclass(slots=True, frozen=True)
class CanvasResponse:
    """Response DTO for the canvas resolution."""

    success: bool
    canvas: overlay_scene_service.CanvasRecord


async def _require_builder(community_id: int) -> None:
    """Admin check + `waddles.streaming.overlay-builder` flag gate (402 when off)."""
    await _require_admin_or_404(community_id)
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101 -- tenant_middleware guarantees this
    if not await feature_enabled(FEATURE_OVERLAY_BUILDER, tenant=ctx.tenant_slug):
        raise payment_required("The overlay builder is not enabled for this plan")


@overlay_bp.route("/<int:community_id>/overlay/widgets", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
async def get_available_widgets(community_id: int) -> Any:
    """List widgets the community may place (enabled bundles + registered widgets)."""
    await _require_builder(community_id)
    async_dal, dal = _dal()
    widgets = await overlay_scene_service.list_available_widgets(
        async_dal, dal, community_id=community_id
    )
    return jsonify_dto(WidgetsResponse(success=True, widgets=widgets))


@overlay_bp.route("/<int:community_id>/overlay/layout", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
async def get_scene_layout(community_id: int) -> Any:
    """Get the composite scene: canvas plus placed components."""
    await _require_builder(community_id)
    async_dal, dal = _dal()
    layout = await overlay_scene_service.get_scene_layout(async_dal, dal, community_id=community_id)
    return jsonify_dto(LayoutResponse(success=True, layout=layout))


@overlay_bp.route("/<int:community_id>/overlay/layout/components", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
@validate_request(AddComponentRequest)
async def add_scene_component(community_id: int, data: AddComponentRequest) -> Any:
    """Place a widget on the composite scene."""
    await _require_builder(community_id)
    async_dal, dal = _dal()
    component = await overlay_scene_service.add_component(
        async_dal,
        dal,
        community_id=community_id,
        data=overlay_scene_service.ComponentInput(
            widget_id=data.widget_id,
            x=data.x,
            y=data.y,
            width=data.width,
            height=data.height,
            z_index=data.z_index,
            props=data.props,
        ),
    )
    return jsonify_dto(ComponentResponse(success=True, component=component), 201)


@overlay_bp.route(
    "/<int:community_id>/overlay/layout/components/<uuid:instance_id>", methods=["PATCH"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
@validate_request(UpdateComponentRequest)
async def update_scene_component(
    community_id: int, instance_id: Any, data: UpdateComponentRequest
) -> Any:
    """Move, resize, show/hide or re-prop a placed component."""
    await _require_builder(community_id)
    async_dal, dal = _dal()
    component = await overlay_scene_service.update_component(
        async_dal,
        dal,
        community_id=community_id,
        instance_id=str(instance_id),
        data=overlay_scene_service.ComponentPatch(
            x=data.x,
            y=data.y,
            width=data.width,
            height=data.height,
            z_index=data.z_index,
            visible=data.visible,
            props=data.props,
        ),
    )
    return jsonify_dto(ComponentResponse(success=True, component=component))


@overlay_bp.route(
    "/<int:community_id>/overlay/layout/components/<uuid:instance_id>", methods=["DELETE"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
async def remove_scene_component(community_id: int, instance_id: Any) -> Any:
    """Remove a component from the scene (404 when it is not this community's)."""
    await _require_builder(community_id)
    async_dal, dal = _dal()
    removed = await overlay_scene_service.remove_component(
        async_dal, dal, community_id=community_id, instance_id=str(instance_id)
    )
    if not removed:
        raise not_found("Component not found")
    return jsonify_dto(RemovedResponse(success=True, removed=True))


@overlay_bp.route("/<int:community_id>/overlay/layout", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
async def clear_scene_layout(community_id: int) -> Any:
    """Remove every placed component from the scene."""
    await _require_builder(community_id)
    async_dal, dal = _dal()
    count = await overlay_scene_service.clear_layout(async_dal, dal, community_id=community_id)
    return jsonify_dto(ClearedResponse(success=True, removed_count=count))


@overlay_bp.route("/<int:community_id>/overlay/layout/canvas", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
@validate_request(SetCanvasRequest)
async def set_scene_canvas(community_id: int, data: SetCanvasRequest) -> Any:
    """Set the scene canvas resolution (1080p, 2k, 4k)."""
    await _require_builder(community_id)
    async_dal, dal = _dal()
    canvas = await overlay_scene_service.set_canvas(
        async_dal, dal, community_id=community_id, resolution=data.resolution
    )
    return jsonify_dto(CanvasResponse(success=True, canvas=canvas))


BLUEPRINTS: list[Blueprint] = [overlay_bp]
