"""Regression: Discord login 'not snowflake' -- client_id hygiene + numeric id as external id."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from services import oauth_service
from services.errors import ApiError

_RealAsyncClient = httpx.AsyncClient


class _FakeDal:
    def __init__(self, rows: list[Any]) -> None:
        self.platform_configs = SimpleNamespace(platform="platform")
        self._rows = rows

    def __call__(self, _q: Any) -> Any:
        return None


class _FakeAsyncDal:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    async def select_async(self, _q: Any) -> list[Any]:
        return self._rows


def _rows(cid: str) -> list[Any]:
    return [
        SimpleNamespace(config_key="client_id", config_value=cid),
        SimpleNamespace(config_key="client_secret", config_value="sec"),
    ]


@pytest.mark.asyncio
async def test_discord_client_id_whitespace_stripped() -> None:
    creds = await oauth_service._get_platform_credentials(
        _FakeAsyncDal(_rows("123456789012345678\n")), _FakeDal([]), "discord"
    )
    assert creds["client_id"] == "123456789012345678"


@pytest.mark.asyncio
async def test_discord_non_snowflake_client_id_fails_loud() -> None:
    with pytest.raises(ApiError):
        await oauth_service._get_platform_credentials(
            _FakeAsyncDal(_rows("REPLACE_ME")), _FakeDal([]), "discord"
        )


@pytest.mark.asyncio
async def test_google_client_id_not_snowflake_checked() -> None:
    creds = await oauth_service._get_platform_credentials(
        _FakeAsyncDal(_rows("abc.apps.googleusercontent.com")), _FakeDal([]), "google"
    )
    assert creds["client_id"] == "abc.apps.googleusercontent.com"


@pytest.mark.asyncio
async def test_discord_exchange_maps_numeric_id(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/oauth2/token":
            assert b"client_id=123456789012345678" in request.content
            return httpx.Response(200, json={"access_token": "t"})
        return httpx.Response(
            200, json={"id": "80351110224678912", "username": "u", "avatar": None}
        )

    def factory(*a: Any, **kw: Any) -> httpx.AsyncClient:
        kw["transport"] = httpx.MockTransport(handler)
        return _RealAsyncClient(*a, **kw)

    monkeypatch.setattr(oauth_service.httpx, "AsyncClient", factory)
    out = await oauth_service._exchange_code(
        "discord", {"client_id": "123456789012345678", "client_secret": "s"}, "c", "https://x/cb"
    )
    assert out["id"] == "80351110224678912"
