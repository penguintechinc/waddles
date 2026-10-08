"""`!compliment [@user]` -> a random compliment, community-scoped, kv-only (light).

v1 scope (KV-ONLY, via `waddle_sdk.community_kv` -- same scope decision as
`bundles/python/joke`'s own docstring, deliberately not depending on the
in-flight #623 `db` API):

- Bare `!compliment` -- posts one random compliment from this module's
  curated, original, family-friendly `_BUILTIN_COMPLIMENTS` list plus the
  community's own custom pool (see below), addressed to the caller
  themselves. Avoids immediately repeating the previous compliment by
  recording its reference in `kv` (`compliment.last`) -- best-effort only:
  a pool of exactly one entry still returns that entry rather than
  erroring (mirrors `joke`'s own `_pick_joke`).
- `!compliment <user>` -- same random pick, addressed to `<user>` instead
  of the caller. A leading `@` (common mention convention) is stripped.
- `!compliment add <text>` -- broadcaster/moderator-only (same fail-closed
  `_caller_role_signal()` pattern as `joke`/`shoutout`/`fish`: absent
  badge fields, e.g. Discord's normalizer today, deny, never implicit
  allow). Adds a per-community custom compliment, which then joins the
  pool bare/targeted `!compliment` draws from.

Uses the shared command grammar parser (`waddle_sdk.command.parse_command`/
`CommandSpec`). **One deliberate grammar extension**, identical to
`bundles/python/shoutout`'s own documented one: `!compliment <user>` (bare
command + a single positional username) has no slot in the shared grammar
-- `parse_command`'s bare case takes no argument, and a non-empty `rest`
must start with either a declared verb (`VERBS`) or a declared sub-module
name, or it raises `CommandUsageError`. `_resolve()` below special-cases
exactly this: a first token that is neither a verb nor a sub-module name
(this command declares none) is treated as the compliment target *before*
`parse_command` ever sees it; the `add` shape delegates to `parse_command`
unchanged.

Scope decision -- PER-COMMUNITY, not per-caller (mirrors `joke`'s own
reasoning): the compliment pool and "last compliment told" state are
channel-wide, so every `kv` sub-key here is a plain literal string
(`compliment.custom.registry`, `compliment.custom.next_id`,
`compliment.last`) with no per-actor hashing. Unlike `joke`/`wheel`'s own
dispatch, this bundle does **not** reject a `None` community:
`community_kv`'s own module docstring documents the host's tenant-wide
sentinel (`community_id=None`, scoped under the literal `"0"` segment) as
a valid community, not an opt-out -- today it is alpha's only activation
shape (one static scope per `svc-ingest-rust` pod), so refusing it here
would make this bundle permanently nonfunctional in that environment.
`envelope.community` is passed straight through to `community_kv`
unchanged.

PII note (same caveat as `shoutout`/`lurk`/`fish`): the tokenization
pipeline (#429) is not merged yet, so `event.actor` and a command's own
`<user>` argument may currently be raw usernames. Neither is ever stored
in `kv` or logged in raw form here -- the compliment reply itself is the
one place a raw handle is intentionally echoed (that IS the command's
purpose, exactly like `shoutout`'s own target echo), never a log line or
a `kv` value.

**kv key charset (gh-631).** Every sub-key above uses `.` only, never `:`
-- the real `kv` host capability
(`core/bundle_host_kv/src/scope.rs::is_allowed_key_byte`) rejects any
guest-supplied key containing a byte outside ASCII alnum + `_`/`-`/`.`,
reserving `:` as its own server-side namespace separator. See
`waddle_sdk.kv`'s own module docstring and `count`'s 1.0.4 fix
(`fix/bump-count-lurk-1.0.4`) for the production incident this charset
rule exists to prevent; `tests/test_app.py::
test_kv_key_constants_are_colon_free` pins it here too.

Gated behind the PostHog flag ``waddles.command-compliment`` -- see
`bundles/python/eightball/src/app.py`'s own docstring for the flag-gate
rationale and ordering (cheap command-match first, flag check second, real
grammar parse last).
"""

