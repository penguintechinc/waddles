"""`!iq [@user]` -> a fun, deterministic "IQ" number per user. Stateless.

No `kv` (only `flags.read` is declared). The score is a pure function of the user's UUID
(SHA-256 -> 60..180), so a given user always gets the same number. Purely for fun.

**Users are referenced by UUID, never by username (PII rule).** Bare `!iq` scores the caller
(`_actor_uuid`); `!iq <user>` resolves the target via `_resolve_target()` (literal UUID,
Discord mention, or handle hashed into a UUIDv5 and DISCARDED). Replies show only the UUID's
first 8 hex chars; typed targets are never echoed, stored or logged.

Fail-loud: more than one argument -> usage reply; unidentifiable target -> explicit reply.

Gated behind the PostHog flag ``waddles.command-iq``.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any

from waddle_sdk import log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-iq"
COMMAND = "!iq"

_USAGE = "Usage: !iq [<user>]"

IQ_MIN = 60
IQ_MAX = 180

#: Flavor per 30-point band starting at `IQ_MIN`.
_FLAVOR: tuple[str, ...] = (
    "bold strategy: vibes over thinking",
    "street smart, snack smarter",
    "perfectly average and proud of it",
    "big-brain energy detected",
    "galaxy brain -- please share your secrets",
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
        log.debug("iq.target_not_uuid", platform=platform, error="ValueError")
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


def _compute_iq(user_uuid: str) -> int:
    """Deterministic IQ in `[IQ_MIN, IQ_MAX]` from the user's UUID."""
    digest = hashlib.sha256(f"iq.{user_uuid}".encode()).hexdigest()
    return IQ_MIN + int(digest[:8], 16) % (IQ_MAX - IQ_MIN + 1)


def _flavor(iq: int) -> str:
    """Flavor line for an IQ value, one band per 30 points."""
    return _FLAVOR[min((iq - IQ_MIN) // 30, len(_FLAVOR) - 1)]


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognise `!iq` and build the reply.

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
    if len(args) > 1:
        reply = _USAGE
    else:
        target = _resolve_target(args[0], event.platform) if args else _actor_uuid(event)
        if target is None:
            log.info("iq.bad_target", platform=event.platform)
            reply = _BAD_TARGET_MSG
        else:
            iq = _compute_iq(target)
            reply = f"\U0001f9e0 User {_short(target)} has an IQ of {iq} -- {_flavor(iq)}!"

    log.info("iq.transform matched")
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
        raise ValueError("iq reply requires a channel_id from the inbound chat.message")
    if not isinstance(text, str) or not text:
        raise ValueError("iq reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("iq.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
