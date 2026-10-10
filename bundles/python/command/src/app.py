"""`!command` -> generic, per-community custom TEXT commands and timers (Twitch-style).

PII note (2026-10-05): the tokenization pipeline (#429) is NOT merged yet, so
`event.actor` may currently be a RAW USERNAME, not an opaque token. Unlike
`count`/`lurk` (which hash `actor` before it ever reaches `kv`), this bundle's
stored state (the command registry and timer configs) never includes `actor`
at all -- only a community-scoped `{name: text}` map and a `{name:
{interval_seconds, enabled}}` map, neither keyed nor valued by caller
identity, so there is nothing to hash. The one place a raw identity DOES
cross this bundle is the `$(username)` placeholder substitution: a stored
command's text is rendered against the CALLER's own `event.actor` at reply
time, transiently, never stored or logged -- the same relay-only exposure
`eightball`'s own docstring accepts for ahead-of-tokenization bundles. Once
#427/#429 land, `event.actor` becomes an opaque token and this substitution
would need a detokenization step (resolving the token back to a display
name) before it's useful as `$(username)` -- tracked as a follow-up, not
implemented here (no detokenization capability exists at this WIT boundary
today).

Declares the `storage.kv` permission (`bundle.yaml`/`hub-manifest.yaml`) --
see `lurk`'s own docstring for why it must be present in both files.

Per-community scope (CRITICAL): every `kv` key below is namespaced
`command:{registry,timers}:{community}` -- `community` is REQUIRED (never a
tenant-wide or `community_id=0` fallback); `!command set/remove/list/timer`
and direct `!<name>` invocation all reply with an explicit
"requires a community context" message rather than silently falling back to
a shared bucket when `community` is `None`.

Deliberate, documented deviation from `count`/`lurk`'s "zero kv access in
`transform()`" convention: `transform()` must read the registry (`kv.get`,
read-only) to decide whether an arbitrary `!<name>` token is one of ITS
commands at all -- there is no other way to distinguish "unknown word,
ignore" from "known custom command, reply" ahead of a lookup. All kv WRITES
(`!command set/remove/timer ...`) still happen only in `dispatch()`, exactly
like `lurk`'s own split. Cost tradeoff, accepted: every bang-prefixed chat
message now pays one `feature_enabled()` check (same as any other command
bundle) and, for tokens that aren't the literal `!command` management word,
one additional `kv.get()` round trip -- unavoidable for a dynamic command
set that can't be declared in the manifest's static `command_prefix` filter.

Timer firing gap (gh-613): `!command timer !<name> set/enable/disable`
persists the timer's interval/enabled state to `kv`
(`command:timers:{community}`) and replies confirming the save, but NO
periodic/scheduled trigger exists anywhere in the `waddle:bundle/stage@1.0.0`
WIT world (`wit/waddle-bundle/stage.wit`) to actually invoke this bundle on
an interval -- every stage export is reactive-only (inbound chat event in,
reply out). Confirmed by grepping `core/svc_ingest`, `core/svc_process`,
`core/bundle_executor`, `core/bundle_capability_gate`, and the WIT source
itself for any `schedule`/`cron`/`tick`/`timer`-firing interface: none
exists for stage bundles today. Every `timer` reply says so explicitly
(loud, not silent) and references gh-613, the tracking issue for the
missing scheduler. The static custom-command CRUD (`set`/`remove`/`list`,
and direct `!<name>` invocation) does not depend on this gap and is fully
functional.

Permission gate platform gap: `set`/`remove`/`timer` require the caller to
be a broadcaster or moderator, read via `event.payload["is_mod"]`/
`["is_broadcaster"]` -- booleans `core/svc_ingest/builtin_handlers/twitch_ingest.py`
and `kick_ingest.py` already normalize onto every chat `PlatformEvent`
(no `data.tables` permission is declared here, so the
`community_members`-role-lookup convention `core/svc_process/builtin_handlers/
social_alias_process.py` uses is not available to this kv-only bundle).
`core/svc_ingest/builtin_handlers/discord_ingest.py` does not yet normalize an
equivalent role signal onto its payload -- `_is_privileged()` fails CLOSED
(denies) whenever both keys are absent, so Discord callers cannot manage
commands yet rather than being granted by a missing-key default. Tracked as
a follow-up, not a new issue (same class of gap `twitch_ingest.py`'s own
module docstring already documents inline for its own ingest-side fields).

Gated behind the PostHog flag ``waddles.command-customcommands`` -- see
`bundles/python/eightball/src/app.py`'s own docstring for the flag-gate
rationale and ordering. Checked once, right after the leading `!` is
confirmed, before any kv access -- same ordering `count`/`eightball`/`lurk`
use, so a flag-disabled bundle never pays a kv round trip either.
"""

