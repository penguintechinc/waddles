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


@pytest.fixture
def app(community_db: Any) -> Quart:
    dal, _ = community_db
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
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


class TestScopeAndTenant:
    async def test_wrong_scope_is_403(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await client.get(
            f"/api/v1/admin/{community_id}/interaction/channels",
            headers=auth_headers(scope="community.interaction:write"),
        )
        assert response.status_code == 403

    async def test_unknown_community_is_404(self, client: Any, auth_headers: Any) -> None:
        response = await client.get(
            "/api/v1/admin/9999/interaction/channels",
            headers=auth_headers(scope="community.interaction:read"),
        )
        assert response.status_code == 404


class TestChannelCrudAndAutoProvision:
    async def test_create_channel_auto_provisions_hub_server(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        dal, community_id = community_db
        response = await _post_json(
            client,
            f"/api/v1/admin/{community_id}/interaction/channels",
            auth_headers(scope="community.interaction:write", user_id="1"),
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
        _, community_id = community_db
        write_headers = auth_headers(scope="community.interaction:write", user_id="1")
        await _post_json(
            client,
            f"/api/v1/admin/{community_id}/interaction/channels",
            write_headers,
            {"name": "listable"},
        )

        response = await client.get(
            f"/api/v1/admin/{community_id}/interaction/channels",
            headers=auth_headers(scope="community.interaction:read"),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert len(body["channels"]) == 1
        assert body["channels"][0]["name"] == "listable"

    async def test_duplicate_channel_name_is_409(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        headers = auth_headers(scope="community.interaction:write", user_id="1")
        await _post_json(
            client, f"/api/v1/admin/{community_id}/interaction/channels", headers, {"name": "dupe"}
        )
        response = await _post_json(
            client, f"/api/v1/admin/{community_id}/interaction/channels", headers, {"name": "dupe"}
        )
        assert response.status_code == 409


class TestForumFlow:
    async def test_create_post_then_reply_then_read(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        write_headers = auth_headers(scope="community.interaction:write", user_id="1")

        channel_resp = await _post_json(
            client,
            f"/api/v1/admin/{community_id}/interaction/channels",
            write_headers,
            {"name": "forum-general", "channel_type": "forum"},
        )
        channel_id = (await channel_resp.get_json())["channel"]["id"]

        post_resp = await _post_json(
            client,
            f"/api/v1/community/{community_id}/interact/forum/{channel_id}/posts",
            write_headers,
            {"title": "Welcome thread", "body": "Say hi!"},
        )
        assert post_resp.status_code == 201
        post_id = (await post_resp.get_json())["post"]["id"]

        reply_resp = await _post_json(
            client,
            f"/api/v1/community/{community_id}/interact/forum/posts/{post_id}/replies",
            write_headers,
            {"content": "Hi there!"},
        )
        assert reply_resp.status_code == 201

        get_resp = await client.get(
            f"/api/v1/community/{community_id}/interact/forum/{channel_id}/posts/{post_id}",
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
        write_headers = auth_headers(scope="community.interaction:write", user_id="1")
        channel_resp = await _post_json(
            client,
            f"/api/v1/admin/{community_id}/interaction/channels",
            write_headers,
            {"name": "locked-forum", "channel_type": "forum"},
        )
        channel_id = (await channel_resp.get_json())["channel"]["id"]
        post_resp = await _post_json(
            client,
            f"/api/v1/community/{community_id}/interact/forum/{channel_id}/posts",
            write_headers,
            {"title": "Locked topic"},
        )
        post_id = (await post_resp.get_json())["post"]["id"]

        await client.put(
            f"/api/v1/admin/{community_id}/interaction/forum/posts/{post_id}",
            headers={**write_headers, "Content-Type": "application/json"},
            data=json_module.dumps({"is_locked": True}),
        )

        reply_resp = await _post_json(
            client,
            f"/api/v1/community/{community_id}/interact/forum/posts/{post_id}/replies",
            write_headers,
            {"content": "too late"},
        )
        assert reply_resp.status_code == 400


class TestCommunityRoles:
    async def test_priority_out_of_range_is_400(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await _post_json(
            client,
            f"/api/v1/admin/{community_id}/interaction/roles",
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
            f"/api/v1/admin/{community_id}/interaction/roles",
            manage_headers,
            {"name": "VIP", "priority": 20, "scopes": ["channels:read"]},
        )
        assert create_resp.status_code == 201
        role = (await create_resp.get_json())["role"]
        assert role["name"] == "vip"

        delete_resp = await client.delete(
            f"/api/v1/admin/{community_id}/interaction/roles/{role['id']}", headers=manage_headers
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
            f"/api/v1/admin/{community_id}/interaction/roles/{role_id}",
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
            ("PUT", "interaction/channels/1", "community.interaction:write"),
            ("DELETE", "interaction/channels/1", "community.interaction:write"),
            ("PUT", "interaction/forum/posts/1", "community.interaction:write"),
            ("DELETE", "interaction/forum/replies/1", "community.interaction:write"),
            ("GET", "interaction/roles", "community.interaction:read"),
            ("POST", "interaction/roles", "community.interaction:manage_roles"),
            ("PUT", "interaction/roles/1", "community.interaction:manage_roles"),
            ("DELETE", "interaction/roles/1", "community.interaction:manage_roles"),
            ("GET", "interaction/channels/1/permissions", "community.interaction:read"),
            ("PUT", "interaction/channels/1/permissions", "community.interaction:manage_channels"),
        ],
    )
    async def test_admin_route_404s_on_unknown_community(
        self, client: Any, auth_headers: Any, method: str, path_suffix: str, scope: str
    ) -> None:
        response = await client.open(
            f"/api/v1/admin/9999/{path_suffix}", method=method, headers=auth_headers(scope=scope)
        )
        assert response.status_code == 404

    @pytest.mark.parametrize(
        "method,path_suffix",
        [
            ("GET", "interact/channels"),
            ("POST", "interact/channels"),
            ("GET", "interact/forum/1/posts"),
            ("GET", "interact/forum/1/posts/1"),
            ("POST", "interact/forum/1/posts"),
            ("POST", "interact/forum/posts/1/replies"),
        ],
    )
    async def test_member_route_404s_on_unknown_community(
        self, client: Any, auth_headers: Any, method: str, path_suffix: str
    ) -> None:
        scope = "community.interaction:write" if method == "POST" else "community.interaction:read"
        response = await client.open(
            f"/api/v1/community/9999/{path_suffix}",
            method=method,
            headers=auth_headers(scope=scope, user_id="1"),
        )
        assert response.status_code == 404


class TestChannelUpdateDelete:
    async def test_update_channel_success_and_not_found(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        write_headers = auth_headers(scope="community.interaction:write", user_id="1")
        channel_resp = await _post_json(
            client,
            f"/api/v1/admin/{community_id}/interaction/channels",
            write_headers,
            {"name": "chan-a"},
        )
        channel_id = (await channel_resp.get_json())["channel"]["id"]

        update_resp = await client.put(
            f"/api/v1/admin/{community_id}/interaction/channels/{channel_id}",
            headers={**write_headers, "Content-Type": "application/json"},
            data=json_module.dumps({"description": "updated"}),
        )
        assert update_resp.status_code == 200
        assert (await update_resp.get_json())["channel"]["description"] == "updated"

        missing_resp = await client.put(
            f"/api/v1/admin/{community_id}/interaction/channels/9999",
            headers={**write_headers, "Content-Type": "application/json"},
            data=json_module.dumps({"description": "x"}),
        )
        assert missing_resp.status_code == 404

    async def test_delete_channel_success_and_not_found(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        write_headers = auth_headers(scope="community.interaction:write", user_id="1")
        channel_resp = await _post_json(
            client,
            f"/api/v1/admin/{community_id}/interaction/channels",
            write_headers,
            {"name": "chan-b"},
        )
        channel_id = (await channel_resp.get_json())["channel"]["id"]

        delete_resp = await client.delete(
            f"/api/v1/admin/{community_id}/interaction/channels/{channel_id}", headers=write_headers
        )
        assert delete_resp.status_code == 200

        missing_resp = await client.delete(
            f"/api/v1/admin/{community_id}/interaction/channels/9999", headers=write_headers
        )
        assert missing_resp.status_code == 404


class TestForumModerationAndReplies:
    async def test_delete_reply_success_and_not_found(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        write_headers = auth_headers(scope="community.interaction:write", user_id="1")
        channel_resp = await _post_json(
            client,
            f"/api/v1/admin/{community_id}/interaction/channels",
            write_headers,
            {"name": "reply-forum", "channel_type": "forum"},
        )
        channel_id = (await channel_resp.get_json())["channel"]["id"]
        post_resp = await _post_json(
            client,
            f"/api/v1/community/{community_id}/interact/forum/{channel_id}/posts",
            write_headers,
            {"title": "topic"},
        )
        post_id = (await post_resp.get_json())["post"]["id"]
        reply_resp = await _post_json(
            client,
            f"/api/v1/community/{community_id}/interact/forum/posts/{post_id}/replies",
            write_headers,
            {"content": "to be deleted"},
        )
        reply_id = (await reply_resp.get_json())["reply"]["id"]

        delete_resp = await client.delete(
            f"/api/v1/admin/{community_id}/interaction/forum/replies/{reply_id}",
            headers=write_headers,
        )
        assert delete_resp.status_code == 200

        missing_resp = await client.delete(
            f"/api/v1/admin/{community_id}/interaction/forum/replies/9999", headers=write_headers
        )
        assert missing_resp.status_code == 404

    async def test_moderate_post_no_action_specified_is_400(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        write_headers = auth_headers(scope="community.interaction:write", user_id="1")
        channel_resp = await _post_json(
            client,
            f"/api/v1/admin/{community_id}/interaction/channels",
            write_headers,
            {"name": "mod-forum", "channel_type": "forum"},
        )
        channel_id = (await channel_resp.get_json())["channel"]["id"]
        post_resp = await _post_json(
            client,
            f"/api/v1/community/{community_id}/interact/forum/{channel_id}/posts",
            write_headers,
            {"title": "topic"},
        )
        post_id = (await post_resp.get_json())["post"]["id"]

        response = await client.put(
            f"/api/v1/admin/{community_id}/interaction/forum/posts/{post_id}",
            headers={**write_headers, "Content-Type": "application/json"},
            data=json_module.dumps({}),
        )
        assert response.status_code == 400

    async def test_moderate_post_delete_action(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        write_headers = auth_headers(scope="community.interaction:write", user_id="1")
        channel_resp = await _post_json(
            client,
            f"/api/v1/admin/{community_id}/interaction/channels",
            write_headers,
            {"name": "del-forum", "channel_type": "forum"},
        )
        channel_id = (await channel_resp.get_json())["channel"]["id"]
        post_resp = await _post_json(
            client,
            f"/api/v1/community/{community_id}/interact/forum/{channel_id}/posts",
            write_headers,
            {"title": "topic"},
        )
        post_id = (await post_resp.get_json())["post"]["id"]

        response = await client.put(
            f"/api/v1/admin/{community_id}/interaction/forum/posts/{post_id}",
            headers={**write_headers, "Content-Type": "application/json"},
            data=json_module.dumps({"delete": True}),
        )
        assert response.status_code == 200

        get_resp = await client.get(
            f"/api/v1/community/{community_id}/interact/forum/{channel_id}/posts/{post_id}",
            headers=auth_headers(scope="community.interaction:read"),
        )
        assert get_resp.status_code == 404

    async def test_create_forum_post_requires_title(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        write_headers = auth_headers(scope="community.interaction:write", user_id="1")
        channel_resp = await _post_json(
            client,
            f"/api/v1/admin/{community_id}/interaction/channels",
            write_headers,
            {"name": "notitle-forum", "channel_type": "forum"},
        )
        channel_id = (await channel_resp.get_json())["channel"]["id"]

        response = await _post_json(
            client,
            f"/api/v1/community/{community_id}/interact/forum/{channel_id}/posts",
            write_headers,
            {"title": ""},
        )
        assert response.status_code == 400

    async def test_create_forum_reply_requires_content(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        write_headers = auth_headers(scope="community.interaction:write", user_id="1")
        channel_resp = await _post_json(
            client,
            f"/api/v1/admin/{community_id}/interaction/channels",
            write_headers,
            {"name": "nocontent-forum", "channel_type": "forum"},
        )
        channel_id = (await channel_resp.get_json())["channel"]["id"]
        post_resp = await _post_json(
            client,
            f"/api/v1/community/{community_id}/interact/forum/{channel_id}/posts",
            write_headers,
            {"title": "topic"},
        )
        post_id = (await post_resp.get_json())["post"]["id"]

        response = await _post_json(
            client,
            f"/api/v1/community/{community_id}/interact/forum/posts/{post_id}/replies",
            write_headers,
            {"content": ""},
        )
        assert response.status_code == 400

    async def test_create_forum_reply_unknown_post_is_404(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        write_headers = auth_headers(scope="community.interaction:write", user_id="1")
        response = await _post_json(
            client,
            f"/api/v1/community/{community_id}/interact/forum/posts/9999/replies",
            write_headers,
            {"content": "hi"},
        )
        assert response.status_code == 404

    async def test_list_forum_posts_and_get_unknown_post(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        write_headers = auth_headers(scope="community.interaction:write", user_id="1")
        channel_resp = await _post_json(
            client,
            f"/api/v1/admin/{community_id}/interaction/channels",
            write_headers,
            {"name": "list-forum", "channel_type": "forum"},
        )
        channel_id = (await channel_resp.get_json())["channel"]["id"]
        await _post_json(
            client,
            f"/api/v1/community/{community_id}/interact/forum/{channel_id}/posts",
            write_headers,
            {"title": "topic 1"},
        )

        list_resp = await client.get(
            f"/api/v1/community/{community_id}/interact/forum/{channel_id}/posts",
            headers=auth_headers(scope="community.interaction:read"),
        )
        assert list_resp.status_code == 200
        list_body = await list_resp.get_json()
        assert list_body["pagination"]["total"] == 1
        assert len(list_body["posts"]) == 1

        missing_resp = await client.get(
            f"/api/v1/community/{community_id}/interact/forum/{channel_id}/posts/9999",
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
            f"/api/v1/admin/{community_id}/interaction/roles",
            headers=auth_headers(scope="community.interaction:read"),
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
            f"/api/v1/admin/{community_id}/interaction/roles/{role_id}",
            headers={**manage_headers, "Content-Type": "application/json"},
            data=json_module.dumps({"displayName": "VIP Member", "scopes": ["channels:read"]}),
        )
        assert update_resp.status_code == 200

        missing_resp = await client.put(
            f"/api/v1/admin/{community_id}/interaction/roles/9999",
            headers={**manage_headers, "Content-Type": "application/json"},
            data=json_module.dumps({"displayName": "x"}),
        )
        assert missing_resp.status_code == 404

    async def test_delete_unknown_role_is_404(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await client.delete(
            f"/api/v1/admin/{community_id}/interaction/roles/9999",
            headers=auth_headers(scope="users:admin", user_id="1"),
        )
        assert response.status_code == 404


class TestChannelPermissionOverrides:
    async def test_get_overrides_unknown_channel_is_404(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await client.get(
            f"/api/v1/admin/{community_id}/interaction/channels/9999/permissions",
            headers=auth_headers(scope="community.interaction:read"),
        )
        assert response.status_code == 404

    async def test_update_overrides_rejects_non_array_and_unknown_channel(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        manage_headers = auth_headers(scope="community.interaction:manage_channels")

        bad_shape_resp = await client.put(
            f"/api/v1/admin/{community_id}/interaction/channels/1/permissions",
            headers={**manage_headers, "Content-Type": "application/json"},
            data=json_module.dumps({"overrides": "not-a-list"}),
        )
        assert bad_shape_resp.status_code == 400

        missing_channel_resp = await client.put(
            f"/api/v1/admin/{community_id}/interaction/channels/9999/permissions",
            headers={**manage_headers, "Content-Type": "application/json"},
            data=json_module.dumps({"overrides": []}),
        )
        assert missing_channel_resp.status_code == 404

    async def test_get_and_update_overrides_full_flow(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        dal, community_id = community_db
        write_headers = auth_headers(scope="community.interaction:write", user_id="1")
        channel_resp = await _post_json(
            client,
            f"/api/v1/admin/{community_id}/interaction/channels",
            write_headers,
            {"name": "perm-chan"},
        )
        channel_id = (await channel_resp.get_json())["channel"]["id"]
        role_id = dal.community_roles.insert(community_id=community_id, name="member", priority=10)
        dal.commit()

        empty_resp = await client.get(
            f"/api/v1/admin/{community_id}/interaction/channels/{channel_id}/permissions",
            headers=auth_headers(scope="community.interaction:read"),
        )
        assert empty_resp.status_code == 200
        assert (await empty_resp.get_json())["overrides"] == []

        update_resp = await client.put(
            f"/api/v1/admin/{community_id}/interaction/channels/{channel_id}/permissions",
            headers={
                **auth_headers(scope="community.interaction:manage_channels"),
                "Content-Type": "application/json",
            },
            data=json_module.dumps(
                {
                    "overrides": [
                        {
                            "communityRoleId": role_id,
                            "grantScopes": ["channels:read"],
                            "denyScopes": [],
                        }
                    ]
                }
            ),
        )
        assert update_resp.status_code == 200

        after_resp = await client.get(
            f"/api/v1/admin/{community_id}/interaction/channels/{channel_id}/permissions",
            headers=auth_headers(scope="community.interaction:read"),
        )
        after_body = await after_resp.get_json()
        assert len(after_body["overrides"]) == 1
        assert after_body["overrides"][0]["role_name"] == "member"

        # An override entry with no communityRoleId is skipped, not inserted.
        skip_resp = await client.put(
            f"/api/v1/admin/{community_id}/interaction/channels/{channel_id}/permissions",
            headers={
                **auth_headers(scope="community.interaction:manage_channels"),
                "Content-Type": "application/json",
            },
            data=json_module.dumps({"overrides": [{"grantScopes": []}]}),
        )
        assert skip_resp.status_code == 200
        final_resp = await client.get(
            f"/api/v1/admin/{community_id}/interaction/channels/{channel_id}/permissions",
            headers=auth_headers(scope="community.interaction:read"),
        )
        assert (await final_resp.get_json())["overrides"] == []


class TestMemberChannels:
    async def test_list_channels_without_current_user_falls_back(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        """No `auth_required` on this route -- `getattr(request, "current_user", None)`.

        is `None` when the caller doesn't have write scope's `auth_required` chain.
        """
        _, community_id = community_db
        response = await client.get(
            f"/api/v1/community/{community_id}/interact/channels",
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
            f"/api/v1/community/{community_id}/interact/channels",
            auth_headers(scope="community.interaction:write", user_id="1"),
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
            f"/api/v1/community/{community_id}/interact/channels",
            auth_headers(scope="community.interaction:write", user_id="1"),
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

        _, community_id = community_db
        monkeypatch.setattr(bp_module, "can_create_channel", lambda *a, **k: True)

        response = await _post_json(
            client,
            f"/api/v1/community/{community_id}/interact/channels",
            auth_headers(scope="community.interaction:write", user_id="1"),
            {"name": "self-created-3"},
        )
        assert response.status_code == 201
        assert (await response.get_json())["channel"]["name"] == "self-created-3"
