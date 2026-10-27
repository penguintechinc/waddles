"""An in-process Twitch: just enough of id.twitch.tv + Helix to drive the real adapter.

Only the *socket* is faked (``httpx.MockTransport``); the adapter's SSRF guard, request
construction, size bound, JSON handling and error mapping all run for real. The fake models the one
Twitch behaviour the refresh design hinges on: **refresh tokens are single use** -- spending one
invalidates it, and presenting a spent one is a 400, exactly like production.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import parse_qs

import httpx

APP_CLIENT_ID = "waddles-app-client-id"
APP_CLIENT_SECRET = "waddles-app-client-secret"  # noqa: S105 - test-only value
OTHER_APP_CLIENT_ID = "some-other-twitch-app"


@dataclass(slots=True)
class FakeAccount:
    """A Twitch user (bot or streamer) known to the fake."""

    user_id: str
    login: str


@dataclass(slots=True)
class FakeToken:
    """One issued user access token."""

    account: FakeAccount
    client_id: str
    scopes: list[str]
    expires_in: int
    user_token: bool = True


@dataclass(slots=True)
class FakeTwitch:
    """Mutable fake state + the ``MockTransport`` the adapter is pointed at."""

    tokens: dict[str, FakeToken] = field(default_factory=dict)
    channels: dict[str, str] = field(default_factory=dict)  # login -> user id
    refresh_tokens: dict[str, FakeToken] = field(default_factory=dict)  # live ones only
    calls: list[tuple[str, str]] = field(default_factory=list)  # (METHOD, host+path)
    urls: list[str] = field(default_factory=list)  # full request URLs (query strings included)
    refresh_calls: int = 0
    down: bool = False
    status_override: dict[str, int] = field(default_factory=dict)  # "validate"|"helix"|"token"
    raw_body_override: dict[str, bytes] = field(default_factory=dict)
    _ids: Callable[[], int] = field(default_factory=lambda: itertools.count(1).__next__)

    def mint(
        self,
        *,
        account: FakeAccount,
        scopes: list[str] | None = None,
        client_id: str = APP_CLIENT_ID,
        expires_in: int = 14000,
        with_refresh: bool = True,
        user_token: bool = True,
    ) -> tuple[str, str | None]:
        """Issue ``(access_token, refresh_token)`` for ``account`` (runtime-built, not literals)."""
        n = self._ids()
        access = f"acc{n}x" + "A" * 12
        token = FakeToken(
            account=account,
            client_id=client_id,
            scopes=["chat:read", "chat:edit"] if scopes is None else scopes,
            expires_in=expires_in,
            user_token=user_token,
        )
        self.tokens[access] = token
        refresh = None
        if with_refresh:
            refresh = f"ref{n}x" + "R" * 12
            self.refresh_tokens[refresh] = token
        return access, refresh

    # -- transport -------------------------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Route a request to the matching fake endpoint."""
        if self.down:
            raise httpx.ConnectError("fake twitch is down")
        self.calls.append((request.method, request.url.host + request.url.path))
        self.urls.append(str(request.url))
        path = request.url.path
        if request.url.host == "id.twitch.tv" and path == "/oauth2/validate":
            return self._validate(request)
        if request.url.host == "api.twitch.tv" and path == "/helix/users":
            return self._helix_users(request)
        if request.url.host == "id.twitch.tv" and path == "/oauth2/token":
            return self._token(request)
        return httpx.Response(404, json={"error": "not found"})

    @property
    def transport(self) -> httpx.MockTransport:
        """The socket replacement handed to ``TwitchAdapter(transport=...)``."""
        return httpx.MockTransport(self.handler)

    def _forced(self, kind: str) -> httpx.Response | None:
        if kind in self.raw_body_override:
            return httpx.Response(200, content=self.raw_body_override[kind])
        if kind in self.status_override:
            return httpx.Response(self.status_override[kind], json={"message": "forced"})
        return None

    def _validate(self, request: httpx.Request) -> httpx.Response:
        forced = self._forced("validate")
        if forced is not None:
            return forced
        header = request.headers.get("Authorization", "")
        token = self.tokens.get(header.removeprefix("OAuth ").strip())
        if not header.startswith("OAuth ") or token is None:
            return httpx.Response(401, json={"status": 401, "message": "invalid access token"})
        if not token.user_token:
            return httpx.Response(
                200, json={"client_id": token.client_id, "scopes": [], "expires_in": 3600}
            )
        return httpx.Response(
            200,
            json={
                "client_id": token.client_id,
                "login": token.account.login,
                "scopes": token.scopes,
                "user_id": token.account.user_id,
                "expires_in": token.expires_in,
            },
        )

    def _helix_users(self, request: httpx.Request) -> httpx.Response:
        forced = self._forced("helix")
        if forced is not None:
            return forced
        bearer = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        token = self.tokens.get(bearer)
        if token is None or request.headers.get("Client-Id") != token.client_id:
            return httpx.Response(401, json={"status": 401, "message": "Invalid OAuth token"})
        login = request.url.params.get("login", "")
        if not login.replace("_", "").isalnum():
            return httpx.Response(400, json={"message": "Invalid login"})
        user_id = self.channels.get(login)
        data = [{"id": user_id, "login": login, "display_name": login.title()}] if user_id else []
        return httpx.Response(200, json={"data": data})

    def _token(self, request: httpx.Request) -> httpx.Response:
        forced = self._forced("token")
        if forced is not None:
            return forced
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.refresh_calls += 1
        old = self.refresh_tokens.pop(form.get("refresh_token", ""), None)
        if (
            form.get("grant_type") != "refresh_token"
            or old is None
            or form.get("client_id") != APP_CLIENT_ID
            or form.get("client_secret") != APP_CLIENT_SECRET
        ):
            return httpx.Response(400, json={"status": 400, "message": "Invalid refresh token"})
        access, refresh = self.mint(
            account=old.account, scopes=list(old.scopes), expires_in=14400, client_id=old.client_id
        )
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "access_token": access,
                    "refresh_token": refresh,
                    "expires_in": 14400,
                    "scope": old.scopes,
                    "token_type": "bearer",
                }
            ),
            headers={"content-type": "application/json"},
        )
