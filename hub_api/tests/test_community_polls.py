"""`blueprints/v1/community_polls.py` -- pure reverse-proxy to `core-engagement`.

Auth/tenant gating is exercised against the real chain (no mocking
needed -- it fails before the proxy call). The proxy call itself is
monkeypatched at `services.community_engagement_proxy` (matching
`writing-python-tests` skill: mock external I/O, not the endpoint under
test) so these tests never make a real network call.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.community_polls import polls_bp
from services import community_engagement_proxy as proxy
from tests.community_ids import UNKNOWN_PUBLIC_ID, cpath, install_community_ids


@pytest.fixture
def app(community_db: Any) -> Quart:
    dal, _ = community_db
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.config["dal"] = dal
    install_community_ids(quart_app, dal)
    quart_app.register_blueprint(polls_bp)
    return quart_app


@pytest.fixture(autouse=True)
def _engagement_rows(community_db: Any) -> Any:
    """Poll 5 belongs to the fixture community; poll 6 to a DIFFERENT community."""
    dal, community_id = community_db
    other = dal.communities.insert(name="other-community", tenant_id=1)
    dal.community_polls.insert(id=5, community_id=community_id)
    dal.community_polls.insert(id=6, community_id=other)
    dal.commit()


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


@pytest.fixture(autouse=True)
def _feature_enabled_default_on(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Default the `community.polls` two-gate Feature flag ON for every test in this file."""
    import blueprints.v1.community_polls as polls_module

    monkeypatch.setattr(polls_module, "feature_enabled", AsyncMock(return_value=True))


class TestScopeAndTenant:
    async def test_wrong_scope_is_403(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        dal, community_id = community_db
        response = await client.get(
            cpath(dal, "/api/v1/admin/{community_id}/polls", community_id),
            headers=auth_headers(scope="community.polls:write"),
        )
        assert response.status_code == 403

    async def test_unknown_community_is_404(self, client: Any, auth_headers: Any) -> None:
        response = await client.get(
            f"/api/v1/admin/{UNKNOWN_PUBLIC_ID}/polls",
            headers=auth_headers(scope="community.polls:read"),
        )
        assert response.status_code == 404


class TestProxyPassthrough:
    async def test_list_polls_forwards_to_engagement_service(
        self, client: Any, auth_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        dal, community_id = community_db
        captured: dict[str, Any] = {}

        async def fake_get_polls(cid: int, authorization: str | None) -> tuple[dict[str, Any], int]:
            captured["community_id"] = cid
            captured["authorization"] = authorization
            return {"success": True, "polls": [{"id": 1, "title": "Best game?"}]}, 200

        monkeypatch.setattr(proxy, "get_polls", fake_get_polls)

        response = await client.get(
            cpath(dal, "/api/v1/admin/{community_id}/polls", community_id),
            headers=auth_headers(scope="community.polls:read"),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body == {"success": True, "polls": [{"id": 1, "title": "Best game?"}]}
        assert captured["community_id"] == community_id
        assert captured["authorization"].startswith("Bearer ")

    async def test_engagement_service_unavailable_returns_502(
        self, client: Any, auth_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        dal, community_id = community_db

        async def fake_get_polls(cid: int, authorization: str | None) -> tuple[dict[str, Any], int]:
            return {"success": False, "error": "core-engagement unavailable"}, 502

        monkeypatch.setattr(proxy, "get_polls", fake_get_polls)

        response = await client.get(
            cpath(dal, "/api/v1/admin/{community_id}/polls", community_id),
            headers=auth_headers(scope="community.polls:read"),
        )
        assert response.status_code == 502

    async def test_get_single_poll_forwards(
        self, client: Any, auth_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        dal, community_id = community_db

        async def fake_get_poll(
            cid: int, pid: int, authorization: str | None
        ) -> tuple[dict[str, Any], int]:
            return {"success": True, "poll": {"id": pid, "title": "x"}}, 200

        monkeypatch.setattr(proxy, "get_poll", fake_get_poll)
        response = await client.get(
            cpath(dal, "/api/v1/admin/{community_id}/polls/5", community_id),
            headers=auth_headers(scope="community.polls:read"),
        )
        assert response.status_code == 200
        assert (await response.get_json())["poll"]["id"] == 5

    async def test_create_poll_forwards(
        self, client: Any, auth_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        import json as json_module

        dal, community_id = community_db

        async def fake_create_poll(
            cid: int, payload: dict[str, Any], authorization: str | None
        ) -> tuple[dict[str, Any], int]:
            return {"success": True, "poll": {"id": 1, "title": payload["title"]}}, 200

        monkeypatch.setattr(proxy, "create_poll", fake_create_poll)
        response = await client.post(
            cpath(dal, "/api/v1/admin/{community_id}/polls", community_id),
            headers={
                **auth_headers(scope="community.polls:write"),
                "Content-Type": "application/json",
            },
            data=json_module.dumps({"title": "Best OS?", "options": ["Linux", "BSD"]}),
        )
        assert response.status_code == 200
        assert (await response.get_json())["poll"]["title"] == "Best OS?"

    async def test_delete_poll_forwards(
        self, client: Any, auth_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        dal, community_id = community_db

        async def fake_delete_poll(
            cid: int, pid: int, authorization: str | None
        ) -> tuple[dict[str, Any], int]:
            return {"success": True, "message": "Poll deleted"}, 200

        monkeypatch.setattr(proxy, "delete_poll", fake_delete_poll)
        response = await client.delete(
            cpath(dal, "/api/v1/admin/{community_id}/polls/5", community_id),
            headers=auth_headers(scope="community.polls:write"),
        )
        assert response.status_code == 200
        assert (await response.get_json())["message"] == "Poll deleted"


class TestPollOwnership:
    """regression: a numeric poll id from another community must 404 BEFORE the proxy runs."""

    @pytest.mark.parametrize("method", ["GET", "DELETE"])
    async def test_foreign_poll_is_404_and_never_proxied(
        self, client: Any, auth_headers: Any, community_db: Any, monkeypatch: Any, method: str
    ) -> None:
        dal, community_id = community_db
        proxied = AsyncMock(return_value=({"success": True}, 200))
        monkeypatch.setattr(proxy, "get_poll", proxied)
        monkeypatch.setattr(proxy, "delete_poll", proxied)
        scope = "community.polls:read" if method == "GET" else "community.polls:write"
        response = await client.open(
            cpath(dal, "/api/v1/admin/{community_id}/polls/6", community_id),
            method=method,
            headers=auth_headers(scope=scope),
        )
        assert response.status_code == 404
        proxied.assert_not_called()

    async def test_unknown_poll_is_404(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        dal, community_id = community_db
        response = await client.get(
            cpath(dal, "/api/v1/admin/{community_id}/polls/9999", community_id),
            headers=auth_headers(scope="community.polls:read"),
        )
        assert response.status_code == 404


pytestmark = pytest.mark.usefixtures("bypass_route_guards")
