"""`blueprints/v1/community_announcements.py` -- CRUD + publish/pin/archive/broadcast.

Uses `data=`/explicit content-type for write bodies (see
`test_community_activity.py`'s comment on the quart-schema test-client
`json=` pre-serialization bug this sidesteps).
"""

from __future__ import annotations

import json as json_module
from typing import Any
from unittest.mock import AsyncMock

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.community_announcements import announcements_bp
from tests.community_ids import UNKNOWN_PUBLIC_ID, install_community_ids, public_id


@pytest.fixture
def app(community_db: Any) -> Quart:
    dal, _ = community_db
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.config["dal"] = dal
    install_community_ids(quart_app, dal)
    quart_app.register_blueprint(announcements_bp)
    return quart_app


_DB: list[Any] = []


@pytest.fixture(autouse=True)
def _bind_db(community_db: Any) -> Any:
    """Expose the test DB to `cu` so seeded numeric ids map to community UUIDs in URLs."""
    _DB[:] = [community_db[0]]
    yield
    _DB.clear()


def cu(community_id: int) -> str:
    """Public UUID of a seeded community (client-facing routes never take the numeric id)."""
    return public_id(_DB[0], community_id)


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


@pytest.fixture(autouse=True)
def _feature_enabled_default_on(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Default the `community.announcements` two-gate Feature flag ON for this file's tests."""
    import blueprints.v1.community_announcements as announcements_module

    monkeypatch.setattr(announcements_module, "feature_enabled", AsyncMock(return_value=True))


async def _post_json(
    client: Any, path: str, headers: dict[str, str], payload: dict[str, Any]
) -> Any:
    return await client.post(
        path,
        headers={**headers, "Content-Type": "application/json"},
        data=json_module.dumps(payload),
    )


class TestScopeAndTenant:
    async def test_wrong_scope_is_403(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await client.get(
            f"/api/v1/admin/{cu(community_id)}/announcements",
            headers=auth_headers(scope="community.announcements:write"),
        )
        assert response.status_code == 403

    async def test_unknown_community_is_404(self, client: Any, auth_headers: Any) -> None:
        response = await client.get(
            f"/api/v1/admin/{UNKNOWN_PUBLIC_ID}/announcements",
            headers=auth_headers(scope="community.announcements:read"),
        )
        assert response.status_code == 404


class TestCreateAndLifecycle:
    async def test_create_requires_title_and_content(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await _post_json(
            client,
            f"/api/v1/admin/{cu(community_id)}/announcements",
            auth_headers(scope="community.announcements:write", user_id="1"),
            {"title": "", "content": "body"},
        )
        assert response.status_code == 400

    async def test_full_create_publish_pin_flow(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        headers = auth_headers(scope="community.announcements:write", user_id="1")

        create_resp = await _post_json(
            client,
            f"/api/v1/admin/{cu(community_id)}/announcements",
            headers,
            {"title": "Server maintenance", "content": "Downtime tonight", "status": "draft"},
        )
        assert create_resp.status_code == 201
        created = (await create_resp.get_json())["data"]
        assert created["status"] == "draft"
        assert created["is_pinned"] is False
        announcement_id = created["id"]

        publish_resp = await client.post(
            f"/api/v1/admin/{cu(community_id)}/announcements/{announcement_id}/publish",
            headers=headers,
        )
        assert publish_resp.status_code == 200
        published = (await publish_resp.get_json())["data"]
        assert published["status"] == "published"
        assert published["published_at"] is not None

        pin_resp = await client.put(
            f"/api/v1/admin/{cu(community_id)}/announcements/{announcement_id}/pin", headers=headers
        )
        assert pin_resp.status_code == 200
        assert (await pin_resp.get_json())["data"]["is_pinned"] is True

        list_resp = await client.get(
            f"/api/v1/admin/{cu(community_id)}/announcements",
            headers=auth_headers(scope="community.announcements:read"),
        )
        assert list_resp.status_code == 200
        list_body = await list_resp.get_json()
        assert list_body["pagination"]["total"] == 1
        assert list_body["data"][0]["id"] == announcement_id

    async def test_get_missing_announcement_is_404(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await client.get(
            f"/api/v1/admin/{cu(community_id)}/announcements/9999",
            headers=auth_headers(scope="community.announcements:read"),
        )
        assert response.status_code == 404

    async def test_create_validation_errors(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        headers = auth_headers(scope="community.announcements:write", user_id="1")

        long_title = await _post_json(
            client,
            f"/api/v1/admin/{cu(community_id)}/announcements",
            headers,
            {"title": "x" * 256, "content": "body"},
        )
        assert long_title.status_code == 400

        no_content = await _post_json(
            client,
            f"/api/v1/admin/{cu(community_id)}/announcements",
            headers,
            {"title": "ok", "content": ""},
        )
        assert no_content.status_code == 400

        long_content = await _post_json(
            client,
            f"/api/v1/admin/{cu(community_id)}/announcements",
            headers,
            {"title": "ok", "content": "x" * 2001},
        )
        assert long_content.status_code == 400

        bad_type = await _post_json(
            client,
            f"/api/v1/admin/{cu(community_id)}/announcements",
            headers,
            {"title": "ok", "content": "body", "announcement_type": "not-a-type"},
        )
        assert bad_type.status_code == 400

        bad_status = await _post_json(
            client,
            f"/api/v1/admin/{cu(community_id)}/announcements",
            headers,
            {"title": "ok", "content": "body", "status": "not-a-status"},
        )
        assert bad_status.status_code == 400

    async def test_update_announcement_full_flow(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        headers = auth_headers(scope="community.announcements:write", user_id="1")
        create_resp = await _post_json(
            client,
            f"/api/v1/admin/{cu(community_id)}/announcements",
            headers,
            {"title": "Original", "content": "original body"},
        )
        announcement_id = (await create_resp.get_json())["data"]["id"]

        update_resp = await client.put(
            f"/api/v1/admin/{cu(community_id)}/announcements/{announcement_id}",
            headers={**headers, "Content-Type": "application/json"},
            data=json_module.dumps(
                {
                    "title": "Updated",
                    "content": "updated body",
                    "announcement_type": "important",
                    "is_pinned": True,
                    "status": "published",
                }
            ),
        )
        assert update_resp.status_code == 200
        updated = (await update_resp.get_json())["data"]
        assert updated["title"] == "Updated"
        assert updated["announcement_type"] == "important"
        assert updated["is_pinned"] is True
        assert updated["status"] == "published"
        assert updated["published_at"] is not None

        missing_resp = await client.put(
            f"/api/v1/admin/{cu(community_id)}/announcements/9999",
            headers={**headers, "Content-Type": "application/json"},
            data=json_module.dumps({"title": "x"}),
        )
        assert missing_resp.status_code == 404

    async def test_update_validation_errors(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        headers = auth_headers(scope="community.announcements:write", user_id="1")
        create_resp = await _post_json(
            client,
            f"/api/v1/admin/{cu(community_id)}/announcements",
            headers,
            {"title": "Original", "content": "original body"},
        )
        announcement_id = (await create_resp.get_json())["data"]["id"]

        for payload in [
            {"title": ""},
            {"title": "x" * 256},
            {"content": ""},
            {"content": "x" * 2001},
            {"announcement_type": "not-a-type"},
            {"status": "not-a-status"},
        ]:
            response = await client.put(
                f"/api/v1/admin/{cu(community_id)}/announcements/{announcement_id}",
                headers={**headers, "Content-Type": "application/json"},
                data=json_module.dumps(payload),
            )
            assert response.status_code == 400, payload

    async def test_delete_unpin_archive_flow(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        headers = auth_headers(scope="community.announcements:write", user_id="1")
        create_resp = await _post_json(
            client,
            f"/api/v1/admin/{cu(community_id)}/announcements",
            headers,
            {"title": "To pin then unpin", "content": "body", "status": "published"},
        )
        announcement_id = (await create_resp.get_json())["data"]["id"]

        await client.put(
            f"/api/v1/admin/{cu(community_id)}/announcements/{announcement_id}/pin", headers=headers
        )
        unpin_resp = await client.put(
            f"/api/v1/admin/{cu(community_id)}/announcements/{announcement_id}/unpin",
            headers=headers,
        )
        assert unpin_resp.status_code == 200
        assert (await unpin_resp.get_json())["data"]["is_pinned"] is False

        archive_resp = await client.post(
            f"/api/v1/admin/{cu(community_id)}/announcements/{announcement_id}/archive",
            headers=headers,
        )
        assert archive_resp.status_code == 200
        assert (await archive_resp.get_json())["data"]["status"] == "archived"

        create_resp2 = await _post_json(
            client,
            f"/api/v1/admin/{cu(community_id)}/announcements",
            headers,
            {"title": "To delete", "content": "body"},
        )
        announcement_id2 = (await create_resp2.get_json())["data"]["id"]
        delete_resp = await client.delete(
            f"/api/v1/admin/{cu(community_id)}/announcements/{announcement_id2}", headers=headers
        )
        assert delete_resp.status_code == 200
        assert (await delete_resp.get_json())["data"]["status"] == "archived"

    async def test_transition_routes_404_on_missing_announcement(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        headers = auth_headers(scope="community.announcements:write", user_id="1")
        for method, suffix in [
            ("DELETE", ""),
            ("POST", "/publish"),
            ("PUT", "/pin"),
            ("PUT", "/unpin"),
            ("POST", "/archive"),
        ]:
            response = await client.open(
                f"/api/v1/admin/{cu(community_id)}/announcements/9999{suffix}",
                method=method,
                headers=headers,
            )
            assert response.status_code == 404, (method, suffix)

    async def test_list_filters_by_status_and_pinned(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        headers = auth_headers(scope="community.announcements:write", user_id="1")
        await _post_json(
            client,
            f"/api/v1/admin/{cu(community_id)}/announcements",
            headers,
            {
                "title": "Published pinned",
                "content": "body",
                "status": "published",
                "is_pinned": True,
            },
        )
        await _post_json(
            client,
            f"/api/v1/admin/{cu(community_id)}/announcements",
            headers,
            {"title": "Draft unpinned", "content": "body", "status": "draft"},
        )

        by_status = await client.get(
            f"/api/v1/admin/{cu(community_id)}/announcements?status=published",
            headers=auth_headers(scope="community.announcements:read"),
        )
        assert (await by_status.get_json())["pagination"]["total"] == 1

        pinned_only = await client.get(
            f"/api/v1/admin/{cu(community_id)}/announcements?pinned=true",
            headers=auth_headers(scope="community.announcements:read"),
        )
        assert (await pinned_only.get_json())["pagination"]["total"] == 1

        unpinned_only = await client.get(
            f"/api/v1/admin/{cu(community_id)}/announcements?pinned=false",
            headers=auth_headers(scope="community.announcements:read"),
        )
        assert (await unpinned_only.get_json())["pagination"]["total"] == 1


class TestBroadcast:
    async def test_broadcast_requires_published_status(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        headers = auth_headers(scope="community.announcements:write", user_id="1")
        create_resp = await _post_json(
            client,
            f"/api/v1/admin/{cu(community_id)}/announcements",
            headers,
            {"title": "Draft only", "content": "not published yet", "status": "draft"},
        )
        announcement_id = (await create_resp.get_json())["data"]["id"]

        response = await _post_json(
            client,
            f"/api/v1/admin/{cu(community_id)}/announcements/{announcement_id}/broadcast",
            headers,
            {"platforms": ["discord"]},
        )
        assert response.status_code == 400

    async def test_broadcast_rejects_empty_platforms(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        headers = auth_headers(scope="community.announcements:write", user_id="1")
        response = await _post_json(
            client,
            f"/api/v1/admin/{cu(community_id)}/announcements/1/broadcast",
            headers,
            {"platforms": []},
        )
        assert response.status_code == 400

    async def test_broadcast_unknown_announcement_is_404(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        headers = auth_headers(scope="community.announcements:write", user_id="1")
        response = await _post_json(
            client,
            f"/api/v1/admin/{cu(community_id)}/announcements/9999/broadcast",
            headers,
            {"platforms": ["discord"]},
        )
        assert response.status_code == 404

    async def test_broadcast_no_servers_returns_error_summary(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        """No `community_servers` row seeded -- `broadcast_to_all_platforms`'s.

        empty-servers branch, no httpx call made.
        """
        _, community_id = community_db
        headers = auth_headers(scope="community.announcements:write", user_id="1")
        create_resp = await _post_json(
            client,
            f"/api/v1/admin/{cu(community_id)}/announcements",
            headers,
            {"title": "Go live", "content": "body", "status": "published"},
        )
        announcement_id = (await create_resp.get_json())["data"]["id"]

        response = await _post_json(
            client,
            f"/api/v1/admin/{cu(community_id)}/announcements/{announcement_id}/broadcast",
            headers,
            {"platforms": ["discord"]},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["results"] == []

    async def test_broadcast_success_and_status(
        self, client: Any, auth_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:

        import services.community_announcements as announcements_svc

        dal, community_id = community_db
        dal.community_servers.insert(
            community_id=community_id, platform="discord", platform_server_id="s1"
        )
        dal.commit()

        async def fake_post_to_platform(
            platform: str, announcement: dict[str, Any]
        ) -> tuple[bool, str | None]:
            return True, None

        monkeypatch.setattr(announcements_svc, "_post_to_platform", fake_post_to_platform)

        headers = auth_headers(scope="community.announcements:write", user_id="1")
        create_resp = await _post_json(
            client,
            f"/api/v1/admin/{cu(community_id)}/announcements",
            headers,
            {"title": "Go live", "content": "body", "status": "published"},
        )
        announcement_id = (await create_resp.get_json())["data"]["id"]

        broadcast_resp = await _post_json(
            client,
            f"/api/v1/admin/{cu(community_id)}/announcements/{announcement_id}/broadcast",
            headers,
            {"platforms": ["discord"]},
        )
        assert broadcast_resp.status_code == 200
        broadcast_body = await broadcast_resp.get_json()
        assert broadcast_body["data"]["results"][0]["success"] is True

        status_resp = await client.get(
            f"/api/v1/admin/{cu(community_id)}/announcements/{announcement_id}/broadcast-status",
            headers=auth_headers(scope="community.announcements:read"),
        )
        assert status_resp.status_code == 200
        status_body = await status_resp.get_json()
        assert len(status_body["data"]) == 1
        assert status_body["data"][0]["status"] == "sent"
        assert status_body["data"][0]["platform"] == "discord"

    async def test_broadcast_failure_recorded(
        self, client: Any, auth_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        import services.community_announcements as announcements_svc

        dal, community_id = community_db
        dal.community_servers.insert(
            community_id=community_id, platform="slack", platform_server_id="s2"
        )
        dal.commit()

        async def fake_post_to_platform(
            platform: str, announcement: dict[str, Any]
        ) -> tuple[bool, str | None]:
            return False, "connection refused"

        monkeypatch.setattr(announcements_svc, "_post_to_platform", fake_post_to_platform)

        headers = auth_headers(scope="community.announcements:write", user_id="1")
        create_resp = await _post_json(
            client,
            f"/api/v1/admin/{cu(community_id)}/announcements",
            headers,
            {"title": "Go live", "content": "body", "status": "published"},
        )
        announcement_id = (await create_resp.get_json())["data"]["id"]

        response = await _post_json(
            client,
            f"/api/v1/admin/{cu(community_id)}/announcements/{announcement_id}/broadcast",
            headers,
            {"platforms": ["slack"]},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["results"][0]["success"] is False
        assert body["data"]["results"][0]["error"] == "connection refused"

    async def test_broadcast_status_unknown_announcement_is_404(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await client.get(
            f"/api/v1/admin/{cu(community_id)}/announcements/9999/broadcast-status",
            headers=auth_headers(scope="community.announcements:read"),
        )
        assert response.status_code == 404


class TestPostToPlatformDirect:
    """`_post_to_platform` itself -- real `httpx.AsyncClient` mocked, the.

    actual I/O boundary (see `test_community_engagement_proxy.py`'s
    module docstring for the same rationale).
    """

    async def test_unknown_platform_returns_error(self) -> None:
        from services.community_announcements import _post_to_platform

        ok, err = await _post_to_platform("not-a-real-platform", {})
        assert ok is False
        assert err is not None and "No action endpoint" in err

    async def test_success_response(self) -> None:
        from unittest.mock import AsyncMock, patch

        from services.community_announcements import _post_to_platform

        mock_client = AsyncMock()
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = False

        with patch("httpx.AsyncClient", return_value=mock_client):
            ok, err = await _post_to_platform("discord", {"title": "x"})
        assert ok is True
        assert err is None

    async def test_non_2xx_response_returns_error(self) -> None:
        from unittest.mock import AsyncMock, patch

        from services.community_announcements import _post_to_platform

        mock_client = AsyncMock()
        mock_response = AsyncMock()
        mock_response.status_code = 500
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = False

        with patch("httpx.AsyncClient", return_value=mock_client):
            ok, err = await _post_to_platform("discord", {"title": "x"})
        assert ok is False
        assert err == "HTTP 500"

    async def test_connection_error_returns_error(self) -> None:
        from unittest.mock import AsyncMock, patch

        import httpx

        from services.community_announcements import _post_to_platform

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = False

        with patch("httpx.AsyncClient", return_value=mock_client):
            ok, err = await _post_to_platform("discord", {"title": "x"})
        assert ok is False
        assert err is not None


#: Every JSON response read here must carry community ids as UUID strings (conftest guard).
UUID_COMMUNITY_RESPONSES = True
