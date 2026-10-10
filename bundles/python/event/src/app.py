"""`!event` / `!rsvp` -> light community events and RSVPs, KV-only.

Commands:

- `!event` / `!event list` -- list the community's events (open to anyone).
- `!event view <id>` -- one event plus its yes/no RSVP counts (open to anyone).
- `!event create <name> <when>` (alias `add`) -- mod/broadcaster-only. `<name>` is one token or a
  `"quoted phrase"`; `<when>` is the free-text remainder (no date parsing -- display only).
- `!event remove <id>` (alias `delete`) -- mod/broadcaster-only.
- `!rsvp <id> yes|no` -- open to anyone with a linked identity; re-RSVPing changes the answer.

This is a LIGHT kv bundle, deliberately separate from the beta-trio event-sync feature (Waddles
as source of truth syncing OUT to Discord) -- it shares no tables, keys, or code with it.

Grammar note: `create`/`view` are not members of the shared `waddle_sdk.command.VERBS`
vocabulary, so this bundle parses its own small grammar (`add`/`delete`/`remove`/`list` are the
shared verbs and are accepted as aliases) rather than `parse_command`.

PII: RSVPs are keyed by the caller's **UUID, never a username**. `event.actor` is accepted only
if it parses as a UUID (stored in canonical lower-case form); anything else -- e.g. a raw handle
before the PII-tokenization pipeline reaches this platform -- gets a loud "identity not linked"
reply and is never stored or logged (fail-loud, no silent fallback to the raw handle). Logs carry
only the command, event id, counts and the community scope -- never an event name/time, the
actor, or the RSVP answers per user.

State (`waddle_sdk.community_kv`, community-scoped; `community=None` is the host's valid
tenant-wide sentinel, passed straight through like `about`). Keys use `.` only (gh-631):

- `event.seq`        -- monotonically increasing id counter (kv `increment`)
- `event.index`      -- JSON list of live event ids (capped at `MAX_EVENTS`)
- `event.rec.<id>`   -- JSON `{"name": ..., "when": ...}`
- `event.rsvp.<id>`  -- JSON `{"<uuid>": "yes"|"no"}` (capped at `MAX_RSVPS`)

Index / RSVP updates are read-modify-write (kv has no list/CAS); concurrent writers within one
community can lose an update. Acceptable for a chat-scale light bundle, documented not hidden.

Gated behind the PostHog flag ``waddles.command-event`` (default OFF; cheap command-match first,
flag check second, grammar parse last).
"""

from __future__ import annotations

import json
import uuid
from typing import Any, NoReturn, cast

from waddle_sdk import community_kv, log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-event"

_EVENT_HEAD = "!event"
_RSVP_HEAD = "!rsvp"

_SEQ_KEY = "event.seq"
_INDEX_KEY = "event.index"
_REC_PREFIX = "event.rec."
_RSVP_PREFIX = "event.rsvp."

MAX_EVENTS = 50
MAX_RSVPS = 500
MAX_NAME_LEN = 40
MAX_WHEN_LEN = 64
MAX_ID_LEN = 9
LIST_SHOW = 10

_USAGE = (
    "Usage: !event [list] | !event view <id> | !event create <name> <when> (mod) | "
    "!event remove <id> (mod) | !rsvp <id> yes|no"
)
_PERMISSION_DENIED_MSG = "only moderators/broadcasters can create or remove events"
_NO_IDENTITY_MSG = "RSVP needs a linked Waddles identity; yours isn't linked yet"

_CREATE_VERBS = frozenset({"create", "add"})
_REMOVE_VERBS = frozenset({"remove", "delete"})
_LIST_VERBS = frozenset({"list"})
_CHOICES = frozenset({"yes", "no"})
_KNOWN_COMMANDS = frozenset({"list", "view", "create", "remove", "rsvp", "usage"})


class _KvFailure(Exception):
    """Internal-only: a `kv` host-call failed, or stored state was corrupt. Always caught."""


