"""Shared tool-invocation helpers for the Wix feature modules (`plan_roles`, `event_sync`).

Platform SERVICE bundles own the Discord tool definitions; this APP bundle only calls them through
`waddle_sdk.tool.invoke` under approved manifest `tool.call:` grants. Every failure here is LOUD:
it is logged with its typed code AND recorded under `wix.fault.<module>` so `!wix modules` shows it
to a moderator -- a missing tool or a refused grant is never a silent no-op.
"""

from __future__ import annotations

from typing import Any

from waddle_sdk import community_kv, log, tool

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
        await record_fault(community, module, exc.code)
        return None
    await clear_fault(community, module)
    return result