from __future__ import annotations

import json
import random
from typing import Any, NoReturn, cast

from waddle_sdk import community_kv, log, relay
from waddle_sdk.command import VERBS, CommandSpec, CommandUsageError, ParsedCommand, parse_command
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-compliment"

#: No sub-modules declared -- `add` is already a member of the shared grammar's `VERBS`
#: vocabulary (`waddle_sdk.command.VERBS`); the bare-target shape is handled by `_resolve()`
#: ahead of `parse_command` (see module docstring).
SPEC = CommandSpec(name="compliment")

#: Durable per-community state -- never expires (`ttl_seconds=0`).
_CUSTOM_REGISTRY_KEY = "compliment.custom.registry"
_CUSTOM_NEXT_ID_KEY = "compliment.custom.next_id"
_LAST_COMPLIMENT_KEY = "compliment.last"

#: Bounds for `!compliment add <text>` -- keeps a mod from storing something pathological
#: (empty, or a wall of text no chat client renders sanely). Mirrors `joke`'s own bounds.
MIN_COMPLIMENT_LEN = 1
MAX_COMPLIMENT_LEN = 300

#: Bounds for the optional `<user>` target -- mirrors `shoutout`'s own `_validate_target`.
MAX_TARGET_LEN = 32
_TARGET_ALLOWED_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-."
)

_USAGE = "Usage: !compliment | !compliment <user> | !compliment add <text> (add: mod/broadcaster only)"
_PERMISSION_DENIED_MSG = "only moderators/broadcasters can manage the compliment pool"
_DEFAULT_TARGET = "friend"

_KNOWN_COMMANDS = frozenset({"tell", "add", "usage"})

#: Curated, original, family-friendly built-in compliments -- written fresh for Waddles, not
#: copied from any third-party source. Phrased as the clause following "<target>, " so a
#: reply reads naturally for both the bare (self-addressed) and targeted shapes.
_BUILTIN_COMPLIMENTS: tuple[str, ...] = (
    "you light up every room you walk into!",
    "your kindness never goes unnoticed.",
    "you make hard things look easy.",
    "your energy is contagious in the best way.",
    "you've got a heart of gold.",
    "you always know how to make people smile.",
    "your creativity never stops amazing people.",
    "you're a genuinely great teammate.",
    "you bring out the best in everyone around you.",
    "your determination is inspiring.",
    "you're way more talented than you give yourself credit for.",
    "you make this community a better place just by being in it.",
    "your sense of humor is top-tier.",
    "you handle challenges with real grace.",
)


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
    """`community_kv.set` (no TTL -- compliment state persists indefinitely), reclassifying `Err`."""
    try:
        await community_kv.set(community, key, value, ttl_seconds=0)
    except Exception as exc:  # noqa: BLE001
        raise _KvFailure(f"kv.set({key!r}) failed: {getattr(exc, 'value', exc)}") from exc


async def _kv_increment(community: str | None, key: str, delta: int) -> int:
    """`community_kv.increment` (no TTL), reclassifying `Err` into `_KvFailure`."""
    try:
        result = await community_kv.increment(community, key, delta, ttl_seconds=0)
    except Exception as exc:  # noqa: BLE001
        raise _KvFailure(f"kv.increment({key!r}) failed: {getattr(exc, 'value', exc)}") from exc
    return cast(int, result)


async def _load_custom_registry(community: str | None) -> dict[str, str]:
    """Return the community's custom compliment id->text map, or `{}` if none exist yet.

    Raises:
        _KvFailure: The `kv` round trip failed, or the stored registry
            isn't a JSON object mapping string ids to string compliments --
            treated as a storage failure (fail-loud), never silently reset.
    """
    raw = await _kv_get(community, _CUSTOM_REGISTRY_KEY)
    if raw is None:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _KvFailure(f"corrupt compliment custom registry: {exc}") from exc
    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in data.items()
    ):
        raise _KvFailure("corrupt compliment custom registry: expected a JSON object of str->str")
    return cast("dict[str, str]", data)


