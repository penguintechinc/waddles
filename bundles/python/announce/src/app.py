"""`!announce` -> saved per-community announcement messages, kv-only, community-scoped.

Migrated from the `bot_process` monolith's "saved announcement messages"
feature (task: "Build the announce (saved announcement messages) Python
WASM bundle"). A moderator/broadcaster saves a short text message under a
short key (`!announce set <key> <message>`); anyone can later recall it with
`!announce <key>`, enumerate every saved key with `!announce list`, and a
moderator/broadcaster can delete one with `!announce remove <key>`.

**Not to be confused with** the pre-existing, DB-backed
`core/svc_process/builtin_handlers/community_announcements_process.py` /
`core/svc_action/builtin_handlers/community_announcements_action.py` built-in stage handlers,
which already parse `!announce publish <announcement_id>` against the
legacy `flask_core`/`svc_process`/`svc_action` stage-runner (not
this repo's newer WASI-component `waddle_sdk` bundle SDK) to broadcast a
web-UI-authored `announcements` DB row. Both consume the same `!announce`
command-text prefix; this bundle only recognizes `set`/`remove`/`list`/a
bare key, and resolves any other verb (including `publish`) to a usage
reply rather than attempting to interpret it -- see `_resolve_command`.
Flagged in this bundle's own PR description as a real command-prefix
overlap for the migration owner to resolve (e.g. retiring the legacy
broadcast command, or renaming one side), not silently papered over here.

First-party Waddles content -- a generic "saved canned message" grammar is
common to many chat bots (Nightbot/StreamElements custom commands,
superpenguintv (Psychoboy)'s own `PenguinTwitchBot`
(https://github.com/Psychoboy/PenguinTwitchBot) Quotes/alerts features
among them); credited for inspiration only, same convention as
`fish`/`music`'s own module docstrings (`sdk/waddle-sdk/AUTHORING.md`:
verbatim reuse needs the license text inline, inspiration-only needs credit
only) -- no original PenguinTwitchBot source code or text is reused, the
grammar, storage shape, and all logic below are written fresh for Waddles.

Uses the shared command grammar parser (`waddle_sdk.command.parse_command`/
`CommandSpec`, merged in #618) for the `set`/`remove`/`list` verb shapes.
`CommandSpec` declares no sub-modules, so a bare `!announce <key>` (not one
of the grammar's own VERBS) is always an "unknown option or sub-module"
`CommandUsageError` from `parse_command` itself -- `_resolve_command` below
catches exactly that one error shape and treats the single remaining token
as a literal announcement key instead of a usage failure, mirroring
`music`'s own documented "a positional argument has no slot in the shared
grammar" extension.

v1 scope (KV-ONLY, no `db` capability):

- Bare `!announce <key>` -- GET: replies with the saved message for `key`,
  or a "no saved announcement" reply if unset. Open to any caller.
- `!announce list` -- GET: replies with every saved key for the community,
  sorted. Open to any caller. `!announce list <anything>` is a usage error,
  not silently ignored.
- `!announce set <key> <message>` -- broadcaster/moderator only (same
  `_caller_role_signal()` fail-closed pattern as `fish`/`music`: absent
  badge fields -- e.g. Discord's normalizer today -- deny, never implicit
  allow). Creates or overwrites `key`'s saved message.
- `!announce remove <key>` -- broadcaster/moderator only: deletes `key`'s
  saved message, if any.

Data scoping: the whole `key -> message` mapping is one JSON object, stored
under one `kv` key (`_REGISTRY_KEY`) via `waddle_sdk.community_kv` -- keyed
by the envelope's own `community_id` ONLY (`AUTHORING.md` Sec2;
`community_kv`'s own module docstring: reputation/user-details are the
platform's only two cross-community exceptions, and this bundle is
neither). `dispatch()` raises before touching `kv` at all if
`envelope.community` is falsy, never defaulting to a tenant-wide bucket.

**kv key charset (gh-631).** Every `kv` key this bundle builds uses `.` as
its separator, never `:` -- the real `kv` host capability
(`core/bundle_host_kv/src/scope.rs::is_allowed_key_byte`) rejects any
guest-supplied key containing a byte outside ASCII alnum + `_`/`-`/`.`,
`:` being the host's own reserved namespace separator
(`waddle_sdk.kv.validate_key` enforces this on every call this bundle
makes, including through `community_kv`). Individual announcement *keys*
(the JSON object's own string keys, e.g. `welcome`/`rules`) are themselves
further restricted to `_KEY_ALLOWED_CHARS` -- conservative, but not
load-bearing for the kv charset itself (they never become `kv` key text on
their own; the whole registry lives under the one fixed `_REGISTRY_KEY`).

Gated behind the PostHog flag ``waddles.command-announce`` -- checked in
`transform()` after the cheap command-head match and before any grammar
resolution (`eightball`/`fish`/`music`'s own documented ordering
rationale).

DO NOT BUILD in v1 -- clean, documented extension points, never a silent
stub:

- **Scheduled/recurring posting** (e.g. "announce this every 30 minutes").
  This bundle is a save/recall store only; nothing here drives a timer.
  `bundles/python/command`'s own `!command timer ...` gh-613 gap (persists
  config, no firing scheduler yet) is the closest existing precedent for
  how that would eventually be built -- not attempted here.
- **Cross-community anything.** Every key here is scoped by `community_id`
  only (`waddle_sdk.community_kv` -- reputation/user-details are the
  platform's only two cross-community exceptions, and this bundle is
  neither).
"""

