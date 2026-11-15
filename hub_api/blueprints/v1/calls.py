"""v1 `calls` group -- RTC voice/video call-room management (Streaming module, M7).

Fronts svc-streaming's `RoomsService` over gRPC (HTTP/2 + mTLS, ES256 machine JWT per call;
see `services/calls_proxy.py`). Ported from `callsController.js` + `routes/calls.js`; the
Go RTC service it once proxied was folded into Rust `svc-streaming`
(`docs/plans/2026-08-31-svc-streaming-design.md` §6/§8.1). Each handler authorises the
caller, calls one RPC, and reshapes the result into hub-api's own `{success, ...}` envelope.

Room identity: the `<room_name>` path segment is the room's UUID `room_id` (returned by
create/list); the caller-supplied name on create is stored as the room's `label`.
Participants are UUIDs derived (uuid5) from tenant + community + user id -- no usernames or
raw user ids cross to svc-streaming. Tenant and community come from the verified JWT /
membership checks, never from a request body.

Raised-hand queues (`raised-hands`, `acknowledge-hand`) have no svc-streaming equivalent
(that state lives in the `rooms` app bundle's kv) and answer an explicit 501 -- never an
empty success.

Two route groups, TWO DIFFERENT authz levels ported from two different Node routers mounting
the SAME controller functions:

  `calls_admin_bp` (`/api/v1/admin/:communityId/calls/*`) -- `community_access.
  require_community_admin()`; the downstream actor role is ADMIN.

  `member_voice_bp` (`/api/v1/community/:id/interact/voice/*`) -- Node applied `requireAuth`
  ONLY. THIS IS THE SECURITY FIX, not a faithful reproduction: every member route also
  requires `community_access.require_community_member()` (active membership, tenant-scoped);
  otherwise any authenticated caller could mint a join token for any community's room. The
  downstream actor role is MEMBER.

Response bodies are not hub-api-owned rows: `@validate_response`/`jsonify_dto()` are NOT used
for the same reason `event.py` skips them (Output Validation guards an OWNED row); the client
(`RoomsClient`) maps protobuf messages to explicit field sets, so nothing extra can leak.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from flask_core.authz import require_scope
from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import TenantContext, get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_request

# isort: split
import grpc_internal  # noqa: F401  (puts the generated pb/ tree on sys.path)

# isort: split
from waddles.streaming.internal.v1 import rooms_pb2

from services import community_access
from services.calls_proxy import RoomsClient, RoomsRpcError
from services.current_user import get_current_user_id
from services.errors import ApiError, payment_required

calls_admin_bp = Blueprint("v1_calls_admin", __name__, url_prefix="/api/v1/admin")
member_voice_bp = Blueprint("v1_member_voice", __name__, url_prefix="/api/v1/community")

SCOPE_READ = "streaming.calls:read"
SCOPE_WRITE = "streaming.calls:write"
SCOPE_ADMIN = "streaming.calls:admin"

#: Two-gate Feature flag -- `libs/streaming_module/features.py`'s
#: `streaming.rtc` Feature contract, free tier.
FEATURE_STREAMING_RTC = "waddles.streaming.rtc"

#: Namespace for the participant/actor UUIDs sent downstream (uuid5 of tenant+community+user).
_PARTICIPANT_NS = uuid.UUID("6f3c1c1e-5b8e-4a67-9d3b-2f0c8f3f6a11")

HAND_RAISE_UNSUPPORTED = (
    "Raised-hand queues are not available: that state lives in the rooms app bundle, "
    "not the call-room service"
)


def _issuer() -> Any:
    """The app's machine-JWT issuer (`SERVICE_JWT_ISSUER` app config), or None when unconfigured."""
    return current_app.config.get("SERVICE_JWT_ISSUER")


_client = RoomsClient(
    issuer_provider=_issuer,
    deployment_env=lambda: str(current_app.config.get("DEPLOYMENT_ENV", "alpha")),
)


def _dal() -> tuple[Any, Any]:
    return current_app.config["async_dal"], current_app.config["dal"]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    from flask_core.api_utils import error_response

    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


def _rpc_error(exc: RoomsRpcError) -> tuple[dict[str, Any], int]:
    """`{success:false, error}` carrying the downstream status (404/400/403/429/501/502...)."""
    return {"success": False, "error": exc.message}, exc.status_code


async def _guarded(op: Callable[[], Awaitable[tuple[dict[str, Any], int]]]) -> Any:
    """Run one RPC-backed operation, mapping `RoomsRpcError` to the error envelope."""
    try:
        return await op()
    except RoomsRpcError as exc:
        return _rpc_error(exc)


