# ruff: noqa: F811
"""Sibling-path authz for suggestion feedback + RCON server-id routes (real pydal schema).

Cross-community ids must answer 404 (never 403, never a write) -- no cross-tenant oracle.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

os.environ.setdefault("RCON_ENCRYPTION_KEY", "ab" * 32)

from blueprints.v1 import bot as bot_blueprint  # noqa: E402
from services import bot_ai_knowledge, bot_rcon  # noqa: E402
from tests.test_bot_blueprint import (  # noqa: E402,F401 - fixtures
    app,
    client,
    community_id,
    dal,
    other_tenant_community_id,
)

WRITE = "bot.ai_knowledge:write"
ADMIN = "bot.server_manager:admin"
READ = "bot.server_manager:read"


def _sug(dal: Any, owner: int | None) -> int:
    sid: int = dal.ai_ticket_suggestions.insert(
        ticket_id=1, community_id=owner, suggestion_text="x", confidence_score=0.9, cited_chunks=[]
    )
    dal.commit()
    return sid


def _fb(dal: Any, sid: int) -> Any:
    dal.commit()
    return dal.ai_ticket_suggestions[sid].feedback


class TestSuggestionFeedbackAuthz:
    async def _post(self, client: Any, auth_headers: Any, sid: int) -> Any:
        return await client.post(
            f"/api/v1/admin/ai-knowledge/suggestions/{sid}/feedback",
            headers=auth_headers(scope=WRITE),
            json={"feedback": "helpful"},
        )

    async def test_foreign_community_404_and_unchanged(
        self, client: Any, auth_headers: Any, dal: Any, other_tenant_community_id: int
    ) -> None:
        sid = _sug(dal, other_tenant_community_id)
        assert (await self._post(client, auth_headers, sid)).status_code == 404
        assert _fb(dal, sid) is None

    async def test_foreign_and_missing_indistinguishable(
        self, client: Any, auth_headers: Any, dal: Any, other_tenant_community_id: int
    ) -> None:
        foreign = await self._post(client, auth_headers, _sug(dal, other_tenant_community_id))
        missing = await self._post(client, auth_headers, 999999)
        assert await foreign.get_json() == await missing.get_json()

    async def test_unowned_row_non_superadmin_404(
        self, client: Any, auth_headers: Any, dal: Any
    ) -> None:
        sid = _sug(dal, None)
        assert (await self._post(client, auth_headers, sid)).status_code == 404
        assert _fb(dal, sid) is None

    async def test_unowned_row_superadmin_ok(
        self, client: Any, auth_headers: Any, dal: Any, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr(bot_blueprint, "_is_super_admin_sync", lambda _d, _u: True)
        sid = _sug(dal, None)
        assert (await self._post(client, auth_headers, sid)).status_code == 200
        assert _fb(dal, sid) == "helpful"

    async def test_own_community_ok(
        self, client: Any, auth_headers: Any, dal: Any, community_id: int
    ) -> None:
        sid = _sug(dal, community_id)
        assert (await self._post(client, auth_headers, sid)).status_code == 200
        assert _fb(dal, sid) == "helpful"

    def test_service_write_is_filtered_by_community(
        self, dal: Any, community_id: int, other_tenant_community_id: int
    ) -> None:
        """ORM-layer filter: wrong owner can never update, even if the route check is bypassed."""
        sid = _sug(dal, other_tenant_community_id)
        with pytest.raises(bot_ai_knowledge.KnowledgeServiceError) as exc:
            bot_ai_knowledge.record_feedback(dal, sid, "helpful", community_id=community_id)
        assert exc.value.status == 404
        assert _fb(dal, sid) is None

    async def test_suggest_persists_owning_community(
        self, dal: Any, community_id: int, monkeypatch: Any
    ) -> None:
        async def fake_search(*_a: Any, **_k: Any) -> list[Any]:
            return [
                bot_ai_knowledge.KnowledgeSearchResult(
                    chunk_id=1, source_id=1, content="c", source_url=None, source_title=None,
                    chunk_index=0, token_count=1, score=0.99,
                )
            ]  # fmt: skip

        async def fake_completion(*_a: Any, **_k: Any) -> str:
            return "answer"

        monkeypatch.setattr(bot_ai_knowledge, "search_knowledge", fake_search)
        monkeypatch.setattr(bot_ai_knowledge, "_generate_completion", fake_completion)
        made = await bot_ai_knowledge.generate_suggestion(dal, 7, "t", community_id=community_id)
        assert made is not None
        assert dal.ai_ticket_suggestions[made.id].community_id == community_id


_SERVER_ROUTES = [
    ("post", "rcon/servers/{s}/command", ADMIN, {"command": "status"}),
    ("post", "rcon/servers/{s}/kick", ADMIN, {"player": "p"}),
    ("post", "rcon/servers/{s}/ban", ADMIN, {"player": "p", "duration": 0}),
    ("get", "rcon/servers/{s}/channels", ADMIN, None),
    ("post", "rcon/servers/{s}/move", ADMIN, {"user_id": "1", "channel_id": 1}),
    ("post", "rcon/servers/{s}/message", ADMIN, {"text": "hi"}),
    ("get", "rcon/servers/{s}/policy", ADMIN, None),
    ("put", "rcon/servers/{s}/policy", ADMIN, {"a": 1}),
    ("post", "rcon/servers/{s}/enforce", ADMIN, None),
    ("get", "rcon/servers/{s}/access-log", ADMIN, None),
    ("get", "rcon/info/{s}/status", READ, None),
    ("get", "rcon/info/{s}/players", READ, None),
    ("post", "rcon/servers/{s}/test", ADMIN, {"password": "x"}),
    ("put", "rcon/servers/{s}", ADMIN, {"display_name": "pwn"}),
    ("delete", "rcon/servers/{s}", ADMIN, None),
]


class TestRconServerIdScope:
    @pytest.mark.parametrize(("method", "path", "scope", "body"), _SERVER_ROUTES)
    async def test_foreign_server_id_404_and_never_proxied(
        self,
        client: Any,
        auth_headers: Any,
        dal: Any,
        community_id: int,
        other_tenant_community_id: int,
        monkeypatch: Any,
        method: str,
        path: str,
        scope: str,
        body: Any,
    ) -> None:
        calls: list[str] = []

        async def fake_proxy(p: str, *_a: Any, **_k: Any) -> Any:
            calls.append(p)
            return {"ok": True}

        monkeypatch.setattr(bot_rcon, "_proxy", fake_proxy)
        foreign = dal.server_status_configs.insert(
            community_id=other_tenant_community_id, display_name="V", game_name="v", host="1.2.3.4"
        )
        dal.commit()
        kwargs: dict[str, Any] = {"headers": auth_headers(scope=scope)}
        if body is not None:
            kwargs["json"] = body
        r = await getattr(client, method)(
            f"/api/v1/admin/{community_id}/{path.format(s=foreign)}", **kwargs
        )
        assert r.status_code == 404
        assert calls == []
