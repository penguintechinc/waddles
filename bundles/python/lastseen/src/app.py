"""`!lastseen [@mention]` -> when a user was last active in this community.

Every chat message from an identifiable user stamps `lastseen.user.<uuid>` with the event
timestamp (RFC 3339, 90-day TTL so storage stays bounded -- anyone unseen for longer reads
as "no activity recorded"). `!lastseen` reports the caller's previous stamp; `!lastseen
<mention>` reports the mentioned user's. The command message itself is stamped AFTER the
read, so `!lastseen` never reports "just now" for the caller.

**Identity is by UUID, never by free-text username (PII rule).** The stable platform user
id on the event (`payload.user_id` / `author_id`) is hashed with the platform into a
UUIDv5 (same namespace as `iq`); the raw id is discarded and never stored, logged or
echoed. Supported lookup targets are:

* self (no argument) -- the event's own platform user id;
* a Discord mention `<@id>` / `<@!id>` (Discord only) -- the id inside the mention;
* a literal UUID (as printed by this bundle's replies).

**Known limitation (documented, not hidden):** resolving a free-text name (Twitch `@name`,
a bare handle) to an identity needs the tokenization pipeline (#429), which has not
landed. Such targets get an explicit "can't look up by name yet" reply -- never a hashed
guess and never a silent default. Replies identify other users by the first 8 hex chars
of their UUID, not a name.

kv keys use `.` never `:` (gh-631); the host scopes them per `(tenant, community,
app_id)`, so activity never leaks between communities. kv cost: one `set` per identifiable
chat message, plus one `get` for a `!lastseen` read.

Logging is PII-free: op names, platform and exception type names only. A `kv` failure on
the command path is logged (`lastseen.kv_failure`) AND answered explicitly; on the passive
recording path (no reply is possible) it is logged at ERROR and the message is otherwise
unaffected -- never swallowed silently.

Gated behind the PostHog flag ``waddles.command-lastseen``.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, cast

from waddle_sdk import clock, kv, log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-lastseen"
COMMAND = "!lastseen"
KEY_PREFIX = "lastseen.user."

#: Stamps expire after 90 days -- bounds storage; unseen-for-longer reads as no record.
TTL_SECONDS = 90 * 24 * 60 * 60

_DISCORD_MENTION_RE = re.compile(r"^<@!?(\d{1,32})>$")
_PLATFORM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ACTOR_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://waddles.penguintech.io/identity")

_USAGE = "Usage: !lastseen [@mention]"
_NO_NAME_LOOKUP = (
    "I can't look users up by name yet (needs identity tokenization, #429) - "
    "mention them with a Discord @mention or give their UUID."
)
_NO_SELF_ID = "I couldn't identify you on this platform, so I can't track or look up activity."


class _KvFailure(Exception):
    """Internal-only: a `kv` host call failed. Always caught in `transform`, never leaked."""


async def _kv_get(key: str) -> bytes | None:
    """`kv.get`, reclassifying the generated WIT `Err` into `_KvFailure` (fail-loud)."""
    try:
        result = await kv.get(key)
    except Exception as exc:  # noqa: BLE001 - classified like waddle_sdk.db/http
        raise _KvFailure("kv.get failed") from exc
    return cast("bytes | None", result)


async def _kv_set(key: str, value: bytes) -> None:
    """`kv.set` with the 90-day TTL, reclassifying `Err` into `_KvFailure`."""
    try:
        await kv.set(key, value, ttl_seconds=TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        raise _KvFailure("kv.set failed") from exc


def _user_uuid(platform: str, platform_user_id: str) -> str:
    """Pseudonymous UUIDv5 for a platform user id; the raw id is never kept."""
    return str(uuid.uuid5(_ACTOR_NAMESPACE, f"{platform}:{platform_user_id}"))


def _actor_uuid(event: PlatformEvent) -> str | None:
    """UUID of the event's own author from its platform user id, or `None` if absent/invalid."""
    for field in ("user_id", "author_id"):
        value = event.payload.get(field)
        if isinstance(value, str) and _PLATFORM_ID_RE.match(value):
            return _user_uuid(event.platform, value)
    return None


