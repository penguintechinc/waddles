"""kick_send_action.py -- real Kick chat API `POST .../messages/send/<chatroom_id>` call.

Built-in action-stage handler contract: `async def <name>(envelope,
config, *, http_client) -> TransportResult` (`runner.py`), matching
`discord_send_action.py`/`slack_send_action.py`/`youtube_send_action.py`'s
own shape -- a direct outbound API call (unlike `twitch_send_action.py`'s
IRC-relay-through-svc-ingest shape; Kick's real chat API is a plain
bearer-authenticated REST endpoint, exactly the `guarded_request` shape
every other direct-call handler already uses).

Reuses the shared `waddle_transports` library's SSRF guard
(`waddle_transports.url_guard.guarded_request`) and secret resolution
(`waddle_transports.signing.resolve_secret` -- env-var-name indirections,
never a raw token/secret in `app_catalog`/`app_activations` config).

OAuth token management is `services.kick_oauth` (see that module's own
docstring): a STORED access token (`config["access_token_ref"]`,
defaulting to `KICK_ACCESS_TOKEN`) takes precedence; if unresolvable, this
handler falls back to `client_id_ref`/`client_secret_ref` (defaulting to
`KICK_CLIENT_ID`/`KICK_CLIENT_SECRET`) exchanged for a client-credentials
app token. On an observed 401, this handler re-fetches the access token
with `force_refresh=True` and retries EXACTLY ONCE -- a no-op in stored-
token mode (there is nothing to refresh, see `services/kick_oauth.py`'s
own docstring), a real refresh in client-credentials mode.

Catalog entry (`app_catalog.stages.action`) for `waddles.bot.kick.default`:

```json
{
  "entrypoint": "builtin_handlers.kick_send_action:send_message",
  "spec": {"required_config": ["access_token_ref"]},
  "config": {"api_base": "https://kick.com/api/v2"}
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

from services.kick_oauth import KickOAuthError, get_access_token

logger = logging.getLogger(__name__)

#: Real Kick REST API base -- overridable via handler `config["api_base"]`
#: (tests point this at a literal-IP mock target, matching every other
#: transport's test convention of avoiding real DNS resolution in unit
#: tests -- `waddle_transports.url_guard.validate_url` resolves the host
#: via `socket.getaddrinfo` before every request, including in tests).
_DEFAULT_API_BASE = "https://kick.com/api/v2"


def _resolve_optional_secret(secret_ref: str) -> str | None:
    """`resolve_secret`, but returns `None` (never raises) when the env var is unset/empty.

    Both credential modes are individually optional (`services/
    kick_oauth.get_access_token` decides which combination is usable) --
    an unset `access_token_ref` must not itself be fatal when
    `client_id_ref`/`client_secret_ref` are set, and vice versa.
    """
    try:
        # waddle_transports ships no py.typed marker yet (this service's
        # own pyproject.toml documents the mypy override) -- resolve_
        # secret's real `-> str` annotation is invisible across that
        # boundary, so mypy --strict sees an Any return here; cast to the
        # type its own source declares.
        return cast(str, resolve_secret(secret_ref))
    except SecretResolutionError:
        return None


async def _guarded_call(
    http_client: httpx.AsyncClient, url: str, access_token: str, body: dict[str, Any]
) -> httpx.Response:
    """SSRF-guarded `POST` to the (config-controlled) Kick API base, bearer-authenticated."""
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    try:
        response = await guarded_request(http_client, "POST", url, headers=headers, json=body)
    except SSRFError as exc:
        raise NonRetryableTransportError(f"kick API URL rejected by SSRF guard: {exc}") from exc
    except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
        raise RetryableTransportError(f"kick API request failed: {exc}") from exc
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
    """Reply in-place: send `envelope.event.payload["text"]` to the resolved `chatroom_id`.

    "Reply-in-place" is the primary behavior -- `chatroom_id` comes from
    the triggering event's own payload (the chatroom the inbound message
    that caused this action came from, set by `builtin_handlers/kick_ingest.py::
    normalize()`), not a statically configured chatroom, so a handler
    activated once serves every Kick channel the bot is in rather than
    always posting to one hardcoded chatroom. `config` supplies
    `chatroom_id` only as a fallback for a proactive/scheduled send with
    no originating chatroom in its payload.

    `config["access_token_ref"]` (default `KICK_ACCESS_TOKEN`) is tried
    first; if unresolvable, `config["client_id_ref"]`/
    `config["client_secret_ref"]` (default `KICK_CLIENT_ID`/
    `KICK_CLIENT_SECRET`) are exchanged for a client-credentials app
    token. `api_base` optionally overrides the Kick API root (default:
    the real Kick API).

    Raises `NonRetryableTransportError` for a config/auth failure (no
    resolvable chatroom_id, no usable credential mode, a persistent 401
    after one forced-refresh retry, or a 403) and `RetryableTransportError`
    for a 429 (rate limited -- one immediate retry, then terminal;
    `retry_with_backoff` in `runner.py` owns actual backoff, this handler
    never sleeps beyond that one retry) or a 5xx/network error.
    """
    event_payload = envelope.event.payload
    payload_chatroom_id = event_payload.get("chatroom_id")
    config_chatroom_id = config.get("chatroom_id")
    chatroom_id = (
        payload_chatroom_id
        if isinstance(payload_chatroom_id, int | str) and payload_chatroom_id != ""
        else (
            config_chatroom_id
            if isinstance(config_chatroom_id, int | str) and config_chatroom_id != ""
            else None
        )
    )
    if chatroom_id is None:
        raise NonRetryableTransportError(
            "kick bundle could not resolve a chatroom_id from either "
            "envelope.event.payload['chatroom_id'] (reply-in-place) or "
            "config['chatroom_id'] (fallback)"
        )

    text = event_payload.get("text")
    if not isinstance(text, str) or not text:
        raise NonRetryableTransportError(
            "action envelope event.payload missing required 'text' string"
        )

    access_token_ref = config.get("access_token_ref") or "KICK_ACCESS_TOKEN"
    client_id_ref = config.get("client_id_ref") or "KICK_CLIENT_ID"
    client_secret_ref = config.get("client_secret_ref") or "KICK_CLIENT_SECRET"
    if not (
        isinstance(access_token_ref, str)
        and isinstance(client_id_ref, str)
        and isinstance(client_secret_ref, str)
    ):
        raise NonRetryableTransportError(
            "kick bundle config 'access_token_ref'/'client_id_ref'/'client_secret_ref' "
            "must be strings"
        )

    stored_access_token = _resolve_optional_secret(access_token_ref)
    client_id = _resolve_optional_secret(client_id_ref)
    client_secret = _resolve_optional_secret(client_secret_ref)

    try:
        access_token = await get_access_token(
            http_client,
            stored_access_token=stored_access_token,
            client_id=client_id,
            client_secret=client_secret,
        )
    except KickOAuthError as exc:
        raise NonRetryableTransportError(str(exc)) from exc

    api_base = config.get("api_base", _DEFAULT_API_BASE)
    if not isinstance(api_base, str) or not api_base:
        api_base = _DEFAULT_API_BASE
    url = f"{api_base}/messages/send/{chatroom_id}"
    body: dict[str, Any] = {"content": text, "type": "message"}

    response = await _guarded_call(http_client, url, access_token, body)

    if response.status_code == 401:
        try:
            access_token = await get_access_token(
                http_client,
                stored_access_token=stored_access_token,
                client_id=client_id,
                client_secret=client_secret,
                force_refresh=True,
            )
        except KickOAuthError as exc:
            raise NonRetryableTransportError(str(exc)) from exc
        response = await _guarded_call(http_client, url, access_token, body)
        if response.status_code == 401:
            logger.warning(
                "kick_send_action.kick_send_rejected chatroom=%s status=401",
                chatroom_id,
            )
            raise NonRetryableTransportError("kick oauth token didn't work (401)")

    if response.status_code == 403:
        raise NonRetryableTransportError("kick chat send forbidden (403)")

    if response.status_code == 429:
        response = await _guarded_call(http_client, url, access_token, body)
        if response.status_code == 429:
            raise RetryableTransportError("kick api rate limited (429)", http_status=429)

    if response.status_code >= 500:
        raise RetryableTransportError(
            f"kick API error: HTTP {response.status_code}", http_status=response.status_code
        )
    if response.status_code >= 400:
        raise NonRetryableTransportError(
            f"kick API error: HTTP {response.status_code}", http_status=response.status_code
        )

    logger.info("kick_send_action.sent chatroom=%s len=%d", chatroom_id, len(text))
    return TransportResult(
        transport="bundle",
        detail=f"kick message sent, chatroom={chatroom_id}",
        http_status=response.status_code,
    )
