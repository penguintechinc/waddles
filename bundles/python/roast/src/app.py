"""`!roast [@user]` -> a playful, family-friendly roast from a built-in list. Stateless.

No `kv` (only `flags.read` is declared): the roast pool is a fixed, original, SFW tuple and
nothing is persisted. Bare `!roast` roasts the caller; `!roast <user>` roasts the target.

**Users are referenced by UUID, never by username (PII rule).** `_resolve_target()` accepts a
literal UUID, a Discord mention `<@id>`, or a `@name`/`name` handle (hashed into a UUIDv5 and
DISCARDED). Only the resulting UUID's first 8 hex chars ever appear in a reply; the typed
target is never echoed, stored or logged. Logs carry only the command name / error type.

Fail-loud: an unidentifiable target gets an explicit reply (never a silent drop and never a
substituted default target); `!roast` with more than one argument gets a usage reply.

Gated behind the PostHog flag ``waddles.command-roast``.
"""

from __future__ import annotations

import random
import re
import uuid
from typing import Any

from waddle_sdk import log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-roast"
COMMAND = "!roast"

_USAGE = "Usage: !roast [<user>]"

#: Original, light-hearted, SFW roasts -- teasing, never cruel, no slurs or sensitive topics.
_ROASTS: tuple[str, ...] = (
    "you bring so much energy to the chat -- mostly the kind that trips over its own cape.",
    "your game plan is like a penguin on roller skates: bold, fast, and slightly off course.",
    "you type like you're wearing oven mitts, and yet somehow we still love you.",
    "your aim is so creative the target keeps checking if it's the intended one.",
    "you're the reason the tutorial has a 'skip' button and a 'please don't' button.",
    "if enthusiasm were skill, you'd be a legend. Good news: enthusiasm counts for something!",
    "your loadout looks like it was picked blindfolded during an earthquake.",
    "you've got the confidence of a pro and the cooldowns of a toaster.",
    "you could get lost in a one-room map, and we'd still cheer for you.",
    "your strategy is 'press everything and hope', and honestly it works more than it should.",
    "you're proof that chaos is a valid playstyle.",
    "you lag behind the group chat like a snail carrying a piano. Adorable, though.",
    "your jokes arrive late but dressed so well we forgive them.",
    "you rage-quit a pillow fight once -- the pillow still talks about it.",
    "you're not slow, you're just enjoying the scenery more than everyone else.",
    "your inventory is 90% snacks and 10% regrets, and we respect that ratio.",
    "you speedrun naps better than any boss fight.",
    "you read the manual upside down and still somehow won. Keep being you.",
)

_DISCORD_MENTION_RE = re.compile(r"^<@!?(\d{1,32})>$")
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

_ACTOR_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://waddles.penguintech.io/identity")

_BAD_TARGET_MSG = "I couldn't identify that user - mention them (e.g. @name) or give their UUID."


def _actor_uuid(event: PlatformEvent) -> str:
    """Stable pseudonymous UUID for the event's actor (same derivation as `_resolve_target`)."""
    author_id = event.payload.get("author_id")
    if event.platform == "discord" and isinstance(author_id, str) and author_id:
        basis = f"discord:{author_id}"
    else:
        basis = f"{event.platform}:name:{(event.actor or 'anonymous').strip().lower()}"
    return str(uuid.uuid5(_ACTOR_NAMESPACE, basis))


def _resolve_target(raw: str, platform: str) -> str | None:
    """Resolve a typed target to a UUID string, or `None` if it isn't a recognisable user.

    The raw text is hashed and dropped here -- callers only ever see the UUID, never the typed
    username/handle (PII rule). Order: literal UUID, Discord mention, then bare/`@` handle
    (UUIDv5 of the lower-cased handle, a non-reversible pseudonym until #429 lands).
    """
    token = raw.strip()
    try:
        return str(uuid.UUID(token))
    except ValueError:
        log.debug("roast.target_not_uuid", platform=platform, error="ValueError")
    mention = _DISCORD_MENTION_RE.match(token)
    if mention:
        return str(uuid.uuid5(_ACTOR_NAMESPACE, f"discord:{mention.group(1)}"))
    handle = token.removeprefix("@")
    if _HANDLE_RE.match(handle):
        return str(uuid.uuid5(_ACTOR_NAMESPACE, f"{platform}:name:{handle.lower()}"))
    return None


def _short(user_uuid: str) -> str:
    """First 8 hex chars of a UUID -- the only user reference ever shown in a reply."""
    return user_uuid[:8]


def _pick(pool: tuple[str, ...]) -> str:
    """Pick one entry uniformly at random. Isolated so tests can pin the choice."""
    return random.choice(pool)  # noqa: S311 - a game, not a security decision


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognise `!roast` and build the reply.

    Exact first-token match (so `!roasts` never matches) comes before the flag check. A
    malformed or unidentifiable target is answered explicitly, never silently dropped.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    head, _, rest = text.strip().partition(" ")
    if head.lower() != COMMAND:
        return None
    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    args = rest.split()
    if len(args) > 1:
        reply = _USAGE
    else:
        target = _resolve_target(args[0], event.platform) if args else _actor_uuid(event)
        if target is None:
            log.info("roast.bad_target", platform=event.platform)
            reply = _BAD_TARGET_MSG
        else:
            reply = f"\U0001f525 Roast for user {_short(target)}: {_pick(_ROASTS)}"

    log.info("roast.transform matched")
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
        ValueError: The payload is missing `channel_id` or `text` (defensive -- `transform`
            always sets both).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    text = payload.get("text")
    if not channel_id:
        raise ValueError("roast reply requires a channel_id from the inbound chat.message")
    if not isinstance(text, str) or not text:
        raise ValueError("roast reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("roast.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
