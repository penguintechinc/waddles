"""Shared fixtures for the calls (RTC) test files: an in-process `RoomsService` and seed helpers.

`FakeRooms` is a REAL `grpc.aio` server implementing every `RoomsService` RPC with the
contract rules svc-streaming enforces, so the real `RoomsClient`, protobuf marshalling,
per-call JWT metadata and status-code mapping all run end to end:

- `api_version` must be `"v1"` (else UNIMPLEMENTED, exactly like `rooms/grpc.rs`);
- the bearer token must carry the scope the RPC needs (`streaming.rooms:read|write|moderate`,
  see the proto) -- the test issuer mints `jwt-for-<scope>` tokens and the server compares;
- moderation RPCs require a moderator/admin actor role;
- every lookup is `(community_id, room_id)`-scoped: a room addressed through another community
  is NOT_FOUND, never a leak.

The module also holds the DB seed helpers and the fixtures (`fake`, `app`, `client`,
`_feature_enabled_default_on`) the test files import. It has no tests of its own.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import grpc
import pytest
import pytest_asyncio
from quart import Quart
from quart_schema import QuartSchema

# isort: split
import grpc_internal  # noqa: F401  (puts the generated pb/ tree on sys.path)

# isort: split
from waddles.streaming.internal.v1 import rooms_pb2, rooms_pb2_grpc

import blueprints.v1.calls as calls_module
from blueprints.v1.calls import calls_admin_bp, member_calls_bp, member_voice_bp
from services.calls_proxy import RoomsClient
from tests.conftest import make_user_token, seed_community, seed_membership

ROOM = "0b6f3e1a-8f5e-4d0a-9a43-6a1d5f7c2e10"
PARTICIPANT = "5d1f2e3a-1111-4a2b-8c3d-9e8f7a6b5c4d"

_SCOPE_BY_RPC: dict[str, str] = {
    **dict.fromkeys(("ListRooms", "GetRoom", "ListParticipants", "ListHands"), "read"),
    **dict.fromkeys(
        ("CreateRoom", "DeleteRoom", "JoinRoom", "LeaveRoom", "RaiseHand", "LowerHand"), "write"
    ),
    **dict.fromkeys(
        (
            "KickParticipant",
            "MuteParticipant",
            "UnmuteParticipant",
            "MuteAll",
            "LockRoom",
            "UnlockRoom",
            "AcknowledgeHand",
            "ResetHands",
        ),
        "moderate",
    ),
}
_MODERATOR_ROLES = (rooms_pb2.ROOM_ROLE_MODERATOR, rooms_pb2.ROOM_ROLE_ADMIN)


def user_uuid(user_id: int) -> str:
    """Deterministic `hub_users.uuid` for a seeded test user."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"test-hub-user-{user_id}"))


def seed_user(overlay_db: Any, user_id: int, *, display_name: str | None = None) -> str:
    """Insert a `hub_users` row with `id == user_id` and a deterministic uuid; returns the uuid."""
    dal = overlay_db.dal
    dal.hub_users.insert(
        id=user_id,
        uuid=user_uuid(user_id),
        display_name=display_name,
        username=f"user{user_id}",
    )
    dal.commit()
    return user_uuid(user_id)


