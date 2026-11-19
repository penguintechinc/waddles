"""Community blueprints -- two-gate Feature guard proofs (SCCEBM per-capability flags).

Dedicated fail-first-verify coverage for the one-line `feature_enabled(...)`
guards added this PR to `community_chat.py`, `community_polls.py`,
`community_interaction.py` (both `community.interactions` and the
separate `community.forums` capability), and `community_loyalty.py` (the
one Professional-tier capability among this set). Every OTHER test file
for these blueprints (`test_community_chat.py` et al.) defaults the gate
ON via its own `_feature_enabled_default_on` autouse fixture and exercises
routing/scope/tenant/proxy behavior instead -- this file is the
complementary "the gate itself actually gates" proof, mirroring
`test_v1_analytics_blueprint.py`'s existing `feature_enabled` mocking
pattern for `analytics.community_health`.

Fail-first proof (executed, not narrated): temporarily reverted
`community_chat.py::chat_history`'s guard (removed the `if not await
feature_enabled(...)` block) and re-ran `test_chat_history_blocked_when_
flag_off` -- it went red (`assert 200 == 402`, the stubbed history
returned instead of the 402 gate). Reverted; green again.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.community_chat import chat_bp
from blueprints.v1.community_interaction import (
    interaction_admin_bp,
    interaction_member_bp,
)
from blueprints.v1.community_loyalty import loyalty_bp
from blueprints.v1.community_polls import polls_bp
from tests.community_ids import cpath, install_community_ids


class TestChatFeatureGate:
    @pytest.fixture
    def app(self, community_db: Any) -> Quart:
        dal, _ = community_db
        quart_app = Quart(__name__)
        QuartSchema(quart_app)
        install_community_ids(quart_app, dal)
        quart_app.register_blueprint(chat_bp)
        quart_app.config["dal"] = dal
        return quart_app

    @pytest.fixture
    def client(self, app: Quart) -> Any:
        return app.test_client()

    async def test_chat_history_blocked_when_flag_off(
        self, client: Any, auth_headers: Any, community_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`waddles.community.chat` OFF -> 402, never reaches the DB/proxy layer."""
        import blueprints.v1.community_chat as chat_module

        gate = AsyncMock(return_value=False)
        monkeypatch.setattr(chat_module, "feature_enabled", gate)
        dal, community_id = community_db

        response = await client.get(
            cpath(dal, "/api/v1/community/{community_id}/chat/history", community_id),
            headers=auth_headers(scope="community.chat:read"),
        )

        assert response.status_code == 402
        gate.assert_awaited_once()
        assert gate.await_args.args[0] == "waddles.community.chat"

    async def test_chat_history_reaches_handler_when_flag_on(
        self, client: Any, auth_headers: Any, community_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Positive counterpart -- flag ON reaches past the gate (200, not 402)."""
        import blueprints.v1.community_chat as chat_module

        monkeypatch.setattr(chat_module, "feature_enabled", AsyncMock(return_value=True))
        dal, community_id = community_db

        response = await client.get(
            cpath(dal, "/api/v1/community/{community_id}/chat/history", community_id),
            headers=auth_headers(scope="community.chat:read"),
        )

        assert response.status_code == 200


class TestPollsFeatureGate:
    @pytest.fixture
    def app(self, community_db: Any) -> Quart:
        dal, _ = community_db
        quart_app = Quart(__name__)
        QuartSchema(quart_app)
        install_community_ids(quart_app, dal)
        quart_app.register_blueprint(polls_bp)
        quart_app.config["dal"] = dal
        return quart_app

    @pytest.fixture
    def client(self, app: Quart) -> Any:
        return app.test_client()

    async def test_list_polls_blocked_when_flag_off(
        self, client: Any, auth_headers: Any, community_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import blueprints.v1.community_polls as polls_module

        gate = AsyncMock(return_value=False)
        monkeypatch.setattr(polls_module, "feature_enabled", gate)
        dal, community_id = community_db

        response = await client.get(
            cpath(dal, "/api/v1/admin/{community_id}/polls", community_id),
            headers=auth_headers(scope="community.polls:read"),
        )

        assert response.status_code == 402
        gate.assert_awaited_once()
        assert gate.await_args.args[0] == "waddles.community.polls"


class TestInteractionsAndForumsGateOnDistinctFlags:
    """`community_interaction.py` serves TWO capabilities -- each must gate independently."""

    @pytest.fixture
    def app(self, community_db: Any) -> Quart:
        dal, _ = community_db
        quart_app = Quart(__name__)
        QuartSchema(quart_app)
        install_community_ids(quart_app, dal)
        quart_app.register_blueprint(interaction_admin_bp)
        quart_app.register_blueprint(interaction_member_bp)
        quart_app.config["dal"] = dal
        return quart_app

    @pytest.fixture
    def client(self, app: Quart) -> Any:
        return app.test_client()

    async def test_admin_list_channels_blocked_when_interactions_flag_off(
        self, client: Any, auth_headers: Any, community_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import blueprints.v1.community_interaction as interaction_module

        gate = AsyncMock(return_value=False)
        monkeypatch.setattr(interaction_module, "feature_enabled", gate)
        dal, community_id = community_db

        response = await client.get(
            cpath(dal, "/api/v1/admin/{community_id}/interaction/channels", community_id),
            headers=auth_headers(scope="users:admin", user_id="1"),
        )

        assert response.status_code == 402
        assert gate.await_args.args[0] == "waddles.community.interactions"

    async def test_member_forum_posts_blocked_when_forums_flag_off(
        self, client: Any, auth_headers: Any, community_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import blueprints.v1.community_interaction as interaction_module

        gate = AsyncMock(return_value=False)
        monkeypatch.setattr(interaction_module, "feature_enabled", gate)
        dal, community_id = community_db

        response = await client.get(
            cpath(dal, "/api/v1/community/{community_id}/interact/forum/1/posts", community_id),
            headers=auth_headers(scope="community.interaction:read"),
        )

        assert response.status_code == 402
        assert gate.await_args.args[0] == "waddles.community.forums"

    async def test_forums_flag_off_does_not_block_interactions_route(
        self, client: Any, auth_headers: Any, community_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The two capabilities are independently gated -- one OFF doesn't take the other down."""
        import blueprints.v1.community_interaction as interaction_module

        async def _gate(flag_key: str, **_kwargs: Any) -> bool:
            return flag_key != "waddles.community.forums"

        monkeypatch.setattr(interaction_module, "feature_enabled", _gate)
        dal, community_id = community_db

        response = await client.get(
            cpath(dal, "/api/v1/admin/{community_id}/interaction/channels", community_id),
            headers=auth_headers(scope="users:admin", user_id="1"),
        )

        assert response.status_code == 200


class TestLoyaltyFeatureGateIsProfessionalTier:
    """`community.loyalty` is the one Professional-tier capability among this set."""

    @pytest.fixture
    def app(self, community_db: Any) -> Quart:
        dal, _ = community_db
        quart_app = Quart(__name__)
        QuartSchema(quart_app)
        install_community_ids(quart_app, dal)
        quart_app.register_blueprint(loyalty_bp)
        quart_app.config["dal"] = dal
        return quart_app

    @pytest.fixture
    def client(self, app: Quart) -> Any:
        return app.test_client()

    async def test_get_config_blocked_below_min_tier(
        self, client: Any, auth_headers: Any, community_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulate the real two-gate denial a free-tier tenant hits.

        The tier check itself is exercised end-to-end in
        `libs/flask_core/tests/test_entitlement.py`; here only the
        blueprint's contract with `feature_enabled` is under test.
        """
        import blueprints.v1.community_loyalty as loyalty_module

        gate = AsyncMock(return_value=False)  # free tier does not entitle community.loyalty
        monkeypatch.setattr(loyalty_module, "feature_enabled", gate)
        dal, community_id = community_db

        response = await client.get(
            cpath(dal, "/api/v1/admin/{community_id}/loyalty/config", community_id),
            headers=auth_headers(scope="community.loyalty:read"),
        )

        assert response.status_code == 402
        assert gate.await_args.args[0] == "waddles.community.loyalty"
