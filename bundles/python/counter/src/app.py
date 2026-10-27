r"""`!counter` -> generic named counters, per community (deaths, wins, fails, ...).

`!counter <name>` shows a counter; `!counter add|sub <name> [n]` changes it (n defaults to 1),
`!counter set <name> <n>` sets it, `!counter reset <name>` zeroes it, `!counter del <name>`
removes it, `!counter list` shows every counter with its value. A counter is created by the first
`add` or `set`. Idea-inspired by the counter features of popular chat bots (concepts only); the
code here is original and shares nothing with the `count` bundle, whose counters are bare
`!<name>` commands -- this one is a single `!counter` namespace, so it never claims arbitrary
`!` words and costs zero kv ops for any message that is not `!counter ...`.

Grammar: `waddle_sdk.command.parse_command` with the shared verb vocabulary (`add`, `sub`, `set`,
`reset`, `remove`/`delete`, `list`; `del` is folded onto `delete` first) and one declared
sub-module, `public`. The bare `!counter <name>` read is the one deliberate pre-check outside the
parser (a counter name is not a verb -- the same layering `quote`'s `!quote <id>` uses).

Permission model -- mutation is broadcaster/moderator only by default, strictly
`is_mod is True or is_broadcaster is True` (a string `"false"` badge is not a moderator; neither
badge present, as on Discord today, is a denial: fail closed). It is configurable per community:
`!counter enable public` (mod-only) lets ANY chatter `add`/`sub` exactly 1 on counters that
already exist -- they cannot pick an amount, create counters, `set`, `reset` or delete, so open
mode cannot be used to vandalise a total or fill the community's counter cap. `!counter disable
public` closes it again (default OFF); `!counter public` reports the state. Reads are always open.

State (kv only; the host scopes every key by tenant/community/app_id, so communities never see
each other's counters): `counter.value.<name>` (integer as ASCII text; mutated with the atomic
`kv.increment`, so concurrent `!counter add` calls never lose an update -- the value key's
existence is what "the counter exists" means), `counter.names` (JSON index driving `list` and the
cap; `MAX_COUNTERS` per community), `counter.lock` (a 5 s claim taken with `kv.increment`, the only
compare-and-swap, so two creations racing cannot lose an index update), `counter.public` (the
open-mode flag). Keys use `.`, never `:` (gh-631). Names are lowercase `[a-z0-9_-]`, <= 32,
first character alphanumeric, and may not be a command verb or `public`. Amounts are whole numbers
with |n| <= `MAX_AMOUNT`; a total that would overflow the host's signed 64-bit counter is refused
by the host and answered with an explicit error reply.

Output safety: no user-authored text is ever stored or echoed -- replies are built from static
strings, validated counter names (charset above; an invalid name is rejected, never echoed) and
integers, and a rejected number never echoes what was typed. Every reply still passes through
`waddle_sdk.safe_text.sanitize_user_text` (a byte-identical no-op for these strings; it is the
defence if a future edit echoes something). A reply cannot start with `/`, `.` or `!`: it begins
with a counter name (first character alphanumeric) or a fixed sentence.

PII-free: this bundle never reads `event.actor` and stores no actor identity at all; kv keys hold
only mod-chosen counter names; logs carry only the operation and counts (`op`, `count`) -- never a
counter name, an amount or an argument. Failures are never swallowed: a kv failure logs
`counter.kv_failure` (exception class name only) and still replies with an explicit error.

Costs: show 1 kv op; `add`/`sub` on an existing counter 2 (get + increment; the existence check
is what keeps open mode from creating counters); first `add`/`set` creating a counter ~6;
`list` 1 + up to `MAX_LIST_VALUES`.

Activation-gated only -- no feature flag: the platform's installed -> enabled -> activated check
decides whether this bundle runs.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any, cast

from waddle_sdk import kv, log, relay
from waddle_sdk.command import VERBS, CommandSpec, CommandUsageError, ParsedCommand, parse_command
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.safe_text import sanitize_user_text

SPEC = CommandSpec(name="counter", sub_modules=frozenset({"public"}))

COMMAND = "!counter"

NAMES_KEY = "counter.names"
VALUE_KEY_PREFIX = "counter.value."
LOCK_KEY = "counter.lock"
PUBLIC_KEY = "counter.public"
#: A creation claims `LOCK_KEY` for this long at most (released as soon as the work ends).
LOCK_TTL_SECONDS = 5

MAX_NAME_LEN = 32
MAX_COUNTERS = 100
#: `!counter list` shows values for this many counters (one kv.get each, well under the 64-op cap).
MAX_LIST_VALUES = 15
#: `!counter list` stops adding entries once the reply would pass this (chat messages are short).
MAX_LIST_REPLY_LEN = 380
#: |n| bound for `add`/`sub`/`set` amounts -- an s64 `kv.increment` would take ~9e6 of these to
#: overflow, and the host refuses an overflow anyway.
MAX_AMOUNT = 10**12

_NAME_ALLOWED_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-")
_NAME_FIRST_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")
_AMOUNT_RE = re.compile(r"^[+-]?[0-9]{1,13}$")

#: Verbs this bundle accepts that the shared `parse_command` vocabulary spells differently.
_VERB_ALIASES = {"del": "delete"}

#: First words that are grammar, not counter names (so `!counter add` is never a read of a counter
#: called "add"): every shared verb, their aliases, the `public` sub-module and `help`.
RESERVED_WORDS: frozenset[str] = frozenset(VERBS) | frozenset(_VERB_ALIASES) | {"public", "help"}

_USAGE = (
    "Usage: !counter <name> | !counter add|sub <name> [n] | !counter set <name> <n> | "
    "!counter reset <name> | !counter del <name> | !counter list"
)
_DENIED = "Only the broadcaster or a moderator can change counters."
_DENIED_AMOUNT = "Only the broadcaster or a moderator can change a counter by a custom amount."
_BUSY = "Another change is in progress - please try again."
_UNAVAILABLE = "Counter storage is unavailable right now - please try again."
_NOT_A_NUMBER = "That isn't a whole number between -1000000000000 and 1000000000000."


class _KvFailure(Exception):
    """Internal-only: a `kv` host call failed. Always caught in `transform`, never leaked."""


async def _kv_get(key: str) -> bytes | None:
    """`kv.get`, reclassifying the generated WIT `Err` into `_KvFailure` (fail-loud)."""
    try:
        result: bytes | None = await kv.get(key)
    except Exception as exc:
        raise _KvFailure("kv.get failed") from exc
    return result


async def _kv_set(key: str, value: bytes) -> None:
    """`kv.set` (no TTL -- counters persist), reclassifying `Err` into `_KvFailure`."""
    try:
        await kv.set(key, value, ttl_seconds=0)
    except Exception as exc:
        raise _KvFailure("kv.set failed") from exc


async def _kv_delete(key: str) -> None:
    """`kv.delete`, reclassifying `Err` into `_KvFailure`."""
    try:
        await kv.delete(key)
    except Exception as exc:
        raise _KvFailure("kv.delete failed") from exc


async def _kv_increment(key: str, delta: int, ttl_seconds: int = 0) -> int:
    """`kv.increment`, reclassifying `Err` into `_KvFailure`."""
    try:
        result: int = await kv.increment(key, delta, ttl_seconds=ttl_seconds)
    except Exception as exc:
        raise _KvFailure("kv.increment failed") from exc
    return result


def _value_key(name: str) -> str:
    """Return the kv key one counter's integer value lives under (`name` is validated)."""
    return f"{VALUE_KEY_PREFIX}{name}"