async def _save_custom_registry(community: str | None, registry: dict[str, str]) -> None:
    """Persist the community's custom compliment id->text map as JSON."""
    await _kv_set(community, _CUSTOM_REGISTRY_KEY, json.dumps(registry).encode("utf-8"))


def _caller_role_signal(payload: dict[str, Any]) -> bool | None:
    """`True`/`False` from the normalized event's own badge fields, or `None` if absent.

    See `joke`/`shoutout`/`fish`'s own identical helper -- `None` (neither
    `is_mod`/`is_broadcaster` present, e.g. Discord's normalizer today) must
    be treated as denied, never as an implicit allow.
    """
    if "is_mod" not in payload and "is_broadcaster" not in payload:
        return None
    return bool(payload.get("is_mod")) or bool(payload.get("is_broadcaster"))


def _validate_target(raw: str) -> tuple[str | None, str | None]:
    """Return `(cleaned_target, None)`, or `(None, error_message)`.

    Strips one leading `@` (a common mention convention) before
    validating. Bounded to `MAX_TARGET_LEN` with a conservative charset --
    mirrors `shoutout`'s own `_validate_target`.
    """
    cleaned = raw.strip().lstrip("@").strip()
    if not cleaned:
        return None, "a username is required, e.g. `!compliment penguin`"
    if len(cleaned) > MAX_TARGET_LEN:
        return None, f"usernames must be {MAX_TARGET_LEN} characters or fewer"
    if any(ch not in _TARGET_ALLOWED_CHARS for ch in cleaned):
        return None, "usernames may only contain letters, digits, '_', '-', and '.'"
    return cleaned, None