def _room_id(room_name: str) -> str:
    """The `<room_name>` path segment must be the room's UUID (canonicalised)."""
    try:
        return str(uuid.UUID(room_name))
    except ValueError as exc:
        raise ApiError(
            message="Room id must be a UUID", status_code=400, code="INVALID_ROOM_ID"
        ) from exc


def _person_uuid(ctx: TenantContext, community_id: int, user_id: int) -> str:
    """Stable pseudonymous UUID for (tenant, community, user) -- never a name or raw id."""
    return str(uuid.uuid5(_PARTICIPANT_NS, f"{ctx.tenant_slug}:{community_id}:{user_id}"))


async def _require_admin(community_id: int) -> str:
    """Enforce `require_community_admin`; returns the admin's pseudonymous actor UUID."""
    async_dal, dal = _dal()
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101 - tenant_middleware already ran (outermost decorator)
    user_id = get_current_user_id(request)
    await community_access.require_community_admin(
        async_dal, dal, request, ctx, community_id=community_id, user_id=user_id
    )
    return _person_uuid(ctx, community_id, user_id)


async def _require_member(community_id: int) -> str:
    """Enforce `require_community_member`; returns the caller's pseudonymous participant UUID."""
    async_dal, dal = _dal()
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101 - tenant_middleware already ran (outermost decorator)
    user_id = get_current_user_id(request)
    await community_access.require_community_member(
        async_dal, dal, request, ctx, community_id=community_id, user_id=user_id
    )
    return _person_uuid(ctx, community_id, user_id)


# ---------------------------------------------------------------------------
# Shared operations -- one per `callsController.js` export, called from both
# `calls_admin_bp` (after `_require_admin`) and `member_voice_bp` (after
# `_require_member`) where Node reuses the same controller function.
# ---------------------------------------------------------------------------


async def _list_rooms(community_id: int) -> tuple[dict[str, Any], int]:
    rooms = await _client.list_rooms(community_id)
    return {"success": True, "rooms": rooms}, 200


async def _create_room(
    community_id: int, *, room_name: str, max_participants: int | None
) -> tuple[dict[str, Any], int]:
    room = await _client.create_room(community_id, label=room_name, max_publishers=max_participants)
    return {"success": True, **room}, 201


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
    return await _guarded(lambda: _list_rooms(community_id))


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
    return await _guarded(
        lambda: _create_room(
            community_id, room_name=data.room_name, max_participants=data.max_participants
        )
    )