from __future__ import annotations

import json
from typing import Any, NoReturn, cast

from waddle_sdk import community_kv, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, ParsedCommand, parse_command
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-announce"

#: No sub-modules declared -- every token other than the grammar's own
#: `set`/`remove`/`list` verbs is either a bare key (see `_resolve_command`)
#: or an unimplemented-but-grammar-legal verb that resolves to `"usage"`.
SPEC = CommandSpec(name="announce")

#: Bounds for an announcement key -- conservative charset so a key can never
#: itself collide with `kv`'s own reserved separator/charset rules even
#: though it's stored as a JSON object key, not a `kv` key, in v1 (module
#: docstring).
MAX_KEY_LEN = 32
_KEY_ALLOWED_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")

#: Bounds one saved announcement's message length.
MAX_MESSAGE_LEN = 500

#: Caps unbounded growth of one community's saved-announcement registry
#: (mirrors `music`'s own `MAX_QUEUE_SIZE`).
MAX_REGISTRY_SIZE = 100

#: Durable per-community state -- never expires (`ttl_seconds=0`). The
#: entire `key -> message` mapping lives under this one `kv` key (module
#: docstring) -- colon-free (gh-631), never `announce:registry`.
_REGISTRY_KEY = "announce.registry"

_USAGE = (
    "Usage: !announce <key> | !announce set <key> <message> (mod only) | "
    "!announce remove <key> (mod only) | !announce list"
)
_PERMISSION_DENIED_MSG = "only moderators/broadcasters can configure !announce"
_EMPTY_LIST_MSG = "no announcements saved yet"

_KNOWN_COMMANDS = frozenset({"get", "list", "set", "remove", "usage"})


def _normalize_key(raw: str) -> str:
    """Lowercase + strip -- `!announce SET Welcome ...` and `!announce set welcome ...` collide."""
    return raw.strip().lower()


def _validate_key(key: str) -> str | None:
    """Return an error message, or `None` if `key` is safe to use as an announcement key."""
    if not key:
        return "an announcement key is required, e.g. `!announce set welcome hi there!`"
    if len(key) > MAX_KEY_LEN:
        return f"announcement keys must be {MAX_KEY_LEN} characters or fewer"
    if any(ch not in _KEY_ALLOWED_CHARS for ch in key):
        return "announcement keys may only contain letters, digits, '_' and '-'"
    return None


def _caller_role_signal(payload: dict[str, Any]) -> bool | None:
    """`True`/`False` from the normalized event's own badge fields, or `None` if absent.

    See `fish`/`music`'s own identical helper -- `None` (neither `is_mod`/
    `is_broadcaster` present, e.g. Discord's normalizer today) must be
    treated as denied, never as an implicit allow.
    """
    if "is_mod" not in payload and "is_broadcaster" not in payload:
        return None
    return payload.get("is_mod") is True or payload.get("is_broadcaster") is True


def _resolve_command(rest: str) -> tuple[str, str | None]:
    """Map the text after `!announce ` onto this bundle's own command set.

    Delegates to the shared grammar parser for the three VERBS this bundle
    implements (`set`/`remove`/`list`); every other grammar-legal VERB
    (`add`/`sub`/`enable`/`disable`/`delete`/`reset`, none of which this
    bundle declares sub-modules or behavior for) parses successfully but
    resolves to `"usage"`, same fail-loud-never-silent rule as `fish`'s own
    `_resolve_command`. A token that is NOT a VERB can't be expressed by the
    shared grammar at all (`SPEC` declares no sub-modules, so `parse_command`
    raises `CommandUsageError` for any such token) -- that's exactly the bare
    `!announce <key>` read shape (module docstring), so this function
    catches that one specific error and treats a single remaining token as a
    literal key instead of a usage failure.
    """
    normalized = f"!announce {rest}" if rest else "!announce"
    try:
        parsed = parse_command(normalized, SPEC)
    except CommandUsageError:
        # Not a declared verb. A key is exactly one token -- `!announce
        # my key` (whitespace inside) is a usage error, not a best-guess
        # partial match.
        if not rest or " " in rest:
            return "usage", None
        return "get", rest
    return _map_parsed(parsed)


