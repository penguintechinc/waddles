"""Test-only Python `stage-v2` bundle that echoes the numeric scope it receives.

Not a shipped bundle. It exists so `core/bundle_executor/tests/stage_v2_python_e2e.rs`
can prove the whole Python v2 path against a REAL `componentize-py -w stage-v2`
build: the SDK's eager wizening of `wit_world.imports.context_v2`, the
`ActionStageV2` app class, and the authoritative numeric ids reaching the bundle
through both `get_bundle_context()` (transform) and the dispatch envelope.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from waddle_sdk.flask_core.bundle_runtime import get_bundle_context
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope


async def transform(event: PlatformEvent) -> PlatformEvent:
    """Echo the bundle context's numeric ids as `payload["result"]`."""
    ctx = get_bundle_context()
    return replace(event, payload={"result": f"{ctx.tenant_id}:{ctx.community_id}:{ctx.tenant}"})


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result (duck-typed by the SDK entry)."""

    __slots__ = ("transport", "detail", "sub_type", "http_status")

    def __init__(self, *, detail: str, sub_type: str) -> None:
        """Carry the echoed scope in `detail`/`sub_type` (-> WIT `provider-message-id`)."""
        self.transport = "echo"
        self.detail = detail
        self.sub_type = sub_type
        self.http_status = None


async def dispatch(envelope: StageEnvelope, config: dict[str, Any], **_: Any) -> DispatchResult:
    """Echo the v2 envelope's numeric ids AND the bundle context's, which must agree."""
    ctx = get_bundle_context()
    env_ids = f"{envelope.tenant_id}:{envelope.community_id}"
    ctx_ids = f"{ctx.tenant_id}:{ctx.community_id}"
    return DispatchResult(detail=f"{env_ids}|{ctx_ids}", sub_type=env_ids)
