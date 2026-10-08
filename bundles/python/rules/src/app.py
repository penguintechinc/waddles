"""`!rules` -> show/manage a per-community rules text, kv-only (light).

v1 scope (KV-ONLY, via `waddle_sdk.community_kv` -- same scope decision as
`bundles/python/joke`'s own docstring, deliberately not depending on the
in-flight #623 `db` API):

- Bare `!rules` -- replies with the community's saved rules text, or a
  "no rules set" message if none has been set yet. Open to anyone -- a
  pure read.
- `!rules set <text>` -- broadcaster/moderator-only (same fail-closed
  `_caller_role_signal()` pattern as `joke`/`count`/`fish`: absent badge
  fields, e.g. Discord's normalizer today, deny, never implicit allow).
  Replaces the community's rules text in `kv`.
- `!rules clear` -- broadcaster/moderator-only. Deletes the community's
  rules text, reverting bare `!rules` to the "no rules set" message.

Uses the shared command grammar parser (`waddle_sdk.command.parse_command`/
`CommandSpec`). **One deliberate grammar extension**: `clear` is not a
member of the shared `VERBS` vocabulary (`waddle_sdk.command.VERBS` has
`remove`/`delete`, not `clear`). `_resolve()` below normalizes a leading
`clear` token onto the grammar's own `remove` verb *before* `parse_command`
ever sees it -- `!rules remove` is therefore a silent, equivalent alias for
`!rules clear` (both route to the same `"clear"` action), the same kind of
pre-`parse_command` text normalization `bundles/python/shoutout` uses for
its own bare-positional-target extension, just for a verb spelling instead
of a positional argument. `!rules clear`/`!rules remove` take no further
arguments -- a trailing word (`!rules clear now`) is a usage error, never a
best-guess partial match.

Scope decision -- PER-COMMUNITY, via `waddle_sdk.community_kv`: the rules
text is channel-wide state, so its `kv` sub-key (`rules.text`) is a plain
literal string with no per-actor hashing. Unlike `joke`/`wheel`'s own
dispatch, this bundle does **not** reject a `None` community:
`community_kv`'s own module docstring documents the host's tenant-wide
sentinel (`community_id=None`, scoped under the literal `"0"` segment) as
a valid community, not an opt-out -- today it is alpha's only activation
shape (one static scope per `svc-ingest-rust` pod), so refusing it here
would make this bundle permanently nonfunctional in that environment.
`envelope.community` is passed straight through to `community_kv`
unchanged.

**kv key charset (gh-631).** The sub-key above uses `.` only, never `:` --
the real `kv` host capability (`core/bundle_host_kv/src/scope.rs::
is_allowed_key_byte`) rejects any guest-supplied key containing a byte
outside ASCII alnum + `_`/`-`/`.`, reserving `:` as its own server-side
namespace separator. See `waddle_sdk.kv`'s own module docstring and
`count`'s 1.0.4 fix (`fix/bump-count-lurk-1.0.4`) for the production
incident this charset rule exists to prevent; `tests/test_app.py::
test_kv_key_constant_is_colon_free` pins it here too.

Gated behind the PostHog flag ``waddles.command-rules`` -- see
`bundles/python/eightball/src/app.py`'s own docstring for the flag-gate
rationale and ordering (cheap command-match first, flag check second, real
grammar parse last).
"""

from __future__ import annotations

from typing import Any, NoReturn, cast

from waddle_sdk import community_kv, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, ParsedCommand, parse_command
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-rules"

#: No sub-modules declared -- `set`/`remove` are already members of the shared grammar's
#: `VERBS` vocabulary (`waddle_sdk.command.VERBS`); `clear` is normalized onto `remove` by
#: `_resolve()` ahead of `parse_command` (see module docstring).
SPEC = CommandSpec(name="rules")

#: Durable per-community state -- never expires (`ttl_seconds=0`).
_RULES_KEY = "rules.text"

#: Bounds for `!rules set <text>` -- keeps a mod from storing something pathological (empty,
#: or a wall of text no chat client renders sanely).
MIN_RULES_LEN = 1
MAX_RULES_LEN = 1000

_NO_RULES_MSG = "No rules have been set for this community yet."
_USAGE = "Usage: !rules | !rules set <text> | !rules clear (set/clear: mod/broadcaster only)"
_PERMISSION_DENIED_MSG = "only moderators/broadcasters can manage the rules"

