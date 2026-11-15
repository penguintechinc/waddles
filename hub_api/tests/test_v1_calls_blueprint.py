"""`blueprints/v1/calls.py` -- the M7 calls group (Streaming module).

Standalone Quart app registering both `calls_admin_bp` and
`member_voice_bp` against the `overlay_db` fixture (`tests/conftest.py`)
-- real JWTs, real pydal membership rows, `CallsProxyClient.request`
mocked (no real network I/O), matching `test_event_blueprint.py`'s own
`proxy_stub` pattern.

Fail-first proof (executed, not narrated), the token-takeover fix:
temporarily commented out the `await community_access.
require_community_member(...)` call inside `blueprints/v1/calls.py::
_require_member` (leaving only `require_scope`) -- all four
`test_member_route_non_member_is_403[...]` parametrizations went red
(200 instead of 403): an authenticated caller with NO membership in the
target community would have received a live (mocked, but a real one
downstream) LiveKit join token for ANY `community_id` supplied in the
URL -- exactly the token-minted-for-the-wrong-user takeover
`community_access.py`'s "Worse gap" docstring describes. Reverted; green
again. Separately, ran the same disable/confirm-red/revert cycle against
`_require_admin`'s `await community_access.require_community_admin(...)`
call -- all eleven `test_admin_route_non_member_is_403[...]` and
`test_admin_route_non_admin_member_is_403[...]` parametrizations went
red too, confirming the admin-surface IDOR fix independently of the
member-surface one.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from quart import Quart
from quart_schema import QuartSchema

import blueprints.v1.calls as calls_module
from blueprints.v1.calls import (
    SCOPE_ADMIN,
    SCOPE_READ,
    SCOPE_WRITE,
    calls_admin_bp,
    member_voice_bp,
)
from services.event_calendar_proxy import ProxyResult
from tests.conftest import (
    make_super_admin_token,
    make_user_token,
    seed_community,
    seed_membership,
    seed_super_admin,
)


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


@pytest.fixture
def proxy_stub(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace the module-level `_proxy_client.request` -- default: empty-body success relay."""
    stub = AsyncMock(return_value=ProxyResult(ok=True, status_code=200, body={}))
    monkeypatch.setattr(calls_module._proxy_client, "request", stub)
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
    ("GET", lambda cid: f"/api/v1/admin/{cid}/calls/rooms/room1", None),
    ("DELETE", lambda cid: f"/api/v1/admin/{cid}/calls/rooms/room1", None),
    ("POST", lambda cid: f"/api/v1/admin/{cid}/calls/rooms/room1/lock", None),
    ("POST", lambda cid: f"/api/v1/admin/{cid}/calls/rooms/room1/unlock", None),
    ("GET", lambda cid: f"/api/v1/admin/{cid}/calls/rooms/room1/participants", None),
    ("POST", lambda cid: f"/api/v1/admin/{cid}/calls/rooms/room1/kick", {"identity": "u1"}),
    ("POST", lambda cid: f"/api/v1/admin/{cid}/calls/rooms/room1/mute-all", None),
    ("GET", lambda cid: f"/api/v1/admin/{cid}/calls/rooms/room1/raised-hands", None),
    (
        "POST",
        lambda cid: f"/api/v1/admin/{cid}/calls/rooms/room1/acknowledge-hand",
        {"user_id": "u1"},
    ),
)

