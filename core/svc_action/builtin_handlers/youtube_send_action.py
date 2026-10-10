"""YouTube send-message ACTION handler -- real YouTube Data API v3 `liveChatMessages.insert` call.

Ported from `action/pushing/youtube_action_module/services/
youtube_service.py`'s `send_live_chat_message` (Google API client,
`liveChatMessages().insert(part="snippet", body={...})`) into the built-in
action-stage handler contract: `async def <name>(envelope,
config, *, http_client) -> TransportResult` (`runner.py`), matching
`discord_send_action.py`/`slack_send_action.py`'s own shape.

Uses plain `httpx` rather than `google-api-python-client` +
`google-auth-oauthlib` (the legacy module's dependencies) -- one POST
with a bearer token and a JSON body is exactly the shape
`waddle_transports.url_guard.guarded_request` already handles for every
other send handler; the Google client SDK's own OAuth flow, discovery-doc
fetch, and HTTP stack would duplicate the SSRF guard/secret-resolution/
retry infrastructure this handler already gets for free from
`waddle_transports`, per `discord_send_action.py`'s own module docstring
precedent for not adding `slack-sdk`.

OAuth token management is `services.youtube_oauth` (a small, deliberately
copied helper, NOT an import of `hub_api` -- see that module's own
docstring for why): refresh-token -> access-token exchange, in-process
cache, and `token_has_scope()` for the pre-flight scope check below.
Config supplies the refresh-token trio as env-var-name indirections
(`client_id_ref`/`client_secret_ref`/`refresh_token_ref`, resolved via
`waddle_transports.signing.resolve_secret` -- never a literal token in
`app_catalog`/`app_activations` config), defaulting to `YOUTUBE_CLIENT_ID`
/`YOUTUBE_CLIENT_SECRET`/`YOUTUBE_REFRESH_TOKEN` respectively so a single
default channel's credentials need no config at all beyond activation.

Deliberately does **not** port `youtube_service.py`'s own `OAuthManager`
(a per-channel-row Postgres token store) -- this handler serves the single
default YouTube channel a service-level refresh token authenticates as,
mirroring the `YOUTUBE_CLIENT_ID`/`YOUTUBE_CLIENT_SECRET`/
`YOUTUBE_REFRESH_TOKEN` env-var precedent `hub_api/services/
music_providers/youtube.py` already established for this repo; a
multi-channel per-activation token store is a future config-schema
addition, not a silent partial feature here. Also does not port that
module's `youtube_oauth_tokens` DB table or its bare `AUDIT`/`ERROR`
f-string logging -- audit is svc-action's platform-level
`action_dispatch_log` (`runner.py::_handle_envelope`), and logging here
uses this repo's standard `logger.info`/`logger.warning` %-style calls.

Pre-flight scope check (`services.youtube_oauth.token_has_scope`, cached
per access token) runs BEFORE the send attempt so a refresh token
provisioned without `youtube.force-ssl` consent fails with the specific,
actionable message below instead of a bare 403 -- if the scope-check
lookup itself fails (network/tokeninfo hiccup), that failure never blocks
the send; the real API call remains the source of truth.

Catalog entry (`app_catalog.stages.action`) for `waddles.bot.youtube.default`:

```json
{
  "entrypoint": "builtin_handlers.youtube_send_action:send_message",
  "spec": {"required_config": ["refresh_token_ref"]},
  "config": {"api_base": "https://www.googleapis.com/youtube/v3"}
}
```
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, cast

import httpx
from flask_core import StageEnvelope
from waddle_transports import NonRetryableTransportError, RetryableTransportError, TransportResult
from waddle_transports.signing import SecretResolutionError, resolve_secret
from waddle_transports.url_guard import SSRFError, guarded_request

from services.youtube_oauth import (
    YOUTUBE_FORCE_SSL_SCOPE,
    YouTubeOAuthError,
    get_access_token_for_community,
    token_has_scope,
)

logger = logging.getLogger(__name__)

#: Real YouTube Data API v3 base -- overridable via handler `config["api_base"]`
#: (tests point this at a literal-IP mock target, matching every other
#: transport's test convention of avoiding real DNS resolution in unit
#: tests -- `waddle_transports.url_guard.validate_url` resolves the host
#: via `socket.getaddrinfo` before every request, including in tests).
_DEFAULT_API_BASE = "https://www.googleapis.com/youtube/v3"

#: YouTube Live Chat's own character cap on `textMessageDetails.messageText`.
_MESSAGE_MAX_LEN = 200

#: `error.errors[0].reason` values meaning the refresh token doesn't carry
#: chat-send permission -- distinct from `quotaExceeded`, mapped to a
#: separate, actionable message.
_SCOPE_ERROR_REASONS = frozenset({"insufficientPermissions", "forbidden"})

#: `error.errors[0].reason` values meaning the target live chat is gone --
#: distinct from any other 404 shape.
_LIVE_CHAT_GONE_REASONS = frozenset({"liveChatNotFound", "liveChatEnded"})


def _truncate_for_youtube(text: str) -> str:
    """Truncate `text` to YouTube Live Chat's `_MESSAGE_MAX_LEN`-char cap, appending `…`."""
    if len(text) <= _MESSAGE_MAX_LEN:
        return text
    return text[: _MESSAGE_MAX_LEN - 1] + "…"


