"""`!motivate` -- `!motivate [@user]` -> a random motivational line, optionally aimed at a mentioned user.

Static, SFW, stateless: no kv/db, no AI, no external API. Relay only. Never logs `event.actor`
or any mention token (logs carry only the platform); a mention is echoed back only as the host's
opaque `{user:<token>}` placeholder, which the egress detokenizer renders -- never a raw handle.

Gated behind the PostHog flag `waddles.command-motivate` (default OFF), checked after the command
text matches so an unrelated event never pays for a host round trip.
"""

from __future__ import annotations

import random
from typing import Any

from waddle_sdk import log, relay
from waddle_sdk.command import CommandSpec
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.identity import InvalidIdentityArgError, mention_tokens, validate_token

SPEC = CommandSpec(name="motivate")
FLAG_KEY = "waddles.command-motivate"
LINES: tuple[str, ...] = (
    "Small steps still move you forward.",
    "You have survived every hard day so far; keep going.",
    "Start where you are, use what you have, do what you can.",
    "Progress beats perfection every single time.",
    "The only bad workout is the one you skipped.",
    "Dream big, start small, begin today.",
    "You are one decision away from a different day.",
    "Fall seven times, stand up eight.",
    "Discipline is just choosing what you want most over what you want now.",
    "Your future self is cheering for you right now.",
    "Hard things become easier the more you do them.",
    "Don't wait for motivation; build momentum.",
    "Every expert was once a beginner.",
    "You miss every shot you don't take, so take the shot.",
    "Be stubborn about your goals, flexible about your methods.",
    "Tough times don't last, but tough people do.",
    "One more try is all it takes sometimes.",
    "Courage is taking the next step while still afraid.",
    "You're closer than you were yesterday.",
    "Make today something your future self will thank you for.",
)
_USAGE = "Usage: !motivate [@user]"
_PREFIX = "\U0001f4aa "

#: Module RNG -- tests replace it with a seeded `random.Random` for determinism.
_rng: random.Random = random.Random()


def _mention(rest: str) -> str | None:
    """Return the single mention token if `rest` is exactly one `{user:<token>}`, else `None`."""
    tokens = mention_tokens(rest)
    if len(tokens) != 1:
        return None
    try:
        token = validate_token(tokens[0])
    except InvalidIdentityArgError:
        return None
    return token if rest == "{user:" + tokens[0] + "}" else None


def build_reply(rest: str) -> str:
    """Return the reply text for the text after `!motivate ` (usage string if malformed)."""
    if not rest:
        return _PREFIX + _rng.choice(LINES)
    token = _mention(rest)
    if token is None:
        return _USAGE
    return f"{_PREFIX}{{user:{token}}}, " + _rng.choice(LINES)


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: match `!motivate`, check the flag, build the reply.

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
    log.info("motivate.transform matched", platform=event.platform)
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
            "motivate reply requires a channel_id from the inbound chat.message"
        )

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": payload.get("text", "")})
    log.info("motivate.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
