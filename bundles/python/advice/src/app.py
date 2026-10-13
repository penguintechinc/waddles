"""`!advice` -> a random piece of advice, relayed to the caller's own origin platform.

Stateless and self-contained: relay only, no kv/db state and NO external API calls -- every
entry comes from the curated, wholesome, original `ADVICE` list baked into this module. Never
echoes or logs `event.actor` or any user text (PII-free logging: platform/count only).
Structure mirrors `bundles/python/eightball` (hand-authored `_entry_wiring.py`, not
`bundle_compiler`).

Gated behind the PostHog flag ``waddles.command-advice`` (new flags default OFF) via
``waddle_sdk.flask_core.feature_flags.feature_enabled``; the flag check runs only after the
cheap command-text match, so an unrelated event never pays for a host round trip. A
flag-disabled `!advice` is indistinguishable from an unrecognized command.

Entries are drawn with the stdlib ``random`` module -- see `eightball`'s own docstring for why
no dedicated WIT random import is needed.
"""

from __future__ import annotations

import random
import re
from typing import Any

from waddle_sdk import log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

#: PostHog flag key, `{product}.{feature-name}` convention.
FLAG_KEY = "waddles.command-advice"

#: Matches `!advice` alone or followed by anything -- trailing text is never inspected.
COMMAND_PATTERN = re.compile(r"^!advice(?:\s+.*)?$", re.IGNORECASE)

#: Reply prefix emoji.
_PREFIX = "💡"

#: Curated, original, wholesome built-in entries.
ADVICE: tuple[str, ...] = (
    "Drink some water. Seriously, right now.",
    "Take a short walk when you feel stuck; fresh air resets your brain.",
    "Be kind: you never know what someone else is dealing with.",
    "Back up your work before you try anything risky.",
    "Sleep on big decisions and look at them again in the morning.",
    "Ask questions; nobody ever regretted asking and learning.",
    "Done is better than perfect, especially for a first draft.",
    "Break big tasks into small steps, then do the first one.",
    "Say thank you more often than you think you need to.",
    "Stretch your shoulders and unclench your jaw.",
    "Listen more than you talk, and you will learn twice as much.",
    "It is okay to take a break; rest is part of the work.",
    "Learn from mistakes, then let them go.",
    "Read the instructions before you start, not after you are stuck.",
    "Keep a little time for something you enjoy every day.",
    "Celebrate small wins; they add up to big ones.",
    "Be patient with beginners; you were one once.",
    "When in doubt, be honest and be gentle about it.",
    "Tidy your workspace and your thoughts get tidier too.",
    "Do not compare your chapter one to someone else's chapter twenty.",
    "Check in on a friend you have not heard from lately.",
    "Practice a little every day rather than a lot once in a while.",
    "Write things down; your future self will thank you.",
    "Smile at someone today; it costs nothing.",
    "Admit when you do not know something and go find out.",
    "Eat a proper meal before making any important decisions.",
    "Take pride in your effort, not only your results.",
    "Give people the benefit of the doubt when you can.",
    "Keep your promises, even the small ones.",
    "Look up from the screen and rest your eyes for a moment.",
    "Surround yourself with people who make you want to be better.",
    "Be the kind of friend you would like to have.",
)


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: `!advice` -> a random built-in entry.

    Returns `None` for any non-matching payload or while the flag is disabled -- never raises
    over an event this bundle was never meant to react to.
    """
    text = event.payload.get("text")
    if not isinstance(text, str) or not COMMAND_PATTERN.match(text.strip()):
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    entry = random.choice(ADVICE)
    log.info("advice.transform matched", platform=event.platform, entries=len(ADVICE))
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"text": f"{_PREFIX} {entry}", "channel_id": event.payload.get("channel_id")},
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
    """Implement `action-stage.dispatch`: relay the reply to the event's own origin platform.

    `config`/`http_client` are accepted but unused -- relays over the WIT `relay` host import
    only, never outbound HTTP.

    Raises:
        ValueError: The envelope's reply payload has no `channel_id`.
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("advice reply requires a channel_id from the inbound chat.message")

    provider = envelope.event.platform
    text = payload.get("text", "")
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("advice.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
