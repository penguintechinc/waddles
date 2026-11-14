"""REST join: the shared room-access policy, the contract join TTL and the browser join body.

`services/call_access.py` is the Python twin of `bundles/rust/webrtc/src/grants.rs`; EVERY case
in the shared contract file `bundles/rust/webrtc/contract/grants.json` is replayed against it
here, and the join route is driven end to end against the in-process `RoomsService`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from blueprints.v1.calls import SCOPE_WRITE
from services import call_access, call_ice
from services.call_access import (
    AccessDeniedError,
    GrantCtx,
    GrantKind,
    GrantRole,
    Window,
    grants_for,
)
from services.calls_proxy import RoomInfo
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

CONTRACT = json.loads(
    (Path(__file__).resolve().parents[2] / "bundles/rust/webrtc/contract/grants.json").read_text()
)


def _ctx(case: dict[str, Any]) -> GrantCtx:
    return GrantCtx(
        closed=case.get("closed", False),
        locked=case.get("locked", False),
        allow_guests=case.get("allow_guests", False),
        window=Window(case.get("window", "none")),
    )


class TestPolicyMatchesTheSharedContract:
    def test_every_contract_case_is_replayed(self) -> None:
        cases = CONTRACT["cases"]
        assert len(cases) >= 20, f"only {len(cases)} contract cases examined"
        for case in cases:
            kind, role, ctx = GrantKind(case["kind"]), GrantRole[case["role"].upper()], _ctx(case)
            expect = case["expect"]
            if "denied" in expect:
                with pytest.raises(AccessDeniedError) as err:
                    grants_for(kind, role, ctx)
                assert err.value.reason == expect["denied"], case["id"]
            else:
                got = grants_for(kind, role, ctx)
                assert (got.publish, got.subscribe) == (
                    expect["publish"],
                    expect["subscribe"],
                ), case["id"]

    def test_join_ttl_numbers_are_the_contracts(self) -> None:
        token = CONTRACT["token"]
        assert call_access.JOIN_TTL_SECS == token["join_ttl_secs"]
        assert call_access.JOIN_TTL_MAX_SECS == token["join_ttl_max_secs"]
        assert call_access.JOIN_TTL_SECS <= call_access.JOIN_TTL_MAX_SECS

    def test_the_enums_cover_exactly_the_contract_vocabulary(self) -> None:
        assert {r.name.lower() for r in GrantRole} == set(CONTRACT["roles"])
        assert {k.value for k in GrantKind} == set(CONTRACT["kinds"])

    def test_hub_api_sees_only_the_lock_of_a_room(self) -> None:
        room = RoomInfo(ROOM, "1", "x", 16, 0, [], 0, True)
        kind, ctx = call_access.facts_from_room(room)
        assert kind is GrantKind.COMMUNITY
        assert ctx == GrantCtx(locked=True)


def _join_url(community_id: int, room: str = ROOM) -> str:
    return f"/api/v1/community/{community_id}/interact/voice/rooms/{room}/join"


def _member(overlay_db: Any, **kw: Any) -> tuple[dict[str, str], int]:
    return user_headers(
        overlay_db, scope=SCOPE_WRITE, role="community-member", user_id=kw.pop("user_id", 600), **kw
    )


class TestJoinRoute:
    async def test_member_gets_the_policy_grants_and_the_contract_ttl(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        headers, cid = _member(overlay_db)
        response = await client.post(_join_url(cid), headers=headers)
        assert response.status_code == 200
        body = await response.get_json()
        join = next(r for name, r, _ in fake.calls if name == "JoinRoom")
        assert (join.publish, join.subscribe) == (True, True)
        assert join.HasField("ttl_secs") and join.ttl_secs == 60 <= call_access.JOIN_TTL_MAX_SECS
        assert join.participant_id == user_uuid(600)
        assert [name for name, _, _ in fake.calls] == ["GetRoom", "JoinRoom"]
        # What a browser WHIP/WHEP client needs:
        assert body["token_type"] == "Bearer"
        assert body["token"] == "room-token"
        assert body["whip_url"].endswith(f"/rtc/rooms/{ROOM}/whip")
        assert body["whep_url"].endswith(f"/rtc/rooms/{ROOM}/whep")
        assert (body["publish"], body["subscribe"]) == (True, True)
        assert body["ice_servers"] == []  # TURN/STUN placeholder: empty until configured
        assert body["expires_at"] == 1700000900

    async def test_the_credential_is_not_cacheable(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        headers, cid = _member(overlay_db)
        response = await client.post(_join_url(cid), headers=headers)
        assert response.headers["Cache-Control"] == "no-store"

    async def test_a_locked_room_refuses_a_member_without_minting_a_token(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        headers, cid = _member(overlay_db)
        fake.seed_room(cid, locked=True)
        response = await client.post(_join_url(cid), headers=headers)
        assert response.status_code == 403
        error = (await response.get_json())["error"]
        assert (error["code"], error["message"]) == ("ROOM_ACCESS_DENIED", "room is locked")
        assert "JoinRoom" not in [name for name, _, _ in fake.calls]

    async def test_staff_are_not_refused_by_hub_apis_policy_for_a_locked_room(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        """hub-api asks for the join with full grants; it never answers ROOM_ACCESS_DENIED.

        Known downstream gap (reported on the PR): svc-streaming's `JoinRoom` refuses EVERY
        caller of a locked room because the request carries no actor role, so the fake -- which
        mirrors that -- still answers 403 FORBIDDEN here. When the server grows a staff bypass
        this request starts succeeding with no hub-api change.
        """
        headers, cid = user_headers(
            overlay_db, scope=SCOPE_WRITE, role="community-owner", user_id=601
        )
        fake.seed_room(cid, locked=True)
        response = await client.post(_join_url(cid), headers=headers)
        join = next(r for name, r, _ in fake.calls if name == "JoinRoom")  # the policy allowed it
        assert (join.publish, join.subscribe) == (True, True)
        if response.status_code != 200:
            assert (await response.get_json())["error"]["code"] == "FORBIDDEN"

    async def test_an_unknown_room_is_a_404_before_any_token(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        headers, cid = _member(overlay_db)
        fake.seed_room(999)  # ROOM exists, but in another community
        response = await client.post(_join_url(cid), headers=headers)
        assert response.status_code == 404
        assert [name for name, _, _ in fake.calls] == ["GetRoom"]

    async def test_configured_stun_servers_are_returned(
        self,
        client: Any,
        overlay_db: Any,
        fake: FakeRooms,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(
            "STREAMING_RTC_ICE_SERVERS", '[{"urls": ["stun:stun.example.org:3478"]}]'
        )
        headers, cid = _member(overlay_db)
        body = await (await client.post(_join_url(cid), headers=headers)).get_json()
        assert body["ice_servers"] == [{"urls": ["stun:stun.example.org:3478"]}]

    async def test_a_turn_config_is_refused_loudly_and_no_token_is_minted(
        self,
        client: Any,
        overlay_db: Any,
        fake: FakeRooms,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("STREAMING_RTC_ICE_SERVERS", '[{"urls": ["turn:relay.example.org"]}]')
        headers, cid = _member(overlay_db)
        response = await client.post(_join_url(cid), headers=headers)
        assert response.status_code == 503
        assert (await response.get_json())["error"]["code"] == "STREAMING_NOT_CONFIGURED"
        assert "JoinRoom" not in [name for name, _, _ in fake.calls]

    async def test_the_room_token_never_reaches_the_logs(
        self,
        client: Any,
        overlay_db: Any,
        fake: FakeRooms,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        headers, cid = _member(overlay_db)
        caplog.set_level(logging.DEBUG)
        body = await (await client.post(_join_url(cid), headers=headers)).get_json()
        assert body["token"]
        assert body["token"] not in caplog.text
        assert body["token_id"] not in caplog.text
        assert "Bearer room-token" not in caplog.text


class TestIceConfig:
    @pytest.mark.parametrize(
        "raw",
        [
            "not json",
            '{"urls": ["stun:a"]}',
            '[{"urls": []}]',
            '[{"urls": ["turn:relay.example.org"]}]',
            '[{"urls": ["turns:relay.example.org"]}]',
            '[{"urls": ["stun:a"], "username": "u", "credential": "c"}]',
            '[{"url": "stun:a"}]',
            "[" + ",".join(['{"urls": ["stun:a"]}'] * 9) + "]",
        ],
    )
    def test_malformed_or_turn_config_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv("STREAMING_RTC_ICE_SERVERS", raw)
        with pytest.raises(call_ice.IceConfigError):
            call_ice.ice_servers_from_env()

    @pytest.mark.parametrize("raw", ["", "   ", "[]"])
    def test_unset_or_empty_means_no_servers(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv("STREAMING_RTC_ICE_SERVERS", raw)
        assert call_ice.ice_servers_from_env() == []
