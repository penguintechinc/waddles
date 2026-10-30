"""`!giveaway` / `!enter` -> a timed, FREE open-entry giveaway with one random winner.

Distinct from `raffle`: a raffle is mod-opened/closed by hand with no deadline, and its
`draw` leaves the entrant list in place; a giveaway is **timed** (`!giveaway start 5m`), entry
is free and open to everyone, and the draw picks exactly ONE winner and closes the giveaway.
(`raffle`'s source has no ticket price either -- the difference is the clock and the
single-winner lifecycle, not entry cost.)

Commands
--------
- ``!giveaway start <duration>`` -- MOD-ONLY. ``<duration>`` is ``<n>s|m|h`` (``30s``, ``5m``,
  ``1h``), at least 1s and at most 24h. A bad duration gets an explicit reply. Only ONE giveaway
  per community at a time: a second ``start`` is rejected (also while an ended giveaway is still
  waiting for ``draw``/``cancel``).
- ``!enter`` (alias ``!giveaway enter``) -- anyone, ONCE per giveaway (deduped by
  ``sha256(platform + author)``). Replies if none is active, the caller already entered, or
  the giveaway's time is up.
- ``!giveaway draw`` -- MOD-ONLY. Picks one random entrant, announces it, closes the giveaway.
  No entries -> explicit "no entries" reply and the giveaway stays open (``cancel`` to abandon).
  A mod may draw before the timer runs out.
- ``!giveaway status`` -- anyone: entry count + time remaining.
- ``!giveaway cancel`` -- MOD-ONLY. Closes without drawing. Also the recovery path for a
  corrupt stored state (it deletes the key without parsing it).

`start`/`draw`/`cancel` are NOT in `waddle_sdk.command.VERBS`, so -- exactly like `raffle`'s
`open`/`close`/`draw` -- they are special-cased here instead of going through `parse_command`.

State
-----
One `kv` key per community, ``giveaway.active`` = JSON ``{"ends_at": <epoch ms>, "entries":
[<sha256 hex>, ...]}``. Community scoping is done by the `kv` host capability server-side (by
tenant, community, app_id), so the key is a plain literal using ``.`` -- never ``:`` (the host's
reserved separator, gh-631). The key is deleted when the giveaway is drawn or cancelled. Entries
are capped at `MAX_ENTRIES` so the stored JSON stays under the host's `too_large` limit.
`kv` has no compare-and-swap, so two `!enter`s processed concurrently could lose one entry
(read-modify-write on one key); a lost free entry is the accepted trade-off against adding a
lock capability, and the viewer simply re-enters (the dedupe makes that safe).

Randomness
----------
Same primitive as `raffle`/`winner`/`slots`: a module-level ``random.SystemRandom()`` (`_RNG`,
the OS CSPRNG -- `wasi:random` in the component), so the draw is unpredictable and cannot be
reconstructed from observable inputs (clock, entrant list). SystemRandom holds no seeded state,
so the componentize-py build snapshot has nothing to freeze. A value-distributing draw must be
fair, hence never a seeded Mersenne Twister.
The winner is announced as the entrant's pseudonym tag (`hash[:8]`) because only hashes are
stored (same PII trade-off as `raffle`: a viewer can recognise their own tag, resolving it to a
display name needs the PII-tokenization pipeline).

Failure handling
----------------
Activation is enforced by the bundle runtime (installed -> tenant enabled -> community activated)
before `transform()` is called. Inside `transform()`, the cheap head match comes first, then the
moderator gate (fail-closed: absent or non-boolean badges deny, via the strict `is True` read),
then `kv`. A `kv` failure or a corrupt stored value becomes a private
`_KvError`, is logged at ERROR and answered with an explicit reply -- never a silent no-op, never
a default substituted for a bad parse. Logs are PII-free: op names, counts and exception types
only, never message text or the actor.

`kv`, `log`, `relay` and `clock` are imported at MODULE LOAD on purpose:
componentize-py only wizens a WIT host-import that is imported eagerly
(`docs/reference/bundle-host-imports.md`).
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import dataclass, field
from typing import Any, cast

from waddle_sdk import clock, kv, log, relay
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

#: The single `kv` key this bundle owns (community-scoped by the host; `.`, never `:`).
#: OS-entropy RNG (`os.urandom`, i.e. `wasi:random` in the component). Never the module-level
#: `random.*` functions or a seeded `random.Random`: predictable / frozen into the build snapshot.
_RNG = random.SystemRandom()

ACTIVE_KEY = "giveaway.active"

_GIVEAWAY_HEAD = "!giveaway"
_ENTER_HEAD = "!enter"

_SECOND_MS = 1000
_MINUTE_MS = 60 * _SECOND_MS
_HOUR_MS = 60 * _MINUTE_MS

#: Duration bounds: at least one second, at most 24 hours.
MIN_DURATION_MS = _SECOND_MS
MAX_DURATION_MS = 24 * _HOUR_MS

#: Caps one giveaway's stored entrant list (keeps the JSON under the host's kv size limit).
MAX_ENTRIES = 5000

_UNIT_MS = {"s": _SECOND_MS, "m": _MINUTE_MS, "h": _HOUR_MS}
_DURATION_RE = re.compile(r"(\d{1,6})([smh])", re.IGNORECASE)

_USAGE = (
    "Usage: !enter | !giveaway status | !giveaway start <5m|30s|1h> | "
    "!giveaway draw | !giveaway cancel (start/draw/cancel are mod only)"
)
_PERMISSION_DENIED_MSG = "only moderators/broadcasters can do that"
_KV_FAILURE_REPLY = "Something went wrong with giveaway storage - please try again."
_MOD_COMMANDS = frozenset({"start", "draw", "cancel"})
_NO_ACTIVE_MSG = "no giveaway is running right now"


class DurationError(ValueError):
    """A `start` duration was malformed or out of range; `str(exc)` is the chat reply."""


class _KvError(Exception):
    """Internal-only: a `kv` host call failed, or the stored giveaway is corrupt.

    Always caught inside `transform`, never leaked. `kind` is the exception type name (or a
    short structural reason) that is safe to log.
    """

    def __init__(self, message: str, *, kind: str) -> None:
        """Record the diagnostic message and a PII-free `kind` for the ERROR log."""
        super().__init__(message)
        self.kind = kind


@dataclass(slots=True)
class Giveaway:
    """One community's active giveaway: deadline (epoch ms) and hashed entrant ids."""

    ends_at: int
    entries: list[str] = field(default_factory=list)


