"""`!joke` -> a random family-friendly joke, community-scoped, kv-only (light).

v1 scope (KV-ONLY, via `waddle_sdk.community_kv` -- deliberately does not
depend on the in-flight #623 `db` API, same scope decision as
`bundles/python/fish`'s own docstring):

- Bare `!joke` -- posts one random joke drawn from this module's curated,
  original, family-friendly `_BUILTIN_JOKES` list plus the community's own
  custom pool (see below). Avoids immediately repeating the previous joke
  by recording its reference in `kv` (`joke.last`) -- best-effort only: a
  pool of exactly one entry still returns that entry rather than erroring.
- `!joke add <text>` / `!joke remove <id>` -- broadcaster/moderator-only
  (same fail-closed `_caller_role_signal()` pattern as `fish`/`count`:
  absent badge fields, e.g. Discord's normalizer today, deny, never
  implicit allow). Adds/removes a per-community custom joke, which then
  joins the pool bare `!joke` draws from.
- `!joke list` -- open to anyone, lists only the community's own custom
  jokes (the built-in list is fixed source and never needs listing).

Uses the shared command grammar parser (`waddle_sdk.command.parse_command`/
`CommandSpec`, first adopted by `fish` -- #618) instead of hand-rolled
`text.split()`. `!joke` declares no sub-modules; `add`/`remove`/`list` are
all already members of the shared `VERBS` vocabulary
(`waddle_sdk.command.VERBS`), so no bundle-specific grammar extension is
needed.

Scope decision -- PER-COMMUNITY, not per-caller (mirrors `count`'s own
reasoning): the joke pool and "last joke told" state are channel-wide, so
every `kv` sub-key here is a plain literal string
(`joke.custom.registry`, `joke.custom.next_id`, `joke.last`) with no
per-actor hashing -- `waddle_sdk.community_kv` already scopes every key by
`community_id`, and no per-caller state is tracked at all (nobody's own
joke-telling history matters here, unlike `fish`'s per-caller catch stats).

**kv key charset (gh-631).** Every sub-key above uses `.` only, never `:`
-- the real `kv` host capability
(`core/bundle_host_kv/src/scope.rs::is_allowed_key_byte`) rejects any
guest-supplied key containing a byte outside ASCII alnum + `_`/`-`/`.`,
reserving `:` as its own server-side namespace separator. See
`waddle_sdk.kv`'s own module docstring and `count`'s 1.0.4 fix
(`fix/bump-count-lurk-1.0.4`) for the production incident this charset
rule exists to prevent; `tests/test_app.py::
test_kv_key_constants_satisfy_host_guest_key_charset` pins it here too.

Gated behind the PostHog flag ``waddles.command-joke`` -- see
`bundles/python/eightball/src/app.py`'s own docstring for the flag-gate
rationale and ordering (cheap command-match first, flag check second, real
grammar parse last).
"""

from __future__ import annotations

import json
import random
from typing import Any, NoReturn, cast

from waddle_sdk import community_kv, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, ParsedCommand, parse_command
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-joke"

#: No sub-modules declared -- `add`/`remove`/`list` are all already members
#: of the shared grammar's `VERBS` vocabulary (`waddle_sdk.command.VERBS`).
SPEC = CommandSpec(name="joke")

#: Durable per-community state -- never expires (`ttl_seconds=0`).
_CUSTOM_REGISTRY_KEY = "joke.custom.registry"
_CUSTOM_NEXT_ID_KEY = "joke.custom.next_id"
_LAST_JOKE_KEY = "joke.last"

#: Bounds for `!joke add <text>` -- keeps a mod from storing something
#: pathological (empty, or a wall of text no chat client renders sanely).
MIN_JOKE_LEN = 1
MAX_JOKE_LEN = 300

_USAGE = (
    "Usage: !joke | !joke add <text> | !joke remove <id> | !joke list "
    "(add/remove: mod/broadcaster only)"
)
_PERMISSION_DENIED_MSG = "only moderators/broadcasters can manage the joke pool"
_NO_CUSTOM_JOKES_MSG = "No custom jokes have been added yet."

_KNOWN_COMMANDS = frozenset({"tell", "add", "remove", "list", "usage"})

