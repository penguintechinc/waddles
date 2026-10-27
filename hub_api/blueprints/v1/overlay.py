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

from dataclasses import dataclass
from typing import Any, cast

from flask_core.authz import require_scope
from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_request

from config import HubAPIConfig
from services import community_access, overlay_service
from services.current_user import get_current_user_id
from services.dto_response import jsonify_dto
from services.errors import ApiError, bad_request, payment_required
from services.overlay_service import OverlayRecord, OverlayStats

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


BLUEPRINTS: list[Blueprint] = [overlay_bp]
