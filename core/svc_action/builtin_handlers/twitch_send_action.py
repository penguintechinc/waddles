"""builtin_handlers/twitch_send_action.py -- OUTBOUND `irc` chat send, relayed to svc-ingest.

Built-in action-stage handler contract: `async def <name>(envelope,
config, *, http_client) -> TransportResult` (`runner.py`), same as
`discord_send_action.py`'s own `send_message` -- but its OWN
implementation does NOT call an HTTP API the way Discord's does. Twitch
chat send relays through Valkey to svc-ingest's own `outbound_drain.py`
(which sends via a fresh `waddle_transports.transports.irc.IrcTransport`
connection) rather than calling Twitch's Helix REST API directly --
svc-action never holds Twitch credentials or opens its own IRC
connections. Uses the shared `waddle_transports.transports.irc_relay.
RelayOutboundIrcTransport` as its own delivery mechanism -- one of the
two documented patterns `runner.py`'s own module docstring describes for
a built-in action handler ("a built-in action handler's own module may
import and call [transport primitives] for its actual delivery
mechanism"), the other being Discord's "implement its own
connector-specific API logic entirely".

Replies to the ORIGIN channel: `envelope.event.payload["channel_name"]`
(the channel the triggering chat message came from, carried through
process->action by `builtin_handlers/twitch_ingest.py::normalize()`'s own payload
shape)
takes precedence over the handler's static `config["channel"]` default --
a reply belongs in the channel the request came from, not a hardcoded
one; the static config value only matters for an action with no
triggering chat message (e.g. a scheduled announcement). Mirrors
`discord_send_action.py`'s own `channel_id` reply-in-place precedence
exactly.

Seeded via `config/postgres/migrations/083_discord_twitch_demo_convergence.sql`
(on the merged `feature/v3-svc-gateway-discord` branch) as
`waddles.bot.twitch.default`'s `stages.action.entrypoint`, alongside the
SAME app_id's ingest/process stages -- T8 convergence unified what used
to be a separate action-only migration onto the connector's one app_id.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx
import redis.asyncio as redis
from flask_core import StageEnvelope
from waddle_transports import NonRetryableTransportError, TransportResult
from waddle_transports.transports.irc_relay import RelayOutboundIrcTransport

#: Lazily-built, process-wide Valkey client -- `send_message`'s own
#: signature (`(envelope, config, *, http_client)`) is the same one every
#: other built-in action handler uses (`runner.py::_handle_envelope`'s
#: fixed call shape), so a redis client is not passed in; built here
#: instead of per-call to avoid opening a new connection on every
#: dispatched message.
_redis_client: redis.Redis | None = None


def _get_redis_client(config: Mapping[str, Any]) -> redis.Redis:
    """Build (once) or return the cached Valkey client for the outbound IRC relay.

    Reads `VALKEY_URL`/`REDIS_URL` env vars, mirroring `config.py`
    `ActionConfig.from_env`'s own fallback chain. Tests never exercise
    this real network path -- they monkeypatch `_get_redis_client` itself
    (module-level function, easy `monkeypatch.setattr` target) to return
    a `fakeredis.FakeAsyncRedis` instead, matching `discord_send_action.py`'s
    own `guarded_request` monkeypatch precedent.
    """
    global _redis_client
    if _redis_client is None:
        import os

        url = (
            os.environ.get("VALKEY_URL")
            or os.environ.get("REDIS_URL")
            or "redis://localhost:6379/0"
        )
        _redis_client = redis.from_url(url, decode_responses=True)
    return _redis_client


async def send_message(
    envelope: StageEnvelope,
    config: Mapping[str, Any],
    *,
    http_client: httpx.AsyncClient,
) -> TransportResult:
    """Reply in-place: relay `envelope.event.payload["text"]` to the origin (or configured) channel.

    `channel` resolution: `envelope.event.payload["channel_name"]` (the
    triggering message's own channel) first, falling back to the handler's
    static `config["channel"]` default. `http_client` is accepted (and
    unused) only because `runner.py::_handle_envelope` always passes it --
    every action-stage entrypoint shares that one call signature.

    Raises `NonRetryableTransportError` for a config error or an empty
    event payload `text`; a `RetryableTransportError`/
    `NonRetryableTransportError` the relay itself raises propagates
    unchanged -- `runner.py`'s own `retry_with_backoff` wrapper catches
    `waddle_transports`' error types directly, same contract every
    action-stage entrypoint follows. On success, returns the transport's
    own `TransportResult` unwrapped.
    """
    event_payload = envelope.event.payload
    payload_channel = event_payload.get("channel_name")
    channel = payload_channel if isinstance(payload_channel, str) and payload_channel else None
    if channel is None:
        config_channel = config.get("channel")
        channel = config_channel if isinstance(config_channel, str) and config_channel else None
    if not channel:
        raise NonRetryableTransportError(
            "twitch bundle could not resolve a channel from either "
            "envelope.event.payload['channel_name'] (reply-in-place) or "
            "config['channel'] (fallback)"
        )

    text = event_payload.get("text")
    if not isinstance(text, str) or not text:
        raise NonRetryableTransportError(
            "action envelope event.payload missing required 'text' string"
        )

    transport = RelayOutboundIrcTransport(provider="twitch", redis_client=_get_redis_client(config))
    return await transport.send({"channel": channel}, {"text": text})
