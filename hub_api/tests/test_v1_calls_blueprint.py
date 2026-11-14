"""`blueprints/v1/calls.py` -- the M7 calls group (Streaming module).

Standalone Quart app registering both `calls_admin_bp` and `member_voice_bp` against the
`overlay_db` fixture (`tests/conftest.py`) -- real JWTs, real pydal membership rows. The
downstream is a REAL in-process `grpc.aio` server implementing `RoomsService` (`FakeRooms`):
the real `RoomsClient`, protobuf marshalling, per-call JWT metadata and status-code mapping
all run; only the room logic behind the server is scripted.

Fail-first proof (executed, not narrated), the token-takeover fix: temporarily commenting out
the `await community_access.require_community_member(...)` call inside
`blueprints/v1/calls.py::_require_member` turned every `test_member_route_non_member_is_403[...]`
red (200 instead of 403) -- an authenticated non-member would have received a live room join
token for ANY `community_id`. The same cycle against `_require_admin` turned every
`test_admin_route_non_member_is_403[...]` / `..._non_admin_member_is_403[...]` red. Reverted.
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
from blueprints.v1.calls import (
    SCOPE_ADMIN,
    SCOPE_READ,
    SCOPE_WRITE,
    calls_admin_bp,
    member_voice_bp,
)
from services.calls_proxy import RoomsClient
from tests.conftest import (
    make_super_admin_token,
    make_user_token,
    seed_community,
    seed_membership,
    seed_super_admin,
)

ROOM = "0b6f3e1a-8f5e-4d0a-9a43-6a1d5f7c2e10"
PARTICIPANT = "5d1f2e3a-1111-4a2b-8c3d-9e8f7a6b5c4d"


class FakeRooms(rooms_pb2_grpc.RoomsServiceServicer):
    """Scripted `RoomsService`: records (rpc, request, metadata); optional per-rpc failure."""

    def __init__(self) -> None:
        """Start with no recorded calls and no scripted failures."""
        self.calls: list[tuple[str, Any, dict[str, str]]] = []
        self.fail: dict[str, tuple[grpc.StatusCode, str]] = {}

    async def _handle(self, rpc: str, request: Any, context: Any, reply: Any) -> Any:
        self.calls.append((rpc, request, dict(context.invocation_metadata())))
        if rpc in self.fail:
            code, message = self.fail[rpc]
            await context.abort(code, message)
        return reply

    def _room(self, community_id: str) -> rooms_pb2.Room:
        return rooms_pb2.Room(
            room_id=ROOM,
            community_id=community_id,
            label="room1",
            max_publishers=16,
            created_at=1700000000,
            locked=False,
        )

    async def ListRooms(self, request: Any, context: Any) -> Any:  # noqa: N802
        return await self._handle(
            "ListRooms",
            request,
            context,
            rooms_pb2.ListRoomsResponse(rooms=[self._room(request.community_id)]),
        )

    async def GetRoom(self, request: Any, context: Any) -> Any:  # noqa: N802
        return await self._handle(
            "GetRoom",
            request,
            context,
            rooms_pb2.GetRoomResponse(room=self._room(request.community_id)),
        )

    async def ListParticipants(self, request: Any, context: Any) -> Any:  # noqa: N802
        return await self._handle(
            "ListParticipants",
            request,
            context,
            rooms_pb2.ListParticipantsResponse(
                room_id=request.room_id,
                participants=[rooms_pb2.Participant(participant_id=PARTICIPANT, publisher=True)],
            ),
        )

    async def CreateRoom(self, request: Any, context: Any) -> Any:  # noqa: N802
        return await self._handle(
            "CreateRoom",
            request,
            context,
            rooms_pb2.CreateRoomResponse(room=self._room(request.community_id)),
        )

    async def DeleteRoom(self, request: Any, context: Any) -> Any:  # noqa: N802
        return await self._handle(
            "DeleteRoom",
            request,
            context,
            rooms_pb2.DeleteRoomResponse(room=self._room(request.community_id)),
        )

    async def JoinRoom(self, request: Any, context: Any) -> Any:  # noqa: N802
        return await self._handle(
            "JoinRoom",
            request,
            context,
            rooms_pb2.JoinRoomResponse(
                token="room-token",
                token_id=str(uuid.uuid4()),
                participant_id=request.participant_id,
                expires_at=1700000900,
                whip_url=f"https://svc/rtc/rooms/{request.room_id}/whip",
                whep_url=f"https://svc/rtc/rooms/{request.room_id}/whep",
            ),
        )

    async def LeaveRoom(self, request: Any, context: Any) -> Any:  # noqa: N802
        return await self._handle("LeaveRoom", request, context, rooms_pb2.LeaveRoomResponse())

    async def KickParticipant(self, request: Any, context: Any) -> Any:  # noqa: N802
        return await self._handle(
            "KickParticipant", request, context, rooms_pb2.KickParticipantResponse()
        )

    async def MuteAll(self, request: Any, context: Any) -> Any:  # noqa: N802
        return await self._handle("MuteAll", request, context, rooms_pb2.MuteAllResponse(muted=2))

    async def LockRoom(self, request: Any, context: Any) -> Any:  # noqa: N802
        return await self._handle(
            "LockRoom", request, context, rooms_pb2.LockRoomResponse(locked=True)
        )

    async def UnlockRoom(self, request: Any, context: Any) -> Any:  # noqa: N802
        return await self._handle("UnlockRoom", request, context, rooms_pb2.UnlockRoomResponse())


class _Issuer:
    """Stands in for `ServiceJwtIssuer`: records the (service, scope) it was asked to mint."""

    def __init__(self) -> None:
        self.issued: list[tuple[str, str]] = []

    def issue(self, service_id: str, scope: str, *, ttl_seconds: int = 0) -> str:
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
    issuer = _Issuer()
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
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(calls_admin_bp)
    quart_app.register_blueprint(member_voice_bp)
    quart_app.config["dal"] = overlay_db.dal
    quart_app.config["async_dal"] = overlay_db
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


@pytest.fixture(autouse=True)
def _feature_enabled_default_on(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Default the `streaming.rtc` two-gate Feature flag ON for every test in this file."""
    stub = AsyncMock(return_value=True)
    monkeypatch.setattr(calls_module, "feature_enabled", stub)
    return stub


