"""`!alias` -> per-community command aliases (`!alias set <name> <target-command>`).

Moderators map a short alias onto an existing command: `!alias set hi !hello` makes
`!hi there` resolve to `!hello there`. `!alias remove <name>` deletes one;
`!alias list` (or bare `!alias`) lists every alias in the community. v2 parity +
SuperPenguin #496 feature request. The logic here is original (nothing ported from
PenguinTwitchBot), so no upstream attribution notice applies.

State: ONE `kv` key per community, ``"alias.registry"`` (a JSON object `{alias: target}`).
The host scopes every key server-side by `(tenant, community, app_id)`, so two
communities never see each other's aliases. Keys use `.` never `:` (gh-631).

**What "resolve" means today (documented limitation).** The `waddle:bundle/stage@1.0.0`
world has no capability to re-inject an event into the pipeline or call another bundle,
so this bundle cannot silently execute the target. On `!<alias> args` it emits the
RESOLVED command text (`!<target> args`) as its reply, relayed to the channel by
`dispatch`. Real in-pipeline re-dispatch needs a host capability that does not exist yet.

Safety: a target must be a single `!command` token (plus optional fixed args), may NOT be
`!alias` or another alias (no chains, so no loops), may not equal its own alias name, and
may not contain control characters. Target existence is NOT verified -- there is no
command registry visible to a bundle. At most `MAX_ALIASES` aliases per community.

Permission model: `set`/`remove` are broadcaster/moderator only and FAIL CLOSED -- when
neither `is_mod` nor `is_broadcaster` is a real `bool` on the event (every Discord message
today) the mutation is denied. `list`/bare/invocation are open to anyone.

Logging is PII-free: only op names, counts and exception type names -- never alias
names, targets or the caller's free text.

kv cost: a message without a leading `!` costs zero kv ops; every other `!token` costs one
`kv.get` of the registry. Failures are never swallowed: a `kv` failure is logged
(`alias.kv_failure`, exception type only) AND answered with an explicit error reply.

Gated behind the PostHog flag ``waddles.command-alias``.
"""

from __future__ import annotations

import json
from typing import Any, cast

from waddle_sdk import kv, log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-alias"
MANAGEMENT_COMMAND = "!alias"
REGISTRY_KEY = "alias.registry"

MAX_ALIASES = 100
MAX_NAME_LEN = 32
MAX_TARGET_LEN = 200
MAX_RESOLVED_LEN = 500
_NAME_ALLOWED_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-")
_RESERVED_NAMES = frozenset({"alias"})

_USAGE = "Usage: !alias set <name> <target-command> | !alias remove <name> | !alias list"


class _KvFailure(Exception):
    """Internal-only: a `kv` host call failed. Always caught in `transform`, never leaked."""


async def _kv_get(key: str) -> bytes | None:
    """`kv.get`, reclassifying the generated WIT `Err` into `_KvFailure` (fail-loud)."""
    try:
        result = await kv.get(key)
    except Exception as exc:  # noqa: BLE001 - classified like waddle_sdk.db/http
        raise _KvFailure("kv.get failed") from exc
    return cast("bytes | None", result)


async def _kv_set(key: str, value: bytes) -> None:
    """`kv.set` (no TTL -- aliases persist), reclassifying `Err` into `_KvFailure`."""
    try:
        await kv.set(key, value, ttl_seconds=0)
    except Exception as exc:  # noqa: BLE001
        raise _KvFailure("kv.set failed") from exc


async def _load_registry() -> dict[str, str]:
    """Return the community's `{alias: target}` map, `{}` if none exist yet.

    Raises:
        _KvFailure: The `kv` call failed or the stored value is corrupt -- never silently
            reset to `{}` (that would drop every alias on the next write).
    """
    raw = await _kv_get(REGISTRY_KEY)
    if raw is None:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _KvFailure("corrupt alias registry") from exc
    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in data.items()
    ):
        raise _KvFailure("corrupt alias registry: expected a JSON object of strings")
    return cast("dict[str, str]", data)


async def _save_registry(registry: dict[str, str]) -> None:
    """Persist the alias map as sorted JSON."""
    await _kv_set(REGISTRY_KEY, json.dumps(registry, sort_keys=True).encode("utf-8"))


def _is_privileged(event: PlatformEvent) -> bool:
    """Broadcaster/moderator check -- fails CLOSED when role info isn't on the event."""
    is_mod = event.payload.get("is_mod")
    is_broadcaster = event.payload.get("is_broadcaster")
    if not isinstance(is_mod, bool) and not isinstance(is_broadcaster, bool):
        log.debug("alias.role_info_unavailable", platform=event.platform)
        return False
    return is_mod is True or is_broadcaster is True


