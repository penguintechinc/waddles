"""`!sr`/`!songrequest` + `!music`/`!queue` -> a per-community song-request queue, kv-only.

Migrated from the `bot_process` monolith's song-request feature (task:
"Build the music (song-request queue) Python WASM app bundle"). Inspiration
credit (not a literal port -- see `bundle.yaml`'s `author`/`notice`): the
general "viewers queue up song requests, a mod advances the queue" shape is
inspired by superpenguintv (Psychoboy)'s `PenguinTwitchBot`
(https://github.com/Psychoboy/PenguinTwitchBot). No original source code or
text is reused here -- the grammar, queue data shape, and all logic below
are written fresh for Waddles, so no MIT notice reproduction is required
(`sdk/waddle-sdk/AUTHORING.md`'s attribution convention: verbatim reuse
needs the license text inline, inspiration-only needs credit only -- see
`fish`/`shoutout`'s own module docstrings for the same shape).

v1 scope (KV-ONLY -- a request QUEUE, never actual playback or an external
music-service API call; no `db`/`http`/egress capability used or declared):

- Bare `!sr <link-or-text>` / `!songrequest <link-or-text>` -- ADD a request
  to the caller's community queue, subject to a per-caller max-pending limit
  (`DEFAULT_MAX_PER_USER`, admin-configurable). These two command names are
  pure free-text adds -- unlike `!music`, they never go through the shared
  `waddle_sdk.command.parse_command` grammar (the request text itself may
  start with any token, including one that looks like a VERB, e.g. a URL),
  mirroring `shoutout`'s own documented "a positional argument has no slot
  in the shared grammar" extension.
- Bare `!music` / `!queue` -- SHOW the current queue (first `SHOW_LIMIT`
  entries). `!music list` is the same read, reached through the standard
  grammar's `list` verb instead of the bare case.
- `!music remove <id>` -- remove one request by its numeric id: the
  requester themself, or any moderator/broadcaster.
- `!music next` / `!music skip` -- moderator/broadcaster only: pop the head
  of the queue (now playing -> the next one up). `next`/`skip` are bare
  advance keywords outside the shared `waddle_sdk.command.VERBS`
  vocabulary -- `_resolve()` special-cases them before the formal grammar
  parse, rather than smuggling them in as fake sub-modules
  (`CommandSpec.sub_modules` names opt-in feature sub-modules, not verbs --
  see `waddle_sdk.sub_modules`'s own docstring).
- `!music set max-per-user <n>` -- moderator/broadcaster only: configures
  the per-caller max-pending-requests limit, bounded
  `[MIN_MAX_PER_USER, MAX_MAX_PER_USER]` (same `_caller_role_signal()`
  fail-closed pattern as `fish`/`shoutout`/`count`/`lurk`: absent badge
  fields -- e.g. Discord's normalizer today -- deny, never implicit allow).

Data scoping: the queue, its id sequence, and the max-per-user config are
all keyed by the envelope's own `community_id` ONLY, via
`waddle_sdk.community_kv` (`AUTHORING.md` Sec2). `dispatch()` raises before
touching `kv` at all if `envelope.community` is falsy, never defaulting to
a tenant-wide bucket.

PII note (same caveat as `fish`/`shoutout`/`lurk`): the tokenization
pipeline (#429) is not merged yet, so `event.actor` may currently be a raw
username. It is never stored in `kv` or logged in raw form --
`_pseudonym()` SHA-256-hashes it before it ever reaches `community_kv`, and
the queue itself stores only `{id, requester_pseudonym, text, ts}` --
never a raw username. Because only the pseudonym is retained, `!music`'s
queue listing cannot (and does not try to) show *who* requested each song;
it instead marks the caller's own entries `(yours)` by comparing the
caller's own freshly computed pseudonym against each stored one -- the same
"count, not identity" trade-off `shoutout`'s own `auto` sub-module
documents for its list command.

Gated behind the PostHog flag ``waddles.command-music`` -- checked in
`transform()` after the cheap command-head match and before any grammar
resolution (`eightball`'s documented ordering rationale).

DO NOT BUILD in v1 -- clean, documented extension points, never a silent
stub:

- **Actual playback / external music-service integration** (e.g. resolving
  a YouTube/Spotify link, driving an overlay). This bundle is a request
  QUEUE only -- `!music next`/`!music skip` advance the *data structure*;
  nothing here calls out to any player or external API, and no `http`/
  egress capability is declared in `bundle.yaml`.
- **Cross-community anything.** Every queue/sequence/config key here is
  scoped by `community_id` only (`waddle_sdk.community_kv` -- reputation/
  user-details are the platform's only two cross-community exceptions, and
  this bundle is neither).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, NoReturn, cast

from waddle_sdk import clock, community_kv, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, ParsedCommand, parse_command
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-music"

#: Chat-invoked heads. `!sr`/`!songrequest` are pure free-text "add" commands
#: (never run through `parse_command` -- see module docstring); `!music`/
#: `!queue` share the one grammar-based `SPEC` below.
_REQUEST_HEADS = frozenset({"!sr", "!songrequest"})
_QUEUE_HEADS = frozenset({"!music", "!queue"})

#: No sub-modules declared -- `!music` has no opt-in feature toggles.
SPEC = CommandSpec(name="music")

#: Bare advance keywords, handled ahead of the formal grammar (see module
#: docstring) -- neither is in `waddle_sdk.command.VERBS`.
_ADVANCE_VERBS = frozenset({"next", "skip"})

#: Per-caller max-pending-requests bounds -- keeps one viewer from
#: monopolizing the queue without a second confirmation step (mirrors
#: `fish`'s own `DEFAULT_COOLDOWN_SECONDS`/`MIN_`/`MAX_` convention).
DEFAULT_MAX_PER_USER = 3
MIN_MAX_PER_USER = 1
MAX_MAX_PER_USER = 10

#: Caps unbounded growth of one community's queue (mirrors `shoutout`'s own
#: `MAX_AUTO_LIST_SIZE`).
MAX_QUEUE_SIZE = 200

#: Caps one request's free-text length.
MAX_REQUEST_TEXT_LEN = 300

#: How many queue entries `!music`/`!music list` renders at once.
SHOW_LIMIT = 10

#: Durable per-community state -- never expires (`ttl_seconds=0`).
_QUEUE_KEY = "music:queue"
_SEQ_KEY = "music:seq"
_MAX_PER_USER_CONFIG_KEY = "music:config:max-per-user"

_USAGE = (
    "Usage: !sr <song> | !music | !music list | !music remove <id> | "
    "!music next|skip (mod only) | !music set max-per-user <n> (mod only)"
)
_PERMISSION_DENIED_MSG = "only moderators/broadcasters can do that"
_QUEUE_EMPTY_MSG = "the queue is empty."

_KNOWN_COMMANDS = frozenset({"add", "show", "remove", "config_set", "advance", "usage"})


@dataclass(slots=True, frozen=True)
class QueueEntry:
    """One per-community song-request queue entry -- PII-tokenized, never a raw username."""

    id: int
    requester_pseudonym: str
    text: str
    ts: int


def _pseudonym(actor: str | None) -> str:
    """Non-reversible per-caller key component -- see module docstring's PII note.

    `event.actor` may currently be a raw username (tokenization pipeline
    #429 not yet merged); hashing it before it ever reaches `community_kv`
    keeps this bundle PII-safe today and after #429 lands unchanged.
    """
    return hashlib.sha256((actor or "anonymous").encode()).hexdigest()


def _caller_role_signal(payload: dict[str, Any]) -> bool | None:
    """`True`/`False` from the normalized event's own badge fields, or `None` if absent.

    See `count`/`lurk`/`fish`/`shoutout`'s own identical helper -- `None`
    (neither `is_mod`/`is_broadcaster` present, e.g. Discord's normalizer
    today) must be treated as denied, never as an implicit allow.
    """
    if "is_mod" not in payload and "is_broadcaster" not in payload:
        return None
    return bool(payload.get("is_mod")) or bool(payload.get("is_broadcaster"))


def _resolve(rest: str) -> tuple[str, str | None]:
    """Map the text after `!music `/`!queue ` onto this bundle's own command set.

    `rest` is already stripped and may be empty. `next`/`skip` are handled
    here, before the formal grammar parse -- see module docstring. Every
    other shape is normalized onto `!music ...` and delegated to
    `parse_command` unchanged (so `!queue list` and `!music list` resolve
    identically).
    """
    if rest:
        first_tok, _, remainder = rest.partition(" ")
        if first_tok.lower() in _ADVANCE_VERBS:
            return ("usage", None) if remainder.strip() else ("advance", None)

    normalized = f"!music {rest}" if rest else "!music"
    try:
        parsed = parse_command(normalized, SPEC)
    except CommandUsageError:
        return "usage", None
    return _map_parsed(parsed)


def _map_parsed(parsed: ParsedCommand) -> tuple[str, str | None]:
    """Map a successfully parsed `ParsedCommand` onto this bundle's own command set.

    Only `option in (None, "list", "remove", "set")` is implemented --
    every other grammar-legal verb (`add`/`sub`/`enable`/`disable`/`reset`,
    none of which this bundle declares sub-modules or behavior for)
    resolves to `"usage"`, same fail-loud-never-silent rule as `fish`'s own
    `_resolve_command`.
    """
    if parsed.option is None:
        return "show", None
    if parsed.option == "list":
        return ("usage", None) if parsed.args is not None else ("show", None)
    if parsed.option == "remove":
        return ("usage", None) if parsed.args is None else ("remove", parsed.args)
    if parsed.option == "set":
        return ("usage", None) if parsed.args is None else ("config_set", parsed.args)
    return "usage", None


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize this bundle's four command heads.

    Cheap-skip first (no matching head -- `None`, zero cost), flag check
    second, real grammar resolution last -- same ordering as
    `eightball`/`fish`/`shoutout`'s own documented rationale. A recognized-
    but-malformed command still produces a reply (`"usage"`), never a
    silent drop.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None

    stripped = text.strip()
    head, _, rest = stripped.partition(" ")
    head_lower = head.lower()

    if head_lower in _REQUEST_HEADS:
        is_request = True
    elif head_lower in _QUEUE_HEADS:
        is_request = False
    else:
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    if is_request:
        arg = rest.strip() or None
        command = "add" if arg is not None else "usage"
    else:
        command, arg = _resolve(rest.strip())

    log.info("music.transform matched", command=command)
    payload: dict[str, Any] = {"command": command, "channel_id": event.payload.get("channel_id")}
    if arg is not None:
        payload["arg"] = arg
    # Forward the normalized badge signal, if present -- see `count`/`lurk`/`fish`/`shoutout`'s own
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

    __slots__ = ("transport", "detail", "sub_type", "http_status")

    def __init__(self, *, transport: str, detail: str) -> None:
        """Record which provider the reply was relayed to, and a short detail string."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = None


async def _fail_kv(exc: Exception, *, provider: str, channel_id: str, op: str) -> NoReturn:
    """Fail-loud kv backend-error path: log, reply an error to chat, then re-raise -- see `fish`."""
    wit_error = getattr(exc, "value", exc)
    case_name = type(wit_error).__name__
    log.error("music.kv_error", op=op, error=case_name)
    await relay.push(
        provider,
        {
            "channel": channel_id,
            "text": "the song queue is temporarily unavailable, try again shortly.",
        },
    )
    raise RuntimeError(f"music kv {op} failed: {case_name}") from exc


async def _fail_state(reason: str, *, provider: str, channel_id: str) -> NoReturn:
    """Fail-loud corrupt-stored-queue path -- see `shoutout`'s own identical `_fail_state`."""
    log.error("music.state_corrupt", reason=reason)
    await relay.push(
        provider,
        {"channel": channel_id, "text": "the song queue is corrupted, please contact support."},
    )
    raise RuntimeError(f"music corrupt state: {reason}")


async def _kv_get(community: str, key: str, *, provider: str, channel_id: str) -> bytes | None:
    """`community_kv.get`, fail-loud on a backend error (see `_fail_kv`)."""
    try:
        result = await community_kv.get(community, key)
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail_kv`
        await _fail_kv(exc, provider=provider, channel_id=channel_id, op="get")
    # `waddle_sdk` ships no `py.typed` marker, so mypy sees `Any` here -- cast back to the
    # real contract (`waddle_sdk/community_kv.py`'s own `get()` signature) rather than
    # leaking `Any` (see `shoutout`'s own identical `_kv_get` for the same pattern).
    return cast("bytes | None", result)


async def _kv_set(
    community: str, key: str, value: bytes, *, ttl_seconds: int, provider: str, channel_id: str
) -> None:
    """`community_kv.set`, fail-loud on a backend error (see `_fail_kv`)."""
    try:
        await community_kv.set(community, key, value, ttl_seconds)
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail_kv`
        await _fail_kv(exc, provider=provider, channel_id=channel_id, op="set")


async def _kv_increment(
    community: str, key: str, delta: int, *, ttl_seconds: int, provider: str, channel_id: str
) -> int:
    """`community_kv.increment`, fail-loud on a backend error (see `_fail_kv`)."""
    try:
        result = await community_kv.increment(community, key, delta, ttl_seconds)
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail_kv`
        await _fail_kv(exc, provider=provider, channel_id=channel_id, op="increment")
    # See `_kv_get`'s own identical cast-back-from-`Any` comment.
    return cast(int, result)


async def _load_queue(community: str, *, provider: str, channel_id: str) -> list[QueueEntry]:
    """Return the community's song-request queue, oldest-first.

    Raises (via `_fail_state`) on corrupt stored JSON -- see that helper's
    own docstring for why this is fail-loud rather than a silent reset.
    """
    raw = await _kv_get(community, _QUEUE_KEY, provider=provider, channel_id=channel_id)
    if raw is None:
        return []
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        await _fail_state(f"corrupt queue: {exc}", provider=provider, channel_id=channel_id)
    if not isinstance(data, list):
        await _fail_state(
            "corrupt queue: expected a JSON array", provider=provider, channel_id=channel_id
        )

    entries: list[QueueEntry] = []
    for item in data:
        if not isinstance(item, dict):
            await _fail_state(
                "corrupt queue entry: expected an object", provider=provider, channel_id=channel_id
            )
        try:
            entries.append(
                QueueEntry(
                    id=int(item["id"]),
                    requester_pseudonym=str(item["requester_pseudonym"]),
                    text=str(item["text"]),
                    ts=int(item["ts"]),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            await _fail_state(
                f"corrupt queue entry: {exc}", provider=provider, channel_id=channel_id
            )
    return entries


async def _save_queue(
    community: str, entries: list[QueueEntry], *, provider: str, channel_id: str
) -> None:
    """Persist the community's song-request queue, oldest-first."""
    serialized = [
        {
            "id": entry.id,
            "requester_pseudonym": entry.requester_pseudonym,
            "text": entry.text,
            "ts": entry.ts,
        }
        for entry in entries
    ]
    await _kv_set(
        community,
        _QUEUE_KEY,
        json.dumps(serialized).encode("utf-8"),
        ttl_seconds=0,
        provider=provider,
        channel_id=channel_id,
    )


async def _get_max_per_user(community: str, *, provider: str, channel_id: str) -> int:
    """Return the community's configured max-pending limit, or the default if unset/corrupt."""
    raw = await _kv_get(
        community, _MAX_PER_USER_CONFIG_KEY, provider=provider, channel_id=channel_id
    )
    if raw is None:
        return DEFAULT_MAX_PER_USER
    try:
        return int(raw.decode())
    except (UnicodeDecodeError, ValueError):
        log.error("music.max_per_user_config_corrupt", community=community)
        return DEFAULT_MAX_PER_USER


async def _handle_add(
    arg: str | None, *, community: str, actor: str | None, provider: str, channel_id: str
) -> str:
    """Validate+enqueue `!sr <text>`/`!songrequest <text>`'s own free-text request."""
    if not arg:
        return _USAGE
    text = arg.strip()
    if not text:
        return _USAGE
    if len(text) > MAX_REQUEST_TEXT_LEN:
        return f"song requests must be {MAX_REQUEST_TEXT_LEN} characters or fewer"

    queue = await _load_queue(community, provider=provider, channel_id=channel_id)
    if len(queue) >= MAX_QUEUE_SIZE:
        return f"the queue is full ({MAX_QUEUE_SIZE} max) -- try again once it drains"

    pseudonym = _pseudonym(actor)
    max_per_user = await _get_max_per_user(community, provider=provider, channel_id=channel_id)
    pending = sum(1 for entry in queue if entry.requester_pseudonym == pseudonym)
    if pending >= max_per_user:
        return f"you already have {pending} pending request(s) -- the limit is {max_per_user}"

    next_id = await _kv_increment(
        community, _SEQ_KEY, 1, ttl_seconds=0, provider=provider, channel_id=channel_id
    )
    queue.append(
        QueueEntry(id=next_id, requester_pseudonym=pseudonym, text=text, ts=clock.now_millis())
    )
    await _save_queue(community, queue, provider=provider, channel_id=channel_id)
    log.info("music.request_added", community=community)
    return f"added to the queue at position {len(queue)} (#{next_id})"


async def _handle_show(
    *, community: str, actor: str | None, provider: str, channel_id: str
) -> str:
    """Render the first `SHOW_LIMIT` queue entries, marking the caller's own `(yours)`."""
    queue = await _load_queue(community, provider=provider, channel_id=channel_id)
    if not queue:
        return _QUEUE_EMPTY_MSG

    pseudonym = _pseudonym(actor)
    shown = queue[:SHOW_LIMIT]
    rendered = [
        f"#{entry.id} {entry.text}" + (" (yours)" if entry.requester_pseudonym == pseudonym else "")
        for entry in shown
    ]
    remainder = len(queue) - len(shown)
    suffix = f" (+{remainder} more)" if remainder > 0 else ""
    return f"Queue ({len(queue)}): " + " | ".join(rendered) + suffix


async def _handle_remove(
    arg: str | None,
    *,
    community: str,
    actor: str | None,
    role_signal: bool | None,
    provider: str,
    channel_id: str,
) -> str:
    """Remove one queue entry by id -- the requester themself, or any moderator/broadcaster."""
    if not arg:
        return _USAGE
    cleaned = arg.strip()
    try:
        target_id = int(cleaned)
    except ValueError:
        return f"'{cleaned}' isn't a valid request id"

    queue = await _load_queue(community, provider=provider, channel_id=channel_id)
    match = next((entry for entry in queue if entry.id == target_id), None)
    if match is None:
        return f"no request #{target_id} found"

    pseudonym = _pseudonym(actor)
    is_owner = match.requester_pseudonym == pseudonym
    if not (is_owner or role_signal is True):
        return "you can only remove your own requests"

    remaining = [entry for entry in queue if entry.id != target_id]
    await _save_queue(community, remaining, provider=provider, channel_id=channel_id)
    log.info("music.request_removed", community=community)
    return f"removed request #{target_id}"


async def _handle_advance(*, community: str, provider: str, channel_id: str) -> str:
    """Pop the head of the queue (`!music next`/`!music skip`) -- moderator/broadcaster only."""
    queue = await _load_queue(community, provider=provider, channel_id=channel_id)
    if not queue:
        return _QUEUE_EMPTY_MSG

    removed, *remaining = queue
    await _save_queue(community, remaining, provider=provider, channel_id=channel_id)
    log.info("music.advanced", community=community)
    if remaining:
        return f"now playing: {removed.text} (up next: {remaining[0].text})"
    return f"now playing: {removed.text} (queue is now empty)"


async def _handle_config_set(
    arg: str | None, *, community: str, provider: str, channel_id: str
) -> str:
    """Parse+apply `!music set max-per-user <n>`'s own free-text `args` tail."""
    if not arg:
        return _USAGE
    parts = arg.split()
    if len(parts) != 2 or parts[0].lower() != "max-per-user":
        return _USAGE
    try:
        value = int(parts[1])
    except ValueError:
        return f"'{parts[1]}' isn't a whole number"
    if not (MIN_MAX_PER_USER <= value <= MAX_MAX_PER_USER):
        return f"max-per-user must be between {MIN_MAX_PER_USER} and {MAX_MAX_PER_USER}"
    await _kv_set(
        community,
        _MAX_PER_USER_CONFIG_KEY,
        str(value).encode(),
        ttl_seconds=0,
        provider=provider,
        channel_id=channel_id,
    )
    return f"max-per-user set to {value}"


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: permission gate where required, then the queue op.

    Raises:
        ValueError: The envelope's payload has no `channel_id`; the
            envelope has no `community` (no tenant-wide fallback -- see
            module docstring's data-scoping section); or an unrecognized
            `command` (defensive -- `transform` only ever emits a member of
            `_KNOWN_COMMANDS`).
        RuntimeError: A `kv` backend call failed, or stored queue state was
            corrupt (see `_fail_kv`/`_fail_state` -- a chat error reply and
            an ERROR log line are always emitted first).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("music reply requires a channel_id from the inbound chat.message")
    command = payload.get("command")
    if command not in _KNOWN_COMMANDS:
        raise ValueError(f"unrecognized music command: {command!r}")

    provider = envelope.event.platform
    community = envelope.community
    if not community:
        log.error("music.missing_community", command=command)
        raise ValueError("music requires a community context and cannot operate tenant-wide")

    if command == "usage":
        await relay.push(provider, {"channel": channel_id, "text": _USAGE})
        return DispatchResult(transport=provider, detail="usage")

    # `add`/`show`/`remove` are open to any caller (remove is further gated by
    # ownership-or-mod inside `_handle_remove`); `config_set`/`advance` are
    # moderator/broadcaster only (module docstring).
    role_signal = _caller_role_signal(payload)
    if command in ("config_set", "advance") and role_signal is not True:
        log.info("music.permission_denied", command=command, role_signal=str(role_signal))
        await relay.push(provider, {"channel": channel_id, "text": _PERMISSION_DENIED_MSG})
        return DispatchResult(transport=provider, detail=f"{command}:denied")

    arg = payload.get("arg")
    arg = arg if isinstance(arg, str) else None
    actor = envelope.event.actor

    if command == "add":
        reply_text = await _handle_add(
            arg, community=community, actor=actor, provider=provider, channel_id=channel_id
        )
    elif command == "show":
        reply_text = await _handle_show(
            community=community, actor=actor, provider=provider, channel_id=channel_id
        )
    elif command == "remove":
        reply_text = await _handle_remove(
            arg,
            community=community,
            actor=actor,
            role_signal=role_signal,
            provider=provider,
            channel_id=channel_id,
        )
    elif command == "config_set":
        reply_text = await _handle_config_set(
            arg, community=community, provider=provider, channel_id=channel_id
        )
    else:  # advance
        reply_text = await _handle_advance(
            community=community, provider=provider, channel_id=channel_id
        )

    await relay.push(provider, {"channel": channel_id, "text": reply_text})
    log.info("music.dispatch relayed", platform=provider, command=command)
    return DispatchResult(transport=provider, detail=command)
