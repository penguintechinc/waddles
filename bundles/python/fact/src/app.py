"""`!fact` -- `!fact` -> a random fun trivia fact from a static built-in list.

Static, SFW, stateless: no kv/db, no AI, no external API. Relay only. Never logs `event.actor`
or any mention token (logs carry only the platform); a mention is echoed back only as the host's
opaque `{user:<token>}` placeholder, which the egress detokenizer renders -- never a raw handle.

Activation-gated only -- no feature flag: the platform's installed -> enabled -> activated check
decides whether this bundle runs.
"""

from __future__ import annotations

import random
from typing import Any

from waddle_sdk import log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, parse_command
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

SPEC = CommandSpec(name="fact")
FACTS: tuple[str, ...] = (
    "Honey never spoils; edible honey has been found in ancient Egyptian tombs.",
    "Octopuses have three hearts and blue blood.",
    "Botanically, bananas are berries, but strawberries are not.",
    "A day on Venus is longer than a year on Venus.",
    "Wombats produce cube-shaped droppings.",
    "Sharks have been around longer than trees.",
    "Sea otters hold hands while they sleep so they don't drift apart.",
    "Lightning can be several times hotter than the surface of the Sun.",
    "A group of flamingos is called a flamboyance.",
    "Scotland's national animal is the unicorn.",
    "Gentoo penguins sometimes offer pebbles to their partners.",
    "Butterflies taste with their feet.",
    "The University of Oxford is older than the Aztec Empire.",
    "Sloths can hold their breath for up to 40 minutes by slowing their heart rate.",
    "Dolphins sleep with half of their brain awake.",
    "Babies are born with more bones than adults have.",
    "The Eiffel Tower can grow a few centimeters taller in hot weather.",
    "Water can boil and freeze at the same time at its triple point.",
    "Bees can recognize human faces.",
    "A bolt of lightning can toast bread, but please do not try it.",
)
_USAGE = "Usage: !fact"
_PREFIX = "\U0001f9e0 "

#: Module RNG -- tests replace it with a seeded `random.Random` for determinism.
_rng: random.Random = random.Random()


def _is_bare(rest: str) -> bool:
    """True if `rest` is empty (and nothing the shared grammar would treat as a verb/argument)."""
    if not rest:
        return True
    try:  # a verb parses cleanly but this command takes no verb/argument -- usage either way
        parse_command(f"!{SPEC.name} {rest}", SPEC)
    except CommandUsageError:
        pass
    return False


def build_reply(rest: str) -> str:
    """Return the reply text for the text after `!fact ` (usage string if malformed)."""
    if not _is_bare(rest):
        return _USAGE
    return _PREFIX + _rng.choice(FACTS)


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: match `!fact`, build the reply.

    Returns `None` for non-matching payloads. A malformed invocation
    gets the usage reply -- never silently dropped, and the malformed text is not echoed.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    head, _, rest = text.strip().partition(" ")
    if head.lower() != f"!{SPEC.name}":
        return None

    reply = build_reply(rest.strip())
    log.info("fact.transform matched", platform=event.platform)
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
            "fact reply requires a channel_id from the inbound chat.message"
        )

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": payload.get("text", "")})
    log.info("fact.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
