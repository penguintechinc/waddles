"""Browser-binding of install/connect OAuth `state` (login-CSRF / install-injection defence).

Drives the REAL authorize -> callback round trip of every install/connect flow (tenant Twitch
install, tenant Discord install, community provider connections) through the real state stores
(in-memory Valkey stand-in with true TTL + GETDEL semantics) and the real seeded DB fixtures.
Only the
outbound provider HTTP and the credential write are mocked, so each refusal case can assert the
write was NEVER reached. Every flow must refuse: missing cookie, another browser's cookie,
replayed state, expired state, and a state/cookie pair from different communities/tenants.
"""

from __future__ import annotations

import json as json_module
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse

import pytest
from quart import Quart
from quart_schema import QuartSchema

import blueprints.v1.community_connections as connections_module
import blueprints.v1.tenant_discord_install as discord_bp_module
import blueprints.v1.tenant_twitch_install as twitch_bp_module
from blueprints.v1.community_connections import connections_bp
from blueprints.v1.tenant_discord_install import tenant_discord_install_bp
from blueprints.v1.tenant_twitch_install import tenant_twitch_install_bp
from config import HubAPIConfig
from services import discord_install_service as discord_svc
from services import oauth_connection_state as conn_state
from services import tenant_discord_install_state as discord_state
from services import twitch_install_state as twitch_state
from services.install_state_binding import ERR_BINDING, ERR_STATE
from services.oauth_providers import ProviderSpec, TokenResponse

CALLBACK_BASE = "http://localhost:19999"


class FakeValkey:
    """In-memory Valkey stand-in: SET EX / GET / GETDEL with real expiry semantics."""

    def __init__(self) -> None:
        """Start empty."""
        self.store: dict[str, tuple[str, float]] = {}

    async def set(self, key: str, value: str, ex: int) -> None:
        self.store[key] = (value, time.monotonic() + ex)

    async def get(self, key: str) -> str | None:
        entry = self.store.get(key)
        if entry is None or time.monotonic() > entry[1]:
            return None
        return entry[0]

    async def getdel(self, key: str) -> str | None:
        value = await self.get(key)
        self.store.pop(key, None)
        return value

    def expire_all(self) -> None:
        """Make every stored state already expired (no sleeping)."""
        self.store = {k: (v, time.monotonic() - 1) for k, (v, _) in self.store.items()}


def _cfg() -> HubAPIConfig:
    return HubAPIConfig(
        module_name="hub-api-test",
        module_version="0.0.0-test",
        module_port=8204,
        grpc_port=50204,
        database_url="sqlite:memory",
        database_read_replica_url=None,
        db_pool_size=1,
        db_max_retries=1,
        db_retry_delay=1,
        secret_key="change-me-in-production",  # noqa: S106
        jwt_algorithm="HS256",
        default_tenant_slug="global",
        posthog_api_key=None,
        posthog_host="https://license.penguintech.io",
        license_server_url="https://license.penguintech.io",
        identity_callback_base_url="http://localhost:8204",
        frontend_origin="http://localhost:5173",
        log_level="INFO",
        connections_callback_base_url=CALLBACK_BASE,
    )


def _cookie_header(response: Any, name: str) -> str:
    for header in response.headers.getlist("Set-Cookie"):
        if header.startswith(f"{name}="):
            return str(header)
    raise AssertionError(f"no Set-Cookie for {name}")


def _cookie_value(header: str) -> str:
    return header.split(";", 1)[0].split("=", 1)[1]


@dataclass(slots=True)
class Flow:
    """One install/connect flow reduced to start / finish / write-count."""

    cookie: str
    start: Callable[[int], Any]
    finish: Callable[[str, str | None], Any]
    writes: Callable[[], int]


@pytest.fixture(autouse=True)
def valkey(monkeypatch: pytest.MonkeyPatch) -> FakeValkey:
    fake = FakeValkey()
    for module in (twitch_state, discord_state, conn_state):
        monkeypatch.setattr(module, "_redis_client", lambda: fake)
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "11" * 32)
    return fake


@pytest.fixture
def twitch_discord_app(bar_citizen_db: Any) -> Quart:
    dal, *_ = bar_citizen_db
    app = Quart(__name__)
    QuartSchema(app)
    app.register_blueprint(tenant_twitch_install_bp)
    app.register_blueprint(tenant_discord_install_bp)
    app.config["dal"] = dal
    app.config["HUB_API_CONFIG"] = _cfg()
    return app


@pytest.fixture
def connections_app(community_db: Any) -> Quart:
    dal, _ = community_db
    app = Quart(__name__)
    QuartSchema(app)
    app.register_blueprint(connections_bp)
    app.config["dal"] = dal
    app.config["async_dal"] = dal
    app.config["HUB_API_CONFIG"] = _cfg()
    return app


