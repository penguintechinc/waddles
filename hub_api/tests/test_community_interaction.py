"""`blueprints/v1/community_interaction.py` -- channels, forum, roles, permission overrides, relay.

Largest single group -- covers admin channel/role CRUD, member forum
read/post/reply, and the internal relay endpoint's `X-Service-Key` gate
(mirror-group fan-out itself is a no-op here: no mirror group exists for
the test community, so `relay_message` returns immediately after finding
zero groups -- exercised in isolation would need `mirror_groups`/
`mirror_group_members` fixture rows, out of scope for this REST-contract
test file).
"""

from __future__ import annotations

import json as json_module
from typing import Any
from unittest.mock import AsyncMock

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.community_interaction import (
    interaction_admin_bp,
    interaction_internal_bp,
    interaction_member_bp,
)
from tests.community_ids import UNKNOWN_PUBLIC_ID, cpath, install_community_ids


@pytest.fixture
def app(community_db: Any) -> Quart:
    dal, _ = community_db
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    install_community_ids(quart_app, dal)
    quart_app.register_blueprint(interaction_admin_bp)
    quart_app.register_blueprint(interaction_member_bp)
    quart_app.register_blueprint(interaction_internal_bp)
    quart_app.config["dal"] = dal
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


@pytest.fixture(autouse=True)
def _feature_enabled_default_on(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Default the `community.interactions`/`community.forums` Feature flags ON for this file.

    `admin_list_channels` gates on `FEATURE_COMMUNITY_INTERACTIONS`;
    `member_forum_posts` gates on the separate `FEATURE_COMMUNITY_FORUMS`
    (this PR) -- see `test_community_features_gate.py` for the dedicated
    per-flag OFF-blocks-handler proof.
    """
    import blueprints.v1.community_interaction as interaction_module

    monkeypatch.setattr(interaction_module, "feature_enabled", AsyncMock(return_value=True))


async def _post_json(
    client: Any, path: str, headers: dict[str, str], payload: dict[str, Any]
) -> Any:
    return await client.post(
        path,
        headers={**headers, "Content-Type": "application/json"},
        data=json_module.dumps(payload),
    )


UNKNOWN_ROLE = "00000000-0000-4000-8000-00000000dead"


def _pid(dal: Any, table: str, numeric_id: int) -> str:
    """Public UUID of a seeded row (route paths and bodies never carry the numeric id)."""
    return str(dal[table][numeric_id].public_id)


class TestScopeAndTenant:
    async def test_wrong_scope_is_403(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        dal, community_id = community_db
        response = await client.get(
            cpath(dal, "/api/v1/admin/{community_id}/interaction/channels", community_id),
            headers=auth_headers(scope="community.interaction:read", user_id="1"),
        )
        assert response.status_code == 403

    async def test_broad_jwt_scopes_do_not_grant_channel_or_forum_admin(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        """Dedicated community scopes are required; the old broad JWT scopes no longer admit."""
        dal, community_id = community_db
        headers = auth_headers(
            scope="community.interaction:read community.interaction:write "
            "community.interaction:manage_channels community:admin",
            user_id="1",
        )
        for method, path in (
            ("GET", "interaction/channels"),
            ("POST", "interaction/channels"),
            ("DELETE", "interaction/channels/00000000-0000-4000-8000-00000000dead"),
            ("DELETE", "interaction/forum/replies/00000000-0000-4000-8000-00000000dead"),
            ("PUT", "interaction/channels/00000000-0000-4000-8000-00000000dead/permissions"),
            ("GET", "interaction/roles"),
        ):
            response = await client.open(
                cpath(dal, "/api/v1/admin/{community_id}/{path}", community_id, path=path),
                method=method,
                headers=headers,
            )
            assert response.status_code == 403, (method, path)

    async def test_unknown_community_is_404(self, client: Any, auth_headers: Any) -> None:
        response = await client.get(
            f"/api/v1/admin/{UNKNOWN_PUBLIC_ID}/interaction/channels",
            headers=auth_headers(scope="users:admin", user_id="1"),
        )
        assert response.status_code == 404


class TestChannelCrudAndAutoProvision:
    async def test_create_channel_auto_provisions_hub_server(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        dal, community_id = community_db
        response = await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/interaction/channels", community_id),
            auth_headers(scope="users:admin", user_id="1"),
            {"name": "general-chat", "channel_type": "chat"},
        )
        assert response.status_code == 201
        channel = (await response.get_json())["channel"]
        assert channel["channel_type"] == "chat"
        assert channel["community_server_channel_id"] is not None

        hub_server = (
            dal(
                (dal.community_servers.community_id == community_id)
                & (dal.community_servers.platform == "hub")
            )
            .select()
            .first()
        )
        assert hub_server is not None

    async def test_list_channels_returns_seeded_channel(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        dal, community_id = community_db
        write_headers = auth_headers(scope="users:admin", user_id="1")
        await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/interaction/channels", community_id),
            write_headers,
            {"name": "listable"},
        )

        response = await client.get(
            cpath(dal, "/api/v1/admin/{community_id}/interaction/channels", community_id),
            headers=auth_headers(scope="users:admin", user_id="1"),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert len(body["channels"]) == 1
        assert body["channels"][0]["name"] == "listable"

    async def test_duplicate_channel_name_is_409(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        dal, community_id = community_db
        headers = auth_headers(scope="users:admin", user_id="1")
        await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/interaction/channels", community_id),
            headers,
            {"name": "dupe"},
        )
        response = await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/interaction/channels", community_id),
            headers,
            {"name": "dupe"},
        )
        assert response.status_code == 409


class TestForumFlow:
    async def test_create_post_then_reply_then_read(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        dal, community_id = community_db
        write_headers = auth_headers(
            scope="community.interaction:write community.interaction:read users:admin", user_id="1"
        )

        channel_resp = await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/interaction/channels", community_id),
            write_headers,
            {"name": "forum-general", "channel_type": "forum"},
        )
        channel_id = (await channel_resp.get_json())["channel"]["id"]

        post_resp = await _post_json(
            client,
            cpath(
                dal,
                "/api/v1/community/{community_id}/interact/forum/{channel_id}/posts",
                community_id,
                channel_id=channel_id,
            ),
            write_headers,
            {"title": "Welcome thread", "body": "Say hi!"},
        )
        assert post_resp.status_code == 201
        post_id = (await post_resp.get_json())["post"]["id"]

        reply_resp = await _post_json(
            client,
            cpath(
                dal,
                "/api/v1/community/{community_id}/interact/forum/posts/{post_id}/replies",
                community_id,
                post_id=post_id,
            ),
            write_headers,
            {"content": "Hi there!"},
        )
        assert reply_resp.status_code == 201

        get_resp = await client.get(
            cpath(
                dal,
                "/api/v1/community/{community_id}/interact/forum/{channel_id}/posts/{post_id}",
                community_id,
                channel_id=channel_id,
                post_id=post_id,
            ),
            headers=auth_headers(scope="community.interaction:read"),
        )
        assert get_resp.status_code == 200
        body = await get_resp.get_json()
        assert body["post"]["reply_count"] == 1
        assert len(body["post"]["replies"]) == 1
        assert body["post"]["replies"][0]["content"] == "Hi there!"

    async def test_reply_on_locked_post_is_rejected(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        dal, community_id = community_db
        write_headers = auth_headers(
            scope="community.interaction:write community.interaction:read users:admin", user_id="1"
        )
        channel_resp = await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/interaction/channels", community_id),
            write_headers,
            {"name": "locked-forum", "channel_type": "forum"},
        )
        channel_id = (await channel_resp.get_json())["channel"]["id"]
        post_resp = await _post_json(
            client,
            cpath(
                dal,
                "/api/v1/community/{community_id}/interact/forum/{channel_id}/posts",
                community_id,
                channel_id=channel_id,
            ),
            write_headers,
            {"title": "Locked topic"},
        )
        post_id = (await post_resp.get_json())["post"]["id"]

        await client.put(
            cpath(
                dal,
                "/api/v1/admin/{community_id}/interaction/forum/posts/{post_id}",
                community_id,
                post_id=post_id,
            ),
            headers={**write_headers, "Content-Type": "application/json"},
            data=json_module.dumps({"is_locked": True}),
        )

        reply_resp = await _post_json(
            client,
            cpath(
                dal,
                "/api/v1/community/{community_id}/interact/forum/posts/{post_id}/replies",
                community_id,
                post_id=post_id,
            ),
            write_headers,
            {"content": "too late"},
        )
        assert reply_resp.status_code == 400


class TestCommunityRoles:
    async def test_priority_out_of_range_is_400(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        dal, community_id = community_db
        response = await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/interaction/roles", community_id),
            auth_headers(scope="users:admin", user_id="1"),
            {"name": "vip", "priority": 99},
        )
        assert response.status_code == 400

    async def test_create_and_delete_custom_role(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        dal, community_id = community_db
        # `member` is the reassignment target on role delete -- seed it,
        # matching `seed_community_system_roles()` (058_tenants_and_claims.sql).
        dal.community_roles.insert(
            community_id=community_id, name="member", is_system=True, priority=10
        )
        dal.commit()

        manage_headers = auth_headers(scope="users:admin", user_id="1")
        create_resp = await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/interaction/roles", community_id),
            manage_headers,
            {"name": "VIP", "priority": 20, "scopes": ["channels:read"]},
        )
        assert create_resp.status_code == 201
        role = (await create_resp.get_json())["role"]
        assert role["name"] == "vip"

        delete_resp = await client.delete(
            cpath(
                dal,
                "/api/v1/admin/{community_id}/interaction/roles/{role__id}",
                community_id,
                role__id=role["id"],
            ),
            headers=manage_headers,
        )
        assert delete_resp.status_code == 200

    async def test_delete_system_role_is_409(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        dal, community_id = community_db
        role_id = dal.community_roles.insert(
            community_id=community_id, name="admin", is_system=True, priority=90
        )
        dal.commit()

        response = await client.delete(
            cpath(
                dal,
                "/api/v1/admin/{community_id}/interaction/roles/{role_id}",
                community_id,
                role_id=_pid(dal, "community_roles", role_id),
            ),
            headers=auth_headers(scope="users:admin", user_id="1"),
        )
        assert response.status_code == 409


class TestInternalRelay:
    async def test_missing_service_key_is_401(self, client: Any) -> None:
        response = await client.post("/api/v1/internal/relay/incoming", json={})
        assert response.status_code == 401

    async def test_unknown_source_channel_is_404(
        self, client: Any, service_key_headers: Any
    ) -> None:
        response = await _post_json(
            client,
            "/api/v1/internal/relay/incoming",
            service_key_headers,
            {"sourcePlatformChannelId": "does-not-exist"},
        )
        assert response.status_code == 404

    async def test_known_source_channel_relays_successfully(
        self, client: Any, service_key_headers: Any, community_db: Any
    ) -> None:
        """Source channel resolves; no mirror group exists so `relay_message`.

        returns immediately after finding zero groups -- still a 200.
        """
        dal, community_id = community_db
        server_id = dal.community_servers.insert(
            community_id=community_id, platform="discord", platform_server_id="s1"
        )
        dal.community_server_channels.insert(
            community_server_id=server_id, platform_channel_id="discord-abc", channel_type="chat"
        )
        dal.commit()

        response = await _post_json(
            client,
            "/api/v1/internal/relay/incoming",
            service_key_headers,
            {
                "sourcePlatformChannelId": "discord-abc",
                "platform": "discord",
                "author": {"platform": "discord", "username": "alice"},
                "content": {"text": "hi"},
                "messageType": "message",
            },
        )
        assert response.status_code == 200
        assert (await response.get_json()) == {"success": True}


class TestUnknownCommunity404Sweep:
    """One 404 case per remaining route -- covers each function's own.

    `if not _tenant_ok(...)` line (coverage.py tracks per-function, not
    per-condition, so the shared helper being tested once elsewhere
    doesn't cover *this* function's copy of the check).
    """

    @pytest.mark.parametrize(
        "method,path_suffix,scope",
        [
            ("POST", "interaction/channels", "community.interaction:write"),
            (
                "PUT",
                "interaction/channels/00000000-0000-4000-8000-00000000dead",
                "community.interaction:write",
            ),
            (
                "DELETE",
                "interaction/channels/00000000-0000-4000-8000-00000000dead",
                "community.interaction:write",
            ),
            (
                "PUT",
                "interaction/forum/posts/00000000-0000-4000-8000-00000000dead",
                "community.interaction:write",
            ),
            (
                "DELETE",
                "interaction/forum/replies/00000000-0000-4000-8000-00000000dead",
                "community.interaction:write",
            ),
            ("GET", "interaction/roles", "community.interaction:read"),
            ("POST", "interaction/roles", "community.interaction:manage_roles"),
            (
                "PUT",
                "interaction/roles/00000000-0000-4000-8000-00000000dead",
                "community.interaction:manage_roles",
            ),
            (
                "DELETE",
                "interaction/roles/00000000-0000-4000-8000-00000000dead",
                "community.interaction:manage_roles",
            ),
            (
                "GET",
                "interaction/channels/00000000-0000-4000-8000-00000000dead/permissions",
                "community.interaction:read",
            ),
            (
                "PUT",
                "interaction/channels/00000000-0000-4000-8000-00000000dead/permissions",
                "community.interaction:manage_channels",
            ),
        ],
    )
    async def test_admin_route_404s_on_unknown_community(
        self, client: Any, auth_headers: Any, method: str, path_suffix: str, scope: str
    ) -> None:
        response = await client.open(
            f"/api/v1/admin/{UNKNOWN_PUBLIC_ID}/{path_suffix}",
            method=method,
            headers=auth_headers(scope=scope),
        )
        assert response.status_code == 404

    @pytest.mark.parametrize(
        "method,path_suffix",
        [
            ("GET", "interact/channels"),
            ("POST", "interact/channels"),
            ("GET", "interact/forum/00000000-0000-4000-8000-00000000dead/posts"),
            (
                "GET",
                "interact/forum/00000000-0000-4000-8000-00000000dead/posts/00000000-0000-4000-8000-00000000dead",
            ),
            ("POST", "interact/forum/00000000-0000-4000-8000-00000000dead/posts"),
            ("POST", "interact/forum/posts/00000000-0000-4000-8000-00000000dead/replies"),
        ],
    )
    async def test_member_route_404s_on_unknown_community(
        self, client: Any, auth_headers: Any, method: str, path_suffix: str
    ) -> None:
        scope = "community.interaction:write" if method == "POST" else "community.interaction:read"
        response = await client.open(
            f"/api/v1/community/{UNKNOWN_PUBLIC_ID}/{path_suffix}",
            method=method,
            headers=auth_headers(scope=scope, user_id="1"),
        )
        assert response.status_code == 404


class TestChannelUpdateDelete:
    async def test_update_channel_success_and_not_found(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        dal, community_id = community_db
        write_headers = auth_headers(scope="users:admin", user_id="1")
        channel_resp = await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/interaction/channels", community_id),
            write_headers,
            {"name": "chan-a"},
        )
        channel_id = (await channel_resp.get_json())["channel"]["id"]

        update_resp = await client.put(
            cpath(
                dal,
                "/api/v1/admin/{community_id}/interaction/channels/{channel_id}",
                community_id,
                channel_id=channel_id,
            ),
            headers={**write_headers, "Content-Type": "application/json"},
            data=json_module.dumps({"description": "updated"}),
        )
        assert update_resp.status_code == 200
        assert (await update_resp.get_json())["channel"]["description"] == "updated"

        missing_resp = await client.put(
            cpath(dal, "/api/v1/admin/{community_id}/interaction/channels/9999", community_id),
            headers={**write_headers, "Content-Type": "application/json"},
            data=json_module.dumps({"description": "x"}),
        )
        assert missing_resp.status_code == 404

    async def test_delete_channel_success_and_not_found(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        dal, community_id = community_db
        write_headers = auth_headers(scope="users:admin", user_id="1")
        channel_resp = await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/interaction/channels", community_id),
            write_headers,
            {"name": "chan-b"},
        )
        channel_id = (await channel_resp.get_json())["channel"]["id"]

        delete_resp = await client.delete(
            cpath(
                dal,
                "/api/v1/admin/{community_id}/interaction/channels/{channel_id}",
                community_id,
                channel_id=channel_id,
            ),
            headers=write_headers,
        )
        assert delete_resp.status_code == 200

        missing_resp = await client.delete(
            cpath(dal, "/api/v1/admin/{community_id}/interaction/channels/9999", community_id),
            headers=write_headers,
        )
        assert missing_resp.status_code == 404


class TestForumModerationAndReplies:
    async def test_delete_reply_success_and_not_found(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        dal, community_id = community_db
        write_headers = auth_headers(
            scope="community.interaction:write community.interaction:read users:admin", user_id="1"
        )
        channel_resp = await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/interaction/channels", community_id),
            write_headers,
            {"name": "reply-forum", "channel_type": "forum"},
        )
        channel_id = (await channel_resp.get_json())["channel"]["id"]
        post_resp = await _post_json(
            client,
            cpath(
                dal,
                "/api/v1/community/{community_id}/interact/forum/{channel_id}/posts",
                community_id,
                channel_id=channel_id,
            ),
            write_headers,
            {"title": "topic"},
        )
        post_id = (await post_resp.get_json())["post"]["id"]
        reply_resp = await _post_json(
            client,
            cpath(
                dal,
                "/api/v1/community/{community_id}/interact/forum/posts/{post_id}/replies",
                community_id,
                post_id=post_id,
            ),
            write_headers,
            {"content": "to be deleted"},
        )
        reply_id = (await reply_resp.get_json())["reply"]["id"]

        delete_resp = await client.delete(
            cpath(
                dal,
                "/api/v1/admin/{community_id}/interaction/forum/replies/{reply_id}",
                community_id,
                reply_id=reply_id,
            ),
            headers=write_headers,
        )
        assert delete_resp.status_code == 200

        missing_resp = await client.delete(
            cpath(dal, "/api/v1/admin/{community_id}/interaction/forum/replies/9999", community_id),
            headers=write_headers,
        )
        assert missing_resp.status_code == 404

    async def test_moderate_post_no_action_specified_is_400(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        dal, community_id = community_db
        write_headers = auth_headers(
            scope="community.interaction:write community.interaction:read users:admin", user_id="1"
        )
        channel_resp = await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/interaction/channels", community_id),
            write_headers,
            {"name": "mod-forum", "channel_type": "forum"},
        )
        channel_id = (await channel_resp.get_json())["channel"]["id"]
        post_resp = await _post_json(
            client,
            cpath(
                dal,
                "/api/v1/community/{community_id}/interact/forum/{channel_id}/posts",
                community_id,
                channel_id=channel_id,
            ),
            write_headers,
            {"title": "topic"},
        )
        post_id = (await post_resp.get_json())["post"]["id"]

        response = await client.put(
            cpath(
                dal,
                "/api/v1/admin/{community_id}/interaction/forum/posts/{post_id}",
                community_id,
                post_id=post_id,
            ),
            headers={**write_headers, "Content-Type": "application/json"},
            data=json_module.dumps({}),
        )
        assert response.status_code == 400

    async def test_moderate_post_delete_action(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        dal, community_id = community_db
        write_headers = auth_headers(
            scope="community.interaction:write community.interaction:read users:admin", user_id="1"
        )
        channel_resp = await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/interaction/channels", community_id),
            write_headers,
            {"name": "del-forum", "channel_type": "forum"},
        )
        channel_id = (await channel_resp.get_json())["channel"]["id"]
        post_resp = await _post_json(
            client,
            cpath(
                dal,
                "/api/v1/community/{community_id}/interact/forum/{channel_id}/posts",
                community_id,
                channel_id=channel_id,
            ),
            write_headers,
            {"title": "topic"},
        )
        post_id = (await post_resp.get_json())["post"]["id"]

        response = await client.put(
            cpath(
                dal,
                "/api/v1/admin/{community_id}/interaction/forum/posts/{post_id}",
                community_id,
                post_id=post_id,
            ),
            headers={**write_headers, "Content-Type": "application/json"},
            data=json_module.dumps({"delete": True}),
        )
        assert response.status_code == 200

        get_resp = await client.get(
            cpath(
                dal,
                "/api/v1/community/{community_id}/interact/forum/{channel_id}/posts/{post_id}",
                community_id,
                channel_id=channel_id,
                post_id=post_id,
            ),
            headers=auth_headers(scope="community.interaction:read"),
        )
        assert get_resp.status_code == 404

    async def test_create_forum_post_requires_title(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        dal, community_id = community_db
        write_headers = auth_headers(
            scope="community.interaction:write community.interaction:read users:admin", user_id="1"
        )
        channel_resp = await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/interaction/channels", community_id),
            write_headers,
            {"name": "notitle-forum", "channel_type": "forum"},
        )
        channel_id = (await channel_resp.get_json())["channel"]["id"]

        response = await _post_json(
            client,
            cpath(
                dal,
                "/api/v1/community/{community_id}/interact/forum/{channel_id}/posts",
                community_id,
                channel_id=channel_id,
            ),
            write_headers,
            {"title": ""},
        )
        assert response.status_code == 400

    async def test_create_forum_reply_requires_content(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        dal, community_id = community_db
        write_headers = auth_headers(
            scope="community.interaction:write community.interaction:read users:admin", user_id="1"
        )
        channel_resp = await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/interaction/channels", community_id),
            write_headers,
            {"name": "nocontent-forum", "channel_type": "forum"},
        )
        channel_id = (await channel_resp.get_json())["channel"]["id"]
        post_resp = await _post_json(
            client,
            cpath(
                dal,
                "/api/v1/community/{community_id}/interact/forum/{channel_id}/posts",
                community_id,
                channel_id=channel_id,
            ),
            write_headers,
            {"title": "topic"},
        )
        post_id = (await post_resp.get_json())["post"]["id"]

        response = await _post_json(
            client,
            cpath(
                dal,
                "/api/v1/community/{community_id}/interact/forum/posts/{post_id}/replies",
                community_id,
                post_id=post_id,
            ),
            write_headers,
            {"content": ""},
        )
        assert response.status_code == 400

    async def test_create_forum_reply_unknown_post_is_404(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        dal, community_id = community_db
        write_headers = auth_headers(
            scope="community.interaction:write community.interaction:read users:admin", user_id="1"
        )
        response = await _post_json(
            client,
            cpath(
                dal,
                "/api/v1/community/{community_id}/interact/forum/posts/9999/replies",
                community_id,
            ),
            write_headers,
            {"content": "hi"},
        )
        assert response.status_code == 404

    async def test_list_forum_posts_and_get_unknown_post(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        dal, community_id = community_db
        write_headers = auth_headers(
            scope="community.interaction:write community.interaction:read users:admin", user_id="1"
        )
        channel_resp = await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/interaction/channels", community_id),
            write_headers,
            {"name": "list-forum", "channel_type": "forum"},
        )
        channel_id = (await channel_resp.get_json())["channel"]["id"]
        await _post_json(
            client,
            cpath(
                dal,
                "/api/v1/community/{community_id}/interact/forum/{channel_id}/posts",
                community_id,
                channel_id=channel_id,
            ),
            write_headers,
            {"title": "topic 1"},
        )

        list_resp = await client.get(
            cpath(
                dal,
                "/api/v1/community/{community_id}/interact/forum/{channel_id}/posts",
                community_id,
                channel_id=channel_id,
            ),
            headers=auth_headers(scope="community.interaction:read"),
        )
        assert list_resp.status_code == 200
        list_body = await list_resp.get_json()
        assert list_body["pagination"]["total"] == 1
        assert len(list_body["posts"]) == 1

        missing_resp = await client.get(
            cpath(
                dal,
                "/api/v1/community/{community_id}/interact/forum/{channel_id}/posts/9999",
                community_id,
                channel_id=channel_id,
            ),
            headers=auth_headers(scope="community.interaction:read"),
        )
        assert missing_resp.status_code == 404


class TestRolesAdditional:
    async def test_list_roles(self, client: Any, auth_headers: Any, community_db: Any) -> None:
        dal, community_id = community_db
        dal.community_roles.insert(
            community_id=community_id, name="member", is_system=True, priority=10
        )
        dal.commit()

        response = await client.get(
            cpath(dal, "/api/v1/admin/{community_id}/interaction/roles", community_id),
            headers=auth_headers(scope="users:admin", user_id="1"),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert len(body["roles"]) == 1
        assert body["roles"][0]["name"] == "member"

    async def test_update_role_success_and_not_found(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        dal, community_id = community_db
        role_id = dal.community_roles.insert(
            community_id=community_id, name="vip", is_system=False, priority=5
        )
        dal.commit()
        manage_headers = auth_headers(scope="users:admin", user_id="1")

        update_resp = await client.put(
            cpath(
                dal,
                "/api/v1/admin/{community_id}/interaction/roles/{role_id}",
                community_id,
                role_id=_pid(dal, "community_roles", role_id),
            ),
            headers={**manage_headers, "Content-Type": "application/json"},
            data=json_module.dumps({"displayName": "VIP Member", "scopes": ["channels:read"]}),
        )
        assert update_resp.status_code == 200

        missing_resp = await client.put(
            cpath(
                dal,
                "/api/v1/admin/{community_id}/interaction/roles/00000000-0000-4000-8000-00000000dead",
                community_id,
            ),
            headers={**manage_headers, "Content-Type": "application/json"},
            data=json_module.dumps({"displayName": "x"}),
        )
        assert missing_resp.status_code == 404

    async def test_delete_unknown_role_is_404(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        dal, community_id = community_db
        response = await client.delete(
            cpath(
                dal,
                "/api/v1/admin/{community_id}/interaction/roles/00000000-0000-4000-8000-00000000dead",
                community_id,
            ),
            headers=auth_headers(scope="users:admin", user_id="1"),
        )
        assert response.status_code == 404


class TestChannelPermissionOverrides:
    async def test_get_overrides_unknown_channel_is_404(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        dal, community_id = community_db
        response = await client.get(
            cpath(
                dal,
                "/api/v1/admin/{community_id}/interaction/channels/00000000-0000-4000-8000-00000000dead/permissions",
                community_id,
            ),
            headers=auth_headers(scope="users:admin", user_id="1"),
        )
        assert response.status_code == 404

    async def test_update_overrides_rejects_non_array_and_unknown_channel(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        dal, community_id = community_db
        manage_headers = auth_headers(scope="users:admin", user_id="1")

        bad_shape_resp = await client.put(
            cpath(
                dal,
                "/api/v1/admin/{community_id}/interaction/channels/00000000-0000-4000-8000-00000000dead/permissions",
                community_id,
            ),
            headers={**manage_headers, "Content-Type": "application/json"},
            data=json_module.dumps({"overrides": "not-a-list"}),
        )
        assert bad_shape_resp.status_code == 400

        missing_channel_resp = await client.put(
            cpath(
                dal,
                "/api/v1/admin/{community_id}/interaction/channels/00000000-0000-4000-8000-00000000dead/permissions",
                community_id,
            ),
            headers={**manage_headers, "Content-Type": "application/json"},
            data=json_module.dumps({"overrides": []}),
        )
        assert missing_channel_resp.status_code == 404

    async def test_get_and_update_overrides_full_flow(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        dal, community_id = community_db
        write_headers = auth_headers(scope="users:admin", user_id="1")
        channel_resp = await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/interaction/channels", community_id),
            write_headers,
            {"name": "perm-chan"},
        )
        channel_id = (await channel_resp.get_json())["channel"]["id"]
        role_id = dal.community_roles.insert(community_id=community_id, name="member", priority=10)
        dal.commit()

        empty_resp = await client.get(
            cpath(
                dal,
                "/api/v1/admin/{community_id}/interaction/channels/{channel_id}/permissions",
                community_id,
                channel_id=channel_id,
            ),
            headers=auth_headers(scope="users:admin", user_id="1"),
        )
        assert empty_resp.status_code == 200
        assert (await empty_resp.get_json())["overrides"] == []

        update_resp = await client.put(
            cpath(
                dal,
                "/api/v1/admin/{community_id}/interaction/channels/{channel_id}/permissions",
                community_id,
                channel_id=channel_id,
            ),
            headers={
                **auth_headers(scope="users:admin", user_id="1"),
                "Content-Type": "application/json",
            },
            data=json_module.dumps(
                {
                    "overrides": [
                        {
                            "communityRoleId": _pid(dal, "community_roles", role_id),
                            "grantScopes": ["channels:read"],
                            "denyScopes": [],
                        }
                    ]
                }
            ),
        )
        assert update_resp.status_code == 200

        after_resp = await client.get(
            cpath(
                dal,
                "/api/v1/admin/{community_id}/interaction/channels/{channel_id}/permissions",
                community_id,
                channel_id=channel_id,
            ),
            headers=auth_headers(scope="users:admin", user_id="1"),
        )
        after_body = await after_resp.get_json()
        assert len(after_body["overrides"]) == 1
        assert after_body["overrides"][0]["role_name"] == "member"

        # An override entry with no communityRoleId is skipped, not inserted.
        skip_resp = await client.put(
            cpath(
                dal,
                "/api/v1/admin/{community_id}/interaction/channels/{channel_id}/permissions",
                community_id,
                channel_id=channel_id,
            ),
            headers={
                **auth_headers(scope="users:admin", user_id="1"),
                "Content-Type": "application/json",
            },
            data=json_module.dumps({"overrides": [{"grantScopes": []}]}),
        )
        assert skip_resp.status_code == 200
        final_resp = await client.get(
            cpath(
                dal,
                "/api/v1/admin/{community_id}/interaction/channels/{channel_id}/permissions",
                community_id,
                channel_id=channel_id,
            ),
            headers=auth_headers(scope="users:admin", user_id="1"),
        )
        assert (await final_resp.get_json())["overrides"] == []


class TestChannelOverrideEscalation:
    """Regression: a channel-override editor may not grant/deny scopes they do not hold."""

    async def _setup(self, community_db: Any) -> tuple[int, int, int]:
        dal, community_id = community_db
        self._dal = dal  # `_put` addresses the community by its public UUID
        channel_id = int(dal.hub_channels.insert(community_id=community_id, name="c1"))
        role_id = int(dal.community_roles.insert(community_id=community_id, name="vip", priority=5))
        mod_role = dal.community_roles.insert(
            community_id=community_id,
            name="chan-mod",
            is_system=False,
            role_kind=0,
            base_claims={"scopes": ["community.channels:manage"]},
        )
        dal.community_members.insert(
            community_id=community_id,
            user_id="7",
            role="chan-mod",
            is_active=True,
            community_role_id=mod_role,
        )
        dal.commit()
        return community_id, channel_id, role_id

    async def _put(
        self, client: Any, auth_headers: Any, ids: tuple[int, int, int], override: dict[str, Any]
    ) -> Any:
        community_id, channel_id, role_id = ids
        role_ref = UNKNOWN_ROLE if role_id == 99999 else _pid(self._dal, "community_roles", role_id)
        return await client.put(
            cpath(
                self._dal,
                "/api/v1/admin/{community_id}/interaction/channels/{channel_id}/permissions",
                community_id,
                channel_id=_pid(self._dal, "hub_channels", channel_id),
            ),
            headers={**auth_headers(scope="", user_id="7"), "Content-Type": "application/json"},
            data=json_module.dumps({"overrides": [{"communityRoleId": role_ref, **override}]}),
        )

    async def test_grant_or_deny_scope_not_held_is_403(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        ids = await self._setup(community_db)
        assert (
            await self._put(client, auth_headers, ids, {"grantScopes": ["billing:manage"]})
        ).status_code == 403
        assert (
            await self._put(client, auth_headers, ids, {"denyScopes": ["billing:manage"]})
        ).status_code == 403

    async def test_unknown_scope_foreign_role_and_bad_scope_field_are_400(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        ids = await self._setup(community_db)
        assert (
            await self._put(client, auth_headers, ids, {"grantScopes": ["nope:nope"]})
        ).status_code == 400
        assert (
            await self._put(client, auth_headers, ids, {"scope": "everything"})
        ).status_code == 400
        community_id, channel_id, _ = ids
        foreign = await self._put(
            client, auth_headers, (community_id, channel_id, 99999), {"grantScopes": []}
        )
        assert foreign.status_code == 400

    async def test_scope_the_editor_holds_is_allowed(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        ids = await self._setup(community_db)
        response = await self._put(
            client, auth_headers, ids, {"grantScopes": ["community.channels:manage"]}
        )
        assert response.status_code == 200

    async def test_moderator_cannot_touch_admin_role_overrides(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        """Privilege inversion: no deny on an admin role, and a PUT never strips its override."""
        community_id, channel_id, role_id = await self._setup(community_db)
        dal, _ = community_db
        admin_role = int(
            dal.community_roles.insert(
                community_id=community_id,
                name="community-admin",
                is_system=True,
                role_kind=1,
                base_claims={"scopes": []},
                priority=100,
            )
        )
        dal.hub_channel_permission_overrides.insert(
            hub_channel_id=channel_id,
            community_role_id=admin_role,
            grant_scopes=["channels:read"],
            deny_scopes=[],
            scope="both",
        )
        dal.commit()
        denied = await self._put(
            client,
            auth_headers,
            (community_id, channel_id, admin_role),
            {"denyScopes": ["community.channels:manage"]},  # a scope the editor DOES hold
        )
        assert denied.status_code == 403
        assert (
            await self._put(
                client,
                auth_headers,
                (community_id, channel_id, admin_role),
                {"grantScopes": ["community.channels:manage"]},
            )
        ).status_code == 403
        # A PUT that omits the admin role's override (entries for a covered role only) keeps it.
        ok = await self._put(
            client, auth_headers, (community_id, channel_id, role_id), {"grantScopes": []}
        )
        assert ok.status_code == 200
        rows = dal(dal.hub_channel_permission_overrides.hub_channel_id == channel_id).select()
        by_role = {int(r.community_role_id): r for r in rows}
        assert admin_role in by_role
        assert list(by_role[admin_role].grant_scopes) == ["channels:read"]
        assert list(by_role[admin_role].deny_scopes) == []


class TestMemberChannels:
    async def test_list_channels_without_current_user_falls_back(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        """No `auth_required` on this route -- `getattr(request, "current_user", None)`.

        is `None` when the caller doesn't have write scope's `auth_required` chain.
        """
        dal, community_id = community_db
        response = await client.get(
            cpath(dal, "/api/v1/community/{community_id}/interact/channels", community_id),
            headers=auth_headers(scope="community.interaction:read"),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["canCreateChannel"] is False

    async def test_member_create_channel_denied_without_permission(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        """`can_create_channel`'s SQL selects `communities.config`, a column the.

        shared `ensure_community_tables()` test/prod binding doesn't project
        (minimal by design -- see `community_common.py`). Adding it here via
        raw `ALTER TABLE` (test-only, no source touched) is safe for *this*
        case specifically: no `community_members` row exists, so `can_create_
        channel` returns `False` from the `base_claims IS NULL AND claims_
        cache IS NULL` short-circuit before ever reading `config`'s value --
        only the column's *existence* matters here, not its (raw-SQL,
        driver-dependent) type. See `test_member_create_channel_allowed_for_
        admin_like_policy`'s skip for the case that does need a real value.
        """
        dal, community_id = community_db
        dal.executesql("ALTER TABLE communities ADD COLUMN config TEXT")

        response = await _post_json(
            client,
            cpath(dal, "/api/v1/community/{community_id}/interact/channels", community_id),
            auth_headers(
                scope="community.interaction:write community.interaction:read users:admin",
                user_id="1",
            ),
            {"name": "self-created"},
        )
        assert response.status_code == 403

    @pytest.mark.skip(
        reason="can_create_channel's 'all_members' policy branch reads "
        "communities.config as an already-decoded dict -- true via psycopg2's "
        "automatic JSONB adaptation on Postgres, but raw dal.executesql on "
        "sqlite:memory returns the column as a plain TEXT string, breaking "
        "the '.get(...)' call. Exercised against real Postgres in "
        "integration testing, not this sqlite-backed unit fixture."
    )
    async def test_member_create_channel_allowed_for_admin_like_policy(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        """`can_create_channel`'s `all_members` policy branch, via `communities.config`."""
        dal, community_id = community_db
        dal.executesql("ALTER TABLE communities ADD COLUMN config TEXT")
        dal(dal.communities.id == community_id).update(
            config={"channel_creation_policy": "all_members"}
        )
        dal.commit()

        response = await _post_json(
            client,
            cpath(dal, "/api/v1/community/{community_id}/interact/channels", community_id),
            auth_headers(
                scope="community.interaction:write community.interaction:read users:admin",
                user_id="1",
            ),
            {"name": "self-created-2"},
        )
        assert response.status_code == 201

    async def test_member_create_channel_success_tail_with_permission_granted(
        self, client: Any, auth_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        """Covers `member_create_channel`'s success tail (the actual `create_hub_channel`.

        call + response) by monkeypatching the permission gate directly -- sidesteps
        the same `communities.config` sqlite limitation as the skipped test above,
        without depending on real policy evaluation (already unit-tested via
        `can_create_channel` deserving its own direct test, out of scope here).
        """
        import blueprints.v1.community_interaction as bp_module

        dal, community_id = community_db
        monkeypatch.setattr(bp_module, "can_create_channel", lambda *a, **k: True)

        response = await _post_json(
            client,
            cpath(dal, "/api/v1/community/{community_id}/interact/channels", community_id),
            auth_headers(
                scope="community.interaction:write community.interaction:read users:admin",
                user_id="1",
            ),
            {"name": "self-created-3"},
        )
        assert response.status_code == 201
        assert (await response.get_json())["channel"]["name"] == "self-created-3"


#: Every JSON response read here must carry community ids as UUID strings (conftest guard).
UUID_COMMUNITY_RESPONSES = True


pytestmark = pytest.mark.usefixtures("bypass_route_guards")


class TestInteractionPublicUuids:
    """Channel/post/reply/role ids are public UUIDs, scoped to the URL community."""

    async def _world(self, client: Any, auth_headers: Any, community_db: Any) -> dict[str, Any]:
        from tests.entity_uuid_support import assert_uuid, second_community

        dal, community_id = community_db
        other = second_community(dal, community_id)
        headers = auth_headers(
            scope="community.interaction:write community.interaction:read users:admin",
            user_id="1",
        )
        channel = (
            await (
                await _post_json(
                    client,
                    cpath(dal, "/api/v1/admin/{community_id}/interaction/channels", community_id),
                    headers,
                    {"name": "forum-x", "channel_type": "forum"},
                )
            ).get_json()
        )["channel"]
        post = (
            await (
                await _post_json(
                    client,
                    cpath(
                        dal,
                        "/api/v1/community/{community_id}/interact/forum/{channel_id}/posts",
                        community_id,
                        channel_id=channel["id"],
                    ),
                    headers,
                    {"title": "t", "body": "b"},
                )
            ).get_json()
        )["post"]
        reply = (
            await (
                await _post_json(
                    client,
                    cpath(
                        dal,
                        "/api/v1/community/{community_id}/interact/forum/posts/{post_id}/replies",
                        community_id,
                        post_id=post["id"],
                    ),
                    headers,
                    {"content": "r"},
                )
            ).get_json()
        )["reply"]
        role = _pid(
            dal,
            "community_roles",
            int(dal.community_roles.insert(community_id=community_id, name="vip", priority=5)),
        )
        dal.commit()
        for value in (channel["id"], post["id"], reply["id"], role):
            assert_uuid(value)
        return {
            "dal": dal,
            "cid": community_id,
            "other": other,
            "headers": headers,
            "channel": channel["id"],
            "post": post["id"],
            "reply": reply["id"],
            "role": role,
        }

    async def test_uuid_resolves_in_url_community(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        w = await self._world(client, auth_headers, community_db)
        base = cpath(w["dal"], "/api/v1/admin/{community_id}/interaction", w["cid"])
        member = cpath(w["dal"], "/api/v1/community/{community_id}/interact", w["cid"])
        json_headers = {**w["headers"], "Content-Type": "application/json"}
        put = await client.put(
            f"{base}/channels/{w['channel']}", headers=json_headers, data='{"name": "renamed"}'
        )
        assert put.status_code == 200
        assert (await put.get_json())["channel"]["id"] == w["channel"]
        got = await client.get(
            f"{member}/forum/{w['channel']}/posts/{w['post']}", headers=w["headers"]
        )
        body = (await got.get_json())["post"]
        assert (got.status_code, body["id"], body["hub_channel_id"]) == (
            200,
            w["post"],
            w["channel"],
        )
        assert body["replies"][0]["id"] == w["reply"]
        role = await client.put(
            f"{base}/roles/{w['role']}", headers=json_headers, data='{"displayName": "VIP"}'
        )
        assert role.status_code == 200
        reply = await client.delete(f"{base}/forum/replies/{w['reply']}", headers=w["headers"])
        assert reply.status_code == 200

    async def test_foreign_community_and_numeric_ids_are_404(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        w = await self._world(client, auth_headers, community_db)
        dal = w["dal"]
        foreign_admin = cpath(dal, "/api/v1/admin/{community_id}/interaction", w["other"])
        foreign_member = cpath(dal, "/api/v1/community/{community_id}/interact", w["other"])
        base = cpath(dal, "/api/v1/admin/{community_id}/interaction", w["cid"])
        numeric = {
            "channel": dal.hub_channels(public_id=w["channel"]).id,
            "post": dal.hub_forum_posts(public_id=w["post"]).id,
            "reply": dal.hub_forum_replies(public_id=w["reply"]).id,
            "role": dal.community_roles(public_id=w["role"]).id,
        }
        h = {**w["headers"], "Content-Type": "application/json"}
        cases = [
            ("DELETE", f"{foreign_admin}/channels/{w['channel']}"),
            ("PUT", f"{foreign_admin}/forum/posts/{w['post']}"),
            ("DELETE", f"{foreign_admin}/forum/replies/{w['reply']}"),
            ("DELETE", f"{foreign_admin}/roles/{w['role']}"),
            ("GET", f"{foreign_member}/forum/{w['channel']}/posts/{w['post']}"),
            ("DELETE", f"{base}/channels/{numeric['channel']}"),
            ("PUT", f"{base}/forum/posts/{numeric['post']}"),
            ("DELETE", f"{base}/forum/replies/{numeric['reply']}"),
            ("DELETE", f"{base}/roles/{numeric['role']}"),
        ]
        for method, url in cases:
            resp = await client.open(url, method=method, headers=h, data="{}")
            assert resp.status_code == 404, (method, url, resp.status_code)
        assert dal(dal.hub_forum_replies).count() == 1
        assert dal(dal.community_roles.id == numeric["role"]).count() == 1
