"""Per-community OAuth "Connections" token resolver (issue #320, chunk C5).

Resolves which OAuth credentials a service should use for a given
`(community_id, provider)` pair: the community's own connected token,
fetched from hub-api's internal Connections API, falling back to the
service's static process-wide environment credentials when the community
has none connected (or hub-api/the feature is unreachable/off). Every
platform connector currently reads its own `<PROVIDER>_CLIENT_ID`/
`_CLIENT_SECRET`/`_REFRESH_TOKEN`-shaped env vars directly (see
`ENV_KEYS`) -- this module is the seam a bundle switches through to make
that a per-community override instead of one process-wide credential set,
without duplicating the HTTP/caching/fallback logic in every consumer.

Internal route contract (`hub_api/blueprints/v1/...`, built concurrently
with this module -- mocked in this module's own tests, never called for
real here):
`GET {hub_api_url}/api/v1/internal/communities/{community_id}/connections/{provider}/token`
(Bearer machine JWT, see Auth below)
-> 200 `{"status": "success", "data": {"access_token": str, "refresh_token": str|null,
"expires_at": iso8601|null, "scopes": [str]}, "meta": {...}}` (flat payload also accepted);
404 when the community hasn't connected
that provider; 502 `{"error": "refresh_failed"}`; 503 when the feature is
off. Every other non-2xx and any transport failure (timeout, connection
refused, malformed JSON) degrades to `None`, logged at WARNING with
`provider`/`community_id`/`status` only -- `fetch_community_tokens` never
raises into a caller's request-handling path.

Auth (HIGH-2 -- NO static shared secret): `Authorization: Bearer <community-bound machine
JWT>`. `_mint_community_jwt` bootstraps one against hub-api's `POST /internal/service-token`
(`SERVICE_JWT_TOKEN_ENDPOINT`, default `{hub_api_url}/internal/service-token`) with the pod's own
projected ServiceAccount token (`SERVICE_JWT_SA_TOKEN_PATH`) -- the Python equivalent of
`service_auth::MachineJwtClient` -- requesting scope `connections:resolve` bound to this
`community_id` (<=300s lifetime), and caches it per community until 30s before expiry. A token
minted for community A is refused by hub-api for community B. When the bootstrap fails (no SA
token file, hub-api rejects) the fetch degrades to `None` like every other failure here.

`ENV_KEYS`' `client_id_env`/`client_secret_env` columns match
`hub_api/services/oauth_providers.py::PROVIDERS`' own `client_id_env`/
`client_secret_env` exactly (that module owns the real OAuth
authorize/exchange flow driving the Connections UI; this module only
needs the env var *names* to build the static env-fallback path, never
the client id/secret values themselves -- `CommunityTokens` carries no
client id/secret fields). `refresh_token_env` matches each connector's
own existing static-credential env var where one already exists:
`YOUTUBE_REFRESH_TOKEN` (`core/svc_action/builtin_handlers/youtube_send_action.py`),
`KICK_ACCESS_TOKEN` (`kick_send_action.py`'s `access_token_ref` default),
`DISCORD_BOT_TOKEN`/`SLACK_BOT_TOKEN` (the one static bot credential
those platforms already use in place of a per-user OAuth refresh token).
`TWITCH_OAUTH_TOKEN`/`SPOTIFY_REFRESH_TOKEN` are this module's own naming
choice for those two providers -- no existing static single-token env
var precedent for either (Twitch's existing paths use either
`TWITCH_CLIENT_ID`/`_SECRET` app-credential `client_credentials` flow,
`core/svc_action/services/twitch_helix.py`, or a `_REF` indirection,
`TWITCH_BOT_TOKEN_REF`; Spotify has none at all in this repo today),
named to match `YOUTUBE_REFRESH_TOKEN`'s pattern for consistency.

Caching: `resolve_community_tokens` keeps a 60s in-process TTL cache
keyed `(community_id, provider)` for a real result, but only 15s for a
`None` outcome (neither the community nor the env fallback produced a
token) -- so a newly-connected community, or a freshly-set env var, is
picked up quickly rather than hidden behind the full 60s window.
`env_tokens`/`fetch_community_tokens` are NOT cached themselves --
caching lives only in the orchestrating `resolve_community_tokens`, so a
caller needing an uncached read (a test, or one that just called
`invalidate()`) can call either directly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import httpx

logger = logging.getLogger(__name__)

#: provider -> (client_id_env, client_secret_env, refresh_token_env). See
#: module docstring for where each column's naming comes from.
ENV_KEYS: dict[str, tuple[str, str, str]] = {
    "youtube": ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"),
    "spotify": ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SPOTIFY_REFRESH_TOKEN"),
    "twitch": ("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET", "TWITCH_OAUTH_TOKEN"),
    "discord": ("DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "DISCORD_BOT_TOKEN"),
    "kick": ("KICK_CLIENT_ID", "KICK_CLIENT_SECRET", "KICK_ACCESS_TOKEN"),
    "slack": ("SLACK_CLIENT_ID", "SLACK_CLIENT_SECRET", "SLACK_BOT_TOKEN"),
}

#: Every service in this repo that already calls hub-api (svc-process,
#: svc-action, svc-ingest `config.py` alike) defaults `HUB_API_URL` to
#: this same value -- the fallback chain in `_resolve_hub_api_url` reuses
#: it so a deployment doesn't need a redundant `HUB_API_INTERNAL_URL` env
#: var/Helm value just to point at the hub-api Quart app it already talks
#: to on `/api/v1/...`.
_DEFAULT_HUB_API_URL = "http://hub-api:8204"
_DEFAULT_TIMEOUT_S = 3.0
#: URL path template, not a secret value.
_TOKEN_PATH = "/api/v1/internal/communities/{community_id}/connections/{provider}/token"  # noqa: S105
#: Machine-JWT bootstrap: path template (relative to the hub-api base URL), scope, and the
#: projected-ServiceAccount-token file the pod already carries for `service_auth`.
_MINT_PATH = "/internal/service-token"  # noqa: S105 -- URL path, not a secret value
_MINT_SCOPE = "connections:resolve"
_DEFAULT_SA_TOKEN_PATH = "/var/run/secrets/waddlebot/service-jwt/service-jwt-bootstrap-token"  # noqa: S105 -- path
#: Re-mint this many seconds before the JWT's own expiry.
_JWT_SKEW_S = 30.0

_POSITIVE_CACHE_TTL_S = 60.0
_NEGATIVE_CACHE_TTL_S = 15.0


@dataclass(slots=True)
class CommunityTokens:
    """One resolved credential set for a `(community_id, provider)` pair.

    `source="community"` -- `access_token` is ready to use directly as a
    Bearer credential (hub-api already performed/owns any refresh).
    `source="env"` -- `access_token` is always `None`; `refresh_token`
    carries whichever static credential `ENV_KEYS[provider][2]` names (a
    genuine OAuth refresh token for some providers, a directly-usable
    bot/access token for others -- see module docstring), and the caller
    performs whatever exchange/usage it already did before this module
    existed.
    """

    access_token: str | None
    refresh_token: str | None
    expires_at: datetime | None
    scopes: list[str]
    source: Literal["community", "env"]


def env_tokens(provider: str) -> CommunityTokens | None:
    """Build a `CommunityTokens` from `ENV_KEYS[provider]`'s refresh/access-token env var.

    `access_token` is always `None` here -- only `refresh_token` (the
    third `ENV_KEYS` column) is populated; see `CommunityTokens`'
    docstring for why. Returns `None` if `provider` is unknown to
    `ENV_KEYS`, or if its refresh-token env var is unset/empty. Never
    raises.
    """
    keys = ENV_KEYS.get(provider)
    if keys is None:
        logger.debug("community_credentials.env_tokens_unknown_provider provider=%s", provider)
        return None

    _client_id_env, _client_secret_env, refresh_token_env = keys
    value = os.environ.get(refresh_token_env)
    if not value:
        logger.debug(
            "community_credentials.env_tokens_unset provider=%s env=%s",
            provider,
            refresh_token_env,
        )
        return None

    logger.debug(
        "community_credentials.env_tokens_hit provider=%s env=%s length=%d",
        provider,
        refresh_token_env,
        len(value),
    )
    return CommunityTokens(
        access_token=None, refresh_token=value, expires_at=None, scopes=[], source="env"
    )


def _resolve_hub_api_url(hub_api_url: str | None) -> str:
    """`hub_api_url` if given, else `HUB_API_INTERNAL_URL`, else `HUB_API_URL`, else the default."""
    return (
        hub_api_url
        or os.environ.get("HUB_API_INTERNAL_URL")
        or os.environ.get("HUB_API_URL")
        or _DEFAULT_HUB_API_URL
    )


def _parse_expires_at(raw: object, *, provider: str, community_id: int) -> datetime | None:
    """Parse an ISO-8601 `expires_at` string; `None` for `null`/absent/malformed (logged DEBUG)."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        logger.debug(
            "community_credentials.fetch_malformed_expires_at provider=%s community_id=%s value=%r",
            provider,
            community_id,
            raw,
        )
        return None