def _owner(overlay_db: Any, *, scope: str) -> tuple[dict[str, str], int]:
    community_id = seed_community(overlay_db)
    user_id = 701
    seed_membership(overlay_db, community_id=community_id, user_id=user_id, role="community-owner")
    token = make_user_token(user_id=user_id, scope=scope)
    return {"Authorization": f"Bearer {token}"}, community_id


def _plain_member(overlay_db: Any, *, scope: str) -> tuple[dict[str, str], int]:
    community_id = seed_community(overlay_db)
    user_id = 702
    seed_membership(overlay_db, community_id=community_id, user_id=user_id, role="community-member")
    token = make_user_token(user_id=user_id, scope=scope)
    return {"Authorization": f"Bearer {token}"}, community_id


#: (method, path_fn(cid), json_body) -- every admin route.
_ADMIN_ROUTES: tuple[tuple[str, Any, dict[str, Any] | None], ...] = (
    ("GET", lambda cid: f"/api/v1/admin/{cid}/calls/rooms", None),
    ("POST", lambda cid: f"/api/v1/admin/{cid}/calls/rooms", {"room_name": "room1"}),
    ("GET", lambda cid: f"/api/v1/admin/{cid}/calls/rooms/{ROOM}", None),
    ("DELETE", lambda cid: f"/api/v1/admin/{cid}/calls/rooms/{ROOM}", None),
    ("POST", lambda cid: f"/api/v1/admin/{cid}/calls/rooms/{ROOM}/lock", None),
    ("POST", lambda cid: f"/api/v1/admin/{cid}/calls/rooms/{ROOM}/unlock", None),
    ("GET", lambda cid: f"/api/v1/admin/{cid}/calls/rooms/{ROOM}/participants", None),
    (
        "POST",
        lambda cid: f"/api/v1/admin/{cid}/calls/rooms/{ROOM}/kick",
        {"identity": PARTICIPANT},
    ),
    ("POST", lambda cid: f"/api/v1/admin/{cid}/calls/rooms/{ROOM}/mute-all", None),
    ("GET", lambda cid: f"/api/v1/admin/{cid}/calls/rooms/{ROOM}/raised-hands", None),
    (
        "POST",
        lambda cid: f"/api/v1/admin/{cid}/calls/rooms/{ROOM}/acknowledge-hand",
        {"user_id": "u1"},
    ),
)

