"""Security regressions for the calls routes: tenancy/community scoping and failure honesty.

- Cross-community access is a 404 (the room is `(community_id, room_id)`-scoped downstream),
  never a leak, for join, list, get, delete, kick and the hands/participants reads.
- `community_id` comes from the URL path -- verified by the membership check -- never from a
  request body; participant ids in requests are UUIDs, names are refused.
- Upstream failures are explicit statuses (the retired 404 -> empty-list masking is gone), and
  a missing client configuration is a loud 503, not a 500 or an empty success.
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

import grpc
import pytest

from blueprints.v1.calls import SCOPE_ADMIN, SCOPE_READ, SCOPE_WRITE
from services.calls_proxy import RoomsClient
from tests.conftest import OTHER_TENANT_SLUG, make_user_token, seed_community, seed_membership
from tests.test_calls_support import (  # noqa: F401  (fixtures are consumed by name)
    PARTICIPANT,
    ROOM,
    FakeRooms,
    Issuer,
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

ROOM_OTHER = "3c2a1b0f-9e8d-4c7b-8a6f-5e4d3c2b1a09"
VICTIM = "9c1d2e3f-0a1b-4c2d-8e3f-4a5b6c7d8e9f"


def _json_headers(headers: dict[str, str]) -> dict[str, str]:
    """Headers for a raw JSON body.

    The quart-schema test client's `json=` dump path crashes on some pydantic-core builds (see
    `services/dto_response.py`), so these tests send raw JSON.
    """
    return {**headers, "Content-Type": "application/json"}


def _two_communities(overlay_db: Any, fake: FakeRooms) -> tuple[dict[str, str], int, int]:
    """Admin+member of community B only; the real room ROOM lives in community A."""
    cid_a = seed_community(overlay_db, name="community-a")
    fake.seed_room(cid_a)  # ROOM belongs to A
    fake.seed_participant(cid_a, VICTIM)
    headers_b, cid_b = user_headers(
        overlay_db,
        scope=f"{SCOPE_ADMIN} {SCOPE_READ} {SCOPE_WRITE}",
        role="community-owner",
        user_id=902,
    )
    return headers_b, cid_a, cid_b


class TestCrossCommunityIsolation:
    """B's owner addressing A's room through B's own (authorised) URL gets a 404."""

    @pytest.mark.parametrize(
        "method,suffix,body",
        [
            ("GET", "", None),
            ("DELETE", "", None),
            ("GET", "/participants", None),
            ("POST", "/kick", {"identity": VICTIM}),
            ("POST", "/mute", {"identity": VICTIM}),
            ("POST", "/lock", None),
            ("POST", "/mute-all", None),
            ("GET", "/raised-hands", None),
            ("POST", "/reset-hands", None),
        ],
    )
    async def test_admin_operations_on_another_communitys_room_are_404(
        self,
        client: Any,
        overlay_db: Any,
        fake: FakeRooms,
        method: str,
        suffix: str,
        body: Any,
    ) -> None:
        headers, cid_a, cid_b = _two_communities(overlay_db, fake)
        response = await client.open(
            f"/api/v1/admin/{cid_b}/calls/rooms/{ROOM}{suffix}",
            method=method,
            headers=headers,
            json=body,
        )
        assert response.status_code == 404, await response.get_json()
        assert (await response.get_json())["error"]["code"] == "NOT_FOUND"
        # A's room is untouched: still there, unlocked, participant not kicked.
        state = fake.rooms[(str(cid_a), ROOM)]
        assert state["locked"] is False
        assert VICTIM in state["members"]
        assert all(req.community_id == str(cid_b) for _, req, _ in fake.calls)

    @pytest.mark.parametrize("action", ["join", "leave", "raise-hand", "lower-hand"])
    async def test_member_operations_on_another_communitys_room_are_404(
        self, client: Any, overlay_db: Any, fake: FakeRooms, action: str
    ) -> None:
        headers, cid_a, cid_b = _two_communities(overlay_db, fake)
        response = await client.post(
            f"/api/v1/community/{cid_b}/interact/voice/rooms/{ROOM}/{action}", headers=headers
        )
        assert response.status_code == 404
        assert "token" not in await response.get_json()  # no join credential leaked

    async def test_listing_shows_only_the_callers_community(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        headers, cid_a, cid_b = _two_communities(overlay_db, fake)
        fake.seed_room(cid_b, ROOM_OTHER, label="b-room")
        body = await (
            await client.get(f"/api/v1/admin/{cid_b}/calls/rooms", headers=headers)
        ).get_json()
        assert [r["room_id"] for r in body["rooms"]] == [ROOM_OTHER]
        assert body["rooms"][0]["label"] == "b-room"

    async def test_a_url_community_the_caller_is_not_in_is_403_before_any_rpc(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        headers, cid_a, cid_b = _two_communities(overlay_db, fake)
        for path in (
            f"/api/v1/admin/{cid_a}/calls/rooms/{ROOM}/participants",
            f"/api/v1/community/{cid_a}/interact/voice/rooms",
        ):
            response = await client.get(path, headers=headers)
            assert response.status_code == 403
        assert fake.calls == []

    async def test_a_community_from_another_tenant_is_403(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        foreign = seed_community(overlay_db, tenant_slug=OTHER_TENANT_SLUG, name="foreign")
        seed_user(overlay_db, 910)
        seed_membership(overlay_db, community_id=foreign, user_id=910, role="community-owner")
        token = make_user_token(user_id=910, scope=SCOPE_ADMIN)  # token is for the default tenant
        response = await client.get(
            f"/api/v1/admin/{foreign}/calls/rooms", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 403
        assert fake.calls == []


class TestCommunityAndParticipantComeFromVerifiedAuth:
    async def test_community_id_in_a_body_is_ignored_on_create(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        headers, cid_a, cid_b = _two_communities(overlay_db, fake)
        response = await client.post(
            f"/api/v1/admin/{cid_b}/calls/rooms",
            headers=_json_headers(headers),
            data=json.dumps({"room_name": "mine", "community_id": cid_a, "tenant": "evil"}),
        )
        assert response.status_code == 201
        _, request, _ = fake.calls[-1]
        assert request.community_id == str(cid_b)
        assert str(cid_a) not in {request.community_id}

    async def test_a_body_cannot_redirect_kick_or_join_to_another_community(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        headers, cid_a, cid_b = _two_communities(overlay_db, fake)
        await client.post(
            f"/api/v1/admin/{cid_b}/calls/rooms/{ROOM}/kick",
            headers=_json_headers(headers),
            data=json.dumps({"identity": VICTIM, "community_id": cid_a}),
        )
        fake.seed_room(cid_b, ROOM_OTHER)  # B own room: the join reaches the RPC
        record_room(overlay_db, cid_b, ROOM_OTHER)
        await client.post(
            f"/api/v1/community/{cid_b}/interact/voice/rooms/{ROOM_OTHER}/join",
            headers=_json_headers(headers),
            data=json.dumps({"community_id": cid_a, "participant_id": VICTIM}),
        )
        assert {req.community_id for _, req, _ in fake.calls} == {str(cid_b)}
        join = next(req for name, req, _ in fake.calls if name == "JoinRoom")
        assert join.participant_id == pid(cid_b, 902)  # the caller, not the body's participant
        assert fake.rooms[(str(cid_a), ROOM)]["members"].keys() == {VICTIM}

    @pytest.mark.parametrize(
        "path_suffix,body",
        [
            ("kick", {"identity": "alice"}),
            ("kick", {"identity": "12345"}),
            ("mute", {"identity": "alice@example.com"}),
            ("unmute", {"identity": "alice"}),
            ("acknowledge-hand", {"user_id": "alice"}),
        ],
    )
    async def test_names_and_raw_ids_are_refused_as_participant_ids(
        self, client: Any, overlay_db: Any, fake: FakeRooms, path_suffix: str, body: Any
    ) -> None:
        headers, _, cid_b = _two_communities(overlay_db, fake)
        response = await client.post(
            f"/api/v1/admin/{cid_b}/calls/rooms/{ROOM}/{path_suffix}", headers=headers, json=body
        )
        assert response.status_code == 400
        assert (await response.get_json())["error"]["code"] == "INVALID_IDENTITY"
        assert fake.calls == []

    async def test_room_must_be_addressed_by_uuid_legacy_names_are_refused(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        headers, _, cid_b = _two_communities(overlay_db, fake)
        for name in ("community_1_general", "general", "0b6f3e1a"):
            response = await client.get(
                f"/api/v1/admin/{cid_b}/calls/rooms/{name}", headers=headers
            )
            assert response.status_code == 400
            assert (await response.get_json())["error"]["code"] == "INVALID_ROOM_ID"
        assert fake.calls == []

    async def test_participant_ids_in_responses_are_uuids_only(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        headers, cid_a, cid_b = _two_communities(overlay_db, fake)
        fake.seed_room(cid_b, ROOM_OTHER)
        fake.seed_participant(cid_b, pid(cid_b, 902), ROOM_OTHER)
        body = await (
            await client.get(
                f"/api/v1/admin/{cid_b}/calls/rooms/{ROOM_OTHER}/participants", headers=headers
            )
        ).get_json()
        assert body["participants"]
        for p in body["participants"]:
            assert str(uuid.UUID(p["participant_id"])) == p["participant_id"]


class TestFailuresAreHonest:
    """The retired proxy turned upstream 404s into empty lists; nothing may do that now."""

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/admin/{cid}/calls/rooms",
            "/api/v1/admin/{cid}/calls/rooms/" + ROOM + "/participants",
            "/api/v1/admin/{cid}/calls/rooms/" + ROOM + "/raised-hands",
            "/api/v1/community/{cid}/interact/voice/rooms",
        ],
    )
    @pytest.mark.parametrize(
        "code,http",
        [
            (grpc.StatusCode.NOT_FOUND, 404),
            (grpc.StatusCode.PERMISSION_DENIED, 403),
            (grpc.StatusCode.UNAVAILABLE, 503),
            (grpc.StatusCode.DEADLINE_EXCEEDED, 504),
        ],
    )
    async def test_read_failures_are_never_an_empty_success(
        self,
        client: Any,
        overlay_db: Any,
        fake: FakeRooms,
        path: str,
        code: grpc.StatusCode,
        http: int,
    ) -> None:
        headers, cid_a, cid_b = _two_communities(overlay_db, fake)
        for rpc in ("ListRooms", "ListParticipants", "ListHands"):
            fake.fail[rpc] = (code, "x")
        response = await client.get(path.format(cid=cid_b), headers=headers)
        assert response.status_code == http
        body = await response.get_json()
        assert body["success"] is False
        assert "rooms" not in body and "participants" not in body and "raised_hands" not in body

    async def test_unavailable_is_marked_retryable(
        self, client: Any, overlay_db: Any, fake: FakeRooms
    ) -> None:
        headers, _, cid_b = _two_communities(overlay_db, fake)
        fake.fail["ListRooms"] = (grpc.StatusCode.UNAVAILABLE, "x")
        body = await (
            await client.get(f"/api/v1/admin/{cid_b}/calls/rooms", headers=headers)
        ).get_json()
        assert body["error"]["code"] == "STREAMING_UNAVAILABLE"
        assert body["error"]["details"] == {"retryable": True}

    async def test_unconfigured_client_is_a_loud_503_with_an_error_log(
        self,
        client: Any,
        overlay_db: Any,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import blueprints.v1.calls as calls_module

        for name in (
            "STREAMING_RTC_GRPC_TARGET",
            "STREAMING_RTC_GRPC_CA_FILE",
            "STREAMING_RTC_GRPC_CLIENT_CERT_FILE",
            "STREAMING_RTC_GRPC_CLIENT_KEY_FILE",
            "MODULE_RTC_URL",
            "STREAMING_RTC_URL",
        ):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("MODULE_RTC_URL", "http://legacy-go-module:8093")  # must be ignored
        monkeypatch.setattr(
            calls_module,
            "_client",
            RoomsClient(issuer_provider=lambda: Issuer()),  # type: ignore[arg-type,return-value]
        )
        headers, cid = user_headers(
            overlay_db, scope=SCOPE_ADMIN, role="community-owner", user_id=920
        )
        caplog.set_level(logging.ERROR)
        response = await client.get(f"/api/v1/admin/{cid}/calls/rooms", headers=headers)
        assert response.status_code == 503
        body = await response.get_json()
        assert body["success"] is False
        assert body["error"]["code"] == "STREAMING_NOT_CONFIGURED"
        assert "rooms" not in body
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert errors and "STREAMING_RTC_GRPC_TARGET" in errors[0].getMessage()

    async def test_missing_service_jwt_issuer_is_a_502_not_a_success(
        self, client: Any, overlay_db: Any, fake: FakeRooms, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import blueprints.v1.calls as calls_module

        monkeypatch.setattr(calls_module._client, "_issuer_provider", lambda: None)
        headers, cid = user_headers(
            overlay_db, scope=SCOPE_ADMIN, role="community-owner", user_id=921
        )
        response = await client.get(f"/api/v1/admin/{cid}/calls/rooms", headers=headers)
        assert response.status_code == 502
        assert (await response.get_json())["error"]["code"] == "STREAMING_AUTH_NOT_CONFIGURED"
        assert fake.calls == []


class TestNoLegacyRemnants:
    def test_no_module_rtc_fallback_or_rest_remnants_in_the_calls_code(self) -> None:
        root = Path(__file__).resolve().parents[1]
        scanned = [
            root / "services/calls_proxy.py",
            root / "services/call_participants.py",
            root / "blueprints/v1/calls.py",
        ]
        assert all(p.is_file() for p in scanned)  # denominator: 3 files examined
        for path in scanned:
            text = path.read_text()
            for needle in (
                "MODULE_RTC_URL",
                "STREAMING_RTC_URL",
                "StreamingUrlNotConfiguredError",
                "httpx",
            ):
                assert needle not in text, (path.name, needle)
