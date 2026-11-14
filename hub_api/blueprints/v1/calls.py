"""v1 `calls` group -- RTC voice/video call-room management (Streaming module, M7).

Fronts svc-streaming's `RoomsService` over gRPC (HTTP/2 + mTLS, ES256 machine JWT per call;
see `services/calls_proxy.py`). Ported from `callsController.js` + `routes/calls.js`; the
Go RTC service it once proxied was folded into Rust `svc-streaming`
(`docs/plans/2026-08-31-svc-streaming-design.md` §6/§8.1). Each handler authorises the
caller, calls one RPC, and reshapes the result into hub-api's own `{success, ...}` envelope.

Room identity: the `<room_name>` path segment is the room's UUID `room_id` (returned by
create/list; legacy names are not accepted); the caller-supplied name on create is stored as
the room's `label`, which list/get return for display.

Participants are UUID-only toward svc-streaming: the participant/actor id is the caller's
`hub_users.uuid` (`services/call_participants.py`), the same identity the `rooms` app bundle
resolves, so REST and chat moderation target the same person. hub-api -- the PII boundary --
resolves display names itself (members of the asked community only; anyone else renders as
"guest") and never sends a name downstream or into a log. Tenant and community come from the
verified JWT / membership checks, never from a request body; a room addressed through the
wrong community is a 404.

Every operation (all routes below) is behind the PostHog flag `waddles.streaming.rtc`
(default OFF; OFF answers HTTP 402 with the standard licensed-feature error body). The flag
is checked after the membership/admin check, so a non-member learns nothing about the plan.

Failures are never masked: an upstream error maps to an explicit status with the standard
error envelope (404 unknown room, 403 refused, 429 limit, 503 service unavailable with
`retryable`), logged at ERROR by the client and counted in a metric.

Two route groups, TWO DIFFERENT authz levels ported from two different Node routers mounting
the SAME controller functions:

  `calls_admin_bp` (`/api/v1/admin/:communityId/calls/*`) -- `community_access.
  require_community_admin()`; the downstream actor role is ADMIN. Moderation routes (lock,
  kick, mute, hands list/acknowledge) authorise by scope `streaming.calls:admin`, never role
  names.

  `member_voice_bp` (`/api/v1/community/:id/interact/voice/*`) -- Node applied `requireAuth`
  ONLY. THIS IS THE SECURITY FIX, not a faithful reproduction: every member route also
  requires `community_access.require_community_member()` (active membership, tenant-scoped);
  otherwise any authenticated caller could mint a join token for any community's room. The
  downstream actor role is MEMBER.

Success bodies are explicit dataclass DTOs (`@validate_response`); the client
(`RoomsClient`) maps protobuf messages to explicit field sets first, so nothing extra can leak.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from typing import Any, cast

from flask_core.authz import require_scope
from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import TenantContext, get_tenant_context, tenant_middleware
from quart import Blueprint, after_this_request, current_app, request
from quart_schema import validate_request, validate_response

# isort: split
import grpc_internal  # noqa: F401  (puts the generated pb/ tree on sys.path)

# isort: split
from waddles.streaming.internal.v1 import rooms_pb2

from services import (
    call_access,
    call_ice,
    call_participants,
    call_rooms,
    call_town_hall,
    community_access,
)
from services.calls_proxy import (
    HandInfo,
    RoomInfo,
    RoomsClient,
    RoomsRpcError,
    StreamingTargetNotConfiguredError,
)
from services.current_user import get_current_user_id
from services.errors import ApiError, payment_required

logger = logging.getLogger(__name__)

calls_admin_bp = Blueprint("v1_calls_admin", __name__, url_prefix="/api/v1/admin")
member_voice_bp = Blueprint("v1_member_voice", __name__, url_prefix="/api/v1/community")
member_calls_bp = Blueprint("v1_member_calls", __name__, url_prefix="/api/v1/communities")

SCOPE_READ = "streaming.calls:read"
SCOPE_WRITE = "streaming.calls:write"
SCOPE_ADMIN = "streaming.calls:admin"

#: Two-gate Feature flag -- `libs/streaming_module/features.py`'s
#: `streaming.rtc` Feature contract, free tier. Gates every route in this module.
FEATURE_STREAMING_RTC = "waddles.streaming.rtc"


def _issuer() -> Any:
    """The app's machine-JWT issuer (`SERVICE_JWT_ISSUER` app config), or None when unconfigured."""
    return current_app.config.get("SERVICE_JWT_ISSUER")


_client = RoomsClient(
    issuer_provider=_issuer,
    deployment_env=lambda: str(current_app.config.get("DEPLOYMENT_ENV", "alpha")),
)


