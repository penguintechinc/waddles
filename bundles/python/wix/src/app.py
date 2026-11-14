"""Wix event-only integration (`waddles.core.integration.wix`, #485).

This bundle exposes NO chat command. It consumes only the Wix webhook connector's normalized
records and syncs EVENTS ONLY: `event.{created,updated,canceled}` -> `event_sync` (Wix events ->
community/Discord scheduled events). Licensing and subscriptions are handled by the PenguinTech
license server, never Wix, so any other Wix record (e.g. `subscription.*`) is ignored.

The module calls the Discord SERVICE bundle's tools under approved manifest `tool.call:` grants
(never direct Discord HTTP). Activation of this bundle is the gate (first-party app bundle, no
tier/licence gate). A runtime without the tool-invocation import is a LOUD failure: the fault is
logged and recorded under `wix.fault.<module>` -- an event is never silently dropped.

Nothing is ever replied to chat, so the action stage refuses to run.
"""

from __future__ import annotations

from typing import Any

from waddle_sdk import log
from waddle_sdk.flask_core import get_bundle_context
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

import event_sync
import toolcall

#: Event-type prefix -> feature module name (also the `wix.fault.<name>` suffix).
_MODULES: dict[str, Any] = {"event.": event_sync}


async def _on_wix_event(event: PlatformEvent) -> None:
    """Route one connector-normalized Wix record to its feature module.

    Failures inside a module are recorded loudly by `toolcall`; a kv error propagates so the stage
    run fails visibly instead of dropping the event.
    """
    community = get_bundle_context().community
    kind = event.event_type
    for prefix, module in _MODULES.items():
        if not kind.startswith(prefix):
            continue
        if not module.capability_available():
            await toolcall.record_fault(community, module.MODULE, "capability_missing")
            return
        await module.handle(kind, event.payload, event.occurred_at, community)
        return
    log.debug("wix.event_ignored", event_type=kind)


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: handle a Wix webhook record; never produce a reply.

    Any non-Wix event is ignored (the manifest only subscribes to `platform: wix`, so reaching
    that branch means a routing bug -- it is logged).
    """
    if event.platform != "wix":
        log.warn("wix.unexpected_platform", platform=event.platform)
        return None
    await _on_wix_event(event)
    return None


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result (kept for the action-stage signature)."""

    __slots__ = ("detail", "http_status", "sub_type", "transport")

    def __init__(self, *, transport: str, detail: str) -> None:
        """Record the transport and a short detail string."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = None


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: this bundle never replies, so reaching here is a bug.

    Raises:
        RuntimeError: Always -- `transform` never produces an event to dispatch.
    """
    log.error("wix.dispatch_unexpected", platform=envelope.event.platform)
    raise RuntimeError("wix bundle is event-only; transform never produces a reply")
