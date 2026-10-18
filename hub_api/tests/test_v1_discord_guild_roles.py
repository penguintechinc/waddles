"""`GET /api/v1/communities/<id>/discord/roles` -- authz, tenancy, DTO shape, Discord error mapping.

Discord REST is mocked at the httpx transport; the pairing/tenant lookup
runs against the real pydal `bar_citizen_db` fixture.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from quart import Quart
from quart_schema import QuartSchema

import services.discord_guild_roles as roles_module
from blueprints.v1.guild_pairing import guild_pairing_bp
from services.credential_resolver import PlatformCredentials, TransportUnavailable

_REAL_ASYNC_CLIENT = httpx.AsyncClient
GUILD = "123456789012345678"  # gitleaks:allow - fake snowflake
FAKE_TOKEN = "fake-bot-token-not-a-secret"  # gitleaks:allow - test fixture


class _FakeResolver:
    def __init__(self, payload: dict[str, Any] | None) -> None:
        self._payload = payload

    async def resolve(
        self, dal: Any, *, tenant_id: int, is_global_tenant: bool, platform: str
    ) -> PlatformCredentials:
        if self._payload is None:
            raise TransportUnavailable("none")
        return PlatformCredentials(tenant_id, platform, self._payload, "tenant")


@pytest.fixture
def app(bar_citizen_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(guild_pairing_bp)
    quart_app.config["dal"] = bar_citizen_db[0]
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


@pytest.fixture(autouse=True)
def _defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    import blueprints.v1.guild_pairing as bp

    monkeypatch.setattr(bp, "feature_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(
        roles_module, "DefaultCredentialResolver", lambda: _FakeResolver({"bot_token": FAKE_TOKEN})
    )


def _mock_discord(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def _h(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    real = _REAL_ASYNC_CLIENT
    monkeypatch.setattr(
        roles_module.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(_h), **kw),
    )
    return seen


def _pair(bar_citizen_db: Any, guild: str = GUILD) -> None:
    dal, community_id, *_ = bar_citizen_db
    dal.guild_tenant_pairings.insert(
        community_id=community_id,
        discord_guild_id=guild,
        direction="bidirectional",
        sync_enabled=False,
        role_name_prefix="",
    )
    dal.commit()


def _url(community_id: int, qs: str = "") -> str:
    return f"/api/v1/communities/{community_id}/discord/roles{qs}"


ROLES = [
    {"id": GUILD, "name": "@everyone", "color": 0, "position": 0, "managed": False},
    {"id": "222", "name": "Member", "color": 255, "position": 1, "permissions": "0"},
    {"id": "333", "name": "Officer", "color": 65280, "position": 5},
]


async def test_happy_path_dto_shape_sorted_everyone_excluded(
    client: Any, auth_headers: Any, bar_citizen_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pair(bar_citizen_db)
    seen = _mock_discord(monkeypatch, lambda r: httpx.Response(200, json=ROLES))
    resp = await client.get(
        _url(bar_citizen_db[1]), headers=auth_headers(scope="community.guild_pairing:read")
    )
    assert resp.status_code == 200
    body = await resp.get_json()
    assert body == {
        "success": True,
        "roles": [
            {"id": "333", "name": "Officer", "color": 65280, "position": 5},
            {"id": "222", "name": "Member", "color": 255, "position": 1},
        ],
    }
    assert all(set(r) == {"id", "name", "color", "position"} for r in body["roles"])
    assert seen[0].url.path.endswith(f"/guilds/{GUILD}/roles")
    assert seen[0].headers["Authorization"] == f"Bot {FAKE_TOKEN}"


@pytest.mark.parametrize(
    ("status", "payload", "http_status", "code"),
    [
        (404, {"code": 10004}, 409, "DISCORD_BOT_NOT_IN_GUILD"),
        (403, {"code": 50001}, 409, "DISCORD_BOT_NOT_IN_GUILD"),
        (403, {"code": 50013}, 502, "DISCORD_MISSING_PERMISSIONS"),
        (403, None, 502, "DISCORD_MISSING_PERMISSIONS"),
        (401, {}, 502, "DISCORD_BOT_TOKEN_INVALID"),
        (429, {}, 503, "DISCORD_RATE_LIMITED"),
        (500, {}, 502, "DISCORD_UNAVAILABLE"),
        (503, {}, 502, "DISCORD_UNAVAILABLE"),
    ],
)
async def test_discord_errors_map_to_distinct_codes(
    client: Any,
    auth_headers: Any,
    bar_citizen_db: Any,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    payload: Any,
    http_status: int,
    code: str,
) -> None:
    _pair(bar_citizen_db)

    def handler(r: httpx.Request) -> httpx.Response:
        if payload is None:
            return httpx.Response(status, content=b"not json")
        return httpx.Response(status, json=payload)

    _mock_discord(monkeypatch, handler)
    resp = await client.get(
        _url(bar_citizen_db[1]), headers=auth_headers(scope="community.guild_pairing:read")
    )
    assert resp.status_code == http_status
    assert (await resp.get_json())["error"]["code"] == code


async def test_transport_error_is_502(
    client: Any, auth_headers: Any, bar_citizen_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pair(bar_citizen_db)

    def boom(r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    _mock_discord(monkeypatch, boom)
    resp = await client.get(
        _url(bar_citizen_db[1]), headers=auth_headers(scope="community.guild_pairing:read")
    )
    assert resp.status_code == 502
    assert (await resp.get_json())["error"]["code"] == "DISCORD_UNAVAILABLE"


async def test_malformed_payload_is_502(
    client: Any, auth_headers: Any, bar_citizen_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pair(bar_citizen_db)
    _mock_discord(monkeypatch, lambda r: httpx.Response(200, json=[{"nope": 1}]))
    resp = await client.get(
        _url(bar_citizen_db[1]), headers=auth_headers(scope="community.guild_pairing:read")
    )
    assert resp.status_code == 502


async def test_no_pairing_is_404(client: Any, auth_headers: Any, bar_citizen_db: Any) -> None:
    resp = await client.get(
        _url(bar_citizen_db[1]), headers=auth_headers(scope="community.guild_pairing:read")
    )
    assert resp.status_code == 404
    assert (await resp.get_json())["error"]["code"] == "DISCORD_PAIRING_NOT_FOUND"


async def test_unpaired_guild_param_is_404_and_never_calls_discord(
    client: Any, auth_headers: Any, bar_citizen_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pair(bar_citizen_db)
    seen = _mock_discord(monkeypatch, lambda r: httpx.Response(200, json=[]))
    resp = await client.get(
        _url(bar_citizen_db[1], "?guild_id=999"),
        headers=auth_headers(scope="community.guild_pairing:read"),
    )
    assert resp.status_code == 404
    assert seen == []


async def test_multiple_guilds_require_guild_id(
    client: Any, auth_headers: Any, bar_citizen_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pair(bar_citizen_db)
    _pair(bar_citizen_db, "555")
    _mock_discord(monkeypatch, lambda r: httpx.Response(200, json=[]))
    headers = auth_headers(scope="community.guild_pairing:read")
    assert (await client.get(_url(bar_citizen_db[1]), headers=headers)).status_code == 400
    ok = await client.get(_url(bar_citizen_db[1], "?guild_id=555"), headers=headers)
    assert ok.status_code == 200
    assert (await ok.get_json()) == {"success": True, "roles": []}


@pytest.mark.parametrize("payload", [None, {"bot_token": ""}])
async def test_credentials_missing_is_503(
    client: Any,
    auth_headers: Any,
    bar_citizen_db: Any,
    monkeypatch: pytest.MonkeyPatch,
    payload: Any,
) -> None:
    _pair(bar_citizen_db)
    monkeypatch.setattr(roles_module, "DefaultCredentialResolver", lambda: _FakeResolver(payload))
    resp = await client.get(
        _url(bar_citizen_db[1]), headers=auth_headers(scope="community.guild_pairing:read")
    )
    assert resp.status_code == 503
    assert (await resp.get_json())["error"]["code"] == "DISCORD_NOT_CONFIGURED"


async def test_real_resolver_with_no_tenant_credentials_is_503(
    client: Any, auth_headers: Any, bar_citizen_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pair(bar_citizen_db)
    monkeypatch.undo()  # drop the fake resolver -> real DefaultCredentialResolver, empty DB
    import blueprints.v1.guild_pairing as bp

    monkeypatch.setattr(bp, "feature_enabled", AsyncMock(return_value=True))
    resp = await client.get(
        _url(bar_citizen_db[1]), headers=auth_headers(scope="community.guild_pairing:read")
    )
    assert resp.status_code == 503


@pytest.mark.parametrize("scope", ["", "community.guild_pairing:write", "other:read"])
async def test_wrong_scope_is_403(
    client: Any, auth_headers: Any, bar_citizen_db: Any, scope: str
) -> None:
    _pair(bar_citizen_db)
    resp = await client.get(_url(bar_citizen_db[1]), headers=auth_headers(scope=scope))
    assert resp.status_code == 403


async def test_no_token_is_rejected(client: Any, bar_citizen_db: Any) -> None:
    resp = await client.get(_url(bar_citizen_db[1]))
    assert resp.status_code in (401, 403)


async def test_cross_tenant_community_is_404(
    client: Any, auth_headers: Any, bar_citizen_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dal = bar_citizen_db[0]
    other_tenant = dal.tenants.insert(slug="other-corp", is_active=True, is_global=False)
    other_community = dal.communities.insert(name="other", tenant_id=other_tenant)
    dal.guild_tenant_pairings.insert(
        community_id=other_community,
        discord_guild_id=GUILD,
        direction="bidirectional",
        sync_enabled=False,
        role_name_prefix="",
    )
    dal.commit()
    seen = _mock_discord(monkeypatch, lambda r: httpx.Response(200, json=ROLES))
    resp = await client.get(
        _url(other_community), headers=auth_headers(scope="community.guild_pairing:read")
    )
    assert resp.status_code == 404
    assert seen == []


async def test_feature_flag_off_is_402(
    client: Any, auth_headers: Any, bar_citizen_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import blueprints.v1.guild_pairing as bp

    monkeypatch.setattr(bp, "feature_enabled", AsyncMock(return_value=False))
    resp = await client.get(
        _url(bar_citizen_db[1]), headers=auth_headers(scope="community.guild_pairing:read")
    )
    assert resp.status_code == 402


async def test_default_client_is_used_and_closed(
    bar_citizen_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Service-level: no injected client -> builds + closes its own; injected client untouched."""
    _pair(bar_citizen_db)
    _mock_discord(monkeypatch, lambda r: httpx.Response(200, json=ROLES))
    roles = await roles_module.list_guild_roles(bar_citizen_db[0], bar_citizen_db[1])
    assert [r.id for r in roles] == ["333", "222"]
    async with _REAL_ASYNC_CLIENT(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=ROLES))
    ) as injected:
        roles = await roles_module.list_guild_roles(
            bar_citizen_db[0], bar_citizen_db[1], http_client=injected
        )
        assert len(roles) == 2
        assert not injected.is_closed


