"""YouTubeLivePollReceiver -- a `waddle_transports.Transport` for inbound YouTube Live Chat polling.

Real YouTube Data API v3 calls (not a stub): `search.list?eventType=live`
to find a channel's active broadcast, `videos.list?part=
liveStreamingDetails` to resolve that broadcast's `activeLiveChatId`, then
`liveChat/messages.list` polled on a loop honoring the API's own
`pollingIntervalMillis`/`nextPageToken` -- the exact endpoint sequence
`trigger/receiver/youtube_live_module/services/{youtube_client.py,
chat_poller.py}` (the legacy module this receiver replaces) used, adapted
onto the `waddle_transports.Transport` ABC (`receive(config) ->
AsyncIterator[Mapping[str, Any]]`) `receivers/twitch_irc.py`/`receivers/
discord_gateway.py` already establish -- one connection-shaped object per
monitored channel, socket-lease-guarded by `app.py`'s wiring
(`provider="youtube", community=<channel_id>`) exactly like Twitch's own
per-channel `TwitchIrcReceiver` instances (see that module's own
docstring). Unlike IRC/gateway sockets, there is no persistent connection
here -- POLLING (not a push webhook, not the legacy module's
PubSubHubbub subscription) is the transport shape, chosen because it
needs zero externally-reachable callback URL and reuses the exact same
lease-per-channel wiring Twitch already proved out; the legacy module's
webhook path is not ported.

Credentials, in precedence order -- config keys `api_key_ref`/
`client_id_ref`/`client_secret_ref`/`refresh_token_ref` name environment
variables (never raw values, resolved via `waddle_transports.signing.
resolve_secret` at connect time, matching `TwitchIrcReceiver`'s
`password_ref` convention):

1. `api_key_ref` resolves -- sent as the `key=` query param on every Data
   API v3 call.
2. `client_id_ref` + `client_secret_ref` + `refresh_token_ref` ALL
   resolve -- exchanged for a short-lived OAuth access token via
   `POST https://oauth2.googleapis.com/token` (`grant_type=refresh_token`),
   cached on THIS instance until `expires_in - 60s`, sent as
   `Authorization: Bearer <token>`. A 401 from the Data API forces one
   token refresh and one retry.

This precedence + refresh-token exchange logic is a DELIBERATE, DOCUMENTED
DUPLICATION of `hub_api/services/music_providers/youtube.py`'s own
`_resolve_auth_mode`/`_get_oauth_access_token` -- copied rather than
imported because hub_api and svc-ingest are separate services/processes
(no shared runtime, no import path between them), trimmed to what a
polling receiver needs (no label-enrichment cache, no `resolve()`/
`search()` track-lookup surface). Neither credential mode resolving raises
`NonRetryableTransportError` naming exactly which config keys are unset
-- `app.py`'s own `_register_youtube_live_receiver` wiring additionally
skips registering ANY poller (WARN, not an exception) when no channel has
usable credentials at startup, matching Discord/Twitch's identical
empty-token skip behavior; this in-`receive()` check is defense in depth
for the case a lease is claimed before that startup check would apply
(e.g. a future per-channel credential override).

Quota discipline: `search.list` (100 units) + `videos.list` (1 unit) run
ONLY while no live chat is currently known (not on every poll), and only
one poller ever runs per channel (the socket lease). A 403 whose Google
`error.errors[0].reason` is quota-related (`quotaExceeded`/
`dailyLimitExceeded`/`rateLimitExceeded`/`userRateLimitExceeded`) backs
off `no_broadcast_backoff_s` (default 30s, `Config.
YOUTUBE_LIVE_POLL_NO_BROADCAST_BACKOFF_S`) and counts toward
`max_consecutive_quota_errors` (default 5,
`Config.YOUTUBE_LIVE_POLL_MAX_QUOTA_ERRORS`) -- hitting that ceiling logs
one clear WARN and ends `receive()` (the supervisor's own restart+backoff
picks it up later, same as any other died receiver). A 403 for any OTHER
reason (`liveChatEnded`, `liveChatDisabled`, `forbidden`, ...) is treated
as "this broadcast/chat is no longer live" -- INFO-logged, state reset,
same `no_broadcast_backoff_s` backoff, quota-error counter untouched.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

import httpx
from waddle_transports import (
    Direction,
    NonRetryableTransportError,
    RetryableTransportError,
    Transport,
)
from waddle_transports.signing import SecretResolutionError, resolve_secret

logger = logging.getLogger(__name__)

#: The `consumes` tag every ingest bundle wanting a raw YouTube Live chat
#: message declares (`builtin_handlers/youtube_live_ingest.py`'s own `stages.
#: ingest.consumes`) -- this receiver's half of that contract.
CONSUMES_TAG = "youtube.message"

_API_BASE = "https://www.googleapis.com/youtube/v3"
_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL, not a credential
_HTTP_TIMEOUT_S = 10.0
#: Refresh this many seconds before the OAuth access token's reported
#: expiry -- matches `hub_api/services/music_providers/youtube.py`'s own
#: safety margin exactly.
_OAUTH_EXPIRY_SAFETY_MARGIN_S = 60
#: `pollingIntervalMillis` fallback when the API response omits it --
#: matches the legacy `chat_poller.py`'s own default.
_DEFAULT_POLL_INTERVAL_MS = 5000
#: Floor under whatever `pollingIntervalMillis` the API reports, so a
#: misbehaving/misconfigured response can never drive a tight poll loop.
_MIN_POLL_INTERVAL_S = 2.0
_NO_BROADCAST_BACKOFF_S = 30.0
_MAX_CONSECUTIVE_QUOTA_ERRORS = 5
_CHAT_MAX_RESULTS = 200
_LIVE_SEARCH_MAX_RESULTS = 5

#: Google API `error.errors[0].reason` values that mean "quota", as
#: opposed to any other 403 cause (chat ended/disabled, access forbidden).
_QUOTA_REASONS = frozenset(
    {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded"}
)


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
    """This instance's cached OAuth access token; `expires_at` is a `time.monotonic()` deadline."""

    value: str
    expires_at: float