@dataclass(slots=True, frozen=True)
class CreateCallRoomRequest:
    """Request DTO for `POST .../calls/rooms` -- snake_case, matches Node's `req.body`."""

    room_name: str
    max_participants: int | None = None


@dataclass(slots=True, frozen=True)
class ParticipantTargetRequest:
    """Request DTO for kick/mute/unmute -- `identity` is the target participant's UUID."""

    identity: str


@dataclass(slots=True, frozen=True)
class AcknowledgeHandRequest:
    """Request DTO for `POST .../acknowledge-hand` -- `user_id` is the participant's UUID."""

    user_id: str


@dataclass(slots=True, frozen=True)
class RoomsListResponse:
    """`GET .../rooms` -- every live room of the community."""

    rooms: list[RoomInfo]
    success: bool = True


@dataclass(slots=True, frozen=True)
class RoomResponse(RoomInfo):
    """One room (create/get): the `RoomInfo` fields plus `success`."""

    success: bool = True


@dataclass(slots=True, frozen=True)
class MessageResponse:
    """Plain acknowledgement."""

    message: str
    success: bool = True


@dataclass(slots=True, frozen=True)
class ParticipantDTO:
    """A room participant: UUID, hub-api-resolved display name, media flags."""

    participant_id: str
    display_name: str
    publisher: bool
    subscriber: bool
    muted: bool
    joined_at: int | None


@dataclass(slots=True, frozen=True)
class MemberParticipantDTO:
    """A participant as a non-staff member sees them: same fields as `ParticipantDTO` + `is_self`.

    Ids are per-community pseudonyms; there are no publisher lists, hand internals or global ids.
    """

    participant_id: str
    display_name: str
    publisher: bool
    subscriber: bool
    muted: bool
    joined_at: int | None
    is_self: bool


@dataclass(slots=True, frozen=True)
class MemberParticipantsResponse:
    """`GET /communities/{id}/calls/rooms/{room}/participants` (member)."""

    participants: list[MemberParticipantDTO]
    locked: bool
    success: bool = True


@dataclass(slots=True, frozen=True)
class ParticipantsResponse:
    """`GET .../participants`."""

    participants: list[ParticipantDTO]
    locked: bool
    success: bool = True


@dataclass(slots=True, frozen=True)
class ParticipantMuteResponse:
    """Result of a per-participant mute/unmute: the participant's resulting muted state."""

    participant_id: str
    muted: bool
    success: bool = True


@dataclass(slots=True, frozen=True)
class MuteAllResponse:
    """Result of `mute-all`: how many publishers were muted."""

    muted: int
    message: str = "All participants muted"
    success: bool = True


@dataclass(slots=True, frozen=True)
class HandDTO:
    """One raised hand with the hub-api-resolved display name (UUIDs elsewhere)."""

    participant_id: str
    display_name: str
    raised_at: int
    acknowledged_at: int | None
    acknowledged_by: str | None


@dataclass(slots=True, frozen=True)
class RaisedHandsResponse:
    """`GET .../raised-hands` -- the queue, oldest first."""

    raised_hands: list[HandDTO]
    count: int
    success: bool = True


@dataclass(slots=True, frozen=True)
class HandResponse:
    """Result of raising/acknowledging one hand."""

    hand: HandDTO
    success: bool = True


@dataclass(slots=True, frozen=True)
class ResetHandsResponse:
    """Result of clearing a room's raised-hand queue: how many hands were cleared."""

    cleared: int
    message: str = "Raised hands cleared"
    success: bool = True


@dataclass(slots=True, frozen=True)
class LowerHandResponse:
    """Result of lowering the caller's hand; `lowered` is False when none was raised."""

    lowered: bool
    success: bool = True


@dataclass(slots=True, frozen=True)
class IceServerDTO:
    """One STUN entry for the browser's `RTCPeerConnection` (never credentials; TURN is later)."""

    urls: list[str]


@dataclass(slots=True, frozen=True)
class JoinResponse:
    """`POST .../join` -- everything a browser WHIP/WHEP client needs, for ONE participant.

    `token` is a short-lived room token to send as `Authorization: Bearer <token>` to the WHIP
    publish and WHEP subscribe endpoints; `publish` / `subscribe` say which of them the policy
    granted. The token is a credential: it is never logged and the response is `no-store`.
    """

    token: str
    token_type: str
    url: str
    whip_url: str
    whep_url: str
    token_id: str
    participant_id: str
    expires_at: int
    publish: bool
    subscribe: bool
    ice_servers: list[IceServerDTO]
    success: bool = True


