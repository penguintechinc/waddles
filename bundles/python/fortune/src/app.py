"""`!fortune` -> a random SFW fortune-cookie line, relayed to the caller's own origin platform.

Stateless (no kv/db), never echoes or logs `event.actor` or any user text -- logs carry only the
platform name. Modeled on `bundles/python/roll`/`eightball`. Randomness via stdlib ``random``.

Gated behind the PostHog flag ``waddles.command-fortune`` (command match first, flag second).
"""

from __future__ import annotations

import random
import re
from typing import Any

from waddle_sdk import log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-fortune"
COMMAND_PATTERN = re.compile(r"^!fortune(?:\s+.*)?$", re.IGNORECASE)

FORTUNES: tuple[str, ...] = (
    "A smooth sea never made a skilled sailor.",
    "Good things come to those who wait, and to those who bring snacks.",
    "You will soon discover a hidden talent.",
    "A pleasant surprise is waiting for you around the corner.",
    "Your hard work will be rewarded sooner than you think.",
    "The best time to start was yesterday; the next best time is now.",
    "A friend is a gift you give yourself.",
    "Today is a fine day to try something new.",
    "Small steps every day lead to big journeys.",
    "Fortune favors the curious.",
    "An unexpected message will brighten your week.",
    "You will find joy in an ordinary moment.",
    "Patience is the companion of wisdom.",
    "Your kindness will return to you tenfold.",
    "A new opportunity is closer than it appears.",
    "Laughter is the shortest distance between two people.",
    "Trust the process; the pieces are falling into place.",
    "You are braver than you believe.",
    "Someone is thinking of you fondly right now.",
    "The journey matters as much as the destination.",
    "Listen more than you speak and you will learn twice as much.",
    "A bug you cannot find today will reveal itself tomorrow.",
    "Great adventures begin with a single click.",
    "Your next idea will be your best one yet.",
    "Share your joy and it will double.",
    "A waddle a day keeps the worries away.",
    "The stars favor you this evening.",
    "Take a break; clarity follows rest.",
    "You will make someone smile today without even trying.",
    "Success is a series of small wins stacked together.",
)


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: `!fortune` -> a random fortune reply.

    Returns `None` for any non-`!fortune` payload or while `waddles.command-fortune`
    is disabled -- never raises over an event this bundle wasn't meant to react to.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    if COMMAND_PATTERN.match(text.strip()) is None:
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    fortune = random.choice(FORTUNES)
    log.info("fortune.transform matched", platform=event.platform)
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={
            "text": f"\U0001f960 {fortune}",
            "channel_id": event.payload.get("channel_id"),
        },
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
    """Implement `action-stage.dispatch`: relay the fortune reply to the event's origin platform.

    Raises:
        ValueError: The envelope's reply payload has no `channel_id`.
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError(
            "fortune reply requires a channel_id from the inbound chat.message"
        )

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": payload.get("text", "")})
    log.info("fortune.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