#: Curated, original, family-friendly built-in jokes -- written fresh for
#: Waddles, not copied from any third-party source. Stable `"b<n>"` ids so
#: `joke.last` can reference one without ambiguity against a custom entry
#: (`"c<id>"`).
_BUILTIN_JOKES: tuple[str, ...] = (
    "Why don't scientists trust atoms? Because they make up everything!",
    "What do you call cheese that isn't yours? Nacho cheese.",
    "I told my computer I needed a break, and it froze immediately.",
    "Why did the scarecrow win an award? He was outstanding in his field.",
    "Why don't eggs tell jokes? They'd crack each other up.",
    "I used to be a banker, but I lost interest.",
    "Why did the bicycle fall over? It was two-tired.",
    "What do you call a bear with no teeth? A gummy bear.",
    "Why can't you give Elsa a balloon? She'll let it go.",
    "What did one wall say to the other wall? I'll meet you at the corner.",
    "Why don't skeletons fight each other? They don't have the guts.",
    "How does a penguin build its house? Igloos it together.",
    "What's orange and sounds like a parrot? A carrot.",
    "Why did the coffee file a police report? It got mugged.",
)


class _KvFailure(Exception):
    """Internal-only: a `kv` host-call failed, or stored state was corrupt. Always caught.

    Mirrors `waddle_sdk.db`'s own documented pattern ("Err is structurally
    classified, never imported") since `waddle_sdk.community_kv` is a thin
    wrapper that does not classify or catch the generated WIT `Err` itself.
    """


async def _kv_get(community: str, key: str) -> bytes | None:
    """`community_kv.get`, reclassifying any backend error into `_KvFailure`."""
    try:
        result = await community_kv.get(community, key)
    except Exception as exc:  # noqa: BLE001 - classified like count/fish's own wrappers
        raise _KvFailure(f"kv.get({key!r}) failed: {getattr(exc, 'value', exc)}") from exc
    return cast("bytes | None", result)


async def _kv_set(community: str, key: str, value: bytes) -> None:
    """`community_kv.set` (no TTL -- joke state persists indefinitely), reclassifying `Err`."""
    try:
        await community_kv.set(community, key, value, ttl_seconds=0)
    except Exception as exc:  # noqa: BLE001
        raise _KvFailure(f"kv.set({key!r}) failed: {getattr(exc, 'value', exc)}") from exc


async def _kv_increment(community: str, key: str, delta: int) -> int:
    """`community_kv.increment` (no TTL), reclassifying `Err` into `_KvFailure`."""
    try:
        result = await community_kv.increment(community, key, delta, ttl_seconds=0)
    except Exception as exc:  # noqa: BLE001
        raise _KvFailure(f"kv.increment({key!r}) failed: {getattr(exc, 'value', exc)}") from exc
    return cast(int, result)


async def _load_custom_registry(community: str) -> dict[str, str]:
    """Return the community's custom joke id->text map, or `{}` if none exist yet.

    Raises:
        _KvFailure: The `kv` round trip failed, or the stored registry
            isn't a JSON object mapping string ids to string jokes --
            treated as a storage failure (fail-loud), never silently reset.
    """
    raw = await _kv_get(community, _CUSTOM_REGISTRY_KEY)
    if raw is None:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _KvFailure(f"corrupt joke custom registry: {exc}") from exc
    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in data.items()
    ):
        raise _KvFailure("corrupt joke custom registry: expected a JSON object of str->str")
    return cast("dict[str, str]", data)


async def _save_custom_registry(community: str, registry: dict[str, str]) -> None:
    """Persist the community's custom joke id->text map as JSON."""
    await _kv_set(community, _CUSTOM_REGISTRY_KEY, json.dumps(registry).encode("utf-8"))


