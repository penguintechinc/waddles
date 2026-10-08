"""`!about` (alias `!bot`) -> static bot info plus a per-community custom blurb, kv-only.

v1 scope (KV-ONLY, via `waddle_sdk.community_kv` -- same scope decision as
`bundles/python/joke`'s own docstring, deliberately not depending on the
in-flight #623 `db` API):

- Bare `!about` (or `!bot`) -- replies with this bundle's static bot name
  and version plus the community's own custom blurb, if one has been set
  (`_DEFAULT_BLURB` otherwise). Open to anyone -- a pure read.
- `!about set <text>` -- broadcaster/moderator-only (same fail-closed
  `_caller_role_signal()` pattern as `joke`/`fish`/`count`: absent badge
  fields, e.g. Discord's normalizer today, deny, never implicit allow).
  Sets the community's custom blurb, stored in `kv`.

`!bot` is a pure alias for `!about` -- both tokens route to the exact same
grammar (`_resolve_command`'s input is re-normalized onto `!about` text
before `parse_command` ever sees it, so the shared grammar parser only
needs to know one command name).

Uses the shared command grammar parser (`waddle_sdk.command.parse_command`/
`CommandSpec`, first adopted by `fish` -- #618) instead of hand-rolled
`text.split()`. `!about` declares no sub-modules; `set` is already a
member of the shared `VERBS` vocabulary (`waddle_sdk.command.VERBS`), so no
bundle-specific grammar extension is needed.

Scope decision -- PER-COMMUNITY, via `waddle_sdk.community_kv`: the blurb is
channel-wide state, so its `kv` sub-key (`about.blurb`) is a plain literal
string with no per-actor hashing. Unlike `joke`/`wheel`'s own dispatch, this
bundle does **not** reject a `None` community: `community_kv`'s own module
docstring documents the host's tenant-wide sentinel (`community_id=None`,
scoped under the literal `"0"` segment) as a valid community, not an
opt-out -- today it is alpha's only activation shape (one static scope per
`svc-ingest-rust` pod), so refusing it here would make this bundle
permanently nonfunctional in that environment. `envelope.community` is
passed straight through to `community_kv` unchanged.

**kv key charset (gh-631).** The sub-key above uses `.` only, never `:` --
the real `kv` host capability (`core/bundle_host_kv/src/scope.rs::
is_allowed_key_byte`) rejects any guest-supplied key containing a byte
outside ASCII alnum + `_`/`-`/`.`, reserving `:` as its own server-side
namespace separator. See `waddle_sdk.kv`'s own module docstring and
`count`'s 1.0.4 fix (`fix/bump-count-lurk-1.0.4`) for the production
incident this charset rule exists to prevent; `tests/test_app.py::
test_kv_key_constant_is_colon_free` pins it here too.

Gated behind the PostHog flag ``waddles.command-about`` -- see
`bundles/python/eightball/src/app.py`'s own docstring for the flag-gate
rationale and ordering (cheap command-match first, flag check second, real
grammar parse last).
"""

from __future__ import annotations

from typing import Any, NoReturn, cast

from waddle_sdk import community_kv, log, relay
from waddle_sdk.command import (
    CommandSpec,
    CommandUsageError,
    ParsedCommand,
    parse_command,
)
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-about"

#: No sub-modules declared -- `set` is already a member of the shared grammar's `VERBS`
#: vocabulary (`waddle_sdk.command.VERBS`).
SPEC = CommandSpec(name="about")

#: `!bot` is a pure alias for `!about` -- `transform` re-normalizes either token onto
#: `!{SPEC.name}` text before handing it to `parse_command`, so the grammar only has to know
#: one command name.
_ALIASES: tuple[str, ...] = ("!about", "!bot")

#: Durable per-community state -- never expires (`ttl_seconds=0`).
_BLURB_KEY = "about.blurb"

#: Static bot identity -- not community-configurable, unlike the blurb.
BOT_NAME = "Waddles"
BOT_VERSION = "3.0"

_DEFAULT_BLURB = "A modular, multi-platform community bot."

#: Bounds for `!about set <text>` -- keeps a mod from storing something pathological (empty,
#: or a wall of text no chat client renders sanely). Mirrors `joke`'s own `MAX_JOKE_LEN`.
MIN_BLURB_LEN = 1
MAX_BLURB_LEN = 300

_USAGE = "Usage: !about | !about set <text> (set: mod/broadcaster only)"
_PERMISSION_DENIED_MSG = "only moderators/broadcasters can set the about blurb"

_KNOWN_COMMANDS = frozenset({"show", "set", "usage"})


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
    """`community_kv.set` (no TTL -- the blurb persists indefinitely), reclassifying `Err`."""
    try:
        await community_kv.set(community, key, value, ttl_seconds=0)
    except Exception as exc:  # noqa: BLE001
        raise _KvFailure(f"kv.set({key!r}) failed: {getattr(exc, 'value', exc)}") from exc


def _caller_role_signal(payload: dict[str, Any]) -> bool | None:
    """`True`/`False` from the normalized event's own badge fields, or `None` if absent.

    See `joke`/`fish`/`count`'s own identical helper -- `None` (neither
    `is_mod`/`is_broadcaster` present, e.g. Discord's normalizer today) must
    be treated as denied, never as an implicit allow.
    """
    if "is_mod" not in payload and "is_broadcaster" not in payload:
        return None
    return bool(payload.get("is_mod")) or bool(payload.get("is_broadcaster"))


