"""`!uwu <text>` -> uwu-ifies the text (r/l -> w, 'nya' endings, a deterministic uwu/owo flourish).

Pure, deterministic text transform: relay only, no kv/db, no egress, no AI. `transform()` matches
the command and builds the reply, `dispatch()` is a pure relay (same split as `echo`).

PII-free logs: only platform, op, input length and resolved reply shape (plus community id in
`dispatch`) are logged -- never the message text, `event.actor`, or any typed argument. The reply
itself carries caller-typed text back to the same public channel it came from.

Activation-gated only -- no feature flag: the platform's installed -> enabled -> activated check
decides whether this bundle runs.

Output safety: the echoed text is user-typed, so the transformed reply goes through
`waddle_sdk.safe_text.sanitize_user_text` LAST (after the transform), neutralizing forged
`{user:<token>}` placeholders and `@everyone`/`@here`/`<@id>` mass-mention markup. The
leading `/ . !` refusal runs on the sanitized text, so a stripped control character cannot
unmask a command lead.
"""

from __future__ import annotations

import re
from typing import Any

from waddle_sdk import log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.safe_text import leads_with_command, sanitize_user_text

SPEC = CommandSpec(name="uwu")

MAX_INPUT_LEN = 200

_USAGE = "Usage: !uwu <text>"
_TOO_LONG = f"uwu text must be {MAX_INPUT_LEN} characters or fewer"
#: Twitch/IRC treat a leading `/` or `.` as a chat command and `!` would re-trigger other bots;
#: a transformed reply starting with one would let a caller make the bot run commands as itself.
_UNSAFE_MSG = "uwu can't produce a reply starting with / . or !"

_FLOURISHES: tuple[str, ...] = (" uwu", " owo", " >w<", " UwU", " OwO", " ^w^")
_NY_RE = re.compile(r"([nN])([aeiouAEIOU])")
_OVE_RE = re.compile(r"ove", re.IGNORECASE)


def _swap_case_like(match: re.Match[str], lower_repl: str) -> str:
    """Return `lower_repl`, upper-cased when the matched text was all caps."""
    return lower_repl.upper() if match.group(0).isupper() else lower_repl


def apply_transform(text: str) -> str:
    """uwu-ify `text`: r/l -> w, n+vowel -> ny+vowel, 'ove' -> 'uv', plus a flourish.

    The flourish is picked from a fixed table by the text's code-point sum, so the same input
    always yields the same output (no RNG -- reproducible and testable).
    """
    out = _OVE_RE.sub(lambda m: _swap_case_like(m, "uv"), text)
    out = _NY_RE.sub(
        lambda m: f"{m.group(1)}{'Y' if m.group(1) == 'N' else 'y'}{m.group(2)}", out
    )
    out = out.translate(str.maketrans("rlRL", "wwWW"))
    return out + _FLOURISHES[sum(ord(c) for c in text) % len(_FLOURISHES)]


def _build_reply(args: str | None) -> tuple[str, str]:
    """Validate `args` and return `(reply_text, shape)`; `shape` is the PII-free log label.

    Raises:
        CommandUsageError: No text was supplied (message is the ready-to-send usage reply).
    """
    if not args:
        raise CommandUsageError(_USAGE)
    if len(args) > MAX_INPUT_LEN:
        return _TOO_LONG, "too_long"
    reply = sanitize_user_text(apply_transform(args))
    if leads_with_command(reply):
        return _UNSAFE_MSG, "unsafe"
    return reply, "uwu"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!uwu` and build the transformed reply.

    Returns `None` (event dropped) for a non-matching payload -- never
    raises over an event this bundle was never meant to react to.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None

    head, _, rest = text.strip().partition(" ")
    # Only the command head is matched (same case-insensitive `!<name>` comparison the shared
    # `parse_command` applies); the free-text remainder is taken verbatim -- a leading word like
    # "add" must transform, not be read as a verb. Not our command => not for this bundle.
    if head.lower() != f"!{SPEC.name}".lower():
        return None

    args = rest.strip() or None
    try:
        reply_text, shape = _build_reply(args)
    except CommandUsageError as exc:
        reply_text, shape = str(exc), "usage"
    # Length + shape only -- NEVER the text itself (PII-free logs).
    log.info(
        "uwu.transform matched",
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
            "uwu reply requires a channel_id from the inbound chat.message"
        )

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": payload.get("text", "")})
    log.info("uwu.dispatch relayed", platform=provider, community_id=envelope.community)
    return DispatchResult(transport=provider, detail="relayed")
