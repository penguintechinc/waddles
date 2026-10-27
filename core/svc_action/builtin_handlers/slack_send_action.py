"""Slack send-message ACTION handler -- real Slack Web API `chat.postMessage` call.

Ported from `action/pushing/slack_action_module/services/slack_service.py`'s
`send_message` (Bot-token auth, `chat.postMessage`, `thread_ts` reply-in-
thread, `mrkdwn` text) into the built-in action-stage handler
contract: `async def <name>(envelope, config, *, http_client) -> TransportResult`
(`runner.py`).

Uses plain `httpx` rather than adding `slack-sdk` as a new dependency --
`chat.postMessage` is one POST with a bearer token and a JSON body, exactly
the shape `waddle_transports.url_guard.guarded_request` already handles for
`discord_send_action.py`; pulling in a whole SDK (plus its own HTTP client,
retry logic, and dependency tree) for a single endpoint would duplicate the
SSRF guard/secret-resolution/retry infrastructure this handler already gets
for free from `waddle_transports`.

Reuses the shared `waddle_transports` library's SSRF guard
(`waddle_transports.url_guard.guarded_request`) and secret resolution
(`waddle_transports.signing.resolve_secret` -- an env-var-name indirection,
never a raw bot token in `app_catalog`/`app_activations` config) rather
than reimplementing either. Deliberately calls `guarded_request` directly
rather than routing through `waddle_transports`' `http` transport's
`rest_api` sub_type, for the same reason `discord_send_action.py`'s module
docstring documents: Slack's real semantics (HTTP 200 with `{"ok": false,
"error": "..."}` on failure, `429` + `Retry-After` on rate limit) need this
handler's own response-body interpretation, not the generic sub_type's
status-code-only classification.

Also deliberately does **not** port `slack_service.py`'s own `slack_actions`
audit table or its Block Kit (`blocks`) support -- audit is now handled by
svc-action's platform-level `action_dispatch_log` (`runner.py::
_handle_envelope`), and Block Kit is out of scope for this reply-in-place
text-send handler (a future bundle/config addition, not a silent partial
feature here).

Catalog entry (`app_catalog.stages.action`, seeded via a future migration
following the `082_discord_send_action_bundle.sql` convention) for
`waddles.bot.slack.default`:

```json
{
  "entrypoint": "builtin_handlers.slack_send_action:send_message",
  "spec": {"required_config": ["channel_id", "bot_token_ref"]},
  "config": {"api_base": "https://slack.com/api"}
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

logger = logging.getLogger(__name__)

#: Real Slack Web API base -- overridable via handler `config["api_base"]`
#: (tests point this at a literal-IP mock target, matching every other
#: transport's test convention of avoiding real DNS resolution in unit
#: tests -- `waddle_transports.url_guard.validate_url` resolves the host
#: via `socket.getaddrinfo` before every request, including in tests).
_DEFAULT_API_BASE = "https://slack.com/api"

#: Slack `chat.postMessage` `error` codes that mean the bot token itself is
#: bad -- distinct from a channel-membership problem, mapped to a distinct
#: message so an operator doesn't chase the wrong fix.
_AUTH_ERROR_CODES = frozenset({"invalid_auth", "not_authed", "token_revoked"})

#: Slack `error` codes that mean the token is fine but the bot isn't
#: positioned to post into the target channel.
_CHANNEL_ERROR_CODES = frozenset({"channel_not_found", "not_in_channel"})


async def send_message(
    envelope: StageEnvelope,
    config: Mapping[str, Any],
    *,
    http_client: httpx.AsyncClient,
) -> TransportResult:
    """Reply in-place: send `envelope.event.payload["text"]` to the resolved Slack channel.

    "Reply-in-place" is the primary behavior -- `channel_id` comes from the
    triggering event's own payload (the channel the inbound message that
    caused this action came from), not a statically configured channel, so
    a handler activated once serves every channel the bot is in rather than
    always posting to one hardcoded channel. `config` (the handler's
    resolved `stages.action.config`) supplies `channel_id` only as a
    fallback for a proactive/scheduled send with no originating channel in
    its payload, and must always declare `bot_token_ref` (an env-var
    *name*, resolved via `resolve_secret` -- never a literal `xoxb-` token
    in DB config). An optional `envelope.event.payload["thread_ts"]`
    replies in-thread instead of posting a new top-level message. `api_base`
    optionally overrides the Slack Web API root (default: the real Slack
    API).

    Raises `NonRetryableTransportError` for a config/auth failure (no
    resolvable channel_id, unresolvable secret, HTTP 401/403, or a Slack
    `{"ok": false, ...}` error body -- auth/channel/other) and
    `RetryableTransportError` for a rate limit (HTTP 429, one retry then a
    terminal `RetryableTransportError` -- `retry_with_backoff` in
    `runner.py` owns the actual backoff; this handler never sleeps beyond
    its one immediate retry) or a 5xx/network error.
    """
    event_payload = envelope.event.payload
    payload_channel_id = event_payload.get("channel_id")
    config_channel_id = config.get("channel_id")
    channel_id = (
        payload_channel_id
        if isinstance(payload_channel_id, str) and payload_channel_id
        else (
            config_channel_id if isinstance(config_channel_id, str) and config_channel_id else None
        )
    )
    if channel_id is None:
        raise NonRetryableTransportError(
            "slack bundle could not resolve a channel_id from either "
            "envelope.event.payload['channel_id'] (reply-in-place) or "
            "config['channel_id'] (fallback)"
        )

    bot_token_ref = config.get("bot_token_ref")
    if not isinstance(bot_token_ref, str) or not bot_token_ref:
        raise NonRetryableTransportError("slack bundle config missing required 'bot_token_ref'")

    text = event_payload.get("text")
    if not isinstance(text, str) or not text:
        raise NonRetryableTransportError(
            "action envelope event.payload missing required 'text' string"
        )

    try:
        bot_token = resolve_secret(bot_token_ref)
    except SecretResolutionError as exc:
        raise NonRetryableTransportError(f"slack bot token resolution failed: {exc}") from exc

    api_base = config.get("api_base", _DEFAULT_API_BASE)
    url = f"{api_base}/chat.postMessage"
    headers = {
        "Authorization": f"Bearer {bot_token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    body: dict[str, Any] = {"channel": channel_id, "text": text, "unfurl_links": False}
    thread_ts = event_payload.get("thread_ts")
    if isinstance(thread_ts, str) and thread_ts:
        body["thread_ts"] = thread_ts

    response = await _post_with_one_retry(http_client, url, headers, body)

    if response.status_code in (401, 403):
        logger.warning(
            "slack_send_action.slack_send_rejected bot_token_ref=%s channel=%s status=%s",
            bot_token_ref,
            channel_id,
            response.status_code,
        )
        raise NonRetryableTransportError(
            f"slack API rejected auth: HTTP {response.status_code}",
            http_status=response.status_code,
        )
    if 400 <= response.status_code < 500:
        raise NonRetryableTransportError(
            f"slack API returned client error: HTTP {response.status_code} {response.text[:200]}",
            http_status=response.status_code,
        )
    if response.status_code >= 500:
        raise RetryableTransportError(
            f"slack API returned server error: HTTP {response.status_code}",
            http_status=response.status_code,
        )

    try:
        result_body = response.json()
    except Exception as exc:  # noqa: BLE001 -- an unparsable 200 body is a permanent failure
        raise NonRetryableTransportError(
            f"slack API returned an unparsable response body: {exc}"
        ) from exc

    if result_body.get("ok") is not True:
        error_code = result_body.get("error", "unknown_error")
        if error_code in _AUTH_ERROR_CODES:
            logger.warning(
                "slack_send_action.slack_send_rejected bot_token_ref=%s channel=%s error=%s",
                bot_token_ref,
                channel_id,
                error_code,
            )
            raise NonRetryableTransportError(f"slack bot token didn't work ({error_code})")
        if error_code in _CHANNEL_ERROR_CODES:
            raise NonRetryableTransportError(f"bot isn't in that Slack channel ({error_code})")
        raise NonRetryableTransportError(f"slack API error: {error_code}")

    message_ts = result_body.get("ts")
    logger.info(
        "slack_send_action.sent channel=%s thread=%s len=%d",
        channel_id,
        bool(isinstance(thread_ts, str) and thread_ts),
        len(text),
    )
    return TransportResult(
        transport="bundle",
        detail=f"slack message sent, channel={channel_id} ts={message_ts}",
        http_status=response.status_code,
    )


async def _post_with_one_retry(
    http_client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
) -> httpx.Response:
    """POST to `url`, retrying exactly once on HTTP 429 before giving up as retryable.

    Slack's own convention is `Retry-After` + client-side wait before
    retrying; this handler honors that with a single immediate re-attempt
    (bounded, unlike the legacy module's unbounded `asyncio.sleep` loop --
    see the module docstring) rather than sleeping for the full
    `Retry-After` duration itself, since `retry_with_backoff` in
    `runner.py` owns backoff timing platform-wide.
    """
    try:
        response = await guarded_request(http_client, "POST", url, headers=headers, json=body)
    except SSRFError as exc:
        raise NonRetryableTransportError(f"slack API URL rejected by SSRF guard: {exc}") from exc
    except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
        raise RetryableTransportError(f"slack API request failed: {exc}") from exc

    if response.status_code != 429:
        # waddle_transports ships no py.typed marker yet (pyproject.toml's
        # documented mypy override) -- guarded_request's real `-> httpx.
        # Response` annotation is invisible across that boundary, so mypy
        # --strict sees an Any return here; cast to the type its own
        # source declares.
        return cast(httpx.Response, response)

    try:
        retry_response = await guarded_request(http_client, "POST", url, headers=headers, json=body)
    except SSRFError as exc:
        raise NonRetryableTransportError(f"slack API URL rejected by SSRF guard: {exc}") from exc
    except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
        raise RetryableTransportError(f"slack API request failed: {exc}") from exc

    if retry_response.status_code == 429:
        retry_after = retry_response.headers.get("Retry-After", "1")
        raise RetryableTransportError(
            f"slack API rate limited, retry after {retry_after}s", http_status=429
        )
    return cast(httpx.Response, retry_response)
