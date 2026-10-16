"""`!iq [@user]` -> a fun, deterministic "IQ" number per user. Stateless.

No `kv` (no permission is declared). The score is a pure function of the user's token id (SHA-256
-> 60..180), so a given user always gets the same number. Purely for fun.

**Users are referenced by the pipeline's `{user:<id>}` PII token, never by username.** Bare `!iq`
scores the caller (the token in `event.actor`); `!iq <user>` takes the `{user:<id>}` token the
pipeline already substituted for the @mention. Replies EMIT `{user:<id>}` and the egress
detokenizer renders the display name. No local identity derivation (no uuid5).

Fail-loud: more than one argument -> usage reply; unidentifiable target/actor -> explicit reply.

Activation-gated only -- no feature flag: the platform's installed -> enabled -> activated check
decides whether this bundle runs.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from waddle_sdk import log, relay
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

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

_USER_TOKEN_RE = re.compile(r"^\{user:([A-Za-z0-9_:.-]+)\}$")


def _parse_user_token(raw: object) -> str | None:
    """Extract the bare id from a `{user:<id>}` PII token, or `None` if `raw` isn't one.

    Grammar matches `core/egress_detokenizer`'s `{user:` + token chars (alphanumeric and
    `-_:.`) + `}`. The pipeline's `pii_tokenize` pass puts this token in `event.actor` and in
    any @mention; hub-api resolves it, so the bundle never derives its own identity.
    """
    if not isinstance(raw, str):
        return None
    match = _USER_TOKEN_RE.match(raw.strip())
    return match.group(1) if match else None


def _user_token(user_id: str) -> str:
    """Render a bare id as the `{user:<id>}` placeholder the egress detokenizer resolves."""
    return f"{{user:{user_id}}}"


_BAD_TARGET_MSG = "I couldn't identify that user - mention them (e.g. @name)."
_NO_ACTOR_MSG = "I couldn't identify you, so I can't work out an IQ."


def _compute_iq(user_uuid: str) -> int:
    """Deterministic IQ in `[IQ_MIN, IQ_MAX]` from the user's token id."""
    digest = hashlib.sha256(f"iq.{user_uuid}".encode()).hexdigest()
    return IQ_MIN + int(digest[:8], 16) % (IQ_MAX - IQ_MIN + 1)


def _flavor(iq: int) -> str:
    """Flavor line for an IQ value, one band per 30 points."""
    return _FLAVOR[min((iq - IQ_MIN) // 30, len(_FLAVOR) - 1)]


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognise `!iq` and build the reply.

    Exact first-token match is the only precondition. Malformed input is answered
    explicitly, never silently dropped.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    head, _, rest = text.strip().partition(" ")
    if head.lower() != COMMAND:
        return None

    args = rest.split()
    if len(args) > 1:
        reply = _USAGE
    else:
        target = _parse_user_token(args[0] if args else event.actor)
        if target is None:
            log.info("iq.bad_target", platform=event.platform, has_arg=bool(args))
            reply = _BAD_TARGET_MSG if args else _NO_ACTOR_MSG
        else:
            iq = _compute_iq(target)
            reply = f"\U0001f9e0 User {_user_token(target)} has an IQ of {iq} -- {_flavor(iq)}!"

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
