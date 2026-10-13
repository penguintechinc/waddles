"""`!mood` -> a random mood / vibe from a built-in list. Stateless.

No `kv` (only `flags.read` is declared) and no user reference at all: the reply names no one,
so there is no username/PII surface. Logs carry only the command name.

Fail-loud: `!mood <anything>` gets a usage reply rather than being silently ignored.

Gated behind the PostHog flag ``waddles.command-mood``.
"""

from __future__ import annotations

import random
from typing import Any

from waddle_sdk import log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-mood"
COMMAND = "!mood"

_USAGE = "Usage: !mood"

_MOODS: tuple[str, ...] = (
    "\U0001f60e smooth like a penguin sliding on fresh ice",
    "\U0001f634 running on 3% battery and a dream",
    "\U0001f929 sparkling with unreasonable optimism",
    "\U0001f914 deep in thought about snacks",
    "\U0001f624 ready to conquer the day, or at least the next level",
    "\U0001f60c peacefully vibing, no notes",
    "\U0001f973 party mode: engaged",
    "\U0001f605 pretending everything is under control",
    "\U0001f9d8 zen master of the chat",
    "\U0001f92a chaotic good, extra chaos",
    "\U0001f970 warm, fuzzy, and a little bit sleepy",
    "\U0001f60f smugly confident for no reason",
    "\U0001f917 big hug energy",
    "\U0001f9d0 suspiciously curious",
    "\U0001f31e sunshine with a side of coffee",
    "\U0001f438 feeling very froggy today",
)


def _pick(pool: tuple[str, ...]) -> str:
    """Pick one entry uniformly at random. Isolated so tests can pin the choice."""
    return random.choice(pool)  # noqa: S311 - a game, not a security decision


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognise `!mood` and build the reply.

    Exact first-token match comes before the flag check.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    head, _, rest = text.strip().partition(" ")
    if head.lower() != COMMAND:
        return None
    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    reply = _USAGE if rest.strip() else f"Current mood: {_pick(_MOODS)}"

    log.info("mood.transform matched")
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"channel_id": event.payload.get("channel_id"), "text": reply},
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
    """Implement `action-stage.dispatch`: relay the reply text `transform` already built.

    Raises:
        ValueError: The payload is missing `channel_id` or `text` (defensive -- `transform`
            always sets both).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    text = payload.get("text")
    if not channel_id:
        raise ValueError("mood reply requires a channel_id from the inbound chat.message")
    if not isinstance(text, str) or not text:
        raise ValueError("mood reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("mood.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