@dataclass(slots=True, frozen=True)
class MyHandResponse:
    """The caller's OWN hand state only: UUID plus timestamps, nothing about anyone else."""

    participant_id: str
    raised: bool
    acknowledged: bool
    raised_at: int | None
    acknowledged_at: int | None
    position: int | None
    queue_length: int
    success: bool = True


@dataclass(slots=True, frozen=True)
class TownHallJoin:
    """How the caller joins the Town Hall: call this hub-api route (it mints the token)."""

    method: str
    path: str


@dataclass(slots=True, frozen=True)
class TownHallResponse:
    """The community's persistent Town Hall room."""

    room_id: str
    label: str
    locked: bool
    participant_count: int
    created: bool
    join: TownHallJoin
    success: bool = True


def _dal() -> tuple[Any, Any]:
    """The app's `(async_dal, dal)` pair."""
    return current_app.config["async_dal"], current_app.config["dal"]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    """An `ApiError` as the standard `{success:false, error:{code,message}}` envelope."""
    from flask_core.api_utils import error_response

    return cast(
        tuple[dict[str, object], int],
        error_response(exc.message, exc.status_code, exc.code, exc.details),
    )


def _rpc_error(exc: RoomsRpcError) -> tuple[dict[str, object], int]:
    """A failed upstream call in the standard envelope; 5xx marks the failure retryable."""
    retryable = exc.status_code in (502, 503, 504)
    return _err(
        ApiError(
            message=exc.message,
            status_code=exc.status_code,
            code=exc.code,
            details={"retryable": True} if retryable else None,
        )
    )


async def _run(op: Callable[[], Awaitable[Any]]) -> Any:
    """Run one operation, mapping authz, upstream and configuration failures to explicit statuses.

    `ApiError` (authz/validation/flag) and `RoomsRpcError` keep their own status; a missing
    svc-streaming target/mTLS configuration is a deployment error answered with 503 and an
    ERROR log -- loud, never an empty success.
    """
    try:
        return await op()
    except ApiError as exc:
        logger.debug("calls request refused: %s %s", exc.status_code, exc.code)
        return _err(exc)
    except RoomsRpcError as exc:
        logger.debug("calls upstream failure relayed: %s %s", exc.status_code, exc.code)
        return _rpc_error(exc)
    except StreamingTargetNotConfiguredError as exc:
        logger.error("rooms gRPC client is not configured: %s", exc)
        return _rpc_error(
            RoomsRpcError(503, "Streaming service is not configured", "STREAMING_NOT_CONFIGURED")
        )


def _room_id(room_name: str) -> str:
    """The `<room_name>` path segment must be the room's UUID (canonicalised)."""
    try:
        return str(uuid.UUID(room_name))
    except ValueError as exc:
        raise ApiError(
            message="Room id must be a UUID", status_code=400, code="INVALID_ROOM_ID"
        ) from exc


def _participant_uuid(value: str, what: str) -> str:
    """A request-supplied participant id must be a UUID (canonicalised); names are refused."""
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ApiError(
            message=f"{what} must be a participant UUID",
            status_code=400,
            code="INVALID_IDENTITY",
        ) from exc


async def _require_rtc_enabled(ctx: TenantContext) -> None:
    """Raise 402 (licensed-feature body) unless `waddles.streaming.rtc` is ON for the tenant."""
    if not await feature_enabled(FEATURE_STREAMING_RTC, tenant=ctx.tenant_slug):
        raise payment_required("Calls / RTC is not enabled for this plan")


async def _require_admin(community_id: int) -> int:
    """Enforce the community-admin gate; returns the caller's `hub_users.id`."""
    async_dal, dal = _dal()
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101 - tenant_middleware already ran (outermost decorator)
    user_id = get_current_user_id(request)
    await community_access.require_community_admin(
        async_dal, dal, request, ctx, community_id=community_id, user_id=user_id
    )
    return user_id


async def _require_member(community_id: int) -> int:
    """Enforce the active-membership gate; returns the caller's `hub_users.id`."""
    async_dal, dal = _dal()
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101 - tenant_middleware already ran (outermost decorator)
    user_id = get_current_user_id(request)
    await community_access.require_community_member(
        async_dal, dal, request, ctx, community_id=community_id, user_id=user_id
    )
    return user_id


async def _gated_admin(community_id: int) -> int:
    """Admin gate, then the RTC flag (authz first: a non-admin never learns the plan)."""
    user_id = await _require_admin(community_id)
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101 - tenant_middleware already ran (outermost decorator)
    await _require_rtc_enabled(ctx)
    return user_id


