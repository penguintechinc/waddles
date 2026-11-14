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
from typing import Any
from unittest.mock import AsyncMock

import grpc
import pytest

# isort: split
import grpc_internal  # noqa: F401  (puts the generated pb/ tree on sys.path)

# isort: split
from waddles.streaming.internal.v1 import rooms_pb2

from blueprints.v1.calls import SCOPE_ADMIN, SCOPE_READ, SCOPE_WRITE
from tests.conftest import (
    TENANT_SLUG,
    make_super_admin_token,
    make_user_token,
    seed_community,
    seed_membership,
    seed_super_admin,
)
from tests.test_calls_support import (  # noqa: F401  (fixtures are consumed by name)
    PARTICIPANT,
    ROOM,
    FakeRooms,
    _feature_enabled_default_on,
    app,
    client,
    fake,
    seed_user,
    user_headers,
    user_uuid,
)


def _owner(overlay_db: Any, *, scope: str) -> tuple[dict[str, str], int]:
    return user_headers(overlay_db, scope=scope, role="community-owner", user_id=701)


def _plain_member(overlay_db: Any, *, scope: str) -> tuple[dict[str, str], int]:
    return user_headers(overlay_db, scope=scope, role="community-member", user_id=702)


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
    (
        "POST",
        lambda cid: f"/api/v1/admin/{cid}/calls/rooms/{ROOM}/mute",
        {"identity": PARTICIPANT},
    ),
    (
        "POST",
        lambda cid: f"/api/v1/admin/{cid}/calls/rooms/{ROOM}/unmute",
        {"identity": PARTICIPANT},
    ),
    ("POST", lambda cid: f"/api/v1/admin/{cid}/calls/rooms/{ROOM}/mute-all", None),
    ("POST", lambda cid: f"/api/v1/admin/{cid}/calls/rooms/{ROOM}/reset-hands", None),
    ("GET", lambda cid: f"/api/v1/admin/{cid}/calls/rooms/{ROOM}/raised-hands", None),
    (
        "POST",
        lambda cid: f"/api/v1/admin/{cid}/calls/rooms/{ROOM}/acknowledge-hand",
        {"user_id": PARTICIPANT},
    ),
)

