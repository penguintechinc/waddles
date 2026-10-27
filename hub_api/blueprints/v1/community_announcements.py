"""v1 `community.announcements` group -- port of Node's `announcementController.js` (M6 Community).

CRUD + publish/pin/unpin/archive/broadcast, mounted under `/api/v1/admin`
matching Node's `routes/admin.js` mount point. See
`services/community_announcements.py`'s module docstring for the
simplified `broadcast_to_all_platforms` port.

Matches the discovery contract every v1 port group follows: a module-
level `BLUEPRINTS: list[Blueprint]`, found and mounted by `routers/v1.py`'s
auto-discovery -- no edit to `routers/v1.py` needed.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import asdict
from typing import Any

from flask_core.api_utils import auth_required
from flask_core.authz import require_scope
from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_response

from services.community_announcements import (
    Announcement,
    AnnouncementListResponse,
    AnnouncementResponse,
    archive_announcement,
    broadcast_to_all_platforms,
    create_announcement,
    delete_announcement,
    get_announcement,
    get_broadcast_status,
    list_announcements,
    pin_announcement,
    publish_announcement,
    unpin_announcement,
    update_announcement,
)
from services.community_common import api_error, community_in_tenant, get_current_user

announcements_bp = Blueprint("v1_community_announcements", __name__, url_prefix="/api/v1/admin")

#: Two-gate Feature flag -- `libs/community_module/features.py`'s
#: `community.announcements` Feature contract, free tier.
FEATURE_COMMUNITY_ANNOUNCEMENTS = "waddles.community.announcements"


def _tenant_ok(community_id: int) -> bool:
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    return community_in_tenant(current_app.config["dal"], community_id, ctx)


@announcements_bp.route("/<int:community_id>/announcements", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.announcements:read")  # type: ignore[misc]
@validate_response(AnnouncementListResponse)
async def list_route(community_id: int) -> AnnouncementListResponse | tuple[dict[str, object], int]:
    """`GET /api/v1/admin/<id>/announcements`."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    if not await feature_enabled(FEATURE_COMMUNITY_ANNOUNCEMENTS, tenant=ctx.tenant_slug):
        return api_error("Community announcements are not enabled for this plan", 402)
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    page = max(1, int(request.args.get("page", "1") or 1))
    limit = min(100, max(1, int(request.args.get("limit", "20") or 20)))
    dal = current_app.config["dal"]
    return list_announcements(
        dal,
        community_id,
        page=page,
        limit=limit,
        status=request.args.get("status"),
        pinned=request.args.get("pinned"),
    )


