"""`!wheel` -> a community-scoped, kv-backed spin-the-wheel random picker.

Standard grammar (`sdk/waddle-sdk/src/waddle_sdk/command.py`'s verb vocabulary,
hand-parsed here the same way `count`/`command` do rather than through
`parse_command()` -- `wheel` has no sub-modules and only needs the flat
`!wheel <verb> [arg]` shape, so the shared parser's sub-module machinery would
be unused weight): `!wheel add <option>` / `!wheel remove <option>` manage the
per-community option list (broadcaster/moderator only); `!wheel list` shows
the current options (open to anyone); `!wheel reset` clears the list
(broadcaster/moderator only -- destructive, gated the same as `add`/`remove`
even though the task spec's own grammar line doesn't repeat the qualifier for
it, matching every sibling bundle's "mutation = privileged, read = open"
convention: `count`'s `add`/`remove` vs `list`, `command`'s `set`/`remove`/
`timer` vs `list`); bare `!wheel` or `!wheel spin` spins the wheel, replying
with a uniformly random option (stdlib `random.choice`, rejecting with an
"empty wheel" message rather than crashing when no options exist yet).

State -- PER-COMMUNITY, via `waddle_sdk.community_kv` (explicit
`community_id`, unlike `count`'s plain `kv` + implicit host-side scoping):
one JSON-array-of-strings key, `wheel.options` (`.` separator, never `:` --
gh-631, see `waddle_sdk.kv.validate_key` / `community_kv._scoped_key`).
`community_id` comes from `waddle_sdk.flask_core.get_bundle_context()`,
available inside `transform()` because `_component_entry.py` binds
`bundle_context()` around every `process-stage.transform` export call (see
that module's own `_transform_impl`) -- the same mechanism `bundles/python/
command`'s dynamic `!<name>` lookup relies on. A `None` community (tenant-
wide invocation) is rejected with an explicit "requires a community context"
reply, never silently falling back to a shared/global bucket.

Business-logic split -- mirrors `count`'s convention (NOT `command`'s
read-only-transform/write-in-dispatch split): ALL `kv` work (reads AND
mutations) happens in `transform()`, because every wheel operation --
including the random pick itself -- needs its *result* in the reply text
`transform()` builds, and there is no dynamic-token registry lookup here
that would otherwise force a transform/dispatch split (`wheel`'s own command
word is static, declared in `bundle.yaml`'s `command_prefix` filter, unlike
`count`'s per-community counter names). `dispatch()` is therefore a pure
relay of the text `transform()` already produced, exactly like `count`/
`roll`/`eightball`'s own `dispatch()`.

Permission model -- `_is_privileged()` reads `event.payload["is_mod"]`/
`["is_broadcaster"]` exactly like `count`'s own helper: fails CLOSED (denies)
when neither key is present as an actual `bool` on the inbound event (today,
every Discord message -- `core/svc_ingest/src/normalize.rs::
normalize_discord` does not populate either field), logging
`wheel.role_info_unavailable` rather than guessing. A known, documented
limitation inherited from the ingest layer, not a bug in this bundle.

Token-safe (ahead of the PII-tokenization pipeline, #427/#429):
`event.actor` may currently be a raw username rather than an opaque token --
this bundle never stores or logs `event.actor` anywhere (no per-caller state
at all, unlike `lurk`'s hashed-actor keys), so there is nothing to tokenize
here in the first place.

Gated behind the PostHog flag ``waddles.command-wheel`` -- see
`bundles/python/eightball/src/app.py`'s own docstring for the flag-gate
rationale and ordering (command-text match first, flag check second, so an
unrelated event never pays for a host round trip it doesn't need).
"""

from __future__ import annotations

import json
import random
from typing import Any, cast

from waddle_sdk import community_kv, log, relay
from waddle_sdk.flask_core import get_bundle_context
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

#: PostHog flag key, `{product}.{feature-name}` convention (`critical-rules.md`).
FLAG_KEY = "waddles.command-wheel"

#: The command word this bundle answers to -- matched case-insensitively,
#: and only as a whole token (`!wheel`/`!wheel ...`), never as a prefix of a
#: longer word like `!wheelbarrow`.
COMMAND_WORD = "!wheel"

#: Single per-community `kv` key: a JSON array of option strings. `.`
#: separator, never `:` (gh-631) -- see module docstring.
OPTIONS_KEY = "wheel.options"

#: Bounds-checked before any `kv` write, same rationale as `count`'s
#: `MAX_NAME_LEN` -- keeps a single wheel's stored JSON blob small and
#: comfortably inside the host's per-value size limit.
MAX_OPTION_LEN = 200
MAX_OPTIONS = 100

