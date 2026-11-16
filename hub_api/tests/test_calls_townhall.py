"""Community-scoped member routes: own hand state (`hands/mine`) and the per-community Town Hall."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from blueprints.v1.calls import SCOPE_READ, SCOPE_WRITE
from services import call_town_hall
from services.call_town_hall import TOWN_HALL_LABEL
from tests.conftest import seed_community, seed_membership
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

BOTH = f"{SCOPE_READ} {SCOPE_WRITE}"
ROOM_B = "3c2a1b0f-9e8d-4c7b-8a6f-5e4d3c2b1a09"


def _mine(cid: int, room: str = ROOM) -> str:
    return f"/api/v1/communities/{cid}/calls/rooms/{room}/hands/mine"


def _th(cid: int) -> str:
    return f"/api/v1/communities/{cid}/town-hall"


def _member(overlay_db: Any, *, user_id: int = 700, cid: int | None = None) -> tuple[dict, int]:
    return user_headers(
        overlay_db, scope=BOTH, role="community-member", user_id=user_id, community_id=cid
    )


class TestMyHand:
    async def test_no_hand_raised(self, client: Any, overlay_db: Any, fake: FakeRooms) -> None:
        headers, cid = _member(overlay_db)
        await join(client, cid, headers)
        response = await client.get(_mine(cid), headers=headers)
        assert response.status_code == 200
        assert await response.get_json() == {
            "participant_id": pid(cid, 700),
            "raised": False,
            "acknowledged": False,
            "raised_at": None,
            "acknowledged_at": None,
            "position": None,
            "queue_length": 0,
            "success": True,
        }
        rpc, request, metadata = fake.calls[-1]
        assert rpc == "ListHands"
        assert metadata["authorization"].endswith("streaming.rooms:read")

    async def test_raised_then_acknowledged_is_visible_to_the_owner_only(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        mine, cid = _member(overlay_db, user_id=701)
        other, _ = _member(overlay_db, user_id=702, cid=cid)
        await join(client, cid, mine)
        await join(client, cid, other)
        base = f"/api/v1/community/{cid}/interact/voice/rooms/{ROOM}"
        await client.post(f"{base}/raise-hand", headers=other)  # someone else's hand
        await client.post(f"{base}/raise-hand", headers=mine)
        body = await (await client.get(_mine(cid), headers=mine)).get_json()
        assert (body["raised"], body["acknowledged"]) == (True, False)
        assert body["raised_at"] is not None and body["acknowledged_at"] is None
        assert (body["position"], body["queue_length"]) == (2, 2)  # counts only, never who
        # Acknowledge as the moderator side (straight into the fake: the admin route is covered
        # in test_calls_hands.py).
        hand = next(h for h in fake.rooms[(str(cid), ROOM)]["hands"] if h["pid"] == pid(cid, 701))
        hand["ack_at"], hand["ack_by"] = 1700001234, pid(cid, 799)
        body = await (await client.get(_mine(cid), headers=mine)).get_json()
        assert (body["raised"], body["acknowledged"]) == (True, True)
        assert body["acknowledged_at"] == 1700001234
        # Only the caller's own data: exact key set, never the other member or the acknowledger.
        assert set(body) == {
            "participant_id",
            "raised",
            "acknowledged",
            "raised_at",
            "acknowledged_at",
            "position",
            "queue_length",
            "success",
        }
        assert pid(cid, 702) not in str(body) and pid(cid, 799) not in str(body)

    async def test_other_communities_room_is_404(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        headers, cid = _member(overlay_db)
        fake.seed_room(999)  # ROOM lives in another community
        response = await client.get(_mine(cid), headers=headers)
        assert response.status_code == 404
        assert (await response.get_json())["error"]["code"] == "NOT_FOUND"

    async def test_non_member_is_403_and_flag_off_is_402(
        self,
        client: Any,
        overlay_db: Any,
        fake: FakeRooms,
        _feature_enabled_default_on: Any,
    ) -> None:
        headers, cid = _member(overlay_db)
        outsider, _ = user_headers(
            overlay_db,
            scope=BOTH,
            role="community-member",
            user_id=777,
            community_id=seed_community(overlay_db, name="elsewhere"),
        )
        assert (await client.get(_mine(cid), headers=outsider)).status_code == 403
        _feature_enabled_default_on.return_value = False
        assert (await client.get(_mine(cid), headers=headers)).status_code == 402
        assert fake.calls == []

    async def test_room_must_be_a_uuid(self, client: Any, overlay_db: Any, fake: FakeRooms) -> None:
        headers, cid = _member(overlay_db)
        assert (await client.get(_mine(cid, "general"), headers=headers)).status_code == 400
        assert fake.calls == []


class TestTownHall:
    async def test_get_is_read_only_and_404_until_a_post_creates_it(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        headers, cid = _member(overlay_db)
        response = await client.get(_th(cid), headers=headers)
        assert response.status_code == 404
        assert (await response.get_json())["error"]["code"] == "TOWN_HALL_NOT_FOUND"
        # a GET never touches svc-streaming and never writes a record
        assert [name for name, _, _ in fake.calls] == []
        assert fake.rooms == {}
        dal = overlay_db.dal
        assert dal(dal.call_rooms).count() == 0

    async def test_post_creates_a_never_expiring_recorded_room(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        headers, cid = _member(overlay_db)
        response = await client.post(_th(cid), headers=headers)
        assert response.status_code == 200
        body = await response.get_json()
        assert body["label"] == TOWN_HALL_LABEL
        assert body["created"] is True and body["participant_count"] == 0
        uuid.UUID(body["room_id"])
        assert body["join"] == {
            "method": "POST",
            "path": f"/api/v1/community/{cid}/interact/voice/rooms/{body['room_id']}/join",
        }
        state = fake.rooms[(str(cid), body["room_id"])]
        assert state["label"] == TOWN_HALL_LABEL and state["empty_timeout_secs"] == 0
        create = next(r for name, r, _ in fake.calls if name == "CreateRoom")
        assert create.HasField("empty_timeout_secs") and create.empty_timeout_secs == 0
        dal = overlay_db.dal
        rows = dal(dal.call_rooms.town_hall == True).select()  # noqa: E712
        assert [(r.community_id, r.room_id) for r in rows] == [(cid, body["room_id"])]

    async def test_get_after_post_and_repeated_posts_are_idempotent(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        headers, cid = _member(overlay_db)
        first = await (await client.post(_th(cid), headers=headers)).get_json()
        second = await (await client.post(_th(cid), headers=headers)).get_json()
        third = await (await client.get(_th(cid), headers=headers)).get_json()
        assert first["room_id"] == second["room_id"] == third["room_id"]
        assert (first["created"], second["created"], third["created"]) == (True, False, False)
        assert [name for name, _, _ in fake.calls].count("CreateRoom") == 1

    async def test_concurrent_posts_end_with_exactly_one_recorded_room(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        headers, cid = _member(overlay_db)
        responses = await asyncio.gather(
            *(client.post(_th(cid), headers=headers) for _ in range(8))
        )
        ids = {(await r.get_json())["room_id"] for r in responses}
        assert len(ids) == 1
        survivors = [
            s for (c, _), s in fake.rooms.items() if c == str(cid) and s["label"] == TOWN_HALL_LABEL
        ]
        assert len(survivors) == 1  # losers deleted only the room each had just created
        dal = overlay_db.dal
        assert dal(dal.call_rooms.town_hall == True).count() == 1  # noqa: E712

    async def test_the_unique_index_decides_a_cross_replica_race(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        """Two creators, no shared lock: one insert conflicts, its room is deleted, one survives."""
        headers, cid = _member(overlay_db)
        import blueprints.v1.calls as calls_module

        results = await asyncio.gather(
            *(
                call_town_hall.ensure_town_hall(
                    calls_module._client, overlay_db, overlay_db.dal, cid
                )
                for _ in range(6)
            )
        )
        assert len({r.room.room_id for r in results}) == 1
        assert sum(r.created for r in results) == 1
        survivors = [s for (c, _), s in fake.rooms.items() if c == str(cid)]
        assert len(survivors) == 1

    async def test_a_member_cannot_create_a_room_called_town_hall(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        """Labels are member-settable, so the reserved name is refused (any case/spacing)."""
        headers, cid = _member(overlay_db)
        base = f"/api/v1/community/{cid}/interact/voice/rooms"
        for label in ("Town Hall", "town hall", "  TOWN   HALL ", "tOwN\thAlL"):
            response = await client.post(base, headers=headers, json={"room_name": label})
            assert response.status_code == 400, label
            assert (await response.get_json())["error"]["code"] == "RESERVED_ROOM_LABEL"
        assert "CreateRoom" not in [name for name, _, _ in fake.calls]

    async def test_a_room_named_like_the_town_hall_is_never_mistaken_for_it(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        """Even a pre-existing room labelled "Town Hall" is not found by label nor deleted."""
        headers, cid = _member(overlay_db)
        fake.seed_room(cid, ROOM_B, TOWN_HALL_LABEL, created_at=1)
        body = await (await client.post(_th(cid), headers=headers)).get_json()
        assert body["created"] is True and body["room_id"] != ROOM_B
        assert (str(cid), ROOM_B) in fake.rooms  # never deleted: this mechanism did not create it
        assert "DeleteRoom" not in [name for name, _, _ in fake.calls]
        assert (await client.get(_th(cid), headers=headers)).status_code == 200

    async def test_a_gone_town_hall_room_is_recreated_not_silently_empty(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        headers, cid = _member(overlay_db)
        fake.seed_room(999)  # the fake's shared ROOM id belongs elsewhere: no lazy re-creation
        first = await (await client.post(_th(cid), headers=headers)).get_json()
        del fake.rooms[(str(cid), first["room_id"])]  # lost upstream (e.g. server restart)
        assert (await client.get(_th(cid), headers=headers)).status_code == 404  # GET: no healing
        again = await (await client.post(_th(cid), headers=headers)).get_json()
        assert again["created"] is True and again["room_id"] != first["room_id"]
        dal = overlay_db.dal
        assert dal(dal.call_rooms.town_hall == True).count() == 1  # noqa: E712

    async def test_participant_count_is_live(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        headers, cid = _member(overlay_db)
        room_id = (await (await client.post(_th(cid), headers=headers)).get_json())["room_id"]
        fake.seed_participant(cid, pid(cid, 1), room_id)
        fake.seed_participant(cid, pid(cid, 2), room_id)
        body = await (await client.get(_th(cid), headers=headers)).get_json()
        assert body["participant_count"] == 2

    async def test_each_community_has_its_own_town_hall(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        a_headers, cid_a = _member(overlay_db, user_id=710)
        b_headers, cid_b = _member(overlay_db, user_id=711)
        a = await (await client.post(_th(cid_a), headers=a_headers)).get_json()
        b = await (await client.post(_th(cid_b), headers=b_headers)).get_json()
        assert a["room_id"] != b["room_id"]
        # B's member cannot see A's room through B's own URL space.
        assert (await client.get(_mine(cid_b, a["room_id"]), headers=b_headers)).status_code == 404

    async def test_non_member_403_flag_off_402_and_scope_required(
        self,
        client: Any,
        overlay_db: Any,
        fake: FakeRooms,
        _feature_enabled_default_on: Any,
    ) -> None:
        headers, cid = _member(overlay_db)
        outsider, _ = user_headers(
            overlay_db,
            scope=BOTH,
            role="community-member",
            user_id=778,
            community_id=seed_community(overlay_db, name="x"),
        )
        assert (await client.get(_th(cid), headers=outsider)).status_code == 403
        read_only, _ = user_headers(
            overlay_db, scope=SCOPE_READ, role="community-member", user_id=779, community_id=cid
        )
        assert (await client.post(_th(cid), headers=read_only)).status_code == 403
        assert fake.calls == []
        _feature_enabled_default_on.return_value = False
        for method in ("GET", "POST"):
            response = await client.open(_th(cid), method=method, headers=headers)
            assert response.status_code == 402
        assert fake.calls == []

    async def test_upstream_failure_is_not_masked(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        import grpc

        headers, cid = _member(overlay_db)
        fake.fail["CreateRoom"] = (grpc.StatusCode.RESOURCE_EXHAUSTED, "limit reached: 100 rooms")
        response = await client.post(_th(cid), headers=headers)
        assert response.status_code == 429
        assert "room_id" not in await response.get_json()


class TestMemberVisibility:
    """What a non-staff member may see: no publisher lists, no acknowledgers, no global ids."""

    async def test_member_room_list_and_create_hide_publisher_ids(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        member, cid = _member(overlay_db)
        admin, _ = user_headers(
            overlay_db,
            scope="streaming.calls:admin streaming.calls:read",
            role="community-owner",
            user_id=790,
            community_id=cid,
        )
        fake.seed_room(cid)
        fake.seed_participant(cid, pid(cid, 5))
        member_list = await (
            await client.get(f"/api/v1/community/{cid}/interact/voice/rooms", headers=member)
        ).get_json()
        admin_list = await (
            await client.get(f"/api/v1/admin/{cid}/calls/rooms", headers=admin)
        ).get_json()
        assert member_list["rooms"][0]["publisher_ids"] == []
        assert admin_list["rooms"][0]["publisher_ids"] == [pid(cid, 5)]  # staff still see them
        created = await (
            await client.post(
                f"/api/v1/community/{cid}/interact/voice/rooms",
                headers=member,
                json={"room_name": "chat"},
            )
        ).get_json()
        assert created["publisher_ids"] == []

    async def test_raise_hand_hides_the_acknowledger_but_staff_see_it(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        member, cid = _member(overlay_db)
        await join(client, cid, member)
        base = f"/api/v1/community/{cid}/interact/voice/rooms/{ROOM}"
        first = await (await client.post(f"{base}/raise-hand", headers=member)).get_json()
        hand = fake.rooms[(str(cid), ROOM)]["hands"][0]
        hand["ack_at"], hand["ack_by"] = 1700001234, pid(cid, 799)
        again = await (await client.post(f"{base}/raise-hand", headers=member)).get_json()
        assert first["hand"]["acknowledged_by"] is None
        assert again["hand"]["acknowledged_at"] == 1700001234
        assert again["hand"]["acknowledged_by"] is None  # a member never learns who acknowledged

    async def test_member_participants_route(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        member, cid = _member(overlay_db)
        seed_user(overlay_db, 801, display_name="Cleo")
        seed_membership(overlay_db, community_id=cid, user_id=801, role="community-member")
        seed_user(overlay_db, 802, display_name="Gone")
        seed_membership(overlay_db, community_id=cid, user_id=802, role="community-member")
        dal = overlay_db.dal
        dal(dal.community_members.user_id == "802").update(is_active=False)
        dal.commit()
        fake.seed_room(cid)
        for uid in (700, 801, 802):
            fake.seed_participant(cid, pid(cid, uid))
        fake.seed_participant(cid, "5d1f2e3a-1111-4a2b-8c3d-9e8f7a6b5c4d")  # unknown guest
        fake.rooms[(str(cid), ROOM)]["hands"].append(
            {"pid": pid(cid, 801), "raised_at": 5, "ack_at": 9, "ack_by": pid(cid, 799)}
        )
        response = await client.get(
            f"/api/v1/communities/{cid}/calls/rooms/{ROOM}/participants", headers=member
        )
        assert response.status_code == 200
        body = await response.get_json()
        by_id = {p["participant_id"]: p for p in body["participants"]}
        assert by_id[pid(cid, 801)]["display_name"] == "Cleo"
        assert by_id[pid(cid, 802)]["display_name"] == "guest"  # deactivated member
        assert by_id["5d1f2e3a-1111-4a2b-8c3d-9e8f7a6b5c4d"]["display_name"] == "guest"
        assert by_id[pid(cid, 700)]["is_self"] is True and by_id[pid(cid, 801)]["is_self"] is False
        text = str(body)
        assert "acknowledged" not in text and "publisher_ids" not in text
        assert user_uuid(801) not in text and user_uuid(700) not in text
        assert set(by_id[pid(cid, 801)]) == {
            "participant_id",
            "display_name",
            "publisher",
            "subscriber",
            "muted",
            "joined_at",
            "is_self",
        }
        assert [r for r, *_ in fake.calls] == ["ListParticipants"]

    async def test_member_participants_route_authz_flag_and_isolation(
        self,
        client: Any,
        overlay_db: Any,
        fake: FakeRooms,
        _feature_enabled_default_on: Any,
    ) -> None:
        member, cid = _member(overlay_db)
        url = f"/api/v1/communities/{cid}/calls/rooms/{ROOM}/participants"
        outsider, _ = user_headers(
            overlay_db,
            scope=BOTH,
            role="community-member",
            user_id=803,
            community_id=seed_community(overlay_db, name="elsewhere"),
        )
        assert (await client.get(url, headers=outsider)).status_code == 403
        fake.seed_room(999)  # ROOM exists, but in another community
        assert (await client.get(url, headers=member)).status_code == 404
        _feature_enabled_default_on.return_value = False
        assert (await client.get(url, headers=member)).status_code == 402
        assert (await client.get(url.replace(ROOM, "general"), headers=member)).status_code in (
            400,
            402,
        )
