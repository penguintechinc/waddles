"""Twitch Helix API client -- app-access-token (`client_credentials`) auth, no user OAuth.

Ported from the legacy `action/interactive/shoutout_interaction_module/
services/twitch_service.py` (`get_user_info`/`get_channel_info`/
`get_stream_info`) and `video_service.py` (`get_twitch_clips`) into one
client sized for `builtin_handlers/twitch_shoutout_action.py`'s action-stage
entrypoint contract -- reuses the shared `httpx.AsyncClient` the runner
already threads through every action-stage script
(`runner.py::_handle_envelope`) rather than opening a fresh `aiohttp`
session per call.

Mints an APP token via `client_credentials` (never a user OAuth token --
`!so`/`!vso` read public user/channel/stream/clip data only, no user-
scoped Helix call needed), cached in-process until 60s before its own
`expires_in`, re-minted once on a live 401 (a revoked/rotated app token)
-- never retried in a loop; `runner.py`'s own `retry_with_backoff` owns
retry timing for whichever `waddle_transports` error type a caller
chooses to raise around this client's own errors.

Reads `TWITCH_CLIENT_ID`/`TWITCH_CLIENT_SECRET` directly from the process
environment (security.md Secrets & Credentials: env vars, never a literal
in handler config) -- the connector's own platform-wide app credentials,
one pair for the whole svc-action process, not a per-tenant
`waddle_transports.signing.resolve_secret` indirection like
`discord_send_action.py`'s `bot_token_ref` (Discord's credential is per-
bot-install; Twitch's app credentials are process-wide, matching
`twitch_send_action.py`'s own "svc-action never holds a *user* token"
boundary -- it holds one platform app credential here, same tier as any
other `*_URL`-style env config).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

_TOKEN_URL = "https://id.twitch.tv/oauth2/token"  # noqa: S105 -- URL constant, not a secret
_HELIX_BASE = "https://api.twitch.tv/helix"
_REQUEST_TIMEOUT_SECONDS = 10.0
#: Re-mint the app token this many seconds before its own `expires_in`
#: elapses -- avoids a request racing the token's real expiry mid-flight.
_EXPIRY_SAFETY_MARGIN_SECONDS = 60.0


class TwitchHelixError(Exception):
    """Raised for any Helix/OAuth failure -- callers (the shoutout handler) catch this directly."""


@dataclass(slots=True)
class _CachedAppToken:
    """One cached app-access-token, keyed by its own monotonic expiry."""

    value: str
    client_id: str
    expires_at: float


class TwitchHelixClient:
    """Thin Helix wrapper: app-token mint/cache/refresh-on-401 + four read-only GET calls."""

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        token_url: str = _TOKEN_URL,
        api_base: str = _HELIX_BASE,
    ) -> None:
        """Bind to a shared `httpx.AsyncClient` -- `token_url`/`api_base` overridable for tests."""
        self._http = http_client
        self._token_url = token_url
        self._api_base = api_base
        self._token: _CachedAppToken | None = None

    async def _mint_app_token(self) -> _CachedAppToken:
        """Mint a fresh `client_credentials` app token. Raises `TwitchHelixError` on any failure."""
        client_id = os.environ.get("TWITCH_CLIENT_ID", "")
        client_secret = os.environ.get("TWITCH_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            raise TwitchHelixError("twitch app credentials not configured")

        try:
            response = await self._http.post(
                self._token_url,
                params={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "client_credentials",
                },
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
            raise TwitchHelixError(f"twitch oauth token request failed: {exc}") from exc

        if response.status_code == 401:
            raise TwitchHelixError("twitch oauth token didn't work (401)")
        if response.status_code != 200:
            raise TwitchHelixError(
                f"twitch oauth token request returned HTTP {response.status_code}"
            )

        try:
            data = response.json()
            token_value = str(data["access_token"])
            expires_in = float(data.get("expires_in", 3600))
        except (ValueError, KeyError, TypeError) as exc:
            raise TwitchHelixError(f"twitch oauth token response malformed: {exc}") from exc

        expires_at = time.monotonic() + max(expires_in - _EXPIRY_SAFETY_MARGIN_SECONDS, 0.0)
        return _CachedAppToken(value=token_value, client_id=client_id, expires_at=expires_at)

    async def _get_app_token(self, *, force_refresh: bool = False) -> _CachedAppToken:
        """Return the cached app token; mints/re-mints if absent, expired, or `force_refresh`."""
        cached = self._token
        if not force_refresh and cached is not None and cached.expires_at > time.monotonic():
            return cached
        self._token = await self._mint_app_token()
        return self._token

    async def _get(
        self, path: str, params: dict[str, str], *, _retried: bool = False
    ) -> dict[str, Any]:
        """One authenticated Helix GET; refreshes the app token exactly once on a 401."""
        token = await self._get_app_token()
        try:
            response = await self._http.get(
                f"{self._api_base}{path}",
                headers={"Client-Id": token.client_id, "Authorization": f"Bearer {token.value}"},
                params=params,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
            raise TwitchHelixError(f"twitch helix request failed: {exc}") from exc

        if response.status_code == 401:
            if _retried:
                raise TwitchHelixError("twitch oauth token didn't work (401)")
            await self._get_app_token(force_refresh=True)
            return await self._get(path, params, _retried=True)
        if response.status_code == 429:
            raise TwitchHelixError("twitch api rate limited (429)")
        if response.status_code >= 400:
            raise TwitchHelixError(f"twitch helix {path} returned HTTP {response.status_code}")

        try:
            return dict(response.json())
        except ValueError as exc:
            raise TwitchHelixError(f"twitch helix {path} response malformed: {exc}") from exc

    async def get_user(self, login: str) -> dict[str, Any]:
        """`GET /users?login=` -- raises `TwitchHelixError` if the login doesn't resolve."""
        data = await self._get("/users", {"login": login.lower()})
        users = data.get("data") or []
        if not users:
            raise TwitchHelixError(f"twitch user '{login}' not found")
        return dict(users[0])

    async def get_channel(self, broadcaster_id: str) -> dict[str, Any] | None:
        """`GET /channels?broadcaster_id=` -- `None` if the broadcaster has no channel row."""
        data = await self._get("/channels", {"broadcaster_id": broadcaster_id})
        channels = data.get("data") or []
        return dict(channels[0]) if channels else None

    async def get_stream(self, user_id: str) -> dict[str, Any] | None:
        """`GET /streams?user_id=` -- `None` if the channel is currently offline."""
        data = await self._get("/streams", {"user_id": user_id})
        streams = data.get("data") or []
        return dict(streams[0]) if streams else None

    async def get_top_clip(self, broadcaster_id: str) -> dict[str, Any] | None:
        """`GET /clips?broadcaster_id=&first=1` -- `None` if the channel has no clips."""
        data = await self._get("/clips", {"broadcaster_id": broadcaster_id, "first": "1"})
        clips = data.get("data") or []
        return dict(clips[0]) if clips else None
