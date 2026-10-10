"""`!birthday` -> per-user birthdays keyed by USER UUID, KV-backed (v2 port).

Commands: `!birthday` shows the caller's birthday; `!birthday set MM-DD` stores it (the caller's
own, anyone); `!birthday remove` deletes the caller's; `!birthday today` lists today's
birthdays. No mod verbs: users manage only their own record.

**PII rule: a birthday is PII, so users are referenced by UUID, never username.** `_actor_uuid()`
derives a UUIDv5 from the platform id (Discord `author_id`) or, for handle-only platforms
(Twitch), from the lower-cased handle -- the same derivation `bundles/python/label` uses -- and
DISCARDS the handle. Only the UUID is stored, logged or echoed. Until the identity-tokenization
pipeline (#429) supplies real `hub_users` UUIDs this is a non-reversible pseudonym. Only
month-day is stored (no year) and `02-29` is accepted. `!birthday today` therefore lists the
8-hex-char short form of each UUID plus a marker for the caller -- resolving UUIDs to display
names is a hub-side concern (gap: needs hub identity tokenization).

State is community-scoped via the host's `(tenant, community, app_id)` kv scoping. Keys are
`.`-separated (never `:`, gh-631): `birthday.user.<uuid>` -> `MM-DD`; `birthday.day.<MM-DD>` ->
JSON array of UUIDs (the index `today` reads, as `kv` has no list/scan). "Today" is the event's
own `occurred_at` (UTC); an unparseable timestamp fails LOUD (ERROR log + error reply).

Logs are PII-free: op, counts, exception-type names -- never a date, UUID or username. Corrupt
stored data raises `_KvFailure`, never a silent default. Gated behind the PostHog flag
``waddles.command-birthday``.
"""

from __future__ import annotations

import calendar
import datetime
import json
import uuid
from typing import Any, cast

from waddle_sdk import kv, log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-birthday"
COMMAND = "!birthday"

USER_PREFIX = "birthday.user."
DAY_PREFIX = "birthday.day."
#: Cap on UUIDs indexed under one day so the blob stays far below the 64 KiB value limit.
MAX_PER_DAY = 200
_SHOWN_TODAY = 20

_ACTOR_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://waddles.penguintech.io/identity")

_USAGE = "Usage: !birthday | !birthday set MM-DD | !birthday remove | !birthday today"
_UNAVAILABLE_MSG = "Something went wrong accessing birthday storage - please try again."


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


async def _kv_delete(key: str) -> None:
    """`kv.delete`, reclassifying a host `Err` into `_KvFailure`."""
    try:
        await kv.delete(key)
    except Exception as exc:
        raise _KvFailure("kv_delete", type(getattr(exc, "value", exc)).__name__) from exc


def _is_privileged(event: PlatformEvent) -> bool:
    """Broadcaster/moderator check -- fails CLOSED when role info isn't on the event."""
    is_mod = event.payload.get("is_mod")
    is_broadcaster = event.payload.get("is_broadcaster")
    if not isinstance(is_mod, bool) and not isinstance(is_broadcaster, bool):
        log.debug("birthday.role_info_unavailable", platform=event.platform)
        return False
    return bool(is_mod) or bool(is_broadcaster)


def _actor_uuid(event: PlatformEvent) -> str:
    """Stable pseudonymous UUID for the event's actor (same derivation as `label`)."""
    author_id = event.payload.get("author_id")
    if event.platform == "discord" and isinstance(author_id, str) and author_id:
        basis = f"discord:{author_id}"
    else:
        basis = f"{event.platform}:name:{(event.actor or 'anonymous').strip().lower()}"
    return str(uuid.uuid5(_ACTOR_NAMESPACE, basis))


def parse_month_day(raw: str) -> str | None:
    """Return the canonical zero-padded `MM-DD`, or `None` if `raw` is not a real calendar day."""
    month_s, sep, day_s = raw.strip().partition("-")
    if not sep or not month_s.isdigit() or not day_s.isdigit():
        return None
    if len(month_s) > 2 or len(day_s) > 2:
        return None
    month, day = int(month_s), int(day_s)
    if not 1 <= month <= 12:
        return None
    if not 1 <= day <= calendar.monthrange(2000, month)[1]:  # 2000: a leap year, allows 02-29
        return None
    return f"{month:02d}-{day:02d}"