#: `community_id -> (monotonic_expiry, jwt)`. Process-wide, same convention as `_cache` below.
_jwt_cache: dict[int, tuple[float, str]] = {}


def _mint_endpoint(base_url: str) -> str:
    """`SERVICE_JWT_TOKEN_ENDPOINT` if set, else `{base_url}/internal/service-token`."""
    return os.environ.get("SERVICE_JWT_TOKEN_ENDPOINT") or f"{base_url}{_MINT_PATH}"


async def _mint_community_jwt(
    client: httpx.AsyncClient, community_id: int, base_url: str
) -> str | None:
    """Return a community-bound `connections:resolve` machine JWT, minting one if needed.

    Cached per community until `_JWT_SKEW_S` before expiry. Returns `None` (logged WARNING, ids
    only -- never a token) when the projected SA token is unreadable or hub-api refuses the
    bootstrap; an `httpx.HTTPError` from the mint call propagates to the caller's handler.
    """
    cached = _jwt_cache.get(community_id)
    if cached is not None and time.monotonic() < cached[0]:
        return cached[1]

    sa_path = os.environ.get("SERVICE_JWT_SA_TOKEN_PATH") or _DEFAULT_SA_TOKEN_PATH
    try:
        sa_token = (await asyncio.to_thread(_read_text, sa_path)).strip()
    except OSError as exc:
        logger.warning(
            "community_credentials.mint_no_sa_token community_id=%s error=%s",
            community_id,
            type(exc).__name__,
        )
        return None
    if not sa_token:
        logger.warning("community_credentials.mint_empty_sa_token community_id=%s", community_id)
        return None

    response = await client.post(
        _mint_endpoint(base_url),
        headers={"Authorization": f"Bearer {sa_token}"},
        json={"scope": _MINT_SCOPE, "community_id": community_id},
    )
    if response.status_code != 200:
        logger.warning(
            "community_credentials.mint_rejected community_id=%s status=%s",
            community_id,
            response.status_code,
        )
        return None
    try:
        body = response.json()
        token = body["token"]
        expires_in = float(body["expires_in"])
    except (ValueError, KeyError, TypeError):
        logger.warning("community_credentials.mint_malformed community_id=%s", community_id)
        return None
    if not isinstance(token, str) or not token or expires_in <= 0:
        logger.warning("community_credentials.mint_malformed community_id=%s", community_id)
        return None
    _jwt_cache[community_id] = (time.monotonic() + max(expires_in - _JWT_SKEW_S, 0.0), token)
    return token


