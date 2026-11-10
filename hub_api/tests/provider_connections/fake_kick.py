"""A tiny fake of the Kick endpoints the adapter calls (only the socket is faked).

Serves ``POST /public/v1/token/introspect``, ``GET /public/v1/users``, ``GET /public/v1/channels``,
``GET kick.com/api/v2/channels/<slug>`` and the OAuth ``POST /oauth/token`` refresh grant, with
rotating single-use refresh tokens (reusing one is a 400, exactly like Kick).
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx

APP_CLIENT_ID = "01KICKAPPCLIENTID"
APP_CLIENT_SECRET = "kick-app-client-secret"  # noqa: S105 - fake
BOT_USER_ID = 555001
BOT_NAME = "waddlebot"
SCOPES = "user:read channel:read chat:write"
CHANNELS: dict[str, tuple[int, int]] = {
    # slug -> (broadcaster_user_id, chatroom_id)
    "streamer-one": (7001, 9001),
    "streamer_two": (7002, 9002),
}
HOSTS = ("api.kick.com", "kick.com", "id.kick.com")


class FakeKick:
    """Stateful fake: knows the issued tokens, counts every call, can be told to misbehave."""

    def __init__(self, clock: Callable[[], datetime], *, ttl_seconds: int = 7200) -> None:
        """Start with no tokens; ``clock`` anchors the ``exp`` it reports."""
        self._clock = clock
        self.ttl = ttl_seconds
        self.scopes = SCOPES
        self.access: dict[str, datetime] = {}
        self.refresh_tokens: set[str] = set()
        self.requests: list[httpx.Request] = []
        self.refresh_calls = 0
        self.refresh_status: int | None = None
        self.refresh_delay = 0.0
        self.refresh_omit_expiry = False
        self.introspect_status: int | None = None
        self.channel_status: int | None = None
        self.chatroom_status: int | None = None
        self.chatroom_user_id: int | None = None
        self.chatroom_id_override: Any = ...
        self.app_token = False
        self._seq = itertools.count(1)

    def mint(self) -> tuple[str, str]:
        """Issue a fresh (access, refresh) pair for the bot, expiring ``ttl`` from now."""
        n = next(self._seq)
        access, refresh = f"kick-access-{n}-AbC", f"kick-refresh-{n}-XyZ"
        self.access[access] = self._clock() + timedelta(seconds=self.ttl)
        self.refresh_tokens.add(refresh)
        return access, refresh

    def count(self, path_suffix: str) -> int:
        """How many requests hit a path ending in ``path_suffix``."""
        return sum(1 for r in self.requests if r.url.path.endswith(path_suffix))

    def _bearer(self, request: httpx.Request) -> str | None:
        header = request.headers.get("authorization", "")
        token = header[7:] if header.startswith("Bearer ") else ""
        return token if token in self.access else None

    async def handler(self, request: httpx.Request) -> httpx.Response:
        """Route one request."""
        self.requests.append(request)
        assert request.url.host in HOSTS, request.url
        path = request.url.path
        if path == "/oauth/token":
            return await self._token(request)
        if path == "/public/v1/token/introspect":
            return self._introspect(request)
        if path == "/public/v1/users":
            if self._bearer(request) is None:
                return httpx.Response(401, json={"message": "Unauthorized"})
            if self.app_token:
                return httpx.Response(200, json={"data": []})
            return httpx.Response(
                200, json={"data": [{"user_id": BOT_USER_ID, "name": BOT_NAME}], "message": "OK"}
            )
        if path == "/public/v1/channels":
            return self._channels(request)
        if path.startswith("/api/v2/channels/"):
            return self._chatroom(path.rsplit("/", 1)[1])
        return httpx.Response(404)

    def _introspect(self, request: httpx.Request) -> httpx.Response:
        if self.introspect_status is not None:
            return httpx.Response(self.introspect_status, json={})
        token = self._bearer(request)
        if token is None:
            return httpx.Response(200, json={"data": {"active": False}})
        return httpx.Response(
            200,
            json={
                "data": {
                    "active": True,
                    "client_id": APP_CLIENT_ID,
                    "exp": int(self.access[token].timestamp()),
                    "scope": self.scopes,
                },
                "message": "OK",
            },
        )

    def _channels(self, request: httpx.Request) -> httpx.Response:
        if self._bearer(request) is None:
            return httpx.Response(401, json={})
        if self.channel_status is not None:
            return httpx.Response(self.channel_status, json={})
        slug = request.url.params.get("slug", "")
        if slug not in CHANNELS:
            return httpx.Response(200, json={"data": [], "message": "OK"})
        return httpx.Response(
            200, json={"data": [{"broadcaster_user_id": CHANNELS[slug][0], "slug": slug}]}
        )

    def _chatroom(self, slug: str) -> httpx.Response:
        if self.chatroom_status is not None:
            return httpx.Response(self.chatroom_status, json={})
        if slug not in CHANNELS:
            return httpx.Response(404, json={})
        broadcaster, room = CHANNELS[slug]
        chatroom: Any = {"id": room}
        if self.chatroom_id_override is not ...:
            chatroom = self.chatroom_id_override
        body: dict[str, Any] = {
            "slug": slug,
            "user_id": self.chatroom_user_id if self.chatroom_user_id is not None else broadcaster,
            "chatroom": chatroom,
        }
        return httpx.Response(200, json=body)

    async def _token(self, request: httpx.Request) -> httpx.Response:
        self.refresh_calls += 1
        if self.refresh_delay:
            await asyncio.sleep(self.refresh_delay)
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        assert form["grant_type"] == "refresh_token"
        assert form["client_id"] == APP_CLIENT_ID and form["client_secret"] == APP_CLIENT_SECRET
        if self.refresh_status is not None:
            return httpx.Response(self.refresh_status, json={"error": "x"})
        used = form["refresh_token"]
        if used not in self.refresh_tokens:
            return httpx.Response(400, json={"error": "invalid_grant"})
        self.refresh_tokens.discard(used)  # rotating: single use
        access, refresh = self.mint()
        body: dict[str, Any] = {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "Bearer",
            "scope": self.scopes,
        }
        if not self.refresh_omit_expiry:
            body["expires_in"] = self.ttl
        return httpx.Response(200, json=body)

    @property
    def transport(self) -> httpx.MockTransport:
        """The socket replacement."""
        return httpx.MockTransport(self.handler)
