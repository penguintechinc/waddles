"""`wordcount` -- per-user message/word counters in `kv`; `!wordcount [@user]` reports them.

Every chat message from an identifiable user increments two atomic counters
(`wordcount.msgs.<pseudonym>`, `wordcount.words.<pseudonym>`) via `kv.increment`.
`!wordcount` reports the caller's totals; `!wordcount @user` reports the mentioned user's.

**PII-free keying.** The actor (`event.actor`) and any @mention reach the bundle as the
hub-issued `{user:<token>}` placeholder -- the bundle never sees a username. The token id is
SHA-256 hashed into a pseudonym (`_pseudonym`) that alone appears in a `kv` key; the token
itself is never stored or logged, only echoed back in a reply for the egress detokenizer. The
community needs no manual hashing: the `kv` host capability already scopes every key
server-side by `(tenant, community, app_id)` (see `count`'s docstring), so the guest-side
pseudonym only has to separate users. A raw typed handle is rejected (never hashed or echoed).

kv keys use `.` only, never `:` (gh-631). Word counts are whitespace-token counts of the raw
message text; the text itself is never stored or logged. A message by a non-token actor is
neither counted nor attributed (fail closed on identity, logged by platform only).

Grammar: `parse_command` validates the bare `!wordcount`; the single positional @mention is the
same documented extension `lastseen` uses. Gated by `waddles.command-wordcount` (default OFF);
flag-off means no counting and no kv traffic at all. Cost: one flag check per message, plus
2 `kv.increment` per counted message (and 2 `kv.get` per report), well under the host op limit.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, cast

from waddle_sdk import identity, kv, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, parse_command
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

SPEC = CommandSpec(name="wordcount")
COMMAND = "!wordcount"
FLAG_KEY = "waddles.command-wordcount"

MSGS_PREFIX = "wordcount.msgs."
WORDS_PREFIX = "wordcount.words."

_USAGE = "Usage: !wordcount [@user]"
_BAD_TARGET_MSG = "I couldn't identify that user - mention them (e.g. @name)."
_NO_SELF_ID = "I couldn't identify you on this platform, so I can't track word counts."

_USER_TOKEN_RE = re.compile(r"^\{user:([A-Za-z0-9_:.-]+)\}$")


class _KvFailure(Exception):
    """Internal-only: a `kv` host call failed. Always caught in `transform`, never leaked."""


def _parse_user_token(raw: object) -> str | None:
    """Extract the bare id from a `{user:<id>}` PII token, or `None` if `raw` isn't one."""
    if not isinstance(raw, str):
        return None
    match = _USER_TOKEN_RE.match(raw.strip())
    return match.group(1) if match else None


def _pseudonym(user_id: str) -> str:
    """Non-reversible hex pseudonym of a token id -- the only user-derived text in any kv key."""
    return hashlib.sha256(f"wordcount|{user_id}".encode()).hexdigest()


def _msgs_key(user_id: str) -> str:
    """kv key of a user's message counter (`.` separators only -- gh-631)."""
    return f"{MSGS_PREFIX}{_pseudonym(user_id)}"


def _words_key(user_id: str) -> str:
    """kv key of a user's word counter (`.` separators only -- gh-631)."""
    return f"{WORDS_PREFIX}{_pseudonym(user_id)}"


def _word_count(text: str) -> int:
    """Whitespace-delimited token count of a message."""
    return len(text.split())


async def _incr(key: str, delta: int) -> int:
    """`kv.increment` (no TTL), reclassifying a host `Err` into `_KvFailure`."""
    try:
        return cast(int, await kv.increment(key, delta, ttl_seconds=0))
    except Exception as exc:
        raise _KvFailure(f"kv.increment failed: {getattr(exc, 'value', exc)}") from exc


async def _read(key: str) -> int:
    """Read one counter (0 if never written); corrupt stored bytes fail loud."""
    try:
        raw = await kv.get(key)
    except Exception as exc:
        raise _KvFailure(f"kv.get failed: {getattr(exc, 'value', exc)}") from exc
    if raw is None:
        return 0
    try:
        return int(cast(bytes, raw).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _KvFailure("corrupt wordcount value") from exc


def _log_kv_failure(exc: _KvFailure, *, op: str) -> None:
    """Log a kv failure by op and exception type only (never keys, ids or text)."""
    log.error("wordcount.kv_failure", op=op, error_type=type(exc).__name__)


async def _answer(rest: str, actor: str | None) -> str:
    """Build the `!wordcount` reply (reads only)."""
    if rest:
        tokens = identity.mention_tokens(rest)
        if len(rest.split()) != 1:
            return _USAGE
        target = _parse_user_token(rest) if len(tokens) == 1 else None
        if target is None:
            return _BAD_TARGET_MSG
        who = "{user:" + target + "}"
    else:
        try:
            parse_command(COMMAND, SPEC)
        except CommandUsageError as exc:  # pragma: no cover - bare shape always parses
            log.debug("wordcount.invalid_grammar", error=type(exc).__name__)
            return _USAGE
        if actor is None:
            return _NO_SELF_ID
        target = actor
        who = "You"
    msgs = await _read(_msgs_key(target))
    words = await _read(_words_key(target))
    verb = "have" if who == "You" else "has"
    return f"{who} {verb} sent {msgs} message(s) and {words} word(s)."


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: count the message, answer `!wordcount`.

    Returns `None` for non-chat payloads, when the flag is OFF, and for every non-command
    message (which is still counted as a side effect). A kv failure on the command path is
    answered explicitly; on the passive path it is logged at ERROR (no reply possible).
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    actor = _parse_user_token(event.actor)
    head, _, rest = text.strip().partition(" ")
    is_command = head.lower() == COMMAND

    if actor is None:
        log.debug("wordcount.no_actor_identity", platform=event.platform)
    else:
        try:
            await _incr(_msgs_key(actor), 1)
            await _incr(_words_key(actor), _word_count(text))
        except _KvFailure as exc:
            _log_kv_failure(exc, op="count")

    if not is_command:
        return None

    try:
        reply = await _answer(rest.strip(), actor)
    except _KvFailure as exc:
        _log_kv_failure(exc, op="read")
        reply = "Word-count storage is unavailable right now - please try again."

    log.info("wordcount.transform matched", platform=event.platform)
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
            "wordcount reply requires a channel_id from the inbound chat.message"
        )
    if not isinstance(text, str) or not text:
        raise ValueError("wordcount reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("wordcount.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