#: The only sub-command tokens that may appear verbatim in a log line (static, code-owned
#: names; the empty string is bare `!wheel`, logged as `spin`). Anything else is user input.
_LOGGED_TOKENS = frozenset({"", "spin", "add", "remove", "list", "reset"})

_USAGE = (
    "Usage: !wheel [spin] | !wheel add <option> | !wheel remove <option> | "
    "!wheel list | !wheel reset"
)
_COMMUNITY_REQUIRED_MSG = (
    "!wheel requires a community context and cannot be used tenant-wide"
)
_PERMISSION_DENIED_MSG = "Only the broadcaster or a moderator can manage the wheel."
_EMPTY_WHEEL_MSG = "The wheel has no options yet -- add some with !wheel add <option>."
_KV_ERROR_MSG = "Something went wrong updating the wheel storage - please try again."


class _KvFailure(Exception):
    """Internal-only: a `community_kv`/`kv` host-call failed. Always caught inside `transform`.

    Mirrors `count`'s own `_KvFailure` pattern: `waddle_sdk.community_kv` is a
    thin wrapper that does not classify or catch the generated WIT `Err`
    itself, so this bundle reclassifies it at its own call sites -- fail
    loud, never silent (`critical-rules.md` Fail-Loud Code Paths).
    """


async def _kv_get(community_id: str, key: str) -> bytes | None:
    """`community_kv.get`, reclassifying the generated WIT `Err` into `_KvFailure`."""
    try:
        result = await community_kv.get(community_id, key)
    except Exception as exc:  # noqa: BLE001 - classified like waddle_sdk.db/count (see module docstring)
        raise _KvFailure(
            f"kv.get({key!r}) failed: {getattr(exc, 'value', exc)}"
        ) from exc
    # `waddle_sdk.community_kv` ships no `py.typed` marker, so mypy sees `Any` here -- cast
    # back to the real contract (`waddle_sdk/community_kv.py`'s own `get()` signature).
    return cast("bytes | None", result)


async def _kv_set(community_id: str, key: str, value: bytes) -> None:
    """`community_kv.set` (no TTL -- wheel options persist indefinitely), reclassifying `Err`."""
    try:
        await community_kv.set(community_id, key, value, ttl_seconds=0)
    except Exception as exc:  # noqa: BLE001
        raise _KvFailure(
            f"kv.set({key!r}) failed: {getattr(exc, 'value', exc)}"
        ) from exc


async def _load_options(community_id: str) -> list[str]:
    """Return the community's current wheel options, or `[]` if none exist yet.

    Raises:
        _KvFailure: The `kv` round trip failed, or the stored value isn't
            valid JSON / isn't a JSON array of strings -- treated as a
            storage failure (fail-loud), never silently reset to `[]`.
    """
    raw = await _kv_get(community_id, OPTIONS_KEY)
    if raw is None:
        return []
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _KvFailure(f"corrupt wheel options: {exc}") from exc
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        raise _KvFailure("corrupt wheel options: expected a JSON array of strings")
    return data


async def _save_options(community_id: str, options: list[str]) -> None:
    """Persist the community's wheel options as a JSON array, insertion order preserved."""
    await _kv_set(community_id, OPTIONS_KEY, json.dumps(options).encode("utf-8"))


def _is_privileged(event: PlatformEvent) -> bool:
    """Broadcaster/moderator check -- fails CLOSED when role info isn't on the event.

    See module docstring for where `core/svc_ingest/src/normalize.rs`
    populates (Twitch) or omits (Discord, today) `is_mod`/`is_broadcaster`.
    """
    is_mod = event.payload.get("is_mod")
    is_broadcaster = event.payload.get("is_broadcaster")
    if not isinstance(is_mod, bool) and not isinstance(is_broadcaster, bool):
        log.debug("wheel.role_info_unavailable", platform=event.platform)
        return False
    return is_mod is True or is_broadcaster is True


async def _handle_add(community_id: str, arg: str, event: PlatformEvent) -> str:
    """Handle `!wheel add <option>` -- broadcaster/moderator only."""
    if not _is_privileged(event):
        log.info("wheel.permission_denied", action="add")
        return _PERMISSION_DENIED_MSG
    option = arg.strip()
    if not option:
        return "Usage: !wheel add <option>"
    if len(option) > MAX_OPTION_LEN:
        return f"options must be {MAX_OPTION_LEN} characters or fewer"
    options = await _load_options(community_id)
    if any(existing.lower() == option.lower() for existing in options):
        return f"'{option}' is already on the wheel."
    if len(options) >= MAX_OPTIONS:
        return (
            f"the wheel already has {MAX_OPTIONS} options (the max) -- remove one first"
        )
    options.append(option)
    await _save_options(community_id, options)
    log.info("wheel.option_added", option_count=len(options))
    plural = "option" if len(options) == 1 else "options"
    return f"Added '{option}' to the wheel ({len(options)} {plural})."