from __future__ import annotations

import json
import re
from typing import Any

from waddle_sdk import kv, log, relay
from waddle_sdk.flask_core import get_bundle_context
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-customcommands"

#: The literal word after `!` that routes to command MANAGEMENT, as opposed
#: to a dynamic, registered custom command name.
_COMMAND_TOKEN = "command"  # noqa: S105 - a command keyword, not a credential

#: Lowercased custom-command name: letters, digits, `-`, `_`, 1-32 chars.
#: `command` itself is reserved -- it can never be registered as a custom
#: command name, since that would collide with the management token above.
_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
_RESERVED_NAMES = frozenset({_COMMAND_TOKEN})

#: `30s` / `5m` / `1h` -- digits followed by a single unit letter.
_INTERVAL_RE = re.compile(r"^(\d+)(s|m|h)$")
_INTERVAL_UNITS = {"s": 1, "m": 60, "h": 3600}
_MIN_INTERVAL_SECONDS = 10
_MAX_INTERVAL_SECONDS = 24 * 60 * 60

_LIST_DISPLAY_LIMIT = 15

_USAGE_MSG = (
    "Usage: !command set !<name> <text> | !command remove !<name> | !command list | "
    "!command timer !<name> set <interval> | !command timer !<name> enable|disable"
)
_SET_USAGE_MSG = "Usage: !command set !<name> <message text>"
_REMOVE_USAGE_MSG = "Usage: !command remove !<name>"
_TIMER_USAGE_MSG = (
    "Usage: !command timer !<name> set <interval> | !command timer !<name> enable|disable"
)
_PERMISSION_DENIED_MSG = "only broadcasters/mods can manage custom commands"
_INVALID_NAME_MSG = "command names are letters, numbers, - and _ (max 32), and can't be 'command'"
_INVALID_INTERVAL_MSG = "invalid interval -- use e.g. 30s, 5m, or 1h (10s-24h)"
_NO_COMMANDS_MSG = "no custom commands set -- try !command set !name some text"
_COMMUNITY_REQUIRED_MSG = (
    "custom commands require a community context and cannot be used tenant-wide"
)
_KV_ERROR_MSG = "sorry, command storage is temporarily unavailable -- try again shortly"
#: gh-613 tracks the missing scheduled-trigger mechanism -- see module docstring.
_TIMER_PENDING_SCHEDULER_SUFFIX = (
    " -- saved, but periodic firing is not wired yet (tracked: gh-613)"
)


def _is_privileged(event: PlatformEvent) -> bool:
    """Broadcaster/mod gate for `set`/`remove`/`timer` writes.

    Fails closed -- see module docstring.
    """
    payload = event.payload
    return bool(payload.get("is_mod")) or bool(payload.get("is_broadcaster"))


def _normalize_name(token: str) -> str | None:
    """Strip a leading `!`, lowercase, and validate -- `None` on any invalid/reserved name."""
    name = token.strip().lstrip("!").lower()
    if not name or not _NAME_RE.match(name) or name in _RESERVED_NAMES:
        return None
    return name


def _parse_interval(raw: str) -> int | None:
    """Parse `30s`/`5m`/`1h` into seconds, clamped to the configured min/max.

    Returns `None` on any malformed or out-of-range input.
    """
    match = _INTERVAL_RE.match(raw.strip().lower())
    if not match:
        return None
    value = int(match.group(1))
    if value <= 0:
        return None
    seconds = value * _INTERVAL_UNITS[match.group(2)]
    if not (_MIN_INTERVAL_SECONDS <= seconds <= _MAX_INTERVAL_SECONDS):
        return None
    return seconds


def _render(text: str, *, username: str) -> str:
    """Substitute `$(username)` with the calling viewer's own `actor` -- see module docstring."""
    return text.replace("$(username)", username)


