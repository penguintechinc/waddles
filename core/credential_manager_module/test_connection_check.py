"""`POST /api/v1/credentials/<platform>/test` + `connection_tester` (gh-969).

# regression: gh-969 -- hub-api's DB role cannot read operator bot tokens (H-3), so the
# superadmin "test connection" check is served here. The route is `credentials:admin`-gated and
# returns the validation outcome only, never the token.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from flask_core.auth import create_jwt_token

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost:5432/db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault(
    "CREDENTIAL_ENCRYPTION_KEY", "d4f9317783becee1a4415c1a1229b9258e7a90b768d72a9e2c7dc891af661df6"
)

from . import app as app_module  # noqa: E402
from .services.connection_tester import validate_token  # noqa: E402
from .services.refresh_service import RefreshService  # noqa: E402
from .services.token_crypto import encrypt_value  # noqa: E402

SECRET = "change-me-in-production"
SECRET_TOKEN = "CANARY-BOT-TOKEN"


def _jwt(scope: str) -> dict[str, str]:
    token = create_jwt_token(
        user_id="1",
        username="a",
        email="a@example.com",
        roles=[],
        secret_key=SECRET,
        tenant="acme-corp",
        scope=scope,
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client() -> Any:
    return app_module.app.test_client()


class TestRoute:
    async def test_requires_token(self, client: Any) -> None:
        assert (await client.post("/api/v1/credentials/twitch/test")).status_code == 403

    async def test_requires_credentials_scope(self, client: Any) -> None:
        r = await client.post("/api/v1/credentials/twitch/test", headers=_jwt("widgets:read"))
        assert r.status_code == 403

    async def test_unsupported_platform_is_400(self, client: Any) -> None:
        r = await client.post("/api/v1/credentials/evil/test", headers=_jwt("credentials:admin"))
        assert r.status_code == 400

    async def test_uninitialized_service_is_503(self, client: Any, monkeypatch: Any) -> None:
        monkeypatch.setattr(app_module, "refresh_service", None)
        r = await client.post("/api/v1/credentials/twitch/test", headers=_jwt("credentials:admin"))
        assert r.status_code == 503

    async def test_missing_credential_is_404_and_ok_never_returns_token(
        self, client: Any, monkeypatch: Any
    ) -> None:
        svc = MagicMock()
        svc.test_bot_connection = AsyncMock(return_value=None)
        monkeypatch.setattr(app_module, "refresh_service", svc)
        r = await client.post("/api/v1/credentials/twitch/test", headers=_jwt("*:admin"))
        assert r.status_code == 404
        svc.test_bot_connection = AsyncMock(return_value=(True, None))
        r = await client.post("/api/v1/credentials/twitch/test", headers=_jwt("*:admin"))
        body = await r.get_json()
        assert r.status_code == 200 and body["data"] == {
            "platform": "twitch",
            "valid": True,
            "error": None,
        }
        assert SECRET_TOKEN not in str(body)


class TestRefreshServiceBotTest:
    async def test_decrypts_in_memory_and_validates(self, monkeypatch: Any) -> None:
        svc = RefreshService(database_url="postgresql://u:p@localhost/db", redis_url="redis://x")
        conn = MagicMock()
        conn.fetchrow = AsyncMock(
            return_value={"access_token": encrypt_value(SECRET_TOKEN), "is_encrypted": True}
        )
        pool = MagicMock()
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers["Authorization"])
            return httpx.Response(200, json={})

        svc._pool = pool
        svc._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        assert await svc.test_bot_connection("twitch") == (True, None)
        assert seen == [f"OAuth {SECRET_TOKEN}"]
        sql = conn.fetchrow.call_args.args[0]
        assert "integration_type = 'bot'" in sql
        conn.fetchrow = AsyncMock(return_value=None)
        assert await svc.test_bot_connection("twitch") is None
        await svc._http.aclose()


class TestValidateToken:
    @pytest.mark.parametrize(
        ("platform", "status", "expected"),
        [
            ("twitch", 200, (True, None)),
            ("discord", 401, (False, "HTTP 401")),
            ("youtube", 200, (True, None)),
            ("kick", 500, (True, None)),
        ],
    )
    async def test_get_platforms(self, platform: str, status: int, expected: Any) -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status)))
        assert await validate_token(client, platform, "t") == expected

    async def test_slack_and_transport_failure_fail_closed(self) -> None:
        ok = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": False}))
        )
        assert await validate_token(ok, "slack", "t") == (False, "invalid token")

        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        bad = httpx.AsyncClient(transport=httpx.MockTransport(boom))
        assert await validate_token(bad, "twitch", "t") == (False, "ConnectError")
