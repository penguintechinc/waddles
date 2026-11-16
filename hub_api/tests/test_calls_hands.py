"""Raised-hand routes: member raise/lower, moderator list/acknowledge/reset.

Backed by the in-process `FakeRooms` (`test_calls_support.py`), a real `grpc.aio` server that
mirrors svc-streaming's actor-based hand authorisation: every hand RPC needs an actor who JOINED
the room; the whole queue / acknowledge / reset need the moderate-scope token AND an actor
recorded as room staff, which only a staff join (moderate-scope JoinRoom) establishes. hub-api
authorises moderation by scope + the admin gate (`streaming.calls:admin`), never role names.
"""

from __future__ import annotations

from typing import Any

import pytest

# isort: split
import grpc_internal  # noqa: F401  (puts the generated pb/ tree on sys.path)

# isort: split
from waddles.streaming.internal.v1 import rooms_pb2

from blueprints.v1.calls import SCOPE_ADMIN, SCOPE_WRITE
from tests.conftest import make_user_token, seed_membership
from tests.test_calls_support import (  # noqa: F401  (fixtures are consumed by name)
    ROOM,
    FakeRooms,
    _feature_enabled_default_on,
    app,
    client,
    fake,
    join,
    pid,
    record_room,
    seed_user,
    user_headers,
    user_uuid,
)

MEMBER_ID = 502
ADMIN_ID = 501


def _base(community_id: int) -> tuple[str, str]:
    return (
        f"/api/v1/community/{community_id}/interact/voice/rooms/{ROOM}",
        f"/api/v1/admin/{community_id}/calls/rooms/{ROOM}",
    )


async def _room_with_people(
    client: Any, overlay_db: Any, *, joined: bool = True
) -> tuple[dict[str, str], dict[str, str], int]:
    """A community with an owner (admin + member scopes) and a plain member, both joined."""
    admin, cid = user_headers(
        overlay_db,
        scope=f"{SCOPE_ADMIN} {SCOPE_WRITE}",
        role="community-owner",
        user_id=ADMIN_ID,
        display_name="Ada",
    )
    member, _ = user_headers(
        overlay_db,
        scope=SCOPE_WRITE,
        role="community-member",
        user_id=MEMBER_ID,
        display_name="Bob",
        community_id=cid,
    )
    if joined:
        await join(client, cid, admin)
        await join(client, cid, member)
    return member, admin, cid


def _rpcs(fake: FakeRooms, name: str) -> list[Any]:
    return [req for rpc, req, _ in fake.calls if rpc == name]


