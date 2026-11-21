"""Bot `<puuid:>` path ids (shoutout creator, rcon server, ai source/suggestion).

Per entity: the owner's UUID resolves, a foreign-community UUID is the identical 404 an unknown
UUID gets, and a numeric id in the path 404s (the converter rejects it). Responses emit UUIDs.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

os.environ.setdefault("RCON_ENCRYPTION_KEY", "ab" * 32)

from services import bot_rcon  # noqa: E402
from tests.entity_ids import pid  # noqa: E402
from tests.test_bot_blueprint import (  # noqa: E402,F401 - fixtures
    app,
    client,
    community_id,
    community_uuid,
    dal,
)

UNKNOWN = "00000000-0000-4000-8000-0000000000ff"


@pytest.fixture(autouse=True)
def _guards(bypass_route_guards: Any, monkeypatch: Any) -> None:
    async def fake_proxy(path: str, method: str = "GET", body: Any = None) -> Any:
        return {"ok": True}

    monkeypatch.setattr(bot_rcon, "_proxy", fake_proxy)


def _foreign_community(dal: Any, community_id: int) -> int:
    """A second community in the SAME tenant (the scope boundary under test)."""
    tenant_id = dal(dal.communities.id == community_id).select().first().tenant_id
    other: int = dal.communities.insert(tenant_id=tenant_id)
    dal.commit()
    return other


async def test_community_scoped_ids_foreign_and_unknown_are_the_same_404(
    client: Any, auth_headers: Any, dal: Any, community_id: int, community_uuid: str
) -> None:
    other = _foreign_community(dal, community_id)
    creator = dal.shoutout_creators.insert(
        community_id=other, platform="twitch", platform_username="x", added_by=0
    )
    server = dal.server_status_configs.insert(
        community_id=other, display_name="F", game_name="f", host="1.2.3.4"
    )
    dal.commit()
    base = f"/api/v1/admin/{community_uuid}"
    creator_uuid = pid(dal, "shoutout_creators", creator)
    server_uuid = pid(dal, "server_status_configs", server)
    cases = [
        ("delete", "/shoutout/creators/{}", creator_uuid, "bot.shoutout:write"),
        ("get", "/rcon/servers/{}/policy", server_uuid, "bot.server_manager:admin"),
        ("delete", "/rcon/servers/{}", server_uuid, "bot.server_manager:admin"),
    ]
    for method, template, foreign_uuid, scope in cases:
        headers = auth_headers(scope=scope)
        foreign = await getattr(client, method)(
            base + template.format(foreign_uuid), headers=headers
        )
        unknown = await getattr(client, method)(base + template.format(UNKNOWN), headers=headers)
        assert foreign.status_code == unknown.status_code == 404, template
        assert await foreign.get_json() == await unknown.get_json(), template
    # neither foreign row was touched
    assert dal(dal.shoutout_creators.id == creator).count() == 1
    assert dal(dal.server_status_configs.id == server).select().first().deleted_at is None


async def test_ai_knowledge_foreign_and_unknown_uuids_are_the_same_404(
    client: Any, auth_headers: Any, dal: Any, community_id: int
) -> None:
    """These ids carry no URL community: the (unchanged) per-row authz decides, still a 404."""
    tenant = dal.tenants.insert(slug="other-t", is_active=True)
    foreign_community = dal.communities.insert(tenant_id=tenant)
    source = dal.ai_knowledge_sources.insert(
        source_name="V", source_type="manual", is_active=True, community_id=foreign_community
    )
    suggestion = dal.ai_ticket_suggestions.insert(
        ticket_id=1,
        community_id=foreign_community,
        suggestion_text="x",
        confidence_score=0.9,
        cited_chunks=[],
    )
    dal.commit()
    headers = auth_headers(scope="bot.ai_knowledge:write")
    src = "/api/v1/admin/ai-knowledge/sources/{}"
    sug = "/api/v1/admin/ai-knowledge/suggestions/{}/feedback"
    for method, template, real in (
        ("delete", src, pid(dal, "ai_knowledge_sources", source)),
        ("post", sug, pid(dal, "ai_ticket_suggestions", suggestion)),
    ):
        kwargs = {"json": {"feedback": "helpful"}} if method == "post" else {}
        foreign = await getattr(client, method)(template.format(real), headers=headers, **kwargs)
        unknown = await getattr(client, method)(template.format(UNKNOWN), headers=headers, **kwargs)
        assert foreign.status_code == unknown.status_code == 404, template
        assert await foreign.get_json() == await unknown.get_json(), template
    assert dal(dal.ai_knowledge_sources.id == source).count() == 1


async def test_numeric_ids_in_the_path_do_not_route(
    client: Any, auth_headers: Any, community_uuid: str
) -> None:
    base = f"/api/v1/admin/{community_uuid}"
    for method, url, scope in (
        ("delete", f"{base}/shoutout/creators/1", "bot.shoutout:write"),
        ("put", f"{base}/rcon/servers/1", "bot.server_manager:admin"),
        ("get", f"{base}/rcon/info/1/status", "bot.server_manager:read"),
        ("put", "/api/v1/admin/ai-knowledge/sources/1", "bot.ai_knowledge:write"),
        ("post", "/api/v1/admin/ai-knowledge/sources/1/reindex", "bot.ai_knowledge:write"),
        ("post", "/api/v1/admin/ai-knowledge/suggestions/1/feedback", "bot.ai_knowledge:write"),
    ):
        response = await getattr(client, method)(url, headers=auth_headers(scope=scope), json={})
        assert response.status_code == 404, url


async def test_own_uuids_resolve_and_responses_emit_them(
    client: Any, auth_headers: Any, dal: Any, community_id: int, community_uuid: str
) -> None:
    base = f"/api/v1/admin/{community_uuid}"
    admin = auth_headers(scope="bot.server_manager:admin")
    created = await client.post(
        f"{base}/rcon/servers",
        headers=admin,
        json={"display_name": "Mine", "host": "8.8.8.8", "game_name": "g"},
    )
    assert created.status_code == 201
    server_uuid = (await created.get_json())["server"]["id"]
    row = dal(dal.server_status_configs.public_id == server_uuid).select().first()
    assert row is not None and row.community_id == community_id  # a UUID, not the numeric pk
    listed = await client.get(f"{base}/rcon/servers", headers=admin)
    assert [s["id"] for s in (await listed.get_json())["servers"]] == [server_uuid]
    policy = await client.get(f"{base}/rcon/servers/{server_uuid}/policy", headers=admin)
    assert policy.status_code == 200
    # the command-log `server_id` filter takes the UUID too (an unknown one is a 404)
    log = await client.get(f"{base}/rcon/log?server_id={server_uuid}", headers=admin)
    assert log.status_code == 200
    missing_log = await client.get(f"{base}/rcon/log?server_id={UNKNOWN}", headers=admin)
    assert missing_log.status_code == 404
    source = dal.ai_knowledge_sources.insert(
        source_name="S", source_type="manual", is_active=True, community_id=community_id
    )
    dal.commit()
    deleted = await client.delete(
        f"/api/v1/admin/ai-knowledge/sources/{pid(dal, 'ai_knowledge_sources', source)}",
        headers=auth_headers(scope="bot.ai_knowledge:write"),
    )
    assert deleted.status_code == 200