async def _handle_remove(community_id: str, arg: str, event: PlatformEvent) -> str:
    """Handle `!wheel remove <option>` -- broadcaster/moderator only, case-insensitive match."""
    if not _is_privileged(event):
        log.info("wheel.permission_denied", action="remove")
        return _PERMISSION_DENIED_MSG
    option = arg.strip()
    if not option:
        return "Usage: !wheel remove <option>"
    options = await _load_options(community_id)
    match = next(
        (existing for existing in options if existing.lower() == option.lower()), None
    )
    if match is None:
        return f"'{option}' isn't on the wheel."
    options.remove(match)
    await _save_options(community_id, options)
    log.info("wheel.option_removed", option_count=len(options))
    return f"Removed '{match}' from the wheel."


async def _handle_list(community_id: str) -> str:
    """Handle `!wheel list` -- open to anyone, read-only."""
    options = await _load_options(community_id)
    if not options:
        return _EMPTY_WHEEL_MSG
    return "Wheel options: " + ", ".join(options)


async def _handle_reset(community_id: str, event: PlatformEvent) -> str:
    """Handle `!wheel reset` -- broadcaster/moderator only (destructive, see module docstring)."""
    if not _is_privileged(event):
        log.info("wheel.permission_denied", action="reset")
        return _PERMISSION_DENIED_MSG
    await _save_options(community_id, [])
    log.info("wheel.reset")
    return "The wheel has been cleared."


async def _handle_spin(community_id: str) -> str:
    """Handle bare `!wheel`/`!wheel spin` -- open to anyone, rejects an empty wheel."""
    options = await _load_options(community_id)
    if not options:
        return _EMPTY_WHEEL_MSG
    winner = random.choice(options)  # noqa: S311 - a game pick, not a security decision
    log.info("wheel.spin", option_count=len(options))
    return f"\U0001f3a1 The wheel lands on: {winner}!"


async def _route(community_id: str, token: str, arg: str, event: PlatformEvent) -> str:
    """Dispatch one `!wheel` subcommand (or the bare/`spin` default) to its handler."""
    if token in ("", "spin"):
        return await _handle_spin(community_id)
    if token == "add":
        return await _handle_add(community_id, arg, event)
    if token == "remove":
        return await _handle_remove(community_id, arg, event)
    if token == "list":
        return await _handle_list(community_id)
    if token == "reset":
        return await _handle_reset(community_id, event)
    return f"Unknown !wheel subcommand '{token}'. {_USAGE}"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: `!wheel ...` -> a reply, built entirely here.

    Returns `None` for any non-matching payload or while `waddles.command-
    wheel` is disabled -- never raises over an event this bundle wasn't meant
    to react to. A recognized command always produces a reply (usage/error
    text included), per `critical-rules.md` Fail-Loud Code Paths.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    lowered = stripped.lower()
    if lowered != COMMAND_WORD and not lowered.startswith(COMMAND_WORD + " "):
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    rest = stripped[len(COMMAND_WORD) :].strip()
    parts = rest.split(maxsplit=1)
    token = parts[0].lower() if parts else ""
    arg = parts[1] if len(parts) > 1 else ""

    ctx = get_bundle_context()
    reply: str
    if ctx.community is None:
        reply = _COMMUNITY_REQUIRED_MSG
    else:
        try:
            reply = await _route(ctx.community, token, arg, event)
        except _KvFailure as exc:
            log.error("wheel.kv_failure", error=str(exc))
            reply = _KV_ERROR_MSG

    # PII-free: `token` is user-typed (e.g. `!wheel @someone`), so log a static name only --
    # an unrecognized token is reported as the fixed string "unknown", never echoed.
    logged_command = (token or "spin") if token in _LOGGED_TOKENS else "unknown"
    log.info("wheel.transform matched", command=logged_command)
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

    All `kv` work happens in `transform()` (see module docstring for why) --
    this is a pure relay, same minimal shape as `count`'s own `dispatch`.

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
            "wheel reply requires a channel_id from the inbound chat.message"
        )
    if not isinstance(text, str) or not text:
        raise ValueError("wheel reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("wheel.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
