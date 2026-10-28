"""Characterization tests for the Bot module (M5) -- `blueprints/v1/bot.py`.

Standalone Quart app registering only `bot_bp` (mirrors
`test_platform_blueprint.py`'s own pattern) -- real JWTs via `conftest.py`'s
`auth_headers`, an in-memory pydal DB with `bind_bot_tables` forced to
`migrate=True` against sqlite (the same override
`test_app_bundle_tables.py` uses for its own `migrate=False` production
tables -- see `db` fixture below). One test class per ported controller
(Shoutout / AIChatter / AI Knowledge / Server Manager), plus a dedicated
tenant-isolation class proving the `_require_community` boundary this
port adds on top of the frozen `/api/v1` contract.

Outbound calls (ai-interaction proxy, Ollama embeddings/completions,
server-manager-service proxy) are mocked at the `services.bot_*` module
boundary -- these are characterization tests of the HTTP contract this
port exposes, not integration tests of the downstream services.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

os.environ.setdefault("RCON_ENCRYPTION_KEY", "ab" * 32)

from blueprints.v1.bot import bot_bp  # noqa: E402 - env var must be set first
from services import bot_ai_chatter, bot_ai_knowledge, bot_rcon  # noqa: E402
from services.bot_tables import bind_bot_tables  # noqa: E402

OTHER_TENANT_SLUG = "other-tenant"


@pytest.fixture
def dal(tenant_db: Any) -> Any:
    """`tenant_db` (conftest.py) + Bot module tables, forced `migrate=True` for sqlite.

    `bind_bot_tables` always passes `migrate=False` in production (the
    schema is owned by the SQL migrations) -- wrapping `define_table` to
    override that for this in-memory test DB mirrors
    `libs/flask_core/tests/test_app_bundle_tables.py`'s own precedent for
    the exact same `migrate=False` production pattern.
    """
    original_define_table = tenant_db.define_table

    def _forced_migrate(name: str, *fields: Any, **kwargs: Any) -> Any:
        kwargs["migrate"] = True
        return original_define_table(name, *fields, **kwargs)

    tenant_db.define_table = _forced_migrate
    bind_bot_tables(tenant_db)
    tenant_db.define_table = original_define_table

    # `_user_id()` (bot.py) falls back to `0` for the test JWTs' non-numeric
    # `sub` ("u1" -- `conftest.py::make_token`, pre-M1 username-based
    # tokens) -- seed a matching `hub_users` row so `added_by`/`user_id`
    # FK columns (server_status_configs, shoutout_creators) resolve under
    # sqlite's enforced FK constraints (migrate=True here; production's
    # migrate=False skips this check, relying on the real, migrated schema).
    tenant_db.hub_users.insert(id=0, display_name="test-user")
    tenant_db.commit()
    return tenant_db


@pytest.fixture
def community_id(dal: Any) -> int:
    """A `communities` row owned by `conftest.py`'s seeded tenant (`acme-corp`)."""
    tenant_row = dal(dal.tenants.slug == "acme-corp").select().first()
    cid: int = dal.communities.insert(tenant_id=tenant_row.id)
    dal.commit()
    return cid


@pytest.fixture
def other_tenant_community_id(dal: Any) -> int:
    """A `communities` row owned by a *different* tenant -- proves `_require_community`."""
    other_tenant_id = dal.tenants.insert(slug=OTHER_TENANT_SLUG, is_active=True)
    dal.commit()
    cid: int = dal.communities.insert(tenant_id=other_tenant_id)
    dal.commit()
    return cid


