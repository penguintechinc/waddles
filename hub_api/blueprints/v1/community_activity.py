"""v1 `community.activity` group -- port of Node's `activityController.js` (M6 Community).

Three route surfaces, same split as Node:

- Member reads (`/api/v1/community/<id>/leaderboard/{watch-time,messages}`,
  `/activity/my-stats`) -- `tenant_middleware` + `require_scope`.
- Admin config (`/api/v1/admin/<id>/leaderboard-config`) -- same auth chain,
  `:admin` scope.
- Internal ingestion (`/api/v1/internal/activity/*`) -- `X-Service-Key`
  only, matching Node's `routes/internal.js` (no tenant/JWT: these are
  called by trigger/router modules, not end users).

Matches the discovery contract every v1 port group follows: a module-
level `BLUEPRINTS: list[Blueprint]`, found and mounted by `routers/v1.py`'s
auto-discovery -- no edit to `routers/v1.py` needed.
"""

from __future__ import annotations

from flask_core.api_utils import auth_required
from flask_core.authz import require_scope
from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_response

from services.community_activity import (
    LeaderboardConfigResponse,
    LeaderboardResponse,
    MyActivityStatsResponse,
    close_stale_watch_sessions,
    get_leaderboard_config,
    get_message_leaderboard,
    get_my_activity_stats,
    get_watch_time_leaderboard,
    record_activity_batch,
    record_message,
    record_watch_session,
    update_leaderboard_config,
)
from services.community_common import (
    api_error,
    community_in_tenant,
    get_current_user,
    is_valid_service_key,
)

activity_member_bp = Blueprint(
    "v1_community_activity_member", __name__, url_prefix="/api/v1/community"
)
activity_admin_bp = Blueprint("v1_community_activity_admin", __name__, url_prefix="/api/v1/admin")
activity_internal_bp = Blueprint(
    "v1_community_activity_internal", __name__, url_prefix="/api/v1/internal/activity"
)

#: Two-gate Feature flag -- `libs/community_module/features.py`'s
#: `community.activity` Feature contract, free tier.
FEATURE_COMMUNITY_ACTIVITY = "waddles.community.activity"


def _int_arg(name: str, default: int) -> int:
    try:
        return int(request.args.get(name, str(default)))
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Member reads
# ---------------------------------------------------------------------------


