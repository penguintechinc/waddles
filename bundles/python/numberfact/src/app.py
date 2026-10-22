"""`!numberfact [n]` -> a fun fact about a number. Static + stateless, no external API.

Curated facts exist for a set of notable numbers; any other integer gets a deterministic fact
computed from its own properties (parity, primality, perfect square, ...). Bare `!numberfact`
picks a random curated number. Activation-gated only -- no feature flag: the platform's installed ->
enabled -> activated check decides whether this bundle runs. PII-free logging: only platform/op are
logged, never the actor or the text.
"""

from __future__ import annotations

import math
import random

from waddle_sdk import log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, parse_command
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

SPEC = CommandSpec(name="numberfact")
MAX_ABS = 10**12
USAGE = "Usage: !numberfact [n]"

#: Curated, SFW facts for notable numbers.
FACTS: dict[int, str] = {
    0: "0 is the only number that is neither positive nor negative.",
    1: "1 is the multiplicative identity: anything times 1 is itself.",
    2: "2 is the only even prime number.",
    3: "3 is the smallest odd prime, and a triangle has exactly 3 sides.",
    4: "4 is the smallest composite number and the only square equal to its own number of letters.",
    5: "5 is the number of Platonic solids.",
    6: "6 is the smallest perfect number: 1 + 2 + 3 = 6.",
    7: "7 is the number of days in a week and the fourth prime.",
    8: "8 is a cube (2 x 2 x 2) and an octopus has 8 arms.",
    9: "9 is the largest single-digit number and a square (3 x 3).",
    10: "10 is the base of our everyday number system.",
    12: "12 is the number of months in a year and has six divisors.",
    13: "13 is a prime and the number of cards in each suit of a standard deck.",
    24: "24 is the number of hours in a day.",
    28: "28 is a perfect number: 1 + 2 + 4 + 7 + 14 = 28.",
    42: "42 is the answer to life, the universe and everything in a famous sci-fi novel.",
    64: "64 is the number of squares on a chessboard.",
    100: "100 is the number of centimeters in a meter.",
    144: "144 is a Fibonacci number and also a perfect square (12 x 12).",
    360: "360 is the number of degrees in a full circle.",
    365: "365 is the number of days in a common year.",
    1000: "1000 is the number of meters in a kilometer.",
    1024: "1024 is 2 to the 10th power, the number of bytes in a kibibyte.",
}


def _is_prime(n: int) -> bool:
    """Trial-division primality for the bounded input range."""
    if n < 2:
        return False
    if n % 2 == 0:
        return n == 2
    return all(n % d for d in range(3, math.isqrt(n) + 1, 2))


def fact_for(n: int) -> str:
    """Return a fact for `n`: the curated one if present, else a computed property fact."""
    if n in FACTS:
        return FACTS[n]
    if n < 0:
        return f"{n} is negative; its absolute value is {-n}."
    if _is_prime(n):
        return f"{n} is a prime number: only divisible by 1 and itself."
    root = math.isqrt(n)
    if root * root == n:
        return f"{n} is a perfect square ({root} x {root})."
    parity = "even" if n % 2 == 0 else "odd"
    return f"{n} is a composite {parity} number, between {root}^2 and {(root + 1)}^2."


def build_reply(arg: str | None, *, rng: random.Random | None = None) -> str:
    """Build the reply for the optional number argument (random curated fact if absent)."""
    if arg is None:
        n = (rng or random).choice(sorted(FACTS))
        return fact_for(n)
    cleaned = arg.strip()
    body = cleaned[1:] if cleaned[:1] in "+-" else cleaned
    if not body or not body.isascii() or not body.isdigit():
        return USAGE
    n = int(cleaned)
    if abs(n) > MAX_ABS:
        return f"{USAGE} (|n| up to {MAX_ABS})"
    return fact_for(n)


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: `!numberfact [n]` -> a fact reply payload."""
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if stripped.partition(" ")[0].lower() != "!numberfact":
        return None

    arg: str | None
    try:
        parsed_cmd = parse_command(stripped, SPEC)
        is_bare = parsed_cmd.option is None
    except CommandUsageError:
        is_bare = False
    arg = None if is_bare else stripped.partition(" ")[2].strip() or None

    log.info("numberfact.transform matched", platform=event.platform)
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"text": build_reply(arg), "channel_id": event.payload.get("channel_id")},
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
    """Implement `action-stage.dispatch`: relay the fact to the origin platform.

    Raises:
        ValueError: The envelope's reply payload has no `channel_id`.
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("numberfact reply requires a channel_id from the inbound chat.message")
    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": payload.get("text", "")})
    log.info("numberfact.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
