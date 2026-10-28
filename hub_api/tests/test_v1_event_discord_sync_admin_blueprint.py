"""Admin (tenant-JWT) event-sync toggle: scope, flag gate, tenant isolation, persistence."""

from __future__ import annotations

import json as json_module
from typing import Any
from unittest.mock import AsyncMock

import pytest
from quart import Quart
from quart_schema import QuartSchema

import blueprints.v1.event_discord_sync as module
from blueprints.v1.event_discord_sync import event_discord_sync_admin_bp
from services.discord_install_service import DEFAULT_BOT_PERMISSIONS, MANAGE_EVENTS_PERMISSION
from services.guild_pairing import create_pairing, list_pairings


@pytest.fixture
def app(event_sync_db: Any) -> Quart:
    dal, *_ = event_sync_db
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(event_discord_sync_admin_bp)
    quart_app.config["dal"] = dal
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "feature_enabled", AsyncMock(return_value=True))


def _url(community_id: int) -> str:
    return f"/api/v1/communities/{community_id}/calendar/event-sync"


def _put(client: Any, headers: dict[str, str], community_id: int, body: dict[str, Any]) -> Any:
    return client.put(
        _url(community_id),
        headers={**headers, "Content-Type": "application/json"},
        data=json_module.dumps(body),
    )


def _pair(dal: Any, community_id: int) -> None:
    create_pairing(
        dal,
        community_id,
        discord_guild_id="123456789012345678",  # gitleaks:allow - fake snowflake
        direction="bidirectional",
        role_name_prefix="[BC]",
        actor_user_id=None,
    )


def test_default_bot_permissions_include_manage_events() -> None:
    assert MANAGE_EVENTS_PERMISSION == 8589934592
    assert DEFAULT_BOT_PERMISSIONS & MANAGE_EVENTS_PERMISSION
    assert DEFAULT_BOT_PERMISSIONS == 1_108_906_961_990


async def test_put_sets_event_sync_enabled(
    client: Any, auth_headers: Any, event_sync_db: Any
) -> None:
    dal, community_id, *_ = event_sync_db
    _pair(dal, community_id)
    resp = await _put(
        client, auth_headers(scope="community.guild_pairing:write"), community_id, {"enabled": True}
    )
    assert resp.status_code == 200
    body = await resp.get_json()
    assert body["event_sync_enabled"] is True
    assert body["pairings_enabled"] == 1
    assert all(p.event_sync_enabled for p in list_pairings(dal, community_id))

    resp = await _put(
        client,
        auth_headers(scope="community.guild_pairing:write"),
        community_id,
        {"enabled": False},
    )
    assert (await resp.get_json())["event_sync_enabled"] is False
    assert not any(p.event_sync_enabled for p in list_pairings(dal, community_id))


async def test_get_reflects_state(client: Any, auth_headers: Any, event_sync_db: Any) -> None:
    dal, community_id, *_ = event_sync_db
    _pair(dal, community_id)
    resp = await client.get(
        _url(community_id), headers=auth_headers(scope="community.guild_pairing:read")
    )
    assert resp.status_code == 200
    body = await resp.get_json()
    assert (body["event_sync_enabled"], body["pairings_total"]) == (False, 1)


async def test_wrong_scope_is_403(client: Any, auth_headers: Any, event_sync_db: Any) -> None:
    _, community_id, *_ = event_sync_db
    resp = await _put(
        client, auth_headers(scope="community.guild_pairing:read"), community_id, {"enabled": True}
    )
    assert resp.status_code == 403


async def test_no_token_rejected(client: Any, event_sync_db: Any) -> None:
    _, community_id, *_ = event_sync_db
    resp = await _put(client, {}, community_id, {"enabled": True})
    assert resp.status_code in (401, 403)


async def test_flag_off_is_402(
    client: Any, auth_headers: Any, event_sync_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "feature_enabled", AsyncMock(return_value=False))
    dal, community_id, *_ = event_sync_db
    _pair(dal, community_id)
    resp = await _put(
        client, auth_headers(scope="community.guild_pairing:write"), community_id, {"enabled": True}
    )
    assert resp.status_code == 402
    assert not any(p.event_sync_enabled for p in list_pairings(dal, community_id))


async def test_other_tenant_is_404(client: Any, auth_headers: Any, event_sync_db: Any) -> None:
    dal, community_id, *_ = event_sync_db
    _pair(dal, community_id)
    resp = await _put(
        client,
        auth_headers(scope="community.guild_pairing:write", tenant="someone-else"),
        community_id,
        {"enabled": True},
    )
    assert resp.status_code in (403, 404)
    assert not any(p.event_sync_enabled for p in list_pairings(dal, community_id))


@pytest.mark.parametrize("body", [{}, {"enabled": "yes"}, {"enabled": 1}])
async def test_bad_body_is_400(
    client: Any, auth_headers: Any, event_sync_db: Any, body: dict[str, Any]
) -> None:
    dal, community_id, *_ = event_sync_db
    _pair(dal, community_id)
    resp = await _put(
        client, auth_headers(scope="community.guild_pairing:write"), community_id, body
    )
    assert resp.status_code == 400


async def test_no_pairings_is_404(client: Any, auth_headers: Any, event_sync_db: Any) -> None:
    _, community_id, *_ = event_sync_db
    resp = await _put(
        client, auth_headers(scope="community.guild_pairing:write"), community_id, {"enabled": True}
    )
    assert resp.status_code == 404