def _map_parsed(parsed: ParsedCommand) -> tuple[str, str | None]:
    """Map a successfully parsed `ParsedCommand` onto this bundle's own command set."""
    if parsed.option is None:
        return "usage", None
    if parsed.option == "list":
        return ("usage", None) if parsed.args is not None else ("list", None)
    if parsed.option == "set":
        return ("usage", None) if parsed.args is None else ("set", parsed.args)
    if parsed.option == "remove":
        return ("usage", None) if parsed.args is None else ("remove", parsed.args)
    return "usage", None


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!announce` and its grammar.

    Cheap-skip first (no leading `!announce` token -- `None`, zero cost),
    flag check second, real grammar resolution last -- same ordering as
    `eightball`/`fish`/`music`'s own documented rationale. A recognized-but-
    malformed `!announce ...` still produces a reply (`"usage"`), never a
    silent drop.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None

    stripped = text.strip()
    head, _, rest = stripped.partition(" ")
    if head.lower() != "!announce":
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    command, arg = _resolve_command(rest.strip())

    log.info("announce.transform matched", command=command)
    payload: dict[str, Any] = {"command": command, "channel_id": event.payload.get("channel_id")}
    if arg is not None:
        payload["arg"] = arg
    # Forward the normalized badge signal, if present -- see `fish`/`music`'s own
    # identical forwarding comment for why absence must reach `dispatch` as absence, not `False`.
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
    """Fail-loud kv backend-error path: log, reply an error to chat, then re-raise.

    See `fish`/`music`'s own identical `_fail_kv`.
    """
    wit_error = getattr(exc, "value", exc)
    case_name = type(wit_error).__name__
    log.error("announce.kv_error", op=op, error=case_name)
    await relay.push(
        provider,
        {
            "channel": channel_id,
            "text": "saved announcements are temporarily unavailable, try again shortly.",
        },
    )
    raise RuntimeError(f"announce kv {op} failed: {case_name}") from exc


async def _fail_state(reason: str, *, provider: str, channel_id: str) -> NoReturn:
    """Fail-loud corrupt-stored-registry path -- see `music`'s own identical `_fail_state`."""
    log.error("announce.state_corrupt", reason=reason)
    await relay.push(
        provider,
        {
            "channel": channel_id,
            "text": "the saved announcements are corrupted, please contact support.",
        },
    )
    raise RuntimeError(f"announce corrupt state: {reason}")


async def _kv_get(community: str, key: str, *, provider: str, channel_id: str) -> bytes | None:
    """`community_kv.get`, fail-loud on a backend error (see `_fail_kv`)."""
    try:
        result = await community_kv.get(community, key)
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail_kv`
        await _fail_kv(exc, provider=provider, channel_id=channel_id, op="get")
    # `waddle_sdk` ships no `py.typed` marker, so mypy sees `Any` here -- cast back to the
    # real contract (`waddle_sdk/community_kv.py`'s own `get()` signature) rather than
    # leaking `Any` (see `music`'s own identical `_kv_get` for the same pattern).
    return cast("bytes | None", result)


