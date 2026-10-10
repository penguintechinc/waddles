"""`!death` -> a streamer death counter, community-scoped, KV-backed (PTB port).

Grammar (verbs per `waddle_sdk.command.VERBS`, bare = read for non-mods):

- `!death` -- moderator/broadcaster: **+1** and report the new total (the streamer's
  "I died again" command, kept mod-gated so chat cannot inflate it); anyone else: read the
  current total.
- `!death list` -- anyone: read the current total (never mutates).
- `!death add` / `!death sub` -- mod/broadcaster: +1 / -1 (never below 0).
- `!death set <n>` -- mod/broadcaster: set the total (`0..MAX_DEATHS`).
- `!death reset` -- mod/broadcaster: back to 0.

Role gating fails CLOSED when the event carries no role info. State is one `kv` key,
`death.count` (`.`-separated, never `:` -- gh-631), scoped per community by the host's
`(tenant, community, app_id)` kv scoping. No user data is stored or referenced anywhere.

Fail-loud (never silent-default): a `kv` host failure or a corrupt stored value is logged
(op + exception-type name only -- never user text) and answered with an explicit
"unavailable" reply; a corrupt counter is NOT treated as 0. Invalid input (`set abc`,
out-of-range, unknown verb) gets an explicit usage/error reply.

Gated behind the PostHog flag ``waddles.command-death``.
"""

from __future__ import annotations

import re
from typing import Any, cast

from waddle_sdk import kv, log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-death"
COMMAND = "!death"

COUNT_KEY = "death.count"
MAX_DEATHS = 1_000_000
_INT_RE = re.compile(r"-?\d{1,9}")

_USAGE = "Usage: !death | !death list | !death add|sub|reset|set <n> (mod only)"
_UNAVAILABLE_MSG = "Something went wrong accessing the death counter - please try again."
_DENIED_MSG = "Only the broadcaster or a moderator can change the death counter."


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


async def _kv_increment(key: str, delta: int) -> int:
    """`kv.increment` (no TTL), reclassifying a host `Err` into `_KvFailure`."""
    try:
        return cast("int", await kv.increment(key, delta, ttl_seconds=0))
    except Exception as exc:
        raise _KvFailure("kv_increment", type(getattr(exc, "value", exc)).__name__) from exc


async def _read_count() -> int:
    """Return the stored total (`0` only if never set); a corrupt value raises `_KvFailure`."""
    raw = await _kv_get(COUNT_KEY)
    if raw is None:
        return 0
    try:
        value = int(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _KvFailure("corrupt_count", type(exc).__name__) from exc
    if value < 0:
        raise _KvFailure("corrupt_count", "NegativeValue")
    return value


def _is_privileged(event: PlatformEvent) -> bool:
    """Broadcaster/moderator check -- fails CLOSED when role info isn't on the event."""
    is_mod = event.payload.get("is_mod")
    is_broadcaster = event.payload.get("is_broadcaster")
    if not isinstance(is_mod, bool) and not isinstance(is_broadcaster, bool):
        log.debug("death.role_info_unavailable", platform=event.platform)
        return False
    return is_mod is True or is_broadcaster is True


def _fmt(total: int) -> str:
    """Render the total as a chat line."""
    return f"\u2620\ufe0f Deaths: {total}"


async def _handle(rest: str, event: PlatformEvent) -> str:
    """Handle everything after `!death`; always returns a reply."""
    verb, _, arg = rest.partition(" ")
    verb = verb.lower()
    arg = arg.strip()
    privileged = _is_privileged(event)

    if verb == "list" or (not verb and not privileged):
        if arg:
            return _USAGE
        total = await _read_count()
        log.info("death.read", count=total)
        return _fmt(total)

    if verb in ("", "add", "sub", "reset", "set"):
        if not privileged:
            log.info("death.permission_denied", op=verb)
            return _DENIED_MSG
        if verb in ("", "add"):
            if arg:
                return _USAGE
            if await _read_count() >= MAX_DEATHS:
                return f"The death counter is capped at {MAX_DEATHS}."
            total = await _kv_increment(COUNT_KEY, 1)
            log.info("death.added", count=total)
            return _fmt(total)
        if verb == "sub":
            if arg:
                return _USAGE
            if await _read_count() == 0:
                return "The death counter is already 0."
            total = await _kv_increment(COUNT_KEY, -1)
            log.info("death.subbed", count=total)
            return _fmt(total)
        if verb == "reset":
            if arg:
                return _USAGE
            await _kv_set(COUNT_KEY, b"0")
            log.info("death.reset")
            return _fmt(0)
        # set
        if not _INT_RE.fullmatch(arg):
            log.info("death.set_not_integer")
            return "Usage: !death set <n> (a whole number)"
        value = int(arg)
        if not 0 <= value <= MAX_DEATHS:
            return f"The death counter must be between 0 and {MAX_DEATHS}."
        await _kv_set(COUNT_KEY, str(value).encode("utf-8"))
        log.info("death.set", count=value)
        return _fmt(value)

    return f"Unknown !death subcommand. {_USAGE}"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognise `!death` and build the reply.

    Exact first-token match (so `!deaths` never matches) comes before the flag check; a `kv`
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
        log.error("death.kv_failure", op=exc.op, error=exc.error)
        reply = _UNAVAILABLE_MSG

    log.info("death.transform matched")
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
        raise ValueError("death reply requires a channel_id from the inbound chat.message")
    if not isinstance(text, str) or not text:
        raise ValueError("death reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("death.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