def _resolve_target(raw: str, platform: str) -> str | None:
    """Resolve a typed target to a UUID: Discord mention (Discord only) or literal UUID.

    Free-text names are NOT resolved (see module docstring) -- returns `None`.
    """
    token = raw.strip()
    mention = _DISCORD_MENTION_RE.match(token)
    if mention and platform == "discord":
        return _user_uuid("discord", mention.group(1))
    try:
        return str(uuid.UUID(token))
    except ValueError:
        return None


def _key(user: str) -> str:
    """The `kv` key one user's last-seen stamp lives under."""
    return f"{KEY_PREFIX}{user}"


def _event_time(event: PlatformEvent) -> str:
    """The stamp to record: the event's own timestamp, else the host clock."""
    if event.occurred_at:
        return str(event.occurred_at)
    return str(clock.now_rfc3339())


async def _read_stamp(user: str) -> str | None:
    """Return a user's stored stamp, or `None` if there's no record.

    Raises:
        _KvFailure: kv failed or the stored bytes aren't UTF-8 (corrupt, fail-loud).
    """
    raw = await _kv_get(_key(user))
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _KvFailure("corrupt lastseen stamp") from exc


async def _record(event: PlatformEvent, actor: str) -> None:
    """Stamp the author as active now (the event's timestamp)."""
    await _kv_set(_key(actor), _event_time(event).encode("utf-8"))


def _log_kv_failure(exc: _KvFailure, *, op: str) -> None:
    """Log a kv failure with the exception TYPE only (never keys, ids or text)."""
    cause = exc.__cause__
    log.error(
        "lastseen.kv_failure",
        op=op,
        error_type=type(cause).__name__ if cause else type(exc).__name__,
    )


async def _answer(args: list[str], event: PlatformEvent, actor: str | None) -> str:
    """Build the `!lastseen` reply (reads only; the caller stamps afterwards)."""
    if len(args) > 1:
        return _USAGE
    if args:
        target = _resolve_target(args[0], event.platform)
        if target is None:
            log.info("lastseen.name_lookup_unsupported", platform=event.platform)
            return _NO_NAME_LOOKUP
        who = f"user {target[:8]}"
    else:
        if actor is None:
            log.info("lastseen.no_self_identity", platform=event.platform)
            return _NO_SELF_ID
        target = actor
        who = "you"
    stamp = await _read_stamp(target)
    if stamp is None:
        return f"No activity recorded for {who} yet."
    if args:
        return f"User {target[:8]} was last seen {stamp}."
    return f"You were last seen {stamp}."


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: stamp activity and answer `!lastseen`.

    Returns `None` for non-chat payloads, a disabled flag, and every non-command message
    (which is still stamped as a side effect). Command-path `kv` failures are logged and
    answered explicitly; passive-stamp failures are logged at ERROR (no reply possible).
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    actor = _actor_uuid(event)
    parts = text.strip().split()
    is_command = bool(parts) and parts[0].lower() == COMMAND

    reply: str | None = None
    if is_command:
        try:
            reply = await _answer(parts[1:], event, actor)
        except _KvFailure as exc:
            _log_kv_failure(exc, op="read")
            reply = "Last-seen storage is unavailable right now - please try again."

    if actor is not None:
        try:
            await _record(event, actor)
        except _KvFailure as exc:
            _log_kv_failure(exc, op="record")
            if reply is None:
                return None

    if reply is None:
        return None

    log.info("lastseen.transform matched", op="read")
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
        ValueError: The payload is missing `channel_id` or `text` (defensive).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    text = payload.get("text")
    if not channel_id:
        raise ValueError("lastseen reply requires a channel_id from the inbound chat.message")
    if not isinstance(text, str) or not text:
        raise ValueError("lastseen reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("lastseen.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
