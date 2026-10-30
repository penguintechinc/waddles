"""`services/credential_manager_client.py` origin pinning (gh-969).

# regression: gh-969 -- the caller's bearer must only ever reach an approved origin.
"""

from __future__ import annotations

import httpx
import pytest

from services import credential_manager_client as cmc
from services.errors import ApiError


@pytest.mark.parametrize(
    "url",
    [
        "http://credential-manager:8095",
        "http://credential-manager.waddlebot.svc:8095",
        "http://credential-manager.waddlebot.svc.cluster.local:8095/",
        "https://credmgr.example.com",
    ],
)
def test_approved_origins_are_accepted(url: str) -> None:
    assert cmc.validate_origin(url).startswith(("http://", "https://"))
    assert not cmc.validate_origin(url).endswith("/")


@pytest.mark.parametrize(
    "url",
    [
        "http://evil.example.com",  # plain http off-cluster
        "ftp://credential-manager",
        "https://user:pw@credmgr.example.com",
        "https://credmgr.example.com/api",
        "https://credmgr.example.com?x=1",
        "https://credmgr.example.com#f",
        "https://10.0.0.5",
        "http://127.0.0.1:8095",
        "http://localhost:8095",
        "https://[::1]",
        "not a url",
        "",
    ],
)
def test_untrusted_origins_are_rejected(url: str) -> None:
    with pytest.raises(cmc.UntrustedOriginError):
        cmc.validate_origin(url)


def test_allowed_hosts_list_narrows_the_origin() -> None:
    allowed = frozenset({"credmgr.example.com"})
    assert cmc.validate_origin("https://credmgr.example.com", allowed_hosts=allowed)
    with pytest.raises(cmc.UntrustedOriginError):
        cmc.validate_origin("https://other.example.com", allowed_hosts=allowed)


async def test_bad_url_never_sends_the_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mis-set CREDENTIAL_MANAGER_URL is a loud 503 and NO request is made."""
    seen: list[httpx.Request] = []
    real = httpx.AsyncClient

    def _factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200))
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", _factory)
    monkeypatch.setenv("CREDENTIAL_MANAGER_URL", "http://attacker.example.com")
    with pytest.raises(ApiError) as exc:
        await cmc.test_bot_connection("twitch", bearer_token="SECRET-BEARER")
    assert exc.value.status_code == 503 and exc.value.code == cmc.UNAVAILABLE_CODE
    assert seen == []


async def test_client_ignores_proxy_env_and_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    real = httpx.AsyncClient

    def _factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        captured.update(kwargs)
        kwargs["transport"] = httpx.MockTransport(
            lambda r: httpx.Response(
                200, json={"data": {"platform": "twitch", "valid": True, "error": None}}
            )
        )
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", _factory)
    monkeypatch.delenv("CREDENTIAL_MANAGER_URL", raising=False)
    assert await cmc.test_bot_connection("twitch", bearer_token="t") == (True, None)
    assert captured["trust_env"] is False and captured["follow_redirects"] is False
