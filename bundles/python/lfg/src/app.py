"""`!lfg` -> looking-for-group posts per community, KV-backed (v2 port).

Commands: `!lfg create <desc>`, `!lfg list`, `!lfg join <id>`, `!lfg leave <id>`,
`!lfg remove <id>` (the group's owner or a mod/broadcaster). Structure mirrors
`bundles/python/count`: `transform` does all `kv` work, `dispatch` is a pure relay.

State is community-scoped via the host's `(tenant, community, app_id)` kv scoping. Keys are
`.`-separated (never `:`, gh-631): `lfg.groups` (one JSON array of groups, capped so the blob
stays far below the host's 64 KiB value limit) and `lfg.seq` (atomic `kv.increment` id counter).

**Members are stored as pseudonymous UUIDs, never usernames.** A chat actor is a raw platform
string; `_actor_uuid()` derives a stable UUIDv5 from `(platform, platform user id)` (Discord
`author_id`) or `(platform, lowercased actor)` and only that UUID is persisted. Until the
identity-tokenization pipeline (#429) supplies real `hub_users` UUIDs this is the non-reversible
stand-in; `list` shows member counts, never identities.

Abuse bounds: `MAX_GROUPS` open groups per community, one group per owner, `MAX_MEMBERS` per
group, `MAX_DESC_LEN` characters per description. When the owner leaves, or the last member
leaves, the group is disbanded.

Logs are PII-free: op, counts, exception-type names -- never the description or an identity.
Gated behind the PostHog flag ``waddles.command-lfg``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, cast

from waddle_sdk import kv, log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-lfg"
COMMAND = "!lfg"

GROUPS_KEY = "lfg.groups"
SEQ_KEY = "lfg.seq"

MAX_GROUPS = 25
MAX_MEMBERS = 20
MAX_DESC_LEN = 200
_REPLY_BUDGET = 400

#: Fixed UUIDv5 namespace for actor pseudonyms (shared derivation with `label`'s scheme).
_ACTOR_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://waddles.penguintech.io/identity"
)

_USAGE = (
    "Usage: !lfg create <description> | !lfg list | !lfg join <id> | "
    "!lfg leave <id> | !lfg remove <id>"
)
_UNAVAILABLE_MSG = "Something went wrong accessing LFG storage - please try again."


class _KvFailure(Exception):
    """Internal-only: a `kv` host call or stored-data check failed; caught in `transform`."""

    def __init__(self, op: str, error: str) -> None:
        """Record the failing op and a PII-free error classification (exception type name)."""
        super().__init__(f"{op}: {error}")
        self.op = op
        self.error = error


@dataclass(slots=True)
class _Group:
    """One open LFG group: short id, description, owner and member UUIDs."""

    id: int
    desc: str
    owner: str
    members: list[str] = field(default_factory=list)


async def _kv_get(key: str) -> bytes | None:
    """`kv.get`, reclassifying a host `Err` into `_KvFailure` (fail-loud, never silent)."""
    try:
        return cast("bytes | None", await kv.get(key))
    except Exception as exc:
        raise _KvFailure("kv_get", type(getattr(exc, "value", exc)).__name__) from exc


async def _kv_set(key: str, value: bytes) -> None:
    """`kv.set` (no TTL), reclassifying a host `Err` into `_KvFailure`."""
    try:
        await kv.set(key, value, ttl_seconds=0)
    except Exception as exc:
        raise _KvFailure("kv_set", type(getattr(exc, "value", exc)).__name__) from exc


async def _kv_increment(key: str, delta: int) -> int:
    """`kv.increment` (no TTL), reclassifying a host `Err` into `_KvFailure`."""
    try:
        return cast(int, await kv.increment(key, delta, ttl_seconds=0))
    except Exception as exc:
        raise _KvFailure(
            "kv_increment", type(getattr(exc, "value", exc)).__name__
        ) from exc


async def _load() -> list[_Group]:
    """Return open groups (`[]` if none); corrupt stored data raises `_KvFailure`."""
    raw = await _kv_get(GROUPS_KEY)
    if raw is None:
        return []
    try:
        data = json.loads(raw.decode("utf-8"))
        return [
            _Group(
                id=int(g["id"]),
                desc=str(g["desc"]),
                owner=str(g["owner"]),
                members=[str(m) for m in g["members"]],
            )
            for g in data
        ]
    except (UnicodeDecodeError, ValueError, TypeError, KeyError) as exc:
        raise _KvFailure("corrupt_groups", type(exc).__name__) from exc


async def _save(groups: list[_Group]) -> None:
    """Persist the full group list."""
    payload = [
        {"id": g.id, "desc": g.desc, "owner": g.owner, "members": g.members}
        for g in groups
    ]
    await _kv_set(GROUPS_KEY, json.dumps(payload).encode("utf-8"))


def _actor_uuid(event: PlatformEvent) -> str:
    """Stable pseudonymous UUID for the event's actor -- the only identity this bundle stores."""
    author_id = event.payload.get("author_id")
    if event.platform == "discord" and isinstance(author_id, str) and author_id:
        basis = f"discord:{author_id}"
    else:
        basis = f"{event.platform}:name:{(event.actor or 'anonymous').strip().lower()}"
    return str(uuid.uuid5(_ACTOR_NAMESPACE, basis))


