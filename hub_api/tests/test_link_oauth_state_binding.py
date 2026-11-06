"""Browser binding of account/identity-link OAuth `state` (hub_oauth_states, DB-backed).

Drives the real start -> callback round trip of both link flows
(`/api/v1/user/identities/link/<p>`, `/api/v1/auth/oauth/<p>/link`) and the social-login flow
(`/api/v1/auth/oauth/<p>`) on the seeded auth DB. Only
the provider token exchange is mocked, so each refusal asserts no identity row was ever written.
Matrix: happy, missing cookie, other user's browser (victim can still finish), replay, expired,
forged, state/cookie pair from different flows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest
from quart import Quart
from quart_schema import QuartSchema

import blueprints.v1.auth as auth_module
import blueprints.v1.identity as identity_module
from blueprints.v1.auth import auth_bp
from blueprints.v1.identity import identity_bp
from config import HubAPIConfig
from services import oauth_service
from services.install_state_binding import ERR_BINDING, ERR_STATE
from tests.conftest import TENANT_SLUG, make_user_token

FLOWS = {
    "login": (
        "/api/v1/auth/oauth/twitch",
        "get",
        "/api/v1/auth/oauth/twitch/callback",
        auth_module.LOGIN_BINDING_COOKIE,
    ),
    "identity": (
        "/api/v1/user/identities/link/twitch",
        "post",
        "/api/v1/user/identities/link/twitch/callback",
        identity_module.IDENTITY_BINDING_COOKIE,
    ),
    "auth": (
        "/api/v1/auth/oauth/twitch/link",
        "get",
        "/api/v1/auth/oauth/twitch/link-callback",
        auth_module.LINK_BINDING_COOKIE,
    ),
}


def _cfg() -> HubAPIConfig:
    return HubAPIConfig(
        module_name="t",
        module_version="0",
        module_port=1,
        grpc_port=1,
        database_url="x",
        database_read_replica_url=None,
        db_pool_size=1,
        db_max_retries=1,
        db_retry_delay=1,
        secret_key="change-me-in-production",  # noqa: S106
        jwt_algorithm="HS256",
        default_tenant_slug="global",
        posthog_api_key=None,
        posthog_host="x",
        license_server_url="x",
        identity_callback_base_url="http://localhost",
        frontend_origin="http://localhost",
        log_level="INFO",
    )


@pytest.fixture
def app(auth_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(identity_bp)
    quart_app.register_blueprint(auth_bp)
    quart_app.config["dal"] = auth_db.dal
    quart_app.config["async_dal"] = auth_db
    quart_app.config["HUB_API_CONFIG"] = _cfg()
    return quart_app


@pytest.fixture(autouse=True)
def sessions(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Mock the login session mint (counted: a refused login must never mint one)."""
    minted = AsyncMock(return_value="jwt")
    monkeypatch.setattr(oauth_service, "create_session_token", minted)
    monkeypatch.setattr(
        oauth_service, "_find_or_create_user_from_oauth", AsyncMock(return_value=object())
    )
    monkeypatch.setattr(
        oauth_service, "create_oauth_exchange_code", AsyncMock(return_value=("xc", "n"))
    )
    return minted


@pytest.fixture(autouse=True)
def _provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        oauth_service,
        "_get_platform_credentials",
        AsyncMock(return_value={"client_id": "cid", "client_secret": "sec"}),  # noqa: S106
    )
    monkeypatch.setattr(
        oauth_service, "_exchange_code", AsyncMock(return_value={"id": "p1", "username": "tw"})
    )


def _user(auth_db: Any, name: str) -> int:
    uid: int = auth_db.dal.hub_users.insert(
        email=f"{name}@example.com", username=name, is_active=True, created_at=datetime.now(UTC)
    )
    auth_db.dal.commit()
    return uid


def _hdr(uid: int) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_user_token(user_id=uid, tenant=TENANT_SLUG)}"}


class Harness:
    """Start/finish helper for one flow, plus an identity-row counter."""

    def __init__(self, app: Quart, auth_db: Any, flow: str, sessions: AsyncMock) -> None:
        """Bind the flow's routes; cookies are passed by hand (no jar)."""
        self.flow = flow
        self.sessions = sessions
        self.client = app.test_client(use_cookies=False)
        self.auth_db = auth_db
        self.start_path, self.method, self.cb_path, self.cookie = FLOWS[flow]

    async def start(self, uid: int) -> tuple[str, str]:
        resp = await getattr(self.client, self.method)(self.start_path, headers=_hdr(uid))
        assert resp.status_code == 200
        header = next(
            h for h in resp.headers.getlist("Set-Cookie") if h.startswith(f"{self.cookie}=")
        )
        for attr in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/", "Max-Age=600"):
            assert attr in header
        body = await resp.get_json()
        url = body["authorizeUrl"]
        return parse_qs(urlparse(url).query)["state"][0], header.split(";", 1)[0].split("=", 1)[1]

    async def finish(self, state: str, nonce: str | None) -> Any:
        headers = {"Cookie": f"{self.cookie}={nonce}"} if nonce else {}
        return await self.client.get(f"{self.cb_path}?state={state}&code=c", headers=headers)

    def linked(self) -> int:
        if self.flow == "login":
            return int(self.sessions.await_count)
        return int(
            self.auth_db.dal(self.auth_db.dal.hub_user_identities.platform == "twitch").count()
        )


