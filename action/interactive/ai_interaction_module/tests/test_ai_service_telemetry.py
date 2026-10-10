"""OTel emission for `AIService.generate_response` -- spans + metrics actually received, with counts.

A real in-process OTel SDK (in-memory exporters) stands in for the OTLP sink; zero received is a
failure, never a pass. The canned fallback is a counted degradation, not a silent one.
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

from services import ai_service as ai_service_module
from services.ai_service import AIService
from services.ollama_provider import OllamaProvider

SECRET_MESSAGE = "synthetic confidential chat message"


class Sink:
    """Local OTel test sink: finished spans + metric data points."""

    def __init__(self) -> None:
        self.spans = InMemorySpanExporter()
        self.reader = InMemoryMetricReader()
        tracer_provider = TracerProvider()
        tracer_provider.add_span_processor(SimpleSpanProcessor(self.spans))
        ai_service_module.telemetry.use_providers(
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
    monkeypatch.setattr(ai_service_module.telemetry, "_instruments", None)  # restored on teardown
    return Sink()


def _patch_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    transport = httpx.MockTransport(handler)
    original_init = httpx.AsyncClient.__init__

    def patched_init(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


def _chat(text: str) -> Any:
    body = {"message": {"role": "assistant", "content": text}, "done_reason": "stop"}
    return lambda request: httpx.Response(200, json=body)


async def test_good_reply_emits_a_span_and_an_ok_duration_point(
    sink: Sink, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_transport(monkeypatch, _chat("Welcome aboard!"))
    service = AIService(provider=OllamaProvider(model="gemma4:e2b"))

    reply = await service.generate_response(SECRET_MESSAGE, "chatMessage", "u1", "twitch", {})

    spans = sink.spans.get_finished_spans()
    durations = sink.points("waddles.ai.provider.duration")
    print(f"telemetry check: spans={len(spans)} durations={len(durations)}")
    assert reply == "Welcome aboard!"
    assert len(spans) == 1 and len(durations) == 1
    assert dict(spans[0].attributes) == {
        "ai.provider": "ollama",
        "ai.model": "gemma4:e2b",
        "ai.mode": "text",
    }
    assert dict(durations[0].attributes)["outcome"] == "ok"
    assert sink.points("waddles.ai.provider.errors") == []


async def test_blank_reply_is_a_counted_degradation_not_a_silent_one(
    sink: Sink, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_transport(monkeypatch, _chat(""))
    service = AIService(provider=OllamaProvider(model="gemma4:e2b"))

    reply = await service.generate_response("hi", "chatMessage", "u1", "twitch", {})

    errors = sink.points("waddles.ai.provider.errors")
    print(f"telemetry check: errors={len(errors)}")
    assert reply == service._get_fallback_response("chatMessage", {})
    assert len(errors) == 1 and errors[0].attributes["error.code"] == "no_reply"
    assert dict(sink.points("waddles.ai.provider.duration")[0].attributes)["outcome"] == "error"


async def test_provider_exception_is_counted_with_its_class(sink: Sink) -> None:
    class Exploding:
        model = "m"

        async def generate_response(self, *args: Any) -> str:
            raise RuntimeError("provider blew up")

    service = AIService(provider=Exploding())  # type: ignore[arg-type]

    reply = await service.generate_response("hi", "subscription", "u1", "twitch", {})

    assert reply == service._get_fallback_response("subscription", {})
    errors = sink.points("waddles.ai.provider.errors")
    assert errors[0].attributes["error.code"] == "RuntimeError"
    assert errors[0].attributes["ai.provider"] == "exploding"
    assert sink.spans.get_finished_spans()[0].status.status_code == trace.StatusCode.ERROR


async def test_no_chat_text_or_user_id_reaches_telemetry(
    sink: Sink, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_transport(monkeypatch, _chat("a private reply"))
    service = AIService(provider=OllamaProvider())

    await service.generate_response(SECRET_MESSAGE, "chatMessage", "user-uuid-9f8e", "twitch", {})

    rendered = repr([dict(s.attributes) for s in sink.spans.get_finished_spans()])
    rendered += repr([dict(p.attributes) for p in sink.points("waddles.ai.provider.duration")])
    for secret in (SECRET_MESSAGE, "user-uuid-9f8e", "a private reply"):
        assert secret not in rendered
