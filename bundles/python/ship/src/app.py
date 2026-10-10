"""`!ship <userA> <userB>` -> a fun compatibility percentage. Stateless, deterministic.

No `kv` (only `flags.read` is declared). The percent is a pure function of the two users' UUIDs
(SHA-256 of the sorted UUID pair, so order never matters): the same pair always gets the same
answer. A user shipped with themselves gets 100%.

**Users are referenced by UUID, never by username (PII rule).** Each target is resolved by
`_resolve_target()` (literal UUID, Discord mention, or handle hashed into a UUIDv5 and
DISCARDED). Replies show only each UUID's first 8 hex chars; typed targets are never echoed,
stored or logged -- logs carry only the command name / platform.

Fail-loud: wrong argument count -> usage reply; an unidentifiable target -> explicit reply.

Gated behind the PostHog flag ``waddles.command-ship``.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any

from waddle_sdk import log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-ship"
COMMAND = "!ship"

_USAGE = "Usage: !ship <userA> <userB>"

#: Flavor lines per 20-point tier (`[0,20)` .. `[80,100]`), picked deterministically.
_FLAVOR_TIERS: tuple[tuple[str, ...], ...] = (
    ("a rocky start, but every ship weathers a storm", "the stars are not aligned today"),
    ("potential is buried in there somewhere", "a slow burn -- give it time"),
    ("a solid maybe -- could go either way", "friendly chemistry, who knows where it leads"),
    ("sparks are flying!", "the chat is shipping it"),
    ("written in the stars", "soulmate energy, no notes"),
)
_SELF_FLAVOR = "self-love is important!"

_DISCORD_MENTION_RE = re.compile(r"^<@!?(\d{1,32})>$")
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

_ACTOR_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://waddles.penguintech.io/identity")

_BAD_TARGET_MSG = "I couldn't identify that user - mention them (e.g. @name) or give their UUID."


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
        log.debug("ship.target_not_uuid", platform=platform, error="ValueError")
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


def _compute_match(uuid_a: str, uuid_b: str) -> tuple[int, str]:
    """Deterministic (percent, flavor) for an unordered UUID pair.

    One SHA-256 over the sorted pair drives the percent (first 4 bytes mod 101) and the
    flavor line within the percent's tier (next 4 bytes).
    """
    digest = hashlib.sha256(".".join(sorted((uuid_a, uuid_b))).encode()).hexdigest()
    percent = int(digest[:8], 16) % 101
    lines = _FLAVOR_TIERS[min(percent // 20, len(_FLAVOR_TIERS) - 1)]
    return percent, lines[int(digest[8:16], 16) % len(lines)]


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognise `!ship` and build the reply.

    Exact first-token match comes before the flag check. Malformed input is answered
    explicitly, never silently dropped.
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
    if len(args) != 2:
        reply = _USAGE
    else:
        uuid_a = _resolve_target(args[0], event.platform)
        uuid_b = _resolve_target(args[1], event.platform)
        if uuid_a is None or uuid_b is None:
            log.info("ship.bad_target", platform=event.platform)
            reply = _BAD_TARGET_MSG
        elif uuid_a == uuid_b:
            reply = f"\U0001f498 {_short(uuid_a)} + {_short(uuid_b)} = 100% -- {_SELF_FLAVOR}"
        else:
            percent, flavor = _compute_match(uuid_a, uuid_b)
            reply = f"\U0001f498 {_short(uuid_a)} + {_short(uuid_b)} = {percent}% -- {flavor}!"

    log.info("ship.transform matched")
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
        raise ValueError("ship reply requires a channel_id from the inbound chat.message")
    if not isinstance(text, str) or not text:
        raise ValueError("ship reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("ship.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
