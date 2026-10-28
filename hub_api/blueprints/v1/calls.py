"""v1 `calls` group -- ported from `callsController.js` + `routes/calls.js` (Streaming module, M7).

RTC voice/video call-room management, fronting LiveKit via the downstream
RTC control plane (Node: Go `module_rtc`; folded into Rust `svc-streaming`
per `docs/plans/2026-08-31-svc-streaming-design.md` §6/§8.1 -- see
`services/calls_proxy.py`'s module docstring for the env-var/contract
caveat). Every handler is a thin proxy: forward to the downstream
service, reshape its JSON body into hub-api's own `{success, ...}`
envelope -- Node's controller functions do the same reshaping (NOT a
pure passthrough like `services/event_calendar_proxy.py`'s `calendar-
interaction` integration), so this blueprint keeps that reshaping
in-line per handler rather than a generic `ProxyRoute` table
(`blueprints/v1/event.py`'s pattern doesn't fit here for the same reason
it wouldn't fit `overlay.py`: each response shape differs).

Two route groups, TWO DIFFERENT authz levels ported from two different
Node routers mounting the SAME controller functions:

  `calls_admin_bp` (`/api/v1/admin/:communityId/calls/*`, `routes/
  calls.js`'s default export) -- Node's `requireCommunityAdmin` on every
  route. Ported as `community_access.require_community_admin()` (see
  that module's docstring) -- `hub_api/PORTING.md`'s tenant+scope pattern
  alone does not verify the caller administers `community_id`.

  `member_voice_bp` (`/api/v1/community/:id/interact/voice/*`, `routes/
  calls.js`'s `memberVoiceRouter`) -- Node applies `requireAuth` ONLY, no
  community-membership check of any kind. THIS IS THE SECURITY FIX, not a
  faithful reproduction (security.md Service-to-Service Auth /
  `community_access.py`'s own docstring "Worse gap" paragraph): as
  shipped, ANY authenticated caller could POST `.../voice/rooms/
  :roomName/join` for ANY `community_id` and receive a live LiveKit join
  token for a room they have no membership in -- token-minted-for-the-
  wrong-user takeover, exactly what security.md's Service-to-Service Auth
  section warns against. Every member route below additionally requires
  `community_access.require_community_member()` (active membership,
  tenant-scoped), which Node never checked.

Response bodies are opaque proxy relays (whatever the downstream RTC
service returns, reshaped into `{success, rooms/participants/...}`) --
`@validate_response`/`jsonify_dto()` are NOT used for the same reason
`event.py`/`event_calendar_proxy.py` skip them: security.md's Output
Validation over-serialization concern guards an OWNED row, not a body
that was never hub-api's row to begin with.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from flask_core.authz import require_scope
from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_request

from services import community_access
from services.calls_proxy import CallsProxyClient
from services.current_user import get_current_user_id
from services.errors import ApiError, payment_required
from services.event_calendar_proxy import ProxyResult

calls_admin_bp = Blueprint("v1_calls_admin", __name__, url_prefix="/api/v1/admin")
member_voice_bp = Blueprint("v1_member_voice", __name__, url_prefix="/api/v1/community")

SCOPE_READ = "streaming.calls:read"
SCOPE_WRITE = "streaming.calls:write"
SCOPE_ADMIN = "streaming.calls:admin"

#: Two-gate Feature flag -- `libs/streaming_module/features.py`'s
#: `streaming.rtc` Feature contract, free tier.
FEATURE_STREAMING_RTC = "waddles.streaming.rtc"

_proxy_client = CallsProxyClient()


def _dal() -> tuple[Any, Any]:
    return current_app.config["async_dal"], current_app.config["dal"]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    from flask_core.api_utils import error_response

    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


def _relay_error(result: ProxyResult, default_message: str) -> tuple[dict[str, Any], int]:
    """Port of Node's `catch` block: `{success:false, error: <downstream error or default>}`."""
    error_message = default_message
    if isinstance(result.body, dict) and isinstance(result.body.get("error"), str):
        error_message = result.body["error"]
    return {"success": False, "error": error_message}, result.status_code or 500


async def _require_admin(community_id: int) -> None:
    async_dal, dal = _dal()
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101 - tenant_middleware already ran (outermost decorator)
    user_id = get_current_user_id(request)
    await community_access.require_community_admin(
        async_dal, dal, request, ctx, community_id=community_id, user_id=user_id
    )


