"""`!vqueue` -> a per-community viewer queue (join/leave/position/list/next/clear), kv-only.

Original Waddles content: a viewer queue (line up for a game, a Q&A turn, a race) is a common
stream-tool idea, but no third-party source, code or text is used here, so no attribution
applies. `author`/`license` in `bundle.yaml` follow the `PenguinTech/waddles` first-party
convention.

Commands (the front of the queue is position 1):

- `!vqueue join` / `!vqueue leave` -- anyone: add / remove the caller. A second `join` while queued
  and a `leave` while not queued are friendly replies, never silent no-ops (fail loud).
- bare `!vqueue` -- anyone: the caller's own position (`#2 of 5`), or how to join.
- `!vqueue list [n]` -- anyone: the front `n` entries (default 10, max 15) plus a "+K more" tail.
- `!vqueue next` -- moderator/broadcaster: pop the front and announce who is up.
- `!vqueue clear` (alias `!vqueue reset`) -- moderator/broadcaster: empty the queue.

`join`/`leave`/`next`/`clear` are not in the shared `waddle_sdk.command.VERBS` vocabulary, so they
are special-cased ahead of the formal `parse_command()` call (the same documented extension
`raffle`/`music` use); bare, `list` and `reset` go through the real shared grammar, and any other
grammar-legal verb replies with usage text.

**Why `!vqueue`, not `!queue`.** `bundles/python/music` already owns `!queue` (its alias for
`!music`, the song-request queue), and two bundles on one trigger answer every line twice. This
bundle (a VIEWER queue: join/leave/next) therefore answers `!vqueue`; `music` keeps `!queue`. The
bundle id, kv keys and log names stay `queue`.

**Identity and PII.** A queue entry must be re-displayable to OTHER viewers (`!vqueue list`), so it
cannot be a one-way hash alone. Following `lastseen`, identity is the pipeline's opaque
`{user:<id>}` token carried in `event.actor`: the bare id is stored (`u:<id>`) and replies EMIT
`{user:<id>}` so the host's egress detokenizer renders the display name. Until ingest
tokenization is universal, `event.actor` may still be a raw platform handle; that case never
stores or logs the handle -- the entry is `h:<24 hex of SHA-256(community|platform|actor)>`,
shown as an opaque `viewer-<6 hex>` label (stable per viewer, so `!vqueue` still tells you your
own place). Only an actor that is EXACTLY one placeholder is a token (a padded/embedded look-alike
is hashed like any handle). No raw handle appears in a key, value, log line or reply. No actor
at all -> "couldn't identify you", never a guessed entry.

**Concurrency.** One community's queue is a single JSON list under `queue.entries`. A
read-modify-write of it is serialized by a short-TTL lock built from `kv.increment` (atomic,
`1` only for the first caller -- the one compare-and-swap the host offers). A caller that cannot
take the lock after three immediate attempts (no sleep exists under WASI) gets "busy, try again"
instead of a silently lost update; the TTL bounds a crashed holder. Reads (`!vqueue`,
`!vqueue list`) take no lock. Every command costs at most 5 kv operations (host cap: 64).

**Output safety.** Every reply is bundle-built from validated tokens, hash labels, integers and
constant text. The single place user-typed text can be echoed is the usage reply to a malformed
command (grammar errors quote the offending token), and that text goes through
`waddle_sdk.safe_text.sanitize_user_text` (no `allow_tokens`: nothing a viewer types may stay a
live mention or egress token).

Limits: 100 entries per community; the stored list is refreshed with a 7-day TTL on every write,
so an abandoned queue expires instead of lingering forever.

Activation-gated only -- no feature flag: the platform's installed -> enabled -> activated check
decides whether this bundle runs.

DO NOT BUILD in v1 -- documented extension points, never a silent stub:

- Per-queue names / multiple queues, a "skip" or "move" verb, priority (subscriber) ordering.
- Pinging the next viewer by DM -- the reply names them through their mention token only.
- Cross-community anything: the queue is keyed by `community_id` only (`waddle_sdk.community_kv`).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
from collections.abc import AsyncIterator
from typing import Any, NoReturn

from waddle_sdk import community_kv, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, ParsedCommand, parse_command
from waddle_sdk.community_kv import TENANT_WIDE_SENTINEL
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.safe_text import sanitize_user_text

_HEAD = "!vqueue"
SPEC = CommandSpec(name="vqueue")

#: Domain verbs special-cased ahead of the formal grammar parse -- none is in
#: `waddle_sdk.command.VERBS` (see module docstring).
_DOMAIN_VERBS = frozenset({"join", "leave", "next", "clear"})
_MOD_COMMANDS = frozenset({"next", "clear"})

#: Entries one community's queue may hold. 100 entries of at most 130 bytes is ~13 KB, far under
#: the host's 64 KiB `kv` value cap (`core/bundle_host_kv/src/limits.rs::MAX_VALUE_BYTES`).
MAX_QUEUE = 100
#: `!vqueue list` shows this many entries by default / at most (a Twitch line is 500 chars; a
#: resolved display name is up to ~25).
DEFAULT_LIST = 10
MAX_LIST = 15

#: `.`-separated, never `:` -- the real `kv` host capability reserves `:` (gh-631).
_QUEUE_KEY = "queue.entries"
_LOCK_KEY = "queue.lock"
#: A lock expires after this many seconds so a holder that crashed can never block the queue for
#: longer than this.
_LOCK_TTL_SECONDS = 10
#: Immediate attempts to take the lock before replying "busy" (no sleep under WASI).
_LOCK_ATTEMPTS = 3
#: Idle queues expire after a week; refreshed by every write.
_QUEUE_TTL_SECONDS = 7 * 24 * 60 * 60

#: The pipeline's `{user:<id>}` placeholder -- grammar mirrors `core/egress_detokenizer`
#: (alphanumerics plus `-_:.`), bounded so a stored entry stays small.
_TOKEN_RE = re.compile(r"\{user:([A-Za-z0-9_:.-]{1,128})\}")
#: A stored entry: `u:<token id>` or `h:<24 hex>`. Anything else in the stored list is corruption.
_ENTRY_RE = re.compile(r"(?:u:[A-Za-z0-9_:.-]{1,128}|h:[0-9a-f]{24})")
_COUNT_RE = re.compile(r"[0-9]{1,3}")

_USAGE = (
    "Usage: !vqueue join | !vqueue leave | !vqueue | !vqueue list [n] | "
    "!vqueue next | !vqueue clear (next/clear: mods only)"
)
_NO_IDENTITY_MSG = "I couldn't identify you on this platform, so I can't manage your queue spot."
_BUSY_MSG = "The queue is busy right now, please try again in a moment."
_UNAVAILABLE_MSG = "The queue is temporarily unavailable, try again shortly."
_CORRUPT_MSG = "The queue state is corrupted, please contact support."
_EMPTY_MSG = "The queue is empty."

_KNOWN_COMMANDS = frozenset({"join", "leave", "position", "list", "next", "clear", "usage"})


class _BusyError(Exception):
    """The queue lock could not be taken -- surfaced as a "busy, try again" reply."""


def _caller_role_signal(payload: dict[str, Any]) -> bool | None:
    """`True`/`False` from the normalized event's own badge fields, or `None` if absent.

    A real `True` only -- a string `"false"` is truthy and must never pass the mod gate. `None`
    (neither `is_mod` nor `is_broadcaster` present, e.g. Discord's normalizer today) is treated
    as denied by the caller, never as an implicit allow.
    """
    if "is_mod" not in payload and "is_broadcaster" not in payload:
        return None
    return payload.get("is_mod") is True or payload.get("is_broadcaster") is True


def _pseudonym(community: str | None, platform: str, actor: str) -> str:
    """24-hex non-reversible id for a raw (untokenized) `actor`, per community AND platform.

    The platform is part of the hash: a handle is only unique within its own platform, so a Discord
    `bob` and a Twitch `bob` in one community are two viewers, never one shared queue entry.
    """
    scope = community if community is not None else TENANT_WIDE_SENTINEL
    return hashlib.sha256(f"{scope}|{platform}|{actor}".encode()).hexdigest()[:24]


def _entry_for(community: str | None, actor: object, *, platform: str) -> str | None:
    """Return the stored entry for `actor`, or `None` if there is no usable identity.

    ONLY an actor that is exactly one host-minted `{user:<id>}` placeholder (no surrounding
    whitespace or text -- the pipeline never adds any) becomes `u:<id>`; any other non-empty string
    is a raw platform handle and is hashed (never stored). A raw handle that merely LOOKS like a
    placeholder (padded, embedded, over-long) is hashed too, so it can never claim another viewer's
    entry. See module docstring.
    """
    if not isinstance(actor, str) or not actor.strip():
        return None
    match = _TOKEN_RE.fullmatch(actor)
    if match:
        return f"u:{match.group(1)}"
    return f"h:{_pseudonym(community, platform, actor.strip())}"


def _label(entry: str) -> str:
    """Render a validated stored entry for chat: a mention placeholder or an opaque hash label."""
    kind, _, ident = entry.partition(":")
    if kind == "u":
        return "{user:" + ident + "}"
    return "viewer-" + ident[:6]


def _resolve(rest: str) -> tuple[str, str | None]:
    """Map the text after `!vqueue` onto `(command, arg)`; `arg` is `list`'s count or usage text.

    `rest` is already stripped and may be empty. Domain verbs are handled before the formal
    grammar parse (module docstring); everything else is delegated to `parse_command` unchanged.
    """
    if rest:
        first, _, remainder = rest.partition(" ")
        verb = first.lower()
        if verb in _DOMAIN_VERBS:
            if remainder.strip():
                return "usage", f"!vqueue {verb} takes no arguments"
            return verb, None

    try:
        parsed = parse_command(f"{_HEAD} {rest}" if rest else _HEAD, SPEC)
    except CommandUsageError as exc:
        # The grammar error quotes the offending token verbatim -- sanitized at the reply; only
        # the exception class is logged.
        log.debug("queue.grammar_rejected", error_type=type(exc).__name__)
        return "usage", str(exc)
    return _map_parsed(parsed)


def _map_parsed(parsed: ParsedCommand) -> tuple[str, str | None]:
    """Map a successful `parse_command()` result onto this bundle's command set.

    Only bare, `list [n]` and `reset` (an alias of `clear`) are implemented; every other
    grammar-legal verb replies with usage text -- never silently dropped.
    """
    if parsed.option is None:
        return "position", None
    if parsed.option == "list":
        return "list", parsed.args
    if parsed.option == "reset":
        if parsed.args is not None:
            return "usage", "!vqueue reset takes no arguments"
        return "clear", None
    return "usage", f"unknown option '{parsed.option}'"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!vqueue` and its grammar.

    Cheap-skip first (exact first-token equality -- a `startswith` would wrongly match
    `!vqueuex`), grammar resolution last. A recognized-but-malformed command still produces a
    reply (`usage`), never a silent drop. No `kv` access here; `dispatch` owns all state I/O.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None

    stripped = text.strip()
    head, _, rest = stripped.partition(" ")
    if head.lower() != _HEAD:
        return None

    command, arg = _resolve(rest.strip())
    log.info("queue.transform matched", command=command)

    payload: dict[str, Any] = {"command": command, "channel_id": event.payload.get("channel_id")}
    if arg is not None:
        payload["arg"] = arg
    # Forward the normalized badge signal only if the platform emitted one -- absence must reach
    # `dispatch` as absence, not as an implicit `False`.
    if "is_mod" in event.payload:
        payload["is_mod"] = event.payload["is_mod"] is True
    if "is_broadcaster" in event.payload:
        payload["is_broadcaster"] = event.payload["is_broadcaster"] is True

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


async def _fail_kv(exc: Exception, *, provider: str, channel_id: str, op: str) -> NoReturn:
    """Fail-loud kv error path: log (op + exception class only), reply to chat, re-raise."""
    case_name = type(getattr(exc, "value", exc)).__name__
    log.error("queue.kv_error", op=op, error=case_name)
    await relay.push(provider, {"channel": channel_id, "text": _UNAVAILABLE_MSG})
    raise RuntimeError(f"queue kv {op} failed: {case_name}") from exc


async def _fail_state(reason: str, *, provider: str, channel_id: str) -> NoReturn:
    """Fail-loud corrupt-stored-queue path. The blob is never overwritten or reset."""
    log.error("queue.state_corrupt", reason=reason)
    await relay.push(provider, {"channel": channel_id, "text": _CORRUPT_MSG})
    raise RuntimeError(f"queue corrupt state: {reason}")


async def _load_entries(community: str | None, *, provider: str, channel_id: str) -> list[str]:
    """Return the community's queue (front first), failing loud on a corrupt stored blob."""
    try:
        raw = await community_kv.get(community, _QUEUE_KEY)
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail_kv`
        await _fail_kv(exc, provider=provider, channel_id=channel_id, op="get")
    if raw is None:
        return []
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        await _fail_state("not valid JSON", provider=provider, channel_id=channel_id)
    if not isinstance(data, list):
        await _fail_state("expected a JSON array", provider=provider, channel_id=channel_id)
    entries: list[str] = []
    for item in data:
        if not isinstance(item, str) or not _ENTRY_RE.fullmatch(item):
            await _fail_state("malformed entry", provider=provider, channel_id=channel_id)
        entries.append(item)
    if len(entries) > MAX_QUEUE:
        await _fail_state("over the entry cap", provider=provider, channel_id=channel_id)
    return entries


async def _save_entries(
    community: str | None, entries: list[str], *, provider: str, channel_id: str
) -> None:
    """Persist the queue with a refreshed idle-expiry TTL."""
    try:
        await community_kv.set(
            community, _QUEUE_KEY, json.dumps(entries).encode("utf-8"), _QUEUE_TTL_SECONDS
        )
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail_kv`
        await _fail_kv(exc, provider=provider, channel_id=channel_id, op="set")


async def _delete_entries(community: str | None, *, provider: str, channel_id: str) -> None:
    """Drop the queue blob entirely (`!vqueue clear`)."""
    try:
        await community_kv.delete(community, _QUEUE_KEY)
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail_kv`
        await _fail_kv(exc, provider=provider, channel_id=channel_id, op="delete")


@contextlib.asynccontextmanager
async def _queue_lock(
    community: str | None, *, provider: str, channel_id: str
) -> AsyncIterator[None]:
    """Hold the community's queue lock for a read-modify-write; raise `_BusyError` if contended.

    `kv.increment` returns `1` only to the caller that created the key -- the compare-and-swap
    `kv.set` lacks. The lock is released in `finally`; a failed release is logged (never
    swallowed silently) and the TTL expires it.
    """
    acquired = False
    for _ in range(_LOCK_ATTEMPTS):
        try:
            count = await community_kv.increment(community, _LOCK_KEY, 1, _LOCK_TTL_SECONDS)
        except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail_kv`
            await _fail_kv(exc, provider=provider, channel_id=channel_id, op="lock")
        if count == 1:
            acquired = True
            break
    if not acquired:
        log.warn("queue.lock_busy")
        raise _BusyError
    try:
        yield
    finally:
        try:
            await community_kv.delete(community, _LOCK_KEY)
        except Exception as exc:  # noqa: BLE001 -- logged, never silent; the TTL expires the lock
            log.error("queue.lock_release_failed", error=type(getattr(exc, "value", exc)).__name__)


async def _handle_join(
    community: str | None, actor: object, *, provider: str, channel_id: str
) -> str:
    """Append the caller to the back of the queue, once."""
    entry = _entry_for(community, actor, platform=provider)
    if entry is None:
        return _NO_IDENTITY_MSG
    label = _label(entry)
    async with _queue_lock(community, provider=provider, channel_id=channel_id):
        entries = await _load_entries(community, provider=provider, channel_id=channel_id)
        if entry in entries:
            return f"{label}, you're already in the queue at position {entries.index(entry) + 1}."
        if len(entries) >= MAX_QUEUE:
            return f"The queue is full ({MAX_QUEUE} waiting)."
        entries.append(entry)
        await _save_entries(community, entries, provider=provider, channel_id=channel_id)
    log.info("queue.joined", size=len(entries))
    return f"{label} joined the queue at position {len(entries)}."


async def _handle_leave(
    community: str | None, actor: object, *, provider: str, channel_id: str
) -> str:
    """Remove the caller from wherever they are in the queue."""
    entry = _entry_for(community, actor, platform=provider)
    if entry is None:
        return _NO_IDENTITY_MSG
    label = _label(entry)
    async with _queue_lock(community, provider=provider, channel_id=channel_id):
        entries = await _load_entries(community, provider=provider, channel_id=channel_id)
        if entry not in entries:
            return f"{label}, you're not in the queue."
        entries.remove(entry)
        await _save_entries(community, entries, provider=provider, channel_id=channel_id)
    log.info("queue.left", size=len(entries))
    return f"{label} left the queue."


async def _handle_position(
    community: str | None, actor: object, *, provider: str, channel_id: str
) -> str:
    """Report the caller's own place in line (pure read)."""
    entry = _entry_for(community, actor, platform=provider)
    if entry is None:
        return _NO_IDENTITY_MSG
    label = _label(entry)
    entries = await _load_entries(community, provider=provider, channel_id=channel_id)
    if entry in entries:
        return f"{label}, you're #{entries.index(entry) + 1} of {len(entries)} in the queue."
    if not entries:
        return f"{label}, the queue is empty. Use !vqueue join to get in line."
    return f"{label}, you're not in the queue ({len(entries)} waiting). Use !vqueue join."


async def _handle_list(
    community: str | None, count_arg: object, *, provider: str, channel_id: str
) -> str:
    """Show the front `n` entries (pure read), `n` defaulting to `DEFAULT_LIST`."""
    shown_count = DEFAULT_LIST
    if count_arg is not None:
        if not isinstance(count_arg, str) or not _COUNT_RE.fullmatch(count_arg):
            return f"Usage: !vqueue list [n] (n is 1-{MAX_LIST})"
        shown_count = int(count_arg)
        if not 1 <= shown_count <= MAX_LIST:
            return f"Usage: !vqueue list [n] (n is 1-{MAX_LIST})"
    entries = await _load_entries(community, provider=provider, channel_id=channel_id)
    if not entries:
        return _EMPTY_MSG
    front = entries[:shown_count]
    body = " ".join(f"{i}. {_label(e)}" for i, e in enumerate(front, start=1))
    more = len(entries) - len(front)
    tail = f" (+{more} more)" if more else ""
    return f"Queue ({len(entries)}): {body}{tail}"


async def _handle_next(community: str | None, *, provider: str, channel_id: str) -> str:
    """Pop the front of the queue and announce who is up (moderator/broadcaster only)."""
    async with _queue_lock(community, provider=provider, channel_id=channel_id):
        entries = await _load_entries(community, provider=provider, channel_id=channel_id)
        if not entries:
            return _EMPTY_MSG
        front = entries.pop(0)
        await _save_entries(community, entries, provider=provider, channel_id=channel_id)
    log.info("queue.advanced", remaining=len(entries))
    waiting = f"{len(entries)} still waiting." if entries else "The queue is now empty."
    return f"Next up: {_label(front)}. {waiting}"


async def _handle_clear(community: str | None, *, provider: str, channel_id: str) -> str:
    """Empty the queue (moderator/broadcaster only)."""
    async with _queue_lock(community, provider=provider, channel_id=channel_id):
        entries = await _load_entries(community, provider=provider, channel_id=channel_id)
        if not entries:
            return "The queue is already empty."
        await _delete_entries(community, provider=provider, channel_id=channel_id)
    log.info("queue.cleared", removed=len(entries))
    return f"Cleared the queue ({len(entries)} removed)."


async def _run_command(
    command: str,
    payload: dict[str, Any],
    actor: object,
    community: str | None,
    *,
    provider: str,
    channel_id: str,
) -> str:
    """Run one validated command (permission-gated where needed) and return its chat reply."""
    if command == "usage":
        arg = payload.get("arg")
        if isinstance(arg, str):
            # Usage text leads the reply so a bot line never starts with `!`/`/`/`.`.
            return f"{_USAGE} ({sanitize_user_text(arg)})"
        return _USAGE

    if command in _MOD_COMMANDS and _caller_role_signal(payload) is not True:
        log.info("queue.permission_denied", command=command)
        return f"Only the broadcaster or a moderator can use !vqueue {command}."

    kwargs: dict[str, Any] = {"provider": provider, "channel_id": channel_id}
    if command == "join":
        return await _handle_join(community, actor, **kwargs)
    if command == "leave":
        return await _handle_leave(community, actor, **kwargs)
    if command == "position":
        return await _handle_position(community, actor, **kwargs)
    if command == "list":
        return await _handle_list(community, payload.get("arg"), **kwargs)
    if command == "next":
        return await _handle_next(community, **kwargs)
    return await _handle_clear(community, **kwargs)


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: permission checks, all `kv` I/O, then relay the reply.

    `config`/`http_client` are accepted but unused -- `kv` + the WIT `relay` import only.

    Raises:
        ValueError: The payload has no `channel_id`; the envelope's `community` is an empty
            string (a caller-side bug -- the host's tenant-wide sentinel is `None`, which works);
            or an unrecognized `command` (defensive -- `transform` only emits a known one).
        RuntimeError: A `kv` backend call failed or the stored queue is corrupt (a chat error
            reply and an ERROR log line are always emitted first).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("queue reply requires a channel_id from the inbound chat.message")
    command = payload.get("command")
    if command not in _KNOWN_COMMANDS:
        raise ValueError(f"unrecognized queue command: {command!r}")

    provider = envelope.event.platform
    community = envelope.community
    if community == "":
        log.error("queue.missing_community", command=command)
        await relay.push(
            provider,
            {
                "channel": channel_id,
                "text": "The queue isn't available here yet -- ask an admin to link this "
                "server/channel to a community.",
            },
        )
        raise ValueError("queue requires a community context (empty-string community)")

    try:
        reply_text = await _run_command(
            command,
            payload,
            envelope.event.actor,
            community,
            provider=provider,
            channel_id=channel_id,
        )
    except _BusyError:
        reply_text = _BUSY_MSG

    await relay.push(provider, {"channel": channel_id, "text": reply_text})
    log.info("queue.dispatch relayed", platform=provider, command=command)
    return DispatchResult(transport=provider, detail=command)
