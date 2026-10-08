"""`!count` -> a dynamic, per-community kv-backed counter system.

Promoted from the original trivial self-counter (1.0.2, a per-(community,
caller) `!count` increment-and-reply) to a full counter management system:
`!count add <name>` creates a new, independently-named counter
(`!count add !die` and `!count add die` both yield `die`); `!count remove
<name>` deletes one; `!count list` lists every counter for the community.
Once created, the counter's own name becomes a live command -- bare `!die`
reads the current value, `!die add [N]`/`!die sub [N]` (N defaults to 1)
and `!die set <N>` mutate it. No platform-level registration is needed for
a newly created name: every `!`-prefixed message this bundle sees is
checked against the community's counter registry in `kv` (see
`_handle_counter_invocation`), so a freshly created `!die` starts routing
here on its very next message.

Scope decision -- PER-COMMUNITY, not per-caller: unlike the 1.0.2 self-
counter (and `lurk`'s toggle), these counters are channel-wide state, so
`kv` keys here are plain literal strings (`"count.registry"`,
`"count.value.{name}"`) with NO manual community/actor hashing -- the WIT
`kv` host capability already scopes every key server-side by
`(tenant, community, app_id)` (`core/bundle_host_kv/src/scope.rs::KvScope`),
so two communities running this same bundle never see each other's
counters even though the guest-side key text is identical. No actor
identity is stored or needed at all, so the PII-hashing dance the 1.0.2
self-counter and `lurk` both do for `_kv_key()` does not apply here --
nothing about these counters is per-caller.

Permission model -- broadcaster/moderator only for every mutation (`add`,
`remove`, `set`, `add`/`sub` on an existing counter); reading (bare
`!<name>`, and `!count list`) is open to anyone. `_is_privileged()` reads
`event.payload["is_mod"]`/`["is_broadcaster"]`, the exact boolean fields
`core/svc_ingest/src/normalize.rs::normalize_twitch_irc` (byte-exact port
of the legacy `twitch_ingest.py::normalize`) already populates from
Twitch's own IRCv3 badge tags. **Fails closed, not open**: if neither key
is present as an actual `bool` on the inbound event, `_is_privileged()`
returns `False` and logs `count.role_info_unavailable` rather than
guessing -- the deliberate, spec-mandated behavior for a platform that
doesn't yet forward role data. Today that is every Discord message:
`core/svc_ingest/src/normalize.rs::normalize_discord` does not populate
`is_mod`/`is_broadcaster` at all (only `text`/`guild_id`/`channel_id`/
`message_id`/`author_id`), so every Discord mutation attempt is rejected
until that normalizer gap is closed upstream -- a known, documented
limitation, not a bug in this bundle.

Business logic split -- deliberately DIFFERENT from every sibling bundle's
"transform recognizes only, dispatch does the kv/relay work" convention
(see `lurk`'s own docstring for that baseline shape): `transform` here does
ALL of the kv work (registry lookup, counter create/remove/read/mutate)
because the registry lookup itself IS the routing decision -- whether a
given `!<name>` token even belongs to this bundle can only be answered by
reading `kv`, and that answer has to be known before `transform` can decide
between returning `None` ("not ours") and a reply. Since `kv` is a `world
stage`-level import (`wit/waddle-bundle/stage.wit`: "Capability: always
granted", no stage restriction, unlike `relay`'s "action-stage bundles
only"), this is available from `transform` same as `dispatch`. `dispatch`
is therefore reduced to a pure relay of the `text` `transform` already
built, mirroring `pyping`'s own minimal action-stage shape.

kv cost per message (noted per task request): every inbound `!`-prefixed
message that isn't `!count` costs exactly one `kv.get("count.registry")`
to decide "is this one of ours" -- unavoidable, since a dynamically created
counter name can't be declared in `bundle.yaml`'s static `command_prefix`
filter (which, separately, `core/svc_process/src/spine.rs::
handle_delivered`'s own doc notes is not actually enforced as a routing
filter yet regardless -- every bundle's `transform` already sees every
`chat.message` event and filters internally, so this is not a new
exposure). A message with no leading `!` costs zero kv ops (cheap-skip
before the registry read). Every mutating op after that costs one or two
more (`add`/`remove` create: get+set+set/delete = 3 total; `set`: get+set =
2; `add`/`sub`: get+increment = 2) -- all well under the host's
`MAX_OPS_PER_INVOKE` (64, `core/bundle_host_kv/src/limits.rs`).

Gated behind the PostHog flag ``waddles.command-count`` -- see
`bundles/python/eightball/src/app.py`'s own docstring for the flag-gate
rationale and ordering.
"""