_KNOWN_COMMANDS = frozenset({"show", "set", "clear", "usage"})


class _KvFailure(Exception):
    """Internal-only: a `kv` host-call failed, or stored state was corrupt. Always caught.

    Mirrors `waddle_sdk.db`'s own documented pattern ("Err is structurally
    classified, never imported") since `waddle_sdk.community_kv` is a thin
    wrapper that does not classify or catch the generated WIT `Err` itself.
    """


async def _kv_get(community: str | None, key: str) -> bytes | None:
    """`community_kv.get`, reclassifying any backend error into `_KvFailure`."""
    try:
        result = await community_kv.get(community, key)
    except Exception as exc:  # noqa: BLE001 - classified like joke/count/fish's own wrappers
        raise _KvFailure(f"kv.get({key!r}) failed: {getattr(exc, 'value', exc)}") from exc
    return cast("bytes | None", result)


async def _kv_set(community: str | None, key: str, value: bytes) -> None:
    """`community_kv.set` (no TTL -- rules text persists indefinitely), reclassifying `Err`."""
    try:
        await community_kv.set(community, key, value, ttl_seconds=0)
    except Exception as exc:  # noqa: BLE001
        raise _KvFailure(f"kv.set({key!r}) failed: {getattr(exc, 'value', exc)}") from exc


async def _kv_delete(community: str | None, key: str) -> None:
    """`community_kv.delete`, reclassifying `Err` into `_KvFailure`."""
    try:
        await community_kv.delete(community, key)
    except Exception as exc:  # noqa: BLE001
        raise _KvFailure(f"kv.delete({key!r}) failed: {getattr(exc, 'value', exc)}") from exc


def _caller_role_signal(payload: dict[str, Any]) -> bool | None:
    """`True`/`False` from the normalized event's own badge fields, or `None` if absent.

    See `joke`/`count`/`fish`'s own identical helper -- `None` (neither
    `is_mod`/`is_broadcaster` present, e.g. Discord's normalizer today) must
    be treated as denied, never as an implicit allow.
    """
    if "is_mod" not in payload and "is_broadcaster" not in payload:
        return None
    return bool(payload.get("is_mod")) or bool(payload.get("is_broadcaster"))


