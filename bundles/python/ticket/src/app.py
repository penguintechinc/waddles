"""`!ticket` -> a per-community support-ticket desk, KV-backed (v2 port).

Commands: `!ticket <description>` (create), `!ticket status <id>` (the ticket's creator or a
mod/broadcaster), `!ticket list` and `!ticket close <id>` (mod/broadcaster only). Structure
mirrors `bundles/python/count`: `transform` does all `kv` work, `dispatch` is a pure relay.

State is community-scoped via the host's `(tenant, community, app_id)` kv scoping; keys are
`.`-separated (never `:`, gh-631):

* `ticket.seq` -- atomic `kv.increment` id counter.
* `ticket.item.<id>` -- one JSON record per ticket (`desc`, `creator` UUID, `status`). Closed
  tickets are rewritten with a 30-day TTL (the host maximum) so history self-expires.
* `ticket.open` -- JSON array of `{id, summary}` for open tickets, so `list` costs ONE read
  (a per-ticket read loop would blow the host's 64 ops/invoke budget).
* `ticket.user.<uuid>` -- open ids per creator, enforcing `MAX_OPEN_PER_USER`.

**Creators are stored as pseudonymous UUIDv5s**, never usernames (see `_actor_uuid`, same
derivation as `lfg`/`label`). A non-creator, non-mod asking for `status` gets the same reply as
for a missing ticket, so ids can't be enumerated.

Abuse bounds: `MAX_OPEN` open tickets, `MAX_OPEN_PER_USER` per creator, `MAX_DESC_LEN` chars.
`list`/`close` fail CLOSED when `is_mod`/`is_broadcaster` are absent.

Logs are PII-free: op, counts, exception-type names -- never the description or an identity.
Gated behind the PostHog flag ``waddles.command-ticket``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, cast

from waddle_sdk import kv, log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-ticket"
COMMAND = "!ticket"

SEQ_KEY = "ticket.seq"
OPEN_KEY = "ticket.open"
ITEM_PREFIX = "ticket.item."
USER_PREFIX = "ticket.user."

MAX_OPEN = 100
MAX_OPEN_PER_USER = 3
MAX_DESC_LEN = 300
_SUMMARY_LEN = 40
_CLOSED_TTL_SECONDS = 30 * 24 * 60 * 60
_REPLY_BUDGET = 400

_ACTOR_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://waddles.penguintech.io/identity"
)

_USAGE = "Usage: !ticket <description> | !ticket status <id> | mods: !ticket list | !ticket close <id>"
_UNAVAILABLE_MSG = "Something went wrong accessing ticket storage - please try again."
_SUBCOMMANDS = frozenset({"status", "list", "close"})


class _KvFailure(Exception):
    """Internal-only: a `kv` host call or stored-data check failed; caught in `transform`."""

    def __init__(self, op: str, error: str) -> None:
        """Record the failing op and a PII-free error classification (exception type name)."""
        super().__init__(f"{op}: {error}")
        self.op = op
        self.error = error


@dataclass(slots=True)
class _Ticket:
    """One support ticket record."""

    id: int
    desc: str
    creator: str
    status: str


async def _kv_get(key: str) -> bytes | None:
    """`kv.get`, reclassifying a host `Err` into `_KvFailure` (fail-loud, never silent)."""
    try:
        return cast("bytes | None", await kv.get(key))
    except Exception as exc:
        raise _KvFailure("kv_get", type(getattr(exc, "value", exc)).__name__) from exc


async def _kv_set(key: str, value: bytes, ttl_seconds: int = 0) -> None:
    """`kv.set`, reclassifying a host `Err` into `_KvFailure`."""
    try:
        await kv.set(key, value, ttl_seconds=ttl_seconds)
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


def _decode(raw: bytes, op: str) -> Any:
    """JSON-decode stored bytes; corrupt data raises `_KvFailure` instead of resetting."""
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _KvFailure(op, type(exc).__name__) from exc


async def _load_open() -> list[tuple[int, str]]:
    """Return `(id, summary)` for every open ticket (`[]` if none)."""
    raw = await _kv_get(OPEN_KEY)
    if raw is None:
        return []
    data = _decode(raw, "corrupt_open")
    try:
        return [(int(e["id"]), str(e["summary"])) for e in data]
    except (TypeError, KeyError, ValueError) as exc:
        raise _KvFailure("corrupt_open", type(exc).__name__) from exc


async def _save_open(entries: list[tuple[int, str]]) -> None:
    """Persist the open-ticket index."""
    payload = [{"id": i, "summary": s} for i, s in entries]
    await _kv_set(OPEN_KEY, json.dumps(payload).encode("utf-8"))


async def _load_user_open(creator: str) -> list[int]:
    """Return a creator's open ticket ids (`[]` if none)."""
    raw = await _kv_get(f"{USER_PREFIX}{creator}")
    if raw is None:
        return []
    data = _decode(raw, "corrupt_user")
    try:
        return [int(i) for i in data]
    except (TypeError, ValueError) as exc:
        raise _KvFailure("corrupt_user", type(exc).__name__) from exc


async def _save_user_open(creator: str, ids: list[int]) -> None:
    """Persist a creator's open ticket ids."""
    await _kv_set(f"{USER_PREFIX}{creator}", json.dumps(ids).encode("utf-8"))


async def _load_ticket(ticket_id: int) -> _Ticket | None:
    """Return one ticket record, or `None` if it never existed / has expired."""
    raw = await _kv_get(f"{ITEM_PREFIX}{ticket_id}")
    if raw is None:
        return None
    data = _decode(raw, "corrupt_item")
    try:
        return _Ticket(
            id=int(data["id"]),
            desc=str(data["desc"]),
            creator=str(data["creator"]),
            status=str(data["status"]),
        )
    except (TypeError, KeyError, ValueError) as exc:
        raise _KvFailure("corrupt_item", type(exc).__name__) from exc


