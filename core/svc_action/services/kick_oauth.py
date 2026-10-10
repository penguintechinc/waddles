"""Kick OAuth2 helper -- a stored access token, or a client-credentials app token, in-process cache.

Small, deliberately self-contained helper mirroring `services/
youtube_oauth.py`'s own shape (module-level in-process cache, `expires_in
- 60s` safety margin, a single forced-refresh-on-401 retry point for the
caller) -- adapted to Kick's own OAuth2 credential shape, which is
simpler: no per-request scope check (Kick's own send-message endpoint has
no documented equivalent of YouTube's `tokeninfo` scope-lookup API), and
two credential MODES rather than one grant type, in precedence order
(mirrors `receivers/youtube_live_poll.py::_resolve_auth_mode`'s own
"explicit credential first, else exchange for one" precedent):

1. A STORED access token (`stored_access_token` -- resolved by the caller
   from `KICK_ACCESS_TOKEN`/`config["access_token_ref"]`, see `builtin_handlers/
   kick_send_action.py`) -- already a live token (issued via Kick's
   Authorization Code + PKCE flow, provisioned externally; that flow is
   out of scope here), used as-is. This module can never refresh a token
   it didn't mint -- `get_access_token(force_refresh=True, ...)` in this
   mode is a documented no-op, it simply returns the same stored value
   again (see `builtin_handlers/kick_send_action.py`'s own docstring for how its
   single retry-on-401 still makes sense given this).
2. `client_id`/`client_secret` -- exchanged for an app access token via
   Kick's own OAuth2 `client_credentials` grant (`POST https://id.
   kick.com/oauth/token`, Kick's public Developer API token endpoint),
   cached in-process per `(client_id, client_secret)` pair until
   `expires_in - 60s`, same safety margin `services/youtube_oauth.py`
   uses.

Never logs, prints, or otherwise surfaces the client secret or access
token value itself -- only cache hit/miss/refresh and which error class a
refresh failure falls into, matching `services/youtube_oauth.py`'s own
logging discipline.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

#: Kick's own OAuth2 client-credentials token endpoint (public Developer API).
_OAUTH_TOKEN_URL = "https://id.kick.com/oauth/token"  # noqa: S105 -- URL, not a secret

#: Refresh this many seconds before the OAuth access token's reported
#: expiry, to avoid a request racing an about-to-expire token -- same
#: margin as `services/youtube_oauth.py`'s own safety margin.
_EXPIRY_SAFETY_MARGIN_SECONDS = 60


class KickOAuthError(Exception):
    """Raised when neither credential mode is usable, or a client-credentials exchange fails."""


@dataclass(slots=True)
class _CachedToken:
    """In-process OAuth access-token cache entry; `expires_at` is a `time.monotonic()` deadline."""

    value: str
    expires_at: float


@dataclass(slots=True, frozen=True)
class _CacheKey:
    """Cache key for a client-credentials pair's access-token cache entry."""

    client_id: str
    client_secret: str


_token_cache: dict[_CacheKey, _CachedToken] = {}
_token_cache_lock = asyncio.Lock()


def _describe_oauth_error(response: httpx.Response) -> str:
    """Extract Kick's OAuth `error`/`error_description` from a token-endpoint failure body."""
    try:
        payload: Any = response.json()
    except ValueError:
        return "unknown error"
    if not isinstance(payload, dict):
        return "unknown error"

    error = payload.get("error")
    description = payload.get("error_description")
    if error and description:
        return f"{error}: {description}"
    if error:
        return str(error)
    return "unknown error"


async def get_access_token(
    http_client: httpx.AsyncClient,
    *,
    stored_access_token: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    force_refresh: bool = False,
) -> str:
    """Return a usable Kick bearer token -- `stored_access_token` first, else client-credentials.

    `stored_access_token`, if non-empty, is ALWAYS returned as-is
    (`force_refresh` has no effect in this mode -- see module docstring).
    Otherwise `client_id`/`client_secret` are exchanged for a cached app
    access token; `force_refresh` bypasses that cache (the caller's single
    retry after an observed 401 -- `builtin_handlers/kick_send_action.py`'s own
    single retry point).

    Raises `KickOAuthError` if neither mode is usable (no stored token AND
    `client_id`/`client_secret` incomplete), or the client-credentials
    exchange itself fails (network error, non-200 response, or a 200
    response missing `access_token`).
    """
    if stored_access_token:
        logger.debug("kick_oauth.mode=stored_access_token")
        return stored_access_token

    if not client_id or not client_secret:
        raise KickOAuthError(
            "kick oauth needs either a stored access token or both client_id/client_secret"
        )

    cache_key = _CacheKey(client_id=client_id, client_secret=client_secret)
    async with _token_cache_lock:
        cached = _token_cache.get(cache_key)
        if not force_refresh and cached is not None and cached.expires_at > time.monotonic():
            logger.debug("kick_oauth.token_cache=hit")
            return cached.value

        logger.debug("kick_oauth.token_cache=%s", "refresh" if force_refresh else "miss")
        try:
            response = await http_client.post(
                _OAUTH_TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "client_credentials",
                },
            )
        except httpx.HTTPError as exc:
            raise KickOAuthError(
                f"kick oauth client-credentials exchange failed: {type(exc).__name__}"
            ) from exc

        if response.status_code != 200:
            raise KickOAuthError(
                f"kick oauth client-credentials exchange failed: HTTP {response.status_code} "
                f"{_describe_oauth_error(response)}"
            )

        payload: Any = response.json()
        token = payload.get("access_token") if isinstance(payload, dict) else None
        expires_in = payload.get("expires_in", 3600) if isinstance(payload, dict) else 3600
        if not token:
            raise KickOAuthError(
                "kick oauth client-credentials exchange failed: response missing access_token"
            )

        new_token = _CachedToken(
            value=str(token),
            expires_at=time.monotonic() + max(0, int(expires_in) - _EXPIRY_SAFETY_MARGIN_SECONDS),
        )
        _token_cache[cache_key] = new_token
        return new_token.value
