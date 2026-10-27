"""`!uptime` -> humanized time since this community first activated the bundle.

A WASM bundle cannot see the OS process start, so "uptime" here means *time since first
activation* in a community: the first `!uptime` a community ever runs records the host
`clock` reading under the single `kv` key `uptime.start` (epoch milliseconds, ASCII) and
reports ~0; every later invocation reports `now - uptime.start`, e.g. ``up 3d 4h 12m``.

State is community-scoped without any manual hashing: the key is a plain literal and the
`kv` host capability scopes every key server-side by `(tenant, community, app_id)`
(`core/bundle_host_kv/src/scope.rs::KvScope`). The key uses `.`, never `:` (the host's
reserved namespace separator, gh-631). No actor identity is stored.

Ordering inside `transform()`: cheap command match first, then command-grammar parsing, then `kv`. A
`kv` failure is reclassified into a private `_KvError`, logged at ERROR and answered with an
explicit reply -- never a silent no-op. Logs are PII-free: only op names and exception types are
ever logged, never message text or the actor.

`kv`, `log`, `relay` and `clock` are imported at MODULE LOAD on purpose:
componentize-py only wizens a WIT host-import that is imported eagerly, so a capability
imported only inside a function is absent from `wit_world.imports` at runtime
(`docs/reference/bundle-host-imports.md`).
"""

from __future__ import annotations

from typing import Any, cast

from waddle_sdk import clock, kv, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, parse_command
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

#: The single `kv` key this bundle owns (community-scoped by the host; `.`, never `:`).
START_KEY = "uptime.start"

_SPEC = CommandSpec(name="uptime")
_COMMAND = "!uptime"
_USAGE = "Usage: !uptime"

_SECOND_MS = 1000
_MINUTE_MS = 60 * _SECOND_MS
_HOUR_MS = 60 * _MINUTE_MS
_DAY_MS = 24 * _HOUR_MS

_KV_FAILURE_REPLY = "Something went wrong reading uptime storage - please try again."


class _KvError(Exception):
    """Internal-only: a `kv` host-call failed or the stored value is corrupt.

    Always caught inside `transform`, never leaked. Mirrors `waddle_sdk.db`'s documented
    pattern ("Err is structurally classified, never imported") since `waddle_sdk.kv` does
    not classify the generated WIT `Err` itself.
    """


async def _kv_get(key: str) -> bytes | None:
    """`kv.get`, reclassifying the generated WIT `Err` into `_KvError` (fail loud)."""
    try:
        result = await kv.get(key)
    except Exception as exc:  # noqa: BLE001 - classified like waddle_sdk.db/http
        raise _KvError(f"kv.get({key!r}) failed: {getattr(exc, 'value', exc)}") from exc
    # `waddle_sdk` ships no `py.typed`, so mypy sees `Any` -- cast back to the real contract.
    return cast("bytes | None", result)


async def _kv_set(key: str, value: bytes) -> None:
    """`kv.set` (no TTL -- the start timestamp persists), reclassifying `Err` into `_KvError`."""
    try:
        await kv.set(key, value, ttl_seconds=0)
    except Exception as exc:  # noqa: BLE001
        raise _KvError(f"kv.set({key!r}) failed: {getattr(exc, 'value', exc)}") from exc


def humanize_elapsed(elapsed_ms: int) -> str:
    """Render a millisecond duration as ``up 3d 4h 12m`` (``up 45s`` under a minute).

    Components run from the largest non-zero unit down to minutes (so ``1d 0h 5m`` keeps
    its zero hour); a negative duration (clock skew) is clamped to zero.
    """
    total = max(elapsed_ms, 0)
    if total < _MINUTE_MS:
        return f"up {total // _SECOND_MS}s"
    days, rem = divmod(total, _DAY_MS)
    hours, rem = divmod(rem, _HOUR_MS)
    minutes = rem // _MINUTE_MS
    if days:
        return f"up {days}d {hours}h {minutes}m"
    if hours:
        return f"up {hours}h {minutes}m"
    return f"up {minutes}m"


async def _elapsed_reply() -> str:
    """Return the uptime reply, recording the start timestamp on first invocation.

    Raises:
        _KvError: A `kv` round trip failed, or the stored start is not an integer.
    """
    now_ms = clock.now_millis()
    raw = await _kv_get(START_KEY)
    if raw is None:
        await _kv_set(START_KEY, str(now_ms).encode("utf-8"))
        log.info("uptime.start_recorded")
        return humanize_elapsed(0)
    try:
        start_ms = int(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _KvError(f"corrupt {START_KEY}: {type(exc).__name__}") from exc
    return humanize_elapsed(now_ms - start_ms)


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!uptime` and build the reply.

    Returns `None` for a non-chat payload, any message that is not `!uptime`, and while
    `waddles.command-uptime` is off. A grammar error replies with the parser's message;
    a `kv` failure logs at ERROR and replies with an explicit failure notice.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    head = text.strip().partition(" ")[0]
    if head.lower() != _COMMAND:
        return None

    try:
        parsed = parse_command(text, _SPEC)
        if parsed.option is not None:
            raise CommandUsageError(_USAGE)
        reply = await _elapsed_reply()
    except CommandUsageError as exc:
        log.debug("uptime.usage_error")
        reply = str(exc)
    except _KvError as exc:
        log.error("uptime.kv_failure", error_type=type(exc.__cause__).__name__)
        reply = _KV_FAILURE_REPLY

    log.info("uptime.transform matched")
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
        ValueError: The envelope's payload is missing `channel_id` or `text`.
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    text = payload.get("text")
    if not channel_id:
        raise ValueError("uptime reply requires a channel_id from the inbound chat.message")
    if not isinstance(text, str) or not text:
        raise ValueError("uptime reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("uptime.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
