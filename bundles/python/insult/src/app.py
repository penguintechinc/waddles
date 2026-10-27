"""`!insult [@user]` -> a playful insult from a built-in static list. Stateless, relay-only.

No `kv` (no storage permission is declared): the pool is a fixed, original, SFW tuple and
nothing is persisted. Bare `!insult` addresses the caller; `!insult @user` addresses the mentioned
user.

**PII-free.** The bundle never sees or stores a username: both the caller (`event.actor`) and an
@mention reach it as the hub-issued `{user:<token>}` placeholder, which is echoed back verbatim
in the reply for the egress detokenizer to render. A target that is not such a token (a typed
raw handle) is answered with an explicit "mention them" reply -- never echoed, hashed, stored or
logged. Logs carry only the command name and platform.

Grammar: `waddle_sdk.command.parse_command` validates the bare shape (`!insult`); the single
positional @mention is the same documented grammar extension `compliment`/`lastseen` use
(`parse_command`'s bare case takes no argument), detected with `identity.mention_tokens`.

Activation-gated only -- no feature flag: the platform's installed -> enabled -> activated check
decides whether this bundle runs. Fail-loud: bad usage always gets a reply.
"""

from __future__ import annotations

import random
import re
from typing import Any

from waddle_sdk import identity, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, parse_command
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

SPEC = CommandSpec(name="insult")
COMMAND = "!insult"

_USAGE = "Usage: !insult [@user]"
_BAD_TARGET_MSG = "I couldn't identify that user - mention them (e.g. @name)."

_USER_TOKEN_RE = re.compile(r"^\{user:([A-Za-z0-9_:.-]+)\}$")

#: Original, light-hearted, SFW lines -- teasing only: no slurs, harassment, or sensitive topics.
_POOL: tuple[str, ...] = (
    "you have the navigational skills of a penguin on a treadmill.",
    "you're about as stealthy as a marching band in a library.",
    "you argue like a toaster: lots of heat, no real substance.",
    "your cooking is so bad the smoke alarm sends you thank-you notes.",
    "you're the human equivalent of a 'low battery' notification.",
    "you bring the confidence of a lion and the sense of direction of a shopping cart.",
    "you have the grace of a baby giraffe on ice, and about as much plan.",
    "your Wi-Fi password is probably 'password', and honestly that tracks.",
    "you're the reason the instructions say 'do not attempt'.",
    "you lose arguments with autocorrect and then blame the keyboard.",
    "you have two brain cells and they're both on a coffee break.",
    "you're a few fries short of a happy meal, but we like the fries we have.",
    "you could trip over a wireless connection.",
    "your sense of timing is a sundial at midnight.",
    "you're like a cloud: when you disappear, it's a beautiful day.",
    "you put the 'fun' in 'dysfunctional spreadsheet'.",
    "your plans have more plot holes than a cheese grater.",
    "you're not lazy, you're just in permanent power-saving mode.",
)


def _user_ref(raw: object) -> str | None:
    """Return `raw` if it is exactly a `{user:<token>}` placeholder, else `None`."""
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    return cleaned if _USER_TOKEN_RE.match(cleaned) else None


def _pick(pool: tuple[str, ...]) -> str:
    """Pick one entry uniformly at random. Isolated so tests can pin the choice."""
    return random.choice(pool)


def _reply_for(text: str, actor: object) -> str:
    """Build the reply for an already-matched `!insult ...` message (never `None`)."""
    _, _, rest = text.strip().partition(" ")
    rest = rest.strip()
    if rest:
        tokens = identity.mention_tokens(rest)
        if (
            len(rest.split()) != 1
            or len(tokens) != 1
            or rest != "{user:" + tokens[0] + "}"
        ):
            return _BAD_TARGET_MSG if len(rest.split()) == 1 else _USAGE
        who = "{user:" + tokens[0] + "}"
    else:
        try:
            parse_command(text.strip(), SPEC)
        except CommandUsageError as exc:
            log.debug("insult.invalid_grammar", error=type(exc).__name__)
            return _USAGE
        who = _user_ref(actor) or "Hey"
    return f"😏 {who}, {_pick(_POOL)}"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognise `!insult` and build the reply.

    Exact first-token match (so `!insults` never matches) is the cheap precondition.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    head = text.strip().partition(" ")[0]
    if head.lower() != COMMAND:
        return None

    reply = _reply_for(text, event.actor)
    log.info("insult.transform matched", platform=event.platform)
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"channel_id": event.payload.get("channel_id"), "text": reply},
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
    """Implement `action-stage.dispatch`: relay the reply text `transform` already built.

    Raises:
        ValueError: The payload is missing `channel_id` or `text` (defensive).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    text = payload.get("text")
    if not channel_id:
        raise ValueError(
            "insult reply requires a channel_id from the inbound chat.message"
        )
    if not isinstance(text, str) or not text:
        raise ValueError("insult reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("insult.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
