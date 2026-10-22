"""`!pickup [@user]` -> a cheesy, SFW pickup line, optionally aimed at a mention.

Stateless static-list relay fun, modeled on `bundles/python/eightball`. The target is taken
from the pipeline's `{user:<token>}` mention placeholder via `waddle_sdk.identity.
mention_tokens` -- never a raw username. The reply EMITS the placeholder and the egress
detokenizer renders the name; this bundle never sees, stores or logs a handle (and does not
call `identity.resolve_*`, so it needs no identity permission). A non-placeholder argument
(e.g. a raw `@name` typed where no mention was resolved) gets the usage reply and is NOT echoed.
`event.actor` is never logged.

Gated by the `waddles.command-pickup` feature flag (default OFF), checked after the match.
"""

from __future__ import annotations

import random
from typing import Any

from waddle_sdk import identity, log, relay
from waddle_sdk.command import CommandSpec
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-pickup"
SPEC = CommandSpec(name="pickup")

#: Static, original, family-friendly pickup lines (all cheesy puns, none crude).
LINES: tuple[str, ...] = (
    "Are you a keyboard? Because you're just my type.",
    "Do you have a map? I keep getting lost in your smile.",
    "Are you a campfire? Because you're hot and I want s'more.",
    "Is your name Wi-Fi? Because I'm feeling a strong connection.",
    "Are you a loading screen? Because I'd wait for you.",
    "If you were a vegetable, you'd be a cute-cumber.",
    "Do you believe in love at first sight, or should I walk by again?",
    "Are you a good game? Because I can't stop playing you in my head.",
    "You must be a penguin, because you make my heart waddle.",
    "Are you made of copper and tellurium? Because you're Cu-Te.",
    "Is this chat on fire? Oh no, that's just you.",
    "Are you a bank loan? You've got my interest.",
    "Do you like raisins? How do you feel about a date?",
    "Are you a camera? Every time I look at you, I smile.",
    "If you were a triangle, you'd be acute one.",
    "Are you a charger? Because I'm dying without you.",
    "I must be a snowflake, because I've fallen for you.",
    "Are you a parking ticket? You've got fine written all over you.",
    "Is your name Google? Because you have everything I've been searching for.",
    "Are you a dictionary? You add meaning to my life.",
)

USAGE = "Usage: !pickup [@user]"


def _target_token(rest: str) -> str | None | bool:
    """Return the mention token, `None` for no target, or `False` if malformed.

    Exactly one `{user:<token>}` placeholder and nothing else is valid; any other argument
    (raw text, several mentions) is malformed.
    """
    if not rest:
        return None
    tokens = identity.mention_tokens(rest)
    if len(tokens) != 1:
        return False
    try:
        token = identity.validate_token(tokens[0])
    except identity.InvalidIdentityArgError:
        return False
    if rest != f"{{user:{token}}}":
        return False
    return token


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: `!pickup [@user]` -> a random pickup line.

    Returns `None` for a non-matching payload or while the feature flag is off; a recognized
    but malformed invocation gets a usage reply (the bad input is not echoed).
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    head, _, rest = text.strip().partition(" ")
    if head.lower() != f"!{SPEC.name}":
        return None
    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    target = _target_token(rest.strip())
    if target is False:
        text_out = USAGE
    else:
        line = random.choice(LINES)
        text_out = f"{{user:{target}}}, {line}" if isinstance(target, str) else line
    log.info("pickup.transform matched", platform=event.platform)
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
            "pickup reply requires a channel_id from the inbound chat.message"
        )
    text = payload.get("text")
    if not isinstance(text, str) or not text:
        raise ValueError("pickup reply requires non-empty text")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("pickup.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