@calls_admin_bp.route("/<int:community_id>/calls/rooms/<room_name>", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
async def get_call_room(community_id: int, room_name: str) -> Any:
    """Get a specific call room (admin)."""
    try:
        await _require_admin(community_id)
        room_id = _room_id(room_name)
    except ApiError as exc:
        return _err(exc)

    async def op() -> tuple[dict[str, Any], int]:
        return {"success": True, **await _client.get_room(community_id, room_id)}, 200

    return await _guarded(op)


@calls_admin_bp.route("/<int:community_id>/calls/rooms/<room_name>", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
async def delete_call_room(community_id: int, room_name: str) -> Any:
    """Delete a call room (admin)."""
    try:
        await _require_admin(community_id)
        room_id = _room_id(room_name)
    except ApiError as exc:
        return _err(exc)

    async def op() -> tuple[dict[str, Any], int]:
        await _client.delete_room(community_id, room_id)
        return {"success": True, "message": "Room deleted"}, 200

    return await _guarded(op)


async def _set_locked(community_id: int, room_name: str, locked: bool) -> Any:
    try:
        actor = await _require_admin(community_id)
        room_id = _room_id(room_name)
    except ApiError as exc:
        return _err(exc)

    async def op() -> tuple[dict[str, Any], int]:
        await _client.set_locked(
            community_id,
            room_id,
            locked,
            actor_id=actor,
            actor_role=rooms_pb2.ROOM_ROLE_ADMIN,
        )
        return {"success": True, "message": "Room locked" if locked else "Room unlocked"}, 200

    return await _guarded(op)


@calls_admin_bp.route("/<int:community_id>/calls/rooms/<room_name>/lock", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
async def lock_call_room(community_id: int, room_name: str) -> Any:
    """Lock a call room (admin)."""
    return await _set_locked(community_id, room_name, True)


@calls_admin_bp.route("/<int:community_id>/calls/rooms/<room_name>/unlock", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
async def unlock_call_room(community_id: int, room_name: str) -> Any:
    """Unlock a call room (admin)."""
    return await _set_locked(community_id, room_name, False)


@calls_admin_bp.route("/<int:community_id>/calls/rooms/<room_name>/participants", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
async def get_call_participants(community_id: int, room_name: str) -> Any:
    """Get participants in a room (admin)."""
    try:
        await _require_admin(community_id)
        room_id = _room_id(room_name)
    except ApiError as exc:
        return _err(exc)

    async def op() -> tuple[dict[str, Any], int]:
        body = await _client.list_participants(community_id, room_id)
        return {
            "success": True,
            "participants": body["participants"],
            "locked": body["locked"],
        }, 200

    return await _guarded(op)


@dataclass(slots=True, frozen=True)
class KickParticipantRequest:
    """Request DTO for `POST .../kick` -- `identity` is the participant's UUID."""

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
        actor = await _require_admin(community_id)
        room_id = _room_id(room_name)
        try:
            participant = str(uuid.UUID(data.identity))
        except ValueError as exc:
            raise ApiError(
                message="identity must be a participant UUID",
                status_code=400,
                code="INVALID_IDENTITY",
            ) from exc
    except ApiError as exc:
        return _err(exc)

    async def op() -> tuple[dict[str, Any], int]:
        await _client.kick_participant(
            community_id,
            room_id,
            participant,
            actor_id=actor,
            actor_role=rooms_pb2.ROOM_ROLE_ADMIN,
        )
        return {"success": True, "message": "Participant removed"}, 200

    return await _guarded(op)


@calls_admin_bp.route("/<int:community_id>/calls/rooms/<room_name>/mute-all", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
async def mute_all_call_participants(community_id: int, room_name: str) -> Any:
    """Mute all participants in a room (admin)."""
    try:
        actor = await _require_admin(community_id)
        room_id = _room_id(room_name)
    except ApiError as exc:
        return _err(exc)

    async def op() -> tuple[dict[str, Any], int]:
        await _client.mute_all(
            community_id, room_id, actor_id=actor, actor_role=rooms_pb2.ROOM_ROLE_ADMIN
        )
        return {"success": True, "message": "All participants muted"}, 200

    return await _guarded(op)


@calls_admin_bp.route("/<int:community_id>/calls/rooms/<room_name>/raised-hands", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
async def get_raised_hands(community_id: int, room_name: str) -> Any:
    """Raised-hands queue (admin) -- 501: not a svc-streaming concern, see module docstring."""
    try:
        await _require_admin(community_id)
    except ApiError as exc:
        return _err(exc)
    return {"success": False, "error": HAND_RAISE_UNSUPPORTED}, 501


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
    """Acknowledge a raised hand (admin) -- 501: not a svc-streaming concern."""
    try:
        await _require_admin(community_id)
    except ApiError as exc:
        return _err(exc)
    return {"success": False, "error": HAND_RAISE_UNSUPPORTED}, 501


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
    return await _guarded(lambda: _list_rooms(community_id))


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
    return await _guarded(
        lambda: _create_room(
            community_id, room_name=data.room_name, max_participants=data.max_participants
        )
    )


@member_voice_bp.route(
    "/<int:community_id>/interact/voice/rooms/<room_name>/join", methods=["POST"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_WRITE)  # type: ignore[misc]
async def join_voice_room(community_id: int, room_name: str) -> Any:
    """Join a voice room -- mints a room token for THIS caller, THIS room, only if a member."""
    try:
        # The participant UUID comes from the verified JWT (`_require_member`), never the body.
        participant = await _require_member(community_id)
        room_id = _room_id(room_name)
    except ApiError as exc:
        return _err(exc)

    async def op() -> tuple[dict[str, Any], int]:
        joined = await _client.join_room(community_id, room_id, participant)
        return {
            "success": True,
            "token": joined["token"],
            "url": joined["whip_url"].rsplit("/", 1)[0],
            "whip_url": joined["whip_url"],
            "whep_url": joined["whep_url"],
            "token_id": joined["token_id"],
            "participant_id": joined["participant_id"],
            "expires_at": joined["expires_at"],
        }, 200

    return await _guarded(op)


@member_voice_bp.route(
    "/<int:community_id>/interact/voice/rooms/<room_name>/leave", methods=["POST"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_WRITE)  # type: ignore[misc]
async def leave_voice_room(community_id: int, room_name: str) -> Any:
    """Leave a voice room (member) -- always acts as oneself (actor == participant)."""
    try:
        participant = await _require_member(community_id)
        room_id = _room_id(room_name)
    except ApiError as exc:
        return _err(exc)

    async def op() -> tuple[dict[str, Any], int]:
        await _client.leave_room(
            community_id,
            room_id,
            participant,
            actor_id=participant,
            actor_role=rooms_pb2.ROOM_ROLE_MEMBER,
        )
        return {"success": True, "message": "Left room"}, 200

    return await _guarded(op)


BLUEPRINTS: list[Blueprint] = [calls_admin_bp, member_voice_bp]
