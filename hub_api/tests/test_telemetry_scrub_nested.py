"""Regression: PII inside NON-primitive values must be scrubbed after stringification.

The first scrubber ran `scrub_attributes()` on the raw extras and only stringified non-primitive
values afterwards, so `extra={"context": {"note": "mail a@b.c"}}`, an exception object, a
dataclass-like object or a span-event attribute holding such a value was exported unscrubbed.
These tests assert over the RAW bytes the in-process OTLP receiver got (record counts printed;
zero records is a failure) and fail against that old behaviour.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import pytest
from opentelemetry import metrics, trace

import telemetry
from tests.telemetry_support import (
    PII_BEARER,
    PII_EMAIL,
    PII_JWT,
    PII_USERNAME,
    _build_app,
    _enable,
    _Receiver,
    _wait_for,
)

pytestmark = pytest.mark.usefixtures("_clean_otel_env")

USERNAME_TEXT = f"username={PII_USERNAME}"
BEARER_TOKEN = PII_BEARER.split(" ", 1)[1]
SECRETS = (PII_EMAIL, PII_USERNAME, BEARER_TOKEN, PII_JWT)


@dataclass
class _Account:
    """Dataclass-like object whose repr (and so str) embeds the secrets."""

    contact: str
    auth: str


class _Opaque:
    """Plain object whose `__str__` embeds the secrets."""

    def __str__(self) -> str:
        return f"opaque({PII_EMAIL} {USERNAME_TEXT} {PII_BEARER} {PII_JWT})"


def _nested_payloads() -> dict[str, Any]:
    """Non-sensitive key names on purpose: the NESTED values must be what gets scrubbed."""
    text = f"{PII_EMAIL} {USERNAME_TEXT} {PII_BEARER} {PII_JWT}"
    return {
        "context": {"details": [text, {"note": text}], "pair": (text, 1)},
        "failure": ValueError(text),
        "account": _Account(contact=PII_EMAIL, auth=f"{PII_BEARER} {PII_JWT}"),
        "thing": _Opaque(),
        "tags": {text, "plain"},
        "raw": text.encode(),
        "by_email": {PII_EMAIL: "x"},
        "nested_sensitive_key": {"username": PII_USERNAME, "token": PII_JWT},
    }


async def test_log_extras_with_nested_objects_are_scrubbed(
    monkeypatch: pytest.MonkeyPatch, receiver: _Receiver
) -> None:
    _enable(monkeypatch, receiver.endpoint)
    app = _build_app()
    probe = logging.getLogger("hub_api.nested_probe")
    async with app.test_app():
        runtime = app.config["telemetry_runtime"]
        assert runtime.enabled, runtime.reason
        probe.warning("nested extras", extra=_nested_payloads())
        try:
            raise RuntimeError(f"failed for {PII_EMAIL} {USERNAME_TEXT} {PII_BEARER}")
        except RuntimeError:
            probe.exception("with exception", extra={"obj": _Opaque()})
        app.config["logger"].info("aaa nested", detail=_nested_payloads())
        await asyncio.to_thread(runtime.force_flush, 10.0)
        assert await _wait_for(
            lambda: sum("nested" in lr.body.string_value for lr in receiver.log_records()) >= 2
        )

    records = [lr for lr in receiver.log_records() if "nested" in lr.body.string_value]
    wire = receiver.wire_bytes()
    print(f"NESTED-LOGS matching_records={len(records)} wire_bytes={len(wire)}")
    assert len(records) >= 2, "FAIL: zero/too few nested log records received"
    assert b"[redacted]" in wire
    for secret in SECRETS:
        at = wire.find(secret.encode())
        assert at < 0, f"{secret!r} leaked in logs: ...{wire[max(0, at - 60) : at + 40]!r}"


async def test_span_event_record_exception_is_scrubbed(
    monkeypatch: pytest.MonkeyPatch, receiver: _Receiver
) -> None:
    _enable(monkeypatch, receiver.endpoint)
    app = _build_app()
    async with app.test_app():
        runtime = app.config["telemetry_runtime"]
        assert runtime.enabled, runtime.reason
        tracer = trace.get_tracer("nested-scrub-test")
        with tracer.start_as_current_span("nested-event-span") as span:
            span.set_attribute("note", f"{PII_EMAIL} {PII_BEARER}")
            try:
                raise ValueError(
                    f"lookup failed {PII_EMAIL} {USERNAME_TEXT} {PII_BEARER} {PII_JWT}"
                )
            except ValueError as exc:
                span.record_exception(exc, attributes=_nested_payloads())  # type: ignore[arg-type]
            span.add_event("custom", attributes={"context": {"note": PII_EMAIL}})  # type: ignore[dict-item]
        await asyncio.to_thread(runtime.force_flush, 10.0)
        assert await _wait_for(lambda: any(s.name == "nested-event-span" for s in receiver.spans()))

    spans = [s for s in receiver.spans() if s.name == "nested-event-span"]
    events = [e for s in spans for e in s.events]
    wire = receiver.wire_bytes()
    print(f"NESTED-EVENTS spans={len(spans)} events={len(events)} wire_bytes={len(wire)}")
    assert spans and len(events) >= 2, "FAIL: span or its events not received"
    for secret in SECRETS:
        at = wire.find(secret.encode())
        assert at < 0, f"{secret!r} leaked in span events: ...{wire[max(0, at - 60) : at + 40]!r}"


async def test_metric_attribute_values_are_scrubbed(
    monkeypatch: pytest.MonkeyPatch, receiver: _Receiver
) -> None:
    _enable(monkeypatch, receiver.endpoint)
    app = _build_app()
    async with app.test_app():
        runtime = app.config["telemetry_runtime"]
        assert runtime.enabled, runtime.reason
        meter = metrics.get_meter("nested-scrub-test")
        counter = meter.create_counter("nested_scrub_total")
        histogram = meter.create_histogram("nested_scrub_seconds")
        attributes = {
            "customer": f"{PII_EMAIL} {USERNAME_TEXT}",
            "trail": [PII_BEARER, PII_JWT],
            "username": PII_USERNAME,
            "peer": "ok",
        }
        counter.add(1, attributes)
        histogram.record(0.5, attributes)
        await asyncio.to_thread(runtime.force_flush, 10.0)
        assert await _wait_for(
            lambda: any(name.startswith("nested_scrub") for name, _, _ in receiver.metric_points())
        )

    points = [p for p in receiver.metric_points() if p[0].startswith("nested_scrub")]
    wire = receiver.wire_bytes()
    print(f"NESTED-METRICS points={len(points)} wire_bytes={len(wire)}")
    assert len(points) >= 2, "FAIL: zero/too few scrub-test metric points received"
    for secret in SECRETS:
        at = wire.find(secret.encode())
        assert at < 0, f"{secret!r} leaked in metrics: ...{wire[max(0, at - 60) : at + 40]!r}"


def test_scrub_any_contract() -> None:
    text = f"{PII_EMAIL} {USERNAME_TEXT} {PII_BEARER} {PII_JWT}"
    cleaned = str(telemetry.scrub_any(_nested_payloads()))
    for secret in SECRETS:
        assert secret not in cleaned, secret
    # primitives are untouched; None passes through; bool stays bool
    assert telemetry.scrub_any(3) == 3 and telemetry.scrub_any(1.5) == 1.5
    assert telemetry.scrub_any(True) is True and telemetry.scrub_any(None) is None
    # depth and size caps: no blow-ups on hostile input
    deep: Any = "leaf " + PII_EMAIL
    for _ in range(50):
        deep = {"k": deep}
    assert PII_EMAIL not in str(telemetry.scrub_any(deep))
    big = {f"k{i}": text for i in range(10_000)}
    assert len(telemetry.scrub_any(big)) <= telemetry.MAX_ITEMS + 1
    huge = telemetry.scrub_text("x" * 100_000)
    assert len(huge) <= telemetry.MAX_TEXT_CHARS + 32

    class _Hostile:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    assert telemetry.scrub_any(_Hostile()) == "<_Hostile>"