def _resolve(rest: str) -> tuple[str, str | None]:
    """Map the text after `!compliment ` onto this bundle's own command set.

    `rest` is already stripped and may be empty. See module docstring for
    the bare-positional-target grammar extension this implements ahead of
    `parse_command`.
    """
    if not rest:
        return "tell", None

    tok1 = rest.split(" ", 1)[0]
    tok1_lower = tok1.lower()
    if tok1_lower not in VERBS:
        # Not a verb -- a positional compliment target. Usernames never contain spaces, so a
        # multi-token `rest` here is a usage error, never a best-guess multi-word target.
        if " " in rest:
            return "usage", None
        return "tell", rest

    try:
        parsed: ParsedCommand = parse_command(f"!compliment {rest}", SPEC)
    except CommandUsageError as exc:
        log.debug("compliment.invalid_grammar", rest=rest, error=str(exc))
        return "usage", None
    if parsed.sub_module is None and parsed.option == "add":
        return "add", parsed.args
    return "usage", None


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!compliment` and its grammar.

    Cheap-skip first (no leading `!compliment` token -- `None`, zero cost),
    flag check second, real grammar resolution last -- same ordering as
    `eightball`/`shoutout`'s own documented rationale. A recognized-but-
    malformed `!compliment ...` still produces a reply (`"usage"`) since
    the caller did invoke this command -- never silently dropped.
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

    log.info("compliment.transform matched", command=command)
    payload: dict[str, Any] = {"command": command, "channel_id": event.payload.get("channel_id")}
    if arg is not None:
        payload["arg"] = arg
    # Forward the normalized badge signal, if present -- see `joke`/`shoutout`'s own identical
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
    """Fail-loud kv/data error path: log, reply an error to chat, then raise -- see `joke`'s own."""
    log.error("compliment.kv_error", op=op, error=reason)
    await relay.push(
        provider,
        {
            "channel": channel_id,
            "text": "compliments are temporarily unavailable, try again shortly.",
        },
    )
    raise RuntimeError(f"compliment {op} failed: {reason}")


async def _pick_compliment(community: str | None, *, provider: str, channel_id: str) -> str:
    """Pick one compliment from the combined built-in + custom pool, avoiding an immediate repeat."""
    try:
        custom = await _load_custom_registry(community)
    except _KvFailure as exc:
        await _fail(str(exc), op="load_registry", provider=provider, channel_id=channel_id)

    pool: list[tuple[str, str]] = [(f"b{i}", text) for i, text in enumerate(_BUILTIN_COMPLIMENTS)]
    pool.extend((f"c{cid}", text) for cid, text in sorted(custom.items()))

    try:
        last_raw = await _kv_get(community, _LAST_COMPLIMENT_KEY)
    except _KvFailure as exc:
        await _fail(str(exc), op="get_last", provider=provider, channel_id=channel_id)
    last_ref = last_raw.decode("utf-8") if last_raw is not None else None

    candidates = [entry for entry in pool if entry[0] != last_ref] if len(pool) > 1 else pool
    ref, text = random.choice(candidates)  # noqa: S311 - a game reply, not a security decision

    try:
        await _kv_set(community, _LAST_COMPLIMENT_KEY, ref.encode("utf-8"))
    except _KvFailure as exc:
        await _fail(str(exc), op="set_last", provider=provider, channel_id=channel_id)

    return text


async def _handle_tell(
    target_raw: str | None,
    *,
    caller: str | None,
    community: str | None,
    provider: str,
    channel_id: str,
) -> str:
    """Handle bare `!compliment` (self-addressed) or `!compliment <user>` (targeted)."""
    if target_raw is not None:
        cleaned, err = _validate_target(target_raw)
        if err is not None:
            return err
        who = cleaned
    else:
        who = caller or _DEFAULT_TARGET

    text = await _pick_compliment(community, provider=provider, channel_id=channel_id)
    return f"{who}, {text}"


async def _handle_add(
    arg: str | None, *, community: str | None, provider: str, channel_id: str
) -> str:
    """Handle `!compliment add <text>` -- caller-permission already checked by `dispatch`."""
    if arg is None or not arg.strip():
        return "Usage: !compliment add <text>"
    text = arg.strip()
    if len(text) > MAX_COMPLIMENT_LEN:
        return f"compliment text must be {MAX_COMPLIMENT_LEN} characters or fewer"

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

    log.info("compliment.custom_added", compliment_id=new_id)
    return f"Added compliment #{new_id} to the pool."


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
        RuntimeError: A `kv` backend call failed, or stored state was
            corrupt (see `_fail` -- a chat error reply and an ERROR log
            line are always emitted first).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("compliment reply requires a channel_id from the inbound chat.message")
    command = payload.get("command")
    if command not in _KNOWN_COMMANDS:
        raise ValueError(f"unrecognized compliment command: {command!r}")

    provider = envelope.event.platform
    community = envelope.community

    if command == "usage":
        await relay.push(provider, {"channel": channel_id, "text": _USAGE})
        return DispatchResult(transport=provider, detail="usage")

    if command == "add":
        role_signal = _caller_role_signal(payload)
        if role_signal is not True:
            log.info("compliment.permission_denied", command=command, role_signal=str(role_signal))
            await relay.push(provider, {"channel": channel_id, "text": _PERMISSION_DENIED_MSG})
            return DispatchResult(transport=provider, detail="add:denied")
        arg = payload.get("arg")
        arg = arg if isinstance(arg, str) else None
        reply_text = await _handle_add(
            arg, community=community, provider=provider, channel_id=channel_id
        )
        await relay.push(provider, {"channel": channel_id, "text": reply_text})
        log.info("compliment.dispatch applied", command=command)
        return DispatchResult(transport=provider, detail=command)

    # tell
    arg = payload.get("arg")
    arg = arg if isinstance(arg, str) else None
    reply_text = await _handle_tell(
        arg,
        caller=envelope.event.actor,
        community=community,
        provider=provider,
        channel_id=channel_id,
    )
    await relay.push(provider, {"channel": channel_id, "text": reply_text})
    log.info("compliment.dispatch relayed", platform=provider, command=command)
    return DispatchResult(transport=provider, detail=command)