@announcements_bp.route("/<int:community_id>/announcements/<int:announcement_id>", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.announcements:read")  # type: ignore[misc]
@validate_response(AnnouncementResponse)
async def get_route(
    community_id: int, announcement_id: int
) -> AnnouncementResponse | tuple[dict[str, object], int]:
    """`GET /api/v1/admin/<id>/announcements/<announcementId>`."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    dto = get_announcement(current_app.config["dal"], community_id, announcement_id)
    if dto is None:
        return api_error("Announcement not found", status_code=404)
    return AnnouncementResponse(success=True, data=dto)


@announcements_bp.route("/<int:community_id>/announcements", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.announcements:write")  # type: ignore[misc]
@auth_required  # type: ignore[misc]
@validate_response(AnnouncementResponse, 201)
async def create_route(community_id: int) -> tuple[AnnouncementResponse | dict[str, object], int]:
    """`POST /api/v1/admin/<id>/announcements`."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    payload = await request.get_json(force=True, silent=True) or {}
    user = get_current_user(request)
    dto, err = create_announcement(
        current_app.config["dal"],
        community_id,
        payload,
        int(user["user_id"]),
        user.get("username", ""),
    )
    if err:
        return api_error(err, status_code=400)
    assert dto is not None  # nosec B101 -- create_announcement: dto is None only when err is set
    return AnnouncementResponse(success=True, data=dto), 201


@announcements_bp.route("/<int:community_id>/announcements/<int:announcement_id>", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.announcements:write")  # type: ignore[misc]
@auth_required  # type: ignore[misc]
@validate_response(AnnouncementResponse)
async def update_route(
    community_id: int, announcement_id: int
) -> AnnouncementResponse | tuple[dict[str, object], int]:
    """`PUT /api/v1/admin/<id>/announcements/<announcementId>`."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    payload = await request.get_json(force=True, silent=True) or {}
    user_id = int(get_current_user(request)["user_id"])
    dto, err = update_announcement(
        current_app.config["dal"], community_id, announcement_id, payload, user_id
    )
    if err == "__not_found__":
        return api_error("Announcement not found", status_code=404)
    if err:
        return api_error(err, status_code=400)
    assert dto is not None  # nosec B101 -- update_announcement: dto is None only when err is set
    return AnnouncementResponse(success=True, data=dto)


_StateTransition = Callable[[Any, int, int, int], Announcement | None]
_MutateResult = AnnouncementResponse | tuple[dict[str, object], int]


def _mutate(
    fn: _StateTransition,
) -> Callable[[int, int], Coroutine[Any, Any, _MutateResult]]:
    """Build a route handler around one of the announcement state-transition functions.

    All four (`delete`/`publish`/`pin`/`unpin`/`archive`) share the same
    shape -- tenant gate, resolve `current_user`, call, 404-or-200 -- so
    the routes below wire this once instead of repeating the boilerplate
    five times.
    """

    async def handler(community_id: int, announcement_id: int) -> _MutateResult:
        if not _tenant_ok(community_id):
            return api_error("Community not found", status_code=404)
        user_id = int(get_current_user(request)["user_id"])
        dto = fn(current_app.config["dal"], community_id, announcement_id, user_id)
        if dto is None:
            return api_error("Announcement not found", status_code=404)
        return AnnouncementResponse(success=True, data=dto)

    return handler


@announcements_bp.route(
    "/<int:community_id>/announcements/<int:announcement_id>", methods=["DELETE"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope("community.announcements:write")  # type: ignore[misc]
@auth_required  # type: ignore[misc]
@validate_response(AnnouncementResponse)
async def delete_route(
    community_id: int, announcement_id: int
) -> AnnouncementResponse | tuple[dict[str, object], int]:
    """`DELETE /api/v1/admin/<id>/announcements/<announcementId>` -- soft delete (archives)."""
    return await _mutate(delete_announcement)(community_id, announcement_id)


@announcements_bp.route(
    "/<int:community_id>/announcements/<int:announcement_id>/publish", methods=["POST"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope("community.announcements:write")  # type: ignore[misc]
@auth_required  # type: ignore[misc]
@validate_response(AnnouncementResponse)
async def publish_route(
    community_id: int, announcement_id: int
) -> AnnouncementResponse | tuple[dict[str, object], int]:
    """`POST /api/v1/admin/<id>/announcements/<announcementId>/publish`."""
    return await _mutate(publish_announcement)(community_id, announcement_id)


@announcements_bp.route(
    "/<int:community_id>/announcements/<int:announcement_id>/pin", methods=["PUT"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope("community.announcements:write")  # type: ignore[misc]
@auth_required  # type: ignore[misc]
@validate_response(AnnouncementResponse)
async def pin_route(
    community_id: int, announcement_id: int
) -> AnnouncementResponse | tuple[dict[str, object], int]:
    """`PUT /api/v1/admin/<id>/announcements/<announcementId>/pin` -- toggles pin state."""
    return await _mutate(pin_announcement)(community_id, announcement_id)


@announcements_bp.route(
    "/<int:community_id>/announcements/<int:announcement_id>/unpin", methods=["PUT"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope("community.announcements:write")  # type: ignore[misc]
@auth_required  # type: ignore[misc]
@validate_response(AnnouncementResponse)
async def unpin_route(
    community_id: int, announcement_id: int
) -> AnnouncementResponse | tuple[dict[str, object], int]:
    """`PUT /api/v1/admin/<id>/announcements/<announcementId>/unpin`."""
    return await _mutate(unpin_announcement)(community_id, announcement_id)


@announcements_bp.route(
    "/<int:community_id>/announcements/<int:announcement_id>/archive", methods=["POST"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope("community.announcements:write")  # type: ignore[misc]
@auth_required  # type: ignore[misc]
@validate_response(AnnouncementResponse)
async def archive_route(
    community_id: int, announcement_id: int
) -> AnnouncementResponse | tuple[dict[str, object], int]:
    """`POST /api/v1/admin/<id>/announcements/<announcementId>/archive`."""
    return await _mutate(archive_announcement)(community_id, announcement_id)


@announcements_bp.route(
    "/<int:community_id>/announcements/<int:announcement_id>/broadcast", methods=["POST"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope("community.announcements:write")  # type: ignore[misc]
@auth_required  # type: ignore[misc]
async def broadcast_route(community_id: int, announcement_id: int) -> tuple[dict[str, object], int]:
    """`POST /api/v1/admin/<id>/announcements/<announcementId>/broadcast`."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    payload = await request.get_json(force=True, silent=True) or {}
    platforms = payload.get("platforms")
    if not isinstance(platforms, list) or not platforms:
        return api_error("Platforms array is required and must not be empty", status_code=400)

    dal = current_app.config["dal"]
    dto = get_announcement(dal, community_id, announcement_id)
    if dto is None:
        return api_error("Announcement not found", status_code=404)
    if dto.status != "published":
        return api_error("Only published announcements can be broadcast", status_code=400)

    outcome = await broadcast_to_all_platforms(
        dal,
        community_id,
        announcement_id,
        {"id": announcement_id, "title": dto.title, "content": dto.content},
        platforms,
    )
    return {
        "success": True,
        "data": {
            "announcementId": announcement_id,
            "platforms": platforms,
            "results": [asdict(r) for r in outcome.results],
            "status": "initiated",
            "message": "Broadcast initiated. Use broadcast status endpoint to check progress.",
        },
    }, 200


@announcements_bp.route(
    "/<int:community_id>/announcements/<int:announcement_id>/broadcast-status", methods=["GET"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope("community.announcements:read")  # type: ignore[misc]
async def broadcast_status_route(
    community_id: int, announcement_id: int
) -> tuple[dict[str, object], int]:
    """`GET /api/v1/admin/<id>/announcements/<announcementId>/broadcast-status`."""
    if not _tenant_ok(community_id):
        return api_error("Community not found", status_code=404)
    dal = current_app.config["dal"]
    if get_announcement(dal, community_id, announcement_id) is None:
        return api_error("Announcement not found", status_code=404)
    records = get_broadcast_status(dal, announcement_id)
    return {"success": True, "data": [asdict(r) for r in records]}, 200


BLUEPRINTS: list[Blueprint] = [announcements_bp]
