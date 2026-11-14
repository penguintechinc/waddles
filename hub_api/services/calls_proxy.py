"""gRPC client from hub-api to svc-streaming's `RoomsService` (call-room control plane).

The Go RTC control plane was folded into `svc-streaming` (Rust). In-cluster service-to-service
traffic is gRPC over HTTP/2 + mTLS with an ES256 machine JWT on every call, never REST,
so `blueprints/v1/calls.py` talks to svc-streaming through this module only. The wire
contract is `proto/waddles/streaming/internal/v1/rooms.proto`; the server is
`core/svc_streaming/src/rooms/grpc.rs`.

Configuration (all from the environment, rendered by the Helm chart into hub-api):

- `STREAMING_RTC_GRPC_TARGET` -- `host:port` of svc-streaming's gRPC listener. No in-code
  default: a guessed host/port silently fails every call, so unset fails loud
  (`StreamingTargetNotConfiguredError`).
- `STREAMING_RTC_GRPC_CA_FILE`, `STREAMING_RTC_GRPC_CLIENT_CERT_FILE`,
  `STREAMING_RTC_GRPC_CLIENT_KEY_FILE` -- the mTLS trust anchor and hub-api's client
  identity (the shared `host-api-tls` material). TLS is mandatory: there is no
  insecure-channel escape hatch.
- `STREAMING_RTC_SERVICE_ID` -- hub-api's allow-listed SPIFFE id (the JWT `sub`); defaults to
  `spiffe://penguintech.io/<DEPLOYMENT_ENV>/hub-api`.
- `STREAMING_RTC_TIMEOUT_SECONDS` -- per-call deadline (default 10).
- `STREAMING_RTC_GRPC_SERVER_NAME` -- optional TLS server-name override.

Every call mints a short-lived (60s) ES256 JWT for hub-api's own SPIFFE identity with the
single scope the RPC needs (`streaming.rooms:read|write|moderate`) via the app's
`ServiceJwtIssuer`; the downstream verifies it before parsing anything. Every request carries
`api_version="v1"`; the server refuses anything else.

Raw gRPC failures are translated to `RoomsRpcError(http_status, message, code)` -- an explicit
HTTP status per failure class, never a masked success (an unknown room is 404, a refused scope
403, an unreachable service 503) -- and every failure is logged at ERROR with the gRPC status
and the downstream message (closed strings and UUIDs only, no PII) and counted in
`waddles_rooms_upstream_failures_total`. `ROUTE_RPCS` is the route -> RPC table the contract
test pins against the proto descriptor.

Results are typed frozen dataclasses (`RoomInfo`, `ParticipantInfo`, `HandInfo`, `JoinInfo`),
never raw protobuf messages or dicts of them, so only the listed fields can reach a response.
Participant ids are UUIDs; display names are resolved by hub-api itself
(`services/call_participants.py`) and never cross to svc-streaming.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import grpc
from flask_core.service_jwt import ServiceJwtError, ServiceJwtIssuer, spiffe_id
from google.protobuf.message import Message
from opentelemetry import metrics

# isort: split
import grpc_internal  # noqa: F401  (puts the generated pb/ tree on sys.path)

# isort: split
from waddles.streaming.internal.v1 import rooms_pb2, rooms_pb2_grpc

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 10.0
_TOKEN_TTL_SECONDS = 60
HUB_API_SERVICE = "hub-api"
# Contract version stamped on every RoomsService request (svc-streaming refuses anything else).
API_VERSION = "v1"

SCOPE_READ = "streaming.rooms:read"
SCOPE_WRITE = "streaming.rooms:write"
SCOPE_MODERATE = "streaming.rooms:moderate"

_ADMIN = "/api/v1/admin/<int:community_id>/calls/rooms"
_MEMBER = "/api/v1/community/<int:community_id>/interact/voice/rooms"
_COMMUNITIES = "/api/v1/communities"

#: (HTTP method, Quart rule) of every `blueprints/v1/calls.py` route -> `RoomsService` RPC.
#: `tests/test_calls_rpc_contract.py` fails when a route is missing here, when a mapped RPC is
#: absent from the proto descriptor, or when a proto RPC is neither mapped here nor listed (with
#: a reason) in `UNROUTED_RPCS`.
ROUTE_RPCS: dict[tuple[str, str], str] = {
    ("GET", _ADMIN): "ListRooms",
    ("POST", _ADMIN): "CreateRoom",
    ("GET", f"{_ADMIN}/<room_name>"): "GetRoom",
    ("DELETE", f"{_ADMIN}/<room_name>"): "DeleteRoom",
    ("POST", f"{_ADMIN}/<room_name>/lock"): "LockRoom",
    ("POST", f"{_ADMIN}/<room_name>/unlock"): "UnlockRoom",
    ("GET", f"{_ADMIN}/<room_name>/participants"): "ListParticipants",
    ("POST", f"{_ADMIN}/<room_name>/kick"): "KickParticipant",
    ("POST", f"{_ADMIN}/<room_name>/mute"): "MuteParticipant",
    ("POST", f"{_ADMIN}/<room_name>/unmute"): "UnmuteParticipant",
    ("POST", f"{_ADMIN}/<room_name>/mute-all"): "MuteAll",
    ("GET", f"{_ADMIN}/<room_name>/raised-hands"): "ListHands",
    ("POST", f"{_ADMIN}/<room_name>/acknowledge-hand"): "AcknowledgeHand",
    ("POST", f"{_ADMIN}/<room_name>/reset-hands"): "ResetHands",
    ("GET", _MEMBER): "ListRooms",
    ("POST", _MEMBER): "CreateRoom",
    ("POST", f"{_MEMBER}/<room_name>/join"): "JoinRoom",
    ("POST", f"{_MEMBER}/<room_name>/leave"): "LeaveRoom",
    ("POST", f"{_MEMBER}/<room_name>/raise-hand"): "RaiseHand",
    ("POST", f"{_MEMBER}/<room_name>/lower-hand"): "LowerHand",
    (
        "GET",
        f"{_COMMUNITIES}/<int:community_id>/calls/rooms/<room_id>/participants",
    ): "ListParticipants",
    ("GET", f"{_COMMUNITIES}/<int:community_id>/calls/rooms/<room_id>/hands/mine"): "ListHands",
    ("GET", f"{_COMMUNITIES}/<int:community_id>/town-hall"): "ListRooms",
    ("POST", f"{_COMMUNITIES}/<int:community_id>/town-hall"): "CreateRoom",
}

#: Proto RPCs deliberately without a hub-api route, each with the reason. The contract test
#: fails when a proto RPC is neither routed nor listed here, so a future RPC cannot be left
#: unmapped silently.
UNROUTED_RPCS: dict[str, str] = {}

#: gRPC status -> (HTTP status, machine code) surfaced to hub-api's caller.
_HTTP_STATUS: dict[grpc.StatusCode, tuple[int, str]] = {
    grpc.StatusCode.NOT_FOUND: (404, "NOT_FOUND"),
    grpc.StatusCode.INVALID_ARGUMENT: (400, "INVALID_ARGUMENT"),
    grpc.StatusCode.RESOURCE_EXHAUSTED: (429, "RESOURCE_EXHAUSTED"),
    grpc.StatusCode.PERMISSION_DENIED: (403, "FORBIDDEN"),
    grpc.StatusCode.UNIMPLEMENTED: (501, "STREAMING_UNIMPLEMENTED"),
    grpc.StatusCode.DEADLINE_EXCEEDED: (504, "STREAMING_TIMEOUT"),
    grpc.StatusCode.UNAVAILABLE: (503, "STREAMING_UNAVAILABLE"),
    # hub-api's own credential was refused: not the caller's fault
    grpc.StatusCode.UNAUTHENTICATED: (502, "STREAMING_AUTH_REFUSED"),
}

#: Statuses whose downstream message is relayed to the caller (closed strings and ids only,
#: see grpc.rs); every other class gets a generic message so internals never leak.
_RELAY_MESSAGE = frozenset(
    {
        grpc.StatusCode.NOT_FOUND,
        grpc.StatusCode.INVALID_ARGUMENT,
        grpc.StatusCode.RESOURCE_EXHAUSTED,
        grpc.StatusCode.PERMISSION_DENIED,
        grpc.StatusCode.UNIMPLEMENTED,
    }
)

_meter = metrics.get_meter("waddles.hub_api.calls_proxy")
_upstream_failures = _meter.create_counter(
    "waddles_rooms_upstream_failures_total",
    description=(
        "Failed hub-api -> svc-streaming RoomsService calls, by rpc and gRPC status "
        "(label values are closed sets: no ids, names or messages)."
    ),
)


class StreamingTargetNotConfiguredError(RuntimeError):
    """`STREAMING_RTC_GRPC_TARGET` (or the mTLS file set) is not configured.

    The svc-streaming address is deployment-specific (the Helm Service name carries the
    release prefix), so there is no in-code default.
    """


class RoomsRpcError(Exception):
    """A failed `RoomsService` call, already translated to an HTTP status + safe message."""

    def __init__(self, status_code: int, message: str, code: str = "STREAMING_ERROR") -> None:
        """Record the HTTP status, safe message and machine-readable error code."""
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code


@dataclass(slots=True, frozen=True)
class RoomsClientConfig:
    """Connection settings for svc-streaming's gRPC listener."""

    target: str
    ca_pem: bytes
    client_cert_pem: bytes
    client_key_pem: bytes
    timeout_seconds: float
    server_name: str | None

    @classmethod
    def from_env(cls) -> RoomsClientConfig:
        """Build from the `STREAMING_RTC_GRPC_*` environment; raises when anything is missing."""
        target = os.getenv("STREAMING_RTC_GRPC_TARGET", "").strip()
        paths = {
            name: os.getenv(name, "").strip()
            for name in (
                "STREAMING_RTC_GRPC_CA_FILE",
                "STREAMING_RTC_GRPC_CLIENT_CERT_FILE",
                "STREAMING_RTC_GRPC_CLIENT_KEY_FILE",
            )
        }
        missing = [n for n, v in paths.items() if not v]
        if not target or missing:
            raise StreamingTargetNotConfiguredError(
                "STREAMING_RTC_GRPC_TARGET and the "
                "STREAMING_RTC_GRPC_{CA,CLIENT_CERT,CLIENT_KEY}_FILE "
                "mTLS files must all be set (rendered by the Helm chart into the hub-api "
                f"Deployment); refusing to guess or to connect without mTLS (unset: "
                f"{', '.join(['STREAMING_RTC_GRPC_TARGET'] * (not target) + missing)})"
            )

        def read(path: str) -> bytes:
            with open(path, "rb") as fh:
                return fh.read()

        return cls(
            target=target,
            ca_pem=read(paths["STREAMING_RTC_GRPC_CA_FILE"]),
            client_cert_pem=read(paths["STREAMING_RTC_GRPC_CLIENT_CERT_FILE"]),
            client_key_pem=read(paths["STREAMING_RTC_GRPC_CLIENT_KEY_FILE"]),
            timeout_seconds=float(
                os.getenv("STREAMING_RTC_TIMEOUT_SECONDS", str(_DEFAULT_TIMEOUT_SECONDS))
            ),
            server_name=os.getenv("STREAMING_RTC_GRPC_SERVER_NAME", "").strip() or None,
        )


