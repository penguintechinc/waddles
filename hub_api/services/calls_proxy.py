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
`ServiceJwtIssuer`; the downstream verifies it before parsing anything.

Raw gRPC failures are translated to `RoomsRpcError(http_status, message)` so the blueprint
keeps its `{success: false, error}` envelope; `ROUTE_RPCS` is the route -> RPC table the
contract test pins against the proto descriptor.
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

#: Sentinel for a `calls.py` route with no svc-streaming equivalent (explicit 501, never a
#: silent no-op). Raise-hand state lives in the rooms app bundle's kv, which hub-api has no
#: client for; svc-streaming only owns media and moderation state.
NOT_IMPLEMENTED = "NOT_IMPLEMENTED"

#: (HTTP method, Quart rule) of every `blueprints/v1/calls.py` route -> `RoomsService` RPC.
#: `tests/test_calls_rpc_contract.py` fails when a route is missing here, when a mapped RPC is
#: absent from the proto descriptor, or when a `NOT_IMPLEMENTED` entry gains an RPC.
ROUTE_RPCS: dict[tuple[str, str], str] = {
    ("GET", "/api/v1/admin/<int:community_id>/calls/rooms"): "ListRooms",
    ("POST", "/api/v1/admin/<int:community_id>/calls/rooms"): "CreateRoom",
    ("GET", "/api/v1/admin/<int:community_id>/calls/rooms/<room_name>"): "GetRoom",
    ("DELETE", "/api/v1/admin/<int:community_id>/calls/rooms/<room_name>"): "DeleteRoom",
    ("POST", "/api/v1/admin/<int:community_id>/calls/rooms/<room_name>/lock"): "LockRoom",
    ("POST", "/api/v1/admin/<int:community_id>/calls/rooms/<room_name>/unlock"): "UnlockRoom",
    (
        "GET",
        "/api/v1/admin/<int:community_id>/calls/rooms/<room_name>/participants",
    ): "ListParticipants",
    (
        "POST",
        "/api/v1/admin/<int:community_id>/calls/rooms/<room_name>/kick",
    ): "KickParticipant",
    ("POST", "/api/v1/admin/<int:community_id>/calls/rooms/<room_name>/mute-all"): "MuteAll",
    (
        "GET",
        "/api/v1/admin/<int:community_id>/calls/rooms/<room_name>/raised-hands",
    ): NOT_IMPLEMENTED,
    (
        "POST",
        "/api/v1/admin/<int:community_id>/calls/rooms/<room_name>/acknowledge-hand",
    ): NOT_IMPLEMENTED,
    ("GET", "/api/v1/community/<int:community_id>/interact/voice/rooms"): "ListRooms",
    ("POST", "/api/v1/community/<int:community_id>/interact/voice/rooms"): "CreateRoom",
    (
        "POST",
        "/api/v1/community/<int:community_id>/interact/voice/rooms/<room_name>/join",
    ): "JoinRoom",
    (
        "POST",
        "/api/v1/community/<int:community_id>/interact/voice/rooms/<room_name>/leave",
    ): "LeaveRoom",
}

#: gRPC status -> HTTP status surfaced to hub-api's caller.
_HTTP_STATUS: dict[grpc.StatusCode, int] = {
    grpc.StatusCode.NOT_FOUND: 404,
    grpc.StatusCode.INVALID_ARGUMENT: 400,
    grpc.StatusCode.RESOURCE_EXHAUSTED: 429,
    grpc.StatusCode.PERMISSION_DENIED: 403,
    grpc.StatusCode.UNIMPLEMENTED: 501,
    grpc.StatusCode.DEADLINE_EXCEEDED: 504,
    # hub-api's own credential was refused: not the caller's fault
    grpc.StatusCode.UNAUTHENTICATED: 502,
}


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