async def _save_ticket(ticket: _Ticket, ttl_seconds: int = 0) -> None:
    """Persist one ticket record."""
    payload = {
        "id": ticket.id,
        "desc": ticket.desc,
        "creator": ticket.creator,
        "status": ticket.status,
    }
    await _kv_set(
        f"{ITEM_PREFIX}{ticket.id}", json.dumps(payload).encode("utf-8"), ttl_seconds
    )


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
        log.debug("ticket.role_info_unavailable", platform=event.platform)
        return False
    return bool(is_mod) or bool(is_broadcaster)


def validate_desc(raw: str) -> str | None:
    """Return an error message, or `None` when `raw` is an acceptable ticket description."""
    if not raw:
        return "a description is required"
    if len(raw) > MAX_DESC_LEN:
        return f"descriptions must be {MAX_DESC_LEN} characters or fewer"
    if any(ord(ch) < 32 for ch in raw):
        return "descriptions can't contain control characters"
    return None


def _parse_id(arg: str) -> int | None:
    """Parse a ticket id argument (`7` or `#7`); `None` means show usage."""
    token = arg.strip().lstrip("#")
    return int(token) if token.isdigit() else None


async def _create(desc: str, event: PlatformEvent) -> str:
    """Create a ticket for the caller, enforcing the global and per-user open caps."""
    error = validate_desc(desc)
    if error:
        return f"Can't create ticket: {error}. {_USAGE}"
    me = _actor_uuid(event)
    open_entries = await _load_open()
    if len(open_entries) >= MAX_OPEN:
        return "The ticket queue is full right now - please try again later."
    mine = await _load_user_open(me)
    if len(mine) >= MAX_OPEN_PER_USER:
        return f"You already have {MAX_OPEN_PER_USER} open tickets - wait for a mod to close one."
    new_id = await _kv_increment(SEQ_KEY, 1)
    await _save_ticket(_Ticket(id=new_id, desc=desc, creator=me, status="open"))
    open_entries.append((new_id, desc[:_SUMMARY_LEN]))
    await _save_open(open_entries)
    mine.append(new_id)
    await _save_user_open(me, mine)
    log.info("ticket.created", open=len(open_entries))
    return f"Ticket #{new_id} created. Check it with !ticket status {new_id}."


async def _status(arg: str, event: PlatformEvent) -> str:
    """Report a ticket's status to its creator or a mod; others get a generic not-found."""
    target = _parse_id(arg)
    if target is None:
        return "Usage: !ticket status <id>"
    ticket = await _load_ticket(target)
    if ticket is None or (
        ticket.creator != _actor_uuid(event) and not _is_privileged(event)
    ):
        return f"No ticket #{target} found for you."
    return f"Ticket #{target}: {ticket.status}."


async def _list_open(event: PlatformEvent) -> str:
    """List open tickets (mods only)."""
    if not _is_privileged(event):
        log.info("ticket.permission_denied", op="list")
        return "Only the broadcaster or a moderator can list tickets."
    entries = await _load_open()
    log.info("ticket.list", open=len(entries))
    if not entries:
        return "No open tickets."
    parts: list[str] = []
    used = 0
    for shown, (tid, summary) in enumerate(entries):
        piece = f"#{tid} {summary}"
        if used + len(piece) + 3 > _REPLY_BUDGET and shown > 0:
            parts.append(f"... and {len(entries) - shown} more")
            break
        parts.append(piece)
        used += len(piece) + 3
    return f"Open tickets ({len(entries)}): " + " | ".join(parts)


async def _close(arg: str, event: PlatformEvent) -> str:
    """Close a ticket (mods only), clearing it from the open and per-user indexes."""
    if not _is_privileged(event):
        log.info("ticket.permission_denied", op="close")
        return "Only the broadcaster or a moderator can close tickets."
    target = _parse_id(arg)
    if target is None:
        return "Usage: !ticket close <id>"
    ticket = await _load_ticket(target)
    if ticket is None:
        return f"No ticket #{target}."
    if ticket.status == "closed":
        return f"Ticket #{target} is already closed."
    ticket.status = "closed"
    await _save_ticket(ticket, _CLOSED_TTL_SECONDS)
    entries = [e for e in await _load_open() if e[0] != target]
    await _save_open(entries)
    mine = [i for i in await _load_user_open(ticket.creator) if i != target]
    await _save_user_open(ticket.creator, mine)
    log.info("ticket.closed", open=len(entries))
    return f"Closed ticket #{target}."


async def _handle(rest: str, event: PlatformEvent) -> str:
    """Handle everything after `!ticket`; always returns a reply."""
    if not rest:
        return _USAGE
    verb, _, arg = rest.partition(" ")
    verb = verb.lower()
    arg = arg.strip()
    if verb not in _SUBCOMMANDS:
        return await _create(rest, event)
    if verb == "status":
        return await _status(arg, event)
    if verb == "list":
        return await _list_open(event)
    return await _close(arg, event)


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognise `!ticket` and build the reply.

    Exact first-token match (so `!tickets` never matches) comes before the flag check; a `kv`
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
        log.error("ticket.kv_failure", op=exc.op, error=exc.error)
        reply = _UNAVAILABLE_MSG

    log.info("ticket.transform matched")
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
            "ticket reply requires a channel_id from the inbound chat.message"
        )
    if not isinstance(text, str) or not text:
        raise ValueError("ticket reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("ticket.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
