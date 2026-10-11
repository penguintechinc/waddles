"""OpenTelemetry metrics/traces and PII-safe failure logging for the connection framework.

Logs, metrics and traces are all mandatory (critical-rules.md Observability). The rules here:

* Metric labels are bounded enums only: provider id, operation, outcome, provider-call kind. Never
  a tenant, community, connection id, channel, account name or token.
* Spans carry the provider id and operation. Exception *messages* are never recorded on a span
  (third-party text can embed a token or URL); only the exception type name is.
* :func:`log_failure` routes every exception through ``flask_core.safe_logging`` -- type, code,
  category and a frame-only traceback, never ``str(exc)``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final, LiteralString

from flask_core.safe_logging import log_exc_safe
from opentelemetry import metrics, trace
from opentelemetry.trace import Span, Status, StatusCode

from services.provider_connections.errors import ProviderConnectionError

_NAME: Final = "waddles.hub_api.provider_connections"
_meter = metrics.get_meter(_NAME)
_tracer = trace.get_tracer(_NAME)

operation_counter = _meter.create_counter(
    "waddles_provider_connection_ops_total",
    description="Connection API / data-plane operations by provider, operation and outcome.",
)
operation_duration = _meter.create_histogram(
    "waddles_provider_connection_op_duration_seconds",
    unit="s",
    description="End-to-end latency of a connection operation, by provider and operation.",
)
provider_call_duration = _meter.create_histogram(
    "waddles_provider_connection_provider_call_duration_seconds",
    unit="s",
    description="Outbound provider call latency (validate / resolve / refresh), by provider.",
)
token_refresh_counter = _meter.create_counter(
    "waddles_provider_connection_token_refresh_total",
    description="Token refreshes by provider and outcome (refreshed, rejected, unavailable...).",
)
dataplane_list_size = _meter.create_histogram(
    "waddles_provider_connection_dataplane_list_size",
    description="Number of bindings returned to the data plane per list call, by provider.",
)


@contextmanager
def operation_span(provider: str, operation: str) -> Iterator[Span]:
    """Span + duration histogram + outcome counter around one framework operation.

    The outcome is ``ok`` when the block completes, otherwise the exception's class name (a
    bounded set of our own error types; unknown exceptions collapse to ``error``). The exception
    still propagates.
    """
    started = time.perf_counter()
    outcome = "ok"
    # record_exception / set_status_on_exception are OFF: the SDK would otherwise attach the
    # exception message and stack to the span, and third-party text can carry a token or URL.
    with _tracer.start_as_current_span(
        f"provider_connections.{operation}",
        attributes={"provider": provider, "operation": operation},
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except BaseException as exc:
            outcome = _outcome_label(exc)
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            raise
        finally:
            labels = {"provider": provider, "operation": operation}
            operation_counter.add(1, {**labels, "outcome": outcome})
            operation_duration.record(time.perf_counter() - started, labels)


def _outcome_label(exc: BaseException) -> str:
    """Bounded outcome label: our own error ``code`` when it is one, else ``error``."""
    if isinstance(exc, ProviderConnectionError):
        return exc.code
    return "error"


@contextmanager
def provider_call(provider: str, call: str) -> Iterator[None]:
    """Time one outbound provider call into the latency histogram (even when it raises)."""
    started = time.perf_counter()
    try:
        yield
    finally:
        provider_call_duration.record(
            time.perf_counter() - started, {"provider": provider, "call": call}
        )


def log_failure(
    logger: logging.Logger,
    event: LiteralString,
    exc: BaseException,
    *,
    level: int = logging.ERROR,
    **fields: str | int,
) -> None:
    """Log ``exc`` as ``event`` with type, safe code/category and frames -- never its message.

    ``fields`` must be identifiers and counts (provider id, connection id, status codes), never
    user input, account names or tokens. Our own :class:`ProviderConnectionError` additionally
    contributes its fixed ``error_code`` (application-authored, safe by construction).
    """
    if isinstance(exc, ProviderConnectionError):
        fields = {**fields, "error_code": exc.code}
    log_exc_safe(logger, level, event, exc, **fields)