def _resolve_command(parsed: ParsedCommand | None) -> str:
    """Map a `ParsedCommand` (or `None` on a parse error) onto this bundle's own command set.

    Only `option in (None, "set")` is implemented -- every other
    grammar-legal verb (`add`/`sub`/`enable`/`disable`/`remove`/`delete`/
    `list`/`reset`, none of which this bundle declares sub-modules or
    behavior for) resolves to `"usage"`, same fail-loud-never-silent rule
    as a parse error itself.
    """
    if parsed is None:
        return "usage"
    if parsed.option is None:
        return "show"
    if parsed.option == "set":
        return "set"
    return "usage"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!about`/`!bot` and its grammar.

    Cheap-skip first (no leading `!about`/`!bot` token -- `None`, zero
    cost), flag check second, real grammar parse last -- same ordering as
    `eightball`'s and `joke`'s own documented rationale. A recognized-but-
    malformed `!about ...` (a `CommandUsageError`, or a grammar-legal verb
    this bundle doesn't implement, e.g. `!about enable`) still produces a
    reply (`"usage"`) since the caller did invoke this command -- never
    silently dropped.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None

    stripped = text.strip()
    head = stripped.split(maxsplit=1)[0].lower() if stripped else ""
    if head not in _ALIASES:
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    # Re-normalize whichever alias was typed onto `!{SPEC.name}` text -- `parse_command`'s
    # grammar only ever recognizes the one declared command name.
    normalized = f"!{SPEC.name}" + stripped[len(head) :]
    try:
        parsed: ParsedCommand | None = parse_command(normalized, SPEC)
    except CommandUsageError:
        parsed = None
    command = _resolve_command(parsed)

    log.info("about.transform matched", command=command)
    payload: dict[str, Any] = {"command": command, "channel_id": event.payload.get("channel_id")}
    if command == "set" and parsed is not None:
        payload["arg"] = parsed.args
    # Forward the normalized badge signal, if present -- see `joke`/`fish`/`count`'s own
    # identical forwarding comment for why absence must reach `dispatch` as absence, not `False`.
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
    log.error("about.kv_error", op=op, error=reason)
    await relay.push(
        provider,
        {"channel": channel_id, "text": "about is temporarily unavailable, try again shortly."},
    )
    raise RuntimeError(f"about {op} failed: {reason}")


async def _handle_show(*, community: str | None, provider: str, channel_id: str) -> str:
    """Handle bare `!about`/`!bot` -- open to anyone, never mutates state."""
    try:
        raw = await _kv_get(community, _BLURB_KEY)
    except _KvFailure as exc:
        await _fail(str(exc), op="show", provider=provider, channel_id=channel_id)

    blurb = raw.decode("utf-8") if raw is not None else _DEFAULT_BLURB
    return f"{BOT_NAME} v{BOT_VERSION} -- {blurb}"


async def _handle_set(
    arg: str | None, *, community: str | None, provider: str, channel_id: str
) -> str:
    """Handle `!about set <text>` -- caller-permission already checked by `dispatch`."""
    if arg is None or not arg.strip():
        return "Usage: !about set <text>"
    text = arg.strip()
    if len(text) > MAX_BLURB_LEN:
        return f"about text must be {MAX_BLURB_LEN} characters or fewer"

    try:
        await _kv_set(community, _BLURB_KEY, text.encode("utf-8"))
    except _KvFailure as exc:
        await _fail(str(exc), op="set", provider=provider, channel_id=channel_id)

    log.info("about.blurb_set", community=str(community))
    return "Updated the about blurb."


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
        raise ValueError("about reply requires a channel_id from the inbound chat.message")
    command = payload.get("command")
    if command not in _KNOWN_COMMANDS:
        raise ValueError(f"unrecognized about command: {command!r}")

    provider = envelope.event.platform
    community = envelope.community

    if command == "usage":
        await relay.push(provider, {"channel": channel_id, "text": _USAGE})
        return DispatchResult(transport=provider, detail="usage")

    if command == "set":
        role_signal = _caller_role_signal(payload)
        if role_signal is not True:
            log.info("about.permission_denied", command=command, role_signal=str(role_signal))
            await relay.push(provider, {"channel": channel_id, "text": _PERMISSION_DENIED_MSG})
            return DispatchResult(transport=provider, detail="set:denied")
        arg = payload.get("arg")
        arg = arg if isinstance(arg, str) else None
        reply_text = await _handle_set(
            arg, community=community, provider=provider, channel_id=channel_id
        )
        await relay.push(provider, {"channel": channel_id, "text": reply_text})
        log.info("about.dispatch applied", command=command)
        return DispatchResult(transport=provider, detail=command)

    reply_text = await _handle_show(community=community, provider=provider, channel_id=channel_id)
    await relay.push(provider, {"channel": channel_id, "text": reply_text})
    log.info("about.dispatch relayed", platform=provider, command=command)
    return DispatchResult(transport=provider, detail=command)
