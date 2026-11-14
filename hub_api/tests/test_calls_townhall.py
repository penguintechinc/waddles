"""Community-scoped member routes: own hand state (`hands/mine`) and the per-community Town Hall."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from blueprints.v1.calls import SCOPE_READ, SCOPE_WRITE
from services import call_town_hall
from services.call_town_hall import TOWN_HALL_LABEL
from tests.conftest import seed_community
from tests.test_calls_support import (  # noqa: F401  (fixtures are consumed by name)
    ROOM,
    FakeRooms,
    _feature_enabled_default_on,
    app,
    client,
    fake,
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
        response = await client.get(_mine(cid), headers=headers)
        assert response.status_code == 200
        assert await response.get_json() == {
            "participant_id": user_uuid(700),
            "raised": False,
            "acknowledged": False,
            "raised_at": None,
            "acknowledged_at": None,
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
        base = f"/api/v1/community/{cid}/interact/voice/rooms/{ROOM}"
        await client.post(f"{base}/raise-hand", headers=other)  # someone else's hand
        await client.post(f"{base}/raise-hand", headers=mine)
        body = await (await client.get(_mine(cid), headers=mine)).get_json()
        assert (body["raised"], body["acknowledged"]) == (True, False)
        assert body["raised_at"] is not None and body["acknowledged_at"] is None
        # Acknowledge as the moderator side (straight into the fake: the admin route is covered
        # in test_calls_hands.py).
        hand = next(h for h in fake.rooms[(str(cid), ROOM)]["hands"] if h["pid"] == user_uuid(701))
        hand["ack_at"], hand["ack_by"] = 1700001234, user_uuid(799)
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
            "success",
        }
        assert user_uuid(702) not in str(body) and user_uuid(799) not in str(body)

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
    async def test_created_lazily_as_a_never_expiring_room(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        headers, cid = _member(overlay_db)
        response = await client.get(_th(cid), headers=headers)
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

    async def test_get_and_post_are_idempotent_and_return_the_same_room(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        headers, cid = _member(overlay_db)
        first = await (await client.get(_th(cid), headers=headers)).get_json()
        second = await (await client.post(_th(cid), headers=headers)).get_json()
        third = await (await client.get(_th(cid), headers=headers)).get_json()
        assert first["room_id"] == second["room_id"] == third["room_id"]
        assert (first["created"], second["created"], third["created"]) == (True, False, False)
        assert [name for name, _, _ in fake.calls].count("CreateRoom") == 1

    async def test_concurrent_requests_create_exactly_one_room(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        headers, cid = _member(overlay_db)
        bodies = await asyncio.gather(*(client.post(_th(cid), headers=headers) for _ in range(8)))
        ids = {(await b.get_json())["room_id"] for b in bodies}
        assert len(ids) == 1
        town_halls = [
            s for (c, _), s in fake.rooms.items() if c == str(cid) and s["label"] == TOWN_HALL_LABEL
        ]
        assert len(town_halls) == 1

    async def test_participant_count_is_live(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        headers, cid = _member(overlay_db)
        room_id = (await (await client.get(_th(cid), headers=headers)).get_json())["room_id"]
        fake.seed_participant(cid, user_uuid(1), room_id)
        fake.seed_participant(cid, user_uuid(2), room_id)
        body = await (await client.get(_th(cid), headers=headers)).get_json()
        assert body["participant_count"] == 2

    async def test_each_community_has_its_own_town_hall(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        a_headers, cid_a = _member(overlay_db, user_id=710)
        b_headers, cid_b = _member(overlay_db, user_id=711)
        a = await (await client.get(_th(cid_a), headers=a_headers)).get_json()
        b = await (await client.get(_th(cid_b), headers=b_headers)).get_json()
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
        response = await client.get(_th(cid), headers=headers)
        assert response.status_code == 429
        assert "room_id" not in await response.get_json()


class TestTownHallConvergence:
    """The cross-replica race: duplicates converge on the oldest room; empty losers are removed."""

    async def test_oldest_wins_and_the_empty_loser_is_deleted(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        headers, cid = _member(overlay_db)
        fake.seed_room(cid, ROOM, TOWN_HALL_LABEL, created_at=100)
        fake.seed_room(cid, ROOM_B, TOWN_HALL_LABEL, created_at=200)
        body = await (await client.get(_th(cid), headers=headers)).get_json()
        assert body["room_id"] == ROOM and body["created"] is False
        assert (str(cid), ROOM_B) not in fake.rooms  # the empty duplicate is gone
        assert "DeleteRoom" in [name for name, _, _ in fake.calls]

    async def test_equal_timestamps_break_ties_by_lowest_room_id(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        headers, cid = _member(overlay_db)
        fake.seed_room(cid, ROOM_B, TOWN_HALL_LABEL, created_at=100)
        fake.seed_room(cid, ROOM, TOWN_HALL_LABEL, created_at=100)
        body = await (await client.get(_th(cid), headers=headers)).get_json()
        assert body["room_id"] == min(ROOM, ROOM_B)

    async def test_an_occupied_loser_is_never_deleted(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        headers, cid = _member(overlay_db)
        fake.seed_room(cid, ROOM, TOWN_HALL_LABEL, created_at=100)
        fake.seed_room(cid, ROOM_B, TOWN_HALL_LABEL, created_at=200)
        fake.seed_participant(cid, user_uuid(5), ROOM_B)
        await client.get(_th(cid), headers=headers)
        assert (str(cid), ROOM_B) in fake.rooms
        assert "DeleteRoom" not in [name for name, _, _ in fake.calls]

    async def test_two_replicas_racing_converge_on_one_room(
        self, client: Any, overlay_db: Any, fake: FakeRooms, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Defeat the in-process lock (another replica has none) and race creates directly."""
        headers, cid = _member(overlay_db)
        import blueprints.v1.calls as calls_module

        class _Locks(dict):  # a fresh lock per call == no shared lock across "replicas"
            def setdefault(self, key: int, default: Any = None) -> Any:
                return asyncio.Lock()

        monkeypatch.setattr(call_town_hall, "_locks", _Locks())
        results = await asyncio.gather(
            *(call_town_hall.ensure_town_hall(calls_module._client, cid) for _ in range(6))
        )
        assert len({r.room.room_id for r in results}) == 1
        survivors = [
            s for (c, _), s in fake.rooms.items() if c == str(cid) and s["label"] == TOWN_HALL_LABEL
        ]
        assert len(survivors) == 1  # every empty duplicate was pruned

    async def test_failed_cleanup_is_logged_not_fatal(
        self, client: Any, overlay_db: Any, fake: FakeRooms, caplog: pytest.LogCaptureFixture
    ) -> None:
        import grpc

        headers, cid = _member(overlay_db)
        fake.seed_room(cid, ROOM, TOWN_HALL_LABEL, created_at=100)
        fake.seed_room(cid, ROOM_B, TOWN_HALL_LABEL, created_at=200)
        fake.fail["DeleteRoom"] = (grpc.StatusCode.UNAVAILABLE, "boom")
        response = await client.get(_th(cid), headers=headers)
        assert response.status_code == 200 and (await response.get_json())["room_id"] == ROOM
        assert "town hall duplicate cleanup failed" in caplog.text
