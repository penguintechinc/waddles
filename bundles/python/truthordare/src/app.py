"""`!truthordare [truth|dare]` -> a random SFW truth or dare prompt.

Stateless static-list relay fun, modeled on `bundles/python/eightball`. Bare `!truthordare`
picks truth or dare at random; `!truthordare truth` / `!truthordare dare` choose the kind.
`truth`/`dare` are declared as sub-modules on the shared `CommandSpec`, so
`waddle_sdk.command.parse_command` does the grammar. They are NOT enable/disable-gated
(`SubModuleGate` needs kv; this bundle is deliberately stateless) -- both are always available.

The reply never echoes user input and `event.actor` is never logged. Activation-gated only -- no
feature flag: the platform's installed -> enabled ->
activated check decides whether this bundle runs.
"""

from __future__ import annotations

import random
from typing import Any

from waddle_sdk import log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, parse_command
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

SPEC = CommandSpec(name="truthordare", sub_modules=frozenset({"truth", "dare"}))

#: Static, original, family-friendly truth prompts.
TRUTHS: tuple[str, ...] = (
    "What is the most embarrassing song on your playlist?",
    "What is the silliest thing you have ever been afraid of?",
    "What is your most useless talent?",
    "What was the last thing you searched for online?",
    "What is the worst gift you have ever received?",
    "What game do you secretly love but never admit to?",
    "What is the weirdest food combination you enjoy?",
    "What is one thing you would change about your first day at school?",
    "What is the longest you have ever gone without sleep?",
    "What is a habit of yours that others might find odd?",
    "What cartoon character do you relate to most?",
    "What is the most fun you have had in a game?",
    "What is the funniest nickname you have ever had?",
    "If you could swap lives with anyone for a day, who would it be?",
    "What is a fear you wish you could get over?",
    "What is the best prank you have ever seen?",
)

#: Static, original, family-friendly (and safe-to-do-at-a-desk) dare prompts.
DARES: tuple[str, ...] = (
    "Type your next three messages using only emojis.",
    "Talk like a pirate for the next five minutes.",
    "Do your best impression of a penguin and describe it in chat.",
    "Change your nickname to something silly for ten minutes.",
    "Sing the chorus of a song in chat, no peeking at the lyrics.",
    "Say the alphabet backwards in one message.",
    "Write a four-line poem about the last thing you ate.",
    "Describe your day using only movie titles.",
    "Compliment the next three people who chat.",
    "Do ten jumping jacks and report back.",
    "Speak in rhymes for your next five messages.",
    "Share your best dad joke right now.",
    "Draw something with your eyes closed and describe it.",
    "Use only capital letters for the next five minutes.",
    "Invent a new handshake and explain how it works.",
    "Tell a two-sentence story about a penguin who found a hat.",
)

USAGE = "Usage: !truthordare [truth|dare]"


def _kind(text: str) -> str | None:
    """Return `truth`/`dare`/`random` for a valid invocation, `usage` if malformed."""
    try:
        parsed = parse_command(text, SPEC)
    except CommandUsageError:
        log.debug("truthordare.usage", reason="malformed_invocation")
        return "usage"
    if parsed.option is not None:
        return "usage"
    return parsed.sub_module or "random"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: `!truthordare [truth|dare]` -> a random prompt.

    Returns `None` for a non-matching payload; a recognized
    but malformed invocation gets a usage reply (the bad input is not echoed).
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    if text.strip().partition(" ")[0].lower() != f"!{SPEC.name}":
        return None

    kind = _kind(text)
    if kind == "usage":
        text_out = USAGE
    else:
        if kind == "random":
            kind = random.choice(("truth", "dare"))
        pool = TRUTHS if kind == "truth" else DARES
        prompt = random.choice(pool)
        text_out = f"{kind.capitalize()}: {prompt}"
    log.info("truthordare.transform matched", platform=event.platform)
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"text": text_out, "channel_id": event.payload.get("channel_id")},
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

    `config`/`http_client` are accepted but unused -- relays over the WIT `relay` host
    import only, never outbound HTTP.

    Raises:
        ValueError: The reply payload has no `channel_id` or no non-empty `text`.
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError(
            "truthordare reply requires a channel_id from the inbound chat.message"
        )
    text = payload.get("text")
    if not isinstance(text, str) or not text:
        raise ValueError("truthordare reply requires non-empty text")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("truthordare.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
