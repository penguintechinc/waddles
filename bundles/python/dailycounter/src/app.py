"""`!dailycounter` -> per-community named counters that roll over every UTC day.

Like `count`, but each counter holds one independent value PER DAY (e.g. "deaths today"):
`!dailycounter add <name>` creates a counter, `!dailycounter remove <name>` deletes it and
`!dailycounter list` (or bare `!dailycounter`) lists them. Once created, `!<name>` reads
today's value and `!<name> add [N]` / `sub [N]` / `set <N>` / `reset` mutate it (N defaults
to 1). Verbs follow the core command vocabulary (set/add/sub/remove/list/reset, bare=read).

State (all `kv`, scoped server-side per `(tenant, community, app_id)` so counters are
per-community by construction; keys use `.` never `:` -- gh-631):

* ``dailycounter.registry``               -- JSON array of live counter names
* ``dailycounter.value.<name>.<YYYY-MM-DD>`` -- that counter's integer for that UTC day

The day comes from the event's own `occurred_at` (UTC date), falling back to the host clock
when absent. Day keys carry a 30-day TTL so history self-expires; `remove` drops the
registry entry and today's key (older day keys expire on their own). Everything is
community-level state -- no user identity is stored, hashed or logged.

Permission model: every mutation (create/remove/add/sub/set/reset) is broadcaster/
moderator only and FAILS CLOSED -- if neither `is_mod` nor `is_broadcaster` is a real
`bool` on the event (every Discord message today) the mutation is denied. Reading and
`list` are open. Counter names may collide with commands owned by other bundles (there's no
cross-bundle registry to check); a small reserved set is rejected, the rest is the
moderator's responsibility. At most `MAX_COUNTERS` per community.

kv cost: a message without a leading `!` costs zero kv ops; any other `!token` costs one
`kv.get` of the registry (a dynamic name can't be declared in the static `command_prefix`
filter). Logging is PII-free: op names, counts, exception type names -- never user text.
`kv` failures are logged (`dailycounter.kv_failure`, exception type only) AND answered
with an explicit error reply; corrupt stored data is treated as a failure, never reset.

Gated behind the PostHog flag ``waddles.command-dailycounter``.
"""

from __future__ import annotations

import json
import re
from typing import Any, cast

from waddle_sdk import clock, kv, log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-dailycounter"
MANAGEMENT_COMMAND = "!dailycounter"
REGISTRY_KEY = "dailycounter.registry"
VALUE_KEY_PREFIX = "dailycounter.value."

#: Per-day values expire after 30 days -- bounds storage, history beyond that is gone.
TTL_SECONDS = 30 * 24 * 60 * 60
MAX_COUNTERS = 50
MAX_NAME_LEN = 32
_NAME_ALLOWED_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-")
_RESERVED_NAMES = frozenset(
    {"dailycounter", "alias", "lastseen", "weather", "count", "command", "list", "add", "remove"}
)
_DAY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")

_S64_MIN = -(2**63)
_S64_MAX = 2**63 - 1

_USAGE = "Usage: !dailycounter add <name> | !dailycounter remove <name> | !dailycounter list"


class _KvFailure(Exception):
    """Internal-only: a `kv` host call failed. Always caught in `transform`, never leaked."""


async def _kv_get(key: str) -> bytes | None:
    """`kv.get`, reclassifying the generated WIT `Err` into `_KvFailure` (fail-loud)."""
    try:
        result = await kv.get(key)
    except Exception as exc:  # noqa: BLE001 - classified like waddle_sdk.db/http
        raise _KvFailure("kv.get failed") from exc
    return cast("bytes | None", result)


async def _kv_set(key: str, value: bytes, ttl_seconds: int = 0) -> None:
    """`kv.set`, reclassifying `Err` into `_KvFailure`."""
    try:
        await kv.set(key, value, ttl_seconds=ttl_seconds)
    except Exception as exc:  # noqa: BLE001
        raise _KvFailure("kv.set failed") from exc


