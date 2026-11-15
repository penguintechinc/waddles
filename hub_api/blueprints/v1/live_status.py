"""v1 live ON/OFF status -- community-scoped current live state (gh #287 S10).

Two mount points, same "public vs internal" split `blueprints/v1/
community_loyalty.py`'s own module docstring establishes:

- Public (`GET /api/v1/public/communities/<id>/live`) -- unauthenticated,
  rate-limited by the app-wide `before_request` hook (`services.
  rate_limiting.install_rate_limiting()`, installed once in `app.py`
  over every registered route -- no route-local limiter reinvented here,
  same precedent `blueprints/v1/public_music_queue.py`'s own module
  docstring documents). Gated to active/non-deleted/PUBLIC communities,
  same `communities.is_active`/`deleted_at`/`is_public` check that
  module's own `get_public_queue` uses -- an unknown/private/inactive/
  deleted community all collapse to the same 404, never leaking which
  case applies. Backs the overlay/webui "LIVE" badge poll (S7).
- Internal (`GET /api/v1/internal/live?community_id=`) -- `X-Service-Key`
  only, no tenant/JWT, same `is_valid_service_key` pattern as
  `community_loyalty.py`'s own `loyalty_internal_bp`; no public/
  is_active/deleted_at gate (a trusted service caller, not a browser).

Both read `services.live_status_service.get_live_status` (`coordination`
JOIN `community_servers` -- the write side is `core/svc_process/
services/live_status.py`, upserted from Twitch EventSub `stream.online`/
`stream.offline`). `Cache-Control: no-store` on both routes -- this is
live state, never cacheable.

Response shape (both routes, `{status, data, meta}`-enveloped):
`data: {live, platform, channel, since, viewer_count,
streams: [{platform, channel, live, since}]}`.

Deliberately its OWN blueprint module (not added to `blueprints/v1/
public.py`'s existing `public_bp`, which already owns a DIFFERENT,
tenant-wide `/api/v1/public/live` listing backed by `services.
public_service.get_live_streams`) -- this task's own edit-scope boundary
is NEW files only; auto-discovery (`routers/_discovery.py::
discover_blueprints`) picks up this module's `BLUEPRINTS` list the same
way it does every other `blueprints/v1/*.py` module, so no router file
needs editing to wire it in.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any, cast

from flask_core.api_utils import error_response
from quart import Blueprint, Response, jsonify, request

from services import live_status_service as svc
from services.community_common import is_valid_service_key
from services.db_routing import get_read_dals, reads_from_replica
from services.errors import ApiError, bad_request, not_found
from services.schema import bind_streaming_tables

logger = logging.getLogger(__name__)

live_status_public_bp = Blueprint("v1_live_status_public", __name__, url_prefix="/api/v1/public")
live_status_internal_bp = Blueprint(
    "v1_live_status_internal", __name__, url_prefix="/api/v1/internal"
)


def _dal() -> tuple[Any, Any]:
    """Return `(async_dal, read_dal)` with this group's tables bound on the serving DAL.

    Both routes are pure GET reads polled by overlays; replica lag (sub-second typical) only
    delays a live/offline flip by that much, so they are replica-eligible.
    """
    return get_read_dals(binders=(bind_streaming_tables,))


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


@dataclass(slots=True, frozen=True)
class LiveStreamItemDTO:
    """One `streams[]` entry -- the exact shape S7's overlay surface polls."""

    platform: str
    channel: str | None
    live: bool
    since: str | None


@dataclass(slots=True, frozen=True)
class LiveStatusResponseDTO:
    """`data` payload shape -- see module docstring."""

    live: bool
    platform: str | None
    channel: str | None
    since: str | None
    viewer_count: int
    streams: list[LiveStreamItemDTO]


def _to_response_dto(status: svc.CommunityLiveStatusDTO) -> LiveStatusResponseDTO:
    return LiveStatusResponseDTO(
        live=status.live,
        platform=status.platform,
        channel=status.channel,
        since=status.since,
        viewer_count=status.viewer_count,
        streams=[
            LiveStreamItemDTO(platform=s.platform, channel=s.channel, live=s.live, since=s.since)
            for s in status.streams
        ],
    )


def _envelope(data: LiveStatusResponseDTO) -> Response:
    response: Response = jsonify(
        {"status": "success", "data": asdict(data), "meta": {"version": 1}}
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@live_status_public_bp.route("/communities/<int:community_id>/live", methods=["GET"])
@reads_from_replica("live_status.get_public_live_status")
async def get_public_live_status(community_id: int) -> Any:
    """Unauthenticated, rate-limited read of one community's current live status.

    Same public-community gate as `blueprints/v1/public_music_queue.py`'s
    own `get_public_queue`.
    """
    async_dal, dal = _dal()
    try:
        community_row = (
            dal(
                (dal.communities.id == community_id)
                & (dal.communities.is_active == True)  # noqa: E712 - pydal idiom
                & (dal.communities.deleted_at == None)  # noqa: E711 - pydal idiom
                & (dal.communities.is_public == True)  # noqa: E712 - pydal idiom
            )
            .select(dal.communities.id)
            .first()
        )
        if community_row is None:
            return _err(not_found("Community not found"))
        status = await svc.get_live_status(async_dal, dal, community_id=community_id)
    except ApiError as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001 - last-resort 500 must still be JSON, never an empty body
        logger.exception(
            "get_public_live_status.unhandled_error", extra={"community_id": community_id}
        )
        return error_response(f"Internal error: {exc}", 500, "INTERNAL_ERROR")

    return _envelope(_to_response_dto(status)), 200


@live_status_internal_bp.route("/live", methods=["GET"])
@reads_from_replica("live_status.get_internal_live_status")
async def get_internal_live_status() -> Any:
    """`GET /api/v1/internal/live?community_id=` -- `X-Service-Key` only, no public/active gate."""
    if not is_valid_service_key(request):
        return {"success": False, "error": "Invalid service key"}, 401

    raw_community_id = request.args.get("community_id")
    community_id: int | None
    try:
        community_id = int(raw_community_id) if raw_community_id is not None else None
    except ValueError:
        community_id = None
    if community_id is None:
        return _err(bad_request("community_id is required"))

    async_dal, dal = _dal()
    try:
        community_row = dal(dal.communities.id == community_id).select(dal.communities.id).first()
        if community_row is None:
            return _err(not_found("Community not found"))
        status = await svc.get_live_status(async_dal, dal, community_id=community_id)
    except ApiError as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001 - last-resort 500 must still be JSON, never an empty body
        logger.exception(
            "get_internal_live_status.unhandled_error", extra={"community_id": community_id}
        )
        return error_response(f"Internal error: {exc}", 500, "INTERNAL_ERROR")

    return _envelope(_to_response_dto(status)), 200


BLUEPRINTS: list[Blueprint] = [live_status_public_bp, live_status_internal_bp]
