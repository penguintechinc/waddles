"""OTel emission for model-provider calls -- spans AND metrics actually received, with counts.

A real in-process OTel SDK (in-memory span exporter + in-memory metric reader) stands in for the
OTLP sink. Counts are printed and asserted: zero received is a failure, never a pass
(critical-rules.md Observability / Verification Integrity).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from services.ai_routing import clients
from services.ai_routing.clients import OllamaClient, OllamaConfig
from services.ai_routing.errors import ApiError
from services.ai_routing.models import AIRequest
from tests.ai_routing_helpers import ollama_body, patch_transport

SECRET_PROMPT = "synthetic confidential prompt text"


class Sink:
    """The local OTel test sink: spans + metric points received."""

    def __init__(self, spans: InMemorySpanExporter, reader: InMemoryMetricReader) -> None:
        """Hold the in-memory span exporter and metric reader."""
        self.spans = spans
        self.reader = reader

    def finished_spans(self) -> list[Any]:
        return list(self.spans.get_finished_spans())

    def points(self, name: str) -> list[Any]:
        data = self.reader.get_metrics_data()
        found: list[Any] = []
        for resource in data.resource_metrics if data else []:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    if metric.name == name:
                        found.extend(metric.data.data_points)
        return found


@pytest.fixture
def sink(monkeypatch: pytest.MonkeyPatch) -> Sink:
    spans = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(spans))
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])
    monkeypatch.setattr(clients.telemetry, "_instruments", None)  # restored on teardown
    clients.telemetry.use_providers(tracer_provider, meter_provider)
    return Sink(spans, reader)


def _client(model: str = "m", *, supports_json: bool = False) -> OllamaClient:
    return OllamaClient(
        OllamaConfig(base_url="http://ollama.test", model=model, supports_json=supports_json)
    )


async def test_successful_call_emits_a_span_a_histogram_point_and_token_counters(
    sink: Sink, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_transport(monkeypatch, lambda r: httpx.Response(200, json=ollama_body("hello")))

    await _client("gemma4:e2b").generate(AIRequest(prompt=SECRET_PROMPT), tier="free")

    spans = sink.finished_spans()
    durations = sink.points("waddles.ai.provider.duration")
    tokens = sink.points("waddles.ai.provider.tokens")
    print(
        f"telemetry check: spans={len(spans)} duration_points={len(durations)} "
        f"token_points={len(tokens)}"
    )
    assert len(spans) == 1 and len(durations) == 1 and len(tokens) == 2
    assert spans[0].name == "ai.provider.generate"
    assert dict(spans[0].attributes) == {
        "ai.provider": "ollama",
        "ai.tier": "free",
        "ai.model": "gemma4:e2b",
        "ai.mode": "text",
    }
    assert spans[0].status.status_code == trace.StatusCode.UNSET
    assert durations[0].count == 1 and durations[0].sum >= 0
    assert dict(durations[0].attributes) == {
        "ai.provider": "ollama",
        "ai.tier": "free",
        "ai.model": "gemma4:e2b",
        "ai.mode": "text",
        "outcome": "ok",
    }
    by_direction = {p.attributes["direction"]: p.value for p in tokens}
    assert by_direction == {"input": 7, "output": 3}  # the provider-reported numbers
    assert sink.points("waddles.ai.provider.errors") == []


async def test_json_mode_is_labelled(sink: Sink, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_transport(monkeypatch, lambda r: httpx.Response(200, json=ollama_body('{"a": 1}')))

    await _client("json-model", supports_json=True).generate(
        AIRequest(prompt="x", wants_json=True), tier="premium"
    )

    assert sink.finished_spans()[0].attributes["ai.mode"] == "json"
    assert sink.points("waddles.ai.provider.duration")[0].attributes["ai.mode"] == "json"


async def test_failed_call_marks_the_span_and_counts_the_typed_error(
    sink: Sink, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_transport(monkeypatch, lambda r: httpx.Response(500, json={"error": "x"}))

    with pytest.raises(ApiError):
        await _client().generate(AIRequest(prompt=SECRET_PROMPT), tier="premium")

    spans = sink.finished_spans()
    errors = sink.points("waddles.ai.provider.errors")
    durations = sink.points("waddles.ai.provider.duration")
    print(f"telemetry check: spans={len(spans)} error_points={len(errors)}")
    assert len(spans) == 1 and len(errors) == 1 and len(durations) == 1
    assert spans[0].status.status_code == trace.StatusCode.ERROR
    assert [e.name for e in spans[0].events] == ["exception"]
    assert errors[0].value == 1
    assert errors[0].attributes["error.code"] == "AI_PROVIDER_ERROR"
    assert durations[0].attributes["outcome"] == "error"
    assert sink.points("waddles.ai.provider.tokens") == []


async def test_empty_completion_counts_as_an_error(
    sink: Sink, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_transport(monkeypatch, lambda r: httpx.Response(200, json=ollama_body("")))

    with pytest.raises(ApiError, match="empty completion"):
        await _client().generate(AIRequest(prompt="x"), tier="free")

    assert sink.points("waddles.ai.provider.errors")[0].value == 1
    assert sink.finished_spans()[0].status.status_code == trace.StatusCode.ERROR


async def test_no_prompt_text_or_secret_ever_reaches_a_span_or_metric_label(
    sink: Sink, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_transport(monkeypatch, lambda r: httpx.Response(200, json=ollama_body("a reply")))

    await _client().generate(AIRequest(prompt=SECRET_PROMPT), tier="free")

    rendered = repr([dict(s.attributes) for s in sink.finished_spans()])
    for name in (
        "waddles.ai.provider.duration",
        "waddles.ai.provider.tokens",
    ):
        rendered += repr([dict(p.attributes) for p in sink.points(name)])
    assert SECRET_PROMPT not in rendered and "a reply" not in rendered


async def test_no_provider_configured_is_a_silent_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clients.telemetry, "_instruments", None)
    patch_transport(monkeypatch, lambda r: httpx.Response(200, json=ollama_body("hello")))

    response = await _client().generate(AIRequest(prompt="x"), tier="free")

    assert response.text == "hello"


async def test_broken_telemetry_never_fails_the_request(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def explode() -> None:
        raise RuntimeError("otel is down")

    monkeypatch.setattr(clients.telemetry, "instruments", explode)
    patch_transport(monkeypatch, lambda r: httpx.Response(200, json=ollama_body("hello")))

    with caplog.at_level("WARNING"):
        response = await _client().generate(AIRequest(prompt="x"), tier="free")

    assert response.text == "hello"
    assert caplog.text.count("ai_telemetry_failed") == 2  # span setup + metric record
