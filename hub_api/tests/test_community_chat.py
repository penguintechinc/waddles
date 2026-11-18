"""`blueprints/v1/community_chat.py` -- chat history/channels REST port.

Fail-first proof (executed, not narrated): temporarily swapped
`chat_history`'s `require_scope("community.chat:read")` for
`community.chat:write`. Result: `test_wrong_scope_is_403` went red
(a `:write` token now satisfied the swapped requirement, returning 200
instead of the expected 403), `test_unknown_community_is_404` and
`test_correct_scope_returns_seeded_history` also went red (their `:read`
tokens no longer satisfied it, returning 403 instead of 404/200) -- 3 of
4 tests failed. `test_channels_always_includes_general` (a different
route, scope untouched) stayed green, confirming the swap's blast radius
was isolated to the one handler. Reverted, all 4 green again.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.community_chat import chat_bp
from tests.community_ids import UNKNOWN_PUBLIC_ID, cpath, install_community_ids


@pytest.fixture
def app(community_db: Any) -> Quart:
    dal, _ = community_db
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.config["dal"] = dal
    install_community_ids(quart_app, dal)
    quart_app.register_blueprint(chat_bp)
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


@pytest.fixture(autouse=True)
def _feature_enabled_default_on(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Default the `community.chat` two-gate Feature flag ON for every test in this file.

    `chat_history` gates on `feature_enabled(FEATURE_COMMUNITY_CHAT, ...)`
    (this PR) -- see `test_community_features_gate.py` for the dedicated
    OFF-blocks-handler proof. Every other test here exercises scope/tenant/
    proxy behavior unrelated to the flag gate.
    """
    import blueprints.v1.community_chat as chat_module

    monkeypatch.setattr(chat_module, "feature_enabled", AsyncMock(return_value=True))


class TestScopeEnforcement:
    async def test_wrong_scope_is_403(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        dal, community_id = community_db
        response = await client.get(
            cpath(dal, "/api/v1/community/{community_id}/chat/history", community_id),
            headers=auth_headers(scope="community.chat:write"),
        )
        assert response.status_code == 403


class TestChatHistory:
    async def test_unknown_community_is_404(self, client: Any, auth_headers: Any) -> None:
        response = await client.get(
            f"/api/v1/community/{UNKNOWN_PUBLIC_ID}/chat/history",
            headers=auth_headers(scope="community.chat:read"),
        )
        assert response.status_code == 404

    async def test_correct_scope_returns_seeded_history(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        dal, community_id = community_db
        dal.hub_chat_messages.insert(
            community_id=community_id,
            channel_name="general",
            sender_username="alice",
            message_content="hello world",
            message_type="text",
            created_at=datetime.utcnow(),
        )
        dal.commit()

        response = await client.get(
            cpath(dal, "/api/v1/community/{community_id}/chat/history", community_id),
            headers=auth_headers(scope="community.chat:read"),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["success"] is True
        assert len(body["messages"]) == 1
        assert body["messages"][0]["content"] == "hello world"
        assert body["messages"][0]["sender_username"] == "alice"

    async def test_channels_always_includes_general(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        dal, community_id = community_db
        response = await client.get(
            cpath(dal, "/api/v1/community/{community_id}/chat/channels", community_id),
            headers=auth_headers(scope="community.chat:read"),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["channels"][0]["name"] == "general"
        assert body["channels"][0]["message_count"] == 0
