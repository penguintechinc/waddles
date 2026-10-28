"""`!emojify <text>` -> sprinkles a relevant emoji after matching words (keyword map, fallback sparkle).

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
A mention the user typed still resolves: the transform keeps `{user:<token>}` verbatim, so
the tokens the host minted for this message (`identity.mention_tokens` of the raw delivered
text) are passed as `allow_tokens` -- never anything derived from the transformed text.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from waddle_sdk import identity, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.safe_text import leads_with_command, sanitize_user_text

SPEC = CommandSpec(name="emojify")

MAX_INPUT_LEN = 200

_USAGE = "Usage: !emojify <text>"
_TOO_LONG = f"emojify text must be {MAX_INPUT_LEN} characters or fewer"
#: Twitch/IRC treat a leading `/` or `.` as a chat command and `!` would re-trigger other bots;
#: a transformed reply starting with one would let a caller make the bot run commands as itself.
_UNSAFE_MSG = "emojify can't produce a reply starting with / . or !"

#: Keyword -> emoji. Matched on the lower-cased word with punctuation stripped, and again with a
#: trailing plural `s` removed (`dogs` -> `dog`).
EMOJI_MAP: dict[str, str] = {
    "love": "\u2764\ufe0f",
    "heart": "\u2764\ufe0f",
    "happy": "\U0001f60a",
    "sad": "\U0001f622",
    "laugh": "\U0001f602",
    "lol": "\U0001f602",
    "fire": "\U0001f525",
    "hot": "\U0001f975",
    "cold": "\U0001f976",
    "cool": "\U0001f60e",
    "dog": "\U0001f436",
    "cat": "\U0001f431",
    "pizza": "\U0001f355",
    "food": "\U0001f354",
    "burger": "\U0001f354",
    "coffee": "\u2615",
    "beer": "\U0001f37a",
    "cake": "\U0001f370",
    "music": "\U0001f3b5",
    "song": "\U0001f3b6",
    "game": "\U0001f3ae",
    "win": "\U0001f3c6",
    "star": "\u2b50",
    "sun": "\u2600\ufe0f",
    "moon": "\U0001f319",
    "rain": "\U0001f327\ufe0f",
    "snow": "\u2744\ufe0f",
    "tree": "\U0001f333",
    "flower": "\U0001f338",
    "money": "\U0001f4b0",
    "party": "\U0001f389",
    "birthday": "\U0001f382",
    "gift": "\U0001f381",
    "sleep": "\U0001f634",
    "angry": "\U0001f620",
    "ghost": "\U0001f47b",
    "rocket": "\U0001f680",
    "space": "\U0001f30c",
    "car": "\U0001f697",
    "ship": "\U0001f6a2",
    "book": "\U0001f4da",
    "idea": "\U0001f4a1",
    "time": "\u23f0",
    "eye": "\U0001f440",
    "good": "\U0001f44d",
    "bad": "\U0001f44e",
    "wow": "\U0001f92f",
    "penguin": "\U0001f427",
}
_FALLBACK_EMOJI = "\u2728"
_STRIP = ".,!?;:\"'()[]{}"


def _lookup(word: str) -> str | None:
    """Return the emoji for `word` (punctuation/plural tolerant), or `None` if unmapped."""
    key = word.strip(_STRIP).lower()
    if key in EMOJI_MAP:
        return EMOJI_MAP[key]
    if key.endswith("s") and key[:-1] in EMOJI_MAP:
        return EMOJI_MAP[key[:-1]]
    return None


def apply_transform(text: str) -> str:
    """Append a mapped emoji after each recognised word; no matches -> one trailing sparkle."""
    out: list[str] = []
    matched = False
    for word in text.split():
        emoji = _lookup(word)
        out.append(f"{word} {emoji}" if emoji else word)
        matched = matched or emoji is not None
    result = " ".join(out)
    return result if matched else f"{result} {_FALLBACK_EMOJI}"


def _build_reply(
    args: str | None, *, allow_tokens: Sequence[str] = ()
) -> tuple[str, str]:
    """Validate `args` and return `(reply_text, shape)`; `shape` is the PII-free log label.

    Raises:
        CommandUsageError: No text was supplied (message is the ready-to-send usage reply).
    """
    if not args:
        raise CommandUsageError(_USAGE)
    if len(args) > MAX_INPUT_LEN:
        return _TOO_LONG, "too_long"
    reply = sanitize_user_text(apply_transform(args), allow_tokens=allow_tokens)
    if leads_with_command(reply):
        return _UNSAFE_MSG, "unsafe"
    return reply, "emojify"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!emojify` and build the transformed reply.

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
        reply_text, shape = _build_reply(
            args, allow_tokens=identity.mention_tokens(text)
        )
    except CommandUsageError as exc:
        reply_text, shape = str(exc), "usage"
    # Length + shape only -- NEVER the text itself (PII-free logs).
    log.info(
        "emojify.transform matched",
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
            "emojify reply requires a channel_id from the inbound chat.message"
        )

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": payload.get("text", "")})
    log.info(
        "emojify.dispatch relayed", platform=provider, community_id=envelope.community
    )
    return DispatchResult(transport=provider, detail="relayed")