class FakeRooms(rooms_pb2_grpc.RoomsServiceServicer):
    """Stateful `RoomsService`: records (rpc, request, metadata); optional per-rpc failure."""

    def __init__(self) -> None:
        """Start with no rooms, no recorded calls and no scripted failures."""
        self.calls: list[tuple[str, Any, dict[str, str]]] = []
        self.fail: dict[str, tuple[grpc.StatusCode, str]] = {}
        #: (community_id, room_id) -> {"label", "locked", "created_at", "members", "hands"}
        self.rooms: dict[tuple[str, str], dict[str, Any]] = {}
        self._clock = 1700000000

    def seed_room(
        self,
        community_id: int | str,
        room_id: str = ROOM,
        label: str = "room1",
        *,
        created_at: int = 1700000000,
        locked: bool = False,
    ) -> None:
        """Create `room_id` in `community_id` explicitly."""
        self.rooms[(str(community_id), room_id)] = {
            "label": label,
            "locked": locked,
            "created_at": created_at,
            "members": {},
            "hands": [],
            "empty_timeout_secs": 300,
        }

    def seed_participant(
        self, community_id: int | str, participant_id: str, room_id: str = ROOM
    ) -> None:
        """Put `participant_id` in the room as a publisher."""
        self.rooms[(str(community_id), room_id)]["members"][participant_id] = {
            "muted": False,
            "joined_at": 1700000100,
        }

    def _tick(self) -> int:
        self._clock += 1
        return self._clock

    async def _guard(self, rpc: str, request: Any, context: Any) -> None:
        """Record the call and enforce api_version, scope, role and scripted failures."""
        metadata = dict(context.invocation_metadata())
        self.calls.append((rpc, request, metadata))
        if request.api_version != "v1":
            await context.abort(
                grpc.StatusCode.UNIMPLEMENTED, f"api_version {request.api_version} not supported"
            )
        needed = f"streaming.rooms:{_SCOPE_BY_RPC[rpc]}"
        if metadata.get("authorization") != f"Bearer jwt-for-{needed}":
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "scope not permitted")
        if _SCOPE_BY_RPC[rpc] == "moderate" and request.actor_role not in _MODERATOR_ROLES:
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "forbidden: moderator required")
        if rpc in self.fail:
            code, message = self.fail[rpc]
            await context.abort(code, message)

    async def _room(self, request: Any, context: Any) -> dict[str, Any]:
        """The `(community, room)` state, creating the shared test ROOM on first touch.

        Unknown rooms -- including a real room addressed through the WRONG community -- are
        NOT_FOUND, exactly the server's behaviour.
        """
        key = (request.community_id, request.room_id)
        if key not in self.rooms:
            owned = any(rid == ROOM for _, rid in self.rooms)
            if request.room_id == ROOM and not owned:
                self.seed_room(request.community_id)
            else:
                await context.abort(grpc.StatusCode.NOT_FOUND, "room not found")
        return self.rooms[key]

    def _msg(self, community_id: str, room_id: str, state: dict[str, Any]) -> rooms_pb2.Room:
        return rooms_pb2.Room(
            room_id=room_id,
            community_id=community_id,
            label=state["label"],
            max_publishers=16,
            created_at=state["created_at"],
            publisher_ids=list(state["members"]),
            subscriber_count=len(state["members"]),
            locked=state["locked"],
            empty_timeout_secs=state["empty_timeout_secs"],
        )

    @staticmethod
    def _hand(h: dict[str, Any]) -> rooms_pb2.RaisedHand:
        hand = rooms_pb2.RaisedHand(participant_id=h["pid"], raised_at=h["raised_at"])
        if h.get("ack_at") is not None:
            hand.acknowledged_at = h["ack_at"]
            hand.acknowledged_by = h["ack_by"]
        return hand

    async def ListRooms(self, request: Any, context: Any) -> Any:  # noqa: N802
        await self._guard("ListRooms", request, context)
        if not any(r == ROOM for _, r in self.rooms):
            self.seed_room(request.community_id)
        return rooms_pb2.ListRoomsResponse(
            rooms=[
                self._msg(c, r, s) for (c, r), s in self.rooms.items() if c == request.community_id
            ]
        )

    async def GetRoom(self, request: Any, context: Any) -> Any:  # noqa: N802
        await self._guard("GetRoom", request, context)
        state = await self._room(request, context)
        return rooms_pb2.GetRoomResponse(
            room=self._msg(request.community_id, request.room_id, state)
        )

    async def ListParticipants(self, request: Any, context: Any) -> Any:  # noqa: N802
        await self._guard("ListParticipants", request, context)
        state = await self._room(request, context)
        participants = state["members"]
        return rooms_pb2.ListParticipantsResponse(
            room_id=request.room_id,
            locked=state["locked"],
            participants=[
                rooms_pb2.Participant(
                    participant_id=pid,
                    publisher=True,
                    subscriber=True,
                    muted=m["muted"],
                    joined_at=m["joined_at"],
                )
                for pid, m in participants.items()
            ],
        )

    async def CreateRoom(self, request: Any, context: Any) -> Any:  # noqa: N802
        await self._guard("CreateRoom", request, context)
        room_id = ROOM if not any(r == ROOM for _, r in self.rooms) else str(uuid.uuid4())
        self.seed_room(request.community_id, room_id, label=request.label, created_at=self._tick())
        if request.HasField("empty_timeout_secs"):
            self.rooms[(request.community_id, room_id)]["empty_timeout_secs"] = (
                request.empty_timeout_secs
            )
        return rooms_pb2.CreateRoomResponse(
            room=self._msg(
                request.community_id, room_id, self.rooms[(request.community_id, room_id)]
            )
        )

    async def DeleteRoom(self, request: Any, context: Any) -> Any:  # noqa: N802
        await self._guard("DeleteRoom", request, context)
        state = await self._room(request, context)
        msg = self._msg(request.community_id, request.room_id, state)
        del self.rooms[(request.community_id, request.room_id)]
        return rooms_pb2.DeleteRoomResponse(room=msg)

    async def JoinRoom(self, request: Any, context: Any) -> Any:  # noqa: N802
        await self._guard("JoinRoom", request, context)
        state = await self._room(request, context)
        if state["locked"]:
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "forbidden: room is locked")
        state["members"][request.participant_id] = {"muted": False, "joined_at": self._tick()}
        return rooms_pb2.JoinRoomResponse(
            token="room-token",
            token_id=str(uuid.uuid4()),
            participant_id=request.participant_id,
            expires_at=1700000900,
            whip_url=f"https://svc/rtc/rooms/{request.room_id}/whip",
            whep_url=f"https://svc/rtc/rooms/{request.room_id}/whep",
        )

    async def LeaveRoom(self, request: Any, context: Any) -> Any:  # noqa: N802
        await self._guard("LeaveRoom", request, context)
        state = await self._room(request, context)
        state["members"].pop(request.participant_id, None)
        state["hands"] = [h for h in state["hands"] if h["pid"] != request.participant_id]
        return rooms_pb2.LeaveRoomResponse()

    async def KickParticipant(self, request: Any, context: Any) -> Any:  # noqa: N802
        await self._guard("KickParticipant", request, context)
        state = await self._room(request, context)
        state["members"].pop(request.participant_id, None)
        return rooms_pb2.KickParticipantResponse(
            room_id=request.room_id, participant_id=request.participant_id
        )

    async def MuteParticipant(self, request: Any, context: Any) -> Any:  # noqa: N802
        return await self._set_muted("MuteParticipant", request, context, True)

    async def UnmuteParticipant(self, request: Any, context: Any) -> Any:  # noqa: N802
        return await self._set_muted("UnmuteParticipant", request, context, False)

    async def _set_muted(self, rpc: str, request: Any, context: Any, muted: bool) -> Any:
        await self._guard(rpc, request, context)
        state = await self._room(request, context)
        member = state["members"].setdefault(
            request.participant_id, {"muted": False, "joined_at": self._tick()}
        )
        member["muted"] = muted
        reply = rooms_pb2.MuteParticipantResponse if muted else rooms_pb2.UnmuteParticipantResponse
        return reply(room_id=request.room_id, participant_id=request.participant_id, muted=muted)

    async def MuteAll(self, request: Any, context: Any) -> Any:  # noqa: N802
        await self._guard("MuteAll", request, context)
        state = await self._room(request, context)
        victims = [p for p in state["members"] if p != request.actor_id] or [PARTICIPANT] * 2
        for pid in victims:
            if pid in state["members"]:
                state["members"][pid]["muted"] = True
        return rooms_pb2.MuteAllResponse(room_id=request.room_id, muted=len(victims))

    async def LockRoom(self, request: Any, context: Any) -> Any:  # noqa: N802
        await self._guard("LockRoom", request, context)
        (await self._room(request, context))["locked"] = True
        return rooms_pb2.LockRoomResponse(room_id=request.room_id, locked=True)

    async def UnlockRoom(self, request: Any, context: Any) -> Any:  # noqa: N802
        await self._guard("UnlockRoom", request, context)
        (await self._room(request, context))["locked"] = False
        return rooms_pb2.UnlockRoomResponse(room_id=request.room_id, locked=False)

    async def ListHands(self, request: Any, context: Any) -> Any:  # noqa: N802
        await self._guard("ListHands", request, context)
        state = await self._room(request, context)
        return rooms_pb2.ListHandsResponse(
            room_id=request.room_id, hands=[self._hand(h) for h in state["hands"]]
        )

    async def RaiseHand(self, request: Any, context: Any) -> Any:  # noqa: N802
        await self._guard("RaiseHand", request, context)
        state = await self._room(request, context)
        existing = next((h for h in state["hands"] if h["pid"] == request.participant_id), None)
        if existing is None:
            existing = {"pid": request.participant_id, "raised_at": self._tick()}
            state["hands"].append(existing)
        return rooms_pb2.RaiseHandResponse(hand=self._hand(existing))

    async def LowerHand(self, request: Any, context: Any) -> Any:  # noqa: N802
        await self._guard("LowerHand", request, context)
        state = await self._room(request, context)
        if request.actor_id != request.participant_id and (
            request.actor_role not in _MODERATOR_ROLES
        ):
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "forbidden: moderator required")
        before = len(state["hands"])
        state["hands"] = [h for h in state["hands"] if h["pid"] != request.participant_id]
        return rooms_pb2.LowerHandResponse(
            room_id=request.room_id,
            participant_id=request.participant_id,
            lowered=len(state["hands"]) < before,
        )

    async def ResetHands(self, request: Any, context: Any) -> Any:  # noqa: N802
        await self._guard("ResetHands", request, context)
        state = await self._room(request, context)
        cleared = len(state["hands"])
        state["hands"] = []
        return rooms_pb2.ResetHandsResponse(room_id=request.room_id, cleared=cleared)

    async def AcknowledgeHand(self, request: Any, context: Any) -> Any:  # noqa: N802
        await self._guard("AcknowledgeHand", request, context)
        state = await self._room(request, context)
        hand = next((h for h in state["hands"] if h["pid"] == request.participant_id), None)
        if hand is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "hand not raised")
        hand["ack_at"] = self._tick()
        hand["ack_by"] = request.actor_id
        return rooms_pb2.AcknowledgeHandResponse(hand=self._hand(hand))


