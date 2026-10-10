"""`flask_core.ai_telemetry` -- spans + metrics for LLM calls, received by a real OTel SDK.

An in-memory span exporter + metric reader stand in for the OTLP sink. Counts are printed and
asserted: zero received is a failure, never a pass.
"""

from __future__ import annotations

from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from flask_core.ai_telemetry import AITelemetry


class Sink:
    """Spans + metric points received by the local test sink."""

    def __init__(self) -> None:
        self.spans = InMemorySpanExporter()
        self.reader = InMemoryMetricReader()
        tracer_provider = TracerProvider()
        tracer_provider.add_span_processor(SimpleSpanProcessor(self.spans))
        self.telemetry = AITelemetry("test.scope")
        self.telemetry.use_providers(tracer_provider, MeterProvider(metric_readers=[self.reader]))

    def points(self, name: str) -> list[Any]:
        data = self.reader.get_metrics_data()
        found: list[Any] = []
        for resource in data.resource_metrics if data else []:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    if metric.name == name:
                        found.extend(metric.data.data_points)
        return found


def test_success_emits_span_histogram_and_token_counters() -> None:
    sink = Sink()
    with sink.telemetry.span(provider="ollama", model="m", mode="text", tier="free"):
        pass
    sink.telemetry.record_call(
        provider="ollama",
        model="m",
        mode="text",
        tier="free",
        duration_ms=12.5,
        input_tokens=7,
        output_tokens=3,
    )

    spans = sink.spans.get_finished_spans()
    durations = sink.points("waddles.ai.provider.duration")
    tokens = sink.points("waddles.ai.provider.tokens")
    print(f"telemetry check: spans={len(spans)} durations={len(durations)} tokens={len(tokens)}")
    assert len(spans) == 1 and len(durations) == 1 and len(tokens) == 2
    assert spans[0].name == "ai.provider.generate"
    assert dict(spans[0].attributes) == {
        "ai.provider": "ollama",
        "ai.model": "m",
        "ai.mode": "text",
        "ai.tier": "free",
    }
    assert spans[0].status.status_code == trace.StatusCode.UNSET
    assert durations[0].count == 1 and durations[0].sum == 12.5
    assert dict(durations[0].attributes)["outcome"] == "ok"
    assert {p.attributes["direction"]: p.value for p in tokens} == {"input": 7, "output": 3}
    assert sink.points("waddles.ai.provider.errors") == []


def test_tier_is_optional() -> None:
    sink = Sink()
    with sink.telemetry.span(provider="waddleai", model="auto", mode="json"):
        pass
    sink.telemetry.record_call(provider="waddleai", model="auto", mode="json", duration_ms=1.0)

    assert "ai.tier" not in sink.spans.get_finished_spans()[0].attributes
    assert "ai.tier" not in sink.points("waddles.ai.provider.duration")[0].attributes


def test_failure_marks_the_span_and_counts_the_error_code() -> None:
    sink = Sink()
    with pytest.raises(RuntimeError, match="boom"):
        with sink.telemetry.span(provider="ollama", model="m", mode="text"):
            raise RuntimeError("boom")
    sink.telemetry.record_call(
        provider="ollama", model="m", mode="text", duration_ms=2.0, error_code="ReadTimeout"
    )

    spans = sink.spans.get_finished_spans()
    errors = sink.points("waddles.ai.provider.errors")
    print(f"telemetry check: spans={len(spans)} errors={len(errors)}")
    assert len(spans) == 1 and len(errors) == 1
    assert spans[0].status.status_code == trace.StatusCode.ERROR
    assert [e.name for e in spans[0].events] == ["exception"]
    assert errors[0].value == 1 and errors[0].attributes["error.code"] == "ReadTimeout"
    assert sink.points("waddles.ai.provider.duration")[0].attributes["outcome"] == "error"
    assert sink.points("waddles.ai.provider.tokens") == []


def test_no_provider_configured_is_a_silent_no_op() -> None:
    telemetry = AITelemetry("test.noop")
    with telemetry.span(provider="ollama", model="m", mode="text") as span:
        assert span is not None
    telemetry.record_call(
        provider="ollama", model="m", mode="text", duration_ms=1.0, input_tokens=1, output_tokens=1
    )
    assert telemetry.instruments() is telemetry.instruments()  # built once, then reused


def test_span_setup_failure_runs_the_body_unspanned(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    telemetry = AITelemetry("test.broken")

    def explode() -> None:
        raise RuntimeError("otel is down")

    monkeypatch.setattr(telemetry, "instruments", explode)
    ran = False
    with caplog.at_level("WARNING"):
        with telemetry.span(provider="ollama", model="m", mode="text") as span:
            ran = True
            assert span is None

    assert ran
    assert "ai_telemetry_failed" in caplog.text


def test_body_errors_still_propagate_when_span_setup_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    telemetry = AITelemetry("test.broken2")
    monkeypatch.setattr(telemetry, "instruments", lambda: (_ for _ in ()).throw(RuntimeError("x")))

    with pytest.raises(ValueError, match="real failure"):
        with telemetry.span(provider="ollama", model="m", mode="text"):
            raise ValueError("real failure")


def test_record_call_never_raises_even_if_an_instrument_dies(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class Dead:
        def record(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("dead exporter")

    class Inst:
        duration = Dead()
        tokens = Dead()
        errors = Dead()

    telemetry = AITelemetry("test.dead")
    monkeypatch.setattr(telemetry, "instruments", lambda: Inst())

    with caplog.at_level("WARNING"):
        telemetry.record_call(provider="ollama", model="m", mode="text", duration_ms=1.0)

    assert "ai_telemetry_failed" in caplog.text