class _QuotaExceededError(Exception):
    """A 403 whose Google `reason` is quota-related -- counts toward the error ceiling."""


class _ChatUnavailableError(Exception):
    """A 403 for any non-quota reason -- the broadcast/chat is simply no longer live."""


def _describe_403_reason(response: httpx.Response) -> str:
    """Extract Google's error `reason` (falling back to `status`/`message`) from a 403 body.

    Duplicated from `hub_api/services/music_providers/youtube.py`'s
    identical helper -- see this module's own docstring for why.
    """
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


# The ignore comment below suppresses mypy --strict's "cannot subclass Any" complaint --
# Transport resolves to Any since waddle_transports ships no py.typed marker (see
# pyproject.toml's follow_imports="skip" override); the real ABC contract
# (name/directions/receive()) is still honored regardless.
class YouTubeLivePollReceiver(Transport):  # type: ignore[misc]
    """One channel's YouTube Live Chat poll loop per `receive()` call.

    Not platform-level like Discord -- `app.py` constructs one instance
    per configured channel (`Config.YOUTUBE_LIVE_CHANNELS`), each wrapped
    in its own `socket_lease.LeasedReceiver` (`provider="youtube",
    community=<channel_id>`), exactly matching `TwitchIrcReceiver`'s own
    per-channel precedent (see that module's own docstring).
    """

    name: ClassVar[str] = "youtube_live_poll"
    directions: ClassVar[frozenset[Direction]] = frozenset({Direction.INBOUND})

    def __init__(self, *, http_client: httpx.AsyncClient | None = None) -> None:
        """Build the receiver -- does not call the Data API yet, see `receive()`.

        `http_client`, if given, is used as-is and never closed by this
        receiver (caller-owned lifecycle) -- the test-only injection
        point matching `tests/conftest.py`'s own `http_client_factory`
        fixture (an `httpx.AsyncClient` wired to a `httpx.MockTransport`).
        `None` (the real/production path) builds and closes its own
        client for the lifetime of one `receive()` call.
        """
        self._injected_http_client = http_client
        self._oauth_cache: _CachedOAuthToken | None = None
        self._oauth_lock = asyncio.Lock()
        # Injectable for tests -- mirrors `socket_lease.LeasedReceiver`'s
        # own `_sleep` field precedent, avoiding real waits in unit tests
        # without monkeypatching the global `asyncio.sleep`.
        self._sleep = asyncio.sleep

    async def receive(self, config: Mapping[str, Any]) -> AsyncIterator[Mapping[str, Any]]:
        """Poll one channel's YouTube Live Chat, yielding one normalized dict per chat message.

        `config["channel_id"]` is required; `config["api_key_ref"]`/
        `config["client_id_ref"]`/`config["client_secret_ref"]`/
        `config["refresh_token_ref"]` supply credentials per this
        module's own docstring precedence. `config["no_broadcast_
        backoff_s"]`/`config["max_consecutive_quota_errors"]`/
        `config["chat_max_results"]` override this module's own default
        constants (`app.py`'s wiring plumbs `Config.YOUTUBE_LIVE_POLL_*`
        through; absent here just uses the constant directly, e.g. for a
        test constructing this receiver standalone).

        Real transform (not a stub) of each `liveChatMessages.list` item
        into the raw event dict `builtin_handlers/youtube_live_ingest.py::
        normalize()` consumes -- field names here are this receiver's own
        contract with that entrypoint, matching `TwitchIrcReceiver.
        receive()`'s own precedent (no repo-wide "raw platform event"
        schema exists yet).
        """
        channel_id = config.get("channel_id")
        if not isinstance(channel_id, str) or not channel_id:
            raise NonRetryableTransportError(
                "youtube live poll config missing required 'channel_id'"
            )

        auth = self._resolve_auth_mode(config)
        no_broadcast_backoff_s = float(
            config.get("no_broadcast_backoff_s", _NO_BROADCAST_BACKOFF_S)
        )
        max_quota_errors = int(
            config.get("max_consecutive_quota_errors", _MAX_CONSECUTIVE_QUOTA_ERRORS)
        )
        chat_max_results = int(config.get("chat_max_results", _CHAT_MAX_RESULTS))

        if self._injected_http_client is not None:
            async for item in self._poll_loop(
                self._injected_http_client,
                channel_id,
                auth,
                no_broadcast_backoff_s=no_broadcast_backoff_s,
                max_quota_errors=max_quota_errors,
                chat_max_results=chat_max_results,
            ):
                yield item
            return

        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S) as client:
            async for item in self._poll_loop(
                client,
                channel_id,
                auth,
                no_broadcast_backoff_s=no_broadcast_backoff_s,
                max_quota_errors=max_quota_errors,
                chat_max_results=chat_max_results,
            ):
                yield item

    async def _poll_loop(
        self,
        client: httpx.AsyncClient,
        channel_id: str,
        auth: _ApiKeyAuth | _OAuthAuth,
        *,
        no_broadcast_backoff_s: float,
        max_quota_errors: int,
        chat_max_results: int,
    ) -> AsyncIterator[dict[str, Any]]:
        """The real find-broadcast/poll-messages loop -- see `receive()`'s own docstring."""
        video_id: str | None = None
        live_chat_id: str | None = None
        page_token: str | None = None
        consecutive_quota_errors = 0

        while True:
            if live_chat_id is None:
                try:
                    video_id, live_chat_id = await self._find_live_video_and_chat(
                        client, auth, channel_id
                    )
                except _QuotaExceededError as exc:
                    consecutive_quota_errors += 1
                    if consecutive_quota_errors >= max_quota_errors:
                        logger.warning(
                            "gateway.youtube_quota_exhausted channel=%s consecutive=%d "
                            "reason=%s -- stopping poller",
                            channel_id,
                            consecutive_quota_errors,
                            exc,
                        )
                        return
                    logger.debug(
                        "gateway.youtube_quota_error channel=%s consecutive=%d reason=%s",
                        channel_id,
                        consecutive_quota_errors,
                        exc,
                    )
                    await self._sleep(no_broadcast_backoff_s)
                    continue

                consecutive_quota_errors = 0
                if live_chat_id is None:
                    logger.debug("gateway.youtube_no_live_broadcast channel=%s", channel_id)
                    await self._sleep(no_broadcast_backoff_s)
                    continue

                logger.info("gateway.youtube_live_ready channel=%s video=%s", channel_id, video_id)

            try:
                messages, page_token, poll_interval_ms = await self._poll_chat_messages(
                    client, auth, live_chat_id, page_token, chat_max_results
                )
                consecutive_quota_errors = 0
            except _QuotaExceededError as exc:
                consecutive_quota_errors += 1
                if consecutive_quota_errors >= max_quota_errors:
                    logger.warning(
                        "gateway.youtube_quota_exhausted channel=%s consecutive=%d "
                        "reason=%s -- stopping poller",
                        channel_id,
                        consecutive_quota_errors,
                        exc,
                    )
                    return
                logger.debug(
                    "gateway.youtube_quota_error channel=%s consecutive=%d reason=%s",
                    channel_id,
                    consecutive_quota_errors,
                    exc,
                )
                await self._sleep(no_broadcast_backoff_s)
                continue
            except _ChatUnavailableError as exc:
                logger.info(
                    "gateway.youtube_chat_ended channel=%s video=%s reason=%s",
                    channel_id,
                    video_id,
                    exc,
                )
                video_id = None
                live_chat_id = None
                page_token = None
                await self._sleep(no_broadcast_backoff_s)
                continue

            for message in messages:
                yield {
                    "platform": "youtube",
                    "channel_id": channel_id,
                    "video_id": video_id,
                    "live_chat_id": live_chat_id,
                    **message,
                }

            interval_s = max(_MIN_POLL_INTERVAL_S, poll_interval_ms / 1000)
            logger.debug(
                "gateway.youtube_poll channel=%s count=%d next_interval_s=%.1f",
                channel_id,
                len(messages),
                interval_s,
            )
            await self._sleep(interval_s)

    @staticmethod
    def _resolve_auth_mode(config: Mapping[str, Any]) -> _ApiKeyAuth | _OAuthAuth:
        """Pick the credential mode per this module's own docstring precedence.

        Raises `NonRetryableTransportError` naming exactly which config
        keys are unset/unresolvable if neither mode is usable.
        """
        api_key_ref = config.get("api_key_ref")
        if isinstance(api_key_ref, str) and api_key_ref:
            try:
                return _ApiKeyAuth(api_key=resolve_secret(api_key_ref))
            except SecretResolutionError:
                pass  # fall through to the OAuth trio -- same precedence as hub_api's youtube.py

        client_id_ref = config.get("client_id_ref")
        client_secret_ref = config.get("client_secret_ref")
        refresh_token_ref = config.get("refresh_token_ref")
        if (
            isinstance(client_id_ref, str)
            and client_id_ref
            and isinstance(client_secret_ref, str)
            and client_secret_ref
            and isinstance(refresh_token_ref, str)
            and refresh_token_ref
        ):
            try:
                return _OAuthAuth(
                    client_id=resolve_secret(client_id_ref),
                    client_secret=resolve_secret(client_secret_ref),
                    refresh_token=resolve_secret(refresh_token_ref),
                )
            except SecretResolutionError:
                pass

        raise NonRetryableTransportError(
            "youtube live poll config missing usable credentials -- set 'api_key_ref' or "
            "'client_id_ref'+'client_secret_ref'+'refresh_token_ref'"
        )

    async def _get_oauth_access_token(
        self, client: httpx.AsyncClient, auth: _OAuthAuth, *, force_refresh: bool = False
    ) -> str:
        """Return a cached or freshly-refreshed OAuth access token for `auth`'s refresh token.

        Cached on THIS instance until `expires_in -
        _OAUTH_EXPIRY_SAFETY_MARGIN_S`; `force_refresh` bypasses the
        cache (used after a 401, once). Guarded by `self._oauth_lock` so
        concurrent Data API calls don't each mint their own token.
        """
        async with self._oauth_lock:
            if (
                not force_refresh
                and self._oauth_cache is not None
                and self._oauth_cache.expires_at > time.monotonic()
            ):
                return self._oauth_cache.value

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
                raise RetryableTransportError(
                    f"youtube oauth refresh failed: {type(exc).__name__}"
                ) from exc

            if response.status_code != 200:
                raise NonRetryableTransportError(
                    f"youtube oauth refresh failed: HTTP {response.status_code} "
                    f"{_describe_oauth_error(response)}",
                    http_status=response.status_code,
                )

            payload: Any = response.json()
            token = payload.get("access_token") if isinstance(payload, dict) else None
            expires_in = payload.get("expires_in", 3600) if isinstance(payload, dict) else 3600
            if not token:
                raise NonRetryableTransportError(
                    "youtube oauth refresh failed: response missing access_token"
                )

            self._oauth_cache = _CachedOAuthToken(
                value=str(token),
                expires_at=time.monotonic()
                + max(0, int(expires_in) - _OAUTH_EXPIRY_SAFETY_MARGIN_S),
            )
            return self._oauth_cache.value

    async def _data_api_get(
        self,
        client: httpx.AsyncClient,
        auth: _ApiKeyAuth | _OAuthAuth,
        path: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """GET one Data API v3 endpoint under `auth`; retries once on a 401 in OAuth mode.

        Raises `_QuotaExceededError`/`_ChatUnavailableError` on a 403
        (see this module's own docstring for the split), `Retryable
        TransportError` on a network failure/5xx/429, and `NonRetryable
        TransportError` on any other 4xx (bad request, unresolved auth).
        """
        request_params = dict(params)
        headers: dict[str, str] = {}

        if isinstance(auth, _ApiKeyAuth):
            request_params["key"] = auth.api_key
        else:
            headers["Authorization"] = f"Bearer {await self._get_oauth_access_token(client, auth)}"

        response = await self._send(client, path, request_params, headers)

        if isinstance(auth, _OAuthAuth) and response.status_code == 401:
            token = await self._get_oauth_access_token(client, auth, force_refresh=True)
            headers["Authorization"] = f"Bearer {token}"
            response = await self._send(client, path, request_params, headers)

        if response.status_code == 403:
            reason = _describe_403_reason(response)
            if reason in _QUOTA_REASONS:
                raise _QuotaExceededError(reason)
            raise _ChatUnavailableError(reason)
        if response.status_code >= 500 or response.status_code == 429:
            raise RetryableTransportError(
                f"youtube data api HTTP {response.status_code}", http_status=response.status_code
            )
        if response.status_code >= 400:
            raise NonRetryableTransportError(
                f"youtube data api HTTP {response.status_code}", http_status=response.status_code
            )

        data: Any = response.json()
        return data if isinstance(data, dict) else {}

    @staticmethod
    async def _send(
        client: httpx.AsyncClient, path: str, params: dict[str, Any], headers: dict[str, str]
    ) -> httpx.Response:
        """GET one Data API v3 endpoint; maps network failures to `RetryableTransportError`."""
        try:
            return await client.get(f"{_API_BASE}{path}", params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise RetryableTransportError(
                f"youtube data api request failed: {type(exc).__name__}"
            ) from exc

    async def _find_live_video_and_chat(
        self, client: httpx.AsyncClient, auth: _ApiKeyAuth | _OAuthAuth, channel_id: str
    ) -> tuple[str | None, str | None]:
        """Find `channel_id`'s active live broadcast + its `activeLiveChatId`, if any.

        Two real Data API v3 calls, exactly the legacy module's own
        sequence (`youtube_client.py`'s `get_live_broadcasts`/
        `_get_live_chat_id`): `search.list?eventType=live` to find a
        candidate video id, then `videos.list?part=liveStreamingDetails`
        to resolve that video's `activeLiveChatId`. `(None, None)` when
        the channel has no live broadcast right now -- not an error, the
        normal "nothing live yet" outcome the poll loop backs off on.
        """
        search_data = await self._data_api_get(
            client,
            auth,
            "/search",
            {
                "part": "id,snippet",
                "channelId": channel_id,
                "eventType": "live",
                "type": "video",
                "maxResults": _LIVE_SEARCH_MAX_RESULTS,
            },
        )
        video_id: str | None = None
        for item in search_data.get("items") or []:
            item_id = item.get("id")
            candidate = item_id.get("videoId") if isinstance(item_id, dict) else None
            if isinstance(candidate, str) and candidate:
                video_id = candidate
                break
        if video_id is None:
            return None, None

        videos_data = await self._data_api_get(
            client, auth, "/videos", {"part": "liveStreamingDetails", "id": video_id}
        )
        items = videos_data.get("items") or []
        if not items:
            return video_id, None
        live_details = items[0].get("liveStreamingDetails") or {}
        live_chat_id = live_details.get("activeLiveChatId")
        return video_id, live_chat_id if isinstance(live_chat_id, str) and live_chat_id else None

    async def _poll_chat_messages(
        self,
        client: httpx.AsyncClient,
        auth: _ApiKeyAuth | _OAuthAuth,
        live_chat_id: str,
        page_token: str | None,
        max_results: int,
    ) -> tuple[list[dict[str, Any]], str | None, int]:
        """One `liveChat/messages.list` call -- returns `(messages, next_page_token, interval_ms)`.

        `pollingIntervalMillis`/`nextPageToken` are the API's own
        pagination/backoff contract (Google's documented Live Chat
        polling guidance) -- honored verbatim, not re-derived.
        """
        params: dict[str, Any] = {
            "part": "id,snippet,authorDetails",
            "liveChatId": live_chat_id,
            "maxResults": max_results,
        }
        if page_token:
            params["pageToken"] = page_token

        data = await self._data_api_get(client, auth, "/liveChat/messages", params)

        messages: list[dict[str, Any]] = []
        for item in data.get("items") or []:
            parsed = self._parse_message(item)
            if parsed is not None:
                messages.append(parsed)

        next_token = data.get("nextPageToken")
        poll_interval_ms = data.get("pollingIntervalMillis", _DEFAULT_POLL_INTERVAL_MS)
        interval_ms = (
            int(poll_interval_ms)
            if isinstance(poll_interval_ms, int | float)
            else _DEFAULT_POLL_INTERVAL_MS
        )
        return (
            messages,
            next_token if isinstance(next_token, str) and next_token else None,
            interval_ms,
        )

    @staticmethod
    def _parse_message(item: dict[str, Any]) -> dict[str, Any] | None:
        """Parse one `liveChatMessages` resource item; `None` (skipped, not raised) if malformed.

        `text` comes from `snippet.displayMessage` -- the API's own
        pre-rendered human-readable text for EVERY message type (plain
        text, Super Chat, membership, ...), not the legacy module's
        per-`snippet.type` manual reconstruction
        (`youtube_client.py::_parse_chat_message`) -- a real item missing
        it (a message type with no displayable text) is skipped, never
        crashes the poll loop.
        """
        snippet = item.get("snippet") or {}
        author = item.get("authorDetails") or {}

        text = snippet.get("displayMessage")
        if not isinstance(text, str) or not text:
            return None

        return {
            "text": text,
            "author_id": author.get("channelId") or None,
            "display_name": author.get("displayName") or None,
            "is_mod": bool(author.get("isChatModerator")),
            "is_owner": bool(author.get("isChatOwner")),
            "is_sponsor": bool(author.get("isChatSponsor")),
            "message_id": item.get("id") or None,
            "published_at": snippet.get("publishedAt") or None,
        }