#: (method, path_fn(cid), json_body) -- every member-facing route.
_MEMBER_ROUTES: tuple[tuple[str, Any, dict[str, Any] | None], ...] = (
    ("GET", lambda cid: f"/api/v1/community/{cid}/interact/voice/rooms", None),
    ("POST", lambda cid: f"/api/v1/community/{cid}/interact/voice/rooms", {"room_name": "room1"}),
    ("POST", lambda cid: f"/api/v1/community/{cid}/interact/voice/rooms/{ROOM}/join", None),
    ("POST", lambda cid: f"/api/v1/community/{cid}/interact/voice/rooms/{ROOM}/leave", None),
)


class TestAdminRouteAuth:
    @pytest.mark.parametrize("method,path_fn,body", _ADMIN_ROUTES)
    async def test_admin_route_without_token_is_401(
        self, client: Any, method: str, path_fn: Any, body: dict[str, Any] | None
    ) -> None:
        response = await client.open(path_fn(1), method=method, json=body)
        assert response.status_code == 401

    @pytest.mark.parametrize("method,path_fn,body", _ADMIN_ROUTES)
    async def test_admin_route_non_admin_member_is_403(
        self,
        client: Any,
        overlay_db: Any,
        fake: FakeRooms,
        method: str,
        path_fn: Any,
        body: dict[str, Any] | None,
    ) -> None:
        """A real, active membership with role='community-member' is still not an admin."""
        headers, community_id = _plain_member(overlay_db, scope=SCOPE_ADMIN)
        response = await client.open(
            path_fn(community_id), method=method, headers=headers, json=body
        )
        assert response.status_code == 403
        assert fake.calls == []

    @pytest.mark.parametrize("method,path_fn,body", _ADMIN_ROUTES)
    async def test_admin_route_non_member_is_403(
        self,
        client: Any,
        overlay_db: Any,
        fake: FakeRooms,
        method: str,
        path_fn: Any,
        body: dict[str, Any] | None,
    ) -> None:
        community_id = seed_community(overlay_db)
        token = make_user_token(user_id=999, scope=SCOPE_ADMIN)
        response = await client.open(
            path_fn(community_id),
            method=method,
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )
        assert response.status_code == 403
        assert fake.calls == []


