"""`!bot` -> static bot name/version/links.

Stateless builtin (no kv/db, no egress): `transform()` builds the reply, `dispatch()` is a pure
relay -- same split as `bundles/python/boop`/`wave`. Gated behind the PostHog flag
``waddles.command-bot`` (default OFF; cheap command-match first, flag check second).

PII-free logs: only platform + resolved reply shape are logged -- never the message text,
`event.actor`, or any caller-typed argument. (The reply itself may legitimately contain
caller-typed text, going back to the same public channel it came from.)
"""

from __future__ import annotations

from typing import Any

from waddle_sdk import log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

#: PostHog flag key, `{product}.{feature-name}` convention.
FLAG_KEY = "waddles.command-bot"

#: Command tokens this bundle answers to (lower-cased match on the first whitespace token).
_COMMANDS: tuple[str, ...] = ('!bot',)

BOT_NAME = "Waddles"
BOT_VERSION = "3.0"
BOT_LINKS = "https://github.com/penguintechinc/waddles | https://penguintech.io"
BOT_INFO = f"{BOT_NAME} v{BOT_VERSION} -- a modular, multi-platform community bot. Links: {BOT_LINKS}"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!bot` and build the reply.

    Returns `None` (event dropped) for a non-matching payload or while the `waddles.command-bot` flag is
    off -- never raises over an event this bundle was never meant to react to.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None

    stripped = text.strip()
    head, _, rest = stripped.partition(" ")
    if head.lower() not in _COMMANDS:
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    rest = rest.strip()
    reply_text, shape = BOT_INFO, "info"
    log.info("bot.transform matched", platform=event.platform, shape=shape)

    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"text": reply_text, "channel_id": event.payload.get("channel_id")},
        occurred_at=event.occurred_at,
    )


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result -- see `pyping`'s own `app.py`."""

    __slots__ = ("detail", "http_status", "sub_type", "transport")

    def __init__(self, *, transport: str, detail: str) -> None:
        """Record which provider the reply was relayed to, and a short detail string."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = None


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: relay the reply `transform()` already built.

    Raises:
        ValueError: The envelope's reply payload has no `channel_id`.
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("bot reply requires a channel_id from the inbound chat.message")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": payload.get("text", "")})
    log.info("bot.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