from __future__ import annotations

import json
from typing import Any, cast

from waddle_sdk import kv, log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-count"

#: `!count` itself is always the management command -- never a counter name
#: (`_validate_counter_name` also rejects "count" outright, so the two can
#: never collide even if someone tries to `!count add count`).
MANAGEMENT_COMMAND = "!count"

#: One registry key per community: a JSON array of every live counter name.
REGISTRY_KEY = "count.registry"
VALUE_KEY_PREFIX = "count.value."

#: Longest counter name accepted -- comfortably inside the host's
#: `MAX_GUEST_KEY_LEN` (256 bytes, `core/bundle_host_kv/src/scope.rs`) once
#: prefixed with `VALUE_KEY_PREFIX`.
MAX_NAME_LEN = 32
_NAME_ALLOWED_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-")
_RESERVED_NAMES = frozenset({"count"})

#: `kv.increment`'s `delta` is a WIT `s64`; `set`/`add`/`sub` amounts are
#: validated against this range before ever crossing the WIT boundary so an
#: out-of-range value becomes a clean error reply, not an undefined host
#: response.
_S64_MIN = -(2**63)
_S64_MAX = 2**63 - 1

_USAGE = "Usage: !count add <name> | !count remove <name> | !count list"


class _KvFailure(Exception):
    """Internal-only: a `kv` host-call failed. Always caught inside `transform`, never leaked.

    Mirrors `waddle_sdk.db`'s own documented pattern ("Err is structurally
    classified, never imported") since `waddle_sdk.kv` is a thin wrapper
    that does not classify or catch the generated `Err` itself.
    """


async def _kv_get(key: str) -> bytes | None:
    """`kv.get`, reclassifying the generated WIT `Err` into `_KvFailure` (fail-loud, never silent)."""
    try:
        result = await kv.get(key)
    except Exception as exc:  # noqa: BLE001 - classified like waddle_sdk.db/http (see module docstring)
        raise _KvFailure(
            f"kv.get({key!r}) failed: {getattr(exc, 'value', exc)}"
        ) from exc
    # `waddle_sdk.kv` ships no `py.typed` marker, so mypy sees `Any` here -- cast back to the
    # real contract (`waddle_sdk/kv.py`'s own `get()` signature) rather than leaking `Any`.
    return cast("bytes | None", result)


async def _kv_set(key: str, value: bytes) -> None:
    """`kv.set` (no TTL -- counters persist indefinitely), reclassifying `Err` into `_KvFailure`."""
    try:
        await kv.set(key, value, ttl_seconds=0)
    except Exception as exc:  # noqa: BLE001
        raise _KvFailure(
            f"kv.set({key!r}) failed: {getattr(exc, 'value', exc)}"
        ) from exc


async def _kv_delete(key: str) -> None:
    """`kv.delete`, reclassifying `Err` into `_KvFailure`."""
    try:
        await kv.delete(key)
    except Exception as exc:  # noqa: BLE001
        raise _KvFailure(
            f"kv.delete({key!r}) failed: {getattr(exc, 'value', exc)}"
        ) from exc


async def _kv_increment(key: str, delta: int) -> int:
    """`kv.increment` (no TTL), reclassifying `Err` into `_KvFailure`."""
    try:
        result = await kv.increment(key, delta, ttl_seconds=0)
    except Exception as exc:  # noqa: BLE001
        raise _KvFailure(
            f"kv.increment({key!r}) failed: {getattr(exc, 'value', exc)}"
        ) from exc
    return cast(int, result)


def _value_key(name: str) -> str:
    """The `kv` key one counter's integer value lives under."""
    return f"{VALUE_KEY_PREFIX}{name}"


async def _load_registry() -> list[str]:
    """Return the community's live counter names, or `[]` if none exist yet.

    Raises:
        _KvFailure: The `kv` round trip failed, or the stored registry
            isn't valid JSON / isn't a JSON array of strings -- treated as
            a storage failure (fail-loud), never silently reset to `[]`.
    """
    raw = await _kv_get(REGISTRY_KEY)
    if raw is None:
        return []
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _KvFailure(f"corrupt count registry: {exc}") from exc
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        raise _KvFailure("corrupt count registry: expected a JSON array of strings")
    return data