async def _require_member(community_id: int) -> int:
    """Enforce `require_community_member`; returns the caller's user id (join/leave need it)."""
    async_dal, dal = _dal()
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101 - tenant_middleware already ran (outermost decorator)
    user_id = get_current_user_id(request)
    await community_access.require_community_member(
        async_dal, dal, request, ctx, community_id=community_id, user_id=user_id
    )
    return user_id


def _auth_header() -> str | None:
    """Forward the caller's own bearer token downstream -- see `calls_proxy.py` docstring."""
    return request.headers.get("Authorization")


# ---------------------------------------------------------------------------
# Shared proxy operations -- one per `callsController.js` export, called from
# both `calls_admin_bp` (after `_require_admin`) and `member_voice_bp` (after
# `_require_member`) where Node reuses the same controller function.
# ---------------------------------------------------------------------------


async def _list_rooms(community_id: int) -> tuple[dict[str, Any], int]:
    result = await _proxy_client.request(
        "GET",
        "/api/v1/rooms",
        authorization=_auth_header(),
        params={"community_id": str(community_id)},
    )
    if not result.ok:
        if result.status_code == 404:
            return {"success": True, "rooms": []}, 200
        return _relay_error(result, "Failed to get call rooms")
    rooms = result.body.get("rooms") if isinstance(result.body, dict) else None
    return {"success": True, "rooms": rooms or []}, 200


async def _create_room(
    community_id: int, *, room_name: str, max_participants: int | None
) -> tuple[dict[str, Any], int]:
    result = await _proxy_client.request(
        "POST",
        "/api/v1/rooms",
        authorization=_auth_header(),
        json_body={
            "community_id": community_id,
            "room_name": room_name,
            "max_participants": max_participants or 100,
        },
    )
    if not result.ok:
        return _relay_error(result, "Failed to create call room")
    body = result.body if isinstance(result.body, dict) else {}
    return {"success": True, **body}, 201


@dataclass(slots=True, frozen=True)
class CreateCallRoomRequest:
    """Request DTO for `POST .../calls/rooms` -- snake_case, matches Node's `req.body`."""

    room_name: str
    max_participants: int | None = None