@activity_member_bp.route("/<int:community_id>/leaderboard/watch-time", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.activity:read")  # type: ignore[misc]
@validate_response(LeaderboardResponse)
async def watch_time_leaderboard(
    community_id: int,
) -> LeaderboardResponse | tuple[dict[str, object], int]:
    """`GET /api/v1/community/<id>/leaderboard/watch-time`."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    if not await feature_enabled(FEATURE_COMMUNITY_ACTIVITY, tenant=ctx.tenant_slug):
        return api_error("Community activity tracking is not enabled for this plan", 402)
    dal = current_app.config["dal"]
    if not community_in_tenant(dal, community_id, ctx):
        return api_error("Community not found", status_code=404)

    period = request.args.get("period", "alltime")
    limit = min(100, max(1, _int_arg("limit", 25)))
    offset = max(0, _int_arg("offset", 0))
    return get_watch_time_leaderboard(dal, community_id, period=period, limit=limit, offset=offset)


@activity_member_bp.route("/<int:community_id>/leaderboard/messages", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.activity:read")  # type: ignore[misc]
@validate_response(LeaderboardResponse)
async def message_leaderboard(
    community_id: int,
) -> LeaderboardResponse | tuple[dict[str, object], int]:
    """`GET /api/v1/community/<id>/leaderboard/messages`."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    dal = current_app.config["dal"]
    if not community_in_tenant(dal, community_id, ctx):
        return api_error("Community not found", status_code=404)

    period = request.args.get("period", "alltime")
    limit = min(100, max(1, _int_arg("limit", 25)))
    offset = max(0, _int_arg("offset", 0))
    return get_message_leaderboard(dal, community_id, period=period, limit=limit, offset=offset)


@activity_member_bp.route("/<int:community_id>/activity/my-stats", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.activity:read")  # type: ignore[misc]
@auth_required  # type: ignore[misc]  # populates get_current_user() for "my" stats
@validate_response(MyActivityStatsResponse)
async def my_activity_stats(
    community_id: int,
) -> MyActivityStatsResponse | tuple[dict[str, object], int]:
    """`GET /api/v1/community/<id>/activity/my-stats`."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    dal = current_app.config["dal"]
    if not community_in_tenant(dal, community_id, ctx):
        return api_error("Community not found", status_code=404)

    try:
        user_id = int(get_current_user(request)["user_id"])
    except (TypeError, ValueError, KeyError):
        return api_error("Caller has no resolvable user id", status_code=400)
    return get_my_activity_stats(dal, community_id, user_id)


# ---------------------------------------------------------------------------
# Admin config
# ---------------------------------------------------------------------------


@activity_admin_bp.route("/<int:community_id>/leaderboard-config", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.activity:admin")  # type: ignore[misc]
@validate_response(LeaderboardConfigResponse)
async def leaderboard_config_get(
    community_id: int,
) -> LeaderboardConfigResponse | tuple[dict[str, object], int]:
    """`GET /api/v1/admin/<id>/leaderboard-config`."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    dal = current_app.config["dal"]
    if not community_in_tenant(dal, community_id, ctx):
        return api_error("Community not found", status_code=404)
    return get_leaderboard_config(dal, community_id)


@activity_admin_bp.route("/<int:community_id>/leaderboard-config", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.activity:admin")  # type: ignore[misc]
async def leaderboard_config_update(community_id: int) -> tuple[dict[str, object], int]:
    """`PUT /api/v1/admin/<id>/leaderboard-config`."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    dal = current_app.config["dal"]
    if not community_in_tenant(dal, community_id, ctx):
        return api_error("Community not found", status_code=404)

    payload = await request.get_json(force=True, silent=True) or {}
    errors = update_leaderboard_config(dal, community_id, payload)
    if errors:
        return api_error(errors[0], status_code=400)
    return {"success": True, "message": "Leaderboard configuration updated"}, 200


# ---------------------------------------------------------------------------
# Internal ingestion -- X-Service-Key, no tenant/JWT (matches Node's internal.js)
# ---------------------------------------------------------------------------


@activity_internal_bp.route("/watch-session", methods=["POST"])
async def watch_session_ingest() -> tuple[dict[str, object], int]:
    """`POST /api/v1/internal/activity/watch-session` -- service-to-service only."""
    if not is_valid_service_key(request):
        return api_error("Invalid service key", status_code=401)
    dal = current_app.config["dal"]
    payload = await request.get_json(force=True, silent=True) or {}
    err = record_watch_session(dal, payload)
    if err:
        return api_error(err, status_code=400)
    return {"success": True}, 200


@activity_internal_bp.route("/message", methods=["POST"])
async def message_ingest() -> tuple[dict[str, object], int]:
    """`POST /api/v1/internal/activity/message` -- service-to-service only."""
    if not is_valid_service_key(request):
        return api_error("Invalid service key", status_code=401)
    dal = current_app.config["dal"]
    payload = await request.get_json(force=True, silent=True) or {}
    err = record_message(dal, payload)
    if err:
        return api_error(err, status_code=400)
    return {"success": True}, 200


@activity_internal_bp.route("/batch", methods=["POST"])
async def batch_ingest() -> tuple[dict[str, object], int]:
    """`POST /api/v1/internal/activity/batch` -- service-to-service only, max 100 events."""
    if not is_valid_service_key(request):
        return api_error("Invalid service key", status_code=401)
    dal = current_app.config["dal"]
    payload = await request.get_json(force=True, silent=True) or {}
    events = payload.get("events")
    if not isinstance(events, list) or not events:
        return api_error("events must be a non-empty array", status_code=400)
    if len(events) > 100:
        return api_error("Maximum 100 events per batch", status_code=400)
    processed, failed = record_activity_batch(dal, events)
    return {"success": True, "processed": processed, "failed": failed}, 200


@activity_internal_bp.route("/close-stale-sessions", methods=["POST"])
async def close_stale_sessions() -> tuple[dict[str, object], int]:
    """`POST /api/v1/internal/activity/close-stale-sessions` -- service-to-service only."""
    if not is_valid_service_key(request):
        return api_error("Invalid service key", status_code=401)
    dal = current_app.config["dal"]
    stale_minutes = _int_arg("staleMinutes", 30)
    closed = close_stale_watch_sessions(dal, stale_minutes)
    return {"success": True, "closedSessions": closed}, 200


BLUEPRINTS: list[Blueprint] = [activity_member_bp, activity_admin_bp, activity_internal_bp]
