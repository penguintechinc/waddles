"""`!yomama [@user]` -> a classic 'yo mama' joke from a built-in static list. Stateless, relay-only.

No `kv` (no storage permission is declared): the pool is a fixed, original, SFW tuple and
nothing is persisted. Bare `!yomama` addresses the caller; `!yomama @user` addresses the mentioned
user.

**PII-free.** The bundle never sees or stores a username: both the caller (`event.actor`) and an
@mention reach it as the hub-issued `{user:<token>}` placeholder, which is echoed back verbatim
in the reply for the egress detokenizer to render. A target that is not such a token (a typed
raw handle) is answered with an explicit "mention them" reply -- never echoed, hashed, stored or
logged. Logs carry only the command name and platform.

Grammar: `waddle_sdk.command.parse_command` validates the bare shape (`!yomama`); the single
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

SPEC = CommandSpec(name="yomama")
COMMAND = "!yomama"

_USAGE = "Usage: !yomama [@user]"
_BAD_TARGET_MSG = "I couldn't identify that user - mention them (e.g. @name)."

_USER_TOKEN_RE = re.compile(r"^\{user:([A-Za-z0-9_:.-]+)\}$")

#: Original, light-hearted, SFW lines -- teasing only: no slurs, harassment, or sensitive topics.
_POOL: tuple[str, ...] = (
    "yo mama's so slow, she got passed by a glacier... and it waved.",
    "yo mama's so forgetful, she put her phone on silent and then lost her ears.",
    "yo mama's so tall, she uses clouds as a footrest.",
    "yo mama's so clumsy, she tripped over a wireless connection.",
    "yo mama's so cheap, she reuses her tea bag until it files for retirement.",
    "yo mama's so lazy, her Fitbit sends her sympathy cards.",
    "yo mama's so old, her birth certificate is carved in stone.",
    "yo mama's so loud, the library asked her to whisper at home.",
    "yo mama's so early, she arrived at yesterday's party.",
    "yo mama's so smart, she taught the GPS a shortcut.",
    "yo mama's so bad at hide and seek, she got found before she started hiding.",
    "yo mama's so dramatic, her shadow asked for a better script.",
    "yo mama's so fast, she beat the speed of dial-up.",
    "yo mama's so thrifty, she cuts coupons for free samples.",
    "yo mama's so chill, ice cubes ask her for tips.",
    "yo mama's so sleepy, she hit snooze on her own alarm clock's alarm.",
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
    """Build the reply for an already-matched `!yomama ...` message (never `None`)."""
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
            log.debug("yomama.invalid_grammar", error=type(exc).__name__)
            return _USAGE
        who = _user_ref(actor) or "Hey"
    return f"💥 {who}, {_pick(_POOL)}"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognise `!yomama` and build the reply.

    Exact first-token match (so `!yomamas` never matches) is the cheap precondition.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    head = text.strip().partition(" ")[0]
    if head.lower() != COMMAND:
        return None

    reply = _reply_for(text, event.actor)
    log.info("yomama.transform matched", platform=event.platform)
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
            "yomama reply requires a channel_id from the inbound chat.message"
        )
    if not isinstance(text, str) or not text:
        raise ValueError("yomama reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("yomama.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