def _has_control_chars(value: str) -> bool:
    """True if `value` contains any ASCII control character (newline, tab, NUL, ...)."""
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def _parse_id(token: str | None) -> str | None:
    """A positive decimal event id (digits only, bounded length), or `None`."""
    if token is None or not token.isascii() or not token.isdigit():
        return None
    if len(token) > MAX_ID_LEN or int(token) < 1:
        return None
    return str(int(token))


def _split_name_when(rest: str) -> tuple[str, str] | None:
    """Split `<name> <when>`: name = one token or a double-quoted phrase; when = remainder."""
    rest = rest.strip()
    if rest.startswith('"'):
        end = rest.find('"', 1)
        if end == -1:
            return None
        name, when = rest[1:end], rest[end + 1 :]
    else:
        name, _, when = rest.partition(" ")
    name, when = name.strip(), when.strip()
    if not name or not when:
        return None
    if len(name) > MAX_NAME_LEN or len(when) > MAX_WHEN_LEN:
        return None
    if _has_control_chars(name) or _has_control_chars(when):
        return None
    return name, when


def _parse(head: str, rest: str) -> dict[str, Any]:
    """Parse the command grammar into a normalized payload; `{"command": "usage"}` if malformed."""
    usage: dict[str, Any] = {"command": "usage"}
    rest = rest.strip()
    if head == _RSVP_HEAD:
        id_tok, _, choice = rest.partition(" ")
        event_id, choice = _parse_id(id_tok), choice.strip().lower()
        if event_id is None or choice not in _CHOICES:
            return usage
        return {"command": "rsvp", "event_id": event_id, "choice": choice}

    verb, _, tail = rest.partition(" ")
    verb, tail = verb.lower(), tail.strip()
    if not verb or verb in _LIST_VERBS:
        return {"command": "list"} if not tail else usage
    if verb == "view":
        event_id = _parse_id(tail)
        return {"command": "view", "event_id": event_id} if event_id else usage
    if verb in _REMOVE_VERBS:
        event_id = _parse_id(tail)
        return {"command": "remove", "event_id": event_id} if event_id else usage
    if verb in _CREATE_VERBS:
        parts = _split_name_when(tail)
        if parts is None:
            return usage
        return {"command": "create", "name": parts[0], "when": parts[1]}
    return usage


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!event`/`!rsvp` and parse its grammar.

    Returns `None` for non-matching text or while the flag is off. A recognized-but-malformed
    command still yields a `usage` reply -- the caller did invoke it, never silently dropped.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None

    stripped = text.strip()
    head, _, rest = stripped.partition(" ")
    head = head.lower()
    if head not in (_EVENT_HEAD, _RSVP_HEAD):
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    payload = _parse(head, rest)
    payload["channel_id"] = event.payload.get("channel_id")
    if "is_mod" in event.payload:
        payload["is_mod"] = bool(event.payload["is_mod"])
    if "is_broadcaster" in event.payload:
        payload["is_broadcaster"] = bool(event.payload["is_broadcaster"])
    log.info("event.transform matched", command=payload["command"])

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


def _caller_role_signal(payload: dict[str, Any]) -> bool | None:
    """`True`/`False` from the normalized badge fields, or `None` if absent (treated as denied)."""
    if "is_mod" not in payload and "is_broadcaster" not in payload:
        return None
    return bool(payload.get("is_mod")) or bool(payload.get("is_broadcaster"))


def _actor_uuid(actor: str | None) -> str | None:
    """Canonical lower-case UUID string if `actor` is a UUID, else `None` (never a username)."""
    if not isinstance(actor, str):
        return None
    try:
        return str(uuid.UUID(actor))
    except ValueError:
        log.debug("event.actor_not_uuid", error="ValueError")
        return None


async def _kv_get(community: str | None, key: str) -> bytes | None:
    """`community_kv.get`, reclassifying any backend error into `_KvFailure`."""
    try:
        return cast("bytes | None", await community_kv.get(community, key))
    except Exception as exc:
        raise _KvFailure(f"kv.get({key!r}) failed: {getattr(exc, 'value', exc)}") from exc


async def _kv_set(community: str | None, key: str, value: bytes) -> None:
    """`community_kv.set` (no TTL), reclassifying any backend error."""
    try:
        await community_kv.set(community, key, value, ttl_seconds=0)
    except Exception as exc:
        raise _KvFailure(f"kv.set({key!r}) failed: {getattr(exc, 'value', exc)}") from exc


async def _kv_delete(community: str | None, key: str) -> None:
    """`community_kv.delete`, reclassifying any backend error."""
    try:
        await community_kv.delete(community, key)
    except Exception as exc:
        raise _KvFailure(f"kv.delete({key!r}) failed: {getattr(exc, 'value', exc)}") from exc


async def _kv_increment(community: str | None, key: str) -> int:
    """`community_kv.increment(+1)`, reclassifying any backend error."""
    try:
        return int(await community_kv.increment(community, key, 1))
    except Exception as exc:
        raise _KvFailure(f"kv.increment({key!r}) failed: {getattr(exc, 'value', exc)}") from exc


async def _load_json(community: str | None, key: str) -> Any | None:
    """Read + decode a JSON value; corrupt stored state is a `_KvFailure`, never a default."""
    raw = await _kv_get(community, key)
    if raw is None:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _KvFailure(f"corrupt stored json at {key!r}") from exc


async def _save_json(community: str | None, key: str, value: Any) -> None:
    """Encode + write a JSON value."""
    await _kv_set(community, key, json.dumps(value, separators=(",", ":")).encode("utf-8"))


async def _load_index(community: str | None) -> list[str]:
    """The live event-id list (empty if none yet); a malformed index is a `_KvFailure`."""
    data = await _load_json(community, _INDEX_KEY)
    if data is None:
        return []
    if not isinstance(data, list) or not all(isinstance(i, str) for i in data):
        raise _KvFailure("event.index is not a list of ids")
    return cast("list[str]", data)


async def _load_record(community: str | None, event_id: str) -> dict[str, str] | None:
    """One event record, or `None` if it doesn't exist; malformed record is a `_KvFailure`."""
    data = await _load_json(community, f"{_REC_PREFIX}{event_id}")
    if data is None:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("name"), str):
        raise _KvFailure(f"malformed event record {event_id}")
    if not isinstance(data.get("when"), str):
        raise _KvFailure(f"malformed event record {event_id}")
    return cast("dict[str, str]", data)


async def _load_rsvps(community: str | None, event_id: str) -> dict[str, str]:
    """The `{uuid: yes|no}` map for an event (empty if none); malformed is a `_KvFailure`."""
    data = await _load_json(community, f"{_RSVP_PREFIX}{event_id}")
    if data is None:
        return {}
    if not isinstance(data, dict) or any(v not in _CHOICES for v in data.values()):
        raise _KvFailure(f"malformed rsvp map for event {event_id}")
    return cast("dict[str, str]", data)


async def _fail(reason: str, *, op: str, provider: str, channel_id: str) -> NoReturn:
    """Fail-loud kv error path: log (reason only), reply an error to chat, then raise."""
    log.error("event.kv_error", op=op, error=reason)
    await relay.push(
        provider,
        {"channel": channel_id, "text": "events are temporarily unavailable, try again shortly."},
    )
    raise RuntimeError(f"event {op} failed: {reason}")


async def _create(payload: dict[str, Any], community: str | None) -> str:
    """Create an event: cap check, allocate id, write record, append to index."""
    index = await _load_index(community)
    if len(index) >= MAX_EVENTS:
        return f"event limit reached ({MAX_EVENTS}); remove one first"
    event_id = str(await _kv_increment(community, _SEQ_KEY))
    await _save_json(
        community, f"{_REC_PREFIX}{event_id}", {"name": payload["name"], "when": payload["when"]}
    )
    await _save_json(community, _INDEX_KEY, [*index, event_id])
    log.info("event.created", event_id=event_id, total=len(index) + 1)
    return f"Created event #{event_id}: {payload['name']} @ {payload['when']}"


async def _list(community: str | None) -> str:
    """One-line summary of the most recent events."""
    index = await _load_index(community)
    if not index:
        return "No events yet. Mods can add one with !event create <name> <when>"
    shown = index[-LIST_SHOW:]
    parts: list[str] = []
    for event_id in reversed(shown):
        record = await _load_record(community, event_id)
        if record is None:
            raise _KvFailure(f"index references missing event {event_id}")
        parts.append(f"#{event_id} {record['name']} ({record['when']})")
    more = f" (+{len(index) - len(shown)} older)" if len(index) > len(shown) else ""
    return "Events: " + " | ".join(parts) + more


async def _view(event_id: str, community: str | None) -> str:
    """Event details plus yes/no counts."""
    record = await _load_record(community, event_id)
    if record is None:
        return f"No event #{event_id}"
    rsvps = await _load_rsvps(community, event_id)
    yes = sum(1 for v in rsvps.values() if v == "yes")
    return (
        f"#{event_id} {record['name']} @ {record['when']} -- "
        f"yes: {yes}, no: {len(rsvps) - yes} (!rsvp {event_id} yes|no)"
    )


async def _remove(event_id: str, community: str | None) -> str:
    """Delete an event, its RSVPs, and its index entry."""
    index = await _load_index(community)
    if event_id not in index:
        return f"No event #{event_id}"
    await _save_json(community, _INDEX_KEY, [i for i in index if i != event_id])
    await _kv_delete(community, f"{_REC_PREFIX}{event_id}")
    await _kv_delete(community, f"{_RSVP_PREFIX}{event_id}")
    log.info("event.removed", event_id=event_id)
    return f"Removed event #{event_id}"


async def _rsvp(event_id: str, choice: str, actor: str | None, community: str | None) -> str:
    """Record the caller's yes/no, keyed by their UUID only."""
    voter = _actor_uuid(actor)
    if voter is None:
        log.warn("event.rsvp_no_identity", event_id=event_id)
        return _NO_IDENTITY_MSG
    if await _load_record(community, event_id) is None:
        return f"No event #{event_id}"
    rsvps = await _load_rsvps(community, event_id)
    if voter not in rsvps and len(rsvps) >= MAX_RSVPS:
        return f"event #{event_id} is full ({MAX_RSVPS} RSVPs)"
    rsvps[voter] = choice
    await _save_json(community, f"{_RSVP_PREFIX}{event_id}", rsvps)
    log.info("event.rsvp", event_id=event_id, total=len(rsvps))
    return f"RSVP recorded for event #{event_id}: {choice}"


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: all kv state reads/writes, then relay the reply.

    Raises:
        ValueError: No `channel_id`, or an unrecognized `command` (defensive).
        RuntimeError: A `kv` call failed or stored state was corrupt (a chat error reply and an
            ERROR log line are always emitted first).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("event reply requires a channel_id from the inbound chat.message")
    command = payload.get("command")
    if command not in _KNOWN_COMMANDS:
        raise ValueError(f"unrecognized event command: {command!r}")

    provider = envelope.event.platform
    community = envelope.community

    if command in ("create", "remove") and _caller_role_signal(payload) is not True:
        log.info("event.permission_denied", command=command)
        reply = _PERMISSION_DENIED_MSG
    elif command == "usage":
        reply = _USAGE
    else:
        try:
            if command == "create":
                reply = await _create(payload, community)
            elif command == "list":
                reply = await _list(community)
            elif command == "view":
                reply = await _view(str(payload["event_id"]), community)
            elif command == "remove":
                reply = await _remove(str(payload["event_id"]), community)
            else:
                reply = await _rsvp(
                    str(payload["event_id"]),
                    str(payload["choice"]),
                    envelope.event.actor,
                    community,
                )
        except _KvFailure as exc:
            await _fail(str(exc), op=str(command), provider=provider, channel_id=channel_id)

    await relay.push(provider, {"channel": channel_id, "text": reply})
    log.info("event.dispatch relayed", platform=provider, command=command)
    return DispatchResult(transport=provider, detail=str(command))
