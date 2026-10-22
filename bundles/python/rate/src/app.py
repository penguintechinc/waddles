"""`!rate <thing>` -> "I rate <thing> X/10", deterministic per input, relayed to the origin platform.

Stateless (no kv/db/egress), SFW. The score is derived from a SHA-256 of the normalized input, so
the same thing always gets the same rating. Gated by `waddles.command-rate` (default OFF) after the
command matches. PII-free logging: only the platform/op are logged, never the actor or the text.
The reply echoes the input into chat (not into logs); `@` is defused so a rating can't mass-ping.
"""

from __future__ import annotations

import hashlib

from waddle_sdk import log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, parse_command
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

SPEC = CommandSpec(name="rate")
FLAG_KEY = "waddles.command-rate"
MAX_THING_LEN = 100
USAGE = "Usage: !rate <thing>"

#: Tasteful flavor text per score, indexed by the 0-10 score.
FLAVOR: tuple[str, ...] = (
    "a true disaster.",
    "not great, honestly.",
    "rough around the edges.",
    "meh.",
    "below average.",
    "perfectly average.",
    "pretty decent.",
    "solid stuff.",
    "really good!",
    "outstanding!",
    "absolutely perfect!",
)


def score_for(thing: str) -> int:
    """Return a stable 0-10 score for `thing` (case/whitespace-insensitive)."""
    normalized = " ".join(thing.lower().split())
    digest = hashlib.sha256(normalized.encode("utf-8")).digest()
    return digest[0] % 11


def _sanitize(thing: str) -> str:
    """Collapse whitespace, cap length, and defuse `@` mentions in the echoed input."""
    cleaned = " ".join(thing.split())[:MAX_THING_LEN]
    return cleaned.replace("@", "@\u200b")


def _build_reply(thing: str | None) -> str:
    """Build the chat reply for the (possibly absent) thing being rated."""
    if thing is None:
        return USAGE
    score = score_for(thing)
    return f"I rate {_sanitize(thing)} {score}/10 -- {FLAVOR[score]}"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: `!rate <thing>` -> a rating reply payload.

    Returns `None` for any non-matching event. A bare `!rate` replies with usage.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if stripped.partition(" ")[0].lower() != "!rate":
        return None
    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    thing: str | None
    try:
        parsed_cmd = parse_command(stripped, SPEC)
        is_bare = parsed_cmd.option is None
    except CommandUsageError:
        is_bare = False
    thing = None if is_bare else stripped.partition(" ")[2].strip() or None

    log.info("rate.transform matched", platform=event.platform)
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"text": _build_reply(thing), "channel_id": event.payload.get("channel_id")},
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
    envelope: StageEnvelope, config: dict[str, object], *, http_client: object
) -> DispatchResult:
    """Implement `action-stage.dispatch`: relay the rating to the event's origin platform.

    Raises:
        ValueError: The envelope's reply payload has no `channel_id`.
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("rate reply requires a channel_id from the inbound chat.message")
    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": payload.get("text", "")})
    log.info("rate.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