@pytest.mark.parametrize("same_tenant", [True, False], ids=["other-community", "other-tenant"])
async def test_guild_paired_to_other_community_is_404_and_never_calls_discord(
    client: Any,
    auth_headers: Any,
    bar_citizen_db: Any,
    monkeypatch: pytest.MonkeyPatch,
    same_tenant: bool,
) -> None:
    """Regression: a guild_id paired to a DIFFERENT community/tenant is not reachable via ours."""
    dal, community_id, *_ = bar_citizen_db
    own_tenant = dal.communities[community_id].tenant_id
    tenant = (
        own_tenant
        if same_tenant
        else dal.tenants.insert(slug="victim-corp", is_active=True, is_global=False)
    )
    victim = dal.communities.insert(name="victim", tenant_id=tenant)
    victim_guild = "888888888888888888"  # gitleaks:allow - fake snowflake
    dal.guild_tenant_pairings.insert(
        community_id=victim,
        discord_guild_id=victim_guild,
        direction="bidirectional",
        sync_enabled=False,
        role_name_prefix="",
    )
    dal.commit()
    _pair(bar_citizen_db)  # caller's own legit pairing exists too
    seen = _mock_discord(monkeypatch, lambda r: httpx.Response(200, json=ROLES))
    resp = await client.get(
        _url(community_id, f"?guild_id={victim_guild}"),
        headers=auth_headers(scope="community.guild_pairing:read"),
    )
    assert resp.status_code == 404
    assert (await resp.get_json())["error"]["code"] == "DISCORD_PAIRING_NOT_FOUND"
    assert seen == []
