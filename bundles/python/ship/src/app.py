"""`!ship <userA> <userB>` -> a fun compatibility percentage. Stateless, deterministic.

No `kv` (no permission is declared). The percent is a pure function of the two users' UUIDs (SHA-256
of the sorted id pair, so order never matters): the same pair always gets the same answer. A user
shipped with themselves gets 100%.

**Users are referenced by the pipeline's `{user:<id>}` PII token, never by username.** Each
target is the token `pii_tokenize` already substituted for the @mention; replies EMIT
`{user:<id>}` and the egress detokenizer renders names. No local identity derivation (no uuid5);
logs carry only the command name / platform.

Fail-loud: wrong argument count -> usage reply; an unidentifiable target -> explicit reply.

Activation-gated only -- no feature flag: the platform's installed -> enabled -> activated check
decides whether this bundle runs.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from waddle_sdk import log, relay
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

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
    if len(args) != 2:
        reply = _USAGE
    else:
        uuid_a = _parse_user_token(args[0])
        uuid_b = _parse_user_token(args[1])
        if uuid_a is None or uuid_b is None:
            log.info("ship.bad_target", platform=event.platform)
            reply = _BAD_TARGET_MSG
        elif uuid_a == uuid_b:
            reply = f"\U0001f498 {_user_token(uuid_a)} + {_user_token(uuid_b)} = 100% -- {_SELF_FLAVOR}"
        else:
            percent, flavor = _compute_match(uuid_a, uuid_b)
            reply = f"\U0001f498 {_user_token(uuid_a)} + {_user_token(uuid_b)} = {percent}% -- {flavor}!"

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