def _today(event: PlatformEvent) -> str:
    """Return today's `MM-DD` (UTC) from the event timestamp; an unparseable one fails loud."""
    try:
        stamp = datetime.datetime.fromisoformat(event.occurred_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise _KvFailure("bad_event_time", type(exc).__name__) from exc
    return f"{stamp.month:02d}-{stamp.day:02d}"


async def _load_day(day: str) -> list[str]:
    """Return the UUIDs indexed under `day` (`[]` if none); corrupt data raises `_KvFailure`."""
    raw = await _kv_get(f"{DAY_PREFIX}{day}")
    if raw is None:
        return []
    try:
        return [str(u) for u in list(json.loads(raw.decode("utf-8")))]
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise _KvFailure("corrupt_day_index", type(exc).__name__) from exc


async def _save_day(day: str, uuids: list[str]) -> None:
    """Persist (or, when empty, delete) one day's UUID index."""
    key = f"{DAY_PREFIX}{day}"
    if uuids:
        await _kv_set(key, json.dumps(uuids).encode("utf-8"))
    else:
        await _kv_delete(key)


async def _load_user(user_uuid: str) -> str | None:
    """Return the user's stored `MM-DD`, or `None`; a malformed value raises `_KvFailure`."""
    raw = await _kv_get(f"{USER_PREFIX}{user_uuid}")
    if raw is None:
        return None
    try:
        stored = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _KvFailure("corrupt_user_record", type(exc).__name__) from exc
    if parse_month_day(stored) != stored:
        raise _KvFailure("corrupt_user_record", "InvalidDate")
    return stored


async def _drop_from_index(day: str, user_uuid: str) -> None:
    """Remove `user_uuid` from `day`'s index."""
    uuids = await _load_day(day)
    if user_uuid in uuids:
        await _save_day(day, [u for u in uuids if u != user_uuid])


async def _handle(rest: str, event: PlatformEvent) -> str:
    """Handle everything after `!birthday`; always returns a reply."""
    verb, _, arg = rest.partition(" ")
    verb = verb.lower()
    arg = arg.strip()
    me = _actor_uuid(event)

    if verb == "":
        stored = await _load_user(me)
        log.info("birthday.show", has_birthday=stored is not None)
        if stored is None:
            return "You haven't set a birthday. Use !birthday set MM-DD."
        return f"Your birthday is {stored}."

    if verb == "set":
        day = parse_month_day(arg)
        if day is None:
            return "Usage: !birthday set MM-DD (a real calendar date, e.g. 03-14)"
        previous = await _load_user(me)
        uuids = await _load_day(day)
        if me not in uuids and len(uuids) >= MAX_PER_DAY:
            return "Too many birthdays are registered for that day."
        if previous is not None and previous != day:
            await _drop_from_index(previous, me)
        if me not in uuids:
            uuids.append(me)
            await _save_day(day, uuids)
        await _kv_set(f"{USER_PREFIX}{me}", day.encode("utf-8"))
        log.info("birthday.set", changed=previous is not None and previous != day)
        return f"Saved your birthday as {day}."

    if verb == "remove":
        previous = await _load_user(me)
        if previous is None:
            return "You don't have a birthday set."
        await _drop_from_index(previous, me)
        await _kv_delete(f"{USER_PREFIX}{me}")
        log.info("birthday.removed")
        return "Removed your birthday."

    if verb == "today":
        uuids = await _load_day(_today(event))
        log.info("birthday.today", count=len(uuids))
        if not uuids:
            return "No birthdays today."
        shown = ", ".join(u[:8] + (" (you!)" if u == me else "") for u in uuids[:_SHOWN_TODAY])
        extra = len(uuids) - _SHOWN_TODAY
        suffix = f" ... and {extra} more" if extra > 0 else ""
        return f"Birthdays today ({len(uuids)}): {shown}{suffix}"

    return f"Unknown !birthday subcommand. {_USAGE}"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognise `!birthday` and build the reply.

    Cheap-skip (exact first-token match) comes before the flag check; a `kv` failure is logged
    loudly (PII-free) and answered with an error reply, never swallowed into a default.
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
        log.error("birthday.kv_failure", op=exc.op, error=exc.error)
        reply = _UNAVAILABLE_MSG

    log.info("birthday.transform matched")
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
        raise ValueError("birthday reply requires a channel_id from the inbound chat.message")
    if not isinstance(text, str) or not text:
        raise ValueError("birthday reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("birthday.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