def _read_text(path: str) -> str:
    """Read a small text file (run via `asyncio.to_thread` so the loop never blocks on disk)."""
    with open(path, encoding="utf-8") as handle:
        return handle.read()


async def fetch_community_tokens(
    community_id: int,
    provider: str,
    *,
    hub_api_url: str | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> CommunityTokens | None:
    """One uncached GET against hub-api's internal Connections token endpoint.

    See module docstring for the route/response contract. Never raises --
    every failure mode (network error, non-2xx, malformed JSON, an
    unexpected field type) is caught, logged at WARNING (`provider`/
    `community_id`/`status` only -- never a token value), and degrades to
    `None`. A 404 (not connected) is the one expected "no token" outcome
    and logs at DEBUG instead of WARNING.
    """
    base_url = _resolve_hub_api_url(hub_api_url).rstrip("/")
    path = _TOKEN_PATH.format(community_id=community_id, provider=provider)
    url = f"{base_url}{path}"

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            bearer = await _mint_community_jwt(client, community_id, base_url)
            if bearer is None:
                return None
            response = await client.get(url, headers={"Authorization": f"Bearer {bearer}"})
    except httpx.HTTPError as exc:
        logger.warning(
            "community_credentials.fetch_unreachable provider=%s community_id=%s error=%s",
            provider,
            community_id,
            type(exc).__name__,
        )
        return None

    if response.status_code in (401, 403):
        _jwt_cache.pop(community_id, None)  # a rejected JWT is never reused

    if response.status_code == 404:
        logger.debug(
            "community_credentials.fetch_not_connected provider=%s community_id=%s",
            provider,
            community_id,
        )
        return None

    if response.status_code != 200:
        logger.warning(
            "community_credentials.fetch_rejected provider=%s community_id=%s status=%s",
            provider,
            community_id,
            response.status_code,
        )
        return None

    try:
        body = response.json()
    except ValueError as exc:
        logger.warning(
            "community_credentials.fetch_malformed_json provider=%s community_id=%s error=%s",
            provider,
            community_id,
            exc,
        )
        return None

    if not isinstance(body, dict):
        logger.warning(
            "community_credentials.fetch_malformed_body provider=%s community_id=%s",
            provider,
            community_id,
        )
        return None

    # hub-api wraps the payload in `{"status","data":{...},"meta"}` (`_envelope`); the flat shape
    # is still accepted. (Found by the live round-trip test -- the mocked tests used the flat one.)
    inner = body.get("data")
    if isinstance(inner, dict):
        body = inner

    access_token = body.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        logger.warning(
            "community_credentials.fetch_missing_access_token provider=%s community_id=%s",
            provider,
            community_id,
        )
        return None

    refresh_token_raw = body.get("refresh_token")
    refresh_token = (
        refresh_token_raw if isinstance(refresh_token_raw, str) and refresh_token_raw else None
    )

    expires_at = _parse_expires_at(
        body.get("expires_at"), provider=provider, community_id=community_id
    )

    scopes_raw = body.get("scopes")
    scopes = [str(item) for item in scopes_raw] if isinstance(scopes_raw, list) else []

    logger.debug(
        "community_credentials.fetch_hit provider=%s community_id=%s access_token_length=%d "
        "scope_count=%d",
        provider,
        community_id,
        len(access_token),
        len(scopes),
    )
    return CommunityTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        scopes=scopes,
        source="community",
    )