def _normalize_name(raw: str) -> str:
    """Strip an optional leading `!` and lowercase -- `!hi` and `hi` both yield `hi`."""
    return raw.strip().lstrip("!").strip().lower()


def _validate_name(name: str) -> str | None:
    """Return an error message, or `None` if `name` is a safe alias name."""
    if not name:
        return "an alias name is required, e.g. `!alias set hi !hello`"
    if len(name) > MAX_NAME_LEN:
        return f"alias names must be {MAX_NAME_LEN} characters or fewer"
    if any(ch not in _NAME_ALLOWED_CHARS for ch in name):
        return "alias names may only contain lowercase letters, digits, '_' and '-'"
    if name in _RESERVED_NAMES:
        return f"'{name}' is reserved and can't be used as an alias name"
    return None


def _validate_target(
    name: str, target_raw: str, registry: dict[str, str]
) -> tuple[str | None, str | None]:
    """Validate and normalize a target. Returns `(target, error)`; exactly one is `None`."""
    target = target_raw.strip()
    if len(target) > MAX_TARGET_LEN:
        return None, f"targets must be {MAX_TARGET_LEN} characters or fewer"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in target):
        return None, "targets may not contain control characters"
    if not target.startswith("!"):
        target = "!" + target
    head = target.split()[0][1:].lower()
    if not head or any(ch not in _NAME_ALLOWED_CHARS for ch in head):
        return None, "the target must start with a command like `!hello`"
    if head == "alias" or head in registry:
        return None, "a target can't be `!alias` or another alias (no alias chains)"
    if head == name:
        return None, "an alias can't point at itself"
    return target, None


async def _handle_management(args: list[str], event: PlatformEvent) -> str:
    """Handle `!alias <set|remove|list>`; bare `!alias` lists. Always replies."""
    sub = args[0].lower() if args else "list"
    rest = args[1:]

    if sub == "list":
        registry = await _load_registry()
        if not registry:
            return "No aliases have been set yet."
        return "Aliases: " + ", ".join(f"!{n} -> {t}" for n, t in sorted(registry.items()))

    if sub == "set":
        if not _is_privileged(event):
            log.info("alias.permission_denied", op="set")
            return "Only the broadcaster or a moderator can set aliases."
        if len(rest) < 2:
            return "Usage: !alias set <name> <target-command>"
        name = _normalize_name(rest[0])
        error = _validate_name(name)
        if error:
            return error
        registry = await _load_registry()
        target, error = _validate_target(name, " ".join(rest[1:]), registry)
        if error is not None:
            return error
        if name in registry:
            verb = "Updated"
        elif len(registry) >= MAX_ALIASES:
            return f"This community already has the maximum of {MAX_ALIASES} aliases."
        else:
            verb = "Created"
        registry[name] = cast("str", target)  # non-None: error was None
        await _save_registry(registry)
        log.info("alias.set", op="set", count=len(registry))
        return f"{verb} alias !{name} -> {registry[name]}"

    if sub == "remove":
        if not _is_privileged(event):
            log.info("alias.permission_denied", op="remove")
            return "Only the broadcaster or a moderator can remove aliases."
        if not rest:
            return "Usage: !alias remove <name>"
        name = _normalize_name(rest[0])
        registry = await _load_registry()
        if name not in registry:
            return f"Alias '{name}' doesn't exist."
        del registry[name]
        await _save_registry(registry)
        log.info("alias.removed", op="remove", count=len(registry))
        return f"Removed alias '{name}'."

    return f"Unknown !alias subcommand '{sub}'. {_USAGE}"


async def _resolve(token: str, args: list[str]) -> str | None:
    """Resolve `!<token> args` through the registry; `None` means "not an alias of ours"."""
    registry = await _load_registry()
    target = registry.get(token[1:])
    if target is None:
        return None
    resolved = " ".join([target, *args]).strip()
    return resolved[:MAX_RESOLVED_LEN]


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!alias ...` and registered aliases.

    Returns `None` for non-chat payloads, text without a leading `!` (zero kv cost),
    a disabled flag, and `!tokens` that aren't aliases. On `kv` failure logs
    `alias.kv_failure` and replies with an explicit error -- never a silent default.
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
            reply = await _resolve(token, args)
    except _KvFailure as exc:
        cause = exc.__cause__
        log.error(
            "alias.kv_failure",
            error_type=type(cause).__name__ if cause else type(exc).__name__,
        )
        reply = "Alias storage is unavailable right now - please try again."

    if reply is None:
        return None

    log.info(
        "alias.transform matched", op="management" if token == MANAGEMENT_COMMAND else "resolve"
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
        raise ValueError("alias reply requires a channel_id from the inbound chat.message")
    if not isinstance(text, str) or not text:
        raise ValueError("alias reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("alias.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