async def _kv_set(
    community: str, key: str, value: bytes, *, ttl_seconds: int, provider: str, channel_id: str
) -> None:
    """`community_kv.set`, fail-loud on a backend error (see `_fail_kv`)."""
    try:
        await community_kv.set(community, key, value, ttl_seconds)
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail_kv`
        await _fail_kv(exc, provider=provider, channel_id=channel_id, op="set")


async def _load_registry(
    community: str, *, provider: str, channel_id: str
) -> dict[str, str]:
    """Return the community's saved announcements (`key -> message`), or `{}` if none yet.

    Raises (via `_fail_state`) on corrupt stored JSON -- see that helper's
    own docstring for why this is fail-loud rather than a silent reset.
    """
    raw = await _kv_get(community, _REGISTRY_KEY, provider=provider, channel_id=channel_id)
    if raw is None:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        await _fail_state(f"corrupt registry: {exc}", provider=provider, channel_id=channel_id)
    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in data.items()
    ):
        await _fail_state(
            "corrupt registry: expected a JSON object of string -> string",
            provider=provider,
            channel_id=channel_id,
        )
    return data


async def _save_registry(
    community: str, registry: dict[str, str], *, provider: str, channel_id: str
) -> None:
    """Persist the community's saved-announcement registry."""
    await _kv_set(
        community,
        _REGISTRY_KEY,
        json.dumps(registry).encode("utf-8"),
        ttl_seconds=0,
        provider=provider,
        channel_id=channel_id,
    )


async def _handle_get(
    key: str, *, community: str, provider: str, channel_id: str
) -> str:
    """Read+render the saved message for one key, or a not-found reply."""
    normalized = _normalize_key(key)
    registry = await _load_registry(community, provider=provider, channel_id=channel_id)
    message = registry.get(normalized)
    if message is None:
        return f"no saved announcement for '{normalized}'"
    return message


async def _handle_list(*, community: str, provider: str, channel_id: str) -> str:
    """Render every saved key for the community, sorted."""
    registry = await _load_registry(community, provider=provider, channel_id=channel_id)
    if not registry:
        return _EMPTY_LIST_MSG
    return "Announcements: " + ", ".join(sorted(registry))


async def _handle_set(
    arg: str | None, *, community: str, provider: str, channel_id: str
) -> str:
    """Parse+apply `!announce set <key> <message>`'s own free-text `args` tail."""
    if not arg:
        return _USAGE
    key_raw, _, message = arg.partition(" ")
    key = _normalize_key(key_raw)
    message = message.strip()

    error = _validate_key(key)
    if error:
        return error
    if not message:
        return "a message is required, e.g. `!announce set welcome hi there!`"
    if len(message) > MAX_MESSAGE_LEN:
        return f"announcement messages must be {MAX_MESSAGE_LEN} characters or fewer"

    registry = await _load_registry(community, provider=provider, channel_id=channel_id)
    if key not in registry and len(registry) >= MAX_REGISTRY_SIZE:
        return f"the announcement list is full ({MAX_REGISTRY_SIZE} max) -- remove one first"

    registry[key] = message
    await _save_registry(community, registry, provider=provider, channel_id=channel_id)
    log.info("announce.saved", community=community)
    return f"saved announcement '{key}'"


async def _handle_remove(
    arg: str | None, *, community: str, provider: str, channel_id: str
) -> str:
    """Parse+apply `!announce remove <key>`."""
    if not arg:
        return _USAGE
    cleaned = arg.strip()
    if " " in cleaned:
        return "announcement keys are a single word -- usage: !announce remove <key>"
    key = _normalize_key(cleaned)

    registry = await _load_registry(community, provider=provider, channel_id=channel_id)
    if key not in registry:
        return f"no saved announcement for '{key}'"

    del registry[key]
    await _save_registry(community, registry, provider=provider, channel_id=channel_id)
    log.info("announce.removed", community=community)
    return f"removed announcement '{key}'"


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: permission gate where required, then the kv op.

    Raises:
        ValueError: The envelope's payload has no `channel_id`; the
            envelope has no `community` (no tenant-wide fallback -- see
            module docstring's data-scoping section); or an unrecognized
            `command` (defensive -- `transform` only ever emits a member of
            `_KNOWN_COMMANDS`).
        RuntimeError: A `kv` backend call failed, or stored registry state
            was corrupt (see `_fail_kv`/`_fail_state` -- a chat error reply
            and an ERROR log line are always emitted first).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("announce reply requires a channel_id from the inbound chat.message")
    command = payload.get("command")
    if command not in _KNOWN_COMMANDS:
        raise ValueError(f"unrecognized announce command: {command!r}")

    provider = envelope.event.platform
    community = envelope.community
    if not community:
        log.error("announce.missing_community", command=command)
        raise ValueError("announce requires a community context and cannot operate tenant-wide")

    if command == "usage":
        await relay.push(provider, {"channel": channel_id, "text": _USAGE})
        return DispatchResult(transport=provider, detail="usage")

    # `get`/`list` are open to any caller; `set`/`remove` are
    # moderator/broadcaster only (module docstring).
    role_signal = _caller_role_signal(payload)
    if command in ("set", "remove") and role_signal is not True:
        log.info("announce.permission_denied", command=command, role_signal=str(role_signal))
        await relay.push(provider, {"channel": channel_id, "text": _PERMISSION_DENIED_MSG})
        return DispatchResult(transport=provider, detail=f"{command}:denied")

    arg = payload.get("arg")
    arg = arg if isinstance(arg, str) else None

    if command == "get":
        reply_text = await _handle_get(
            arg or "", community=community, provider=provider, channel_id=channel_id
        )
    elif command == "list":
        reply_text = await _handle_list(
            community=community, provider=provider, channel_id=channel_id
        )
    elif command == "set":
        reply_text = await _handle_set(
            arg, community=community, provider=provider, channel_id=channel_id
        )
    else:  # remove
        reply_text = await _handle_remove(
            arg, community=community, provider=provider, channel_id=channel_id
        )

    await relay.push(provider, {"channel": channel_id, "text": reply_text})
    log.info("announce.dispatch relayed", platform=provider, command=command)
    return DispatchResult(transport=provider, detail=command)
