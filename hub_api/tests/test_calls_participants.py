"""`services/call_participants.py` -- participant identity and display-name resolution.

Covers the resolver itself, the identity derivation (plan risk R5: REST and the `rooms` app
bundle must name the same human with the same UUID) and the PII boundary: names resolve only
inside hub-api, only for members of the asked community, and never reach svc-streaming or a
log record.
"""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from typing import Any

import grpc
import pytest

from blueprints.v1.calls import SCOPE_ADMIN, SCOPE_WRITE
from services import call_participants
from services.call_participants import GUEST_LABEL, identity_uuid, resolve_display_names
from services.errors import ApiError
from tests.conftest import make_user_token, seed_community, seed_membership
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

REPO = Path(__file__).resolve().parents[2]


def _seed_two_member_community(overlay_db: Any) -> int:
    cid = seed_community(overlay_db)
    seed_user(overlay_db, 11, display_name="Alice")
    seed_user(overlay_db, 12, display_name=None)  # username fallback ("user12")
    seed_user(overlay_db, 13, display_name="Mallory")  # a hub user, but NOT in this community
    seed_membership(overlay_db, community_id=cid, user_id=11, role="community-member")
    seed_membership(overlay_db, community_id=cid, user_id=12, role="community-member")
    return cid


class TestIdentityUuid:
    async def test_is_the_hub_users_uuid(self, overlay_db: Any) -> None:
        seed_user(overlay_db, 21)
        assert await identity_uuid(overlay_db, overlay_db.dal, user_id=21) == user_uuid(21)

    async def test_unknown_user_is_a_loud_401_never_a_made_up_id(self, overlay_db: Any) -> None:
        with pytest.raises(ApiError) as err:
            await identity_uuid(overlay_db, overlay_db.dal, user_id=4040)
        assert (err.value.status_code, err.value.code) == (401, "UNKNOWN_USER")

    async def test_matches_how_the_rooms_app_derives_its_actor_uuid(self, overlay_db: Any) -> None:
        """R5: the id hub-api sends equals the one the data plane's identity view yields.

        The `rooms` bundle's host-verified actor is `community_member_identities.user_uuid`,
        i.e. `community_members.user_uuid`, which `resolve_identity_uuid` (alembic 0048) sets to
        `hub_users.uuid` for a hub-linked member. This replays that join (member row ->
        `hub_users.id::text = user_id` -> uuid) and requires hub-api's participant id to be
        identical, so a REST kick and a chat `!room kick` target the same participant.
        """
        cid = seed_community(overlay_db)
        seed_user(overlay_db, 31, display_name="Carol")
        seed_membership(overlay_db, community_id=cid, user_id=31, role="community-member")
        dal = overlay_db.dal
        member = (
            dal(
                (dal.community_members.community_id == cid)
                & (dal.community_members.user_id == "31")
            )
            .select()
            .first()
        )
        user = dal(dal.hub_users.id == int(member.user_id)).select().first()
        data_plane_actor = str(uuid.UUID(user.uuid))  # the resolver's explicit-link rule
        assert await identity_uuid(overlay_db, dal, user_id=31) == data_plane_actor

    def test_the_resolver_rule_this_relies_on_is_still_in_the_migration(self) -> None:
        """Source pin on the resolver rule `identity_uuid` mirrors.

        If `resolve_identity_uuid` stops resolving a hub link to `hub_users.uuid` this fails and
        `identity_uuid` must be revisited (a silent drift would split one human into two ids).
        """
        sql = (REPO / "alembic/versions/0048_identity_forged_uuid.py").read_text()
        assert re.search(
            r"SELECT hu\.uuid INTO v FROM hub_users hu WHERE hu\.id::text = p_user_id", sql
        ), "resolve_identity_uuid no longer maps an explicit hub link to hub_users.uuid"
        assert "uuid5" not in (REPO / "hub_api/blueprints/v1/calls.py").read_text()