#: `(community_id, provider) -> (monotonic_expiry, tokens_or_none)`. Module-level, process-wide
#: -- same bespoke-dict-cache convention `core/svc_process/services/community_resolver.py`'s own
#: `_last_warned_at` uses (no shared TTL-cache helper exists at this dependency tier;
#: `flask_core.cache.CacheManager` is Redis-backed -- the wrong tool for a sub-millisecond-cheap
#: in-process guard, and this library stays dependency-free of `flask_core`, see `types.py`).
_cache: dict[tuple[int | None, str], tuple[float, CommunityTokens | None]] = {}


async def resolve_community_tokens(
    community_id: int | None, provider: str
) -> CommunityTokens | None:
    """Resolve credentials for `(community_id, provider)`: community connection, then env fallback.

    Order: `fetch_community_tokens(community_id, provider)` (skipped
    entirely when `community_id is None`) -> `env_tokens(provider)`. The
    outcome -- including `None` -- is cached in-process, keyed
    `(community_id, provider)`, for 60s (a real token) or 15s (`None`) --
    see module docstring. Never raises -- both of its own calls already
    degrade to `None` on any failure.
    """
    cache_key = (community_id, provider)
    cached = _cache.get(cache_key)
    if cached is not None:
        expires_at, cached_value = cached
        if time.monotonic() < expires_at:
            return cached_value

    tokens: CommunityTokens | None = None
    if community_id is not None:
        tokens = await fetch_community_tokens(community_id, provider)
    if tokens is None:
        tokens = env_tokens(provider)

    ttl = _POSITIVE_CACHE_TTL_S if tokens is not None else _NEGATIVE_CACHE_TTL_S
    _cache[cache_key] = (time.monotonic() + ttl, tokens)
    return tokens


def invalidate(community_id: int | None, provider: str) -> None:
    """Drop any cached `resolve_community_tokens` entry for `(community_id, provider)`."""
    _cache.pop((community_id, provider), None)


def reset_cache_for_tests() -> None:
    """Clear the entire cache. Test isolation only, never called by production code."""
    _cache.clear()
    _jwt_cache.clear()
