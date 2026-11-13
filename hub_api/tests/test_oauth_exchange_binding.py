"""Browser binding of the OAuth/SSO exchange code (`POST /api/v1/auth/exchange`).

The login callback mints a single-use code carried in the redirect URL; without a browser binding a
code leaked/injected into ANOTHER browser would log that browser into the attacker's account (login
CSRF / session fixation). The code embeds SHA-256(nonce); the nonce rides in a
`__Host-` HttpOnly cookie set on the callback redirect. Real callback -> exchange round trip on
the seeded auth DB; only the provider login (`oauth_service.oauth_callback`) is stubbed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.auth import auth_bp
from config import HubAPIConfig
from services import oauth_service
from services.install_state_binding import ERR_BINDING, ERR_STATE, bound_state

COOKIE = oauth_service.EXCHANGE_BINDING_COOKIE


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
        default_tenant_slug="default",
        posthog_api_key=None,
        posthog_host="x",
        license_server_url="x",
        identity_callback_base_url="http://localhost",
        frontend_origin="http://localhost",
        log_level="INFO",
    )


@pytest.fixture
def client(auth_db: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    app = Quart(__name__)
    QuartSchema(app)
    app.register_blueprint(auth_bp)
    app.config["dal"] = auth_db.dal
    app.config["async_dal"] = auth_db
    app.config["HUB_API_CONFIG"] = _cfg()

    async def fake_login(*_a: Any, **_k: Any) -> str:
        return "victim-session-jwt"

    monkeypatch.setattr(oauth_service, "oauth_callback", fake_login)
    return app.test_client(use_cookies=False)


async def _login(client: Any) -> tuple[str, str]:
    """Run the callback; return (code from the redirect URL, nonce from the Set-Cookie)."""
    resp = await client.get("/api/v1/auth/oauth/discord/callback?code=c&state=s")
    assert resp.status_code == 302
    header = next(h for h in resp.headers.getlist("Set-Cookie") if h.startswith(f"{COOKIE}="))
    for attr in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/", "Max-Age=60"):
        assert attr in header
    code = resp.headers["Location"].split("code=")[1]
    return code, header.split(";", 1)[0].split("=", 1)[1]


async def _exchange(client: Any, code: str, nonce: str | None) -> Any:
    headers = {"Cookie": f"{COOKIE}={nonce}"} if nonce else {}
    return await client.post("/api/v1/auth/exchange", json={"code": code}, headers=headers)


async def _refused(resp: Any, code: str) -> None:
    assert resp.status_code == 400
    text = await resp.get_data(as_text=True)
    assert code in text
    assert "victim-session-jwt" not in text
    assert not any(h.startswith("wb_session=") for h in resp.headers.getlist("Set-Cookie"))


class TestExchangeBinding:
    async def test_happy_path_returns_token_and_clears_binding_cookie(self, client: Any) -> None:
        code, nonce = await _login(client)
        resp = await _exchange(client, code, nonce)
        assert resp.status_code == 200
        assert (await resp.get_json())["token"] == "victim-session-jwt"
        cleared = [h for h in resp.headers.getlist("Set-Cookie") if h.startswith(f"{COOKIE}=")]
        assert cleared
        assert any(h.startswith("wb_session=") for h in resp.headers.getlist("Set-Cookie"))

    async def test_code_without_the_cookie_is_refused(self, client: Any) -> None:
        code, _ = await _login(client)
        await _refused(await _exchange(client, code, None), ERR_BINDING)

    async def test_code_with_another_browsers_cookie_is_refused_and_not_burned(
        self, client: Any
    ) -> None:
        victim_code, victim_nonce = await _login(client)
        _other_code, attacker_nonce = await _login(client)
        await _refused(await _exchange(client, victim_code, attacker_nonce), ERR_BINDING)
        assert (await _exchange(client, victim_code, victim_nonce)).status_code == 200

    async def test_replayed_code_is_refused(self, client: Any) -> None:
        code, nonce = await _login(client)
        assert (await _exchange(client, code, nonce)).status_code == 200
        await _refused(await _exchange(client, code, nonce), ERR_STATE)

    async def test_expired_code_is_refused(self, client: Any, auth_db: Any) -> None:
        code = bound_state("expired-exchange-code", "n")
        auth_db.dal.hub_oauth_exchange_codes.insert(
            code=code,
            token="victim-session-jwt",  # noqa: S106
            platform="discord",
            used=False,
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
            created_at=datetime.now(UTC) - timedelta(minutes=2),
        )
        auth_db.dal.commit()
        await _refused(await _exchange(client, code, "n"), ERR_STATE)

    async def test_unbound_code_is_refused(self, client: Any) -> None:
        _code, nonce = await _login(client)
        await _refused(await _exchange(client, "plain-unbound-code", nonce), ERR_STATE)


class TestSsoHandoffBinds:
    async def test_sso_finish_redirect_sets_the_exchange_binding_cookie(
        self, auth_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import blueprints.v1.sso as sso

        class _Ctx:
            async_dal = auth_db
            dal = auth_db.dal

        monkeypatch.setattr(sso, "_ctx", lambda: _Ctx())
        monkeypatch.setattr(sso, "_cfg", _cfg)
        response = await sso._finish_redirect("conn1", "sso-jwt", "oidc")
        header = next(h for h in response.headers.getlist("Set-Cookie") if h.startswith(COOKIE))
        nonce = header.split(";", 1)[0].split("=", 1)[1]
        code = response.headers["Location"].split("code=")[1]
        assert nonce
        assert (
            await oauth_service.redeem_oauth_exchange_code(
                auth_db, auth_db.dal, code=code, binding_nonce=nonce
            )
            == "sso-jwt"
        )
        with pytest.raises(oauth_service.ApiError):
            await oauth_service.redeem_oauth_exchange_code(
                auth_db, auth_db.dal, code=code, binding_nonce=nonce
            )
