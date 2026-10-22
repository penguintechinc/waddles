"""`!dance [@user]` -> a lighthearted flavor-text reply (busts out a dance move), relayed back.

Stateless relay: no `kv`, no db, no egress. `transform()` holds all logic; `dispatch()` is a pure
relay of the text `transform()` built.

Command grammar: `waddle_sdk.command.parse_command` + `CommandSpec` validate the command head (a
bare `!dance` is the action). The optional argument is a single @mention -- the host replaces it with
a `{user:<token>}` placeholder, which `waddle_sdk.identity.mention_tokens` extracts.

**Users are referenced by TOKEN, never by username.** Replies emit `{user:<token>}` placeholders
(the actor's own pipeline token from `event.actor`, and the mention token) and the egress
detokenizer renders display names. This bundle never resolves, stores, or logs a raw handle; log
lines carry only platform + reply shape (`solo`/`targeted`/`usage`).

Activation-gated only -- no feature flag: the platform's installed -> enabled -> activated check
decides whether this bundle runs. No role
badges are consulted -- the command is open to everyone, so there is no mod/broadcaster signal to
read (any future badge check must be a strict `is True`).
"""

from __future__ import annotations

import random
import re
from typing import Any

from waddle_sdk import identity, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, parse_command
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

COMMAND = "dance"
_SPEC = CommandSpec(name=COMMAND)

_USAGE = "Usage: !dance [@user]"

#: Matches a pipeline `{user:<id>}` token (same grammar as `core/egress_detokenizer`).
_USER_TOKEN_RE = re.compile(r"^\{user:[A-Za-z0-9_:.-]+\}$")

#: Used when `event.actor` is not a pipeline token -- never echo an unrecognized raw value.
_FALLBACK_ACTOR = "Someone"

#: Original flavor text written fresh for this bundle. `{actor}`/`{target}` are
#: `str.format()`-substituted with `{user:<token>}` placeholders (code-owned strings).
_SOLO: tuple[str, ...] = (
    "💃 {actor} busts out the robot. Beep boop!",
    "💃 {actor} does a flawless moonwalk across the chat.",
    "💃 {actor} breaks into an enthusiastic penguin shuffle.",
    "💃 {actor} spins, dips, and absolutely nails it.",
)

_TARGETED: tuple[str, ...] = (
    "💃 {actor} grabs {target} for a spontaneous dance!",
    "💃 {actor} and {target} cut a rug together.",
    "💃 {actor} challenges {target} to a dance-off!",
    "💃 {actor} twirls {target} across the dance floor.",
)


def _user_placeholder(token: str) -> str:
    """Render an opaque mention token as the `{user:<token>}` placeholder egress resolves."""
    return f"{{user:{token}}}"


def _actor_ref(actor: object) -> str:
    """Return the actor as a `{user:..}` token if it is one, else a generic fallback."""
    if isinstance(actor, str) and _USER_TOKEN_RE.match(actor.strip()):
        return actor.strip()
    return _FALLBACK_ACTOR


def _build_reply(actor: str, rest: str) -> tuple[str, str]:
    """Return `(reply_text, shape)` for the arguments after `!dance`.

    Shape is `solo` (no argument), `targeted` (exactly one @mention and nothing else) or
    `usage` (anything else -- never silently dropped).
    """
    if not rest:
        template = random.choice(_SOLO)
        return template.format(actor=actor), "solo"
    tokens = identity.mention_tokens(rest)
    if len(tokens) != 1 or rest != _user_placeholder(tokens[0]):
        return _USAGE, "usage"
    template = random.choice(_TARGETED)
    return template.format(actor=actor, target=_user_placeholder(tokens[0])), "targeted"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: `!dance [@user]` -> a flavor-text reply.

    Returns `None` for a non-matching payload. A recognized but
    malformed invocation gets a usage reply.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    head, _, rest = stripped.partition(" ")
    try:
        parse_command(head, _SPEC)
    except CommandUsageError:
        return None

    reply_text, shape = _build_reply(_actor_ref(event.actor), rest.strip())
    log.info("dance.transform matched", platform=event.platform, shape=shape)
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"text": reply_text, "channel_id": event.payload.get("channel_id")},
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
    """Implement `action-stage.dispatch`: relay the reply `transform()` already built.

    `config`/`http_client` are accepted but unused -- relays over the WIT `relay` import only.

    Raises:
        ValueError: The reply payload has no `channel_id`.
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError(
            "dance reply requires a channel_id from the inbound chat.message"
        )
    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": payload.get("text", "")})
    log.info("dance.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