@pytest.fixture
def app(dal: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(bot_bp)
    quart_app.config["dal"] = dal
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


# ═══════════════════════════════════════════════════════════════════════
# Tenant isolation -- fail-first proof #1 (auth-bypass prevention)
# ═══════════════════════════════════════════════════════════════════════


class TestTenantIsolation:
    """`_require_community` -- the path-param tenant re-verification this port adds."""

    async def test_missing_token_is_401(self, client: Any, community_id: int) -> None:
        """Fail-first auth-bypass check #1: no bearer token at all."""
        response = await client.get(f"/api/v1/admin/{community_id}/shoutout/config")
        assert response.status_code == 401

    async def test_community_from_other_tenant_is_403(
        self, client: Any, auth_headers: Any, other_tenant_community_id: int
    ) -> None:
        """Fail-first auth-bypass check #2.

        Valid token/scope, but `community_id` in the URL path belongs to a
        *different* tenant than the caller's JWT -- must be rejected, never
        silently served (security.md: never trust a path param).
        """
        headers = auth_headers(scope="bot.shoutout:read")
        response = await client.get(
            f"/api/v1/admin/{other_tenant_community_id}/shoutout/config", headers=headers
        )
        assert response.status_code == 403

    async def test_unknown_community_is_403(self, client: Any, auth_headers: Any) -> None:
        headers = auth_headers(scope="bot.shoutout:read")
        response = await client.get("/api/v1/admin/999999/shoutout/config", headers=headers)
        assert response.status_code == 403


class TestScopeEnforcement:
    """Fail-first scope-check proof."""

    async def test_missing_scope_is_403(
        self, client: Any, auth_headers: Any, community_id: int
    ) -> None:
        headers = auth_headers(scope="")
        response = await client.get(
            f"/api/v1/admin/{community_id}/shoutout/config", headers=headers
        )
        assert response.status_code == 403

    async def test_wrong_scope_is_403(
        self, client: Any, auth_headers: Any, community_id: int
    ) -> None:
        headers = auth_headers(scope="bot.rcon:read")
        response = await client.get(
            f"/api/v1/admin/{community_id}/shoutout/config", headers=headers
        )
        assert response.status_code == 403

    async def test_wildcard_scope_passes(
        self, client: Any, auth_headers: Any, community_id: int
    ) -> None:
        headers = auth_headers(scope="*:read")
        response = await client.get(
            f"/api/v1/admin/{community_id}/shoutout/config", headers=headers
        )
        assert response.status_code == 200


# ═══════════════════════════════════════════════════════════════════════
# Shoutout -- 6 endpoints (adminController.js's live shoutout functions)
# ═══════════════════════════════════════════════════════════════════════


class TestShoutoutConfig:
    async def test_get_creates_default_on_first_access(
        self, client: Any, auth_headers: Any, community_id: int
    ) -> None:
        response = await client.get(
            f"/api/v1/admin/{community_id}/shoutout/config",
            headers=auth_headers(scope="bot.shoutout:read"),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["success"] is True
        assert body["config"]["soPermission"] == "mod"
        assert body["config"]["widgetPosition"] == "bottom-right"

    async def test_put_persists_full_replace(
        self, client: Any, auth_headers: Any, community_id: int
    ) -> None:
        payload = {
            "soEnabled": False,
            "soPermission": "vip",
            "vsoEnabled": True,
            "vsoPermission": "everyone",
            "autoShoutoutMode": "all_creators",
            "triggerFirstMessage": True,
            "triggerRaidHost": False,
            "widgetPosition": "top-left",
            "widgetDurationSeconds": 45,
            "cooldownMinutes": 15,
        }
        response = await client.put(
            f"/api/v1/admin/{community_id}/shoutout/config",
            headers=auth_headers(scope="bot.shoutout:write"),
            json=payload,
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["config"]["soPermission"] == "vip"
        assert body["config"]["widgetDurationSeconds"] == 45

        # Round-trip: a subsequent GET sees the persisted update.
        get_response = await client.get(
            f"/api/v1/admin/{community_id}/shoutout/config",
            headers=auth_headers(scope="bot.shoutout:read"),
        )
        assert (await get_response.get_json())["config"]["soPermission"] == "vip"

    async def test_put_read_scope_is_403(
        self, client: Any, auth_headers: Any, community_id: int
    ) -> None:
        response = await client.put(
            f"/api/v1/admin/{community_id}/shoutout/config",
            headers=auth_headers(scope="bot.shoutout:read"),
            json={
                "soEnabled": True,
                "soPermission": "mod",
                "vsoEnabled": True,
                "vsoPermission": "mod",
                "autoShoutoutMode": "disabled",
                "triggerFirstMessage": False,
                "triggerRaidHost": True,
                "widgetPosition": "bottom-right",
                "widgetDurationSeconds": 30,
                "cooldownMinutes": 60,
            },
        )
        assert response.status_code == 403


class TestShoutoutCreators:
    async def test_list_empty(self, client: Any, auth_headers: Any, community_id: int) -> None:
        response = await client.get(
            f"/api/v1/admin/{community_id}/shoutout/creators",
            headers=auth_headers(scope="bot.shoutout:read"),
        )
        assert response.status_code == 200
        assert (await response.get_json())["creators"] == []

    async def test_add_success(self, client: Any, auth_headers: Any, community_id: int) -> None:
        response = await client.post(
            f"/api/v1/admin/{community_id}/shoutout/creators",
            headers=auth_headers(scope="bot.shoutout:write"),
            json={"platform": "twitch", "username": "SomeStreamer"},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["creator"]["platformUsername"] == "SomeStreamer"

    async def test_add_missing_fields_is_400(
        self, client: Any, auth_headers: Any, community_id: int
    ) -> None:
        response = await client.post(
            f"/api/v1/admin/{community_id}/shoutout/creators",
            headers=auth_headers(scope="bot.shoutout:write"),
            json={"platform": "twitch"},
        )
        assert response.status_code == 400

    async def test_add_duplicate_is_409(
        self, client: Any, auth_headers: Any, community_id: int
    ) -> None:
        headers = auth_headers(scope="bot.shoutout:write")
        payload = {"platform": "twitch", "username": "dupe"}
        first = await client.post(
            f"/api/v1/admin/{community_id}/shoutout/creators", headers=headers, json=payload
        )
        assert first.status_code == 200
        second = await client.post(
            f"/api/v1/admin/{community_id}/shoutout/creators", headers=headers, json=payload
        )
        assert second.status_code == 409

    async def test_remove_success(self, client: Any, auth_headers: Any, community_id: int) -> None:
        headers = auth_headers(scope="bot.shoutout:write")
        created = await client.post(
            f"/api/v1/admin/{community_id}/shoutout/creators",
            headers=headers,
            json={"platform": "youtube", "username": "vidcreator"},
        )
        creator_id = (await created.get_json())["creator"]["id"]
        response = await client.delete(
            f"/api/v1/admin/{community_id}/shoutout/creators/{creator_id}", headers=headers
        )
        assert response.status_code == 200
        assert (await response.get_json())["message"] == "Creator removed"

    async def test_remove_not_found_is_404(
        self, client: Any, auth_headers: Any, community_id: int
    ) -> None:
        response = await client.delete(
            f"/api/v1/admin/{community_id}/shoutout/creators/999999",
            headers=auth_headers(scope="bot.shoutout:write"),
        )
        assert response.status_code == 404


class TestShoutoutHistory:
    async def test_empty_history_pagination_shape(
        self, client: Any, auth_headers: Any, community_id: int
    ) -> None:
        response = await client.get(
            f"/api/v1/admin/{community_id}/shoutout/history",
            headers=auth_headers(scope="bot.shoutout:read"),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["history"] == []
        assert body["pagination"] == {"page": 1, "limit": 25, "total": 0, "totalPages": 0}

    async def test_history_reflects_seeded_rows(
        self, client: Any, auth_headers: Any, community_id: int, dal: Any
    ) -> None:
        dal.shoutout_history.insert(
            community_id=community_id,
            platform="twitch",
            target_username="viewer1",
            shoutout_type="text",
            trigger_type="manual",
        )
        dal.commit()
        response = await client.get(
            f"/api/v1/admin/{community_id}/shoutout/history?limit=10",
            headers=auth_headers(scope="bot.shoutout:read"),
        )
        body = await response.get_json()
        assert body["pagination"]["total"] == 1
        assert body["history"][0]["targetUsername"] == "viewer1"


# ═══════════════════════════════════════════════════════════════════════
# AIChatter -- 2 endpoints, pure proxy (mocked at the service boundary)
# ═══════════════════════════════════════════════════════════════════════


class TestAIChatter:
    async def test_get_config_proxies_service_response(
        self, client: Any, auth_headers: Any, community_id: int, monkeypatch: Any
    ) -> None:
        async def fake_get(cid: int) -> dict[str, Any]:
            assert cid == community_id
            return {"enabled": True, "max_responses_per_window": 5}

        monkeypatch.setattr(bot_ai_chatter, "get_chatter_config", fake_get)
        response = await client.get(
            f"/api/v1/admin/{community_id}/ai-chatter/config",
            headers=auth_headers(scope="bot.ai_chatter:read"),
        )
        assert response.status_code == 200
        body = await response.get_json()
        # Output-DTO: exact field set (partial upstream bodies are filled with defaults).
        assert body == {
            "success": True,
            "config": {
                "enabled": True,
                "max_responses_per_window": 5,
                "window_seconds": 600,
                "max_per_user_per_window": 2,
                "response_probability": 0.30,
                "min_message_length": 10,
            },
        }

    async def test_get_config_drops_unknown_upstream_keys(
        self, client: Any, auth_headers: Any, community_id: int, monkeypatch: Any
    ) -> None:
        """Regression (output-DTO audit): relayed upstream extras never reach the client."""

        async def fake_get(cid: int) -> dict[str, Any]:
            return {"enabled": True, "internal_api_key": "sk-secret", "model_endpoint": "http://x"}

        monkeypatch.setattr(bot_ai_chatter, "get_chatter_config", fake_get)
        response = await client.get(
            f"/api/v1/admin/{community_id}/ai-chatter/config",
            headers=auth_headers(scope="bot.ai_chatter:read"),
        )
        body = await response.get_json()
        assert set(body["config"]) == {
            "enabled",
            "max_responses_per_window",
            "window_seconds",
            "max_per_user_per_window",
            "response_probability",
            "min_message_length",
        }

    async def test_update_config_proxies_service_response(
        self, client: Any, auth_headers: Any, community_id: int, monkeypatch: Any
    ) -> None:
        async def fake_update(
            cid: int, update: bot_ai_chatter.AIChatterConfigUpdate
        ) -> dict[str, Any]:
            assert cid == community_id
            assert update.enabled is True
            return {"enabled": True}

        monkeypatch.setattr(bot_ai_chatter, "update_chatter_config", fake_update)
        response = await client.put(
            f"/api/v1/admin/{community_id}/ai-chatter/config",
            headers=auth_headers(scope="bot.ai_chatter:write"),
            json={"enabled": True},
        )
        assert response.status_code == 200

    async def test_update_config_out_of_range_is_400(
        self, client: Any, auth_headers: Any, community_id: int
    ) -> None:
        """Real (unmocked) validation -- `AIChatterValidationError` -> 400, matches Node."""
        response = await client.put(
            f"/api/v1/admin/{community_id}/ai-chatter/config",
            headers=auth_headers(scope="bot.ai_chatter:write"),
            json={"window_seconds": 1},
        )
        assert response.status_code == 400


# ═══════════════════════════════════════════════════════════════════════
# AI Knowledge -- 8 endpoints (no community_id path param)
# ═══════════════════════════════════════════════════════════════════════


class TestAIKnowledgeSources:
    async def test_list_empty(self, client: Any, auth_headers: Any) -> None:
        response = await client.get(
            "/api/v1/admin/ai-knowledge/sources",
            headers=auth_headers(scope="bot.ai_knowledge:read"),
        )
        assert response.status_code == 200
        assert (await response.get_json())["sources"] == []

    async def test_create_manual_source_no_background_index(
        self, client: Any, auth_headers: Any, monkeypatch: Any
    ) -> None:
        called = False

        async def fake_index(dal: Any, source_id: int) -> None:
            nonlocal called
            called = True

        monkeypatch.setattr(bot_ai_knowledge, "index_source", fake_index)
        response = await client.post(
            "/api/v1/admin/ai-knowledge/sources",
            headers=auth_headers(scope="bot.ai_knowledge:write"),
            json={"source_name": "Manual FAQ", "source_type": "manual"},
        )
        assert response.status_code == 201
        body = await response.get_json()
        assert body["source"]["source_name"] == "Manual FAQ"
        assert called is False

    async def test_create_non_manual_source_triggers_background_index(
        self, client: Any, auth_headers: Any, monkeypatch: Any
    ) -> None:
        called_with: list[int] = []

        async def fake_index(dal: Any, source_id: int) -> None:
            called_with.append(source_id)

        monkeypatch.setattr(bot_ai_knowledge, "index_source", fake_index)
        response = await client.post(
            "/api/v1/admin/ai-knowledge/sources",
            headers=auth_headers(scope="bot.ai_knowledge:write"),
            json={
                "source_name": "Docs",
                "source_type": "mkdocs",
                "source_url": "https://example.com",
            },
        )
        assert response.status_code == 201
        # Fire-and-forget: give the event loop one tick to run the task.
        import asyncio

        await asyncio.sleep(0)
        assert len(called_with) == 1

    async def test_create_invalid_source_type_is_400(self, client: Any, auth_headers: Any) -> None:
        response = await client.post(
            "/api/v1/admin/ai-knowledge/sources",
            headers=auth_headers(scope="bot.ai_knowledge:write"),
            json={"source_name": "Bad", "source_type": "not-a-type"},
        )
        assert response.status_code == 400

    async def test_update_source(self, client: Any, auth_headers: Any, dal: Any) -> None:
        source_id = dal.ai_knowledge_sources.insert(
            source_name="Old", source_type="manual", is_active=True
        )
        dal.commit()
        response = await client.put(
            f"/api/v1/admin/ai-knowledge/sources/{source_id}",
            headers=auth_headers(scope="bot.ai_knowledge:write"),
            json={"source_name": "New"},
        )
        assert response.status_code == 200
        assert (await response.get_json())["source"]["source_name"] == "New"

    async def test_update_source_not_found_is_404(self, client: Any, auth_headers: Any) -> None:
        response = await client.put(
            "/api/v1/admin/ai-knowledge/sources/999999",
            headers=auth_headers(scope="bot.ai_knowledge:write"),
            json={"source_name": "New"},
        )
        assert response.status_code == 404

    async def test_delete_source(self, client: Any, auth_headers: Any, dal: Any) -> None:
        source_id = dal.ai_knowledge_sources.insert(
            source_name="ToDelete", source_type="manual", is_active=True
        )
        dal.commit()
        response = await client.delete(
            f"/api/v1/admin/ai-knowledge/sources/{source_id}",
            headers=auth_headers(scope="bot.ai_knowledge:write"),
        )
        assert response.status_code == 200

    async def test_delete_source_not_found_is_404(self, client: Any, auth_headers: Any) -> None:
        response = await client.delete(
            "/api/v1/admin/ai-knowledge/sources/999999",
            headers=auth_headers(scope="bot.ai_knowledge:write"),
        )
        assert response.status_code == 404

    async def test_reindex_triggers_background_index(
        self, client: Any, auth_headers: Any, dal: Any, monkeypatch: Any
    ) -> None:
        source_id = dal.ai_knowledge_sources.insert(
            source_name="R", source_type="manual", is_active=True
        )
        dal.commit()
        called_with: list[int] = []

        async def fake_index(dal_arg: Any, sid: int) -> None:
            called_with.append(sid)

        monkeypatch.setattr(bot_ai_knowledge, "index_source", fake_index)
        response = await client.post(
            f"/api/v1/admin/ai-knowledge/sources/{source_id}/reindex",
            headers=auth_headers(scope="bot.ai_knowledge:write"),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body == {"success": True, "message": "Reindex started", "sourceId": source_id}
        import asyncio

        await asyncio.sleep(0)
        assert called_with == [source_id]


class TestAIKnowledgeSourceSSRFGuard:
    """Write-time SSRF rejection -- `add_knowledge_source`/`update_knowledge_source`.

    Fetch-time coverage (the crawler itself) lives in `test_url_guard.py`'s
    `TestGuardedGetRedirects`; these exercise the HTTP-layer boundary a
    malicious `source_url` never even gets persisted through.
    """

    async def test_create_rejects_loopback_url(self, client: Any, auth_headers: Any) -> None:
        response = await client.post(
            "/api/v1/admin/ai-knowledge/sources",
            headers=auth_headers(scope="bot.ai_knowledge:write"),
            json={
                "source_name": "Evil",
                "source_type": "mkdocs",
                "source_url": "http://127.0.0.1/admin",
            },
        )
        assert response.status_code == 400

    async def test_create_rejects_cloud_metadata_url(self, client: Any, auth_headers: Any) -> None:
        response = await client.post(
            "/api/v1/admin/ai-knowledge/sources",
            headers=auth_headers(scope="bot.ai_knowledge:write"),
            json={
                "source_name": "Evil",
                "source_type": "generic_url",
                "source_url": "http://169.254.169.254/latest/meta-data/",
            },
        )
        assert response.status_code == 400

    async def test_create_rejects_private_10_range_url(
        self, client: Any, auth_headers: Any
    ) -> None:
        response = await client.post(
            "/api/v1/admin/ai-knowledge/sources",
            headers=auth_headers(scope="bot.ai_knowledge:write"),
            json={
                "source_name": "Evil",
                "source_type": "generic_url",
                "source_url": "http://10.0.0.5/internal",
            },
        )
        assert response.status_code == 400

    async def test_create_rejects_private_192_168_range_url(
        self, client: Any, auth_headers: Any
    ) -> None:
        response = await client.post(
            "/api/v1/admin/ai-knowledge/sources",
            headers=auth_headers(scope="bot.ai_knowledge:write"),
            json={
                "source_name": "Evil",
                "source_type": "generic_url",
                "source_url": "http://192.168.1.1/router",
            },
        )
        assert response.status_code == 400

    async def test_create_rejects_file_scheme(self, client: Any, auth_headers: Any) -> None:
        response = await client.post(
            "/api/v1/admin/ai-knowledge/sources",
            headers=auth_headers(scope="bot.ai_knowledge:write"),
            json={
                "source_name": "Evil",
                "source_type": "generic_url",
                "source_url": "file:///etc/passwd",
            },
        )
        assert response.status_code == 400

    async def test_create_rejects_gopher_scheme(self, client: Any, auth_headers: Any) -> None:
        response = await client.post(
            "/api/v1/admin/ai-knowledge/sources",
            headers=auth_headers(scope="bot.ai_knowledge:write"),
            json={
                "source_name": "Evil",
                "source_type": "generic_url",
                "source_url": "gopher://127.0.0.1/x",
            },
        )
        assert response.status_code == 400

    async def test_create_allows_normal_public_url(
        self, client: Any, auth_headers: Any, monkeypatch: Any
    ) -> None:
        """A genuinely public target (literal IP -- no real DNS needed) is still allowed."""

        async def fake_index(dal: Any, source_id: int) -> None:
            return None

        monkeypatch.setattr(bot_ai_knowledge, "index_source", fake_index)
        response = await client.post(
            "/api/v1/admin/ai-knowledge/sources",
            headers=auth_headers(scope="bot.ai_knowledge:write"),
            json={
                "source_name": "Public Docs",
                "source_type": "generic_url",
                "source_url": "http://1.2.3.4/docs",
            },
        )
        assert response.status_code == 201

    async def test_update_rejects_loopback_url(
        self, client: Any, auth_headers: Any, dal: Any
    ) -> None:
        source_id = dal.ai_knowledge_sources.insert(
            source_name="Old", source_type="manual", is_active=True
        )
        dal.commit()
        response = await client.put(
            f"/api/v1/admin/ai-knowledge/sources/{source_id}",
            headers=auth_headers(scope="bot.ai_knowledge:write"),
            json={"source_url": "http://127.0.0.1/admin"},
        )
        assert response.status_code == 400

    async def test_update_rejects_cloud_metadata_url(
        self, client: Any, auth_headers: Any, dal: Any
    ) -> None:
        source_id = dal.ai_knowledge_sources.insert(
            source_name="Old", source_type="manual", is_active=True
        )
        dal.commit()
        response = await client.put(
            f"/api/v1/admin/ai-knowledge/sources/{source_id}",
            headers=auth_headers(scope="bot.ai_knowledge:write"),
            json={"source_url": "http://169.254.169.254/latest/meta-data/"},
        )
        assert response.status_code == 400


class TestAIKnowledgeSearchAndSuggest:
    async def test_search_missing_query_is_400(self, client: Any, auth_headers: Any) -> None:
        response = await client.post(
            "/api/v1/admin/ai-knowledge/search",
            headers=auth_headers(scope="bot.ai_knowledge:read"),
            json={"query": "   "},
        )
        assert response.status_code == 400

    async def test_search_proxies_service_results(
        self, client: Any, auth_headers: Any, monkeypatch: Any
    ) -> None:
        async def fake_search(dal: Any, query: str, **kwargs: Any) -> list[Any]:
            assert query == "how do I reset my password"
            return [
                bot_ai_knowledge.KnowledgeSearchResult(
                    chunk_id=1,
                    source_id=2,
                    content="Reset via settings.",
                    source_url="https://docs.example.com/reset",
                    source_title="Password Reset",
                    chunk_index=0,
                    token_count=5,
                    score=0.91,
                )
            ]

        monkeypatch.setattr(bot_ai_knowledge, "search_knowledge", fake_search)
        response = await client.post(
            "/api/v1/admin/ai-knowledge/search",
            headers=auth_headers(scope="bot.ai_knowledge:read"),
            json={"query": "how do I reset my password"},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["results"][0]["score"] == 0.91
        assert body["results"][0]["chunk"]["id"] == 1

    async def test_suggest_missing_fields_is_400(self, client: Any, auth_headers: Any) -> None:
        response = await client.post(
            "/api/v1/admin/ai-knowledge/suggest",
            headers=auth_headers(scope="bot.ai_knowledge:write"),
            json={"ticketId": 1, "ticketText": ""},
        )
        assert response.status_code == 400

    async def test_suggest_below_confidence_threshold_returns_null_suggestion(
        self, client: Any, auth_headers: Any, monkeypatch: Any
    ) -> None:
        async def fake_generate(dal: Any, ticket_id: int, text: str, **kwargs: Any) -> None:
            return None

        monkeypatch.setattr(bot_ai_knowledge, "generate_suggestion", fake_generate)
        response = await client.post(
            "/api/v1/admin/ai-knowledge/suggest",
            headers=auth_headers(scope="bot.ai_knowledge:write"),
            json={"ticketId": 1, "ticketText": "help me"},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["suggestion"] is None

    async def test_suggest_found_returns_201(
        self, client: Any, auth_headers: Any, monkeypatch: Any
    ) -> None:
        async def fake_generate(dal: Any, ticket_id: int, text: str, **kwargs: Any) -> Any:
            return bot_ai_knowledge.TicketSuggestion(
                id=1,
                ticket_id=ticket_id,
                suggestion_text="Try resetting via settings [1].",
                confidence_score=0.85,
                cited_chunks=[1],
                feedback=None,
                is_auto_posted=False,
                created_at="2026-08-31T00:00:00",
            )

        monkeypatch.setattr(bot_ai_knowledge, "generate_suggestion", fake_generate)
        response = await client.post(
            "/api/v1/admin/ai-knowledge/suggest",
            headers=auth_headers(scope="bot.ai_knowledge:write"),
            json={"ticketId": 1, "ticketText": "help me reset"},
        )
        assert response.status_code == 201
        body = await response.get_json()
        assert body["suggestion"]["confidence_score"] == 0.85

    async def test_feedback_invalid_value_is_400(self, client: Any, auth_headers: Any) -> None:
        response = await client.post(
            "/api/v1/admin/ai-knowledge/suggestions/1/feedback",
            headers=auth_headers(scope="bot.ai_knowledge:write"),
            json={"feedback": "meh"},
        )
        assert response.status_code == 400

    async def test_feedback_not_found_is_404(self, client: Any, auth_headers: Any) -> None:
        response = await client.post(
            "/api/v1/admin/ai-knowledge/suggestions/999999/feedback",
            headers=auth_headers(scope="bot.ai_knowledge:write"),
            json={"feedback": "helpful"},
        )
        assert response.status_code == 404

    async def test_feedback_success(self, client: Any, auth_headers: Any, dal: Any) -> None:
        suggestion_id = dal.ai_ticket_suggestions.insert(
            ticket_id=1, suggestion_text="x", confidence_score=0.9, cited_chunks=[]
        )
        dal.commit()
        response = await client.post(
            f"/api/v1/admin/ai-knowledge/suggestions/{suggestion_id}/feedback",
            headers=auth_headers(scope="bot.ai_knowledge:write"),
            json={"feedback": "helpful"},
        )
        assert response.status_code == 200
        assert (await response.get_json())["suggestion"]["feedback"] == "helpful"


# ═══════════════════════════════════════════════════════════════════════
# Server Manager / RCON -- 19 route registrations (18 unique handlers)
# ═══════════════════════════════════════════════════════════════════════


class TestRconServerCrud:
    async def test_list_admin_view_includes_host(
        self, client: Any, auth_headers: Any, community_id: int, dal: Any
    ) -> None:
        dal.server_status_configs.insert(
            community_id=community_id, display_name="Rust #1", game_name="rust1", host="1.2.3.4"
        )
        dal.commit()
        response = await client.get(
            f"/api/v1/admin/{community_id}/rcon/servers",
            headers=auth_headers(scope="bot.server_manager:admin"),
        )
        assert response.status_code == 200
        servers = (await response.get_json())["servers"]
        assert servers[0]["host"] == "1.2.3.4"

    async def test_create_success(self, client: Any, auth_headers: Any, community_id: int) -> None:
        response = await client.post(
            f"/api/v1/admin/{community_id}/rcon/servers",
            headers=auth_headers(scope="bot.server_manager:admin"),
            json={"display_name": "My Server", "host": "1.2.3.5", "password": "secret123"},
        )
        assert response.status_code == 201
        body = await response.get_json()
        assert body["server"]["display_name"] == "My Server"
        assert body["server"]["game_name"] == "my_server"

    async def test_create_private_ip_is_400(
        self, client: Any, auth_headers: Any, community_id: int
    ) -> None:
        response = await client.post(
            f"/api/v1/admin/{community_id}/rcon/servers",
            headers=auth_headers(scope="bot.server_manager:admin"),
            json={"display_name": "Local", "host": "192.168.1.5"},
        )
        assert response.status_code == 400

    async def test_create_missing_display_name_is_400(
        self, client: Any, auth_headers: Any, community_id: int
    ) -> None:
        response = await client.post(
            f"/api/v1/admin/{community_id}/rcon/servers",
            headers=auth_headers(scope="bot.server_manager:admin"),
            json={"display_name": "  ", "host": "1.2.3.5"},
        )
        assert response.status_code == 400

    async def test_update_success(
        self, client: Any, auth_headers: Any, community_id: int, dal: Any
    ) -> None:
        server_id = dal.server_status_configs.insert(
            community_id=community_id, display_name="Old", game_name="old", host="1.2.3.5"
        )
        dal.commit()
        response = await client.put(
            f"/api/v1/admin/{community_id}/rcon/servers/{server_id}",
            headers=auth_headers(scope="bot.server_manager:admin"),
            json={"display_name": "New Name"},
        )
        assert response.status_code == 200
        assert (await response.get_json())["server"]["display_name"] == "New Name"

    async def test_update_not_found_is_404(
        self, client: Any, auth_headers: Any, community_id: int
    ) -> None:
        response = await client.put(
            f"/api/v1/admin/{community_id}/rcon/servers/999999",
            headers=auth_headers(scope="bot.server_manager:admin"),
            json={"display_name": "New"},
        )
        assert response.status_code == 404

    async def test_delete_success(
        self, client: Any, auth_headers: Any, community_id: int, dal: Any
    ) -> None:
        server_id = dal.server_status_configs.insert(
            community_id=community_id, display_name="Del", game_name="del", host="1.2.3.5"
        )
        dal.commit()
        response = await client.delete(
            f"/api/v1/admin/{community_id}/rcon/servers/{server_id}",
            headers=auth_headers(scope="bot.server_manager:admin"),
        )
        assert response.status_code == 200
        assert (await response.get_json())["message"] == "Server deleted"

    async def test_delete_not_found_is_404(
        self, client: Any, auth_headers: Any, community_id: int
    ) -> None:
        response = await client.delete(
            f"/api/v1/admin/{community_id}/rcon/servers/999999",
            headers=auth_headers(scope="bot.server_manager:admin"),
        )
        assert response.status_code == 404


@pytest.fixture
def seeded_server_id(dal: Any, community_id: int) -> int:
    server_id: int = dal.server_status_configs.insert(
        community_id=community_id, display_name="S1", game_name="s1", host="1.2.3.5"
    )
    dal.commit()
    return server_id


class TestRconLiveCommandProxy:
    """Every route here proxies to `server-manager-service` -- `bot_rcon._proxy` is mocked."""

    @pytest.fixture(autouse=True)
    def _mock_proxy(self, monkeypatch: Any) -> list[tuple[str, str, Any]]:
        calls: list[tuple[str, str, Any]] = []

        async def fake_proxy(path: str, method: str = "GET", body: Any = None) -> Any:
            calls.append((path, method, body))
            return {"ok": True}

        monkeypatch.setattr(bot_rcon, "_proxy", fake_proxy)
        return calls

    async def test_test_connection(
        self, client: Any, auth_headers: Any, community_id: int, seeded_server_id: int
    ) -> None:
        response = await client.post(
            f"/api/v1/admin/{community_id}/rcon/servers/{seeded_server_id}/test",
            headers=auth_headers(scope="bot.server_manager:admin"),
            json={"password": "x"},
        )
        assert response.status_code == 200
        assert (await response.get_json()) == {"ok": True}

    async def test_execute_command(
        self, client: Any, auth_headers: Any, community_id: int, seeded_server_id: int
    ) -> None:
        response = await client.post(
            f"/api/v1/admin/{community_id}/rcon/servers/{seeded_server_id}/command",
            headers=auth_headers(scope="bot.server_manager:admin"),
            json={"command": "status"},
        )
        assert response.status_code == 200

    async def test_execute_command_missing_command_is_400(
        self, client: Any, auth_headers: Any, community_id: int, seeded_server_id: int
    ) -> None:
        response = await client.post(
            f"/api/v1/admin/{community_id}/rcon/servers/{seeded_server_id}/command",
            headers=auth_headers(scope="bot.server_manager:admin"),
            json={"command": "  "},
        )
        assert response.status_code == 400

    async def test_kick_player(
        self, client: Any, auth_headers: Any, community_id: int, seeded_server_id: int
    ) -> None:
        response = await client.post(
            f"/api/v1/admin/{community_id}/rcon/servers/{seeded_server_id}/kick",
            headers=auth_headers(scope="bot.server_manager:admin"),
            json={"player": "griefer"},
        )
        assert response.status_code == 200

    async def test_kick_player_missing_player_is_400(
        self, client: Any, auth_headers: Any, community_id: int, seeded_server_id: int
    ) -> None:
        response = await client.post(
            f"/api/v1/admin/{community_id}/rcon/servers/{seeded_server_id}/kick",
            headers=auth_headers(scope="bot.server_manager:admin"),
            json={"player": ""},
        )
        assert response.status_code == 400

    async def test_ban_player(
        self, client: Any, auth_headers: Any, community_id: int, seeded_server_id: int
    ) -> None:
        response = await client.post(
            f"/api/v1/admin/{community_id}/rcon/servers/{seeded_server_id}/ban",
            headers=auth_headers(scope="bot.server_manager:admin"),
            json={"player": "cheater", "duration": 0},
        )
        assert response.status_code == 200

    async def test_get_channels(
        self, client: Any, auth_headers: Any, community_id: int, seeded_server_id: int
    ) -> None:
        response = await client.get(
            f"/api/v1/admin/{community_id}/rcon/servers/{seeded_server_id}/channels",
            headers=auth_headers(scope="bot.server_manager:admin"),
        )
        assert response.status_code == 200

    async def test_move_user(
        self, client: Any, auth_headers: Any, community_id: int, seeded_server_id: int
    ) -> None:
        response = await client.post(
            f"/api/v1/admin/{community_id}/rcon/servers/{seeded_server_id}/move",
            headers=auth_headers(scope="bot.server_manager:admin"),
            json={"user_id": "42", "channel_id": 3},
        )
        assert response.status_code == 200

    async def test_send_message(
        self, client: Any, auth_headers: Any, community_id: int, seeded_server_id: int
    ) -> None:
        response = await client.post(
            f"/api/v1/admin/{community_id}/rcon/servers/{seeded_server_id}/message",
            headers=auth_headers(scope="bot.server_manager:admin"),
            json={"text": "hello"},
        )
        assert response.status_code == 200

    async def test_send_message_missing_text_is_400(
        self, client: Any, auth_headers: Any, community_id: int, seeded_server_id: int
    ) -> None:
        response = await client.post(
            f"/api/v1/admin/{community_id}/rcon/servers/{seeded_server_id}/message",
            headers=auth_headers(scope="bot.server_manager:admin"),
            json={"text": ""},
        )
        assert response.status_code == 400

    async def test_get_access_policy(
        self, client: Any, auth_headers: Any, community_id: int, seeded_server_id: int
    ) -> None:
        response = await client.get(
            f"/api/v1/admin/{community_id}/rcon/servers/{seeded_server_id}/policy",
            headers=auth_headers(scope="bot.server_manager:admin"),
        )
        assert response.status_code == 200

    async def test_update_access_policy(
        self, client: Any, auth_headers: Any, community_id: int, seeded_server_id: int
    ) -> None:
        response = await client.put(
            f"/api/v1/admin/{community_id}/rcon/servers/{seeded_server_id}/policy",
            headers=auth_headers(scope="bot.server_manager:admin"),
            json={"auto_kick_enabled": True},
        )
        assert response.status_code == 200

    async def test_trigger_enforcement(
        self, client: Any, auth_headers: Any, community_id: int, seeded_server_id: int
    ) -> None:
        response = await client.post(
            f"/api/v1/admin/{community_id}/rcon/servers/{seeded_server_id}/enforce",
            headers=auth_headers(scope="bot.server_manager:admin"),
        )
        assert response.status_code == 200

    async def test_get_access_log(
        self, client: Any, auth_headers: Any, community_id: int, seeded_server_id: int
    ) -> None:
        response = await client.get(
            f"/api/v1/admin/{community_id}/rcon/servers/{seeded_server_id}/access-log",
            headers=auth_headers(scope="bot.server_manager:admin"),
        )
        assert response.status_code == 200

    async def test_member_status(
        self, client: Any, auth_headers: Any, community_id: int, seeded_server_id: int
    ) -> None:
        response = await client.get(
            f"/api/v1/admin/{community_id}/rcon/info/{seeded_server_id}/status",
            headers=auth_headers(scope="bot.server_manager:read"),
        )
        assert response.status_code == 200

    async def test_member_players(
        self, client: Any, auth_headers: Any, community_id: int, seeded_server_id: int
    ) -> None:
        response = await client.get(
            f"/api/v1/admin/{community_id}/rcon/info/{seeded_server_id}/players",
            headers=auth_headers(scope="bot.server_manager:read"),
        )
        assert response.status_code == 200


class TestRconCommandLogAndMemberInfo:
    async def test_command_log_empty(
        self, client: Any, auth_headers: Any, community_id: int
    ) -> None:
        response = await client.get(
            f"/api/v1/admin/{community_id}/rcon/log",
            headers=auth_headers(scope="bot.server_manager:admin"),
        )
        assert response.status_code == 200
        assert (await response.get_json())["log"] == []

    async def test_member_info_excludes_admin_only_servers(
        self, client: Any, auth_headers: Any, community_id: int, dal: Any
    ) -> None:
        dal.server_status_configs.insert(
            community_id=community_id,
            display_name="Hidden",
            game_name="hidden",
            host="1.2.3.9",
            visibility="admin_only",
        )
        dal.server_status_configs.insert(
            community_id=community_id,
            display_name="Visible",
            game_name="visible",
            host="1.2.3.10",
            visibility="members",
        )
        dal.commit()
        response = await client.get(
            f"/api/v1/admin/{community_id}/rcon/info",
            headers=auth_headers(scope="bot.server_manager:read"),
        )
        assert response.status_code == 200
        servers = (await response.get_json())["servers"]
        assert [s["display_name"] for s in servers] == ["Visible"]
        assert "host" not in servers[0]