def _registry_key(community: str) -> str:
    """Per-community kv key for the `{name: text}` custom-command registry.

    Uses `.` as the separator, never `:` (gh-631): the real `kv` host
    capability (`core/bundle_host_kv/src/scope.rs::is_allowed_key_byte`)
    reserves `:` as its own namespace separator and rejects any guest key
    containing one -- see `waddle_sdk.kv.validate_key`.
    """
    return f"command.registry.{community}"


def _timers_key(community: str) -> str:
    """Per-community kv key for the `{name: {interval_seconds, enabled}}` timer config map.

    `.` separator, never `:` -- see `_registry_key`'s docstring (gh-631).
    """
    return f"command.timers.{community}"


async def _load_registry(community: str) -> dict[str, str]:
    """Read and JSON-decode the community's command registry, defaulting to empty."""
    raw = await kv.get(_registry_key(community))
    if raw is None:
        return {}
    data = json.loads(raw.decode())
    return data if isinstance(data, dict) else {}


async def _save_registry(community: str, registry: dict[str, str]) -> None:
    """JSON-encode and persist the community's command registry. `ttl_seconds=0` -- no expiry."""
    await kv.set(_registry_key(community), json.dumps(registry).encode(), ttl_seconds=0)


async def _load_timers(community: str) -> dict[str, dict[str, Any]]:
    """Read and JSON-decode the community's timer config map, defaulting to empty."""
    raw = await kv.get(_timers_key(community))
    if raw is None:
        return {}
    data = json.loads(raw.decode())
    return data if isinstance(data, dict) else {}


async def _save_timers(community: str, timers: dict[str, dict[str, Any]]) -> None:
    """JSON-encode and persist the community's timer config map. `ttl_seconds=0` -- no expiry."""
    await kv.set(_timers_key(community), json.dumps(timers).encode(), ttl_seconds=0)


def _handle_set(event: PlatformEvent, args: str) -> dict[str, Any]:
    """Build the `set_command` action payload for `!command set !<name> <text>`. No kv access."""
    if not _is_privileged(event):
        return {"action": "reply", "text": _PERMISSION_DENIED_MSG}
    parts = args.split(maxsplit=1)
    if len(parts) < 2:
        return {"action": "reply", "text": _SET_USAGE_MSG}
    name = _normalize_name(parts[0])
    message_text = parts[1].strip()
    if name is None:
        return {"action": "reply", "text": _INVALID_NAME_MSG}
    if not message_text:
        return {"action": "reply", "text": _SET_USAGE_MSG}
    return {"action": "set_command", "name": name, "text": message_text}


def _handle_remove(event: PlatformEvent, args: str) -> dict[str, Any]:
    """Build the `remove_command` action payload for `!command remove !<name>`. No kv access."""
    if not _is_privileged(event):
        return {"action": "reply", "text": _PERMISSION_DENIED_MSG}
    name = _normalize_name(args)
    if name is None:
        text = _REMOVE_USAGE_MSG if not args.strip() else _INVALID_NAME_MSG
        return {"action": "reply", "text": text}
    return {"action": "remove_command", "name": name}


def _handle_timer(event: PlatformEvent, args: str) -> dict[str, Any]:
    """Build the `timer_{set,enable,disable}` action payload for `!command timer ...`.

    No kv access -- see module docstring.
    """
    if not _is_privileged(event):
        return {"action": "reply", "text": _PERMISSION_DENIED_MSG}
    parts = args.split(maxsplit=2)
    if len(parts) < 2:
        return {"action": "reply", "text": _TIMER_USAGE_MSG}
    name = _normalize_name(parts[0])
    if name is None:
        return {"action": "reply", "text": _INVALID_NAME_MSG}
    sub = parts[1].lower()
    if sub == "set":
        if len(parts) < 3:
            return {"action": "reply", "text": _TIMER_USAGE_MSG}
        interval_seconds = _parse_interval(parts[2])
        if interval_seconds is None:
            return {"action": "reply", "text": _INVALID_INTERVAL_MSG}
        return {"action": "timer_set", "name": name, "interval_seconds": interval_seconds}
    if sub == "enable":
        return {"action": "timer_enable", "name": name}
    if sub == "disable":
        return {"action": "timer_disable", "name": name}
    return {"action": "reply", "text": _TIMER_USAGE_MSG}


