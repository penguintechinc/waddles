"""YouTube Data API v3 resolver -- real search/videos.list calls, real response parsing.

Credentials, in precedence order (issue #320 adds credential mode 0 ahead of the
pre-existing env-only modes 1/2, unchanged):

0. A community's own connected YouTube account, when `resolve()`/`search()` are called
   with both `db` and `community_id` -- `services.community_connections.
   get_decrypted_tokens()` fetches the community's stored OAuth material; a token that's
   `None`/expiring within 60s is refreshed via `services.oauth_providers.
   refresh_access_token()` and persisted via `store_refreshed_access_token()`. This is a
   *preference*, not a hard requirement: no connection, no refresh token on an expired
   token, or a failed refresh (`OAuthExchangeError`) all fall through to mode 1 below
   rather than raising -- logged at WARNING (community id + provider only, never token
   material). Cached in-process per community id, <=60s, including a "no usable
   connection" result, to avoid a DB decrypt on every call (`_resolve_community_auth()`).
1. `YOUTUBE_API_KEY` env var (or `~/.youtube.token` line 1 -- see `_load_api_key()`'s
   own docstring for the OAuth-client-id-is-unusable-here caveat) -- sent as the `key=`
   query param on every Data API v3 call.
2. `YOUTUBE_CLIENT_ID` + `YOUTUBE_CLIENT_SECRET` + `YOUTUBE_REFRESH_TOKEN` env vars, ALL
   three set -- exchanged for a short-lived OAuth access token via
   `POST https://oauth2.googleapis.com/token` (`grant_type=refresh_token`), cached
   in-process until `expires_in - 60s`, sent as `Authorization: Bearer <token>` (no
   `key=` param). A 401 from the Data API triggers one forced token refresh and one
   retry, for the case where the cached token was revoked server-side mid-lifetime.

None usable -> `ProviderUnavailable` naming exactly which env vars to set.
`youtube_credentials_configured()` is the presence-only, community-unaware env check (no
network I/O, no token refresh, signature unchanged by issue #320) that
`services.music_providers` uses for its bare-text YouTube-first default.
`youtube_credentials_source()` is the community-aware counterpart backing `!sr status`
(`services.music_status_service.check_youtube_health()`).

Never logs, prints, or otherwise surfaces the key/token/client-secret value itself --
only which credential mode was selected and OAuth token cache hit/miss/refresh.

Label enrichment (gh-313): after the primary `videos.list` call resolves a video,
`_fetch_labels()` makes a SECOND `videos.list?part=snippet,topicDetails` call (same
credential mode as the first) to populate `Track.labels` -- the union of the video's
category name, its `snippet.tags[]`, and its `topicDetails.topicCategories[]` leaf
names, lowercased and de-duplicated. Results are cached in-process per video id (24h
TTL, bounded size). This second call is decoupled from primary resolution on purpose:
any failure (network, quota, malformed response) degrades to `labels=()` plus a WARN
log, never raising -- a video the user can already see/play must never become
unplayable just because label enrichment failed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

import httpx

from services.community_connections import (
    SYSTEM,
    get_decrypted_tokens,
    store_refreshed_access_token,
)
from services.music_providers.errors import ProviderUnavailable, TrackNotFound
from services.music_providers.track import Track
from services.oauth_providers import OAuthExchangeError, refresh_access_token

logger = logging.getLogger(__name__)

_API_BASE = "https://www.googleapis.com/youtube/v3"
_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
_TIMEOUT_SECONDS = 10.0
_SEARCH_MAX_RESULTS = 10
_TOKEN_FILE = Path.home() / ".youtube.token"
#: Refresh this many seconds before the OAuth access token's reported expiry, to avoid
#: a request racing an about-to-expire token. Reused as the community-connection
#: near-expiry threshold (issue #320, `_fetch_community_auth()`).
_OAUTH_EXPIRY_SAFETY_MARGIN_SECONDS = 60

#: In-process community-YouTube-auth cache TTL (issue #320) -- avoids a DB decrypt (and
#: a possible refresh-token network round trip) on every `resolve()`/`search()` call for
#: the same community. See `_resolve_community_auth()`.
_COMMUNITY_AUTH_CACHE_TTL_SECONDS = 60.0

#: Highest-quality-first; `snippet.thumbnails` only guarantees `default`.
_THUMBNAIL_PRIORITY = ("maxres", "standard", "high", "medium", "default")

#: `contentDetails.duration` is ISO-8601 (`PT4M13S`); `P0D` for live streams.
_ISO8601_DURATION_RE = re.compile(
    r"^P(?:\d+D)?T?(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?$"
)

#: Standard (assignable) YouTube `videoCategories.list` ids -> lowercase display name.
#: Not exhaustive of every region-specific category Google has ever returned, but
#: covers the standard/global set; an unmapped id falls back to `category:<id>`
#: rather than silently dropping the signal (`_category_label()`).
_CATEGORY_NAMES: dict[str, str] = {
    "1": "film & animation",
    "2": "autos & vehicles",
    "10": "music",
    "15": "pets & animals",
    "17": "sports",
    "18": "short movies",
    "19": "travel & events",
    "20": "gaming",
    "21": "videoblogging",
    "22": "people & blogs",
    "23": "comedy",
    "24": "entertainment",
    "25": "news & politics",
    "26": "howto & style",
    "27": "education",
    "28": "science & technology",
    "29": "nonprofits & activism",
    "30": "movies",
    "31": "anime/animation",
    "32": "action/adventure",
    "33": "classics",
    "34": "comedy",
    "35": "documentary",
    "36": "drama",
    "37": "family",
    "38": "foreign",
    "39": "horror",
    "40": "sci-fi/fantasy",
    "41": "thriller",
    "42": "shorts",
    "43": "shows",
    "44": "trailers",
}

#: In-process `Track.labels` cache: video id -> `_CachedLabels`. 24h TTL, bounded
#: size -- see `_fetch_labels()`/`_labels_cache_get()`/`_labels_cache_set()`.
_LABELS_CACHE_TTL_SECONDS = 24 * 60 * 60
_LABELS_CACHE_MAX_SIZE = 512


@dataclass(slots=True)
class _ApiKeyAuth:
    """Data API v3 authenticated via the `key=` query param."""

    api_key: str


@dataclass(slots=True)
class _OAuthAuth:
    """Data API v3 authenticated via an OAuth refresh-token-derived bearer token."""

    client_id: str
    client_secret: str
    refresh_token: str


@dataclass(slots=True)
class _CachedOAuthToken:
    """In-process OAuth access-token cache entry; `expires_at` is a `time.monotonic()` deadline."""

    value: str
    expires_at: float


_oauth_token_cache: _CachedOAuthToken | None = None
_oauth_token_lock = asyncio.Lock()


@dataclass(slots=True)
class _CommunityAuth:
    """Data API v3 authenticated via a community-connected bearer access token (issue #320).

    Already-valid (or freshly-refreshed) -- unlike `_OAuthAuth`, this carries no
    client id/secret/refresh token; refresh happens once, up front, in
    `_fetch_community_auth()`, not lazily per-request.
    """

    access_token: str


@dataclass(slots=True)
class _CachedCommunityAuth:
    """In-process per-community auth cache entry (issue #320); `expires_at` is `time.monotonic()`.

    `value` is `None` when the community has no usable YouTube connection right
    now (absent, or expired with no refresh token, or a failed refresh) -- cached
    too, so a community without one doesn't re-hit the DB on every call within
    the TTL.
    """

    value: _CommunityAuth | None
    expires_at: float


#: Community id -> cached auth/no-auth result. See `_resolve_community_auth()`.
_community_auth_cache: dict[int, _CachedCommunityAuth] = {}
_community_auth_cache_lock = asyncio.Lock()


@dataclass(slots=True)
class _CachedLabels:
    """In-process `Track.labels` cache entry; `expires_at` is a `time.monotonic()` deadline."""

    value: tuple[str, ...]
    expires_at: float


#: Insertion-ordered (plain dict, Python 3.7+ semantics) so `_labels_cache_set()`
#: can evict the oldest entry in O(1) once `_LABELS_CACHE_MAX_SIZE` is reached.
_labels_cache: dict[str, _CachedLabels] = {}


def _load_api_key() -> str | None:
    """Resolve a usable Data API v3 key from env or `~/.youtube.token`; `None` if unusable."""
    env_key = os.getenv("YOUTUBE_API_KEY")
    if env_key:
        return env_key

    if not _TOKEN_FILE.exists():
        return None
    try:
        lines = _TOKEN_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if not lines:
        return None

    candidate = lines[0].strip()
    if not candidate or candidate.endswith(".apps.googleusercontent.com"):
        # OAuth client id, not a server API key -- unusable for this flow.
        return None
    return candidate


def _load_oauth_credentials() -> tuple[str, str, str] | None:
    """Resolve `(client_id, client_secret, refresh_token)` from env; `None` unless all three set."""
    client_id = os.getenv("YOUTUBE_CLIENT_ID")
    client_secret = os.getenv("YOUTUBE_CLIENT_SECRET")
    refresh_token = os.getenv("YOUTUBE_REFRESH_TOKEN")
    if client_id and client_secret and refresh_token:
        return client_id, client_secret, refresh_token
    return None


def youtube_credentials_configured() -> bool:
    """True if a usable Data API key or a full OAuth refresh-token trio is configured.

    Presence-only, community-unaware check (no network I/O, no token refresh,
    signature unchanged by issue #320) -- used by `services.music_providers`'s
    bare-text YouTube-first default policy to decide whether YouTube is even
    worth trying before Spotify.
    """
    return _load_api_key() is not None or _load_oauth_credentials() is not None


async def youtube_credentials_source(
    db: Any, community_id: int | None
) -> Literal["community", "env", "api_key", "none"]:
    """Which credential mode `resolve()`/`search()` would use for `community_id` right now.

    Mirrors `_resolve_auth()`'s own precedence (community connection, then
    `_resolve_auth_mode()`'s API-key-then-OAuth-trio env order) without making a
    Data API v3 call -- used by `services.music_status_service.
    check_youtube_health()` so `!sr status` can report which account is actually
    in use. `community_id=None` skips the community check entirely. Reuses the
    same <=60s community-auth cache `resolve()`/`search()` populate, so calling
    this immediately before/after a real resolve for the same community costs no
    extra DB round trip.
    """
    if community_id is not None and await _resolve_community_auth(db, community_id) is not None:
        return "community"
    if _load_api_key() is not None:
        return "api_key"
    if _load_oauth_credentials() is not None:
        return "env"
    return "none"


def _resolve_auth_mode() -> _ApiKeyAuth | _OAuthAuth:
    """Pick the credential mode per module docstring precedence: API key, then OAuth trio.

    Raises `ProviderUnavailable` naming exactly which env vars to set if neither is usable.
    """
    api_key = _load_api_key()
    if api_key is not None:
        logger.debug("youtube.auth mode=api_key")
        return _ApiKeyAuth(api_key=api_key)

    oauth_credentials = _load_oauth_credentials()
    if oauth_credentials is not None:
        logger.debug("youtube.auth mode=oauth")
        client_id, client_secret, refresh_token = oauth_credentials
        return _OAuthAuth(
            client_id=client_id, client_secret=client_secret, refresh_token=refresh_token
        )

    raise ProviderUnavailable(
        "youtube credentials not configured: set YOUTUBE_API_KEY or "
        "YOUTUBE_CLIENT_ID+YOUTUBE_CLIENT_SECRET+YOUTUBE_REFRESH_TOKEN"
    )


async def _fetch_community_auth(db: Any, community_id: int) -> _CommunityAuth | None:
    """Resolve `community_id`'s connected YouTube bearer token, refreshing if near expiry.

    Returns `None` (never raises) if there's no active connection, an expired
    token with no refresh token, or the refresh attempt itself fails
    (`OAuthExchangeError`) -- all fall through to credential mode 1 (module
    docstring) per this module's community-preference contract. A refresh
    failure is logged at WARNING with community id + provider only, never token
    material.
    """
    # `community_id` is already authorized by the (tenant-scoped) music route that built this
    # provider; there is no request tenant here, so SYSTEM resolves it from the community row.
    tokens = await get_decrypted_tokens(db, community_id, "youtube", tenant_id=SYSTEM)
    if tokens is None:
        return None

    now = datetime.now(UTC)
    still_fresh = tokens.expires_at is None or tokens.expires_at > now + timedelta(
        seconds=_OAUTH_EXPIRY_SAFETY_MARGIN_SECONDS
    )
    if still_fresh:
        return _CommunityAuth(access_token=tokens.access_token)

    if not tokens.refresh_token:
        logger.warning(
            "youtube.community_auth expired_no_refresh_token community_id=%s provider=youtube",
            community_id,
        )
        return None

    try:
        refreshed = await refresh_access_token("youtube", refresh_token=tokens.refresh_token)
    except OAuthExchangeError:
        logger.warning(
            "youtube.community_auth refresh_failed community_id=%s provider=youtube",
            community_id,
        )
        return None

    new_expires_at = (
        now + timedelta(seconds=refreshed.expires_in) if refreshed.expires_in is not None else None
    )
    await store_refreshed_access_token(
        db,
        community_id,
        "youtube",
        access_token=refreshed.access_token,
        expires_at=new_expires_at,
        tenant_id=SYSTEM,
    )
    return _CommunityAuth(access_token=refreshed.access_token)


async def _resolve_community_auth(db: Any, community_id: int) -> _CommunityAuth | None:
    """Cached (<=60s) wrapper around `_fetch_community_auth()`.

    Guarded by `_community_auth_cache_lock` (held across the fetch, mirroring
    `_get_oauth_access_token()`'s own `_oauth_token_lock` pattern) so concurrent
    calls for the same community don't each hit the DB / mint their own refresh.
    """
    async with _community_auth_cache_lock:
        cached = _community_auth_cache.get(community_id)
        if cached is not None and cached.expires_at > time.monotonic():
            logger.debug("youtube.community_auth cache=hit community_id=%s", community_id)
            return cached.value

        logger.debug("youtube.community_auth cache=miss community_id=%s", community_id)
        auth = await _fetch_community_auth(db, community_id)
        _community_auth_cache[community_id] = _CachedCommunityAuth(
            value=auth, expires_at=time.monotonic() + _COMMUNITY_AUTH_CACHE_TTL_SECONDS
        )
        return auth


async def _resolve_auth(
    db: Any | None, community_id: int | None
) -> _ApiKeyAuth | _OAuthAuth | _CommunityAuth:
    """Pick the credential mode per module docstring precedence: community, then env.

    `db`/`community_id` are optional; omitting either (both default `None`)
    preserves the env-only behavior every pre-issue-#320 caller of this module
    relies on -- e.g. `music_status_service`'s own presence probe. Raises
    `ProviderUnavailable` (same as `_resolve_auth_mode()`) if nothing usable is
    found at all.
    """
    if db is not None and community_id is not None:
        community_auth = await _resolve_community_auth(db, community_id)
        if community_auth is not None:
            logger.debug("youtube.auth mode=community community_id=%s", community_id)
            return community_auth

    return _resolve_auth_mode()


def _describe_oauth_error(response: httpx.Response) -> str:
    """Extract Google's OAuth `error`/`error_description` from a token-endpoint failure body."""
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


async def _get_oauth_access_token(
    client: httpx.AsyncClient, auth: _OAuthAuth, *, force_refresh: bool = False
) -> str:
    """Return a cached or freshly-refreshed OAuth access token for `auth`'s refresh token.

    Cached in-process until `expires_in - _OAUTH_EXPIRY_SAFETY_MARGIN_SECONDS`;
    `force_refresh` bypasses the cache (used after a 401 from the Data API, once).
    Guarded by `_oauth_token_lock` so concurrent resolve()/search() calls don't each
    mint their own token.
    """
    global _oauth_token_cache
    async with _oauth_token_lock:
        if (
            not force_refresh
            and _oauth_token_cache is not None
            and _oauth_token_cache.expires_at > time.monotonic()
        ):
            logger.debug("youtube.oauth token_cache=hit")
            return _oauth_token_cache.value

        logger.debug("youtube.oauth token_cache=%s", "refresh" if force_refresh else "miss")
        try:
            response = await client.post(
                _OAUTH_TOKEN_URL,
                data={
                    "client_id": auth.client_id,
                    "client_secret": auth.client_secret,
                    "refresh_token": auth.refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(
                f"youtube oauth refresh failed: {type(exc).__name__}"
            ) from exc

        if response.status_code != 200:
            raise ProviderUnavailable(
                f"youtube oauth refresh failed: HTTP {response.status_code} "
                f"{_describe_oauth_error(response)}"
            ) from None

        payload: Any = response.json()
        token = payload.get("access_token") if isinstance(payload, dict) else None
        expires_in = payload.get("expires_in", 3600) if isinstance(payload, dict) else 3600
        if not token:
            raise ProviderUnavailable("youtube oauth refresh failed: response missing access_token")

        _oauth_token_cache = _CachedOAuthToken(
            value=str(token),
            expires_at=time.monotonic()
            + max(0, int(expires_in) - _OAUTH_EXPIRY_SAFETY_MARGIN_SECONDS),
        )
        return _oauth_token_cache.value


def _extract_video_id(url: str) -> str | None:
    """Pull a video id out of any of youtube.com/youtu.be's URL shapes."""
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()

    if host == "youtu.be":
        video_id = parsed.path.lstrip("/")
        return video_id or None

    if host == "youtube.com" or host.endswith(".youtube.com"):
        query = parse_qs(parsed.query)
        if "v" in query and query["v"]:
            return query["v"][0]
        for prefix in ("/embed/", "/shorts/", "/live/"):
            if parsed.path.startswith(prefix):
                video_id = parsed.path[len(prefix) :].split("/")[0]
                return video_id or None

    return None


def _parse_iso8601_duration_ms(duration: str) -> int:
    """Convert `contentDetails.duration` (e.g. `PT4M13S`) to milliseconds."""
    match = _ISO8601_DURATION_RE.match(duration)
    if not match:
        return 0
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    return (hours * 3600 + minutes * 60 + seconds) * 1000


def _best_thumbnail(thumbnails: dict[str, Any]) -> str | None:
    """Pick the highest-resolution thumbnail available, `None` if the dict is empty."""
    for key in _THUMBNAIL_PRIORITY:
        thumb = thumbnails.get(key)
        if isinstance(thumb, dict) and thumb.get("url"):
            return str(thumb["url"])
    return None


def _track_from_video_item(item: dict[str, Any]) -> Track:
    """Map one `videos.list` item into a normalized `Track`."""
    snippet = item.get("snippet") or {}
    content_details = item.get("contentDetails") or {}
    video_id = str(item.get("id") or "")
    return Track(
        provider="youtube",
        external_id=video_id,
        title=str(snippet.get("title") or ""),
        artist=str(snippet.get("channelTitle") or ""),
        duration_ms=_parse_iso8601_duration_ms(str(content_details.get("duration") or "")),
        artwork_url=_best_thumbnail(snippet.get("thumbnails") or {}),
        url=f"https://www.youtube.com/watch?v={video_id}",
    )


def _category_label(category_id: Any) -> str | None:
    """Map `snippet.categoryId` to its lowercase display name, `category:<id>` if unmapped."""
    cid = str(category_id) if category_id else ""
    if not cid:
        return None
    return _CATEGORY_NAMES.get(cid, f"category:{cid}")


def _topic_leaf_name(topic_url: str) -> str | None:
    """Last path segment of a `topicDetails.topicCategories[]` Wikipedia URL, underscores->spaces.

    E.g. `https://en.wikipedia.org/wiki/Pop_music` -> `Pop music`.
    """
    parsed = urlparse(topic_url)
    segment = parsed.path.rsplit("/", 1)[-1]
    if not segment:
        return None
    return segment.replace("_", " ")


def _extract_labels(item: dict[str, Any]) -> tuple[str, ...]:
    """Build one video's `Track.labels`: category name + tags + topic leaf names.

    Lowercased, de-duplicated (first-seen order preserved). `item` is a
    `videos.list?part=snippet,topicDetails` item -- see `_fetch_labels()`.
    """
    snippet = item.get("snippet") or {}
    topic_details = item.get("topicDetails") or {}

    raw_labels: list[str] = []

    category_label = _category_label(snippet.get("categoryId"))
    if category_label:
        raw_labels.append(category_label)

    for tag in snippet.get("tags") or []:
        if isinstance(tag, str) and tag.strip():
            raw_labels.append(tag)

    for topic_url in topic_details.get("topicCategories") or []:
        if not isinstance(topic_url, str):
            continue
        leaf = _topic_leaf_name(topic_url)
        if leaf:
            raw_labels.append(leaf)

    deduped: dict[str, None] = {}
    for label in raw_labels:
        lowered = label.strip().lower()
        if lowered:
            deduped.setdefault(lowered, None)
    return tuple(deduped.keys())


def _labels_cache_get(video_id: str) -> tuple[str, ...] | None:
    """Return a still-fresh cached label set for `video_id`, `None` on miss/expiry."""
    entry = _labels_cache.get(video_id)
    if entry is None:
        return None
    if entry.expires_at <= time.monotonic():
        del _labels_cache[video_id]
        return None
    return entry.value


def _labels_cache_set(video_id: str, labels: tuple[str, ...]) -> None:
    """Cache `labels` for `video_id`, evicting the oldest entry if at capacity."""
    if video_id in _labels_cache:
        del _labels_cache[video_id]
    elif len(_labels_cache) >= _LABELS_CACHE_MAX_SIZE:
        oldest_id = next(iter(_labels_cache))
        del _labels_cache[oldest_id]
    _labels_cache[video_id] = _CachedLabels(
        value=labels, expires_at=time.monotonic() + _LABELS_CACHE_TTL_SECONDS
    )


async def _fetch_labels(
    client: httpx.AsyncClient, auth: _ApiKeyAuth | _OAuthAuth | _CommunityAuth, video_id: str
) -> tuple[str, ...]:
    """Fetch `Track.labels` for one video id via a second `videos.list` call, cached 24h.

    Never raises -- any failure (network, quota, malformed response) degrades to
    `()` plus a WARN log, per this module's own docstring: label enrichment must
    never block a video that already resolved successfully.
    """
    cached = _labels_cache_get(video_id)
    if cached is not None:
        logger.debug("youtube.labels cache=hit video_id=%s", video_id)
        return cached

    try:
        data = await _data_api_get(
            client, auth, "/videos", {"part": "snippet,topicDetails", "id": video_id}
        )
        items = data.get("items") or []
        labels = _extract_labels(items[0]) if items else ()
    except Exception as exc:  # noqa: BLE001 - label enrichment must never block resolution
        logger.warning("youtube.labels fetch_failed video_id=%s reason=%s", video_id, exc)
        return ()

    _labels_cache_set(video_id, labels)
    logger.debug("youtube.labels cache=miss video_id=%s label_count=%d", video_id, len(labels))
    return labels


def _describe_403_reason(response: httpx.Response) -> str:
    """Extract Google's error `reason` (falling back to `status`/`message`) from a 403 body."""
    try:
        payload: Any = response.json()
    except ValueError:
        return "forbidden"
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return "forbidden"

    errors = error.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        reason = errors[0].get("reason")
        if isinstance(reason, str) and reason:
            return reason

    for key in ("status", "message"):
        value = error.get(key)
        if isinstance(value, str) and value:
            return value

    return "forbidden"


def _parse_data_api_response(response: httpx.Response) -> dict[str, Any]:
    """Map a Data API v3 response to its JSON body, or raise `ProviderUnavailable` on failure."""
    if response.status_code == 403:
        # Quota exhaustion / API-not-enabled / access-denied -- surface Google's own
        # `reason` (e.g. quotaExceeded, accessNotConfigured, forbidden) for `!sr status`.
        raise ProviderUnavailable(
            f"youtube quota/access error: {_describe_403_reason(response)}"
        ) from None
    if response.status_code in (400, 401, 429) or response.status_code >= 500:
        # Invalid/revoked key or token, rate limited, or upstream outage -- all mean
        # "youtube isn't usable right now", same as absent creds.
        raise ProviderUnavailable("youtube") from None
    response.raise_for_status()

    data: Any = response.json()
    return data if isinstance(data, dict) else {}


async def _send_data_api_request(
    client: httpx.AsyncClient, path: str, params: dict[str, Any], headers: dict[str, str]
) -> httpx.Response:
    """GET one Data API v3 endpoint; maps network failures to `ProviderUnavailable`."""
    try:
        return await client.get(f"{_API_BASE}{path}", params=params, headers=headers)
    except httpx.HTTPError as exc:
        raise ProviderUnavailable("youtube") from exc


async def _data_api_get(
    client: httpx.AsyncClient,
    auth: _ApiKeyAuth | _OAuthAuth | _CommunityAuth,
    path: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """GET one Data API v3 endpoint under `auth`; retries once on a 401 in OAuth mode.

    API-key mode sends `key=` as a query param, never retries (a bad key doesn't get
    better on retry). OAuth and community modes send `Authorization: Bearer <token>`;
    OAuth mode's 401 forces one token refresh (bypassing the cache) and one retry,
    covering a token revoked server-side mid-lifetime -- community mode already
    refreshed up front (`_fetch_community_auth()`) so a 401 there means the
    community's own token is bad right now, not something this call can fix.
    """
    request_params = dict(params)
    headers: dict[str, str] = {}

    if isinstance(auth, _ApiKeyAuth):
        request_params["key"] = auth.api_key
    elif isinstance(auth, _CommunityAuth):
        headers["Authorization"] = f"Bearer {auth.access_token}"
    else:
        headers["Authorization"] = f"Bearer {await _get_oauth_access_token(client, auth)}"

    response = await _send_data_api_request(client, path, request_params, headers)

    if isinstance(auth, _OAuthAuth) and response.status_code == 401:
        logger.debug("youtube.data_api retry=1 reason=401_refresh_and_retry")
        token = await _get_oauth_access_token(client, auth, force_refresh=True)
        headers["Authorization"] = f"Bearer {token}"
        response = await _send_data_api_request(client, path, request_params, headers)

    return _parse_data_api_response(response)


async def resolve(url: str, *, db: Any = None, community_id: int | None = None) -> Track:
    """Resolve a YouTube video URL to a `Track` via `videos.list`.

    `db`/`community_id` (issue #320): when both given, the community's own
    connected YouTube account is preferred over this module's env credentials
    -- see the module docstring's credential-precedence list and
    `_resolve_auth()`. Omitting either preserves the env-only behavior every
    pre-issue-#320 caller relies on.

    `Track.labels` is populated by a second, decoupled `videos.list` call
    (`_fetch_labels()`, same resolved `auth`) -- see this module's own
    docstring for why a failure there never raises out of `resolve()`.
    """
    auth = await _resolve_auth(db, community_id)

    video_id = _extract_video_id(url)
    if video_id is None:
        raise TrackNotFound(url)

    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        data = await _data_api_get(
            client, auth, "/videos", {"part": "snippet,contentDetails", "id": video_id}
        )

        items = data.get("items") or []
        if not items:
            raise TrackNotFound(url)
        track = _track_from_video_item(items[0])
        track.labels = await _fetch_labels(client, auth, video_id)

    return track


async def search(query: str, *, db: Any = None, community_id: int | None = None) -> list[Track]:
    """Search YouTube via `search.list`, then hydrate durations via `videos.list`.

    `db`/`community_id` (issue #320): same community-first credential
    precedence as `resolve()` -- see that function's own docstring.

    `Track.labels` for each result is populated the same way `resolve()` does --
    one additional `videos.list` call per result id (cached, see `_fetch_labels()`).
    """
    auth = await _resolve_auth(db, community_id)

    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        search_data = await _data_api_get(
            client,
            auth,
            "/search",
            {"part": "snippet", "q": query, "type": "video", "maxResults": _SEARCH_MAX_RESULTS},
        )
        video_ids = [
            str(item["id"]["videoId"])
            for item in search_data.get("items") or []
            if isinstance(item.get("id"), dict) and item["id"].get("videoId")
        ]
        if not video_ids:
            raise TrackNotFound(query)

        videos_data = await _data_api_get(
            client, auth, "/videos", {"part": "snippet,contentDetails", "id": ",".join(video_ids)}
        )

        items = videos_data.get("items") or []
        if not items:
            raise TrackNotFound(query)
        tracks = [_track_from_video_item(item) for item in items]
        for track in tracks:
            track.labels = await _fetch_labels(client, auth, track.external_id)

    return tracks