IssuerProvider = Callable[[], ServiceJwtIssuer | None]


@dataclass(slots=True, frozen=True)
class RoomInfo:
    """A call room: ids and counts only, no PII. `label` is the creator-supplied display name."""

    room_id: str
    community_id: str
    label: str | None
    max_publishers: int
    created_at: int
    publisher_ids: list[str]
    subscriber_count: int
    locked: bool


@dataclass(slots=True, frozen=True)
class ParticipantInfo:
    """A room participant as svc-streaming knows it: a UUID plus media flags, never a name."""

    participant_id: str
    publisher: bool
    subscriber: bool
    muted: bool
    joined_at: int | None


@dataclass(slots=True, frozen=True)
class ParticipantList:
    """`ListParticipants` result."""

    room_id: str
    locked: bool
    participants: list[ParticipantInfo]


@dataclass(slots=True, frozen=True)
class HandInfo:
    """One raised hand: participant UUID plus queue timestamps (unix seconds)."""

    participant_id: str
    raised_at: int
    acknowledged_at: int | None
    acknowledged_by: str | None


@dataclass(slots=True, frozen=True)
class HandList:
    """`ListHands` result: the visible entries, the full queue length and the actor's position."""

    hands: list[HandInfo]
    count: int
    position: int | None


@dataclass(slots=True, frozen=True)
class MyHand:
    """The caller's own hand (None when not raised) with queue length and 1-based position."""

    hand: HandInfo | None
    queue_length: int
    position: int | None