def _handle_meta(event: PlatformEvent, rest: str) -> dict[str, Any]:
    """Route `!command ...`'s subcommand (`set`/`remove`/`list`/`timer`, or bare -> usage)."""
    rest = rest.strip()
    if not rest:
        return {"action": "reply", "text": _USAGE_MSG}

    sub_parts = rest.split(maxsplit=1)
    sub = sub_parts[0].lower()
    sub_rest = sub_parts[1].strip() if len(sub_parts) > 1 else ""

    if sub == "list":
        return {"action": "list_commands"}
    if sub == "set":
        return _handle_set(event, sub_rest)
    if sub == "remove":
        return _handle_remove(event, sub_rest)
    if sub == "timer":
        return _handle_timer(event, sub_rest)
    return {"action": "reply", "text": _USAGE_MSG}


async def _handle_lookup(event: PlatformEvent, token: str) -> dict[str, Any] | None:
    """Look `token` up in the community's registry -- the one kv READ `transform()` performs.

    Returns `None` for an invalid name, a missing community context, or a
    registry miss -- all three mean "not ours", not an error. A kv failure
    IS an error -- replies loud rather than silently dropping the event.
    """
    name = _normalize_name(token)
    if name is None:
        return None
    ctx = get_bundle_context()
    if ctx.community is None:
        return None
    try:
        registry = await _load_registry(ctx.community)
    except Exception as exc:  # noqa: BLE001 - kv read must surface, never vanish silently
        log.error("command.transform.kv_error", action="lookup", error=str(exc))
        return {"action": "reply", "text": _KV_ERROR_MSG}
    text = registry.get(name)
    if text is None:
        return None
    return {"action": "reply", "text": _render(text, username=event.actor or "friend")}


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: route `!command ...` and registered `!<name>` calls.

    Returns `None` for any non-bang text, an unrecognized `!<name>` (not
    `command` and not a registered custom command), or while
    `waddles.command-customcommands` is disabled. See module docstring for
    why this is the one bundle whose `transform()` reads `kv` (registry
    lookups only -- all writes stay in `dispatch()`).
    """
    text = event.payload.get("text")
    if not isinstance(text, str) or not text.strip().startswith("!"):
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    stripped = text.strip()
    parts = stripped[1:].split(maxsplit=1)
    if not parts or not parts[0]:
        return None
    token = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""

    reply: dict[str, Any]
    if token == _COMMAND_TOKEN:
        reply = _handle_meta(event, rest)
    else:
        lookup_result = await _handle_lookup(event, token)
        if lookup_result is None:
            return None
        reply = lookup_result

    log.info("command.transform matched", token=token, action=reply["action"])
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={**reply, "channel_id": event.payload.get("channel_id")},
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


async def _dispatch_list(community: str | None) -> str:
    """Format the community's active custom commands, sorted, capped at `_LIST_DISPLAY_LIMIT`."""
    if community is None:
        return _COMMUNITY_REQUIRED_MSG
    try:
        registry = await _load_registry(community)
    except Exception as exc:  # noqa: BLE001 - kv read must reply, never crash the stage
        log.error("command.dispatch.kv_error", action="list", error=str(exc))
        return _KV_ERROR_MSG
    if not registry:
        return _NO_COMMANDS_MSG
    names = sorted(registry)
    shown = names[:_LIST_DISPLAY_LIMIT]
    body = ", ".join(f"!{name}" for name in shown)
    if len(names) > _LIST_DISPLAY_LIMIT:
        body += f", …and {len(names) - _LIST_DISPLAY_LIMIT} more"
    return f"custom commands: {body}"


async def _dispatch_set(community: str | None, name: object, text: str) -> str:
    """Upsert `name` -> `text` in the community's registry."""
    if community is None:
        return _COMMUNITY_REQUIRED_MSG
    if not isinstance(name, str) or not name:
        return _SET_USAGE_MSG
    try:
        registry = await _load_registry(community)
        registry[name] = text
        await _save_registry(community, registry)
    except Exception as exc:  # noqa: BLE001 - kv write must reply, never crash the stage
        log.error("command.dispatch.kv_error", action="set", error=str(exc))
        return _KV_ERROR_MSG
    return f"command saved: !{name}"


