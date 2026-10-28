"""Tests for the Google consumer social-login provider in `services/oauth_service.py`."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from services import oauth_service

_RealAsyncClient = httpx.AsyncClient


def _patch_http(monkeypatch: pytest.MonkeyPatch, userinfo: dict[str, Any]) -> list[httpx.Request]:
    """Swap httpx.AsyncClient for a MockTransport serving Google's token + userinfo."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if str(request.url) == "https://oauth2.googleapis.com/token":
            return httpx.Response(200, json={"access_token": "tok"})
        if str(request.url) == "https://openidconnect.googleapis.com/v1/userinfo":
            return httpx.Response(200, json=userinfo)
        return httpx.Response(404)

    def factory(*a: Any, **kw: Any) -> httpx.AsyncClient:
        kw["transport"] = httpx.MockTransport(handler)
        return _RealAsyncClient(*a, **kw)

    monkeypatch.setattr(oauth_service.httpx, "AsyncClient", factory)
    return seen


def test_google_is_valid_platform() -> None:
    assert "google" in oauth_service.VALID_PLATFORMS


def test_authorize_url() -> None:
    url = oauth_service._generate_authorize_url("google", "cid", "https://x/cb", "st")
    parsed = urlparse(url)
    q = parse_qs(parsed.query)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == (
        "https://accounts.google.com/o/oauth2/v2/auth"
    )
    assert q["scope"] == ["openid email profile"]
    assert q["redirect_uri"] == ["https://x/cb"]
    assert q["response_type"] == ["code"]
    assert q["client_id"] == ["cid"]
    assert q["state"] == ["st"]
    assert q["access_type"] == ["online"]
    assert q["prompt"] == ["select_account"]


def test_other_platforms_have_no_google_params() -> None:
    q = parse_qs(urlparse(oauth_service._generate_authorize_url("discord", "c", "r", "s")).query)
    assert "prompt" not in q and "access_type" not in q


async def test_exchange_maps_userinfo(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _patch_http(
        monkeypatch,
        {
            "sub": "1234",
            "email": "a@b.com",
            "email_verified": True,
            "name": "Ann",
            "picture": "https://p/x.png",
        },
    )
    out = await oauth_service._exchange_code(
        "google", {"client_id": "c", "client_secret": "s"}, "code", "https://x/cb"
    )
    assert out == {
        "id": "1234",
        "username": "Ann",
        "email": "a@b.com",
        "avatar_url": "https://p/x.png",
    }
    assert seen[1].headers["Authorization"] == "Bearer tok"
    body = parse_qs(seen[0].content.decode())
    assert body["grant_type"] == ["authorization_code"] and body["code"] == ["code"]


async def test_unverified_email_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_http(monkeypatch, {"sub": "9", "email": "evil@b.com", "email_verified": False})
    out = await oauth_service._exchange_code(
        "google", {"client_id": "c", "client_secret": "s"}, "code", "r"
    )
    assert out["email"] is None
    assert out["username"] == "evil"


class _FakeAsyncDal:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    async def select_async(self, _q: Any) -> list[Any]:
        return self.rows


class _Col:
    def __eq__(self, other: object) -> bool:  # type: ignore[override]
        return True


async def test_creds_env_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "env-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "env-secret")
    dal = SimpleNamespace(platform_configs=SimpleNamespace(platform=_Col()))
    creds = await oauth_service._get_platform_credentials(
        _FakeAsyncDal([]), _CallableDal(dal), "google"
    )
    assert creds == {"client_id": "env-id", "client_secret": "env-secret"}


class _CallableDal:
    def __init__(self, inner: Any) -> None:
        self.platform_configs = inner.platform_configs

    def __call__(self, q: Any) -> Any:
        return q


async def test_creds_platform_configs_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "env-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "env-secret")
    rows = [
        SimpleNamespace(config_key="client_id", config_value="db-id"),
        SimpleNamespace(config_key="client_secret", config_value="db-secret"),
    ]
    dal = _CallableDal(SimpleNamespace(platform_configs=SimpleNamespace(platform=_Col())))
    creds = await oauth_service._get_platform_credentials(_FakeAsyncDal(rows), dal, "google")
    assert creds == {"client_id": "db-id", "client_secret": "db-secret"}