#: (method, path_fn(cid), json_body) -- every member-facing route.
_MEMBER_ROUTES: tuple[tuple[str, Any, dict[str, Any] | None], ...] = (
    ("GET", lambda cid: f"/api/v1/community/{cid}/interact/voice/rooms", None),
    ("POST", lambda cid: f"/api/v1/community/{cid}/interact/voice/rooms", {"room_name": "room1"}),
    ("POST", lambda cid: f"/api/v1/community/{cid}/interact/voice/rooms/room1/join", None),
    ("POST", lambda cid: f"/api/v1/community/{cid}/interact/voice/rooms/room1/leave", None),
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
        proxy_stub: AsyncMock,
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
        proxy_stub.assert_not_awaited()

    @pytest.mark.parametrize("method,path_fn,body", _ADMIN_ROUTES)
    async def test_admin_route_moderator_scopes_are_not_community_admin(
        self,
        client: Any,
        overlay_db: Any,
        proxy_stub: AsyncMock,
        method: str,
        path_fn: Any,
        body: dict[str, Any] | None,
    ) -> None:
        """Authorization is the `community:admin` scope: a moderator-kind member lacks it."""
        community_id = seed_community(overlay_db)
        seed_membership(overlay_db, community_id=community_id, user_id=703, role="moderator")
        headers = {"Authorization": f"Bearer {make_user_token(user_id=703, scope=SCOPE_ADMIN)}"}
        response = await client.open(
            path_fn(community_id), method=method, headers=headers, json=body
        )
        assert response.status_code == 403
        proxy_stub.assert_not_awaited()

    @pytest.mark.parametrize("method,path_fn,body", _ADMIN_ROUTES)
    async def test_admin_route_non_member_is_403(
        self,
        client: Any,
        overlay_db: Any,
        proxy_stub: AsyncMock,
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
        proxy_stub.assert_not_awaited()


class TestAdminRouteReshaping:
    async def test_get_call_rooms_relays_rooms(
        self, client: Any, overlay_db: Any, proxy_stub: AsyncMock
    ) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        proxy_stub.return_value = ProxyResult(
            ok=True, status_code=200, body={"rooms": [{"room_name": "room1"}]}
        )
        response = await client.get(f"/api/v1/admin/{community_id}/calls/rooms", headers=headers)
        assert response.status_code == 200
        body = await response.get_json()
        assert body == {"success": True, "rooms": [{"room_name": "room1"}]}

    async def test_get_call_rooms_downstream_404_is_empty_list(
        self, client: Any, overlay_db: Any, proxy_stub: AsyncMock
    ) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        proxy_stub.return_value = ProxyResult(ok=False, status_code=404, body=None)
        response = await client.get(f"/api/v1/admin/{community_id}/calls/rooms", headers=headers)
        assert response.status_code == 200
        assert (await response.get_json())["rooms"] == []

    async def test_get_call_rooms_downstream_error_is_masked_500(
        self, client: Any, overlay_db: Any, proxy_stub: AsyncMock
    ) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        proxy_stub.return_value = ProxyResult(
            ok=False, status_code=503, body={"error": "downstream unavailable"}
        )
        response = await client.get(f"/api/v1/admin/{community_id}/calls/rooms", headers=headers)
        assert response.status_code == 503
        body = await response.get_json()
        assert body == {"success": False, "error": "downstream unavailable"}

    async def test_create_call_room_defaults_max_participants(
        self, client: Any, overlay_db: Any, proxy_stub: AsyncMock
    ) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        proxy_stub.return_value = ProxyResult(ok=True, status_code=200, body={"room_name": "room1"})
        response = await client.post(
            f"/api/v1/admin/{community_id}/calls/rooms",
            headers=headers,
            json={"room_name": "room1"},
        )
        assert response.status_code == 201
        sent_json = proxy_stub.await_args.kwargs["json_body"]
        assert sent_json["max_participants"] == 100
        assert sent_json["community_id"] == community_id

    async def test_create_call_room_downstream_error(
        self, client: Any, overlay_db: Any, proxy_stub: AsyncMock
    ) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        proxy_stub.return_value = ProxyResult(
            ok=False, status_code=409, body={"error": "room already exists"}
        )
        response = await client.post(
            f"/api/v1/admin/{community_id}/calls/rooms",
            headers=headers,
            json={"room_name": "room1"},
        )
        assert response.status_code == 409
        assert (await response.get_json())["error"] == "room already exists"

    async def test_get_call_participants_downstream_404_is_empty_list(
        self, client: Any, overlay_db: Any, proxy_stub: AsyncMock
    ) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        proxy_stub.return_value = ProxyResult(ok=False, status_code=404, body=None)
        response = await client.get(
            f"/api/v1/admin/{community_id}/calls/rooms/room1/participants", headers=headers
        )
        assert response.status_code == 200
        assert (await response.get_json())["participants"] == []

    async def test_delete_call_room_message(
        self, client: Any, overlay_db: Any, proxy_stub: AsyncMock
    ) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        response = await client.delete(
            f"/api/v1/admin/{community_id}/calls/rooms/room1", headers=headers
        )
        assert response.status_code == 200
        assert (await response.get_json())["message"] == "Room deleted"

    async def test_get_raised_hands_downstream_404_is_empty_list(
        self, client: Any, overlay_db: Any, proxy_stub: AsyncMock
    ) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        proxy_stub.return_value = ProxyResult(ok=False, status_code=404, body=None)
        response = await client.get(
            f"/api/v1/admin/{community_id}/calls/rooms/room1/raised-hands", headers=headers
        )
        assert response.status_code == 200
        assert (await response.get_json())["raised_hands"] == []


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
        proxy_stub: AsyncMock,
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
        proxy_stub.assert_not_awaited()

    async def test_join_voice_room_plain_member_succeeds(
        self, client: Any, overlay_db: Any, proxy_stub: AsyncMock
    ) -> None:
        """A plain 'community-member' (not owner/admin) CAN join -- membership is enough."""
        headers, community_id = _plain_member(overlay_db, scope=SCOPE_WRITE)
        proxy_stub.return_value = ProxyResult(
            ok=True, status_code=200, body={"token": "lk-token", "url": "wss://livekit.example"}
        )
        response = await client.post(
            f"/api/v1/community/{community_id}/interact/voice/rooms/room1/join", headers=headers
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body == {"success": True, "token": "lk-token", "url": "wss://livekit.example"}

    async def test_join_voice_room_uses_jwt_user_id_not_request_body(
        self, client: Any, overlay_db: Any, proxy_stub: AsyncMock
    ) -> None:
        """The downstream `user_id` comes from the verified JWT `sub`, never a client-supplied one.

        A malicious body claiming a different `user_id` is simply ignored
        -- `join_voice_room` never reads `request.get_json()` at all.
        """
        community_id = seed_community(overlay_db)
        seed_membership(overlay_db, community_id=community_id, user_id=42, role="community-member")
        token = make_user_token(user_id=42, scope=SCOPE_WRITE)
        proxy_stub.return_value = ProxyResult(ok=True, status_code=200, body={"token": "t"})
        response = await client.post(
            f"/api/v1/community/{community_id}/interact/voice/rooms/room1/join",
            headers={"Authorization": f"Bearer {token}"},
            json={"user_id": "9999-attacker-supplied"},
        )
        assert response.status_code == 200
        sent_json = proxy_stub.await_args.kwargs["json_body"]
        assert sent_json["user_id"] == "42"


class TestSuperAdminBypass:
    async def test_admin_route_super_admin_bypasses_membership(
        self, client: Any, overlay_db: Any, proxy_stub: AsyncMock
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
        self, client: Any, overlay_db: Any, proxy_stub: AsyncMock
    ) -> None:
        community_id = seed_community(overlay_db)  # no membership row at all
        seed_super_admin(overlay_db, user_id=1)
        token = make_super_admin_token(user_id=1, scope=SCOPE_READ)
        response = await client.get(
            f"/api/v1/community/{community_id}/interact/voice/rooms",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200


class TestAdminRouteSuccessPaths:
    """Success-path reshaping for every remaining admin handler.

    `TestAdminRouteReshaping` above already covers `get_call_rooms`/
    `create_call_room`/`delete_call_room`/`raised-hands` 404.
    """

    async def test_get_call_room(self, client: Any, overlay_db: Any, proxy_stub: AsyncMock) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        proxy_stub.return_value = ProxyResult(
            ok=True, status_code=200, body={"room_name": "room1", "is_locked": False}
        )
        response = await client.get(
            f"/api/v1/admin/{community_id}/calls/rooms/room1", headers=headers
        )
        assert response.status_code == 200
        assert (await response.get_json()) == {
            "success": True,
            "room_name": "room1",
            "is_locked": False,
        }

    async def test_get_call_room_downstream_error(
        self, client: Any, overlay_db: Any, proxy_stub: AsyncMock
    ) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        proxy_stub.return_value = ProxyResult(ok=False, status_code=404, body=None)
        response = await client.get(
            f"/api/v1/admin/{community_id}/calls/rooms/room1", headers=headers
        )
        assert response.status_code == 404
        assert (await response.get_json())["success"] is False

    async def test_delete_call_room_downstream_error(
        self, client: Any, overlay_db: Any, proxy_stub: AsyncMock
    ) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        proxy_stub.return_value = ProxyResult(ok=False, status_code=404, body=None)
        response = await client.delete(
            f"/api/v1/admin/{community_id}/calls/rooms/room1", headers=headers
        )
        assert response.status_code == 404

    async def test_lock_call_room(
        self, client: Any, overlay_db: Any, proxy_stub: AsyncMock
    ) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        response = await client.post(
            f"/api/v1/admin/{community_id}/calls/rooms/room1/lock", headers=headers
        )
        assert response.status_code == 200
        assert (await response.get_json())["message"] == "Room locked"

    async def test_lock_call_room_downstream_error(
        self, client: Any, overlay_db: Any, proxy_stub: AsyncMock
    ) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        proxy_stub.return_value = ProxyResult(ok=False, status_code=500, body=None)
        response = await client.post(
            f"/api/v1/admin/{community_id}/calls/rooms/room1/lock", headers=headers
        )
        assert response.status_code == 500

    async def test_unlock_call_room(
        self, client: Any, overlay_db: Any, proxy_stub: AsyncMock
    ) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        response = await client.post(
            f"/api/v1/admin/{community_id}/calls/rooms/room1/unlock", headers=headers
        )
        assert response.status_code == 200
        assert (await response.get_json())["message"] == "Room unlocked"

    async def test_unlock_call_room_downstream_error(
        self, client: Any, overlay_db: Any, proxy_stub: AsyncMock
    ) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        proxy_stub.return_value = ProxyResult(ok=False, status_code=500, body=None)
        response = await client.post(
            f"/api/v1/admin/{community_id}/calls/rooms/room1/unlock", headers=headers
        )
        assert response.status_code == 500

    async def test_get_call_participants(
        self, client: Any, overlay_db: Any, proxy_stub: AsyncMock
    ) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        proxy_stub.return_value = ProxyResult(
            ok=True, status_code=200, body={"participants": [{"identity": "u1"}]}
        )
        response = await client.get(
            f"/api/v1/admin/{community_id}/calls/rooms/room1/participants", headers=headers
        )
        assert response.status_code == 200
        assert (await response.get_json())["participants"] == [{"identity": "u1"}]

    async def test_get_call_participants_downstream_error(
        self, client: Any, overlay_db: Any, proxy_stub: AsyncMock
    ) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        proxy_stub.return_value = ProxyResult(ok=False, status_code=500, body=None)
        response = await client.get(
            f"/api/v1/admin/{community_id}/calls/rooms/room1/participants", headers=headers
        )
        assert response.status_code == 500

    async def test_kick_call_participant(
        self, client: Any, overlay_db: Any, proxy_stub: AsyncMock
    ) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        response = await client.post(
            f"/api/v1/admin/{community_id}/calls/rooms/room1/kick",
            headers=headers,
            json={"identity": "u1"},
        )
        assert response.status_code == 200
        assert (await response.get_json())["message"] == "Participant removed"
        assert proxy_stub.await_args.kwargs["json_body"]["identity"] == "u1"

    async def test_kick_call_participant_downstream_error(
        self, client: Any, overlay_db: Any, proxy_stub: AsyncMock
    ) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        proxy_stub.return_value = ProxyResult(ok=False, status_code=500, body=None)
        response = await client.post(
            f"/api/v1/admin/{community_id}/calls/rooms/room1/kick",
            headers=headers,
            json={"identity": "u1"},
        )
        assert response.status_code == 500

    async def test_mute_all_call_participants(
        self, client: Any, overlay_db: Any, proxy_stub: AsyncMock
    ) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        response = await client.post(
            f"/api/v1/admin/{community_id}/calls/rooms/room1/mute-all", headers=headers
        )
        assert response.status_code == 200
        assert (await response.get_json())["message"] == "All participants muted"

    async def test_mute_all_call_participants_downstream_error(
        self, client: Any, overlay_db: Any, proxy_stub: AsyncMock
    ) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        proxy_stub.return_value = ProxyResult(ok=False, status_code=500, body=None)
        response = await client.post(
            f"/api/v1/admin/{community_id}/calls/rooms/room1/mute-all", headers=headers
        )
        assert response.status_code == 500

    async def test_get_raised_hands(
        self, client: Any, overlay_db: Any, proxy_stub: AsyncMock
    ) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        proxy_stub.return_value = ProxyResult(
            ok=True, status_code=200, body={"raised_hands": [{"user_id": "u1"}]}
        )
        response = await client.get(
            f"/api/v1/admin/{community_id}/calls/rooms/room1/raised-hands", headers=headers
        )
        assert response.status_code == 200
        assert (await response.get_json())["raised_hands"] == [{"user_id": "u1"}]

    async def test_get_raised_hands_downstream_error(
        self, client: Any, overlay_db: Any, proxy_stub: AsyncMock
    ) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        proxy_stub.return_value = ProxyResult(ok=False, status_code=500, body=None)
        response = await client.get(
            f"/api/v1/admin/{community_id}/calls/rooms/room1/raised-hands", headers=headers
        )
        assert response.status_code == 500

    async def test_acknowledge_hand(
        self, client: Any, overlay_db: Any, proxy_stub: AsyncMock
    ) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        response = await client.post(
            f"/api/v1/admin/{community_id}/calls/rooms/room1/acknowledge-hand",
            headers=headers,
            json={"user_id": "u1"},
        )
        assert response.status_code == 200
        assert (await response.get_json())["message"] == "Hand acknowledged"

    async def test_acknowledge_hand_downstream_error(
        self, client: Any, overlay_db: Any, proxy_stub: AsyncMock
    ) -> None:
        headers, community_id = _owner(overlay_db, scope=SCOPE_ADMIN)
        proxy_stub.return_value = ProxyResult(ok=False, status_code=500, body=None)
        response = await client.post(
            f"/api/v1/admin/{community_id}/calls/rooms/room1/acknowledge-hand",
            headers=headers,
            json={"user_id": "u1"},
        )
        assert response.status_code == 500


class TestMemberRouteSuccessPaths:
    async def test_list_voice_rooms(
        self, client: Any, overlay_db: Any, proxy_stub: AsyncMock
    ) -> None:
        headers, community_id = _plain_member(overlay_db, scope=SCOPE_READ)
        proxy_stub.return_value = ProxyResult(
            ok=True, status_code=200, body={"rooms": [{"room_name": "room1"}]}
        )
        response = await client.get(
            f"/api/v1/community/{community_id}/interact/voice/rooms", headers=headers
        )
        assert response.status_code == 200
        assert (await response.get_json())["rooms"] == [{"room_name": "room1"}]

    async def test_create_ad_hoc_voice_room(
        self, client: Any, overlay_db: Any, proxy_stub: AsyncMock
    ) -> None:
        headers, community_id = _plain_member(overlay_db, scope=SCOPE_WRITE)
        proxy_stub.return_value = ProxyResult(ok=True, status_code=200, body={"room_name": "room1"})
        response = await client.post(
            f"/api/v1/community/{community_id}/interact/voice/rooms",
            headers=headers,
            json={"room_name": "room1"},
        )
        assert response.status_code == 201

    async def test_join_voice_room_downstream_error(
        self, client: Any, overlay_db: Any, proxy_stub: AsyncMock
    ) -> None:
        headers, community_id = _plain_member(overlay_db, scope=SCOPE_WRITE)
        proxy_stub.return_value = ProxyResult(ok=False, status_code=409, body=None)
        response = await client.post(
            f"/api/v1/community/{community_id}/interact/voice/rooms/room1/join", headers=headers
        )
        assert response.status_code == 409

    async def test_leave_voice_room(
        self, client: Any, overlay_db: Any, proxy_stub: AsyncMock
    ) -> None:
        headers, community_id = _plain_member(overlay_db, scope=SCOPE_WRITE)
        response = await client.post(
            f"/api/v1/community/{community_id}/interact/voice/rooms/room1/leave", headers=headers
        )
        assert response.status_code == 200
        assert (await response.get_json())["message"] == "Left room"

    async def test_leave_voice_room_downstream_error(
        self, client: Any, overlay_db: Any, proxy_stub: AsyncMock
    ) -> None:
        headers, community_id = _plain_member(overlay_db, scope=SCOPE_WRITE)
        proxy_stub.return_value = ProxyResult(ok=False, status_code=500, body=None)
        response = await client.post(
            f"/api/v1/community/{community_id}/interact/voice/rooms/room1/leave", headers=headers
        )
        assert response.status_code == 500
