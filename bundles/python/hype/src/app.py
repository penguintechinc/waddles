"""`!hype` -> a per-community kv-backed hype counter.

Themed single-counter sibling of `bundles/python/count` (which manages dynamic named counters).
Verbs: bare `!hype` pays +1 (the themed action itself); `!hype add [N]` +N (default 1, N in
1..1000); `!hype total` (aliases `get`, `show`, `count`) reads the current total without
changing it; `!hype reset` sets it to 0 (broadcaster/moderator only, fails CLOSED when role info
is absent -- same `is_mod`/`is_broadcaster` contract as `count`). Any other trailing text
(e.g. `!hype someone`) is treated as a bare +1 and is never stored or logged.

State: ONE community-scoped counter at kv key `hype.total`. The host `kv` capability already
scopes every key by (tenant, community, app_id), so no community id or actor identity is
embedded in the key (and kv keys never contain ':', see gh-631). Logging is PII-free: only
op / count / exception type are logged -- never raw message text or the target of the command.

Gated behind the PostHog flag ``waddles.command-hype``.
"""

from __future__ import annotations

from typing import Any, cast

from waddle_sdk import kv, log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-hype"
COMMAND = "!hype"
TOTAL_KEY = "hype.total"
MAX_INCREMENT = 1000
_READ_OPS = frozenset({"total", "get", "show", "count"})


class _KvFailure(Exception):
    """Internal-only: a `kv` host-call failed. Always caught inside `transform`."""


async def _kv_get(key: str) -> bytes | None:
    """`kv.get`, reclassifying the generated WIT `Err` into `_KvFailure` (fail-loud)."""
    try:
        result = await kv.get(key)
    except Exception as exc:  # noqa: BLE001 - classified like waddle_sdk.db/http
        raise _KvFailure(f"kv.get failed: {getattr(exc, 'value', exc)}") from exc
    return cast("bytes | None", result)


async def _kv_set(key: str, value: bytes) -> None:
    """`kv.set` (no TTL), reclassifying `Err` into `_KvFailure`."""
    try:
        await kv.set(key, value, ttl_seconds=0)
    except Exception as exc:  # noqa: BLE001
        raise _KvFailure(f"kv.set failed: {getattr(exc, 'value', exc)}") from exc


async def _kv_increment(key: str, delta: int) -> int:
    """`kv.increment` (atomic, no TTL), reclassifying `Err` into `_KvFailure`."""
    try:
        result = await kv.increment(key, delta, ttl_seconds=0)
    except Exception as exc:  # noqa: BLE001
        raise _KvFailure(f"kv.increment failed: {getattr(exc, 'value', exc)}") from exc
    return cast(int, result)


async def _read_total() -> int:
    """Return the current total, or 0 if never written."""
    raw = await _kv_get(TOTAL_KEY)
    if raw is None:
        return 0
    try:
        return int(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _KvFailure("corrupt hype total") from exc


def _is_privileged(event: PlatformEvent) -> bool:
    """Broadcaster/moderator check -- fails CLOSED when role info isn't on the event."""
    is_mod = event.payload.get("is_mod")
    is_broadcaster = event.payload.get("is_broadcaster")
    if not isinstance(is_mod, bool) and not isinstance(is_broadcaster, bool):
        log.debug("hype.role_info_unavailable", platform=event.platform)
        return False
    return bool(is_mod) or bool(is_broadcaster)


def _inc_reply(total: int) -> str:
    """Reply after a +N increment."""
    return f"HYPE! Hype level: {total}"


def _read_reply(total: int) -> str:
    """Reply for a read-only total query."""
    return f"Current hype level: {total}"


async def _handle(args: list[str], event: PlatformEvent) -> str:
    """Run one `!hype` invocation and return the reply text."""
    op = args[0].lower() if args else ""

    if op in _READ_OPS:
        total = await _read_total()
        log.debug("hype.read", op="read", count=total)
        return _read_reply(total)

    if op == "add":
        amount = 1
        if len(args) > 1:
            try:
                amount = int(args[1])
            except ValueError:
                return "Usage: !hype add [N] (N is a whole number)"
        if not 1 <= amount <= MAX_INCREMENT:
            return f"N must be between 1 and {MAX_INCREMENT}"
        total = await _kv_increment(TOTAL_KEY, amount)
        log.info("hype.incremented", op="add", count=total)
        return _inc_reply(total)

    if op == "reset":
        if not _is_privileged(event):
            log.info("hype.permission_denied", op="reset")
            return "Only the broadcaster or a moderator can reset this counter."
        await _kv_set(TOTAL_KEY, b"0")
        log.info("hype.reset", op="reset", count=0)
        return "Hype counter reset to 0."

    # Bare command, or free text after it (e.g. `!hype someone`): +1.
    total = await _kv_increment(TOTAL_KEY, 1)
    log.info("hype.incremented", op="increment", count=total)
    return _inc_reply(total)


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!hype` and reply with the counter state.

    Returns `None` for non-chat payloads, non-`!hype` text, or while the flag is off.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    parts = text.strip().split()
    if not parts or parts[0].lower() != COMMAND:
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    try:
        reply = await _handle(parts[1:], event)
    except _KvFailure as exc:
        log.error("hype.kv_failure", error_type=type(exc.__cause__ or exc).__name__)
        reply = "Something went wrong updating the counter storage - please try again."

    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"channel_id": event.payload.get("channel_id"), "text": reply},
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
        raise ValueError("hype reply requires a channel_id from the inbound chat.message")
    if not isinstance(text, str) or not text:
        raise ValueError("hype reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("hype.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