class TestResolveDisplayNames:
    async def test_members_resolve_and_everyone_else_is_guest(self, overlay_db: Any) -> None:
        cid = _seed_two_member_community(overlay_db)
        unknown = str(uuid.uuid4())
        ids = [user_uuid(11), user_uuid(12), user_uuid(13), unknown, "not-a-uuid", ""]
        names = await resolve_display_names(
            overlay_db, overlay_db.dal, community_id=cid, participant_ids=ids
        )
        assert names == {
            user_uuid(11): "Alice",
            user_uuid(12): "user12",  # no display_name -> username
            user_uuid(13): GUEST_LABEL,  # exists, but is not a member of THIS community
            unknown: GUEST_LABEL,
            "not-a-uuid": GUEST_LABEL,
            "": GUEST_LABEL,
        }

    async def test_a_member_of_another_community_is_not_named(self, overlay_db: Any) -> None:
        cid_a = _seed_two_member_community(overlay_db)
        cid_b = seed_community(overlay_db, name="other")
        seed_membership(overlay_db, community_id=cid_b, user_id=13, role="community-member")
        in_a = await resolve_display_names(
            overlay_db, overlay_db.dal, community_id=cid_a, participant_ids=[user_uuid(13)]
        )
        in_b = await resolve_display_names(
            overlay_db, overlay_db.dal, community_id=cid_b, participant_ids=[user_uuid(13)]
        )
        assert (in_a[user_uuid(13)], in_b[user_uuid(13)]) == (GUEST_LABEL, "Mallory")

    async def test_platform_only_member_resolves_by_member_row_uuid(self, overlay_db: Any) -> None:
        """A chat-only member (no hub account) carries a pseudonym `user_uuid` and its own name."""
        cid = seed_community(overlay_db)
        pseudonym = str(uuid.uuid4())
        dal = overlay_db.dal
        dal.community_members.insert(
            community_id=cid,
            user_id=None,
            platform="discord",
            platform_user_id="p-1",
            user_uuid=pseudonym,
            display_name="PlatformPat",
            is_active=True,
        )
        dal.commit()
        names = await resolve_display_names(
            overlay_db, dal, community_id=cid, participant_ids=[pseudonym]
        )
        assert names == {pseudonym: "PlatformPat"}

    async def test_hub_name_wins_over_the_member_row_name(self, overlay_db: Any) -> None:
        cid = seed_community(overlay_db)
        seed_user(overlay_db, 41, display_name="HubName")
        dal = overlay_db.dal
        dal.community_members.insert(
            community_id=cid,
            user_id="41",
            user_uuid=user_uuid(41),
            display_name="ChatName",
            is_active=True,
        )
        dal.commit()
        names = await resolve_display_names(
            overlay_db, dal, community_id=cid, participant_ids=[user_uuid(41)]
        )
        assert names == {user_uuid(41): "HubName"}

    async def test_empty_input_runs_no_query_and_returns_empty(self, overlay_db: Any) -> None:
        assert (
            await resolve_display_names(
                overlay_db, overlay_db.dal, community_id=1, participant_ids=[]
            )
            == {}
        )

    async def test_resolution_is_chunked_without_losing_ids(
        self, overlay_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(call_participants, "_CHUNK", 1)
        cid = _seed_two_member_community(overlay_db)
        names = await resolve_display_names(
            overlay_db,
            overlay_db.dal,
            community_id=cid,
            participant_ids=[user_uuid(11), user_uuid(12), user_uuid(11)],
        )
        assert names == {user_uuid(11): "Alice", user_uuid(12): "user12"}

    async def test_uppercase_uuid_input_is_matched_and_keyed_as_given(
        self, overlay_db: Any
    ) -> None:
        cid = _seed_two_member_community(overlay_db)
        upper = user_uuid(11).upper()
        names = await resolve_display_names(
            overlay_db, overlay_db.dal, community_id=cid, participant_ids=[upper]
        )
        assert names == {upper: "Alice"}


class TestPiiBoundaryThroughTheRoutes:
    async def test_participants_route_names_them_but_svc_streaming_never_sees_a_name(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        admin, cid = user_headers(
            overlay_db, scope=SCOPE_ADMIN, role="community-owner", user_id=51, display_name="Ada"
        )
        seed_user(overlay_db, 52, display_name="Bob")
        seed_membership(overlay_db, community_id=cid, user_id=52, role="community-member")
        fake.seed_room(cid)
        fake.seed_participant(cid, user_uuid(52))
        response = await client.get(
            f"/api/v1/admin/{cid}/calls/rooms/{ROOM}/participants", headers=admin
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["participants"][0]["participant_id"] == user_uuid(52)
        assert body["participants"][0]["display_name"] == "Bob"
        assert body["participants"][0]["joined_at"] == 1700000100
        wire = b"".join(req.SerializeToString() for _, req, _ in fake.calls)
        assert b"Bob" not in wire and b"Ada" not in wire and b"user52" not in wire

    async def test_names_never_reach_the_logs(
        self,
        client: Any,
        overlay_db: Any,
        fake: FakeRooms,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        admin, cid = user_headers(
            overlay_db, scope=SCOPE_ADMIN, role="community-owner", user_id=61, display_name="Ada"
        )
        seed_user(overlay_db, 62, display_name="Zelda")
        seed_membership(overlay_db, community_id=cid, user_id=62, role="community-member")
        fake.seed_room(cid)
        fake.seed_participant(cid, user_uuid(62))
        fake.fail["ListHands"] = (grpc.StatusCode.UNAVAILABLE, "boom")
        caplog.set_level(logging.DEBUG)
        base = f"/api/v1/admin/{cid}/calls/rooms/{ROOM}"
        await client.get(f"{base}/participants", headers=admin)
        await client.get(f"{base}/raised-hands", headers=admin)  # fails -> ERROR log path
        assert "Zelda" not in caplog.text and "Ada" not in caplog.text
        assert "user62" not in caplog.text

    async def test_names_are_per_community_through_the_route(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        """A participant who is not a member of the asked community renders as guest."""
        admin, cid = user_headers(
            overlay_db, scope=SCOPE_ADMIN, role="community-owner", user_id=71, display_name="Ada"
        )
        seed_user(overlay_db, 72, display_name="Outsider")  # no membership anywhere
        fake.seed_room(cid)
        fake.seed_participant(cid, user_uuid(72))
        body = await (
            await client.get(f"/api/v1/admin/{cid}/calls/rooms/{ROOM}/participants", headers=admin)
        ).get_json()
        assert body["participants"][0]["display_name"] == GUEST_LABEL

    async def test_member_token_scope_is_still_required(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        cid = seed_community(overlay_db)
        seed_user(overlay_db, 81)
        seed_membership(overlay_db, community_id=cid, user_id=81, role="community-owner")
        token = make_user_token(user_id=81, scope=SCOPE_WRITE)  # member scope, admin route
        response = await client.get(
            f"/api/v1/admin/{cid}/calls/rooms/{ROOM}/participants",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403
        assert fake.calls == []
