"""OTel tracer/meter access + a reusable span helper for the M2b workstream/usage surface.

API only -- `opentelemetry.trace` and `opentelemetry.metrics`. No SDK,
no exporter, no vendor library. Where the data goes is a deployment
concern set through the standard OTLP env vars
(`OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_PROTOCOL`,
`OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_SERVICE_NAME`,
`OTEL_RESOURCE_ATTRIBUTES`); with no provider configured every call
here resolves to the API's no-op implementation and costs nothing.
Telemetry failure is never a request failure.

**Scope note.** The full M2b plan's `bundle_telemetry.py` (Task 38)
also owns bundle-version-upload/approval/grant counters for the
bundle-install control plane, which is out of this slice's scope (see
`alembic/versions/0020_ingest_sources_and_rbac_roles.py`'s own scope
note). This module carries forward only `get_tracer()`/`get_meter()`/
`bundle_span()` -- the shared primitives `services/usage_aggregator_
service.py` needs for its own counters/histograms.

No PII, no secrets in any span attribute or metric label.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from opentelemetry import metrics, trace

_TRACER: Any = None
_METER: Any = None

_SCOPE = "waddles.hub_api.workstreams"


def get_tracer() -> Any:
    """The module's tracer, created once. No-op when no provider is configured."""
    global _TRACER
    if _TRACER is None:
        _TRACER = trace.get_tracer(_SCOPE)
    return _TRACER


def get_meter() -> Any:
    """The module's meter, created once. No-op when no provider is configured."""
    global _METER
    if _METER is None:
        _METER = metrics.get_meter(_SCOPE)
    return _METER


@asynccontextmanager
async def bundle_span(name: str, **attributes: Any) -> AsyncIterator[Any]:
    """Span the real work.

    Sets an ERROR status and records the exception when the body
    raises, then re-raises -- observability never swallows a failure.
    """
    started = time.perf_counter()
    with get_tracer().start_as_current_span(name) as span:
        for key, value in attributes.items():
            if value is not None:
                span.set_attribute(key, value)
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
            raise
        finally:
            span.set_attribute("duration_ms", (time.perf_counter() - started) * 1000.0)
