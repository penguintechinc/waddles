"""`!remember` / `!memories` / `!forget` -> per-user remembered facts. Self-managed, public read.

Three commands, one bundle:

* ``!remember <text>`` -- the CALLER appends a fact to their OWN list (not mod-gated).
  ``text`` is a single printable line of at most `MAX_TEXT_CHARS` characters; a user holds at
  most `MAX_MEMORIES` entries and an over-cap ``!remember`` is rejected with an explicit reply.
* ``!memories`` -- list the caller's own memories; ``!memories @user`` / ``!memories <@id>``
  lists another user's (PUBLIC read: anyone may read anyone's list).
* ``!forget <id>`` -- the caller removes one of their OWN memories by the id shown in the list;
  ``!forget all`` clears their whole list. There is no way to edit or delete someone else's.

**State**: ONE `kv` key per user, ``memories.<sha256(platform:identity)>``, holding a JSON list
of ``{"id": int, "text": str, "ts": str}`` (no `db` facade -- migration-free). The identity is
hashed with SHA-256 so a raw platform id/handle never becomes a key (PII rule; mirrors
`_pseudonym()` in `bundles/python/duel`/`social`). Keys use ``.``, never ``:`` (the host's
reserved separator, gh-631). ``id`` is monotonic per user (``max(id) + 1``) and never reused
while a higher id exists, so ``!forget 3`` always targets the entry the user was shown as ``3``.
``ts`` is the inbound event's own ``occurred_at`` (no clock capability needed).

**Identity**: on Discord the caller is their numeric author/user id (so ``<@id>`` mentions
resolve to the same key); on every other platform the caller is their case-folded ``actor``
handle (so ``@name`` resolves to the same key). A typed target that cannot be resolved on the
current platform (a bare ``@name`` on Discord, a ``<@id>`` mention elsewhere) is answered
explicitly -- never looked up against a guessed key.

**Fail loud, never silent.** Stored JSON that fails to decode/validate raises `_CorruptStateError`
(never "treated as empty": a ``!remember`` on top of a corrupt blob would destroy the user's
data); a `kv` host error raises `_StateError`. Both are caught only in `transform`, logged at
ERROR (op + exception type -- never memory text, actor or target) and answered with an explicit
reply. ``!forget all`` deletes the key without parsing it, so it is also the recovery path for
a corrupt blob.

**Known limitation**: read-modify-write on the blob is not atomic across hosts -- two
simultaneous ``!remember`` from the SAME user could lose one write. Per-user chat rates make
this a non-issue in practice; documented rather than papered over.

Replies are capped at `MAX_REPLY_CHARS` (Twitch's 500-char chat limit): a longer list is
truncated with an explicit ``(+N more)`` suffix.

Gated behind the PostHog flag ``waddles.command-memories`` (default OFF). Ordering inside
`transform()`: cheap command match, then flag, then parsing/`kv`. `kv`, `log`, `relay` and
`feature_enabled` are imported at MODULE LOAD on purpose: componentize-py only wizens a WIT
host-import that is imported eagerly (`docs/reference/bundle-host-imports.md`).

The shared `waddle_sdk.command` grammar parser is intentionally NOT used: it models
``!<command> [sub-module] <verb> <input>`` for ONE command name from a fixed verb set, while
this bundle owns three different command heads whose arguments are free text, an id, or a
user mention.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, cast

from waddle_sdk import kv, log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

#: PostHog flag key, `{product}.{feature-name}` convention (default OFF).
FLAG_KEY = "waddles.command-memories"

#: Per-user `kv` key prefix (`.`, never `:`); the suffix is a SHA-256 hex digest.
KEY_PREFIX = "memories."

MAX_MEMORIES = 20
MAX_TEXT_CHARS = 200
#: Twitch's chat limit is 500 chars; leave headroom.
MAX_REPLY_CHARS = 450

_REMEMBER = "!remember"
_MEMORIES = "!memories"
_FORGET = "!forget"
_COMMANDS = frozenset({_REMEMBER, _MEMORIES, _FORGET})

_HEAD_RE = re.compile(r"^(\S+)(?:\s+(.*))?$", re.DOTALL)
_DISCORD_MENTION_RE = re.compile(r"^<@!?(\d{1,32})>$")
_PLATFORM_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
#: Plausible handle (same shape as `bundles/python/duel`'s `_USERNAME_RE`).
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}$")
_ID_RE = re.compile(r"^\d{1,9}$")

_USAGE_REMEMBER = f"Usage: !remember <text> (one line, up to {MAX_TEXT_CHARS} chars)"
_USAGE_MEMORIES = "Usage: !memories [@user]"
_USAGE_FORGET = "Usage: !forget <id> | !forget all"
_TOO_LONG = f"That's too long - memories are limited to {MAX_TEXT_CHARS} characters."
_NOT_SINGLE_LINE = "Memories must be a single line of plain text."
_OVER_CAP = (
    f"You already have {MAX_MEMORIES} memories - use !forget <id> to make room "
    "(see !memories for ids)."
)
_NO_SELF_ID = "I couldn't identify you on this platform, so I can't store or read your memories."
_NO_TS = "I couldn't timestamp that message, so nothing was saved."
_BAD_TARGET = "I couldn't identify that user - on Discord use an @mention, elsewhere use @name."
_NO_OWN = "You have no memories yet - try !remember <text>."
_NO_OTHER = "They have no memories yet."
_NOTHING_TO_FORGET = "You have no memories to forget."
_KV_FAILURE_REPLY = "Something went wrong with memory storage - please try again."
_CORRUPT_REPLY = (
    "Your saved memories couldn't be read, so nothing was changed. Use !forget all to reset them."
)


class _StateError(Exception):
    """Internal-only: a `kv` host call failed. Always caught in `transform`, never leaked.

    Mirrors `waddle_sdk.db`'s documented pattern ("Err is structurally classified, never
    imported") since `waddle_sdk.kv` does not classify the generated WIT `Err` itself.
    """

    def __init__(self, op: str, message: str) -> None:
        """Record which `kv` op (`get`/`set`/`delete`) failed, and a non-PII message."""
        super().__init__(message)
        self.op = op


class _CorruptStateError(_StateError):
    """Internal-only: the stored blob is not valid UTF-8 JSON of the expected shape."""


@dataclass(slots=True, frozen=True)
class Memory:
    """One remembered fact: its stable per-user id, the text, and the event timestamp."""

    id: int
    text: str
    ts: str


def _pseudonym(platform: str, identity: str) -> str:
    """Non-reversible per-user key component -- see `duel`'s own `_pseudonym()` for why.

    `identity` may be a raw handle/id (tokenization pipeline #429 not yet merged); hashing it
    with the platform before it ever reaches `kv` keeps this bundle PII-safe today and
    unchanged after #429 lands.
    """
    return hashlib.sha256(f"{platform}:{identity}".encode()).hexdigest()


def _memory_key(platform: str, identity: str) -> str:
    """The single `kv` key one user's memory list lives under."""
    return f"{KEY_PREFIX}{_pseudonym(platform, identity)}"


def _caller_identity(event: PlatformEvent) -> str | None:
    """The event author's identity string, or `None` if no usable id/handle is present.

    Discord: numeric author/user id first (what a `<@id>` mention carries), actor as fallback.
    Other platforms: case-folded actor handle first (what an `@name` target resolves to), then
    the id fields.
    """
    ids = [event.payload.get("author_id"), event.payload.get("user_id")]
    actor = event.actor
    handles = [actor.lower()] if isinstance(actor, str) else []
    ordered = [*ids, *handles] if event.platform == "discord" else [*handles, *ids]
    for candidate in ordered:
        if isinstance(candidate, str) and _PLATFORM_ID_RE.match(candidate):
            return candidate
    return None


def _resolve_target(raw: str, platform: str) -> str | None:
    """Resolve a typed target to the identity string `_caller_identity` would produce for them.

    Discord needs a real `<@id>` mention (a bare name cannot be mapped to an id); every other
    platform needs a plausible `@name`/`name` handle (case-folded) and cannot honour `<@id>`.
    The typed text is hashed by the caller and never logged or echoed.
    """
    token = raw.strip()
    mention = _DISCORD_MENTION_RE.match(token)
    if platform == "discord":
        return mention.group(1) if mention else None
    if mention:
        return None
    handle = token.removeprefix("@")
    return handle.lower() if _HANDLE_RE.match(handle) else None


async def _kv_get(key: str) -> bytes | None:
    """`kv.get`, reclassifying the generated WIT `Err` into `_StateError` (fail loud)."""
    try:
        result = await kv.get(key)
    except Exception as exc:  # noqa: BLE001 - classified like waddle_sdk.db/http
        raise _StateError("get", "kv.get failed") from exc
    # `waddle_sdk` ships no `py.typed`, so mypy sees `Any` -- cast back to the real contract.
    return cast("bytes | None", result)


async def _kv_set(key: str, value: bytes) -> None:
    """`kv.set` (no TTL -- memories persist), reclassifying `Err` into `_StateError`."""
    try:
        await kv.set(key, value, ttl_seconds=0)
    except Exception as exc:  # noqa: BLE001
        raise _StateError("set", "kv.set failed") from exc


async def _kv_delete(key: str) -> None:
    """`kv.delete`, reclassifying `Err` into `_StateError`."""
    try:
        await kv.delete(key)
    except Exception as exc:  # noqa: BLE001
        raise _StateError("delete", "kv.delete failed") from exc


def _parse_blob(raw: bytes) -> list[Memory]:
    """Decode and strictly validate a stored blob.

    Raises:
        _CorruptStateError: Not UTF-8, not JSON, not a list of `{id,text,ts}` entries with
            unique int ids, or longer than `MAX_MEMORIES`.
    """
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _CorruptStateError("get", f"undecodable blob: {type(exc).__name__}") from exc
    if not isinstance(data, list) or len(data) > MAX_MEMORIES:
        raise _CorruptStateError("get", "blob is not a list within the cap")
    items: list[Memory] = []
    for entry in data:
        if not isinstance(entry, dict):
            raise _CorruptStateError("get", "entry is not an object")
        mem_id, text, ts = entry.get("id"), entry.get("text"), entry.get("ts")
        if not (
            isinstance(mem_id, int)
            and not isinstance(mem_id, bool)
            and mem_id > 0
            and isinstance(text, str)
            and isinstance(ts, str)
        ):
            raise _CorruptStateError("get", "entry has wrong field types")
        items.append(Memory(id=mem_id, text=text, ts=ts))
    if len({m.id for m in items}) != len(items):
        raise _CorruptStateError("get", "duplicate ids")
    return items


async def _load(key: str) -> list[Memory]:
    """Return the user's stored memories (empty if none stored yet).

    Raises:
        _StateError: `kv` failed, or `_CorruptStateError` if the stored blob is invalid.
    """
    raw = await _kv_get(key)
    if raw is None:
        return []
    return _parse_blob(raw)


async def _save(key: str, items: list[Memory]) -> None:
    """Persist `items` as the user's blob; an empty list deletes the key instead."""
    if not items:
        await _kv_delete(key)
        return
    blob = json.dumps(
        [{"id": m.id, "text": m.text, "ts": m.ts} for m in items],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    await _kv_set(key, blob.encode("utf-8"))


def _validate_text(text: str) -> str | None:
    """Return a rejection reply for invalid memory text, or `None` if it is acceptable."""
    if len(text) > MAX_TEXT_CHARS:
        return _TOO_LONG
    if not text.isprintable():
        return _NOT_SINGLE_LINE
    return None


def _format_list(items: list[Memory]) -> str:
    """Render `id. text` entries joined by ` | `, truncated to `MAX_REPLY_CHARS` with a count."""
    parts: list[str] = []
    used = 0
    for index, memory in enumerate(items):
        part = f"{memory.id}. {memory.text}"
        extra = len(part) + (3 if parts else 0)
        if used + extra > MAX_REPLY_CHARS:
            return " | ".join(parts) + f" (+{len(items) - index} more)"
        parts.append(part)
        used += extra
    return " | ".join(parts)


async def _remember(event: PlatformEvent, rest: str) -> str:
    """`!remember <text>`: append to the caller's own list, enforcing text rules and the cap."""
    if not rest:
        return _USAGE_REMEMBER
    rejection = _validate_text(rest)
    if rejection is not None:
        log.debug("memories.remember_rejected", reason="invalid_text")
        return rejection
    identity = _caller_identity(event)
    if identity is None:
        return _NO_SELF_ID
    if not event.occurred_at:
        return _NO_TS
    key = _memory_key(event.platform, identity)
    items = await _load(key)
    if len(items) >= MAX_MEMORIES:
        log.debug("memories.remember_rejected", reason="over_cap")
        return _OVER_CAP
    new_id = max((m.id for m in items), default=0) + 1
    items.append(Memory(id=new_id, text=rest, ts=event.occurred_at))
    await _save(key, items)
    log.info("memories.remembered", count=len(items))
    return f"Remembered as #{new_id} ({len(items)}/{MAX_MEMORIES})."


async def _list(event: PlatformEvent, rest: str) -> str:
    """`!memories [@user]`: list the caller's own, or (public read) another user's, memories."""
    tokens = rest.split()
    if len(tokens) > 1:
        return _USAGE_MEMORIES
    own = not tokens
    if own:
        identity = _caller_identity(event)
        if identity is None:
            return _NO_SELF_ID
    else:
        identity = _resolve_target(tokens[0], event.platform)
        if identity is None:
            log.debug("memories.target_unresolved", platform=event.platform)
            return _BAD_TARGET
    items = await _load(_memory_key(event.platform, identity))
    if not items:
        return _NO_OWN if own else _NO_OTHER
    log.info("memories.listed", count=len(items), own=own)
    return _format_list(items)


async def _forget(event: PlatformEvent, rest: str) -> str:
    """`!forget <id|all>`: remove one of the caller's OWN memories, or clear their list."""
    arg = rest.strip()
    if not arg or " " in arg:
        return _USAGE_FORGET
    identity = _caller_identity(event)
    if identity is None:
        return _NO_SELF_ID
    key = _memory_key(event.platform, identity)
    if arg.lower() == "all":
        # Deliberately unparsed: also the recovery path for a corrupt blob.
        await _kv_delete(key)
        log.info("memories.cleared")
        return "Cleared all your memories."
    if not _ID_RE.match(arg):
        return _USAGE_FORGET
    target_id = int(arg)
    items = await _load(key)
    remaining = [m for m in items if m.id != target_id]
    if len(remaining) == len(items):
        return _NOTHING_TO_FORGET if not items else f"You have no memory #{target_id}."
    await _save(key, remaining)
    log.info("memories.forgot", count=len(remaining))
    return f"Forgot #{target_id}."


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize the three commands and build the reply.

    Returns `None` for a non-chat payload, any message whose first token is not one of the
    three commands, and while `waddles.command-memories` is off. A `kv` failure or corrupt
    blob logs at ERROR and replies with an explicit notice -- never a silent no-op.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    match = _HEAD_RE.match(text.strip())
    if match is None:
        return None
    head = match.group(1).lower()
    if head not in _COMMANDS:
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    rest = (match.group(2) or "").strip()
    try:
        if head == _REMEMBER:
            reply = await _remember(event, rest)
        elif head == _MEMORIES:
            reply = await _list(event, rest)
        else:
            reply = await _forget(event, rest)
    except _CorruptStateError as exc:
        log.error(
            "memories.state_corrupt", op=exc.op, error_type=type(exc.__cause__ or exc).__name__
        )
        reply = _CORRUPT_REPLY
    except _StateError as exc:
        log.error("memories.kv_failure", op=exc.op, error_type=type(exc.__cause__ or exc).__name__)
        reply = _KV_FAILURE_REPLY

    log.info("memories.transform matched", command=head.removeprefix("!"))
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
        ValueError: The envelope's payload is missing `channel_id` or `text`.
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    text = payload.get("text")
    if not channel_id:
        raise ValueError("memories reply requires a channel_id from the inbound chat.message")
    if not isinstance(text, str) or not text:
        raise ValueError("memories reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("memories.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