@calls_admin_bp.route("/<int:community_id>/calls/rooms", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
async def get_call_rooms(community_id: int) -> Any:
    """Get all call rooms for a community (admin)."""
    try:
        await _require_admin(community_id)
        ctx = get_tenant_context(request)
        assert ctx is not None  # nosec B101 -- tenant_middleware guarantees this
        if not await feature_enabled(FEATURE_STREAMING_RTC, tenant=ctx.tenant_slug):
            raise payment_required("Calls / RTC is not enabled for this plan")
    except ApiError as exc:
        return _err(exc)
    return await _list_rooms(community_id)


@calls_admin_bp.route("/<int:community_id>/calls/rooms", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
@validate_request(CreateCallRoomRequest)
async def create_call_room(community_id: int, data: CreateCallRoomRequest) -> Any:
    """Create a new call room (admin)."""
    try:
        await _require_admin(community_id)
    except ApiError as exc:
        return _err(exc)
    return await _create_room(
        community_id, room_name=data.room_name, max_participants=data.max_participants
    )


@calls_admin_bp.route("/<int:community_id>/calls/rooms/<room_name>", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
async def get_call_room(community_id: int, room_name: str) -> Any:
    """Get a specific call room (admin)."""
    try:
        await _require_admin(community_id)
    except ApiError as exc:
        return _err(exc)
    result = await _proxy_client.request(
        "GET",
        f"/api/v1/rooms/{room_name}",
        authorization=_auth_header(),
        params={"community_id": str(community_id)},
    )
    if not result.ok:
        return _relay_error(result, "Failed to get call room")
    body = result.body if isinstance(result.body, dict) else {}
    return {"success": True, **body}, 200


@calls_admin_bp.route("/<int:community_id>/calls/rooms/<room_name>", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
async def delete_call_room(community_id: int, room_name: str) -> Any:
    """Delete a call room (admin)."""
    try:
        await _require_admin(community_id)
    except ApiError as exc:
        return _err(exc)
    result = await _proxy_client.request(
        "DELETE",
        f"/api/v1/rooms/{room_name}",
        authorization=_auth_header(),
        params={"community_id": str(community_id)},
    )
    if not result.ok:
        return _relay_error(result, "Failed to delete call room")
    return {"success": True, "message": "Room deleted"}, 200


@calls_admin_bp.route("/<int:community_id>/calls/rooms/<room_name>/lock", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
async def lock_call_room(community_id: int, room_name: str) -> Any:
    """Lock a call room (admin)."""
    try:
        await _require_admin(community_id)
    except ApiError as exc:
        return _err(exc)
    result = await _proxy_client.request(
        "POST",
        f"/api/v1/rooms/{room_name}/lock",
        authorization=_auth_header(),
        json_body={"community_id": community_id},
    )
    if not result.ok:
        return _relay_error(result, "Failed to lock room")
    return {"success": True, "message": "Room locked"}, 200


@calls_admin_bp.route("/<int:community_id>/calls/rooms/<room_name>/unlock", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
async def unlock_call_room(community_id: int, room_name: str) -> Any:
    """Unlock a call room (admin)."""
    try:
        await _require_admin(community_id)
    except ApiError as exc:
        return _err(exc)
    result = await _proxy_client.request(
        "POST",
        f"/api/v1/rooms/{room_name}/unlock",
        authorization=_auth_header(),
        json_body={"community_id": community_id},
    )
    if not result.ok:
        return _relay_error(result, "Failed to unlock room")
    return {"success": True, "message": "Room unlocked"}, 200


@calls_admin_bp.route("/<int:community_id>/calls/rooms/<room_name>/participants", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
async def get_call_participants(community_id: int, room_name: str) -> Any:
    """Get participants in a room (admin)."""
    try:
        await _require_admin(community_id)
    except ApiError as exc:
        return _err(exc)
    result = await _proxy_client.request(
        "GET",
        f"/api/v1/rooms/{room_name}/participants",
        authorization=_auth_header(),
        params={"community_id": str(community_id)},
    )
    if not result.ok:
        if result.status_code == 404:
            return {"success": True, "participants": []}, 200
        return _relay_error(result, "Failed to get participants")
    participants = result.body.get("participants") if isinstance(result.body, dict) else None
    return {"success": True, "participants": participants or []}, 200


@dataclass(slots=True, frozen=True)
class KickParticipantRequest:
    """Request DTO for `POST .../kick`."""

    identity: str


@calls_admin_bp.route("/<int:community_id>/calls/rooms/<room_name>/kick", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
@validate_request(KickParticipantRequest)
async def kick_call_participant(
    community_id: int, room_name: str, data: KickParticipantRequest
) -> Any:
    """Kick a participant from a room (admin)."""
    try:
        await _require_admin(community_id)
    except ApiError as exc:
        return _err(exc)
    result = await _proxy_client.request(
        "POST",
        f"/api/v1/rooms/{room_name}/kick",
        authorization=_auth_header(),
        json_body={"community_id": community_id, "identity": data.identity},
    )
    if not result.ok:
        return _relay_error(result, "Failed to kick participant")
    return {"success": True, "message": "Participant removed"}, 200


@calls_admin_bp.route("/<int:community_id>/calls/rooms/<room_name>/mute-all", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
async def mute_all_call_participants(community_id: int, room_name: str) -> Any:
    """Mute all participants in a room (admin)."""
    try:
        await _require_admin(community_id)
    except ApiError as exc:
        return _err(exc)
    result = await _proxy_client.request(
        "POST",
        f"/api/v1/rooms/{room_name}/mute-all",
        authorization=_auth_header(),
        json_body={"community_id": community_id},
    )
    if not result.ok:
        return _relay_error(result, "Failed to mute all")
    return {"success": True, "message": "All participants muted"}, 200


@calls_admin_bp.route("/<int:community_id>/calls/rooms/<room_name>/raised-hands", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
async def get_raised_hands(community_id: int, room_name: str) -> Any:
    """Get raised hands queue (admin)."""
    try:
        await _require_admin(community_id)
    except ApiError as exc:
        return _err(exc)
    result = await _proxy_client.request(
        "GET",
        f"/api/v1/rooms/{room_name}/raised-hands",
        authorization=_auth_header(),
        params={"community_id": str(community_id)},
    )
    if not result.ok:
        if result.status_code == 404:
            return {"success": True, "raised_hands": []}, 200
        return _relay_error(result, "Failed to get raised hands")
    raised_hands = result.body.get("raised_hands") if isinstance(result.body, dict) else None
    return {"success": True, "raised_hands": raised_hands or []}, 200


@dataclass(slots=True, frozen=True)
class AcknowledgeHandRequest:
    """Request DTO for `POST .../acknowledge-hand`."""

    user_id: str


@calls_admin_bp.route(
    "/<int:community_id>/calls/rooms/<room_name>/acknowledge-hand", methods=["POST"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
@validate_request(AcknowledgeHandRequest)
async def acknowledge_hand(community_id: int, room_name: str, data: AcknowledgeHandRequest) -> Any:
    """Acknowledge a raised hand (admin)."""
    try:
        await _require_admin(community_id)
    except ApiError as exc:
        return _err(exc)
    result = await _proxy_client.request(
        "POST",
        f"/api/v1/rooms/{room_name}/acknowledge-hand",
        authorization=_auth_header(),
        json_body={"community_id": community_id, "user_id": data.user_id},
    )
    if not result.ok:
        return _relay_error(result, "Failed to acknowledge hand")
    return {"success": True, "message": "Hand acknowledged"}, 200


# ---------------------------------------------------------------------------
# Member-facing voice routes -- `community_access.require_community_member()`
# is THIS PORT'S FIX (Node applied `requireAuth` only). See module docstring.
# ---------------------------------------------------------------------------


@member_voice_bp.route("/<int:community_id>/interact/voice/rooms", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_READ)  # type: ignore[misc]
async def list_voice_rooms(community_id: int) -> Any:
    """List voice rooms for a community (member)."""
    try:
        await _require_member(community_id)
    except ApiError as exc:
        return _err(exc)
    return await _list_rooms(community_id)


@member_voice_bp.route("/<int:community_id>/interact/voice/rooms", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_WRITE)  # type: ignore[misc]
@validate_request(CreateCallRoomRequest)
async def create_ad_hoc_voice_room(community_id: int, data: CreateCallRoomRequest) -> Any:
    """Create an ad-hoc voice room (member)."""
    try:
        await _require_member(community_id)
    except ApiError as exc:
        return _err(exc)
    return await _create_room(
        community_id, room_name=data.room_name, max_participants=data.max_participants
    )


@member_voice_bp.route(
    "/<int:community_id>/interact/voice/rooms/<room_name>/join", methods=["POST"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_WRITE)  # type: ignore[misc]
async def join_voice_room(community_id: int, room_name: str) -> Any:
    """Join a voice room -- mints a LiveKit token for THIS caller, THIS room, only if a member."""
    try:
        user_id = await _require_member(community_id)
    except ApiError as exc:
        return _err(exc)
    result = await _proxy_client.request(
        "POST",
        f"/api/v1/rooms/{room_name}/join",
        authorization=_auth_header(),
        # user_id/username come from the verified JWT (`_require_member`),
        # never from the request body -- matches Node's own
        # `String(req.user?.id)` (never `req.body.user_id`), the one part
        # of Node's join flow that was already correct.
        json_body={"community_id": community_id, "user_id": str(user_id)},
    )
    if not result.ok:
        return _relay_error(result, "Failed to join room")
    body = result.body if isinstance(result.body, dict) else {}
    return {"success": True, "token": body.get("token"), "url": body.get("url")}, 200


@member_voice_bp.route(
    "/<int:community_id>/interact/voice/rooms/<room_name>/leave", methods=["POST"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_WRITE)  # type: ignore[misc]
async def leave_voice_room(community_id: int, room_name: str) -> Any:
    """Leave a voice room (member)."""
    try:
        user_id = await _require_member(community_id)
    except ApiError as exc:
        return _err(exc)
    result = await _proxy_client.request(
        "POST",
        f"/api/v1/rooms/{room_name}/leave",
        authorization=_auth_header(),
        json_body={"community_id": community_id, "user_id": str(user_id)},
    )
    if not result.ok:
        return _relay_error(result, "Failed to leave room")
    return {"success": True, "message": "Left room"}, 200


BLUEPRINTS: list[Blueprint] = [calls_admin_bp, member_voice_bp]
