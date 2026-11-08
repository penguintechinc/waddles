"""Shared tool-invocation helpers for the Wix feature modules (`plan_roles`, `event_sync`).

Platform SERVICE bundles own the Discord tool definitions; this APP bundle only calls them through
`waddle_sdk.tool.invoke` under approved manifest `tool.call:` grants. Every failure here is LOUD:
it is logged with its typed code AND recorded under `wix.fault.<module>` so `!wix modules` shows it
to a moderator -- a missing tool or a refused grant is never a silent no-op.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from waddle_sdk import community_kv, log, tool

#: The SDK's host-forwarded logger under the name the no-stubs silent-fallback gate recognises.
logger = log

#: Service bundle owning the Discord tool definitions.
DISCORD_SERVICE = "waddles.core.service.discord"
#: The WIT import the tool-invocation path needs (only in the `stage-next-tools` world).
REQUIRED_IMPORT = "tool"
MISSING_CAPABILITY_MSG = (
    "Wix {module} sync can't be enabled: this runtime doesn't provide the tool-invocation "
    "capability (the bundle must run in the stage-next-tools world)."
)


def capability_available() -> bool:
    """True iff the runtime's `wit_world.imports` exposes the tool-invocation import."""
    # The probe's contract is a boolean: callers refuse enabling loudly with the missing-capability
    # message when it is False. `log` itself needs `wit_world`, so it cannot be used here --
    # absence of the import IS the signal being reported, not a swallowed error.
    try:
        import wit_world
    except ImportError:
        wit_world = None
    if wit_world is None:
        return False
    return getattr(wit_world.imports, REQUIRED_IMPORT, None) is not None


def fault_key(module: str) -> str:
    """kv key (`.`-only charset) holding the last tool failure code for `module`."""
    return f"wix.fault.{module}"


async def record_fault(community: str | None, module: str, code: str) -> None:
    """Persist a typed failure code so a moderator can see why the module is not working."""
    log.error("wix.tool_failed", module=module, code=code)
    try:
        await community_kv.set(
            community, fault_key(module), code.encode(), ttl_seconds=0
        )
    except Exception as exc:  # noqa: BLE001 - kv is best-effort for the fault MARKER only
        log.error(
            "wix.fault_record_failed", module=module, error_type=type(exc).__name__
        )


async def clear_fault(community: str | None, module: str) -> None:
    """Forget a recorded failure after a successful call."""
    try:
        await community_kv.delete(community, fault_key(module))
    except Exception as exc:  # noqa: BLE001 - marker cleanup only; the call itself succeeded
        log.warn("wix.fault_clear_failed", module=module, error_type=type(exc).__name__)


async def call(
    community: str | None,
    module: str,
    name: str,
    args: dict[str, Any],
    *,
    idempotency_key: str,
) -> Any | None:
    """Invoke one Discord tool; on any failure record a fault loudly and return `None`."""
    if not capability_available():
        await record_fault(community, module, "capability_missing")
        return None
    try:
        result = await tool.invoke(
            DISCORD_SERVICE, name, args, idempotency_key=idempotency_key
        )
    except tool.ToolError as exc:
        logger.error("wix.tool_call_failed", module=module, tool=name, code=exc.code)
        await record_fault(community, module, exc.code)
        return None
    await clear_fault(community, module)
    return result


#: States after which nothing may re-grant or resurrect (revocations always win).
TERMINAL = frozenset({"ENDED", "CANCELED"})


def parse_ts(raw: str) -> datetime | None:
    """Timezone-aware datetime for an RFC 3339 string, or `None` when unparseable/naive."""
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


async def read_order(
    community: str | None, key: str
) -> tuple[datetime, str, bool] | None:
    """Last applied `(occurred_at, status, wix_sourced)` under `key`; corrupt -> loud failure."""
    raw = await community_kv.get(community, key)
    if raw is None:
        return None
    ts_text, _, rest = raw.decode("utf-8", "replace").partition("|")
    status, _, src = rest.partition("|")
    ts = parse_ts(ts_text)
    if ts is None or not status or src not in ("w", "r"):
        log.error("wix.order_state_corrupt")
        raise ValueError("corrupt ordering state")
    return ts, status, src == "w"


def grant_is_newer(
    prior: tuple[datetime, str, bool] | None, ts: datetime, *, wix_time: bool
) -> bool:
    """True iff a GRANT may proceed: Wix-sourced and strictly newer than a Wix-sourced prior.

    Timestamps from different clocks are never compared. A receive-time-sourced non-terminal state is
    never stored (see `write_order`), so `prior` None means unseen.
    """
    if not wix_time:
        return False
    if prior is None:
        return True
    return prior[2] and ts > prior[0]


async def write_order(
    community: str | None, key: str, ts: datetime, status: str, *, wix_time: bool
) -> None:
    """Record applied `(occurred_at, status, clock)`; terminal always, others only if orderable.

    A non-terminal state from the receive-time clock is NOT stored: it cannot be ordered against
    Wix's clock, so keeping the prior (Wix-sourced) state is the only comparison-safe choice.
    """
    if not wix_time and status not in TERMINAL:
        return
    src = "w" if wix_time else "r"
    await community_kv.set(
        community, key, f"{ts.isoformat()}|{status}|{src}".encode(), ttl_seconds=0
    )
