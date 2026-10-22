"""`!horoscope` -- `!horoscope <sign>` -> a generic, clearly-for-fun horoscope for one of the 12 zodiac signs.

Static, SFW, stateless: no kv/db, no AI, no external API. Relay only. Never logs `event.actor`
or any mention token (logs carry only the platform); a mention is echoed back only as the host's
opaque `{user:<token>}` placeholder, which the egress detokenizer renders -- never a raw handle.

Gated behind the PostHog flag `waddles.command-horoscope` (default OFF), checked after the command
text matches so an unrelated event never pays for a host round trip.
"""

from __future__ import annotations

import random
from typing import Any

from waddle_sdk import log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, parse_command
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

SPEC = CommandSpec(name="horoscope")
FLAG_KEY = "waddles.command-horoscope"
LINES: tuple[str, ...] = (
    "Today, a small kindness comes back around.",
    "Snacks will taste better than expected.",
    "You will find the exact song your mood needs.",
    "A tiny win is hiding in your to-do list.",
    "Someone will laugh at your joke, even the bad one.",
    "Your favorite chair is calling; answer it.",
    "A good idea arrives while you are doing something boring.",
    "Today, trust the plan you already made.",
    "Expect a pleasant surprise near the end of the day.",
    "The universe suggests a glass of water and a stretch.",
    "A message you have been waiting for may finally show up.",
    "Take the scenic route; it pays off.",
)
_USAGE = (
    "Usage: !horoscope <sign> (aries, taurus, gemini, cancer, leo, virgo, "
    "libra, scorpio, sagittarius, capricorn, aquarius, pisces)"
)
_PREFIX = "\U0001f52e "

#: Module RNG -- tests replace it with a seeded `random.Random` for determinism.
_rng: random.Random = random.Random()

_SIGNS: tuple[tuple[str, str, str], ...] = (
    ("aries", "Aries", "bold and ready to lead"),
    ("taurus", "Taurus", "steady and fond of comfort"),
    ("gemini", "Gemini", "curious and chatty"),
    ("cancer", "Cancer", "caring and a little sentimental"),
    ("leo", "Leo", "radiant and full of flair"),
    ("virgo", "Virgo", "thoughtful and detail-minded"),
    ("libra", "Libra", "charming and fair-minded"),
    ("scorpio", "Scorpio", "intense and determined"),
    ("sagittarius", "Sagittarius", "adventurous and upbeat"),
    ("capricorn", "Capricorn", "driven and dependable"),
    ("aquarius", "Aquarius", "inventive and independent"),
    ("pisces", "Pisces", "dreamy and compassionate"),
)
_BY_KEY: dict[str, tuple[str, str]] = {
    key: (name, trait) for key, name, trait in _SIGNS
}


def build_reply(rest: str) -> str:
    """Return a for-fun horoscope for `rest` (one sign name), or the usage string listing all 12."""
    key = rest.strip().lower()
    if not key or " " in key:
        return _USAGE
    sign = _BY_KEY.get(key)
    if sign is None:
        try:
            parse_command(f"!{SPEC.name} {rest}", SPEC)
        except CommandUsageError:
            pass
        return _USAGE
    name, trait = sign
    return f"{_PREFIX}{name} ({trait}) -- {_rng.choice(LINES)} (just for fun!)"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: match `!horoscope`, check the flag, build the reply.

    Returns `None` for non-matching payloads or while the flag is off. A malformed invocation
    gets the usage reply -- never silently dropped, and the malformed text is not echoed.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    head, _, rest = text.strip().partition(" ")
    if head.lower() != f"!{SPEC.name}":
        return None
    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    reply = build_reply(rest.strip())
    log.info("horoscope.transform matched", platform=event.platform)
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"text": reply, "channel_id": event.payload.get("channel_id")},
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

    Raises:
        ValueError: The reply payload has no `channel_id`.
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError(
            "horoscope reply requires a channel_id from the inbound chat.message"
        )

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": payload.get("text", "")})
    log.info("horoscope.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
