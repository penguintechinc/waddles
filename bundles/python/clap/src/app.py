"""`!clap <text>` -> puts a clap between each word (hello world -> hello clap world clap).

Pure, deterministic text transform: relay only, no kv/db, no egress, no AI. `transform()` matches
the command and builds the reply, `dispatch()` is a pure relay (same split as `echo`).

PII-free logs: only platform, op, input length and resolved reply shape (plus community id in
`dispatch`) are logged -- never the message text, `event.actor`, or any typed argument. The reply
itself carries caller-typed text back to the same public channel it came from.

Gated by the `waddles.command-clap` feature flag (default OFF), checked after the command matches
so an unrelated event never pays for a host round trip.
"""

from __future__ import annotations

from typing import Any

from waddle_sdk import log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, parse_command
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-clap"
SPEC = CommandSpec(name="clap")

MAX_INPUT_LEN = 200

_USAGE = "Usage: !clap <text>"
_TOO_LONG = f"clap text must be {MAX_INPUT_LEN} characters or fewer"
#: Twitch/IRC treat a leading `/` or `.` as a chat command and `!` would re-trigger other bots;
#: a transformed reply starting with one would let a caller make the bot run commands as itself.
_UNSAFE_LEAD = ("/", ".", "!")
_UNSAFE_MSG = "clap can't produce a reply starting with / . or !"

_CLAP = "\U0001f44f"


def apply_transform(text: str) -> str:
    """Join the words of `text` with a clap emoji between each, and one trailing clap."""
    words = text.split()
    return f" {_CLAP} ".join(words) + f" {_CLAP}"


def _build_reply(args: str | None) -> tuple[str, str]:
    """Validate `args` and return `(reply_text, shape)`; `shape` is the PII-free log label.

    Raises:
        CommandUsageError: No text was supplied (message is the ready-to-send usage reply).
    """
    if not args:
        raise CommandUsageError(_USAGE)
    if len(args) > MAX_INPUT_LEN:
        return _TOO_LONG, "too_long"
    reply = apply_transform(args)
    if reply.startswith(_UNSAFE_LEAD):
        return _UNSAFE_MSG, "unsafe"
    return reply, "clap"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!clap` and build the transformed reply.

    Returns `None` (event dropped) for a non-matching payload or while the flag is off -- never
    raises over an event this bundle was never meant to react to.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None

    head, _, rest = text.strip().partition(" ")
    try:
        # The command head is validated by the shared parser; the free-text remainder is taken
        # verbatim (a leading word like "add" must transform, not be read as a verb).
        parse_command(head, SPEC)
    except CommandUsageError:
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    args = rest.strip() or None
    try:
        reply_text, shape = _build_reply(args)
    except CommandUsageError as exc:
        reply_text, shape = str(exc), "usage"
    # Length + shape only -- NEVER the text itself (PII-free logs).
    log.info(
        "clap.transform matched",
        platform=event.platform,
        shape=shape,
        length=len(args or ""),
    )

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

    Raises:
        ValueError: The envelope's reply payload has no `channel_id`.
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError(
            "clap reply requires a channel_id from the inbound chat.message"
        )

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": payload.get("text", "")})
    log.info(
        "clap.dispatch relayed", platform=provider, community_id=envelope.community
    )
    return DispatchResult(transport=provider, detail="relayed")