def _resolve(rest: str) -> tuple[str, str | None]:
    """Map the text after `!rules ` onto this bundle's own command set.

    `rest` is already stripped and may be empty. See module docstring for
    the `clear`-onto-`remove` grammar normalization this implements ahead
    of `parse_command`.
    """
    if not rest:
        return "show", None

    tok1, _, tail = rest.partition(" ")
    tail = tail.strip()
    normalized_tok = "remove" if tok1.lower() == "clear" else tok1
    normalized_text = f"!{SPEC.name} {normalized_tok}" + (f" {tail}" if tail else "")

    try:
        parsed: ParsedCommand = parse_command(normalized_text, SPEC)
    except CommandUsageError as exc:
        log.debug("rules.invalid_grammar", rest=rest, error=str(exc))
        return "usage", None

    if parsed.sub_module is not None:
        return "usage", None
    if parsed.option == "set":
        return "set", parsed.args
    if parsed.option == "remove":
        if parsed.args is not None:
            return "usage", None
        return "clear", None
    return "usage", None


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!rules` and its grammar.

    Cheap-skip first (no leading `!rules` token -- `None`, zero cost), flag
    check second, real grammar resolution last -- same ordering as
    `eightball`/`joke`'s own documented rationale. A recognized-but-
    malformed `!rules ...` still produces a reply (`"usage"`) since the
    caller did invoke this command -- never silently dropped.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None

    stripped = text.strip()
    head, _, rest = stripped.partition(" ")
    if head.lower() != f"!{SPEC.name}":
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    command, arg = _resolve(rest.strip())

    log.info("rules.transform matched", command=command)
    payload: dict[str, Any] = {"command": command, "channel_id": event.payload.get("channel_id")}
    if command == "set" and arg is not None:
        payload["arg"] = arg
    # Forward the normalized badge signal, if present -- see `joke`/`count`'s own identical
    # forwarding comment for why absence must reach `dispatch` as absence, not `False`.
    if "is_mod" in event.payload:
        payload["is_mod"] = bool(event.payload["is_mod"])
    if "is_broadcaster" in event.payload:
        payload["is_broadcaster"] = bool(event.payload["is_broadcaster"])

    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload=payload,
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


async def _fail(reason: str, *, op: str, provider: str, channel_id: str) -> NoReturn:
    """Fail-loud kv error path: log, reply an error to chat, then raise -- see `joke`'s own."""
    log.error("rules.kv_error", op=op, error=reason)
    await relay.push(
        provider,
        {"channel": channel_id, "text": "rules are temporarily unavailable, try again shortly."},
    )
    raise RuntimeError(f"rules {op} failed: {reason}")


async def _handle_show(*, community: str | None, provider: str, channel_id: str) -> str:
    """Handle bare `!rules` -- open to anyone, never mutates state."""
    try:
        raw = await _kv_get(community, _RULES_KEY)
    except _KvFailure as exc:
        await _fail(str(exc), op="show", provider=provider, channel_id=channel_id)

    if raw is None:
        return _NO_RULES_MSG
    return raw.decode("utf-8")


async def _handle_set(
    arg: str | None, *, community: str | None, provider: str, channel_id: str
) -> str:
    """Handle `!rules set <text>` -- caller-permission already checked by `dispatch`."""
    if arg is None or not arg.strip():
        return "Usage: !rules set <text>"
    text = arg.strip()
    if len(text) > MAX_RULES_LEN:
        return f"rules text must be {MAX_RULES_LEN} characters or fewer"

    try:
        await _kv_set(community, _RULES_KEY, text.encode("utf-8"))
    except _KvFailure as exc:
        await _fail(str(exc), op="set", provider=provider, channel_id=channel_id)

    log.info("rules.set", community=str(community))
    return "Rules have been updated."


async def _handle_clear(*, community: str | None, provider: str, channel_id: str) -> str:
    """Handle `!rules clear` (alias `!rules remove`) -- caller-permission already checked."""
    try:
        await _kv_delete(community, _RULES_KEY)
    except _KvFailure as exc:
        await _fail(str(exc), op="clear", provider=provider, channel_id=channel_id)

    log.info("rules.cleared", community=str(community))
    return "Rules have been cleared."


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: all kv state reads/writes, then relay the reply.

    Raises:
        ValueError: The envelope's payload has no `channel_id`, or an
            unrecognized `command` (defensive -- `transform` only ever
            emits a member of `_KNOWN_COMMANDS`). Unlike `joke`/`wheel`,
            a missing `envelope.community` is NOT an error here -- `None`
            is passed straight through to `community_kv`, which treats it
            as the host's own valid tenant-wide sentinel (module docstring).
        RuntimeError: A `kv` backend call failed (see `_fail` -- a chat
            error reply and an ERROR log line are always emitted first).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("rules reply requires a channel_id from the inbound chat.message")
    command = payload.get("command")
    if command not in _KNOWN_COMMANDS:
        raise ValueError(f"unrecognized rules command: {command!r}")

    provider = envelope.event.platform
    community = envelope.community

    if command == "usage":
        await relay.push(provider, {"channel": channel_id, "text": _USAGE})
        return DispatchResult(transport=provider, detail="usage")

    if command in ("set", "clear"):
        role_signal = _caller_role_signal(payload)
        if role_signal is not True:
            log.info("rules.permission_denied", command=command, role_signal=str(role_signal))
            await relay.push(provider, {"channel": channel_id, "text": _PERMISSION_DENIED_MSG})
            return DispatchResult(transport=provider, detail=f"{command}:denied")
        if command == "set":
            arg = payload.get("arg")
            arg = arg if isinstance(arg, str) else None
            reply_text = await _handle_set(
                arg, community=community, provider=provider, channel_id=channel_id
            )
        else:
            reply_text = await _handle_clear(
                community=community, provider=provider, channel_id=channel_id
            )
        await relay.push(provider, {"channel": channel_id, "text": reply_text})
        log.info("rules.dispatch applied", command=command)
        return DispatchResult(transport=provider, detail=command)

    reply_text = await _handle_show(community=community, provider=provider, channel_id=channel_id)
    await relay.push(provider, {"channel": channel_id, "text": reply_text})
    log.info("rules.dispatch relayed", platform=provider, command=command)
    return DispatchResult(transport=provider, detail=command)
