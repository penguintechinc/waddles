"""Telemetry validation for services.bundle_telemetry -- spans actually emitted.

Counts are asserted AND printed: a zero-item run is a failure, never a
pass (critical-rules.md Verification Integrity, testing.md Telemetry
Validation).
"""

from __future__ import annotations

from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.fixture
def otel_sink(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A real in-process OTel SDK wired to an in-memory span exporter.

    The local OTLP test sink.
    """
    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))

    monkeypatch.setattr(trace, "get_tracer_provider", lambda: tracer_provider)

    import services.bundle_telemetry as telemetry

    monkeypatch.setattr(telemetry, "_TRACER", None)
    monkeypatch.setattr(telemetry, "_METER", None)

    return exporter


async def test_bundle_span_emits_a_span(otel_sink: Any) -> None:
    from services.bundle_telemetry import bundle_span

    async with bundle_span("hub.usage.test_span", stream="waddles:usage"):
        pass

    spans = otel_sink.get_finished_spans()
    print(f"telemetry check: spans received = {len(spans)}")
    assert len(spans) == 1
    assert spans[0].name == "hub.usage.test_span"
    assert spans[0].attributes["stream"] == "waddles:usage"
    assert spans[0].status.status_code == trace.StatusCode.UNSET


async def test_bundle_span_records_the_exception_and_reraises(otel_sink: Any) -> None:
    from services.bundle_telemetry import bundle_span

    with pytest.raises(ValueError, match="boom"):
        async with bundle_span("hub.usage.test_error"):
            raise ValueError("boom")

    spans = otel_sink.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == trace.StatusCode.ERROR


async def test_no_provider_configured_is_a_silent_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dead or absent exporter must never break the app (critical-rules.md Observability)."""
    import services.bundle_telemetry as telemetry

    monkeypatch.setattr(telemetry, "_TRACER", None)
    monkeypatch.setattr(telemetry, "_METER", None)
    async with telemetry.bundle_span("hub.usage.noop"):
        pass
