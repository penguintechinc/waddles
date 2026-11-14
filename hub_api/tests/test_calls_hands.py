"""Raised-hand routes: member raise/lower, moderator list/acknowledge/reset.

Backed by the in-process `FakeRooms` (`test_calls_support.py`), a real `grpc.aio` server, so
the typed client, protobuf marshalling, scope metadata and status mapping all run. Moderation
authorisation is by scope + the admin gate (`streaming.calls:admin`), never role names.
"""

from __future__ import annotations

from typing import Any

import pytest

# isort: split
import grpc_internal  # noqa: F401  (puts the generated pb/ tree on sys.path)

# isort: split
from waddles.streaming.internal.v1 import rooms_pb2

from blueprints.v1.calls import SCOPE_ADMIN, SCOPE_WRITE
from tests.conftest import seed_membership
from tests.test_calls_support import (  # noqa: F401  (fixtures are consumed by name)
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

MEMBER_ID = 502
ADMIN_ID = 501


def _base(community_id: int) -> tuple[str, str]:
    return (
        f"/api/v1/community/{community_id}/interact/voice/rooms/{ROOM}",
        f"/api/v1/admin/{community_id}/calls/rooms/{ROOM}",
    )


def _member_and_admin(overlay_db: Any) -> tuple[dict[str, str], dict[str, str], int]:
    admin, cid = user_headers(
        overlay_db, scope=SCOPE_ADMIN, role="community-owner", user_id=ADMIN_ID, display_name="Ada"
    )
    member, _ = user_headers(
        overlay_db,
        scope=SCOPE_WRITE,
        role="community-member",
        user_id=MEMBER_ID,
        display_name="Bob",
        community_id=cid,
    )
    return member, admin, cid


class TestMemberHands:
    async def test_raise_hand_acts_as_the_caller_and_names_them(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        member, _, cid = _member_and_admin(overlay_db)
        member_base, _ = _base(cid)
        response = await client.post(
            f"{member_base}/raise-hand",
            headers=member,
            json={"participant_id": "00000000-0000-4000-8000-000000000000"},  # ignored
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["success"] is True
        assert body["hand"]["participant_id"] == user_uuid(MEMBER_ID)
        assert body["hand"]["display_name"] == "Bob"
        assert body["hand"]["acknowledged_at"] is None
        rpc, request, metadata = fake.calls[0]
        assert rpc == "RaiseHand"
        assert (request.api_version, request.community_id) == ("v1", str(cid))
        assert request.participant_id == user_uuid(MEMBER_ID)
        assert metadata["authorization"].endswith("streaming.rooms:write")

    async def test_raising_twice_keeps_the_original_position(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        member, _, cid = _member_and_admin(overlay_db)
        member_base, _ = _base(cid)
        first = await (await client.post(f"{member_base}/raise-hand", headers=member)).get_json()
        second = await (await client.post(f"{member_base}/raise-hand", headers=member)).get_json()
        assert first["hand"]["raised_at"] == second["hand"]["raised_at"]

    async def test_lower_hand_is_a_noop_when_nothing_is_raised(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        member, _, cid = _member_and_admin(overlay_db)
        member_base, _ = _base(cid)
        await client.post(f"{member_base}/raise-hand", headers=member)
        first = await (await client.post(f"{member_base}/lower-hand", headers=member)).get_json()
        again = await (await client.post(f"{member_base}/lower-hand", headers=member)).get_json()
        assert (first["lowered"], again["lowered"]) == (True, False)
        _, request, _ = fake.calls[1]
        assert request.actor_id == request.participant_id == user_uuid(MEMBER_ID)
        assert request.actor_role == rooms_pb2.ROOM_ROLE_MEMBER

    async def test_leaving_clears_the_hand(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        member, admin, cid = _member_and_admin(overlay_db)
        member_base, admin_base = _base(cid)
        await client.post(f"{member_base}/raise-hand", headers=member)
        await client.post(f"{member_base}/leave", headers=member)
        body = await (await client.get(f"{admin_base}/raised-hands", headers=admin)).get_json()
        assert (body["count"], body["raised_hands"]) == (0, [])


class TestModeratorHands:
    async def test_list_is_oldest_first_with_names_and_guest_for_unknown(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        member, admin, cid = _member_and_admin(overlay_db)
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
            (user_uuid(MEMBER_ID), "Bob"),
            (stranger, "guest"),
        ]
        rpc, request, metadata = fake.calls[-1]
        assert rpc == "ListHands"
        assert metadata["authorization"].endswith("streaming.rooms:read")

    async def test_acknowledge_marks_the_hand_and_acts_as_admin(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        member, admin, cid = _member_and_admin(overlay_db)
        member_base, admin_base = _base(cid)
        await client.post(f"{member_base}/raise-hand", headers=member)
        response = await client.post(
            f"{admin_base}/acknowledge-hand",
            headers=admin,
            json={"user_id": user_uuid(MEMBER_ID)},
        )
        assert response.status_code == 200
        hand = (await response.get_json())["hand"]
        assert hand["participant_id"] == user_uuid(MEMBER_ID)
        assert hand["acknowledged_by"] == user_uuid(ADMIN_ID)
        assert hand["acknowledged_at"] is not None
        rpc, request, metadata = fake.calls[-1]
        assert rpc == "AcknowledgeHand"
        assert request.actor_id == user_uuid(ADMIN_ID)
        assert request.actor_role == rooms_pb2.ROOM_ROLE_ADMIN
        assert metadata["authorization"].endswith("streaming.rooms:moderate")

    async def test_acknowledging_a_hand_that_is_not_raised_is_a_404(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        _, admin, cid = _member_and_admin(overlay_db)
        _, admin_base = _base(cid)
        response = await client.post(
            f"{admin_base}/acknowledge-hand", headers=admin, json={"user_id": user_uuid(MEMBER_ID)}
        )
        assert response.status_code == 404
        assert (await response.get_json())["error"]["code"] == "NOT_FOUND"

    async def test_reset_clears_every_hand_and_reports_the_count(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        member, admin, cid = _member_and_admin(overlay_db)
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
            ("POST", "acknowledge-hand", {"user_id": user_uuid(MEMBER_ID)}),
            ("POST", "reset-hands", None),
        ],
    )
    async def test_moderation_needs_the_admin_scope_not_just_membership(
        self, client: Any, overlay_db: Any, fake: FakeRooms, method: str, suffix: str, body: Any
    ) -> None:
        """A token with only the member-level scope is refused before any RPC."""
        member, _, cid = _member_and_admin(overlay_db)
        _, admin_base = _base(cid)
        response = await client.open(
            f"{admin_base}/{suffix}", method=method, headers=member, json=body
        )
        assert response.status_code == 403
        assert fake.calls == []

    async def test_acknowledge_needs_a_participant_uuid_not_a_name(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        _, admin, cid = _member_and_admin(overlay_db)
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
        _, admin, cid = _member_and_admin(overlay_db)
        fake.seed_room(999)  # ROOM now exists, but only in ANOTHER community
        response = await client.get(
            f"/api/v1/admin/{cid}/calls/rooms/{ROOM}/raised-hands", headers=admin
        )
        assert response.status_code == 404

    async def test_plain_member_cannot_reach_moderation_even_with_admin_scope(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        _, admin, cid = _member_and_admin(overlay_db)
        seed_user(overlay_db, 503)
        seed_membership(overlay_db, community_id=cid, user_id=503, role="community-member")
        from tests.conftest import make_user_token

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
