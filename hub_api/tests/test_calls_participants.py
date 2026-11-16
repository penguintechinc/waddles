"""`services/call_participants.py` -- participant pseudonyms and display-name resolution.

Covers the per-community pseudonym (never the global `hub_users.uuid`), the resolver (ACTIVE
members only, "guest" for everyone else) and the PII boundary: names resolve only inside
hub-api and never reach svc-streaming or a log record.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import grpc
import pytest

from blueprints.v1.calls import SCOPE_ADMIN
from services import call_participants
from services.call_participants import GUEST_LABEL, identity_uuid, resolve_display_names
from tests.conftest import TENANT_SLUG, make_user_token, seed_community, seed_membership
from tests.test_calls_support import (  # noqa: F401  (fixtures are consumed by name)
    ROOM,
    FakeRooms,
    _feature_enabled_default_on,
    app,
    client,
    fake,
    pid,
    record_room,
    seed_user,
    user_headers,
    user_uuid,
)


def _resolve(overlay_db: Any, cid: int, ids: list[str]) -> Any:
    return resolve_display_names(
        overlay_db, overlay_db.dal, tenant_slug=TENANT_SLUG, community_id=cid, participant_ids=ids
    )


def _seed_two_member_community(overlay_db: Any) -> int:
    cid = seed_community(overlay_db)
    seed_user(overlay_db, 11, display_name="Alice")
    seed_user(overlay_db, 12, display_name=None)  # username fallback ("user12")
    seed_user(overlay_db, 13, display_name="Mallory")  # a hub user, but NOT in this community
    seed_membership(overlay_db, community_id=cid, user_id=11, role="community-member")
    seed_membership(overlay_db, community_id=cid, user_id=12, role="community-member")
    return cid


class TestIdentityPseudonym:
    def test_is_per_community_and_never_the_global_user_uuid(self) -> None:
        a = identity_uuid(tenant_slug="acme", community_id=1, user_id=21)
        b = identity_uuid(tenant_slug="acme", community_id=2, user_id=21)
        other_tenant = identity_uuid(tenant_slug="beta", community_id=1, user_id=21)
        assert len({a, b, other_tenant}) == 3  # one person, three unlinkable ids
        assert user_uuid(21) not in {a, b, other_tenant}
        assert a == identity_uuid(tenant_slug="acme", community_id=1, user_id=21)  # stable
        uuid.UUID(a)

    def test_the_derivation_uses_no_global_identity_column(self) -> None:
        import inspect

        assert set(inspect.signature(call_participants.identity_uuid).parameters) == {
            "tenant_slug",
            "community_id",
            "user_id",
        }  # pure: no DAL, so no global identity column can be read


class TestResolveDisplayNames:
    async def test_active_members_resolve_and_everyone_else_is_guest(self, overlay_db: Any) -> None:
        cid = _seed_two_member_community(overlay_db)
        unknown = str(uuid.uuid4())
        ids = [pid(cid, 11), pid(cid, 12), pid(cid, 13), unknown, "not-a-uuid", ""]
        names = await _resolve(overlay_db, cid, ids)
        assert names == {
            pid(cid, 11): "Alice",
            pid(cid, 12): "user12",  # no display_name -> username
            pid(cid, 13): GUEST_LABEL,  # exists, but is not a member of THIS community
            unknown: GUEST_LABEL,
            "not-a-uuid": GUEST_LABEL,
            "": GUEST_LABEL,
        }

    async def test_a_global_hub_uuid_never_resolves(self, overlay_db: Any) -> None:
        cid = _seed_two_member_community(overlay_db)
        assert await _resolve(overlay_db, cid, [user_uuid(11)]) == {user_uuid(11): GUEST_LABEL}

    async def test_former_and_deactivated_members_are_guests(self, overlay_db: Any) -> None:
        cid = _seed_two_member_community(overlay_db)
        dal = overlay_db.dal
        dal(
            (dal.community_members.community_id == cid) & (dal.community_members.user_id == "11")
        ).update(is_active=False)
        dal.commit()
        names = await _resolve(overlay_db, cid, [pid(cid, 11), pid(cid, 12)])
        assert names == {pid(cid, 11): GUEST_LABEL, pid(cid, 12): "user12"}

    async def test_inactive_platform_member_is_a_guest_too(self, overlay_db: Any) -> None:
        cid = seed_community(overlay_db)
        active, gone = str(uuid.uuid4()), str(uuid.uuid4())
        dal = overlay_db.dal
        for pseudonym, name, is_active in ((active, "Pat", True), (gone, "Left", False)):
            dal.community_members.insert(
                community_id=cid,
                user_id=None,
                platform="discord",
                platform_user_id=pseudonym[:8],
                user_uuid=pseudonym,
                display_name=name,
                is_active=is_active,
            )
        dal.commit()
        names = await _resolve(overlay_db, cid, [active, gone])
        assert names == {active: "Pat", gone: GUEST_LABEL}

    async def test_a_member_of_another_community_is_not_named(self, overlay_db: Any) -> None:
        cid_a = _seed_two_member_community(overlay_db)
        cid_b = seed_community(overlay_db, name="other")
        seed_membership(overlay_db, community_id=cid_b, user_id=13, role="community-member")
        in_a = await _resolve(overlay_db, cid_a, [pid(cid_a, 13)])
        in_b = await _resolve(overlay_db, cid_b, [pid(cid_b, 13)])
        assert (in_a[pid(cid_a, 13)], in_b[pid(cid_b, 13)]) == (GUEST_LABEL, "Mallory")
        # the id minted for community B does not resolve in community A (unlinkable)
        assert (await _resolve(overlay_db, cid_a, [pid(cid_b, 11)]))[pid(cid_b, 11)] == GUEST_LABEL

    async def test_empty_input_runs_no_query_and_returns_empty(self, overlay_db: Any) -> None:
        assert await _resolve(overlay_db, 1, []) == {}

    async def test_resolution_is_chunked_without_losing_ids(
        self, overlay_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(call_participants, "_CHUNK", 1)
        cid = _seed_two_member_community(overlay_db)
        names = await _resolve(overlay_db, cid, [pid(cid, 11), pid(cid, 12), pid(cid, 11)])
        assert names == {pid(cid, 11): "Alice", pid(cid, 12): "user12"}

    async def test_uppercase_uuid_input_is_matched_and_keyed_as_given(
        self, overlay_db: Any
    ) -> None:
        cid = _seed_two_member_community(overlay_db)
        upper = pid(cid, 11).upper()
        assert await _resolve(overlay_db, cid, [upper]) == {upper: "Alice"}


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
        fake.seed_participant(cid, pid(cid, 52))
        response = await client.get(
            f"/api/v1/admin/{cid}/calls/rooms/{ROOM}/participants", headers=admin
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["participants"][0]["participant_id"] == pid(cid, 52)
        assert body["participants"][0]["display_name"] == "Bob"
        assert body["participants"][0]["joined_at"] == 1700000100
        wire = b"".join(req.SerializeToString() for _, req, _ in fake.calls)
        assert b"Bob" not in wire and b"Ada" not in wire and b"user52" not in wire
        assert user_uuid(52).encode() not in wire  # the global identity never goes downstream
        assert user_uuid(52) not in str(body)

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
        fake.seed_participant(cid, pid(cid, 62))
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
        fake.seed_participant(cid, pid(cid, 72))
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
        token = make_user_token(user_id=81, scope="streaming.calls:write")  # member scope only
        response = await client.get(
            f"/api/v1/admin/{cid}/calls/rooms/{ROOM}/participants",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403
        assert fake.calls == []