@pytest.fixture(params=sorted(FLOWS))
def h(request: pytest.FixtureRequest, app: Quart, auth_db: Any, sessions: AsyncMock) -> Harness:
    return Harness(app, auth_db, request.param, sessions)


def _loc(resp: Any) -> str:
    assert resp.status_code == 302
    return str(resp.headers["Location"])


class TestLinkBinding:
    async def test_happy_path_links_once_and_clears_cookie(self, h: Harness, auth_db: Any) -> None:
        state, nonce = await h.start(_user(auth_db, "alice"))
        resp = await h.finish(state, nonce)
        assert "error" not in _loc(resp)
        assert h.linked() == 1
        assert any(
            x.startswith(f"{h.cookie}=;") or x.startswith(f'{h.cookie}="";')
            for x in resp.headers.getlist("Set-Cookie")
        )

    async def test_missing_cookie_is_refused(self, h: Harness, auth_db: Any) -> None:
        state, _ = await h.start(_user(auth_db, "alice"))
        assert ERR_BINDING in _loc(await h.finish(state, None))
        assert h.linked() == 0

    async def test_other_users_browser_refused_and_victim_still_finishes(
        self, h: Harness, auth_db: Any
    ) -> None:
        victim_state, victim_nonce = await h.start(_user(auth_db, "victim"))
        _s, attacker_nonce = await h.start(_user(auth_db, "attacker"))
        assert ERR_BINDING in _loc(await h.finish(victim_state, attacker_nonce))
        assert h.linked() == 0
        assert "error" not in _loc(await h.finish(victim_state, victim_nonce))
        assert h.linked() == 1

    async def test_replayed_state_is_refused(self, h: Harness, auth_db: Any) -> None:
        state, nonce = await h.start(_user(auth_db, "alice"))
        await h.finish(state, nonce)
        assert ERR_STATE in _loc(await h.finish(state, nonce))
        assert h.linked() == 1

    async def test_expired_state_is_refused(self, h: Harness, auth_db: Any) -> None:
        state, nonce = await h.start(_user(auth_db, "alice"))
        auth_db.dal(auth_db.dal.hub_oauth_states.state == state).update(
            expires_at=datetime.now(UTC) - timedelta(minutes=1)
        )
        auth_db.dal.commit()
        assert ERR_STATE in _loc(await h.finish(state, nonce))
        assert h.linked() == 0

    async def test_unbound_or_forged_state_is_refused(self, h: Harness, auth_db: Any) -> None:
        _state, nonce = await h.start(_user(auth_db, "alice"))
        assert ERR_STATE in _loc(await h.finish("plain-unbound-state", nonce))
        assert ERR_BINDING in _loc(await h.finish("forged.state-hash", nonce))
        assert h.linked() == 0


class TestRefusalsAreLogged:
    async def test_login_start_failure_is_a_typed_refusal_and_logged(
        self, app: Quart, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = app.test_client(use_cookies=False)
        with caplog.at_level("WARNING", logger=auth_module.logger.name):
            resp = await client.get("/api/v1/auth/oauth/not-a-platform")
        assert resp.status_code == 400
        assert any("auth.oauth_start.refused" in r.getMessage() for r in caplog.records)
        assert not any(
            h.startswith(f"{auth_module.LOGIN_BINDING_COOKIE}=")
            for h in resp.headers.getlist("Set-Cookie")
        )

    async def test_callback_refusal_is_logged_with_its_typed_code(
        self, h: Harness, auth_db: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        if h.flow == "login":
            tag = "auth.oauth_callback.refused"
        elif h.flow == "auth":
            tag = "auth.oauth_link_callback.refused"
        else:
            tag = "identity.link_callback.refused"
        state, _ = await h.start(_user(auth_db, "alice"))
        with caplog.at_level("WARNING"):
            await h.finish(state, None)
        assert any(tag in r.getMessage() and ERR_BINDING in r.getMessage() for r in caplog.records)