def _parse_value(raw: bytes) -> int:
    """Decode a stored counter value, or raise `_KvFailure` for a corrupt one (never guess 0)."""
    try:
        return int(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _KvFailure("corrupt counter value") from exc


async def _load_names() -> list[str]:
    """Return the community's counter names, `[]` if none exist yet.

    Raises:
        _KvFailure: The kv call failed or the stored index is corrupt -- never silently reset to
            `[]` (that would drop the whole index on the next write).
    """
    raw = await _kv_get(NAMES_KEY)
    if raw is None:
        return []
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _KvFailure("corrupt counter index") from exc
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        raise _KvFailure("corrupt counter index: expected a JSON array of strings")
    return cast("list[str]", data)


async def _save_names(names: list[str]) -> None:
    """Persist the counter-name index as a sorted, de-duplicated JSON array."""
    await _kv_set(NAMES_KEY, json.dumps(sorted(set(names))).encode("utf-8"))


def _is_privileged(event: PlatformEvent) -> bool:
    """Broadcaster/moderator check -- strict `is True`, fails CLOSED when no badge is present."""
    return event.payload.get("is_mod") is True or event.payload.get("is_broadcaster") is True


def _normalize_name(raw: str) -> str:
    """Strip an optional leading `!` and lowercase -- `!Deaths` and `deaths` both yield `deaths`."""
    return raw.strip().lstrip("!").strip().lower()


def _name_error(name: str) -> str | None:
    """Return an error message, or `None` if `name` is a legal counter name (never echoes it)."""
    if not name:
        return "A counter name is required, e.g. `!counter add deaths`."
    if len(name) > MAX_NAME_LEN:
        return f"Counter names must be {MAX_NAME_LEN} characters or fewer."
    if name[0] not in _NAME_FIRST_CHARS or any(ch not in _NAME_ALLOWED_CHARS for ch in name):
        return (
            "Counter names may only contain lowercase letters, digits, '_' and '-', "
            "and must start with a letter or digit."
        )
    if name in RESERVED_WORDS:
        return "That name is a counter command word and can't be used as a counter name."
    return None


def _missing(name: str) -> str:
    """Return the reply for a counter that doesn't exist (`name` is a validated counter name)."""
    return (
        f"Counter '{name}' doesn't exist yet - a moderator can create it with !counter add {name}"
    )


def _parse_amount(raw: str) -> int | None:
    """Parse a whole-number amount within `MAX_AMOUNT`, or `None` (never echoed back)."""
    if not _AMOUNT_RE.match(raw):
        return None
    value = int(raw)
    return value if abs(value) <= MAX_AMOUNT else None


async def _run_locked(work: Callable[[], Awaitable[str | None]]) -> str | None:
    """Run `work()` while holding the creation claim; `None` from `work` means success.

    `kv.increment` is the only compare-and-swap: it returns 1 to exactly one caller, so a second
    creation racing this one is told to retry instead of racing the names-index write.
    """
    claimed = await _kv_increment(LOCK_KEY, 1, LOCK_TTL_SECONDS)
    if claimed != 1:
        log.info("counter.busy")
        return _BUSY
    try:
        return await work()
    finally:
        await _kv_delete(LOCK_KEY)


async def _register(name: str) -> str | None:
    """Add `name` to the index (cap-checked, idempotent). Returns an error reply, else `None`."""

    async def work() -> str | None:
        names = await _load_names()
        if name in names:
            return None
        if len(names) >= MAX_COUNTERS:
            return f"This community already has the maximum of {MAX_COUNTERS} counters."
        names.append(name)
        await _save_names(names)
        return None

    return await _run_locked(work)


def _split_name_and_amount(
    args: str | None, *, takes_amount: bool
) -> tuple[str, str | None, str | None]:
    """Split `<name> [n]` into `(name, amount_text, error)`; exactly one of name/error is empty.

    Never echoes the typed text: a bad name or an extra word is answered with a fixed sentence.
    """
    words = (args or "").split()
    if not words:
        return "", None, _USAGE
    name = _normalize_name(words[0])
    error = _name_error(name)
    if error is not None:
        return "", None, error
    extra = words[1:]
    if len(extra) > (1 if takes_amount else 0):
        return "", None, _USAGE
    return name, (extra[0] if extra else None), None


async def _handle_show(name: str) -> str:
    """`!counter <name>`: show one counter (open to everyone)."""
    raw = await _kv_get(_value_key(name))
    if raw is None:
        return _missing(name)
    return f"{name}: {_parse_value(raw)}"


async def _handle_delta(args: str | None, *, sign: int, event: PlatformEvent) -> str:
    """`!counter add|sub <name> [n]`: mods any amount (add creates); open mode: +-1 on existing."""
    name, amount_text, error = _split_name_and_amount(args, takes_amount=True)
    if error is not None:
        return error
    privileged = _is_privileged(event)
    if not privileged:
        if await _kv_get(PUBLIC_KEY) != b"1":
            log.info("counter.permission_denied", op="delta")
            return _DENIED
        if amount_text is not None:
            log.info("counter.permission_denied", op="delta_amount")
            return _DENIED_AMOUNT
    amount = 1
    if amount_text is not None:
        parsed = _parse_amount(amount_text)
        if parsed is None:
            return _NOT_A_NUMBER
        amount = parsed

    key = _value_key(name)
    if await _kv_get(key) is None:
        if not privileged or sign < 0:
            return _missing(name)
        registered = await _register(name)
        if registered is not None:
            return registered
    total = await _kv_increment(key, sign * amount)
    log.info("counter.changed", op="add" if sign > 0 else "sub")
    return f"{name}: {total}"


async def _handle_set(args: str | None) -> str:
    """`!counter set <name> <n>` (mod only; creates the counter when missing)."""
    name, amount_text, error = _split_name_and_amount(args, takes_amount=True)
    if error is not None:
        return error
    if amount_text is None:
        return "Usage: !counter set <name> <n>"
    amount = _parse_amount(amount_text)
    if amount is None:
        return _NOT_A_NUMBER
    key = _value_key(name)
    if await _kv_get(key) is None:
        registered = await _register(name)
        if registered is not None:
            return registered
    await _kv_set(key, str(amount).encode("utf-8"))
    log.info("counter.changed", op="set")
    return f"{name}: {amount}"


async def _handle_reset(args: str | None) -> str:
    """`!counter reset <name>` (mod only): back to 0; the counter must already exist."""
    name, _, error = _split_name_and_amount(args, takes_amount=False)
    if error is not None:
        return error
    key = _value_key(name)
    if await _kv_get(key) is None:
        return f"Counter '{name}' doesn't exist."
    await _kv_set(key, b"0")
    log.info("counter.changed", op="reset")
    return f"{name}: 0"


async def _handle_delete(args: str | None) -> str:
    """`!counter del <name>` (mod only): remove the counter and its index entry (idempotent)."""
    name, _, error = _split_name_and_amount(args, takes_amount=False)
    if error is not None:
        return error

    async def work() -> str | None:
        names = await _load_names()
        exists = name in names or await _kv_get(_value_key(name)) is not None
        if not exists:
            return f"Counter '{name}' doesn't exist."
        await _kv_delete(_value_key(name))
        if name in names:
            names.remove(name)
            await _save_names(names)
        log.info("counter.removed", op="del", count=len(names))
        return None

    failure = await _run_locked(work)
    return failure if failure is not None else f"Removed counter '{name}'."


async def _handle_list() -> str:
    """`!counter list`: counters with their values (open to everyone), one chat message long."""
    names = sorted(await _load_names())
    if not names:
        return "No counters have been created yet."
    head = f"Counters ({len(names)}): "
    shown: list[str] = []
    length = len(head)
    for name in names[:MAX_LIST_VALUES]:
        raw = await _kv_get(_value_key(name))
        entry = f"{name}: {_parse_value(raw) if raw is not None else 0}"
        length += len(entry) + 2
        if length > MAX_LIST_REPLY_LEN:
            break
        shown.append(entry)
    reply = head + ", ".join(shown)
    if len(shown) < len(names):
        reply += f" ... and {len(names) - len(shown)} more"
    return reply


async def _handle_public(parsed: ParsedCommand, event: PlatformEvent) -> str:
    """`!counter enable|disable public` (mod only) and the bare `!counter public` status."""
    if parsed.option is None:
        state = "on" if await _kv_get(PUBLIC_KEY) == b"1" else "off"
        return (
            f"Public add/sub is {state}. A moderator can change it: !counter enable|disable public"
        )
    if parsed.option not in ("enable", "disable"):
        return _USAGE
    if not _is_privileged(event):
        log.info("counter.permission_denied", op="public")
        return _DENIED
    if parsed.option == "enable":
        await _kv_set(PUBLIC_KEY, b"1")
        log.info("counter.public", op="enable")
        return "Public add/sub is now on: anyone can add or subtract 1 on an existing counter."
    await _kv_delete(PUBLIC_KEY)
    log.info("counter.public", op="disable")
    return "Public add/sub is now off: only the broadcaster or a moderator can change counters."


def _canonical_text(words: list[str]) -> str:
    """Rebuild `!counter <verb> <tail>`, folding this bundle's verb aliases onto the shared ones."""
    verb = _VERB_ALIASES.get(words[0].lower(), words[0])
    return " ".join([COMMAND, verb, *words[1:]])


async def _handle(rest: str, event: PlatformEvent) -> str:
    """Handle `!counter ...`; always produces a reply (never silence for a recognised command)."""
    words = rest.split()
    if not words or words[0].lower() == "help":
        return _USAGE
    if words[0].lower() not in RESERVED_WORDS:
        if len(words) > 1:
            return _USAGE
        name = _normalize_name(words[0])
        return _name_error(name) or await _handle_show(name)

    try:
        parsed = parse_command(_canonical_text(words), SPEC)
    except CommandUsageError:
        log.debug("counter.usage_error")
        return _USAGE
    if parsed.sub_module is not None:
        return await _handle_public(parsed, event)

    option = parsed.option
    if option == "list":
        return await _handle_list()
    if option in ("add", "sub"):
        return await _handle_delta(parsed.args, sign=1 if option == "add" else -1, event=event)
    if option in ("set", "reset", "delete", "remove"):
        if not _is_privileged(event):
            log.info("counter.permission_denied", op=option)
            return _DENIED
        if option == "set":
            return await _handle_set(parsed.args)
        if option == "reset":
            return await _handle_reset(parsed.args)
        return await _handle_delete(parsed.args)
    return _USAGE


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!counter ...` only.

    Returns `None` for non-chat payloads and for any text whose first token is not exactly
    `!counter` (zero kv cost). On a kv failure logs `counter.kv_failure` (exception class only)
    and replies with an explicit error -- never a silent default.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    parts = text.strip().split(None, 1)
    if not parts or parts[0].lower() != COMMAND:
        return None
    rest = parts[1] if len(parts) > 1 else ""

    try:
        reply = await _handle(rest, event)
    except _KvFailure as exc:
        cause = exc.__cause__
        log.error(
            "counter.kv_failure",
            error_type=type(cause).__name__ if cause else type(exc).__name__,
        )
        reply = _UNAVAILABLE

    log.info("counter.transform matched")
    sanitized: str = sanitize_user_text(reply)
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"channel_id": event.payload.get("channel_id"), "text": sanitized},
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
        raise ValueError("counter reply requires a channel_id from the inbound chat.message")
    if not isinstance(text, str) or not text:
        raise ValueError("counter reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("counter.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
