"""`!say <text>` -> the bot repeats the text as an announcement (moderators/broadcaster only).

Inspired by PenguinTwitchBot by SuperPenguinTV (with permission) -- ideas/design only, no code
copied. See `bundle.yaml`'s `notice`.

Role gate: the normalized chat event's `is_mod` / `is_broadcaster` badge fields, read with a
STRICT `is True` (never `bool()` -- a string badge such as `"false"` must not pass). Absent badge
fields (e.g. Discord's normalizer today) deny: never an implicit allow. A denied caller gets a
polite reply, never silence.

`!say <free text>` cannot go through `waddle_sdk.command.parse_command` (its grammar treats the
first token after the command as a verb/sub-module and rejects free text), so the head token is
matched by hand, exactly like `echo`.

Safety: an announcement beginning with `/`, `.` or `!` is refused -- it would make the bot run a
chat command (or trigger another bot) as itself. Length is capped.

PII-free logs: only platform, outcome shape and the community-less op are logged -- NEVER the
announced text, `event.actor`, or any caller-typed value. Activation-gated only -- no feature
flag: the platform's installed -> enabled -> activated check decides whether this bundle runs.

Output safety: the announcement is caller-typed, so it goes through
`waddle_sdk.safe_text.sanitize_user_text` LAST, in `dispatch`. A mention the moderator typed
still resolves (the host-minted tokens of the delivered text are `allow_tokens`); a forged
`{user:<token>}` and `@everyone`/`@here` are neutralized even for a moderator. The `/ . !`
refusal runs on the sanitized text, so a stripped control character cannot unmask a lead.
"""

from __future__ import annotations

from typing import Any

from waddle_sdk import identity, log, relay
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.safe_text import leads_with_command, sanitize_user_text

_COMMAND = "!say"

MAX_SAY_LEN = 300

_USAGE = "Usage: !say <text>"
_DENIED_MSG = "Sorry, only moderators and the broadcaster can use !say."
_TOO_LONG = f"say text must be {MAX_SAY_LEN} characters or fewer"
_UNSAFE_MSG = "say can't announce text starting with / . or !"

_KNOWN_ACTIONS = frozenset({"announce", "usage"})


def _is_privileged(payload: dict[str, Any]) -> bool:
    """True only when `is_mod` or `is_broadcaster` is exactly the boolean `True`."""
    return payload.get("is_mod") is True or payload.get("is_broadcaster") is True


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!say`, forward text + strict role badges.

    Returns `None` for a non-matching payload. The role decision is made in `dispatch` so a
    denial still produces a reply; `transform` only forwards the badge booleans (strictly
    normalized with `is True`) and the text.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None

    head, _, rest = text.strip().partition(" ")
    if head.lower() != _COMMAND:
        return None

    rest = rest.strip()
    log.info("say.transform matched", platform=event.platform, has_text=bool(rest))
    payload: dict[str, Any] = {
        "action": "announce" if rest else "usage",
        "text": rest,
        "channel_id": event.payload.get("channel_id"),
        "is_mod": event.payload.get("is_mod") is True,
        "is_broadcaster": event.payload.get("is_broadcaster") is True,
    }
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload=payload,
        occurred_at=event.occurred_at,
    )


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result -- see `pyping`'s own `app.py`."""

    __slots__ = ("transport", "detail", "sub_type", "http_status")

    def __init__(self, *, transport: str, detail: str) -> None:
        """Record which provider the reply was relayed to, and a short detail string."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = None


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: enforce the mod gate, then relay the announcement.

    Raises:
        ValueError: The payload has no `channel_id`, or an unrecognized `action`.
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("say reply requires a channel_id from the inbound chat.message")
    action = payload.get("action")
    if action not in _KNOWN_ACTIONS:
        raise ValueError(f"unrecognized say action: {action!r}")

    provider = envelope.event.platform

    if not _is_privileged(payload):
        log.info("say.denied", platform=provider)
        await relay.push(provider, {"channel": channel_id, "text": _DENIED_MSG})
        return DispatchResult(transport=provider, detail="denied")

    text = payload.get("text")
    if action == "usage" or not isinstance(text, str) or not text:
        reply, detail = _USAGE, "usage"
    elif len(text) > MAX_SAY_LEN:
        reply, detail = _TOO_LONG, "too_long"
    else:
        safe = sanitize_user_text(text, allow_tokens=identity.mention_tokens(text))
        if leads_with_command(safe):
            reply, detail = _UNSAFE_MSG, "unsafe"
        else:
            reply, detail = safe, "announce"

    await relay.push(provider, {"channel": channel_id, "text": reply})
    log.info("say.dispatch relayed", platform=provider, shape=detail)
    return DispatchResult(transport=provider, detail=detail)
