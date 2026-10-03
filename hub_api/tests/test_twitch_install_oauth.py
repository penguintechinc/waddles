"""Tests for `services/twitch_install_oauth.py` -- tenant-supplied-credential Twitch OAuth.

Same `httpx.MockTransport` approach as `test_oauth_providers.py`: no real
network I/O, `validate_outbound_url` swapped for a no-op pass-through by
default (guard *integration* is exercised directly in
`TestSSRFGuardIntegration`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from services import twitch_install_oauth as mod
from services.errors import bad_request

_RealAsyncClient = httpx.AsyncClient


def _client_factory(transport: httpx.MockTransport) -> Callable[..., httpx.AsyncClient]:
    def factory(*_args: Any, **_kwargs: Any) -> httpx.AsyncClient:
        return _RealAsyncClient(transport=transport)

    return factory


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))


@pytest.fixture(autouse=True)
def _no_real_ssrf_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _pass_through(url: str, *, allowed_schemes: tuple[str, ...]) -> str:
        return url

    monkeypatch.setattr(mod, "validate_outbound_url", _pass_through)


class TestBuildAuthorizeUrl:
    def test_requests_role_sync_scopes(self) -> None:
        url = mod.build_authorize_url(
            client_id="cid", redirect_uri="https://hub.example/callback", state="s1"
        )
        assert url.startswith(mod.TWITCH_AUTHORIZE_URL)
        assert "scope=channel%3Aread%3Asubscriptions" in url
        assert "moderation%3Aread" in url
        assert "channel%3Amanage%3Amoderators" in url
        assert "state=s1" in url
        assert "client_id=cid" in url
        assert "force_verify=true" in url


class TestExchangeCode:
    async def test_success_returns_normalized_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url) == mod.TWITCH_TOKEN_URL
            return httpx.Response(
                200,
                json={
                    "access_token": "tok_abc",  # noqa: S106
                    "refresh_token": "ref_abc",  # noqa: S106
                    "expires_in": 14400,
                    "scope": list(mod.ROLE_SYNC_SCOPES),
                    "token_type": "bearer",
                },
            )

        _install_transport(monkeypatch, handler)
        result = await mod.exchange_code(
            client_id="cid", client_secret="csecret", code="authcode", redirect_uri="https://x/cb"
        )
        assert result.access_token == "tok_abc"  # noqa: S105
        assert result.refresh_token == "ref_abc"  # noqa: S105
        assert result.expires_in == 14400
        assert result.scopes == list(mod.ROLE_SYNC_SCOPES)

    async def test_non_2xx_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"message": "invalid code"})

        _install_transport(monkeypatch, handler)
        with pytest.raises(mod.TwitchOAuthError, match="HTTP 400"):
            await mod.exchange_code(
                client_id="cid", client_secret="csecret", code="bad", redirect_uri="https://x/cb"
            )

    async def test_missing_refresh_token_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"access_token": "tok_abc"})  # noqa: S106

        _install_transport(monkeypatch, handler)
        with pytest.raises(mod.TwitchOAuthError, match="refresh_token"):
            await mod.exchange_code(
                client_id="cid",
                client_secret="csecret",
                code="authcode",
                redirect_uri="https://x/cb",
            )

    async def test_transport_failure_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        _install_transport(monkeypatch, handler)
        with pytest.raises(mod.TwitchOAuthError, match="token request failed"):
            await mod.exchange_code(
                client_id="cid",
                client_secret="csecret",
                code="authcode",
                redirect_uri="https://x/cb",
            )


class TestRefreshAccessToken:
    async def test_success_returns_rotated_pair(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = request.content.decode()
            assert "grant_type=refresh_token" in body
            assert "refresh_token=old_ref" in body
            return httpx.Response(
                200,
                json={
                    "access_token": "tok_new",  # noqa: S106
                    "refresh_token": "ref_new",  # noqa: S106
                    "expires_in": 14400,
                    "scope": list(mod.ROLE_SYNC_SCOPES),
                    "token_type": "bearer",
                },
            )

        _install_transport(monkeypatch, handler)
        result = await mod.refresh_access_token(
            client_id="cid", client_secret="csecret", refresh_token="old_ref"
        )
        assert result.access_token == "tok_new"  # noqa: S105
        assert result.refresh_token == "ref_new"  # noqa: S105

    async def test_rejected_refresh_token_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Simulates Twitch rejecting an already-rotated (reused) refresh token."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"message": "Invalid refresh token"})

        _install_transport(monkeypatch, handler)
        with pytest.raises(mod.TwitchOAuthError):
            await mod.refresh_access_token(
                client_id="cid", client_secret="csecret", refresh_token="reused_ref"
            )


class TestSSRFGuardIntegration:
    async def test_guard_rejection_becomes_oauth_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _reject(url: str, *, allowed_schemes: tuple[str, ...]) -> str:
            raise bad_request("blocked")

        monkeypatch.setattr(mod, "validate_outbound_url", _reject)
        with pytest.raises(mod.TwitchOAuthError, match="outbound URL blocked"):
            await mod.exchange_code(
                client_id="cid",
                client_secret="csecret",
                code="authcode",
                redirect_uri="https://x/cb",
            )


class TestNoSecretsInLogs:
    async def test_failure_log_never_contains_secret(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"message": "nope"})

        _install_transport(monkeypatch, handler)
        with caplog.at_level("WARNING"):
            with pytest.raises(mod.TwitchOAuthError):
                await mod.exchange_code(
                    client_id="cid",
                    client_secret="super-secret-value",  # noqa: S106
                    code="authcode-value",
                    redirect_uri="https://x/cb",
                )
        text = caplog.text
        assert "super-secret-value" not in text
        assert "authcode-value" not in text