async def _save_registry(names: list[str]) -> None:
    """Persist the community's counter name list as a sorted, deduplicated JSON array."""
    await _kv_set(REGISTRY_KEY, json.dumps(sorted(set(names))).encode("utf-8"))


async def _get_counter_value(name: str) -> int:
    """Return one counter's current integer value, or `0` if it was never written."""
    raw = await _kv_get(_value_key(name))
    if raw is None:
        return 0
    try:
        return int(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _KvFailure(f"corrupt value for counter {name!r}: {exc}") from exc


def _is_privileged(event: PlatformEvent) -> bool:
    """Broadcaster/moderator check -- fails CLOSED (rejects) when role info isn't on the event.

    Reads `is_mod`/`is_broadcaster` booleans directly from `event.payload`
    -- see module docstring for where `core/svc_ingest/src/normalize.rs`
    populates (Twitch) or omits (Discord, today) these fields. Deliberately
    does not guess or default to allow: absent role info logs
    `count.role_info_unavailable` and returns `False`.
    """
    is_mod = event.payload.get("is_mod")
    is_broadcaster = event.payload.get("is_broadcaster")
    if not isinstance(is_mod, bool) and not isinstance(is_broadcaster, bool):
        log.debug("count.role_info_unavailable", platform=event.platform)
        return False
    return bool(is_mod) or bool(is_broadcaster)


def _normalize_counter_name(raw: str) -> str:
    """Strip an optional leading `!` and lowercase -- `!die` and `die` both yield `"die"`."""
    return raw.strip().lstrip("!").strip().lower()


def _validate_counter_name(name: str) -> str | None:
    """Return an error message, or `None` if `name` is safe to use as a counter name.

    Charset is deliberately conservative (lowercase alnum, `_`, `-` only)
    -- every one of these characters is also inside the host's own
    guest-key charset allowlist (`core/bundle_host_kv/src/scope.rs::
    is_allowed_key_byte`), so a valid counter name can never itself produce
    an invalid `kv` key once prefixed with `VALUE_KEY_PREFIX`. This only
    holds if `VALUE_KEY_PREFIX` itself stays inside that same allowlist --
    it previously used `:` (a byte the host explicitly forbids as its own
    namespace separator) and broke every mutation in production; see
    `REGISTRY_KEY`/`VALUE_KEY_PREFIX` above and
    `tests/test_app.py::test_kv_key_constants_satisfy_host_guest_key_charset`.
    """
    if not name:
        return "a counter name is required, e.g. `!count add die`"
    if len(name) > MAX_NAME_LEN:
        return f"counter names must be {MAX_NAME_LEN} characters or fewer"
    if any(ch not in _NAME_ALLOWED_CHARS for ch in name):
        return "counter names may only contain lowercase letters, digits, '_' and '-'"
    if name in _RESERVED_NAMES:
        return f"'{name}' is reserved and can't be used as a counter name"
    return None


def _resolve_amount(
    raw: str | None, *, default: int | None
) -> tuple[int | None, str | None]:
    """Parse an optional numeric CLI argument into an s64-safe int.

    Returns `(value, error_message)` -- exactly one is `None`. `raw` being
    `None`/empty uses `default`; passing `default=None` makes the argument
    mandatory (`set`'s own contract -- there's no sensible default to set a
    counter to).
    """
    if raw is None or raw.strip() == "":
        if default is None:
            return None, "a number is required, e.g. `set 3`"
        return default, None
    try:
        value = int(raw.strip())
    except ValueError as exc:
        log.debug("count.invalid_amount", error_type=type(exc).__name__)
        return None, f"'{raw}' isn't a whole number"
    if not (_S64_MIN <= value <= _S64_MAX):
        return None, f"'{raw}' is out of range"
    return value, None


async def _handle_management(args: list[str], event: PlatformEvent) -> str:
    """Handle `!count <add|remove|list>` -- always produces a reply, never `None`.

    `!count` is unconditionally ours once recognized, so even an empty or
    unrecognized subcommand gets a usage/error reply rather than silence.
    """
    if not args:
        return _USAGE

    sub = args[0].lower()
    rest = args[1:]

    if sub == "list":
        names = await _load_registry()
        if not names:
            return "No counters have been created yet."
        return "Counters: " + ", ".join(sorted(names))

    if sub == "add":
        if not _is_privileged(event):
            log.info("count.permission_denied", action="add")
            return "Only the broadcaster or a moderator can create counters."
        if not rest:
            return "Usage: !count add <name>"
        name = _normalize_counter_name(rest[0])
        error = _validate_counter_name(name)
        if error:
            return error
        names = await _load_registry()
        if name in names:
            return f"Counter '{name}' already exists."
        names.append(name)
        await _save_registry(names)
        await _kv_set(_value_key(name), b"0")
        log.info("count.counter_created", counter=name)
        return f"Created counter '{name}' (starting at 0). Use !{name} to read it."

    if sub == "remove":
        if not _is_privileged(event):
            log.info("count.permission_denied", action="remove")
            return "Only the broadcaster or a moderator can remove counters."
        if not rest:
            return "Usage: !count remove <name>"
        name = _normalize_counter_name(rest[0])
        names = await _load_registry()
        if name not in names:
            return f"Counter '{name}' doesn't exist."
        names.remove(name)
        await _save_registry(names)
        await _kv_delete(_value_key(name))
        log.info("count.counter_removed", counter=name)
        return f"Removed counter '{name}'."

    return f"Unknown !count subcommand '{sub}'. {_USAGE}"


async def _handle_counter_invocation(
    name: str, args: list[str], event: PlatformEvent
) -> str | None:
    """Handle `!<name> ...` for a registry-matched token. Returns `None` if `name` isn't registered.

    `None` here means "not ours" -- `transform` returns `None` to the host
    unchanged, exactly as if this bundle never saw the message.
    """
    names = await _load_registry()
    if name not in names:
        return None

    if not args:
        value = await _get_counter_value(name)
        return f"{name}: {value}"

    op = args[0].lower()

    if op in ("add", "sub"):
        if not _is_privileged(event):
            log.info("count.permission_denied", action=op, counter=name)
            return "Only the broadcaster or a moderator can change this counter."
        amount, error = _resolve_amount(args[1] if len(args) > 1 else None, default=1)
        if error is not None:
            return error
        assert (
            amount is not None
        )  # _resolve_amount guarantees exactly one of (amount, error)
        delta = amount if op == "add" else -amount
        new_total = await _kv_increment(_value_key(name), delta)
        return f"{name}: {new_total}"

    if op == "set":
        if not _is_privileged(event):
            log.info("count.permission_denied", action="set", counter=name)
            return "Only the broadcaster or a moderator can change this counter."
        amount, error = _resolve_amount(
            args[1] if len(args) > 1 else None, default=None
        )
        if error is not None:
            return error
        assert (
            amount is not None
        )  # _resolve_amount guarantees exactly one of (amount, error)
        await _kv_set(_value_key(name), str(amount).encode("utf-8"))
        return f"{name}: {amount}"

    return (
        f"Unknown operation '{op}' for counter '{name}'. "
        f"Usage: !{name} | !{name} add [N] | !{name} sub [N] | !{name} set <N>"
    )


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!count ...` and every registered counter.

    Returns `None` while `waddles.command-count` is disabled, for any
    non-chat payload, for text with no leading `!` (cheap-skip, zero `kv`
    cost), and for an unrecognized `!<token>` that the registry doesn't
    know about. On a `kv` failure anywhere in dispatch logic below, logs
    loudly (`count.kv_failure`) and still returns a reply -- never silently
    drops the message, never crashes `transform`.
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
            reply = await _handle_counter_invocation(token[1:], args, event)
    except _KvFailure as exc:
        log.error("count.kv_failure", error=str(exc))
        reply = "Something went wrong updating the counter storage - please try again."

    if reply is None:
        return None

    log.info("count.transform matched", command=token)
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"channel_id": event.payload.get("channel_id"), "text": reply},
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
    """Implement `action-stage.dispatch`: relay the reply text `transform` already built.

    All `kv` work happens in `transform` (see module docstring for why) --
    this is a pure relay, same minimal shape as `pyping`'s own `dispatch`.

    Raises:
        ValueError: The envelope's payload is missing `channel_id` or
            `text` (defensive -- `transform` always sets both when it
            returns a non-`None` event).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    text = payload.get("text")
    if not channel_id:
        raise ValueError(
            "count reply requires a channel_id from the inbound chat.message"
        )
    if not isinstance(text, str) or not text:
        raise ValueError("count reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("count.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
