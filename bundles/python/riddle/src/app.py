"""`!riddle` -> a random riddle; `!riddle answer <n>` -> riddle n's answer. Static + stateless.

Each riddle is shown with its number so the answer can be revealed later without any stored
state. Activation-gated only -- no feature flag: the platform's installed -> enabled -> activated
check decides whether this bundle runs. PII-free
logging: only platform/op are logged, never the actor or the user's text.
"""

from __future__ import annotations

import random

from waddle_sdk import log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, parse_command
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

SPEC = CommandSpec(name="riddle")

#: (riddle, answer) pairs -- original/classic, SFW. Riddle numbers are 1-based list positions.
RIDDLES: tuple[tuple[str, str], ...] = (
    ("What has keys but can't open locks?", "A piano."),
    ("What has hands but can't clap?", "A clock."),
    ("What gets wetter the more it dries?", "A towel."),
    ("What has a head and a tail but no body?", "A coin."),
    ("What can you catch but not throw?", "A cold."),
    ("What has many teeth but can't bite?", "A comb."),
    ("What goes up but never comes down?", "Your age."),
    ("What has one eye but can't see?", "A needle."),
    ("What can travel around the world while staying in a corner?", "A stamp."),
    ("The more you take, the more you leave behind. What am I?", "Footsteps."),
    ("What has a neck but no head?", "A bottle."),
    ("What runs but never walks?", "Water."),
    ("What is full of holes but still holds water?", "A sponge."),
    ("What comes once in a minute, twice in a moment, but never in a thousand years?",
     "The letter M."),
    ("I speak without a mouth and hear without ears. What am I?", "An echo."),
)

USAGE = "Usage: !riddle | !riddle answer <n>"


def _parse_number(arg: str) -> int | None:
    """Return the 1-based riddle number from `arg`, or `None` if invalid/out of range."""
    cleaned = arg.strip().lstrip("#")
    if not cleaned.isascii() or not cleaned.isdigit():
        return None
    number = int(cleaned)
    return number if 1 <= number <= len(RIDDLES) else None


def build_reply(args: str | None, *, rng: random.Random | None = None) -> str:
    """Build the reply: a random riddle for bare input, or an answer for `answer <n>`."""
    if args is None:
        number = (rng or random).randint(1, len(RIDDLES))
        return f"Riddle #{number}: {RIDDLES[number - 1][0]} (answer: !riddle answer {number})"
    sub, _, rest = args.partition(" ")
    if sub.lower() != "answer":
        return USAGE
    number = _parse_number(rest)
    if number is None:
        return f"{USAGE} (n is 1-{len(RIDDLES)})"
    return f"Riddle #{number} answer: {RIDDLES[number - 1][1]}"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: `!riddle [answer <n>]` -> a reply payload."""
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if stripped.partition(" ")[0].lower() != "!riddle":
        return None

    args: str | None
    try:
        parsed_cmd = parse_command(stripped, SPEC)
        is_bare = parsed_cmd.option is None
    except CommandUsageError:
        is_bare = False
    args = None if is_bare else stripped.partition(" ")[2].strip() or None

    log.info("riddle.transform matched", platform=event.platform)
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"text": build_reply(args), "channel_id": event.payload.get("channel_id")},
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
    envelope: StageEnvelope, config: dict[str, object], *, http_client: object
) -> DispatchResult:
    """Implement `action-stage.dispatch`: relay the riddle reply to the origin platform.

    Raises:
        ValueError: The envelope's reply payload has no `channel_id`.
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("riddle reply requires a channel_id from the inbound chat.message")
    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": payload.get("text", "")})
    log.info("riddle.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