async def _gated_member(community_id: int) -> int:
    """Membership gate, then the RTC flag."""
    user_id = await _require_member(community_id)
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101 - tenant_middleware already ran (outermost decorator)
    await _require_rtc_enabled(ctx)
    return user_id


def _tenant_slug() -> str:
    """The verified tenant of this request (set by `tenant_middleware`)."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101 - tenant_middleware already ran (outermost decorator)
    return ctx.tenant_slug


def _identity(community_id: int, user_id: int) -> str:
    """The caller's PER-COMMUNITY participant/actor pseudonym (never the global user uuid)."""
    return call_participants.identity_uuid(
        tenant_slug=_tenant_slug(), community_id=community_id, user_id=user_id
    )


async def _admin_actor(community_id: int) -> str:
    """Gated admin check; returns the admin's participant/actor pseudonym."""
    return _identity(community_id, await _gated_admin(community_id))


async def _member_actor(community_id: int) -> str:
    """Gated membership check; returns the caller's participant pseudonym."""
    return _identity(community_id, await _gated_member(community_id))


async def _names(community_id: int, participant_ids: list[str]) -> dict[str, str]:
    """Display names for `participant_ids` (ACTIVE members only; others are "guest")."""
    async_dal, dal = _dal()
    return await call_participants.resolve_display_names(
        async_dal,
        dal,
        tenant_slug=_tenant_slug(),
        community_id=community_id,
        participant_ids=participant_ids,
    )


def _hand_dto(hand: HandInfo, names: dict[str, str], *, staff: bool) -> HandDTO:
    """A `HandInfo` plus its resolved name; `acknowledged_by` is staff-only."""
    return HandDTO(
        participant_id=hand.participant_id,
        display_name=names.get(hand.participant_id, call_participants.GUEST_LABEL),
        raised_at=hand.raised_at,
        acknowledged_at=hand.acknowledged_at,
        acknowledged_by=hand.acknowledged_by if staff else None,
    )


async def _hand_response(community_id: int, hand: HandInfo, *, staff: bool) -> HandResponse:
    """Resolve the one participant's name and build the single-hand response."""
    names = await _names(community_id, [hand.participant_id])
    return HandResponse(hand=_hand_dto(hand, names, staff=staff))


def _member_room(room: RoomInfo) -> RoomInfo:
    """A room as a non-staff member may see it: no publisher ids (who is speaking)."""
    return replace(room, publisher_ids=[])


async def _list_rooms(community_id: int, *, staff: bool) -> RoomsListResponse:
    """List the community's rooms; `publisher_ids` are returned to staff only."""
    rooms = await _client.list_rooms(community_id)
    return RoomsListResponse(rooms=rooms if staff else [_member_room(r) for r in rooms])


async def _create_room(
    community_id: int, *, room_name: str, max_participants: int | None, staff: bool
) -> tuple[RoomResponse, int]:
    """Create a room labelled `room_name`; the label "Town Hall" is reserved."""
    if call_town_hall.is_reserved_label(room_name):
        raise ApiError(
            message='The room name "Town Hall" is reserved',
            status_code=400,
            code="RESERVED_ROOM_LABEL",
        )
    room = await _client.create_room(community_id, label=room_name, max_publishers=max_participants)
    async_dal, dal = _dal()
    try:
        await call_rooms.record_room(
            async_dal, dal, community_id=community_id, room_id=room.room_id
        )
    except Exception as exc:  # an unrecorded room would be unjoinable: fail loudly, undo our room
        dal.rollback()
        logger.error("could not record created room: %s", type(exc).__name__)
        await _client.delete_room(community_id, room.room_id)  # only the room this call created
        raise RoomsRpcError(503, "Room could not be recorded", "ROOM_RECORD_FAILED") from exc
    return RoomResponse(**asdict(room if staff else _member_room(room))), 201


# ---------------------------------------------------------------------------
# Admin routes -- `community_access.require_community_admin()`, actor role ADMIN.
# ---------------------------------------------------------------------------