class TestStaffRegistration:
    async def test_admin_joins_with_the_moderate_token_and_members_with_write(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        _, _, cid = await _room_with_people(client, overlay_db)
        join_tokens = [m["authorization"] for rpc, _, m in fake.calls if rpc == "JoinRoom"]
        assert [t.rsplit(":", 1)[1] for t in join_tokens] == ["moderate", "write"]
        state = next(iter(fake.rooms.values()))
        assert state["staff"] == {pid(cid, ADMIN_ID)}


class TestMemberHands:
    async def test_raise_hand_acts_as_the_caller_and_names_them(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        member, _, cid = await _room_with_people(client, overlay_db)
        member_base, _ = _base(cid)
        response = await client.post(
            f"{member_base}/raise-hand",
            headers=member,
            json={"participant_id": "00000000-0000-4000-8000-000000000000"},  # ignored
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["success"] is True
        assert body["hand"]["participant_id"] == pid(cid, MEMBER_ID)
        assert body["hand"]["display_name"] == "Bob"
        assert body["hand"]["acknowledged_at"] is None
        rpc, request, metadata = fake.calls[-1]
        assert rpc == "RaiseHand"
        assert (request.api_version, request.community_id) == ("v1", str(cid))
        # the participant is the caller and the actor is the caller: nobody raises for others
        assert request.participant_id == request.actor_id == pid(cid, MEMBER_ID)
        assert metadata["authorization"].endswith("streaming.rooms:write")

    async def test_raising_without_having_joined_is_refused_not_masked(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        member, _, cid = await _room_with_people(client, overlay_db, joined=False)
        fake.seed_room(cid)
        member_base, _ = _base(cid)
        response = await client.post(f"{member_base}/raise-hand", headers=member)
        assert response.status_code == 403
        assert (await response.get_json())["error"]["code"] == "FORBIDDEN"

    async def test_raising_twice_keeps_the_original_position(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        member, _, cid = await _room_with_people(client, overlay_db)
        member_base, _ = _base(cid)
        first = await (await client.post(f"{member_base}/raise-hand", headers=member)).get_json()
        second = await (await client.post(f"{member_base}/raise-hand", headers=member)).get_json()
        assert first["hand"]["raised_at"] == second["hand"]["raised_at"]

    async def test_lower_hand_is_a_noop_when_nothing_is_raised(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        member, _, cid = await _room_with_people(client, overlay_db)
        member_base, _ = _base(cid)
        await client.post(f"{member_base}/raise-hand", headers=member)
        first = await (await client.post(f"{member_base}/lower-hand", headers=member)).get_json()
        again = await (await client.post(f"{member_base}/lower-hand", headers=member)).get_json()
        assert (first["lowered"], again["lowered"]) == (True, False)
        request = _rpcs(fake, "LowerHand")[0]
        assert request.actor_id == request.participant_id == pid(cid, MEMBER_ID)
        assert request.actor_role == rooms_pb2.ROOM_ROLE_MEMBER

    async def test_leaving_clears_the_hand(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        member, admin, cid = await _room_with_people(client, overlay_db)
        member_base, admin_base = _base(cid)
        await client.post(f"{member_base}/raise-hand", headers=member)
        await client.post(f"{member_base}/leave", headers=member)
        body = await (await client.get(f"{admin_base}/raised-hands", headers=admin)).get_json()
        assert (body["count"], body["raised_hands"]) == (0, [])


class TestModeratorHands:
    async def test_list_is_oldest_first_with_names_and_guest_for_unknown(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        member, admin, cid = await _room_with_people(client, overlay_db)
        member_base, admin_base = _base(cid)
        # A participant hub-api cannot resolve (e.g. a guest token holder) renders as "guest".
        stranger = "7b1b2f0e-6d3a-4c57-9a52-1f3a1d9b0e11"
        await client.post(f"{member_base}/raise-hand", headers=member)
        fake.rooms[(str(cid), ROOM)]["hands"].append({"pid": stranger, "raised_at": 1700009999})
        response = await client.get(f"{admin_base}/raised-hands", headers=admin)
        assert response.status_code == 200
        body = await response.get_json()
        assert body["count"] == 2
        assert [(h["participant_id"], h["display_name"]) for h in body["raised_hands"]] == [
            (pid(cid, MEMBER_ID), "Bob"),
            (stranger, "guest"),
        ]
        rpc, request, metadata = fake.calls[-1]
        assert rpc == "ListHands"
        assert request.actor_id == pid(cid, ADMIN_ID)
        assert metadata["authorization"].endswith("streaming.rooms:moderate")

    async def test_listing_needs_the_admin_to_have_joined_as_staff(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        """The whole queue is staff-only server-side; an admin who never joined is refused."""
        _, admin, cid = await _room_with_people(client, overlay_db, joined=False)
        fake.seed_room(cid)
        _, admin_base = _base(cid)
        response = await client.get(f"{admin_base}/raised-hands", headers=admin)
        assert response.status_code == 403
        assert (await response.get_json())["error"]["code"] == "FORBIDDEN"

    async def test_acknowledge_marks_the_hand_and_acts_as_admin(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        member, admin, cid = await _room_with_people(client, overlay_db)
        member_base, admin_base = _base(cid)
        await client.post(f"{member_base}/raise-hand", headers=member)
        response = await client.post(
            f"{admin_base}/acknowledge-hand",
            headers=admin,
            json={"user_id": pid(cid, MEMBER_ID)},
        )
        assert response.status_code == 200
        hand = (await response.get_json())["hand"]
        assert hand["participant_id"] == pid(cid, MEMBER_ID)
        assert hand["acknowledged_by"] == pid(cid, ADMIN_ID)
        assert hand["acknowledged_at"] is not None
        rpc, request, metadata = fake.calls[-1]
        assert rpc == "AcknowledgeHand"
        assert request.actor_id == pid(cid, ADMIN_ID)
        assert request.actor_role == rooms_pb2.ROOM_ROLE_ADMIN
        assert metadata["authorization"].endswith("streaming.rooms:moderate")

    async def test_acknowledging_a_hand_that_is_not_raised_is_a_404(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        _, admin, cid = await _room_with_people(client, overlay_db)
        _, admin_base = _base(cid)
        response = await client.post(
            f"{admin_base}/acknowledge-hand", headers=admin, json={"user_id": pid(cid, MEMBER_ID)}
        )
        assert response.status_code == 404
        assert (await response.get_json())["error"]["code"] == "NOT_FOUND"

    async def test_reset_clears_every_hand_and_reports_the_count(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        member, admin, cid = await _room_with_people(client, overlay_db)
        member_base, admin_base = _base(cid)
        await client.post(f"{member_base}/raise-hand", headers=member)
        response = await client.post(f"{admin_base}/reset-hands", headers=admin)
        assert response.status_code == 200
        assert (await response.get_json())["cleared"] == 1
        rpc, request, metadata = fake.calls[-1]
        assert rpc == "ResetHands"
        assert request.actor_role == rooms_pb2.ROOM_ROLE_ADMIN
        assert metadata["authorization"].endswith("streaming.rooms:moderate")
        listed = await (await client.get(f"{admin_base}/raised-hands", headers=admin)).get_json()
        assert listed["count"] == 0

    @pytest.mark.parametrize(
        "method,suffix,body",
        [
            ("GET", "raised-hands", None),
            ("POST", "acknowledge-hand", {"user_id": "5d1f2e3a-1111-4a2b-8c3d-9e8f7a6b5c4d"}),
            ("POST", "reset-hands", None),
        ],
    )
    async def test_moderation_needs_the_admin_scope_not_just_membership(
        self, client: Any, overlay_db: Any, fake: FakeRooms, method: str, suffix: str, body: Any
    ) -> None:
        """A token with only the member-level scope is refused before any RPC."""
        member, _, cid = await _room_with_people(client, overlay_db, joined=False)
        _, admin_base = _base(cid)
        response = await client.open(
            f"{admin_base}/{suffix}", method=method, headers=member, json=body
        )
        assert response.status_code == 403
        assert fake.calls == []

    async def test_acknowledge_needs_a_participant_uuid_not_a_name(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        _, admin, cid = await _room_with_people(client, overlay_db, joined=False)
        _, admin_base = _base(cid)
        response = await client.post(
            f"{admin_base}/acknowledge-hand", headers=admin, json={"user_id": "Bob"}
        )
        assert response.status_code == 400
        assert (await response.get_json())["error"]["code"] == "INVALID_IDENTITY"
        assert fake.calls == []

    async def test_unknown_room_is_404_not_an_empty_queue(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        _, admin, cid = await _room_with_people(client, overlay_db, joined=False)
        fake.seed_room(999)  # ROOM now exists, but only in ANOTHER community
        response = await client.get(
            f"/api/v1/admin/{cid}/calls/rooms/{ROOM}/raised-hands", headers=admin
        )
        assert response.status_code == 404

    async def test_plain_member_cannot_reach_moderation_even_with_admin_scope(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        _, admin, cid = await _room_with_people(client, overlay_db, joined=False)
        seed_user(overlay_db, 503)
        seed_membership(overlay_db, community_id=cid, user_id=503, role="community-member")
        token = make_user_token(user_id=503, scope=SCOPE_ADMIN)
        _, admin_base = _base(cid)
        for method, suffix in (("GET", "raised-hands"), ("POST", "reset-hands")):
            response = await client.open(
                f"{admin_base}/{suffix}",
                method=method,
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 403
        assert fake.calls == []