@dataclass(slots=True, frozen=True)
class JoinInfo:
    """`JoinRoom` result: a room token for ONE participant plus the WHIP/WHEP endpoints."""

    token: str
    token_id: str
    participant_id: str
    expires_at: int
    whip_url: str
    whep_url: str


def _room(room: rooms_pb2.Room) -> RoomInfo:
    """A `Room` message as the explicit `RoomInfo` field set."""
    return RoomInfo(
        room_id=room.room_id,
        community_id=room.community_id,
        label=room.label if room.HasField("label") else None,
        max_publishers=room.max_publishers,
        created_at=room.created_at,
        publisher_ids=list(room.publisher_ids),
        subscriber_count=room.subscriber_count,
        locked=room.locked,
    )


def _hand(hand: rooms_pb2.RaisedHand) -> HandInfo:
    """A `RaisedHand` message as the explicit `HandInfo` field set (unset optionals -> None)."""
    return HandInfo(
        participant_id=hand.participant_id,
        raised_at=hand.raised_at,
        acknowledged_at=hand.acknowledged_at if hand.HasField("acknowledged_at") else None,
        acknowledged_by=hand.acknowledged_by if hand.HasField("acknowledged_by") else None,
    )


class RoomsClient:
    """One method per `RoomsService` RPC; every call is mTLS + a per-call ES256 JWT.

    The channel and config resolve lazily on first use so importing the blueprint never needs
    the environment; a missing target fails loudly then (`StreamingTargetNotConfiguredError`).
    `channel` / `issuer_provider` are injectable for tests.
    """

    def __init__(
        self,
        config: RoomsClientConfig | None = None,
        *,
        issuer_provider: IssuerProvider | None = None,
        deployment_env: Callable[[], str] | None = None,
        channel: grpc.aio.Channel | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        """Store the lazy config/issuer/channel hooks; no connection is made until first use."""
        self._explicit_config = config
        self._issuer_provider = issuer_provider
        self._deployment_env = deployment_env or (lambda: os.getenv("DEPLOYMENT_ENV", "alpha"))
        self._channel = channel
        self._timeout = timeout_seconds
        self._stub: rooms_pb2_grpc.RoomsServiceStub | None = None

    def _get_stub(self) -> tuple[rooms_pb2_grpc.RoomsServiceStub, float]:
        """Build (once) the mTLS channel + stub and return it with the per-call deadline."""
        config = self._explicit_config
        if self._stub is None:
            if self._channel is None:
                config = config or RoomsClientConfig.from_env()
                creds = grpc.ssl_channel_credentials(
                    root_certificates=config.ca_pem,
                    private_key=config.client_key_pem,
                    certificate_chain=config.client_cert_pem,
                )
                options = []
                if config.server_name:
                    options.append(("grpc.ssl_target_name_override", config.server_name))
                self._channel = grpc.aio.secure_channel(config.target, creds, options=options)
            self._stub = rooms_pb2_grpc.RoomsServiceStub(self._channel)
        timeout = self._timeout or (config.timeout_seconds if config else _DEFAULT_TIMEOUT_SECONDS)
        return self._stub, timeout

    def _token(self, scope: str) -> str:
        """Mint hub-api's short-lived machine JWT for `scope`; fail loud when it cannot."""
        issuer = self._issuer_provider() if self._issuer_provider else None
        if issuer is None:
            self._record_failure("(token)", "NOT_CONFIGURED", "service JWT issuer not configured")
            raise RoomsRpcError(
                502, "Streaming service unavailable", "STREAMING_AUTH_NOT_CONFIGURED"
            )
        try:
            service_id = os.getenv("STREAMING_RTC_SERVICE_ID", "").strip() or spiffe_id(
                self._deployment_env(), HUB_API_SERVICE
            )
            return str(
                issuer.issue(
                    service_id,
                    scope,
                    ttl_seconds=_TOKEN_TTL_SECONDS,
                )
            )
        except ServiceJwtError as exc:
            self._record_failure("(token)", type(exc).__name__, str(exc))
            raise RoomsRpcError(
                502, "Streaming service unavailable", "STREAMING_AUTH_NOT_CONFIGURED"
            ) from exc

    @staticmethod
    def _record_failure(rpc: str, status: str, message: str, *, exc_info: bool = False) -> None:
        """Log one upstream failure at ERROR (status + message, no PII) and count it."""
        logger.error(
            "rooms gRPC %s failed: %s: %s",
            rpc,
            status,
            message,
            exc_info=exc_info,
        )
        _upstream_failures.add(1, {"rpc": rpc, "status": status})

    async def _call(
        self,
        rpc: str,
        scope: str,
        request: Message,
    ) -> Any:
        """Issue one RPC with a fresh token; translate failures to `RoomsRpcError`."""
        stub, timeout = self._get_stub()
        method: Callable[..., Awaitable[Any]] = getattr(stub, rpc)
        metadata = (("authorization", f"Bearer {self._token(scope)}"),)
        try:
            return await method(request, timeout=timeout, metadata=metadata)
        except grpc.aio.AioRpcError as exc:
            grpc_code = exc.code()
            status, code = _HTTP_STATUS.get(grpc_code, (502, "STREAMING_ERROR"))
            details = exc.details() or grpc_code.name
            self._record_failure(rpc, grpc_code.name, details, exc_info=status >= 500)
            if grpc_code in _RELAY_MESSAGE:
                # Downstream messages are built from closed strings and ids only (see grpc.rs).
                raise RoomsRpcError(status, details, code) from exc
            raise RoomsRpcError(status, "Streaming service unavailable", code) from exc

    async def list_rooms(self, community_id: int) -> list[RoomInfo]:
        """`ListRooms` -- every live room the community owns."""
        resp = await self._call(
            "ListRooms",
            SCOPE_READ,
            rooms_pb2.ListRoomsRequest(api_version=API_VERSION, community_id=str(community_id)),
        )
        return [_room(r) for r in resp.rooms]

    async def get_room(self, community_id: int, room_id: str) -> RoomInfo:
        """`GetRoom` -- one room, community-scoped."""
        resp = await self._call(
            "GetRoom",
            SCOPE_READ,
            rooms_pb2.GetRoomRequest(
                api_version=API_VERSION, community_id=str(community_id), room_id=room_id
            ),
        )
        return _room(resp.room)

    async def list_participants(self, community_id: int, room_id: str) -> ParticipantList:
        """`ListParticipants` -- participants by UUID with publisher/subscriber/muted flags."""
        resp = await self._call(
            "ListParticipants",
            SCOPE_READ,
            rooms_pb2.ListParticipantsRequest(
                api_version=API_VERSION, community_id=str(community_id), room_id=room_id
            ),
        )
        return ParticipantList(
            room_id=resp.room_id,
            locked=resp.locked,
            participants=[
                ParticipantInfo(
                    participant_id=p.participant_id,
                    publisher=p.publisher,
                    subscriber=p.subscriber,
                    muted=p.muted,
                    joined_at=p.joined_at if p.HasField("joined_at") else None,
                )
                for p in resp.participants
            ],
        )

    async def create_room(
        self,
        community_id: int,
        *,
        label: str,
        max_publishers: int | None,
        empty_timeout_secs: int | None = None,
    ) -> RoomInfo:
        """`CreateRoom` -- `label` carries the caller-supplied room name.

        `empty_timeout_secs`: None = server default, 0 = never expire (persistent rooms such as
        the Town Hall), otherwise seconds an empty room lives (the server clamps it).
        """
        resp = await self._call(
            "CreateRoom",
            SCOPE_WRITE,
            rooms_pb2.CreateRoomRequest(
                api_version=API_VERSION,
                community_id=str(community_id),
                label=label,
                max_publishers=max_publishers,
                empty_timeout_secs=empty_timeout_secs,
            ),
        )
        return _room(resp.room)

    async def delete_room(self, community_id: int, room_id: str) -> RoomInfo:
        """`DeleteRoom` -- closes the room."""
        resp = await self._call(
            "DeleteRoom",
            SCOPE_WRITE,
            rooms_pb2.DeleteRoomRequest(
                api_version=API_VERSION, community_id=str(community_id), room_id=room_id
            ),
        )
        return _room(resp.room)

    async def join_room(
        self,
        community_id: int,
        room_id: str,
        participant_id: str,
        *,
        publish: bool,
        subscribe: bool,
        ttl_secs: int,
        staff: bool = False,
    ) -> JoinInfo:
        """`JoinRoom` -- mints a room token for ONE participant with exactly the given grants.

        The grants come from the shared access policy (`services/call_access.py`), never a
        constant, and `ttl_secs` is the contract's short join lifetime. `staff` makes the call
        carry the moderate-scope service JWT, which is the only way svc-streaming records a
        participant as room staff (no request field can claim it).
        """
        scope = SCOPE_MODERATE if staff else SCOPE_WRITE
        resp = await self._call(
            "JoinRoom",
            scope,
            rooms_pb2.JoinRoomRequest(
                api_version=API_VERSION,
                community_id=str(community_id),
                room_id=room_id,
                participant_id=participant_id,
                publish=publish,
                subscribe=subscribe,
                ttl_secs=ttl_secs,
            ),
        )
        return JoinInfo(
            token=resp.token,
            token_id=resp.token_id,
            participant_id=resp.participant_id,
            expires_at=resp.expires_at,
            whip_url=resp.whip_url,
            whep_url=resp.whep_url,
        )

    async def leave_room(
        self,
        community_id: int,
        room_id: str,
        participant_id: str,
        *,
        actor_id: str,
        actor_role: int,
    ) -> int:
        """`LeaveRoom` -- returns how many live sessions were closed."""
        resp = await self._call(
            "LeaveRoom",
            SCOPE_WRITE,
            rooms_pb2.LeaveRoomRequest(
                api_version=API_VERSION,
                community_id=str(community_id),
                room_id=room_id,
                participant_id=participant_id,
                actor_id=actor_id,
                actor_role=actor_role,
            ),
        )
        return int(resp.sessions_closed)

    async def kick_participant(
        self,
        community_id: int,
        room_id: str,
        participant_id: str,
        *,
        actor_id: str,
        actor_role: int,
    ) -> int:
        """`KickParticipant` -- removes and blocklists the participant; returns sessions closed."""
        resp = await self._call(
            "KickParticipant",
            SCOPE_MODERATE,
            rooms_pb2.KickParticipantRequest(
                api_version=API_VERSION,
                community_id=str(community_id),
                room_id=room_id,
                participant_id=participant_id,
                actor_id=actor_id,
                actor_role=actor_role,
            ),
        )
        return int(resp.sessions_closed)

    async def set_participant_muted(
        self,
        community_id: int,
        room_id: str,
        participant_id: str,
        muted: bool,
        *,
        actor_id: str,
        actor_role: int,
    ) -> bool:
        """`MuteParticipant` / `UnmuteParticipant`; returns the participant's resulting state."""
        if muted:
            mute_resp = await self._call(
                "MuteParticipant",
                SCOPE_MODERATE,
                rooms_pb2.MuteParticipantRequest(
                    api_version=API_VERSION,
                    community_id=str(community_id),
                    room_id=room_id,
                    participant_id=participant_id,
                    actor_id=actor_id,
                    actor_role=actor_role,
                ),
            )
            return bool(mute_resp.muted)
        unmute_resp = await self._call(
            "UnmuteParticipant",
            SCOPE_MODERATE,
            rooms_pb2.UnmuteParticipantRequest(
                api_version=API_VERSION,
                community_id=str(community_id),
                room_id=room_id,
                participant_id=participant_id,
                actor_id=actor_id,
                actor_role=actor_role,
            ),
        )
        return bool(unmute_resp.muted)

    async def mute_all(
        self, community_id: int, room_id: str, *, actor_id: str, actor_role: int
    ) -> int:
        """`MuteAll` -- mutes every publisher except the actor; returns how many."""
        resp = await self._call(
            "MuteAll",
            SCOPE_MODERATE,
            rooms_pb2.MuteAllRequest(
                api_version=API_VERSION,
                community_id=str(community_id),
                room_id=room_id,
                actor_id=actor_id,
                actor_role=actor_role,
            ),
        )
        return int(resp.muted)

    async def set_locked(
        self, community_id: int, room_id: str, locked: bool, *, actor_id: str, actor_role: int
    ) -> bool:
        """`LockRoom` / `UnlockRoom`; returns the resulting lock state."""
        if locked:
            resp = await self._call(
                "LockRoom",
                SCOPE_MODERATE,
                rooms_pb2.LockRoomRequest(
                    api_version=API_VERSION,
                    community_id=str(community_id),
                    room_id=room_id,
                    actor_id=actor_id,
                    actor_role=actor_role,
                ),
            )
        else:
            resp = await self._call(
                "UnlockRoom",
                SCOPE_MODERATE,
                rooms_pb2.UnlockRoomRequest(
                    api_version=API_VERSION,
                    community_id=str(community_id),
                    room_id=room_id,
                    actor_id=actor_id,
                    actor_role=actor_role,
                ),
            )
        return bool(resp.locked)

    async def list_hands(
        self, community_id: int, room_id: str, *, actor_id: str, staff: bool = False
    ) -> HandList:
        """`ListHands` as `actor_id` (who must have joined the room).

        `staff=True` carries the moderate-scope JWT: the server then returns the WHOLE queue,
        provided the actor is recorded as room staff. Otherwise (read scope) it returns only the
        actor's own entry plus the queue length and the actor's position.
        """
        scope = SCOPE_MODERATE if staff else SCOPE_READ
        resp = await self._call(
            "ListHands",
            scope,
            rooms_pb2.ListHandsRequest(
                api_version=API_VERSION,
                community_id=str(community_id),
                room_id=room_id,
                actor_id=actor_id,
            ),
        )
        return HandList(
            hands=[_hand(h) for h in resp.hands],
            count=resp.count,
            position=resp.position if resp.HasField("position") else None,
        )

    async def my_hand(self, community_id: int, room_id: str, participant_id: str) -> MyHand:
        """The caller's own queue entry, length and position (read scope: no one else's data).

        The server returns only the actor's own entry for a read-scope call; the filter below
        is defence in depth so a misbehaving server cannot leak another participant's entry.
        """
        listed = await self.list_hands(community_id, room_id, actor_id=participant_id)
        own = next((h for h in listed.hands if h.participant_id == participant_id), None)
        return MyHand(hand=own, queue_length=listed.count, position=listed.position)

    async def raise_hand(self, community_id: int, room_id: str, participant_id: str) -> HandInfo:
        """`RaiseHand` for oneself -- idempotent: raising twice keeps the original position."""
        resp = await self._call(
            "RaiseHand",
            SCOPE_WRITE,
            rooms_pb2.RaiseHandRequest(
                api_version=API_VERSION,
                community_id=str(community_id),
                room_id=room_id,
                participant_id=participant_id,
                actor_id=participant_id,
            ),
        )
        return _hand(resp.hand)

    async def lower_hand(
        self,
        community_id: int,
        room_id: str,
        participant_id: str,
        *,
        actor_id: str,
        actor_role: int,
    ) -> bool:
        """`LowerHand` -- returns False when the participant had no raised hand (a no-op)."""
        resp = await self._call(
            "LowerHand",
            SCOPE_WRITE,
            rooms_pb2.LowerHandRequest(
                api_version=API_VERSION,
                community_id=str(community_id),
                room_id=room_id,
                participant_id=participant_id,
                actor_id=actor_id,
                actor_role=actor_role,
            ),
        )
        return bool(resp.lowered)

    async def acknowledge_hand(
        self,
        community_id: int,
        room_id: str,
        participant_id: str,
        *,
        actor_id: str,
        actor_role: int,
    ) -> HandInfo:
        """`AcknowledgeHand` -- a moderator marks the participant's hand as seen."""
        resp = await self._call(
            "AcknowledgeHand",
            SCOPE_MODERATE,
            rooms_pb2.AcknowledgeHandRequest(
                api_version=API_VERSION,
                community_id=str(community_id),
                room_id=room_id,
                participant_id=participant_id,
                actor_id=actor_id,
                actor_role=actor_role,
            ),
        )
        return _hand(resp.hand)

    async def reset_hands(
        self, community_id: int, room_id: str, *, actor_id: str, actor_role: int
    ) -> int:
        """`ResetHands` -- clears the whole queue; returns how many hands were cleared."""
        resp = await self._call(
            "ResetHands",
            SCOPE_MODERATE,
            rooms_pb2.ResetHandsRequest(
                api_version=API_VERSION,
                community_id=str(community_id),
                room_id=room_id,
                actor_id=actor_id,
                actor_role=actor_role,
            ),
        )
        return int(resp.cleared)
