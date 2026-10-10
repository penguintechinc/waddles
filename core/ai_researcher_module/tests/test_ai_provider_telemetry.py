"""OTel emission for `AIProviderService.generate` -- spans + metrics actually received, with counts.

A real in-process OTel SDK (in-memory exporters) stands in for the OTLP sink; zero received is a
failure, never a pass. No prompt text, user id or secret may appear in any attribute/label.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from ai_fakes import FakeConfig
from opentelemetry import trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from services import ai_provider as ai_provider_module
from services.ai_provider import AIProviderService, EmptyCompletionError

SECRET_PROMPT = "synthetic confidential prompt text"


class Sink:
    """Local OTel test sink: finished spans + metric data points."""

    def __init__(self) -> None:
        self.spans = InMemorySpanExporter()
        self.reader = InMemoryMetricReader()
        tracer_provider = TracerProvider()
        tracer_provider.add_span_processor(SimpleSpanProcessor(self.spans))
        ai_provider_module.telemetry.use_providers(
            tracer_provider, MeterProvider(metric_readers=[self.reader])
        )

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
    monkeypatch.setattr(ai_provider_module.telemetry, "_instruments", None)  # restored on teardown
    return Sink()


def _service(handler: Any, **overrides: Any) -> AIProviderService:
    service = AIProviderService(FakeConfig(**overrides))  # type: ignore[arg-type]
    service._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return service


def _ok(text: str) -> Any:
    body = {"response": text, "done": True, "done_reason": "stop", "eval_count": 5}
    return lambda request: httpx.Response(200, json=body)


async def test_success_emits_span_duration_and_output_tokens(sink: Sink) -> None:
    await _service(_ok("hello"), OLLAMA_MODEL="gemma4:e2b").generate(SECRET_PROMPT)

    spans = sink.spans.get_finished_spans()
    durations = sink.points("waddles.ai.provider.duration")
    tokens = sink.points("waddles.ai.provider.tokens")
    print(f"telemetry check: spans={len(spans)} durations={len(durations)} tokens={len(tokens)}")
    assert len(spans) == 1 and len(durations) == 1 and len(tokens) == 1
    assert dict(spans[0].attributes) == {
        "ai.provider": "ollama",
        "ai.model": "gemma4:e2b",
        "ai.mode": "text",
    }
    assert dict(durations[0].attributes)["outcome"] == "ok"
    assert tokens[0].attributes["direction"] == "output" and tokens[0].value == 5


async def test_json_mode_is_labelled(sink: Sink) -> None:
    service = _service(_ok('{"a": 1}'), OLLAMA_SUPPORTS_JSON=True)

    await service.generate("x", want_json=True)

    assert sink.spans.get_finished_spans()[0].attributes["ai.mode"] == "json"


async def test_failure_marks_the_span_and_counts_the_typed_error(sink: Sink) -> None:
    service = _service(_ok(""))

    with pytest.raises(EmptyCompletionError):
        await service.generate(SECRET_PROMPT)

    spans = sink.spans.get_finished_spans()
    errors = sink.points("waddles.ai.provider.errors")
    print(f"telemetry check: spans={len(spans)} errors={len(errors)}")
    assert len(spans) == 1 and len(errors) == 1
    assert spans[0].status.status_code == trace.StatusCode.ERROR
    assert errors[0].attributes["error.code"] == "EmptyCompletionError"
    assert sink.points("waddles.ai.provider.tokens") == []


async def test_http_failure_counts_with_the_exception_class(sink: Sink) -> None:
    service = _service(lambda r: httpx.Response(500, json={"error": "x"}))

    with pytest.raises(httpx.HTTPStatusError):
        await service.generate("x")

    assert sink.points("waddles.ai.provider.errors")[0].attributes["error.code"] == (
        "HTTPStatusError"
    )


async def test_nothing_sensitive_reaches_telemetry(sink: Sink) -> None:
    await _service(_ok("a private reply")).generate(SECRET_PROMPT)

    rendered = repr([dict(s.attributes) for s in sink.spans.get_finished_spans()])
    for name in ("waddles.ai.provider.duration", "waddles.ai.provider.tokens"):
        rendered += repr([dict(p.attributes) for p in sink.points(name)])
    assert SECRET_PROMPT not in rendered and "a private reply" not in rendered
    assert json.dumps(rendered)  # printable


async def test_unimplemented_provider_is_counted_then_raises(sink: Sink) -> None:
    service = AIProviderService(FakeConfig(AI_PROVIDER="openai"))  # type: ignore[arg-type]

    with pytest.raises(NotImplementedError):
        await service.generate("x")

    errors = sink.points("waddles.ai.provider.errors")
    assert errors[0].attributes["error.code"] == "NotImplementedError"
    assert errors[0].attributes["ai.provider"] == "openai"
