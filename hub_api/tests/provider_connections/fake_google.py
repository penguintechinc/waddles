"""An in-process Google: just enough OAuth2 + YouTube Data API to drive the real YouTube adapter.

Only the *socket* is faked (``httpx.MockTransport``); the adapter's host allowlist, SSRF guard,
request construction, size bound, JSON handling and error mapping all run for real. The fake
verifies what Google verifies: the authorization code is single use and bound to its client,
redirect URI and PKCE challenge (S256), the client secret must match, and a refresh token only
works for the client that minted it. Optional rotation makes ``/token`` return a NEW refresh token.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import parse_qs

import httpx

APP_CLIENT_ID = "tenant-app.apps.googleusercontent.com"
APP_CLIENT_SECRET = "tenant-app-client-secret"  # noqa: S105 - test-only value
OTHER_CLIENT_ID = "some-other-app.apps.googleusercontent.com"
FORCE_SSL = "https://www.googleapis.com/auth/youtube.force-ssl"
READONLY = "https://www.googleapis.com/auth/youtube.readonly"


@dataclass(slots=True)
class FakeChannel:
    """A YouTube channel owned by a Google account."""

    channel_id: str
    title: str


@dataclass(slots=True)
class FakeToken:
    """One issued access token."""

    channel: FakeChannel | None
    client_id: str
    scopes: list[str]
    expires_in: int


@dataclass(slots=True)
class FakeGoogle:
    """Mutable fake state + the ``MockTransport`` the adapter is pointed at."""

    tokens: dict[str, FakeToken] = field(default_factory=dict)
    refresh_tokens: dict[str, FakeToken] = field(default_factory=dict)  # live ones only
    codes: dict[str, tuple[FakeToken, str, str, bool]] = field(default_factory=dict)
    #: channel id -> list of liveBroadcasts items (already in API shape)
    broadcasts: dict[str, list[dict[str, object]]] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)  # (METHOD, host+path)
    bodies: list[dict[str, str]] = field(default_factory=list)  # form bodies of POSTs
    rotate_refresh: bool = False
    down: bool = False
    token_error: str | None = None  # force /token to answer 400 {"error": <this>}
    youtube_status: int | None = None  # force every Data API call to this status
    youtube_reason: str | None = None  # ... carrying this error reason
    refresh_calls: int = 0
    _ids: Callable[[], int] = field(default_factory=lambda: itertools.count(1).__next__)

    def mint(
        self,
        *,
        channel: FakeChannel | None,
        client_id: str = APP_CLIENT_ID,
        scopes: list[str] | None = None,
        expires_in: int = 3599,
        with_refresh: bool = True,
    ) -> tuple[str, str | None]:
        """Issue ``(access, refresh | None)`` (runtime-built values, never literals)."""
        n = self._ids()
        access = f"ya29.acc{n}" + "A" * 12
        token = FakeToken(channel, client_id, [FORCE_SSL] if scopes is None else scopes, expires_in)
        self.tokens[access] = token
        refresh = None
        if with_refresh:
            refresh = f"1//ref{n}" + "R" * 12
            self.refresh_tokens[refresh] = token
        return access, refresh

    def issue_code(
        self,
        *,
        channel: FakeChannel | None,
        challenge: str,
        redirect_uri: str,
        client_id: str = APP_CLIENT_ID,
        with_refresh: bool = True,
        scopes: list[str] | None = None,
    ) -> str:
        """What the consent screen does: bind a one-time code to client + PKCE + redirect."""
        n = self._ids()
        code = f"4/code{n}" + "C" * 10
        token = FakeToken(channel, client_id, [FORCE_SSL] if scopes is None else scopes, 3599)
        self.codes[code] = (token, challenge, redirect_uri, with_refresh)
        return code

    @property
    def transport(self) -> httpx.MockTransport:
        """The mock socket."""
        return httpx.MockTransport(self._handle)

    # -- handlers --------------------------------------------------------------------------

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("fake google is down", request=request)
        self.calls.append((request.method, request.url.host + request.url.path))
        path = request.url.path
        if path == "/tokeninfo":
            return self._tokeninfo(request)
        if path == "/token":
            return self._token(request)
        if path.startswith("/youtube/v3/"):
            return self._youtube(request)
        return httpx.Response(404, json={"error": "not_found"})

    def _form(self, request: httpx.Request) -> dict[str, str]:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.bodies.append(form)
        return form

    def _tokeninfo(self, request: httpx.Request) -> httpx.Response:
        token = self.tokens.get(self._form(request).get("access_token", ""))
        if token is None:
            return httpx.Response(400, json={"error": "invalid_token"})
        return httpx.Response(
            200,
            json={
                "aud": token.client_id,
                "azp": token.client_id,
                "scope": " ".join(token.scopes),
                "expires_in": str(token.expires_in),
            },
        )

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = self._form(request)
        if self.token_error is not None:
            return httpx.Response(400, json={"error": self.token_error})
        if form.get("grant_type") == "authorization_code":
            return self._exchange(form)
        if form.get("grant_type") == "refresh_token":
            return self._refresh(form)
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def _exchange(self, form: dict[str, str]) -> httpx.Response:
        entry = self.codes.pop(form.get("code", ""), None)  # single use
        if entry is None:
            return httpx.Response(400, json={"error": "invalid_grant"})
        token, challenge, redirect_uri, with_refresh = entry
        digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
        computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        if (
            form.get("client_id") != token.client_id
            or form.get("client_secret") != APP_CLIENT_SECRET
            or form.get("redirect_uri") != redirect_uri
            or computed != challenge
        ):
            return httpx.Response(400, json={"error": "invalid_grant"})
        access = f"ya29.acc{self._ids()}" + "A" * 12
        self.tokens[access] = token
        body: dict[str, object] = {
            "access_token": access,
            "expires_in": token.expires_in,
            "scope": " ".join(token.scopes),
            "token_type": "Bearer",
        }
        if with_refresh:
            refresh = f"1//ref{self._ids()}" + "R" * 12
            self.refresh_tokens[refresh] = token
            body["refresh_token"] = refresh
        return httpx.Response(200, json=body)

    def _refresh(self, form: dict[str, str]) -> httpx.Response:
        self.refresh_calls += 1
        if form.get("client_secret") != APP_CLIENT_SECRET:
            return httpx.Response(401, json={"error": "invalid_client"})
        presented = form.get("refresh_token", "")
        token = self.refresh_tokens.get(presented)
        if token is None or token.client_id != form.get("client_id"):
            return httpx.Response(400, json={"error": "invalid_grant"})
        access = f"ya29.acc{self._ids()}" + "A" * 12
        self.tokens[access] = FakeToken(token.channel, token.client_id, token.scopes, 3599)
        body: dict[str, object] = {
            "access_token": access,
            "expires_in": 3599,
            "scope": " ".join(token.scopes),
            "token_type": "Bearer",
        }
        if self.rotate_refresh:
            del self.refresh_tokens[presented]
            new_refresh = f"1//ref{self._ids()}" + "R" * 12
            self.refresh_tokens[new_refresh] = token
            body["refresh_token"] = new_refresh
        return httpx.Response(200, json=body)

    def _youtube(self, request: httpx.Request) -> httpx.Response:
        if self.youtube_status is not None:
            reasons = [{"reason": self.youtube_reason}] if self.youtube_reason else []
            return httpx.Response(self.youtube_status, json={"error": {"errors": reasons}})
        header = request.headers.get("Authorization", "")
        token = self.tokens.get(header.removeprefix("Bearer "))
        if token is None:
            return httpx.Response(401, json={"error": {"errors": []}})
        if token.channel is None:  # a Google account with no YouTube channel
            return httpx.Response(200, json={"items": []})
        if request.url.path.endswith("/channels"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"id": token.channel.channel_id, "snippet": {"title": token.channel.title}}
                    ]
                },
            )
        if request.url.path.endswith("/liveBroadcasts"):
            return httpx.Response(
                200, json={"items": self.broadcasts.get(token.channel.channel_id, [])}
            )
        return httpx.Response(404, json={"error": {"errors": []}})


def broadcast(
    chat_id: str | None,
    *,
    channel_id: str,
    status: str = "live",
    started: str = "2026-11-05T10:00:00Z",
) -> dict[str, object]:
    """One ``liveBroadcasts`` item in the API's shape."""
    snippet: dict[str, object] = {"channelId": channel_id, "actualStartTime": started}
    if chat_id is not None:
        snippet["liveChatId"] = chat_id
    return {"snippet": snippet, "status": {"lifeCycleStatus": status}}