class Issuer:
    """Stands in for `ServiceJwtIssuer`: records the (service, scope) it was asked to mint."""

    def __init__(self) -> None:
        """Start with nothing issued."""
        self.issued: list[tuple[str, str]] = []

    def issue(self, service_id: str, scope: str, *, ttl_seconds: int = 0) -> str:
        """Record the request and return a token that names its scope."""
        self.issued.append((service_id, scope))
        return f"jwt-for-{scope}"


@pytest_asyncio.fixture
async def fake(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeRooms]:
    """Real `grpc.aio` server + the real `RoomsClient` wired into the blueprint module."""
    servicer = FakeRooms()
    server = grpc.aio.server()
    rooms_pb2_grpc.add_RoomsServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    issuer = Issuer()
    servicer.issuer = issuer  # type: ignore[attr-defined]
    monkeypatch.setattr(
        calls_module,
        "_client",
        RoomsClient(
            issuer_provider=lambda: issuer,  # type: ignore[arg-type,return-value]
            deployment_env=lambda: "alpha",
            channel=channel,
            timeout_seconds=5.0,
        ),
    )
    yield servicer
    await channel.close()
    await server.stop(None)


@pytest.fixture
def app(overlay_db: Any) -> Quart:
    """A standalone Quart app with both calls blueprints over the `overlay_db` fixture."""
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(calls_admin_bp)
    quart_app.register_blueprint(member_voice_bp)
    quart_app.register_blueprint(member_calls_bp)
    quart_app.config["dal"] = overlay_db.dal
    quart_app.config["async_dal"] = overlay_db
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    """The Quart test client."""
    return app.test_client()


@pytest.fixture(autouse=True)
def _feature_enabled_default_on(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Default the `streaming.rtc` two-gate Feature flag ON for every calls test."""
    stub = AsyncMock(return_value=True)
    monkeypatch.setattr(calls_module, "feature_enabled", stub)
    return stub


def user_headers(
    overlay_db: Any,
    *,
    scope: str,
    role: str,
    user_id: int,
    display_name: str | None = None,
    community_id: int | None = None,
) -> tuple[dict[str, str], int]:
    """Seed a user + membership (in `community_id`, or a fresh community) and mint its token."""
    cid = community_id if community_id is not None else seed_community(overlay_db)
    seed_user(overlay_db, user_id, display_name=display_name)
    seed_membership(overlay_db, community_id=cid, user_id=user_id, role=role)
    return {"Authorization": f"Bearer {make_user_token(user_id=user_id, scope=scope)}"}, cid