async def _dispatch_remove(community: str | None, name: object) -> str:
    """Delete `name` from the community's registry, if present."""
    if community is None:
        return _COMMUNITY_REQUIRED_MSG
    if not isinstance(name, str) or not name:
        return _REMOVE_USAGE_MSG
    try:
        registry = await _load_registry(community)
        if name not in registry:
            return f"no custom command named !{name}"
        del registry[name]
        await _save_registry(community, registry)
    except Exception as exc:  # noqa: BLE001 - kv write must reply, never crash the stage
        log.error("command.dispatch.kv_error", action="remove", error=str(exc))
        return _KV_ERROR_MSG
    return f"command removed: !{name}"


async def _dispatch_timer_set(community: str | None, name: object, interval_seconds: object) -> str:
    """Persist `name`'s timer interval (preserving its existing enabled state). See gh-613."""
    if community is None:
        return _COMMUNITY_REQUIRED_MSG
    if not isinstance(name, str) or not name or not isinstance(interval_seconds, int):
        return _TIMER_USAGE_MSG
    try:
        registry = await _load_registry(community)
        if name not in registry:
            return f"no custom command named !{name} -- set it first with !command set"
        timers = await _load_timers(community)
        existing = timers.get(name, {})
        timers[name] = {
            "interval_seconds": interval_seconds,
            "enabled": bool(existing.get("enabled", False)),
        }
        await _save_timers(community, timers)
    except Exception as exc:  # noqa: BLE001 - kv write must reply, never crash the stage
        log.error("command.dispatch.kv_error", action="timer_set", error=str(exc))
        return _KV_ERROR_MSG
    return f"timer set for !{name} every {interval_seconds}s{_TIMER_PENDING_SCHEDULER_SUFFIX}"


async def _dispatch_timer_toggle(community: str | None, name: object, *, enabled: bool) -> str:
    """Flip `name`'s timer `enabled` flag. Requires a prior `timer ... set`. See gh-613."""
    if community is None:
        return _COMMUNITY_REQUIRED_MSG
    if not isinstance(name, str) or not name:
        return _TIMER_USAGE_MSG
    try:
        timers = await _load_timers(community)
        if name not in timers:
            return (
                f"no timer configured for !{name} -- "
                f"set one first with !command timer !{name} set <interval>"
            )
        timers[name]["enabled"] = enabled
        await _save_timers(community, timers)
    except Exception as exc:  # noqa: BLE001 - kv write must reply, never crash the stage
        log.error("command.dispatch.kv_error", action="timer_toggle", error=str(exc))
        return _KV_ERROR_MSG
    state = "enabled" if enabled else "disabled"
    return f"timer {state} for !{name}{_TIMER_PENDING_SCHEDULER_SUFFIX if enabled else ''}"


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: perform the kv side effect (if any), then relay a reply.

    Raises:
        ValueError: The envelope's payload has no `channel_id`, or an
            unrecognized `action` (defensive -- `transform` only ever emits
            one of the actions handled below).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("command reply requires a channel_id from the inbound chat.message")

    action = payload.get("action")
    community = envelope.community

    if action == "reply":
        reply_text = str(payload.get("text", ""))
    elif action == "list_commands":
        reply_text = await _dispatch_list(community)
    elif action == "set_command":
        name_arg = payload.get("name")
        text_arg = str(payload.get("text", ""))
        reply_text = await _dispatch_set(community, name_arg, text_arg)
    elif action == "remove_command":
        reply_text = await _dispatch_remove(community, payload.get("name"))
    elif action == "timer_set":
        name_arg = payload.get("name")
        interval_arg = payload.get("interval_seconds")
        reply_text = await _dispatch_timer_set(community, name_arg, interval_arg)
    elif action in ("timer_enable", "timer_disable"):
        reply_text = await _dispatch_timer_toggle(
            community, payload.get("name"), enabled=(action == "timer_enable")
        )
    else:
        raise ValueError(f"unrecognized command action: {action!r}")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": reply_text})
    log.info("command.dispatch relayed", platform=provider, action=action)
    return DispatchResult(transport=provider, detail=str(action))