def _extract_reason(response: httpx.Response) -> str | None:
    """Pull `error.errors[0].reason` out of a YouTube Data API v3 error response body."""
    try:
        payload: Any = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    errors = error.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        reason = errors[0].get("reason")
        if isinstance(reason, str):
            return reason
    return None


async def _guarded_call(
    http_client: httpx.AsyncClient,
    method: str,
    url: str,
    access_token: str,
    *,
    json_body: dict[str, Any] | None = None,
) -> httpx.Response:
    """SSRF-guarded call to the (config-controlled) YouTube API base, bearer-authenticated."""
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        response = await guarded_request(http_client, method, url, headers=headers, json=json_body)
    except SSRFError as exc:
        raise NonRetryableTransportError(f"youtube API URL rejected by SSRF guard: {exc}") from exc
    except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
        raise RetryableTransportError(f"youtube API request failed: {exc}") from exc
    # waddle_transports ships no py.typed marker yet (this service's own
    # pyproject.toml documents the mypy override) -- guarded_request's real
    # `-> httpx.Response` annotation is invisible across that boundary, so
    # mypy --strict sees an Any return here; cast to the type its own
    # source declares.
    return cast(httpx.Response, response)


async def send_message(
    envelope: StageEnvelope,
    config: Mapping[str, Any],
    *,
    http_client: httpx.AsyncClient,
) -> TransportResult:
    """Reply in-place: send `envelope.event.payload["text"]` into the resolved live chat.

    `live_chat_id` comes from `envelope.event.payload["live_chat_id"]`
    (set by `builtin_handlers/youtube_live_ingest.py::normalize()` on the
    triggering chat event) first; if absent, falls back to resolving it
    from `envelope.event.payload["video_id"]` via one `videos.list` call
    (`liveStreamingDetails.activeLiveChatId`), cached in-process per
    video id so a burst of replies to the same stream costs one lookup.
    `config` must always declare `refresh_token_ref` (an env-var *name*,
    resolved via `resolve_secret` -- never a literal refresh token in DB
    config); `client_id_ref`/`client_secret_ref` default to
    `YOUTUBE_CLIENT_ID`/`YOUTUBE_CLIENT_SECRET` if not set. `api_base`
    optionally overrides the YouTube Data API v3 root (default: the real
    API). `text` longer than YouTube's 200-char live-chat cap is
    truncated with a trailing `…`.

    OAuth token resolution is community-aware (gh-320,
    `services.youtube_oauth.get_access_token_for_community`): if
    `envelope.community` has a per-community-connected YouTube account,
    its token is used; otherwise this falls back to the `refresh_token_ref`
    env-credential flow above, unchanged.

    Raises `NonRetryableTransportError` for a config/auth failure (no
    resolvable live_chat_id, unresolvable secret, OAuth refresh failure,
    a refresh token missing the `youtube.force-ssl` scope, a persistent
    401, or a 403/404 the API itself reports) and `RetryableTransportError`
    for a 429 (rate limited -- one immediate retry, then terminal; runner.
    py's `retry_with_backoff` owns actual backoff, this handler never
    sleeps beyond that one retry) or a 5xx/network error.
    """
    event_payload = envelope.event.payload
    text = event_payload.get("text")
    if not isinstance(text, str) or not text:
        raise NonRetryableTransportError(
            "action envelope event.payload missing required 'text' string"
        )
    text = _truncate_for_youtube(text)

    community_id: int | None
    try:
        community_id = int(envelope.community) if envelope.community is not None else None
    except (TypeError, ValueError):
        community_id = None

    client_id_ref = config.get("client_id_ref") or "YOUTUBE_CLIENT_ID"
    client_secret_ref = config.get("client_secret_ref") or "YOUTUBE_CLIENT_SECRET"
    refresh_token_ref = config.get("refresh_token_ref") or "YOUTUBE_REFRESH_TOKEN"
    if not isinstance(client_id_ref, str) or not isinstance(client_secret_ref, str):
        raise NonRetryableTransportError(
            "youtube bundle config 'client_id_ref'/'client_secret_ref' must be strings"
        )
    if not isinstance(refresh_token_ref, str):
        raise NonRetryableTransportError(
            "youtube bundle config 'refresh_token_ref' must be a string"
        )

    try:
        client_id = resolve_secret(client_id_ref)
        client_secret = resolve_secret(client_secret_ref)
        refresh_token = resolve_secret(refresh_token_ref)
    except SecretResolutionError as exc:
        raise NonRetryableTransportError(f"youtube bundle secret resolution failed: {exc}") from exc

    try:
        access_token = await get_access_token_for_community(
            http_client, community_id, client_id, client_secret, refresh_token
        )
    except YouTubeOAuthError as exc:
        raise NonRetryableTransportError(str(exc)) from exc

    try:
        has_scope = await token_has_scope(http_client, access_token, YOUTUBE_FORCE_SSL_SCOPE)
    except YouTubeOAuthError as exc:
        logger.warning("youtube_send_action.scope_check_unavailable error=%s", exc)
        has_scope = True  # never block a send on a scope-lookup infra hiccup
    if not has_scope:
        raise NonRetryableTransportError(
            "youtube refresh token lacks the youtube.force-ssl scope needed to send chat"
        )

    api_base = config.get("api_base", _DEFAULT_API_BASE)
    if not isinstance(api_base, str) or not api_base:
        api_base = _DEFAULT_API_BASE

    live_chat_id = event_payload.get("live_chat_id")
    if not (isinstance(live_chat_id, str) and live_chat_id):
        video_id = event_payload.get("video_id")
        if not (isinstance(video_id, str) and video_id):
            raise NonRetryableTransportError(
                "action envelope event.payload missing both 'live_chat_id' and 'video_id'"
            )
        live_chat_id = await _resolve_live_chat_id(http_client, api_base, access_token, video_id)

    send_url = f"{api_base}/liveChatMessages?part=snippet"
    body: dict[str, Any] = {
        "snippet": {
            "liveChatId": live_chat_id,
            "type": "textMessageEvent",
            "textMessageDetails": {"messageText": text},
        }
    }

    response = await _guarded_call(http_client, "POST", send_url, access_token, json_body=body)
    if response.status_code == 401:
        try:
            access_token = await get_access_token_for_community(
                http_client,
                community_id,
                client_id,
                client_secret,
                refresh_token,
                force_refresh=True,
            )
        except YouTubeOAuthError as exc:
            raise NonRetryableTransportError(str(exc)) from exc
        response = await _guarded_call(http_client, "POST", send_url, access_token, json_body=body)
        if response.status_code == 401:
            raise NonRetryableTransportError("youtube oauth token didn't work (401)")

    if response.status_code == 403:
        reason = _extract_reason(response)
        if reason in _SCOPE_ERROR_REASONS:
            raise NonRetryableTransportError(
                "youtube refresh token lacks the youtube.force-ssl scope needed to send chat"
            )
        if reason == "quotaExceeded":
            raise NonRetryableTransportError("youtube api quota exceeded")
        raise NonRetryableTransportError(
            f"youtube API returned client error: HTTP 403 {response.text[:200]}",
            http_status=403,
        )

    if response.status_code == 404:
        reason = _extract_reason(response)
        if reason in _LIVE_CHAT_GONE_REASONS:
            raise NonRetryableTransportError("that YouTube live chat has ended")
        raise NonRetryableTransportError(
            f"youtube API returned client error: HTTP 404 {response.text[:200]}",
            http_status=404,
        )

    if response.status_code == 429:
        response = await _guarded_call(http_client, "POST", send_url, access_token, json_body=body)
        if response.status_code == 429:
            raise RetryableTransportError("youtube api rate limited", http_status=429)

    if response.status_code == 401:
        raise NonRetryableTransportError("youtube oauth token didn't work (401)")
    if 400 <= response.status_code < 500:
        raise NonRetryableTransportError(
            f"youtube API returned client error: HTTP {response.status_code} {response.text[:200]}",
            http_status=response.status_code,
        )
    if response.status_code >= 500:
        raise RetryableTransportError(
            f"youtube API returned server error: HTTP {response.status_code}",
            http_status=response.status_code,
        )

    logger.info(
        "youtube_send_action.sent live_chat=%s len=%d",
        live_chat_id,
        len(text),
    )
    return TransportResult(
        transport="bundle",
        detail=f"youtube live chat message sent, live_chat={live_chat_id}",
        http_status=response.status_code,
    )


