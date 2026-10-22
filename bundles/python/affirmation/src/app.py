"""`!affirmation` -- `!affirmation` -> a random positive affirmation from a static built-in list.

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

SPEC = CommandSpec(name="affirmation")
AFFIRMATIONS: tuple[str, ...] = (
    "I am worthy of good things.",
    "I am doing my best, and my best is enough.",
    "I can handle whatever today brings.",
    "I am allowed to take up space.",
    "I grow a little stronger every day.",
    "I deserve rest as much as I deserve success.",
    "My effort matters, even when nobody sees it.",
    "I am learning, and that is plenty.",
    "I choose to be kind to myself today.",
    "I am capable of more than I realize.",
    "Mistakes help me grow; they do not define me.",
    "I bring something unique to this world.",
    "I am proud of how far I have come.",
    "I can take this one step at a time.",
    "I am surrounded by possibilities.",
    "I trust myself to figure it out.",
    "I release what I cannot control.",
    "My feelings are valid, and they will pass.",
    "I am enough, exactly as I am.",
    "Today is a fresh start, and I am ready.",
)
_USAGE = "Usage: !affirmation"
_PREFIX = "\u2728 "

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
    """Return the reply text for the text after `!affirmation ` (usage string if malformed)."""
    if not _is_bare(rest):
        return _USAGE
    return _PREFIX + _rng.choice(AFFIRMATIONS)


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: match `!affirmation`, build the reply.

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
    log.info("affirmation.transform matched", platform=event.platform)
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
            "affirmation reply requires a channel_id from the inbound chat.message"
        )

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": payload.get("text", "")})
    log.info("affirmation.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