def parse_duration_ms(token: str) -> int:
    """Parse ``30s`` / ``5m`` / ``1h`` into milliseconds, enforcing the 1s..24h bounds.

    Raises:
        DurationError: Not ``<digits><s|m|h>``, zero, or longer than 24 hours.
    """
    match = _DURATION_RE.fullmatch(token.strip())
    if match is None:
        raise DurationError(f"'{token[:20]}' is not a valid duration -- use e.g. 30s, 5m or 1h")
    duration_ms = int(match.group(1)) * _UNIT_MS[match.group(2).lower()]
    if duration_ms < MIN_DURATION_MS:
        raise DurationError("the duration must be at least 1s")
    if duration_ms > MAX_DURATION_MS:
        raise DurationError("the duration can be at most 24h")
    return duration_ms


def humanize_duration(duration_ms: int) -> str:
    """Render milliseconds as ``1h 2m 3s`` (largest non-zero unit first; ``0s`` if <= 0)."""
    total_s = max(duration_ms, 0) // _SECOND_MS
    hours, rem = divmod(total_s, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _is_privileged(payload: dict[str, Any]) -> bool:
    """`True` only for a genuine moderator/broadcaster -- strict-boolean, fail-closed.

    Absent badge fields (e.g. Discord's normalizer today), strings like ``"false"``/``"true"``
    and ints all deny: only the real boolean ``True`` admits (fix/bundle-defects-wave).
    """
    return payload.get("is_mod") is True or payload.get("is_broadcaster") is True


def _pseudonym(platform: str, author: str) -> str:
    """Non-reversible dedupe key: ``sha256(platform + author)`` -- never a raw identity."""
    return hashlib.sha256(f"{platform}\x1f{author}".encode()).hexdigest()


def _pick_winner(entries: list[str]) -> str:
    """Pick one entrant uniformly with the OS CSPRNG (`_RNG`).

    See the module docstring's Randomness section. Isolated so tests can patch the draw.
    """
    return _RNG.choice(entries)


async def _kv_get(key: str) -> bytes | None:
    """`kv.get`, reclassifying the generated WIT `Err` into `_KvError` (fail loud)."""
    try:
        result = await kv.get(key)
    except Exception as exc:  # classified like waddle_sdk.db/http
        raise _KvError(f"kv.get({key!r}) failed", kind=type(exc).__name__) from exc
    # `waddle_sdk` ships no `py.typed`, so mypy sees `Any` -- cast back to the real contract.
    return cast("bytes | None", result)


async def _kv_set(key: str, value: bytes) -> None:
    """`kv.set` (no TTL -- a giveaway lives until drawn or cancelled), `Err` -> `_KvError`."""
    try:
        await kv.set(key, value, ttl_seconds=0)
    except Exception as exc:
        raise _KvError(f"kv.set({key!r}) failed", kind=type(exc).__name__) from exc


async def _kv_delete(key: str) -> None:
    """`kv.delete`, reclassifying the generated WIT `Err` into `_KvError`."""
    try:
        await kv.delete(key)
    except Exception as exc:
        raise _KvError(f"kv.delete({key!r}) failed", kind=type(exc).__name__) from exc


def _decode_giveaway(raw: bytes) -> Giveaway:
    """Decode the stored JSON, raising `_KvError` (never a default) on any corruption."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _KvError("corrupt giveaway.active", kind=type(exc).__name__) from exc
    if not isinstance(data, dict):
        raise _KvError("corrupt giveaway.active: not an object", kind="NotAnObject")
    ends_at = data.get("ends_at")
    entries = data.get("entries")
    if not isinstance(ends_at, int) or isinstance(ends_at, bool):
        raise _KvError("corrupt giveaway.active: bad ends_at", kind="BadEndsAt")
    if not isinstance(entries, list) or not all(isinstance(e, str) for e in entries):
        raise _KvError("corrupt giveaway.active: bad entries", kind="BadEntries")
    return Giveaway(ends_at=ends_at, entries=list(entries))


async def _load() -> Giveaway | None:
    """The community's active giveaway, or `None` when none exists."""
    raw = await _kv_get(ACTIVE_KEY)
    if raw is None:
        return None
    return _decode_giveaway(raw)


async def _save(giveaway: Giveaway) -> None:
    """Persist the active giveaway."""
    payload = {"ends_at": giveaway.ends_at, "entries": giveaway.entries}
    await _kv_set(ACTIVE_KEY, json.dumps(payload).encode("utf-8"))


async def _handle_start(duration_token: str) -> str:
    """`!giveaway start <duration>` -- open a new giveaway unless one already exists."""
    duration_ms = parse_duration_ms(duration_token)  # DurationError -> explicit reply
    now_ms = clock.now_millis()
    existing = await _load()
    if existing is not None:
        if now_ms >= existing.ends_at:
            return (
                "a giveaway has ended and is waiting -- use !giveaway draw or "
                "!giveaway cancel first"
            )
        return (
            "a giveaway is already running "
            f"({humanize_duration(existing.ends_at - now_ms)} left) -- use !giveaway cancel "
            "to stop it"
        )
    await _save(Giveaway(ends_at=now_ms + duration_ms))
    log.info("giveaway.started", duration_ms=duration_ms)
    return f"A giveaway is open for {humanize_duration(duration_ms)}! Type !enter to join."


async def _handle_enter(platform: str, author: str | None) -> str:
    """`!enter` -- add the caller to the active giveaway, once."""
    if not author:
        return "couldn't identify you, so you can't be entered"
    giveaway = await _load()
    if giveaway is None:
        return f"{_NO_ACTIVE_MSG} -- ask a mod to !giveaway start"
    if clock.now_millis() >= giveaway.ends_at:
        return "the giveaway has ended -- waiting for a mod to draw a winner"
    pseudonym = _pseudonym(platform, author)
    if pseudonym in giveaway.entries:
        return f"{author}, you're already entered!"
    if len(giveaway.entries) >= MAX_ENTRIES:
        return f"the giveaway is full ({MAX_ENTRIES} max entries)"
    giveaway.entries.append(pseudonym)
    await _save(giveaway)
    log.info("giveaway.entered", entries=len(giveaway.entries))
    return f"{author} entered the giveaway! ({len(giveaway.entries)} entered)"


async def _handle_draw() -> str:
    """`!giveaway draw` -- pick one random entrant, announce it, close the giveaway."""
    giveaway = await _load()
    if giveaway is None:
        return f"{_NO_ACTIVE_MSG} -- nothing to draw"
    if not giveaway.entries:
        return "no entries yet -- nobody has used !enter (use !giveaway cancel to close it)"
    winner = _pick_winner(giveaway.entries)
    await _kv_delete(ACTIVE_KEY)
    log.info("giveaway.drawn", entries=len(giveaway.entries))
    return (
        f"The giveaway winner is entrant {winner[:8]} out of {len(giveaway.entries)} "
        "entries! Congratulations - contact a mod to claim your prize."
    )


async def _handle_status() -> str:
    """`!giveaway status` -- entry count and time remaining."""
    giveaway = await _load()
    if giveaway is None:
        return _NO_ACTIVE_MSG
    count = len(giveaway.entries)
    remaining_ms = giveaway.ends_at - clock.now_millis()
    if remaining_ms <= 0:
        return f"the giveaway has ended with {count} entries -- waiting for a mod to draw"
    return f"giveaway running: {count} entries, {humanize_duration(remaining_ms)} left"


async def _handle_cancel() -> str:
    """`!giveaway cancel` -- close without drawing; deletes the key without parsing it."""
    raw = await _kv_get(ACTIVE_KEY)
    if raw is None:
        return f"{_NO_ACTIVE_MSG} -- nothing to cancel"
    await _kv_delete(ACTIVE_KEY)
    log.info("giveaway.cancelled")
    return "the giveaway was cancelled -- no winner drawn."


def _resolve(head: str, rest: str) -> tuple[str, str]:
    """Map the head and trailing text onto `(command, argument)`; `usage` on a bad shape."""
    if head == _ENTER_HEAD:
        return ("usage", "") if rest else ("enter", "")
    verb, _, arg = rest.partition(" ")
    verb = verb.lower()
    arg = arg.strip()
    if verb == "start":
        return ("start", arg) if arg and " " not in arg else ("usage", "")
    if verb in ("draw", "cancel", "status", "enter"):
        return ("usage", "") if arg else (verb, "")
    return ("usage", "")


async def _execute(command: str, arg: str, event: PlatformEvent) -> str:
    """Run one resolved command against `kv` and return the chat reply."""
    if command == "start":
        return await _handle_start(arg)
    if command == "enter":
        return await _handle_enter(event.platform, event.actor)
    if command == "draw":
        return await _handle_draw()
    if command == "cancel":
        return await _handle_cancel()
    return await _handle_status()


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize the commands and build the reply.

    Returns `None` for a non-chat payload or any message that is not `!giveaway`/`!enter`.
    Activation is enforced by the runtime before this stage runs. Mod-only commands from a non-moderator are
    answered with a denial before any `kv` access. A bad duration replies with the parse error;
    a `kv` failure logs at ERROR and replies with an explicit failure notice.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    head, _, rest = text.strip().partition(" ")
    head = head.lower()
    if head not in (_GIVEAWAY_HEAD, _ENTER_HEAD):
        return None

    command, arg = _resolve(head, rest.strip())
    if command in _MOD_COMMANDS and not _is_privileged(event.payload):
        log.info("giveaway.permission_denied", command=command)
        reply = _PERMISSION_DENIED_MSG
    elif command == "usage":
        log.debug("giveaway.usage_error")
        reply = _USAGE
    else:
        try:
            reply = await _execute(command, arg, event)
        except DurationError as exc:
            log.debug("giveaway.bad_duration")
            reply = str(exc)
        except _KvError as exc:
            log.error("giveaway.kv_failure", command=command, error_type=exc.kind)
            reply = _KV_FAILURE_REPLY

    log.info("giveaway.transform matched", command=command)
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
        raise ValueError("giveaway reply requires a channel_id from the inbound chat.message")
    if not isinstance(text, str) or not text:
        raise ValueError("giveaway reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("giveaway.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