@calls_admin_bp.route("/<int:community_id>/calls/rooms", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
@validate_response(RoomsListResponse)
async def get_call_rooms(community_id: int) -> Any:
    """Get all call rooms for a community (admin)."""

    async def op() -> Any:
        await _gated_admin(community_id)
        return await _list_rooms(community_id, staff=True)

    return await _run(op)


@calls_admin_bp.route("/<int:community_id>/calls/rooms", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
@validate_request(CreateCallRoomRequest)
@validate_response(RoomResponse, 201)
async def create_call_room(community_id: int, data: CreateCallRoomRequest) -> Any:
    """Create a new call room (admin)."""

    async def op() -> Any:
        await _gated_admin(community_id)
        return await _create_room(
            community_id,
            room_name=data.room_name,
            max_participants=data.max_participants,
            staff=True,
        )

    return await _run(op)


@calls_admin_bp.route("/<int:community_id>/calls/rooms/<room_name>", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
@validate_response(RoomResponse)
async def get_call_room(community_id: int, room_name: str) -> Any:
    """Get a specific call room (admin)."""

    async def op() -> Any:
        await _gated_admin(community_id)
        room = await _client.get_room(community_id, _room_id(room_name))
        return RoomResponse(**asdict(room))

    return await _run(op)


@calls_admin_bp.route("/<int:community_id>/calls/rooms/<room_name>", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
@validate_response(MessageResponse)
async def delete_call_room(community_id: int, room_name: str) -> Any:
    """Delete a call room (admin)."""

    async def op() -> Any:
        await _gated_admin(community_id)
        room_id = _room_id(room_name)
        await _client.delete_room(community_id, room_id)
        async_dal, dal = _dal()
        await call_rooms.forget_room(async_dal, dal, community_id=community_id, room_id=room_id)
        return MessageResponse(message="Room deleted")

    return await _run(op)


async def _set_locked(community_id: int, room_name: str, locked: bool) -> Any:
    """Lock or unlock a room as an admin (shared by the two lock routes)."""

    async def op() -> Any:
        actor = await _admin_actor(community_id)
        await _client.set_locked(
            community_id,
            _room_id(room_name),
            locked,
            actor_id=actor,
            actor_role=rooms_pb2.ROOM_ROLE_ADMIN,
        )
        return MessageResponse(message="Room locked" if locked else "Room unlocked")

    return await _run(op)


@calls_admin_bp.route("/<int:community_id>/calls/rooms/<room_name>/lock", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
@validate_response(MessageResponse)
async def lock_call_room(community_id: int, room_name: str) -> Any:
    """Lock a call room (admin)."""
    return await _set_locked(community_id, room_name, True)


@calls_admin_bp.route("/<int:community_id>/calls/rooms/<room_name>/unlock", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
@validate_response(MessageResponse)
async def unlock_call_room(community_id: int, room_name: str) -> Any:
    """Unlock a call room (admin)."""
    return await _set_locked(community_id, room_name, False)


@calls_admin_bp.route("/<int:community_id>/calls/rooms/<room_name>/participants", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
@validate_response(ParticipantsResponse)
async def get_call_participants(community_id: int, room_name: str) -> Any:
    """Get participants in a room (admin), with display names resolved by hub-api."""

    async def op() -> Any:
        await _gated_admin(community_id)
        listed = await _client.list_participants(community_id, _room_id(room_name))
        names = await _names(community_id, [p.participant_id for p in listed.participants])
        return ParticipantsResponse(
            participants=[
                ParticipantDTO(
                    participant_id=p.participant_id,
                    display_name=names.get(p.participant_id, call_participants.GUEST_LABEL),
                    publisher=p.publisher,
                    subscriber=p.subscriber,
                    muted=p.muted,
                    joined_at=p.joined_at,
                )
                for p in listed.participants
            ],
            locked=listed.locked,
        )

    return await _run(op)


@calls_admin_bp.route("/<int:community_id>/calls/rooms/<room_name>/kick", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
@validate_request(ParticipantTargetRequest)
@validate_response(MessageResponse)
async def kick_call_participant(
    community_id: int, room_name: str, data: ParticipantTargetRequest
) -> Any:
    """Kick a participant from a room (admin)."""

    async def op() -> Any:
        actor = await _admin_actor(community_id)
        await _client.kick_participant(
            community_id,
            _room_id(room_name),
            _participant_uuid(data.identity, "identity"),
            actor_id=actor,
            actor_role=rooms_pb2.ROOM_ROLE_ADMIN,
        )
        return MessageResponse(message="Participant removed")

    return await _run(op)


async def _set_participant_muted(
    community_id: int, room_name: str, data: ParticipantTargetRequest, muted: bool
) -> Any:
    """Mute or unmute one participant as an admin (shared by the two routes)."""

    async def op() -> Any:
        actor = await _admin_actor(community_id)
        target = _participant_uuid(data.identity, "identity")
        state = await _client.set_participant_muted(
            community_id,
            _room_id(room_name),
            target,
            muted,
            actor_id=actor,
            actor_role=rooms_pb2.ROOM_ROLE_ADMIN,
        )
        return ParticipantMuteResponse(participant_id=target, muted=state)

    return await _run(op)


@calls_admin_bp.route("/<int:community_id>/calls/rooms/<room_name>/mute", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
@validate_request(ParticipantTargetRequest)
@validate_response(ParticipantMuteResponse)
async def mute_call_participant(
    community_id: int, room_name: str, data: ParticipantTargetRequest
) -> Any:
    """Mute one participant (admin)."""
    return await _set_participant_muted(community_id, room_name, data, True)


@calls_admin_bp.route("/<int:community_id>/calls/rooms/<room_name>/unmute", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
@validate_request(ParticipantTargetRequest)
@validate_response(ParticipantMuteResponse)
async def unmute_call_participant(
    community_id: int, room_name: str, data: ParticipantTargetRequest
) -> Any:
    """Lift a moderation mute from one participant (admin)."""
    return await _set_participant_muted(community_id, room_name, data, False)


@calls_admin_bp.route("/<int:community_id>/calls/rooms/<room_name>/mute-all", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
@validate_response(MuteAllResponse)
async def mute_all_call_participants(community_id: int, room_name: str) -> Any:
    """Mute all publishers in a room except the acting admin (admin)."""

    async def op() -> Any:
        actor = await _admin_actor(community_id)
        muted = await _client.mute_all(
            community_id,
            _room_id(room_name),
            actor_id=actor,
            actor_role=rooms_pb2.ROOM_ROLE_ADMIN,
        )
        return MuteAllResponse(muted=muted)

    return await _run(op)


@calls_admin_bp.route("/<int:community_id>/calls/rooms/<room_name>/raised-hands", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
@validate_response(RaisedHandsResponse)
async def get_raised_hands(community_id: int, room_name: str) -> Any:
    """The room's raised-hand queue, oldest first, with display names (admin)."""

    async def op() -> Any:
        # The whole queue needs the moderate scope AND an actor recorded as room staff, which
        # svc-streaming only does for a participant who joined with the staff token (`join`).
        actor = await _admin_actor(community_id)
        listed = await _client.list_hands(
            community_id, _room_id(room_name), actor_id=actor, staff=True
        )
        names = await _names(community_id, [h.participant_id for h in listed.hands])
        return RaisedHandsResponse(
            raised_hands=[_hand_dto(h, names, staff=True) for h in listed.hands],
            count=len(listed.hands),
        )

    return await _run(op)


@calls_admin_bp.route(
    "/<int:community_id>/calls/rooms/<room_name>/acknowledge-hand", methods=["POST"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
@validate_request(AcknowledgeHandRequest)
@validate_response(HandResponse)
async def acknowledge_hand(community_id: int, room_name: str, data: AcknowledgeHandRequest) -> Any:
    """Acknowledge a participant's raised hand (admin)."""

    async def op() -> Any:
        actor = await _admin_actor(community_id)
        hand = await _client.acknowledge_hand(
            community_id,
            _room_id(room_name),
            _participant_uuid(data.user_id, "user_id"),
            actor_id=actor,
            actor_role=rooms_pb2.ROOM_ROLE_ADMIN,
        )
        return await _hand_response(community_id, hand, staff=True)

    return await _run(op)


@calls_admin_bp.route("/<int:community_id>/calls/rooms/<room_name>/reset-hands", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_ADMIN)  # type: ignore[misc]
@validate_response(ResetHandsResponse)
async def reset_hands(community_id: int, room_name: str) -> Any:
    """Clear the room's whole raised-hand queue (admin)."""

    async def op() -> Any:
        actor = await _admin_actor(community_id)
        cleared = await _client.reset_hands(
            community_id,
            _room_id(room_name),
            actor_id=actor,
            actor_role=rooms_pb2.ROOM_ROLE_ADMIN,
        )
        return ResetHandsResponse(cleared=cleared)

    return await _run(op)


# ---------------------------------------------------------------------------
# Member-facing voice routes -- `community_access.require_community_member()`
# is THIS PORT'S FIX (Node applied `requireAuth` only). See module docstring.
# ---------------------------------------------------------------------------


@member_voice_bp.route("/<int:community_id>/interact/voice/rooms", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_READ)  # type: ignore[misc]
@validate_response(RoomsListResponse)
async def list_voice_rooms(community_id: int) -> Any:
    """List voice rooms for a community (member)."""

    async def op() -> Any:
        await _gated_member(community_id)
        return await _list_rooms(community_id, staff=False)

    return await _run(op)


@member_voice_bp.route("/<int:community_id>/interact/voice/rooms", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_WRITE)  # type: ignore[misc]
@validate_request(CreateCallRoomRequest)
@validate_response(RoomResponse, 201)
async def create_ad_hoc_voice_room(community_id: int, data: CreateCallRoomRequest) -> Any:
    """Create an ad-hoc voice room (member)."""

    async def op() -> Any:
        await _gated_member(community_id)
        return await _create_room(
            community_id,
            room_name=data.room_name,
            max_participants=data.max_participants,
            staff=False,
        )

    return await _run(op)


async def _join(community_id: int, room_id: str) -> JoinResponse:
    """Authorise, derive the grants from the shared policy, mint the token for ONE participant."""
    user_id = await _gated_member(community_id)
    participant = _identity(community_id, user_id)
    async_dal, dal = _dal()
    # Fail fast (and 404 across communities) before the policy: the room must exist here.
    room = await _client.get_room(community_id, room_id)
    staff = await call_participants.is_community_staff(
        async_dal, dal, community_id=community_id, user_id=user_id
    )
    recorded = await call_rooms.get_facts(
        async_dal, dal, community_id=community_id, room_id=room_id
    )
    facts = call_access.facts_from_room(room, recorded)
    if facts is None:
        # hub-api never recorded this room, so its kind/closed/guest facts are unknown. Fail
        # closed: only staff may join, under the plain community policy (still lock-aware).
        if not staff:
            raise ApiError(
                message="room access cannot be verified",
                status_code=403,
                code="ROOM_ACCESS_DENIED",
            )
        facts = (call_access.GrantKind.COMMUNITY, call_access.GrantCtx(locked=room.locked))
    kind, ctx = facts
    try:
        grants = call_access.grants_for(
            kind, call_access.GrantRole.ADMIN if staff else call_access.GrantRole.MEMBER, ctx
        )
    except call_access.AccessDeniedError as exc:
        raise ApiError(message=exc.reason, status_code=403, code="ROOM_ACCESS_DENIED") from exc
    ice = call_ice.ice_servers_from_env()
    joined = await _client.join_room(
        community_id,
        room_id,
        participant,
        publish=grants.publish,
        subscribe=grants.subscribe,
        ttl_secs=call_access.JOIN_TTL_SECS,
        staff=staff,
    )

    @after_this_request  # type: ignore[untyped-decorator,unused-ignore]
    def _no_store(response: Any) -> Any:
        """The body carries a credential: never cache it."""
        response.headers["Cache-Control"] = "no-store"
        return response

    return JoinResponse(
        token=joined.token,
        token_type="Bearer",  # noqa: S106 - the HTTP auth scheme name, not a secret
        url=joined.whip_url.rsplit("/", 1)[0],
        whip_url=joined.whip_url,
        whep_url=joined.whep_url,
        token_id=joined.token_id,
        participant_id=joined.participant_id,
        expires_at=joined.expires_at,
        publish=grants.publish,
        subscribe=grants.subscribe,
        ice_servers=[IceServerDTO(urls=i.urls) for i in ice],
    )


@member_voice_bp.route(
    "/<int:community_id>/interact/voice/rooms/<room_name>/join", methods=["POST"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_WRITE)  # type: ignore[misc]
@validate_response(JoinResponse)
async def join_voice_room(community_id: int, room_name: str) -> Any:
    """Join a voice room -- mints a room token for THIS caller, THIS room, only if a member."""

    async def op() -> Any:
        # The participant UUID comes from the verified JWT (`_member_actor`), never the body.
        return await _join(community_id, _room_id(room_name))

    return await _run(op)


@member_voice_bp.route(
    "/<int:community_id>/interact/voice/rooms/<room_name>/leave", methods=["POST"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_WRITE)  # type: ignore[misc]
@validate_response(MessageResponse)
async def leave_voice_room(community_id: int, room_name: str) -> Any:
    """Leave a voice room (member) -- always acts as oneself (actor == participant)."""

    async def op() -> Any:
        participant = await _member_actor(community_id)
        await _client.leave_room(
            community_id,
            _room_id(room_name),
            participant,
            actor_id=participant,
            actor_role=rooms_pb2.ROOM_ROLE_MEMBER,
        )
        return MessageResponse(message="Left room")

    return await _run(op)


@member_voice_bp.route(
    "/<int:community_id>/interact/voice/rooms/<room_name>/raise-hand", methods=["POST"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_WRITE)  # type: ignore[misc]
@validate_response(HandResponse)
async def raise_voice_hand(community_id: int, room_name: str) -> Any:
    """Raise the caller's own hand (member); idempotent -- the original queue spot is kept."""

    async def op() -> Any:
        participant = await _member_actor(community_id)
        hand = await _client.raise_hand(community_id, _room_id(room_name), participant)
        return await _hand_response(community_id, hand, staff=False)

    return await _run(op)


@member_voice_bp.route(
    "/<int:community_id>/interact/voice/rooms/<room_name>/lower-hand", methods=["POST"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_WRITE)  # type: ignore[misc]
@validate_response(LowerHandResponse)
async def lower_voice_hand(community_id: int, room_name: str) -> Any:
    """Lower the caller's own hand (member); a no-op (`lowered: false`) when none is raised."""

    async def op() -> Any:
        participant = await _member_actor(community_id)
        lowered = await _client.lower_hand(
            community_id,
            _room_id(room_name),
            participant,
            actor_id=participant,
            actor_role=rooms_pb2.ROOM_ROLE_MEMBER,
        )
        return LowerHandResponse(lowered=lowered)

    return await _run(op)


# ---------------------------------------------------------------------------
# Community-scoped member routes (`/api/v1/communities/...`): own hand state, Town Hall.
# ---------------------------------------------------------------------------


@member_calls_bp.route("/<int:community_id>/calls/rooms/<room_id>/hands/mine", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_READ)  # type: ignore[misc]
@validate_response(MyHandResponse)
async def get_my_hand(community_id: int, room_id: str) -> Any:
    """The caller's own raised-hand state (member): their own entry only, via `ListHands`."""

    async def op() -> Any:
        participant = await _member_actor(community_id)
        mine = await _client.my_hand(community_id, _room_id(room_id), participant)
        hand = mine.hand
        return MyHandResponse(
            participant_id=participant,
            raised=hand is not None,
            acknowledged=bool(hand and hand.acknowledged_at is not None),
            raised_at=hand.raised_at if hand else None,
            acknowledged_at=hand.acknowledged_at if hand else None,
            position=mine.position,
            queue_length=mine.queue_length,
        )

    return await _run(op)


async def _town_hall_response(
    community_id: int, town_hall: call_town_hall.TownHall
) -> TownHallResponse:
    """Describe the Town Hall (participant count is a live figure; no internals)."""
    room = town_hall.room
    listed = await _client.list_participants(community_id, room.room_id)
    return TownHallResponse(
        room_id=room.room_id,
        label=room.label or call_town_hall.TOWN_HALL_LABEL,
        locked=room.locked,
        participant_count=len(listed.participants),
        created=town_hall.created,
        join=TownHallJoin(
            method="POST",
            path=f"/api/v1/community/{community_id}/interact/voice/rooms/{room.room_id}/join",
        ),
    )


@member_calls_bp.route("/<int:community_id>/town-hall", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_READ)  # type: ignore[misc]
@validate_response(TownHallResponse)
async def get_town_hall(community_id: int) -> Any:
    """The community's Town Hall (member). Read-only: 404 until a POST has created it."""

    async def op() -> Any:
        await _gated_member(community_id)
        async_dal, dal = _dal()
        found = await call_town_hall.get_town_hall(_client, async_dal, dal, community_id)
        if found is None:
            raise ApiError(
                message="This community has no Town Hall yet",
                status_code=404,
                code="TOWN_HALL_NOT_FOUND",
            )
        return await _town_hall_response(community_id, found)

    return await _run(op)


@member_calls_bp.route("/<int:community_id>/town-hall", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_WRITE)  # type: ignore[misc]
@validate_response(TownHallResponse)
async def post_town_hall(community_id: int) -> Any:
    """Ensure the community's Town Hall exists (member, write scope); idempotent."""

    async def op() -> Any:
        await _gated_member(community_id)
        async_dal, dal = _dal()
        town_hall = await call_town_hall.ensure_town_hall(_client, async_dal, dal, community_id)
        return await _town_hall_response(community_id, town_hall)

    return await _run(op)


@member_calls_bp.route("/<int:community_id>/calls/rooms/<room_id>/participants", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_READ)  # type: ignore[misc]
@validate_response(MemberParticipantsResponse)
async def get_room_participants_as_member(community_id: int, room_id: str) -> Any:
    """Who is in a room (member): pseudonymous ids, display names, media state; no internals."""

    async def op() -> Any:
        me = await _member_actor(community_id)
        listed = await _client.list_participants(community_id, _room_id(room_id))
        names = await _names(community_id, [p.participant_id for p in listed.participants])
        return MemberParticipantsResponse(
            participants=[
                MemberParticipantDTO(
                    participant_id=p.participant_id,
                    display_name=names.get(p.participant_id, call_participants.GUEST_LABEL),
                    publisher=p.publisher,
                    subscriber=p.subscriber,
                    muted=p.muted,
                    joined_at=p.joined_at,
                    is_self=p.participant_id == me,
                )
                for p in listed.participants
            ],
            locked=listed.locked,
        )

    return await _run(op)


BLUEPRINTS: list[Blueprint] = [calls_admin_bp, member_voice_bp, member_calls_bp]