def _twitch_flow(app: Quart, auth: Any, monkeypatch: pytest.MonkeyPatch) -> Flow:
    client = app.test_client(use_cookies=False)
    monkeypatch.setattr(twitch_bp_module, "feature_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(
        twitch_bp_module.oauth_svc, "exchange_code", AsyncMock(return_value=MagicMock())
    )
    store = AsyncMock()
    monkeypatch.setattr(twitch_bp_module.creds_svc, "store_initial_credentials", store)

    async def start(user_id: int) -> tuple[str, str]:
        resp = await client.post(
            "/api/v1/tenant/twitch-install/authorize",
            headers={
                **auth(user_id=user_id, scope="tenant:admin"),
                "Content-Type": "application/json",
            },
            data=json_module.dumps({"client_id": "tcid", "client_secret": "tcsecret"}),
        )
        assert resp.status_code == 200
        header = _cookie_header(resp, twitch_bp_module.BINDING_COOKIE)
        for attr in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/", "Max-Age=600"):
            assert attr in header
        url = (await resp.get_json())["authorize_url"]
        return parse_qs(urlparse(url).query)["state"][0], _cookie_value(header)

    async def finish(state: str, nonce: str | None) -> Any:
        headers = {"Cookie": f"{twitch_bp_module.BINDING_COOKIE}={nonce}"} if nonce else {}
        return await client.get(
            f"/api/v1/tenant/twitch-install/callback?state={state}&code=c", headers=headers
        )

    return Flow(twitch_bp_module.BINDING_COOKIE, start, finish, lambda: store.await_count)


