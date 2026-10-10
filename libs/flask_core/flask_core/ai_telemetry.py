"""OTel spans + metrics for model-provider (LLM) calls, shared by every AI-calling service.

API only -- `opentelemetry.trace` / `opentelemetry.metrics`; no vendor library and no exporter
wiring here. Where the data goes is a deployment concern (`OTEL_EXPORTER_OTLP_ENDPOINT` and the
other standard OTLP env vars); with no provider configured every call resolves to the API's no-op
implementation. Telemetry failure is never a request failure: nothing in this module raises into
the caller.

Signals (no PII, no prompt/response text, no secrets in any attribute or label):

* span ``ai.provider.generate`` -- attrs ``ai.provider``, ``ai.model``, ``ai.mode`` (``text`` or
  ``json``) and optionally ``ai.tier``; ERROR status + recorded exception if the call fails.
* histogram ``waddles.ai.provider.duration`` (ms) -- one point per call; labels provider, model,
  mode, outcome (and tier when set).
* counter ``waddles.ai.provider.tokens`` -- provider-reported usage; label ``direction``
  input/output.
* counter ``waddles.ai.provider.errors`` -- failed calls; label ``error.code`` (a class name or
  typed error code, never a message).

Usage::

    telemetry = AITelemetry("waddles.my_service.ai")
    with telemetry.span(provider="ollama", model=model, mode="text"):
        ...call the model...
    telemetry.record_call(provider="ollama", model=model, mode="text", duration_ms=12.0,
                          input_tokens=7, output_tokens=3)
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from opentelemetry import metrics, trace

from flask_core.db_errors import describe_db_error

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Instruments:
    """The tracer and metric instruments, built once against whichever provider is current."""

    tracer: Any
    duration: Any
    tokens: Any
    errors: Any


class AITelemetry:
    """Per-service handle on the shared AI-call instruments (lazy; safe with no provider set)."""

    def __init__(self, scope: str) -> None:
        """Bind to an instrumentation scope name, e.g. ``waddles.hub_api.ai_routing``."""
        self._scope = scope
        self._instruments: Instruments | None = None

    def use_providers(self, tracer_provider: Any = None, meter_provider: Any = None) -> Instruments:
        """(Re)build the instruments against explicit providers (tests, embedding apps)."""
        meter = metrics.get_meter(self._scope, meter_provider=meter_provider)
        self._instruments = Instruments(
            tracer=trace.get_tracer(self._scope, tracer_provider=tracer_provider),
            duration=meter.create_histogram(
                "waddles.ai.provider.duration", unit="ms", description="Model-provider call latency"
            ),
            tokens=meter.create_counter(
                "waddles.ai.provider.tokens",
                unit="{token}",
                description="Provider-reported token usage",
            ),
            errors=meter.create_counter(
                "waddles.ai.provider.errors",
                unit="{call}",
                description="Failed model-provider calls",
            ),
        )
        return self._instruments

    def instruments(self) -> Instruments:
        """The instruments, created on first use against the process-global providers."""
        if self._instruments is None:
            return self.use_providers()
        return self._instruments

    @contextmanager
    def span(
        self, *, provider: str, model: str, mode: str, tier: str | None = None
    ) -> Iterator[Any]:
        """Span one provider call.

        The OTel API records the exception and sets ERROR status when the body raises, then lets
        it propagate. If telemetry setup itself fails the body still runs, un-spanned (`None`).
        """
        try:
            tracer = self.instruments().tracer
        except Exception as exc:  # telemetry must never fail the request
            logger.warning("ai_telemetry_failed %s", describe_db_error(exc))
            yield None
            return
        with tracer.start_as_current_span("ai.provider.generate") as span:
            span.set_attribute("ai.provider", provider)
            span.set_attribute("ai.model", model)
            span.set_attribute("ai.mode", mode)
            if tier is not None:
                span.set_attribute("ai.tier", tier)
            yield span

    def record_call(
        self,
        *,
        provider: str,
        model: str,
        mode: str,
        duration_ms: float,
        input_tokens: int = 0,
        output_tokens: int = 0,
        error_code: str | None = None,
        tier: str | None = None,
    ) -> None:
        """Record one finished call's histogram point and counters. Never raises."""
        try:
            inst = self.instruments()
            labels = {
                "ai.provider": provider,
                "ai.model": model,
                "ai.mode": mode,
                "outcome": "error" if error_code else "ok",
            }
            if tier is not None:
                labels["ai.tier"] = tier
            inst.duration.record(duration_ms, labels)
            if error_code:
                inst.errors.add(1, {**labels, "error.code": error_code})
                return
            if input_tokens:
                inst.tokens.add(input_tokens, {**labels, "direction": "input"})
            if output_tokens:
                inst.tokens.add(output_tokens, {**labels, "direction": "output"})
        except Exception as exc:  # telemetry must never fail the request
            logger.warning("ai_telemetry_failed %s", describe_db_error(exc))
