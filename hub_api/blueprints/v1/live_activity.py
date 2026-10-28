"""v1 `community.live-activity` group -- live bot-interaction feed for the WebUI.

Two GET routes (`blueprints/v1/live_activity.py`, matching the discovery
contract every v1 port group follows: a module-level `BLUEPRINTS: list[
Blueprint]`, found and mounted by `routers/v1.py`'s auto-discovery --
`routers/v1.py`/`app.py` are never edited to add this group).

**Auth (both routes, including `/stream`):** `tenant_middleware` +
`require_scope("community.live_activity:read")` -- the SAME chain every
other community-read endpoint uses (e.g. `blueprints/v1/admin.py::
get_connected_platforms`). Native browser `EventSource` cannot attach an
`Authorization` header, but that is already solved at the app level, not
per-route: `app.py`'s global `bridge_session_cookie_to_bearer`
`before_request` hook (`services/session_cookie.py`) rewrites the browser
SPA's HttpOnly `wb_session` session cookie into a synthetic
`Authorization: Bearer <token>` header before ANY route runs, whenever
the request has no `Authorization` header of its own. A same-origin
`EventSource` connection (the WebUI's Express proxy, per this task's
confirmed contract) sends that cookie automatically, so `/stream` needs
no bespoke no-auth-required path -- it just requires the same
`tenant_middleware` + `require_scope` this file's list route requires,
and the cookie bridge makes that transparent to a browser client that
never set an `Authorization` header itself.
"""

from __future__ import annotations

from flask_core.authz import require_scope
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, Response, current_app, request
from quart_schema import validate_response

from services import live_activity as svc
from services.community_common import api_error, community_in_tenant
from services.pagination import parse_limit

live_activity_bp = Blueprint("v1_live_activity", __name__, url_prefix="/api/v1")

#: Matches this task's own spec default (`?limit=50`).
DEFAULT_LIST_LIMIT = 50


@live_activity_bp.route("/community/<int:community_id>/live-activity", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.live_activity:read")  # type: ignore[misc]
@validate_response(svc.LiveActivityListResponse)
async def list_live_activity(
    community_id: int,
) -> svc.LiveActivityListResponse | tuple[dict[str, object], int]:
    """`GET /api/v1/community/<id>/live-activity?limit=50` -- most recent events, newest first."""
    dal = current_app.config["dal"]
    ctx = get_tenant_context(request)
    if ctx is None:
        return api_error("Authentication required", status_code=401)
    if not community_in_tenant(dal, community_id, ctx):
        return api_error("Community not found", status_code=404)

    limit = parse_limit(request.args.get("limit"), default=DEFAULT_LIST_LIMIT)
    events = svc.list_recent_events(dal, community_id=community_id, limit=limit)
    return svc.LiveActivityListResponse(success=True, events=events)


@live_activity_bp.route("/community/<int:community_id>/live-activity/stream", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.live_activity:read")  # type: ignore[misc]
async def stream_live_activity(community_id: int) -> Response | tuple[dict[str, object], int]:
    """`GET /api/v1/community/<id>/live-activity/stream` -- SSE, see `services/live_activity.py`.

    Not `@validate_response`-decorated -- a streaming `Response` (built
    directly, not returned as a DTO for quart-schema to convert) is the
    established shape for an SSE route in this codebase, matching
    `core/svc_presentation/blueprints/overlay.py::live()`.
    """
    dal = current_app.config["dal"]
    ctx = get_tenant_context(request)
    if ctx is None:
        return api_error("Authentication required", status_code=401)
    if not community_in_tenant(dal, community_id, ctx):
        return api_error("Community not found", status_code=404)

    return Response(
        svc.event_stream(dal, community_id=community_id),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


BLUEPRINTS: list[Blueprint] = [live_activity_bp]