def _discord_flow(app: Quart, auth: Any, monkeypatch: pytest.MonkeyPatch) -> Flow:
    client = app.test_client(use_cookies=False)
    monkeypatch.setattr(discord_bp_module, "feature_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(discord_svc, "_exchange_code", AsyncMock(return_value={}))
    store = MagicMock()
    monkeypatch.setattr(discord_svc, "store_tenant_credentials", store)

    async def start(user_id: int) -> tuple[str, str]:
        resp = await client.post(
            "/api/v1/tenants/discord/install/authorize",
            headers={
                **auth(user_id=user_id, scope="tenant.discord_install:write"),
                "Content-Type": "application/json",
            },
            data=json_module.dumps({"application_id": "app-1", "client_secret": "sec-1"}),
        )
        assert resp.status_code == 200
        header = _cookie_header(resp, discord_bp_module.BINDING_COOKIE)
        for attr in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/", "Max-Age=600"):
            assert attr in header
        url = (await resp.get_json())["authorize_url"]
        return parse_qs(urlparse(url).query)["state"][0], _cookie_value(header)

    async def finish(state: str, nonce: str | None) -> Any:
        headers = {"Cookie": f"{discord_bp_module.BINDING_COOKIE}={nonce}"} if nonce else {}
        return await client.get(
            f"/api/v1/discord/install/callback?state={state}&code=c", headers=headers
        )

    return Flow(discord_bp_module.BINDING_COOKIE, start, finish, lambda: store.call_count)


def _connections_flow(
    app: Quart, auth: Any, monkeypatch: pytest.MonkeyPatch, community_id: int
) -> Flow:
    client = app.test_client(use_cookies=False)
    cookie = connections_module._binding_cookie("youtube")
    spec = ProviderSpec(
        name="youtube",
        display_name="YouTube",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",  # noqa: S106
        scopes=("scope-a",),
        extra_authorize_params={},
        uses_pkce=False,
        client_id_env="YOUTUBE_CLIENT_ID",
        client_secret_env="YOUTUBE_CLIENT_SECRET",  # noqa: S106
        token_auth="body",  # noqa: S106
    )
    prov = connections_module.providers_svc
    monkeypatch.setattr(connections_module, "feature_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(connections_module, "_tenant_ok", lambda _cid: True)
    monkeypatch.setattr(prov, "get_provider", lambda _n: spec)
    monkeypatch.setattr(
        prov, "build_authorize_url", lambda _n, **kw: f"https://x.example/a?state={kw['state']}"
    )
    monkeypatch.setattr(
        prov,
        "exchange_code",
        AsyncMock(
            return_value=TokenResponse(
                access_token="a",
                refresh_token=None,
                expires_in=60,
                scopes=[],
                token_type="Bearer",  # noqa: S106
            )
        ),
    )
    monkeypatch.setattr(prov, "fetch_account_label", AsyncMock(return_value="acct"))
    upsert = AsyncMock()
    monkeypatch.setattr(connections_module.connections_svc, "upsert_connection", upsert)

    async def start(user_id: int) -> tuple[str, str]:
        resp = await client.post(
            f"/api/v1/communities/{community_id}/connections/youtube/authorize",
            headers=auth(user_id=user_id, scope="community.connections:write"),
        )
        assert resp.status_code == 200
        header = _cookie_header(resp, cookie)
        for attr in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/", "Max-Age=600"):
            assert attr in header
        url = (await resp.get_json())["data"]["authorize_url"]
        return parse_qs(urlparse(url).query)["state"][0], _cookie_value(header)

    async def finish(state: str, nonce: str | None) -> Any:
        headers = {"Cookie": f"{cookie}={nonce}"} if nonce else {}
        return await client.get(
            f"/api/v1/connections/callback/youtube?state={state}&code=c", headers=headers
        )

    return Flow(cookie, start, finish, lambda: upsert.await_count)


@pytest.fixture(params=["twitch", "discord", "connections"])
def flow(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    user_auth_headers: Any,
) -> Flow:
    if request.param == "twitch":
        app = request.getfixturevalue("twitch_discord_app")
        return _twitch_flow(app, user_auth_headers, monkeypatch)
    if request.param == "discord":
        app = request.getfixturevalue("twitch_discord_app")
        return _discord_flow(app, user_auth_headers, monkeypatch)
    app = request.getfixturevalue("connections_app")
    return _connections_flow(app, user_auth_headers, monkeypatch, 1)


async def _refused(resp: Any, code: str) -> None:
    assert resp.status_code == 400
    assert code in (await resp.get_data(as_text=True))


class TestBinding:
    async def test_happy_path_completes_once_and_clears_cookie(self, flow: Flow) -> None:
        state, nonce = await flow.start(1)
        resp = await flow.finish(state, nonce)
        assert resp.status_code == 200
        assert flow.writes() == 1
        assert _cookie_header(resp, flow.cookie).split(";", 1)[0].endswith("=")  # cleared

    async def test_missing_cookie_is_refused(self, flow: Flow) -> None:
        state, _nonce = await flow.start(1)
        await _refused(await flow.finish(state, None), ERR_BINDING)
        assert flow.writes() == 0

    async def test_other_users_browser_is_refused_and_victim_can_still_finish(
        self, flow: Flow
    ) -> None:
        victim_state, victim_nonce = await flow.start(1)
        _attacker_state, attacker_nonce = await flow.start(2)
        # Attacker's link (victim state) opened in the ATTACKER's browser, or vice versa.
        await _refused(await flow.finish(victim_state, attacker_nonce), ERR_BINDING)
        assert flow.writes() == 0
        # The refusal did not burn the legitimate pending install.
        assert (await flow.finish(victim_state, victim_nonce)).status_code == 200
        assert flow.writes() == 1

    async def test_replayed_state_is_refused(self, flow: Flow) -> None:
        state, nonce = await flow.start(1)
        assert (await flow.finish(state, nonce)).status_code == 200
        await _refused(await flow.finish(state, nonce), ERR_STATE)
        assert flow.writes() == 1

    async def test_expired_state_is_refused(self, flow: Flow, valkey: FakeValkey) -> None:
        state, nonce = await flow.start(1)
        valkey.expire_all()
        await _refused(await flow.finish(state, nonce), ERR_STATE)
        assert flow.writes() == 0

    async def test_forged_state_is_refused(self, flow: Flow) -> None:
        _state, nonce = await flow.start(1)
        await _refused(await flow.finish("forged-state-value", nonce), ERR_STATE)
        assert flow.writes() == 0


class TestCrossContext:
    async def test_state_from_another_community_cannot_use_this_communitys_cookie(
        self,
        connections_app: Quart,
        user_auth_headers: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        flow_a = _connections_flow(connections_app, user_auth_headers, monkeypatch, 1)
        state_a, nonce_a = await flow_a.start(1)
        flow_b = _connections_flow(connections_app, user_auth_headers, monkeypatch, 2)
        state_b, nonce_b = await flow_b.start(1)

        await _refused(await flow_a.finish(state_a, nonce_b), ERR_BINDING)
        await _refused(await flow_b.finish(state_b, nonce_a), ERR_BINDING)
        assert flow_a.writes() == 0
        assert (await flow_a.finish(state_a, nonce_a)).status_code == 200

    async def test_state_is_stored_with_nonce_hash_only(
        self, flow: Flow, valkey: FakeValkey
    ) -> None:
        _state, nonce = await flow.start(1)
        assert nonce not in " ".join(v for v, _ in valkey.store.values())