def _room_dict(room: rooms_pb2.Room) -> dict[str, Any]:
    """A `Room` message as the JSON object hub-api returns (ids/counts only, no PII)."""
    return {
        "room_id": room.room_id,
        "community_id": room.community_id,
        "label": room.label if room.HasField("label") else None,
        "max_publishers": room.max_publishers,
        "created_at": room.created_at,
        "publisher_ids": list(room.publisher_ids),
        "subscriber_count": room.subscriber_count,
        "locked": room.locked,
    }


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
        issuer = self._issuer_provider() if self._issuer_provider else None
        if issuer is None:
            raise RoomsRpcError(
                502, "Streaming service unavailable", "STREAMING_AUTH_NOT_CONFIGURED"
            )
        try:
            service_id = os.getenv("STREAMING_RTC_SERVICE_ID", "").strip() or spiffe_id(
                self._deployment_env(), HUB_API_SERVICE
            )
            return issuer.issue(
                service_id,
                scope,
                ttl_seconds=_TOKEN_TTL_SECONDS,
            )
        except ServiceJwtError as exc:
            logger.error("rooms gRPC: cannot mint service JWT (%s): %s", scope, type(exc).__name__)
            raise RoomsRpcError(
                502, "Streaming service unavailable", "STREAMING_AUTH_NOT_CONFIGURED"
            ) from exc

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
            code = exc.code()
            status = _HTTP_STATUS.get(code, 502)
            logger.warning("rooms gRPC %s failed: %s", rpc, code.name)
            if status == 502:
                raise RoomsRpcError(
                    502, "Streaming service unavailable", "STREAMING_UNAVAILABLE"
                ) from exc
            # Downstream messages are built from closed strings and ids only (see grpc.rs).
            raise RoomsRpcError(status, exc.details() or code.name) from exc

    async def list_rooms(self, community_id: int) -> list[dict[str, Any]]:
        """`ListRooms` -- every live room the community owns."""
        resp = await self._call(
            "ListRooms",
            SCOPE_READ,
            rooms_pb2.ListRoomsRequest(api_version=API_VERSION, community_id=str(community_id)),
        )
        return [_room_dict(r) for r in resp.rooms]

    async def get_room(self, community_id: int, room_id: str) -> dict[str, Any]:
        """`GetRoom` -- one room, community-scoped."""
        resp = await self._call(
            "GetRoom",
            SCOPE_READ,
            rooms_pb2.GetRoomRequest(
                api_version=API_VERSION, community_id=str(community_id), room_id=room_id
            ),
        )
        return _room_dict(resp.room)

    async def list_participants(self, community_id: int, room_id: str) -> dict[str, Any]:
        """`ListParticipants` -- participants by UUID with publisher/subscriber/muted flags."""
        resp = await self._call(
            "ListParticipants",
            SCOPE_READ,
            rooms_pb2.ListParticipantsRequest(
                api_version=API_VERSION, community_id=str(community_id), room_id=room_id
            ),
        )
        return {
            "room_id": resp.room_id,
            "locked": resp.locked,
            "participants": [
                {
                    "participant_id": p.participant_id,
                    "publisher": p.publisher,
                    "subscriber": p.subscriber,
                    "muted": p.muted,
                }
                for p in resp.participants
            ],
        }

    async def create_room(
        self, community_id: int, *, label: str, max_publishers: int | None
    ) -> dict[str, Any]:
        """`CreateRoom` -- `label` carries the caller-supplied room name."""
        resp = await self._call(
            "CreateRoom",
            SCOPE_WRITE,
            rooms_pb2.CreateRoomRequest(
                api_version=API_VERSION,
                community_id=str(community_id),
                label=label,
                max_publishers=max_publishers,
            ),
        )
        return _room_dict(resp.room)

    async def delete_room(self, community_id: int, room_id: str) -> dict[str, Any]:
        """`DeleteRoom` -- closes the room."""
        resp = await self._call(
            "DeleteRoom",
            SCOPE_WRITE,
            rooms_pb2.DeleteRoomRequest(
                api_version=API_VERSION, community_id=str(community_id), room_id=room_id
            ),
        )
        return _room_dict(resp.room)

    async def join_room(
        self, community_id: int, room_id: str, participant_id: str
    ) -> dict[str, Any]:
        """`JoinRoom` -- mints a publish+subscribe room token for ONE participant."""
        resp = await self._call(
            "JoinRoom",
            SCOPE_WRITE,
            rooms_pb2.JoinRoomRequest(
                api_version=API_VERSION,
                community_id=str(community_id),
                room_id=room_id,
                participant_id=participant_id,
                publish=True,
                subscribe=True,
            ),
        )
        return {
            "token": resp.token,
            "token_id": resp.token_id,
            "participant_id": resp.participant_id,
            "expires_at": resp.expires_at,
            "whip_url": resp.whip_url,
            "whep_url": resp.whep_url,
        }

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