#: In-process video id -> resolved live chat id cache -- a live stream's
#: `activeLiveChatId` is stable for the stream's own lifetime, so one
#: `videos.list` lookup per video id is enough (mirrors `hub_api/
#: services/music_providers/youtube.py`'s own in-process caching
#: convention for per-id API lookups).
_video_live_chat_id_cache: dict[str, str] = {}


async def _resolve_live_chat_id(
    http_client: httpx.AsyncClient, api_base: str, access_token: str, video_id: str
) -> str:
    """Resolve `video_id`'s `activeLiveChatId` via one cached `videos.list` call.

    Raises `NonRetryableTransportError` if the video can't be resolved
    (non-200 response, video not found, or the video has no active live
    chat -- e.g. it isn't currently live).
    """
    cached = _video_live_chat_id_cache.get(video_id)
    if cached is not None:
        return cached

    url = f"{api_base}/videos?part=liveStreamingDetails&id={video_id}"
    response = await _guarded_call(http_client, "GET", url, access_token)
    if response.status_code != 200:
        raise NonRetryableTransportError(
            f"youtube could not resolve a live chat id for video {video_id}: "
            f"HTTP {response.status_code} {response.text[:200]}",
            http_status=response.status_code,
        )

    try:
        payload: Any = response.json()
    except ValueError as exc:
        raise NonRetryableTransportError(
            f"youtube videos.list returned an unparsable response body: {exc}"
        ) from exc

    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items:
        raise NonRetryableTransportError(f"youtube video {video_id} not found")

    details = items[0].get("liveStreamingDetails") if isinstance(items[0], dict) else None
    live_chat_id = details.get("activeLiveChatId") if isinstance(details, dict) else None
    if not isinstance(live_chat_id, str) or not live_chat_id:
        raise NonRetryableTransportError(f"youtube video {video_id} has no active live chat")

    _video_live_chat_id_cache[video_id] = live_chat_id
    return live_chat_id