def _is_privileged(event: PlatformEvent) -> bool:
    """Broadcaster/moderator check -- fails CLOSED when role info isn't on the event."""
    is_mod = event.payload.get("is_mod")
    is_broadcaster = event.payload.get("is_broadcaster")
    if not isinstance(is_mod, bool) and not isinstance(is_broadcaster, bool):
        log.debug("lfg.role_info_unavailable", platform=event.platform)
        return False
    return bool(is_mod) or bool(is_broadcaster)


def validate_desc(raw: str) -> str | None:
    """Return an error message, or `None` when `raw` is an acceptable group description."""
    if not raw:
        return "a description is required, e.g. `!lfg create need 2 for raid tonight`"
    if len(raw) > MAX_DESC_LEN:
        return f"descriptions must be {MAX_DESC_LEN} characters or fewer"
    if any(ord(ch) < 32 for ch in raw):
        return "descriptions can't contain control characters"
    return None


def _parse_id(arg: str, verb: str) -> int | None:
    """Parse a group id argument; `None` means the usage reply should be shown."""
    token = arg.strip().lstrip("#")
    if not token.isdigit():
        log.debug("lfg.bad_id", op=verb)
        return None
    return int(token)


def _render(groups: list[_Group]) -> str:
    """Format open groups within the chat reply budget."""
    parts: list[str] = []
    used = 0
    for shown, g in enumerate(groups):
        piece = f"#{g.id} {g.desc} ({len(g.members)}/{MAX_MEMBERS})"
        if used + len(piece) + 3 > _REPLY_BUDGET and shown > 0:
            parts.append(f"... and {len(groups) - shown} more")
            break
        parts.append(piece)
        used += len(piece) + 3
    return f"Open groups ({len(groups)}): " + " | ".join(parts)


async def _handle(rest: str, event: PlatformEvent) -> str:
    """Handle everything after `!lfg`; always returns a reply."""
    verb, _, arg = rest.partition(" ")
    verb = verb.lower()
    arg = arg.strip()
    if not verb:
        return _USAGE

    if verb == "list":
        groups = await _load()
        log.info("lfg.list", count=len(groups))
        return (
            "No open groups right now - start one with !lfg create."
            if not groups
            else _render(groups)
        )

    me = _actor_uuid(event)

    if verb == "create":
        error = validate_desc(arg)
        if error:
            return f"Can't create: {error}."
        groups = await _load()
        if any(g.owner == me for g in groups):
            return "You already have an open group - use !lfg remove <id> first."
        if len(groups) >= MAX_GROUPS:
            return f"Too many open groups ({MAX_GROUPS}); wait for one to close."
        new_id = await _kv_increment(SEQ_KEY, 1)
        groups.append(_Group(id=new_id, desc=arg, owner=me, members=[me]))
        await _save(groups)
        log.info("lfg.created", count=len(groups))
        return f"Created group #{new_id}. Others can join with !lfg join {new_id}."

    if verb in ("join", "leave", "remove"):
        target = _parse_id(arg, verb)
        if target is None:
            return f"Usage: !lfg {verb} <id>"
        groups = await _load()
        group = next((g for g in groups if g.id == target), None)
        if group is None:
            return f"No open group #{target}."

        if verb == "join":
            if me in group.members:
                return f"You're already in group #{target}."
            if len(group.members) >= MAX_MEMBERS:
                return f"Group #{target} is full."
            group.members.append(me)
            await _save(groups)
            log.info("lfg.joined", count=len(group.members))
            return f"Joined group #{target} ({len(group.members)}/{MAX_MEMBERS})."

        if verb == "leave":
            if me not in group.members:
                return f"You're not in group #{target}."
            group.members.remove(me)
            disbanded = me == group.owner or not group.members
            if disbanded:
                groups = [g for g in groups if g.id != target]
            await _save(groups)
            log.info("lfg.left", disbanded=disbanded, count=len(groups))
            return (
                f"Group #{target} disbanded." if disbanded else f"Left group #{target}."
            )

        if me != group.owner and not _is_privileged(event):
            log.info("lfg.permission_denied", op="remove")
            return "Only the group's owner or a moderator can remove it."
        groups = [g for g in groups if g.id != target]
        await _save(groups)
        log.info("lfg.removed", count=len(groups))
        return f"Removed group #{target}."

    return f"Unknown !lfg subcommand. {_USAGE}"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognise `!lfg` and build the reply.

    Exact first-token match (so `!lfgx` never matches) comes before the flag check; a `kv`
    failure is logged loudly (PII-free) and answered with an error reply, never swallowed.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    head, _, rest = text.strip().partition(" ")
    if head.lower() != COMMAND:
        return None
    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    try:
        reply = await _handle(rest.strip(), event)
    except _KvFailure as exc:
        log.error("lfg.kv_failure", op=exc.op, error=exc.error)
        reply = _UNAVAILABLE_MSG

    log.info("lfg.transform matched")
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
        ValueError: The payload is missing `channel_id` or `text` (defensive -- `transform`
            always sets both).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    text = payload.get("text")
    if not channel_id:
        raise ValueError(
            "lfg reply requires a channel_id from the inbound chat.message"
        )
    if not isinstance(text, str) or not text:
        raise ValueError("lfg reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("lfg.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
