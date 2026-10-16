"""`!lastseen [@mention]` -> when a user was last active in this community.

Every chat message from an identifiable user stamps `lastseen.user.<uuid>` with the event
timestamp (RFC 3339, 90-day TTL so storage stays bounded -- anyone unseen for longer reads
as "no activity recorded"). `!lastseen` reports the caller's previous stamp; `!lastseen
<mention>` reports the mentioned user's. The command message itself is stamped AFTER the
read, so `!lastseen` never reports "just now" for the caller.

**Identity is the pipeline's `{user:<id>}` PII token, never a name or a locally derived id.**
`pii_tokenize` puts the token in `event.actor` and substitutes it for @mentions; this bundle
parses the bare id out and uses it as the kv key, and EMITS `{user:<id>}` in replies so the
egress detokenizer renders the display name. No uuid5 / platform-id hashing. Lookup targets:

* self (no argument) -- the token in `event.actor`;
* an @mention -- already a `{user:<id>}` token by the time the bundle sees it.

A non-token actor (tokenization off, DM without actor) is not tracked and gets an explicit
"couldn't identify you" reply; a non-token target gets the "mention them" reply -- never a
hashed guess.

kv keys use `.` never `:` (gh-631); the host scopes them per `(tenant, community,
app_id)`, so activity never leaks between communities. kv cost: one `set` per identifiable
chat message, plus one `get` for a `!lastseen` read.

Logging is PII-free: op names, platform and exception type names only. A `kv` failure on
the command path is logged (`lastseen.kv_failure`) AND answered explicitly; on the passive
recording path (no reply is possible) it is logged at ERROR and the message is otherwise
unaffected -- never swallowed silently.

Activation-gated only -- no feature flag: the platform's installed -> enabled -> activated check
decides whether this bundle runs.
"""

from __future__ import annotations

import re
from typing import Any, cast

from waddle_sdk import clock, kv, log, relay
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

COMMAND = "!lastseen"
KEY_PREFIX = "lastseen.user."

#: Stamps expire after 90 days -- bounds storage; unseen-for-longer reads as no record.
TTL_SECONDS = 90 * 24 * 60 * 60

_USER_TOKEN_RE = re.compile(r"^\{user:([A-Za-z0-9_:.-]+)\}$")


def _parse_user_token(raw: object) -> str | None:
    """Extract the bare id from a `{user:<id>}` PII token, or `None` if `raw` isn't one.

    Grammar matches `core/egress_detokenizer`'s `{user:` + token chars (alphanumeric and
    `-_:.`) + `}`. The pipeline's `pii_tokenize` pass puts this token in `event.actor` and in
    any @mention; hub-api resolves it, so the bundle never derives its own identity.
    """
    if not isinstance(raw, str):
        return None
    match = _USER_TOKEN_RE.match(raw.strip())
    return match.group(1) if match else None


def _user_token(user_id: str) -> str:
    """Render a bare id as the `{user:<id>}` placeholder the egress detokenizer resolves."""
    return f"{{user:{user_id}}}"


_USAGE = "Usage: !lastseen [@mention]"
_NO_NAME_LOOKUP = "I couldn't identify that user - mention them (e.g. @name)."
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


def _key(user: str) -> str:
    """The `kv` key one user's last-seen stamp lives under."""
    # kv keys can't contain ':' (host-reserved, gh-631); handle-style tokens carry them.
    return f"{KEY_PREFIX}{user.replace(':', '.')}"


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
        target = _parse_user_token(args[0])
        if target is None:
            log.info("lastseen.name_lookup_unsupported", platform=event.platform)
            return _NO_NAME_LOOKUP
        who = f"user {_user_token(target)}"
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
        return f"User {_user_token(target)} was last seen {stamp}."
    return f"You were last seen {stamp}."


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: stamp activity and answer `!lastseen`.

    Returns `None` for non-chat payloads, and every non-command message
    (which is still stamped as a side effect). Command-path `kv` failures are logged and
    answered explicitly; passive-stamp failures are logged at ERROR (no reply possible).
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None

    actor = _parse_user_token(event.actor)
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
