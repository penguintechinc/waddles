"""The `componentize-py componentize` app module for the `pyping` bundle.

componentize-py 0.25.1's real generated bindings for a world exporting two
*separate* interfaces (`wit/waddle-bundle/stage.wit`'s `process-stage` +
`action-stage`, both under `world stage`) require one class **per exported
interface**, named after the interface (`ProcessStage`, `ActionStage`) --
confirmed directly by running `componentize-py bindings` against the
committed WIT and reading `wit_world/exports/__init__.py`, then confirmed
again the hard way: `waddle_sdk._component_entry.WitWorld` (a single class
implementing both exports) fails `componentize-py componentize` with
`AttributeError: module 'waddle_sdk._component_entry' has no attribute
'ProcessStage'`. That module's own docstring claim ("componentize-py's
expected app-class name for the `stage` world") does not hold for a
multi-interface-export world under 0.25.1; this file is this bundle's own,
narrowly-scoped fix, kept local rather than touching the shared SDK
(out of scope here -- `bundle_compiler`/SDK changes are not this task's
job).

Both classes delegate into `app.py`'s plain `transform`/`dispatch`
coroutines, reusing `waddle_sdk`'s own facades (WIT-record conversion,
`PollLoop`, `bundle_context`) for the actual host-boundary plumbing --
the same wiring shape `waddle_sdk._component_entry.WitWorld` uses, just
split across the two classes componentize-py 0.25.1 actually requires.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from waddle_sdk import _asyncio_patch  # noqa: F401 - patch asyncio.to_thread before any use
from waddle_sdk._poll_loop import PollLoop
from waddle_sdk.flask_core.bundle_runtime import bundle_context
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.http import HttpClient, RetryableTransportError

from app import dispatch as bundle_dispatch
from app import transform as bundle_transform


def _run_coro(coro: Any) -> Any:
    """Drive one coroutine to completion with `PollLoop` (`asyncio.run()` cannot be used here)."""
    loop = PollLoop()  # type: ignore[abstract]
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        asyncio.set_event_loop(None)


class ProcessStage:
    """Implements the exported `process-stage.transform` WIT function."""

    def transform(self, event: Any) -> Any:
        """`event` is the generated `wit_world.imports.types.PlatformEvent` dataclass."""
        import wit_world

        sdk_event = PlatformEvent.from_wit_record(event)
        ctx = wit_world.imports.context.get_context()

        async def _run() -> PlatformEvent | None:
            with bundle_context(tenant=ctx.tenant, community=ctx.community, app_id=ctx.app_id):
                return await bundle_transform(sdk_event)

        result = _run_coro(_run())
        return result.to_wit_record(wit_world.imports.types) if result is not None else None


class ActionStage:
    """Implements the exported `action-stage.dispatch` WIT function."""

    def dispatch(self, envelope: Any, config: str) -> Any:
        """`envelope` is the generated `wit_world.imports.types.StageEnvelope` dataclass."""
        import wit_world

        sdk_envelope = StageEnvelope.from_wit_record(envelope)
        config_dict = json.loads(config) if config else {}

        async def _run() -> Any:
            with bundle_context(
                tenant=sdk_envelope.tenant,
                community=sdk_envelope.community,
                app_id=sdk_envelope.app_id,
            ):
                return await bundle_dispatch(sdk_envelope, config_dict, http_client=HttpClient())

        try:
            result = _run_coro(_run())
        except Exception as exc:  # noqa: BLE001 - maps *TransportError onto types.TransportError
            from componentize_py_types import Err

            retryable = isinstance(exc, RetryableTransportError)
            raise Err(
                wit_world.imports.types.TransportError(
                    retryable=retryable,
                    code=type(exc).__name__,
                    message=str(exc),
                    retry_after_ms=None,
                )
            ) from exc

        http_status = getattr(result, "http_status", None)
        return wit_world.imports.types.TransportResult(
            ok=True if http_status is None else (200 <= http_status < 400),
            status=http_status,
            detail=getattr(result, "detail", None),
            provider_message_id=getattr(result, "sub_type", None),
        )