async def _kv_delete(key: str) -> None:
    """`kv.delete`, reclassifying `Err` into `_KvFailure`."""
    try:
        await kv.delete(key)
    except Exception as exc:  # noqa: BLE001
        raise _KvFailure("kv.delete failed") from exc


async def _kv_increment(key: str, delta: int) -> int:
    """`kv.increment` with the day TTL, reclassifying `Err` into `_KvFailure`."""
    try:
        result = await kv.increment(key, delta, ttl_seconds=TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        raise _KvFailure("kv.increment failed") from exc
    return cast(int, result)


def _today(event: PlatformEvent) -> str:
    """UTC `YYYY-MM-DD` for the event (its own timestamp, else the host clock)."""
    match = _DAY_RE.match(event.occurred_at or "")
    if match:
        return match.group(1)
    return str(clock.now_rfc3339())[:10]


def _value_key(name: str, day: str) -> str:
    """The `kv` key one counter's value for one UTC day lives under."""
    return f"{VALUE_KEY_PREFIX}{name}.{day}"


async def _load_registry() -> list[str]:
    """Return the community's live counter names, `[]` if none exist yet.

    Raises:
        _KvFailure: kv failed or the stored registry is corrupt (never silently reset).
    """
    raw = await _kv_get(REGISTRY_KEY)
    if raw is None:
        return []
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _KvFailure("corrupt dailycounter registry") from exc
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        raise _KvFailure("corrupt dailycounter registry: expected a JSON array of strings")
    return data


async def _save_registry(names: list[str]) -> None:
    """Persist the counter names as a sorted, deduplicated JSON array."""
    await _kv_set(REGISTRY_KEY, json.dumps(sorted(set(names))).encode("utf-8"))


async def _get_value(name: str, day: str) -> int:
    """Return one counter's value for `day`, `0` if never written.

    Raises:
        _KvFailure: kv failed or the stored value isn't an integer.
    """
    raw = await _kv_get(_value_key(name, day))
    if raw is None:
        return 0
    try:
        return int(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _KvFailure("corrupt dailycounter value") from exc


def _is_privileged(event: PlatformEvent) -> bool:
    """Broadcaster/moderator check -- fails CLOSED when role info isn't on the event."""
    is_mod = event.payload.get("is_mod")
    is_broadcaster = event.payload.get("is_broadcaster")
    if not isinstance(is_mod, bool) and not isinstance(is_broadcaster, bool):
        log.debug("dailycounter.role_info_unavailable", platform=event.platform)
        return False
    return bool(is_mod) or bool(is_broadcaster)


def _normalize_name(raw: str) -> str:
    """Strip an optional leading `!` and lowercase."""
    return raw.strip().lstrip("!").strip().lower()


def _validate_name(name: str) -> str | None:
    """Return an error message, or `None` if `name` is a safe counter name."""
    if not name:
        return "a counter name is required, e.g. `!dailycounter add deaths`"
    if len(name) > MAX_NAME_LEN:
        return f"counter names must be {MAX_NAME_LEN} characters or fewer"
    if any(ch not in _NAME_ALLOWED_CHARS for ch in name):
        return "counter names may only contain lowercase letters, digits, '_' and '-'"
    if name in _RESERVED_NAMES:
        return f"'{name}' is reserved and can't be used as a counter name"
    return None


def _resolve_amount(raw: str | None, *, default: int | None) -> tuple[int | None, str | None]:
    """Parse an optional numeric argument into an s64-safe int: `(value, error)`."""
    if raw is None or raw.strip() == "":
        if default is None:
            return None, "a number is required, e.g. `set 3`"
        return default, None
    try:
        value = int(raw.strip())
    except ValueError:
        return None, "that isn't a whole number"
    if not (_S64_MIN <= value <= _S64_MAX):
        return None, "that number is out of range"
    return value, None


async def _handle_management(args: list[str], event: PlatformEvent) -> str:
    """Handle `!dailycounter <add|remove|list>`; bare lists. Always replies."""
    sub = args[0].lower() if args else "list"
    rest = args[1:]

    if sub == "list":
        names = await _load_registry()
        if not names:
            return "No daily counters have been created yet."
        return "Daily counters: " + ", ".join(sorted(names))

    if sub == "add":
        if not _is_privileged(event):
            log.info("dailycounter.permission_denied", op="add")
            return "Only the broadcaster or a moderator can create daily counters."
        if not rest:
            return "Usage: !dailycounter add <name>"
        name = _normalize_name(rest[0])
        error = _validate_name(name)
        if error:
            return error
        names = await _load_registry()
        if name in names:
            return f"Counter '{name}' already exists."
        if len(names) >= MAX_COUNTERS:
            return f"This community already has the maximum of {MAX_COUNTERS} daily counters."
        names.append(name)
        await _save_registry(names)
        log.info("dailycounter.created", op="add", count=len(names))
        return f"Created daily counter '{name}' (resets each UTC day). Use !{name} to read it."

    if sub == "remove":
        if not _is_privileged(event):
            log.info("dailycounter.permission_denied", op="remove")
            return "Only the broadcaster or a moderator can remove daily counters."
        if not rest:
            return "Usage: !dailycounter remove <name>"
        name = _normalize_name(rest[0])
        names = await _load_registry()
        if name not in names:
            return f"Counter '{name}' doesn't exist."
        names.remove(name)
        await _save_registry(names)
        await _kv_delete(_value_key(name, _today(event)))
        log.info("dailycounter.removed", op="remove", count=len(names))
        return f"Removed daily counter '{name}'."

    return f"Unknown !dailycounter subcommand '{sub}'. {_USAGE}"


async def _handle_counter(name: str, args: list[str], event: PlatformEvent) -> str | None:
    """Handle `!<name> ...`. Returns `None` when `name` isn't a registered counter."""
    names = await _load_registry()
    if name not in names:
        return None

    day = _today(event)
    key = _value_key(name, day)
    if not args:
        return f"{name} today: {await _get_value(name, day)}"

    op = args[0].lower()
    if op not in ("add", "sub", "set", "reset"):
        return (
            f"Unknown operation '{op}' for '{name}'. "
            f"Usage: !{name} | !{name} add [N] | !{name} sub [N] | !{name} set <N> | !{name} reset"
        )
    if not _is_privileged(event):
        log.info("dailycounter.permission_denied", op=op)
        return "Only the broadcaster or a moderator can change this counter."

    if op == "reset":
        await _kv_set(key, b"0", TTL_SECONDS)
        return f"{name} today: 0"

    amount, error = _resolve_amount(
        args[1] if len(args) > 1 else None, default=None if op == "set" else 1
    )
    if error is not None:
        return error
    amount = cast(int, amount)  # non-None: error was None

    if op == "set":
        await _kv_set(key, str(amount).encode("utf-8"), TTL_SECONDS)
        return f"{name} today: {amount}"
    delta = amount if op == "add" else -amount
    return f"{name} today: {await _kv_increment(key, delta)}"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!dailycounter` and registered counters.

    Returns `None` for non-chat payloads, text without a leading `!` (zero kv cost), a
    disabled flag, and `!tokens` that aren't registered counters. `kv` failures are logged
    and answered with an explicit error reply -- never a silent default.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped.startswith("!"):
        return None
    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    parts = stripped.split()
    token = parts[0].lower()
    args = parts[1:]

    reply: str | None
    try:
        if token == MANAGEMENT_COMMAND:
            reply = await _handle_management(args, event)
        else:
            reply = await _handle_counter(token[1:], args, event)
    except _KvFailure as exc:
        cause = exc.__cause__
        log.error(
            "dailycounter.kv_failure",
            error_type=type(cause).__name__ if cause else type(exc).__name__,
        )
        reply = "Daily counter storage is unavailable right now - please try again."

    if reply is None:
        return None

    log.info(
        "dailycounter.transform matched",
        op="management" if token == MANAGEMENT_COMMAND else "counter",
    )
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
        raise ValueError("dailycounter reply requires a channel_id from the inbound chat.message")
    if not isinstance(text, str) or not text:
        raise ValueError("dailycounter reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("dailycounter.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
