"""`!winner` -> pick a random winner from the community's entrant list, KV-backed.

The entrant list is this bundle's own (the host scopes `kv` per app, so another bundle's
list -- e.g. `raffle`'s -- is not readable here by design). Grammar (verbs per
`waddle_sdk.command.VERBS` where one fits; `enter` is bundle-specific):

- `!winner` -- moderator/broadcaster: draw one uniformly-random entrant and announce it. The
  entrant list is left intact (`!winner reset` clears it), so a mod can re-draw.
- `!winner enter` -- anyone: join the list once (idempotent). Capped at `MAX_ENTRANTS`.
- `!winner list` -- anyone: the number of entrants (never mutates).
- `!winner reset` -- mod/broadcaster: clear all entrants.

**PII: entrants are stored and announced by UUID only.** Each entrant is the caller's
pseudonymous actor UUID (UUIDv5 of platform + id/handle -- see `_actor_uuid`), never a raw
username; the draw announces the winner's first 8 hex chars, which the winner can recognise
via their own tag (the same trade-off `raffle` documents). Logs carry only op/community-free
counts and exception-type names.

State: one `kv` key, `winner.entrants` (JSON array of UUID strings; `.`-separated key, never
`:` -- gh-631), per community via the host's kv scoping. Role gating fails CLOSED.

Fail-loud (never silent-default): a `kv` host failure or a corrupt stored list is logged
(op + exception type only) and answered with an explicit "unavailable" reply -- a corrupt
list is NOT treated as empty. Empty list on draw gets an explicit no-entrants reply.

Gated behind the PostHog flag ``waddles.command-winner``.
"""

from __future__ import annotations

import json
import random
import uuid
from typing import Any, cast

from waddle_sdk import kv, log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-winner"
COMMAND = "!winner"

ENTRANTS_KEY = "winner.entrants"
MAX_ENTRANTS = 500

_ACTOR_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://waddles.penguintech.io/identity")

_USAGE = "Usage: !winner (draw, mod only) | !winner enter | !winner list | !winner reset (mod only)"
_UNAVAILABLE_MSG = "Something went wrong accessing the entrant list - please try again."
_DENIED_MSG = "Only the broadcaster or a moderator can do that."
_NO_ENTRANTS_MSG = "No entrants yet -- viewers can join with !winner enter."


class _KvFailure(Exception):
    """Internal-only: a `kv` host call or stored-data check failed; caught in `transform`."""

    def __init__(self, op: str, error: str) -> None:
        """Record the failing op and a PII-free error classification (exception type name)."""
        super().__init__(f"{op}: {error}")
        self.op = op
        self.error = error


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


def _actor_uuid(event: PlatformEvent) -> str:
    """Stable pseudonymous UUID for the event's actor (never the raw username)."""
    author_id = event.payload.get("author_id")
    if event.platform == "discord" and isinstance(author_id, str) and author_id:
        basis = f"discord:{author_id}"
    else:
        basis = f"{event.platform}:name:{(event.actor or 'anonymous').strip().lower()}"
    return str(uuid.uuid5(_ACTOR_NAMESPACE, basis))


async def _load_entrants() -> list[str]:
    """Return the entrant UUIDs (`[]` only if never set); corrupt data raises `_KvFailure`."""
    raw = await _kv_get(ENTRANTS_KEY)
    if raw is None:
        return []
    try:
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
            raise TypeError("expected a JSON array of strings")
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise _KvFailure("corrupt_entrants", type(exc).__name__) from exc
    return cast("list[str]", data)


async def _save_entrants(entrants: list[str]) -> None:
    """Persist the entrant list."""
    await _kv_set(ENTRANTS_KEY, json.dumps(entrants).encode("utf-8"))


def _pick_winner(entrants: list[str]) -> str:
    """Pick one uniformly-random entrant. Isolated so tests can pin the choice."""
    return random.choice(entrants)  # noqa: S311 - a game, not a security decision


def _is_privileged(event: PlatformEvent) -> bool:
    """Broadcaster/moderator check -- fails CLOSED when role info isn't on the event."""
    is_mod = event.payload.get("is_mod")
    is_broadcaster = event.payload.get("is_broadcaster")
    if not isinstance(is_mod, bool) and not isinstance(is_broadcaster, bool):
        log.debug("winner.role_info_unavailable", platform=event.platform)
        return False
    return is_mod is True or is_broadcaster is True


async def _handle(rest: str, event: PlatformEvent) -> str:
    """Handle everything after `!winner`; always returns a reply."""
    verb, _, arg = rest.partition(" ")
    verb = verb.lower()
    if arg.strip():
        return _USAGE

    if verb == "enter":
        me = _actor_uuid(event)
        entrants = await _load_entrants()
        if me in entrants:
            return "You're already entered."
        if len(entrants) >= MAX_ENTRANTS:
            return f"The entrant list is full ({MAX_ENTRANTS} max)."
        entrants.append(me)
        await _save_entrants(entrants)
        log.info("winner.entered", count=len(entrants))
        return f"You're in! ({len(entrants)} entered)"

    if verb == "list":
        entrants = await _load_entrants()
        log.info("winner.list", count=len(entrants))
        return f"{len(entrants)} entrant(s) so far."

    if verb in ("", "reset"):
        if not _is_privileged(event):
            log.info("winner.permission_denied", op=verb or "draw")
            return _DENIED_MSG
        if verb == "reset":
            await _save_entrants([])
            log.info("winner.reset")
            return "Entrant list cleared."
        entrants = await _load_entrants()
        if not entrants:
            log.info("winner.draw_empty")
            return _NO_ENTRANTS_MSG
        winner = _pick_winner(entrants)
        log.info("winner.drawn", count=len(entrants))
        return f"\U0001f3c6 The winner is entrant {winner[:8]}! (out of {len(entrants)})"

    return f"Unknown !winner subcommand. {_USAGE}"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognise `!winner` and build the reply.

    Exact first-token match (so `!winners` never matches) comes before the flag check; a `kv`
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
        log.error("winner.kv_failure", op=exc.op, error=exc.error)
        reply = _UNAVAILABLE_MSG

    log.info("winner.transform matched")
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
        raise ValueError("winner reply requires a channel_id from the inbound chat.message")
    if not isinstance(text, str) or not text:
        raise ValueError("winner reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("winner.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