class TestAdminRoutes:
    async def test_get_call_rooms_maps_rooms_and_sends_community_from_path_auth(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        response = await client.get(f"/api/v1/admin/{community_id}/calls/rooms", headers=headers)
        assert response.status_code == 200
        body = await response.get_json()
        assert body["success"] is True
        assert body["rooms"][0]["room_id"] == ROOM
        assert body["rooms"][0]["label"] == "room1"
        rpc, request, metadata = fake.calls[0]
        assert rpc == "ListRooms"
        assert request.community_id == str(community_id)
        assert metadata["authorization"] == "Bearer jwt-for-streaming.rooms:read"

    async def test_feature_flag_off_is_402_and_no_rpc(
        self,
        client: Any,
        overlay_db: Any,
        fake: FakeRooms,
        _feature_enabled_default_on: AsyncMock,
    ) -> None:
        _feature_enabled_default_on.return_value = False
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        response = await client.get(f"/api/v1/admin/{community_id}/calls/rooms", headers=headers)
        assert response.status_code == 402
        assert fake.calls == []

    async def test_create_call_room_stores_name_as_label(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        response = await client.post(
            f"/api/v1/admin/{community_id}/calls/rooms",
            headers=headers,
            json={"room_name": "room1", "max_participants": 8},
        )
        assert response.status_code == 201
        assert (await response.get_json())["room_id"] == ROOM
        _, request, metadata = fake.calls[0]
        assert request.label == "room1"
        assert request.max_publishers == 8
        assert metadata["authorization"].endswith("streaming.rooms:write")

    async def test_downstream_error_is_relayed_with_status(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        fake.fail["CreateRoom"] = (grpc.StatusCode.RESOURCE_EXHAUSTED, "limit reached: 100 rooms")
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        response = await client.post(
            f"/api/v1/admin/{community_id}/calls/rooms",
            headers=headers,
            json={"room_name": "room1"},
        )
        assert response.status_code == 429
        assert await response.get_json() == {"success": False, "error": "limit reached: 100 rooms"}

    @pytest.mark.parametrize(
        "code,http",
        [
            (grpc.StatusCode.NOT_FOUND, 404),
            (grpc.StatusCode.INVALID_ARGUMENT, 400),
            (grpc.StatusCode.PERMISSION_DENIED, 403),
            (grpc.StatusCode.UNIMPLEMENTED, 501),
        ],
    )
    async def test_status_mapping(
        self, client: Any, overlay_db: Any, fake: FakeRooms, code: grpc.StatusCode, http: int
    ) -> None:
        fake.fail["GetRoom"] = (code, "x")
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        response = await client.get(
            f"/api/v1/admin/{community_id}/calls/rooms/{ROOM}", headers=headers
        )
        assert response.status_code == http

    async def test_unavailable_is_masked_502(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        fake.fail["GetRoom"] = (grpc.StatusCode.UNAVAILABLE, "secret internal detail")
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        response = await client.get(
            f"/api/v1/admin/{community_id}/calls/rooms/{ROOM}", headers=headers
        )
        assert response.status_code == 502
        assert "secret" not in (await response.get_json())["error"]

    async def test_room_name_must_be_a_uuid(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        response = await client.get(
            f"/api/v1/admin/{community_id}/calls/rooms/not-a-uuid", headers=headers
        )
        assert response.status_code == 400
        assert fake.calls == []

    async def test_get_and_delete_room(self, client: Any, overlay_db: Any, fake: FakeRooms) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        got = await client.get(f"/api/v1/admin/{community_id}/calls/rooms/{ROOM}", headers=headers)
        assert (await got.get_json())["room_id"] == ROOM
        deleted = await client.delete(
            f"/api/v1/admin/{community_id}/calls/rooms/{ROOM}", headers=headers
        )
        assert (await deleted.get_json())["message"] == "Room deleted"
        assert [c[0] for c in fake.calls] == ["GetRoom", "DeleteRoom"]

    @pytest.mark.parametrize(
        "action,rpc,locked", [("lock", "LockRoom", True), ("unlock", "UnlockRoom", False)]
    )
    async def test_lock_unlock_act_as_admin_with_moderate_scope(
        self, client: Any, overlay_db: Any, fake: FakeRooms, action: str, rpc: str, locked: bool
    ) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        response = await client.post(
            f"/api/v1/admin/{community_id}/calls/rooms/{ROOM}/{action}", headers=headers
        )
        assert response.status_code == 200
        name, request, metadata = fake.calls[0]
        assert name == rpc
        assert request.actor_role == rooms_pb2.ROOM_ROLE_ADMIN
        uuid.UUID(request.actor_id)  # a pseudonymous UUID, never a raw user id
        assert metadata["authorization"].endswith("streaming.rooms:moderate")

    async def test_participants_kick_and_mute_all(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        base = f"/api/v1/admin/{community_id}/calls/rooms/{ROOM}"
        parts = await client.get(f"{base}/participants", headers=headers)
        assert (await parts.get_json())["participants"][0]["participant_id"] == PARTICIPANT
        kick = await client.post(f"{base}/kick", headers=headers, json={"identity": PARTICIPANT})
        assert kick.status_code == 200
        mute = await client.post(f"{base}/mute-all", headers=headers)
        assert mute.status_code == 200
        assert [c[0] for c in fake.calls] == ["ListParticipants", "KickParticipant", "MuteAll"]
        assert fake.calls[1][1].participant_id == PARTICIPANT

    async def test_kick_identity_must_be_a_uuid(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        response = await client.post(
            f"/api/v1/admin/{community_id}/calls/rooms/{ROOM}/kick",
            headers=headers,
            json={"identity": "some-username"},
        )
        assert response.status_code == 400
        assert fake.calls == []

    @pytest.mark.parametrize(
        "method,suffix,body",
        [("GET", "raised-hands", None), ("POST", "acknowledge-hand", {"user_id": "u1"})],
    )
    async def test_hand_raise_routes_are_explicit_501(
        self, client: Any, overlay_db: Any, fake: FakeRooms, method: str, suffix: str, body: Any
    ) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        response = await client.open(
            f"/api/v1/admin/{community_id}/calls/rooms/{ROOM}/{suffix}",
            method=method,
            headers=headers,
            json=body,
        )
        assert response.status_code == 501
        assert (await response.get_json())["success"] is False
        assert fake.calls == []


class TestMemberVoiceRoutes:
    """The token-takeover fix -- Node applies `requireAuth` only; this port adds membership."""

    @pytest.mark.parametrize("method,path_fn,body", _MEMBER_ROUTES)
    async def test_member_route_without_token_is_401(
        self, client: Any, method: str, path_fn: Any, body: dict[str, Any] | None
    ) -> None:
        response = await client.open(path_fn(1), method=method, json=body)
        assert response.status_code == 401

    @pytest.mark.parametrize("method,path_fn,body", _MEMBER_ROUTES)
    async def test_member_route_non_member_is_403(
        self,
        client: Any,
        overlay_db: Any,
        fake: FakeRooms,
        method: str,
        path_fn: Any,
        body: dict[str, Any] | None,
    ) -> None:
        """Authenticated, correct scope, but NO membership row -- the join-token takeover fix."""
        community_id = seed_community(overlay_db)
        token = make_user_token(user_id=888, scope=f"{SCOPE_READ} {SCOPE_WRITE}")
        response = await client.open(
            path_fn(community_id),
            method=method,
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )
        assert response.status_code == 403
        assert fake.calls == []

    async def test_list_and_create(self, client: Any, overlay_db: Any, fake: FakeRooms) -> None:
        headers, community_id = _plain_member(overlay_db, scope=f"{SCOPE_READ} {SCOPE_WRITE}")
        base = f"/api/v1/community/{community_id}/interact/voice/rooms"
        listed = await client.get(base, headers=headers)
        assert (await listed.get_json())["rooms"][0]["room_id"] == ROOM
        created = await client.post(base, headers=headers, json={"room_name": "chat"})
        assert created.status_code == 201
        assert [c[0] for c in fake.calls] == ["ListRooms", "CreateRoom"]

    async def test_join_mints_token_for_jwt_user_not_request_body(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        """The participant UUID derives from the verified JWT user, never a client-supplied one."""
        community_id = seed_community(overlay_db)
        seed_membership(overlay_db, community_id=community_id, user_id=42, role="community-member")
        token = make_user_token(user_id=42, scope=SCOPE_WRITE)
        response = await client.post(
            f"/api/v1/community/{community_id}/interact/voice/rooms/{ROOM}/join",
            headers={"Authorization": f"Bearer {token}"},
            json={"user_id": "9999-attacker-supplied", "participant_id": PARTICIPANT},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["token"] == "room-token"
        assert body["url"] == f"https://svc/rtc/rooms/{ROOM}"
        _, request, _ = fake.calls[0]
        assert request.participant_id not in {PARTICIPANT, "9999-attacker-supplied", "42"}
        assert request.publish and request.subscribe
        assert request.community_id == str(community_id)

    async def test_join_is_stable_per_user_and_distinct_per_user(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        community_id = seed_community(overlay_db)
        for uid in (42, 43):
            seed_membership(
                overlay_db, community_id=community_id, user_id=uid, role="community-member"
            )
        seen = []
        for uid in (42, 42, 43):
            token = make_user_token(user_id=uid, scope=SCOPE_WRITE)
            await client.post(
                f"/api/v1/community/{community_id}/interact/voice/rooms/{ROOM}/join",
                headers={"Authorization": f"Bearer {token}"},
            )
            seen.append(fake.calls[-1][1].participant_id)
        assert seen[0] == seen[1] != seen[2]

    async def test_join_downstream_error(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        fake.fail["JoinRoom"] = (grpc.StatusCode.PERMISSION_DENIED, "forbidden: room is locked")
        headers, community_id = _plain_member(overlay_db, scope=SCOPE_WRITE)
        response = await client.post(
            f"/api/v1/community/{community_id}/interact/voice/rooms/{ROOM}/join", headers=headers
        )
        assert response.status_code == 403
        assert (await response.get_json())["error"] == "forbidden: room is locked"

    async def test_leave_acts_as_self_with_member_role(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        headers, community_id = _plain_member(overlay_db, scope=SCOPE_WRITE)
        response = await client.post(
            f"/api/v1/community/{community_id}/interact/voice/rooms/{ROOM}/leave", headers=headers
        )
        assert response.status_code == 200
        _, request, _ = fake.calls[0]
        assert request.actor_id == request.participant_id
        assert request.actor_role == rooms_pb2.ROOM_ROLE_MEMBER


class TestSuperAdminBypass:
    async def test_admin_route_super_admin_bypasses_membership(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        community_id = seed_community(overlay_db)  # no membership row at all
        seed_super_admin(overlay_db, user_id=1)
        token = make_super_admin_token(user_id=1, scope=SCOPE_ADMIN)
        response = await client.get(
            f"/api/v1/admin/{community_id}/calls/rooms",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200

    async def test_member_route_super_admin_bypasses_membership(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        community_id = seed_community(overlay_db)  # no membership row at all
        seed_super_admin(overlay_db, user_id=1)
        token = make_super_admin_token(user_id=1, scope=SCOPE_READ)
        response = await client.get(
            f"/api/v1/community/{community_id}/interact/voice/rooms",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