def _caller_role_signal(payload: dict[str, Any]) -> bool | None:
    """`True`/`False` from the normalized event's own badge fields, or `None` if absent.

    See `fish`/`count`'s own identical helper -- `None` (neither
    `is_mod`/`is_broadcaster` present, e.g. Discord's normalizer today) must
    be treated as denied, never as an implicit allow.
    """
    if "is_mod" not in payload and "is_broadcaster" not in payload:
        return None
    return payload.get("is_mod") is True or payload.get("is_broadcaster") is True


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!joke` and its grammar, via `parse_command`.

    Cheap-skip first (no leading `!joke` token -- `None`, zero cost), flag
    check second, real grammar parse last -- same ordering as `eightball`'s
    and `fish`'s own documented rationale. A recognized-but-malformed
    `!joke ...` (a `CommandUsageError`, or a grammar-legal verb this bundle
    doesn't implement, e.g. `!joke enable`) still produces a reply
    (`"usage"`) since the caller did invoke this command -- never silently
    dropped.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None

    stripped = text.strip()
    head = stripped.partition(" ")[0]
    if head.lower() != "!joke":
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    try:
        parsed: ParsedCommand | None = parse_command(stripped, SPEC)
    except CommandUsageError:
        parsed = None
    command = _resolve_command(parsed)

    log.info("joke.transform matched", command=command)
    payload: dict[str, Any] = {"command": command, "channel_id": event.payload.get("channel_id")}
    if command in ("add", "remove") and parsed is not None:
        payload["arg"] = parsed.args
    # Forward the normalized badge signal, if present -- see `fish`/`count`'s own identical
    # forwarding comment for why absence must reach `dispatch` as absence, not `False`.
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


def _resolve_command(parsed: ParsedCommand | None) -> str:
    """Map a `ParsedCommand` (or `None` on a grammar error) onto this bundle's own command set.

    Only `option in (None, "add", "remove", "list")` is implemented --
    every other grammar-legal verb (`set`/`sub`/`enable`/`disable`/
    `delete`/`reset`, none of which this bundle declares sub-modules or
    behavior for) resolves to `"usage"`, same fail-loud-never-silent rule
    as a parse error itself.
    """
    if parsed is None:
        return "usage"
    if parsed.option is None:
        return "tell"
    if parsed.option == "list":
        return "list" if parsed.args is None else "usage"
    if parsed.option in ("add", "remove"):
        return cast(str, parsed.option)
    return "usage"


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result -- see `pyping`'s own `app.py`."""

    __slots__ = ("transport", "detail", "sub_type", "http_status")

    def __init__(self, *, transport: str, detail: str) -> None:
        """Record which provider the reply was relayed to, and a short detail string."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = None


async def _fail(reason: str, *, op: str, provider: str, channel_id: str) -> NoReturn:
    """Fail-loud kv/data error path: log, reply an error to chat, then raise -- see `fish`'s own."""
    log.error("joke.kv_error", op=op, error=reason)
    await relay.push(
        provider,
        {"channel": channel_id, "text": "jokes are temporarily unavailable, try again shortly."},
    )
    raise RuntimeError(f"joke {op} failed: {reason}")


async def _pick_joke(community: str, *, provider: str, channel_id: str) -> str:
    """Pick one joke from the combined built-in + custom pool, avoiding an immediate repeat."""
    try:
        custom = await _load_custom_registry(community)
    except _KvFailure as exc:
        await _fail(str(exc), op="load_registry", provider=provider, channel_id=channel_id)

    pool: list[tuple[str, str]] = [(f"b{i}", text) for i, text in enumerate(_BUILTIN_JOKES)]
    pool.extend((f"c{joke_id}", text) for joke_id, text in sorted(custom.items()))

    try:
        last_raw = await _kv_get(community, _LAST_JOKE_KEY)
    except _KvFailure as exc:
        await _fail(str(exc), op="get_last", provider=provider, channel_id=channel_id)
    last_ref = last_raw.decode("utf-8") if last_raw is not None else None

    candidates = [entry for entry in pool if entry[0] != last_ref] if len(pool) > 1 else pool
    ref, joke_text = random.choice(candidates)  # noqa: S311 - a game reply, not a security decision

    try:
        await _kv_set(community, _LAST_JOKE_KEY, ref.encode("utf-8"))
    except _KvFailure as exc:
        await _fail(str(exc), op="set_last", provider=provider, channel_id=channel_id)

    return joke_text


async def _handle_add(
    arg: str | None, *, community: str, provider: str, channel_id: str
) -> str:
    """Handle `!joke add <text>` -- caller-permission already checked by `dispatch`."""
    if arg is None or not arg.strip():
        return "Usage: !joke add <text>"
    text = arg.strip()
    if len(text) > MAX_JOKE_LEN:
        return f"joke text must be {MAX_JOKE_LEN} characters or fewer"

    try:
        new_id = await _kv_increment(community, _CUSTOM_NEXT_ID_KEY, 1)
        registry = await _load_custom_registry(community)
    except _KvFailure as exc:
        await _fail(str(exc), op="add", provider=provider, channel_id=channel_id)

    registry[str(new_id)] = text
    try:
        await _save_custom_registry(community, registry)
    except _KvFailure as exc:
        await _fail(str(exc), op="add_save", provider=provider, channel_id=channel_id)

    log.info("joke.custom_added", joke_id=new_id)
    return f"Added joke #{new_id} to the pool."


async def _handle_remove(
    arg: str | None, *, community: str, provider: str, channel_id: str
) -> str:
    """Handle `!joke remove <id>` -- caller-permission already checked by `dispatch`."""
    if arg is None or not arg.strip():
        return "Usage: !joke remove <id>"
    raw_id = arg.strip()
    if not raw_id.isdigit():
        return f"'{raw_id}' isn't a valid joke id"

    try:
        registry = await _load_custom_registry(community)
    except _KvFailure as exc:
        await _fail(str(exc), op="remove", provider=provider, channel_id=channel_id)

    if raw_id not in registry:
        return f"No custom joke #{raw_id} exists."
    del registry[raw_id]
    try:
        await _save_custom_registry(community, registry)
    except _KvFailure as exc:
        await _fail(str(exc), op="remove_save", provider=provider, channel_id=channel_id)

    log.info("joke.custom_removed", joke_id=raw_id)
    return f"Removed joke #{raw_id}."


async def _handle_list(community: str, *, provider: str, channel_id: str) -> str:
    """Handle `!joke list` -- open to anyone, lists only the community's custom jokes."""
    try:
        registry = await _load_custom_registry(community)
    except _KvFailure as exc:
        await _fail(str(exc), op="list", provider=provider, channel_id=channel_id)

    if not registry:
        return _NO_CUSTOM_JOKES_MSG
    entries = (f"#{joke_id}: {text}" for joke_id, text in sorted(registry.items(), key=lambda kv_: int(kv_[0])))
    return "Custom jokes: " + " | ".join(entries)


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: all kv state reads/writes, then relay the reply.

    Raises:
        ValueError: The envelope's payload has no `channel_id`; the
            envelope has no `community` (no tenant-wide fallback -- see
            module docstring's data-scoping section); or an unrecognized
            `command` (defensive -- `transform` only ever emits a member of
            `_KNOWN_COMMANDS`).
        RuntimeError: A `kv` backend call failed, or stored state was
            corrupt (see `_fail` -- a chat error reply and an ERROR log
            line are always emitted first).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("joke reply requires a channel_id from the inbound chat.message")
    command = payload.get("command")
    if command not in _KNOWN_COMMANDS:
        raise ValueError(f"unrecognized joke command: {command!r}")

    provider = envelope.event.platform
    community = envelope.community
    if not community:
        log.error("joke.missing_community", command=command)
        raise ValueError("joke requires a community context and cannot operate tenant-wide")

    if command == "usage":
        await relay.push(provider, {"channel": channel_id, "text": _USAGE})
        return DispatchResult(transport=provider, detail="usage")

    if command in ("add", "remove"):
        role_signal = _caller_role_signal(payload)
        if role_signal is not True:
            log.info("joke.permission_denied", command=command, role_signal=str(role_signal))
            await relay.push(provider, {"channel": channel_id, "text": _PERMISSION_DENIED_MSG})
            return DispatchResult(transport=provider, detail=f"{command}:denied")
        arg = payload.get("arg")
        arg = arg if isinstance(arg, str) else None
        if command == "add":
            reply_text = await _handle_add(
                arg, community=community, provider=provider, channel_id=channel_id
            )
        else:
            reply_text = await _handle_remove(
                arg, community=community, provider=provider, channel_id=channel_id
            )
        await relay.push(provider, {"channel": channel_id, "text": reply_text})
        log.info("joke.dispatch applied", command=command)
        return DispatchResult(transport=provider, detail=command)

    if command == "list":
        reply_text = await _handle_list(community, provider=provider, channel_id=channel_id)
    else:  # tell
        reply_text = await _pick_joke(community, provider=provider, channel_id=channel_id)

    await relay.push(provider, {"channel": channel_id, "text": reply_text})
    log.info("joke.dispatch relayed", platform=provider, command=command)
    return DispatchResult(transport=provider, detail=command)