#: (method, path_fn(cid), json_body) -- every member-facing route.
_MEMBER_ROUTES: tuple[tuple[str, Any, dict[str, Any] | None], ...] = (
    ("GET", lambda cid: f"/api/v1/community/{cid}/interact/voice/rooms", None),
    ("POST", lambda cid: f"/api/v1/community/{cid}/interact/voice/rooms", {"room_name": "room1"}),
    ("POST", lambda cid: f"/api/v1/community/{cid}/interact/voice/rooms/{ROOM}/join", None),
    ("POST", lambda cid: f"/api/v1/community/{cid}/interact/voice/rooms/{ROOM}/leave", None),
    ("POST", lambda cid: f"/api/v1/community/{cid}/interact/voice/rooms/{ROOM}/raise-hand", None),
    ("POST", lambda cid: f"/api/v1/community/{cid}/interact/voice/rooms/{ROOM}/lower-hand", None),
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
        error = (await response.get_json())["error"]
        assert (error["code"], error["message"]) == (
            "RESOURCE_EXHAUSTED",
            "limit reached: 100 rooms",
        )

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

    async def test_unavailable_is_a_structured_503_without_internal_detail(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        fake.fail["GetRoom"] = (grpc.StatusCode.UNAVAILABLE, "secret internal detail")
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        response = await client.get(
            f"/api/v1/admin/{community_id}/calls/rooms/{ROOM}", headers=headers
        )
        assert response.status_code == 503
        body = await response.get_json()
        assert body["success"] is False
        assert body["error"]["code"] == "STREAMING_UNAVAILABLE"
        assert body["error"]["details"] == {"retryable": True}
        assert "secret" not in str(body)

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
        fake.seed_room(community_id)
        fake.seed_participant(community_id, PARTICIPANT)
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
        "action,rpc,muted",
        [("mute", "MuteParticipant", True), ("unmute", "UnmuteParticipant", False)],
    )
    async def test_mute_and_unmute_one_participant(
        self, client: Any, overlay_db: Any, fake: FakeRooms, action: str, rpc: str, muted: bool
    ) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        response = await client.post(
            f"/api/v1/admin/{community_id}/calls/rooms/{ROOM}/{action}",
            headers=headers,
            json={"identity": PARTICIPANT},
        )
        assert response.status_code == 200
        assert await response.get_json() == {
            "participant_id": PARTICIPANT,
            "muted": muted,
            "success": True,
        }
        name, request, metadata = fake.calls[0]
        assert name == rpc
        assert request.participant_id == PARTICIPANT
        assert request.actor_role == rooms_pb2.ROOM_ROLE_ADMIN
        assert metadata["authorization"].endswith("streaming.rooms:moderate")

    @pytest.mark.parametrize("action", ["mute", "unmute"])
    async def test_mute_identity_must_be_a_uuid(
        self, client: Any, overlay_db: Any, fake: FakeRooms, action: str
    ) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        response = await client.post(
            f"/api/v1/admin/{community_id}/calls/rooms/{ROOM}/{action}",
            headers=headers,
            json={"identity": "alice"},
        )
        assert response.status_code == 400
        assert fake.calls == []

    async def test_mute_all_reports_the_count(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        response = await client.post(
            f"/api/v1/admin/{community_id}/calls/rooms/{ROOM}/mute-all", headers=headers
        )
        body = await response.get_json()
        assert (body["success"], body["muted"]) == (True, 2)
        assert fake.calls[0][1].actor_id == user_uuid(701)


class TestFeatureFlag:
    """`waddles.streaming.rtc` gates EVERY route; OFF is 402 with the licensed-feature body."""

    @pytest.mark.parametrize("method,path_fn,body", _ADMIN_ROUTES)
    async def test_admin_route_flag_off_is_402_and_no_rpc(
        self,
        client: Any,
        overlay_db: Any,
        fake: FakeRooms,
        _feature_enabled_default_on: AsyncMock,
        method: str,
        path_fn: Any,
        body: dict[str, Any] | None,
    ) -> None:
        _feature_enabled_default_on.return_value = False
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        response = await client.open(
            path_fn(community_id), method=method, headers=headers, json=body
        )
        assert response.status_code == 402
        error = (await response.get_json())["error"]
        assert error["code"] == "FEATURE_NOT_ENABLED"
        assert fake.calls == []

    @pytest.mark.parametrize("method,path_fn,body", _MEMBER_ROUTES)
    async def test_member_route_flag_off_is_402_and_no_rpc(
        self,
        client: Any,
        overlay_db: Any,
        fake: FakeRooms,
        _feature_enabled_default_on: AsyncMock,
        method: str,
        path_fn: Any,
        body: dict[str, Any] | None,
    ) -> None:
        _feature_enabled_default_on.return_value = False
        headers, community_id = _plain_member(overlay_db, scope=f"{SCOPE_READ} {SCOPE_WRITE}")
        response = await client.open(
            path_fn(community_id), method=method, headers=headers, json=body
        )
        assert response.status_code == 402
        assert (await response.get_json())["error"]["code"] == "FEATURE_NOT_ENABLED"
        assert fake.calls == []

    async def test_flag_is_asked_for_the_callers_tenant(
        self,
        client: Any,
        overlay_db: Any,
        fake: FakeRooms,
        _feature_enabled_default_on: AsyncMock,
    ) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        await client.get(f"/api/v1/admin/{community_id}/calls/rooms", headers=headers)
        _feature_enabled_default_on.assert_awaited_once()
        assert _feature_enabled_default_on.await_args.args == ("waddles.streaming.rtc",)
        assert _feature_enabled_default_on.await_args.kwargs == {"tenant": TENANT_SLUG}

    async def test_non_member_gets_403_before_the_plan_is_revealed(
        self,
        client: Any,
        overlay_db: Any,
        fake: FakeRooms,
        _feature_enabled_default_on: AsyncMock,
    ) -> None:
        _feature_enabled_default_on.return_value = False
        community_id = seed_community(overlay_db)
        token = make_user_token(user_id=999, scope=SCOPE_ADMIN)
        response = await client.get(
            f"/api/v1/admin/{community_id}/calls/rooms",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403
        _feature_enabled_default_on.assert_not_awaited()


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
        """The participant UUID is the verified JWT user's `hub_users.uuid`, not the body's."""
        community_id = seed_community(overlay_db)
        seed_user(overlay_db, 42)
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
        request = next(r for name, r, _ in fake.calls if name == "JoinRoom")
        assert request.participant_id == user_uuid(42)
        assert request.participant_id not in {PARTICIPANT, "9999-attacker-supplied", "42"}
        assert request.publish and request.subscribe
        assert request.community_id == str(community_id)

    async def test_join_is_stable_per_user_and_distinct_per_user(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        community_id = seed_community(overlay_db)
        for uid in (42, 43):
            seed_user(overlay_db, uid)
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

    async def test_user_without_identity_record_is_a_loud_401(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        """A valid token whose `hub_users` row is gone never gets a made-up participant id."""
        community_id = seed_community(overlay_db)
        seed_membership(overlay_db, community_id=community_id, user_id=77, role="community-member")
        token = make_user_token(user_id=77, scope=SCOPE_WRITE)
        response = await client.post(
            f"/api/v1/community/{community_id}/interact/voice/rooms/{ROOM}/join",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 401
        assert (await response.get_json())["error"]["code"] == "UNKNOWN_USER"
        assert fake.calls == []

    async def test_join_downstream_error(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        fake.fail["JoinRoom"] = (grpc.StatusCode.PERMISSION_DENIED, "forbidden: room is locked")
        headers, community_id = _plain_member(overlay_db, scope=SCOPE_WRITE)
        response = await client.post(
            f"/api/v1/community/{community_id}/interact/voice/rooms/{ROOM}/join", headers=headers
        )
        assert response.status_code == 403
        assert (await response.get_json())["error"]["message"] == "forbidden: room is locked"

    async def test_leave_acts_as_self_with_member_role(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        headers, community_id = _plain_member(overlay_db, scope=SCOPE_WRITE)
        response = await client.post(
            f"/api/v1/community/{community_id}/interact/voice/rooms/{ROOM}/leave", headers=headers
        )
        assert response.status_code == 200
        _, request, _ = fake.calls[0]
        assert request.actor_id == request.participant_id == user_uuid(702)
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
