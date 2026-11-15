"""OTel for the platform secret store: counters, gauges, a histogram and spans -- never a value.

API only (`opentelemetry.metrics` / `.trace`); where the data goes is the standard `OTEL_*` env
configuration, and with no provider every call is a no-op. Telemetry failure is never a bootstrap
failure. Labels are limited to the registry `type` name and a fixed `outcome` word; attributes to
ints and those words. No value, fingerprint, ciphertext, key id or exception message (SQL errors can
echo bound parameters) ever reaches a span, a metric label or a log line from here: failed spans
carry the exception CLASS name only.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from typing import Any, Final

from opentelemetry import metrics, trace
from opentelemetry.metrics import CallbackOptions, Observation

logger = logging.getLogger(__name__)

_SCOPE: Final = "waddles.hub_api.platform_secrets"

#: Allowed `outcome` label values (everything else is a programming error, coerced to `other`).
OUTCOMES: Final = frozenset(
    {
        "ok",
        "conflict",
        "rejected_placeholder",
        "rejected_invalid",
        "mismatch",
        "wiped",
        "chain_break",
        "unopenable",
        "inconsistent",
        "error",
        "skipped",
        "restored",
        "escrow_invalid",
        "escrow_tampered",
        "escrow_drift",
        "escrow_ahead",
        "escrow_rollback",
        "restore_blocked",
        "unavailable",
        "retired",
        "replayed",
        "rate_limited",
        "no_active",
        "window_open",
        "version_not_found",
        "not_verify_only",
        "not_system_tenant",
        "not_global_admin",
        "mfa_required",
        "csrf_rejected",
        "bad_request",
        "other",
    }
)

_STATE: dict[str, Any] = {}


def _meter() -> Any:
    """The module meter, created once."""
    if "meter" not in _STATE:
        _STATE["meter"] = metrics.get_meter(_SCOPE)
    return _STATE["meter"]


def _counter(name: str) -> Any:
    """A counter by metric name, created once."""
    if name not in _STATE:
        _STATE[name] = _meter().create_counter(name, unit="1", description=name.replace("_", " "))
    return _STATE[name]


def _histogram(name: str) -> Any:
    """A histogram by metric name, created once."""
    if name not in _STATE:
        _STATE[name] = _meter().create_histogram(name, unit="s", description=name.replace("_", " "))
    return _STATE[name]


def _safe(fn: Callable[[], None]) -> None:
    """Run a telemetry call; a failing exporter must never fail the caller."""
    try:
        fn()
    except Exception:  # noqa: BLE001 -- telemetry failure is never a request failure
        logger.debug("platform secret telemetry call failed", exc_info=False)


def _labels(type_name: str, outcome: str) -> dict[str, str]:
    """The only label set: registry type name + a fixed outcome word."""
    return {"type": type_name, "outcome": outcome if outcome in OUTCOMES else "other"}


def count_generated(type_name: str, outcome: str) -> None:
    """`waddles_platform_secrets_generated_total{type,outcome}`."""
    _safe(
        lambda: _counter("waddles_platform_secrets_generated_total").add(
            1, _labels(type_name, outcome)
        )
    )


def count_imported(type_name: str, outcome: str) -> None:
    """`waddles_platform_secrets_imported_total{type,outcome}` (outcome ok / rejected_*)."""
    _safe(
        lambda: _counter("waddles_platform_secrets_imported_total").add(
            1, _labels(type_name, outcome)
        )
    )


def count_open_failure(type_name: str, outcome: str = "unopenable") -> None:
    """`waddles_platform_secrets_open_failures_total{type,outcome}`."""
    _safe(
        lambda: _counter("waddles_platform_secrets_open_failures_total").add(
            1, _labels(type_name, outcome)
        )
    )


def count_drift(type_name: str, outcome: str) -> None:
    """`waddles_platform_secrets_drift_detected_total{type,outcome}`."""
    _safe(
        lambda: _counter("waddles_platform_secrets_drift_detected_total").add(
            1, _labels(type_name, outcome)
        )
    )


def count_escrow(type_name: str, outcome: str) -> None:
    """`waddles_platform_secrets_escrow_total{type,outcome}` (ok / error / restored / refusals)."""
    _safe(
        lambda: _counter("waddles_platform_secrets_escrow_total").add(
            1, _labels(type_name, outcome)
        )
    )


def count_rotation(type_name: str, outcome: str) -> None:
    """`waddles_platform_secrets_rotations_total{type,outcome}` (ok / retired / refusal words)."""
    _safe(
        lambda: _counter("waddles_platform_secrets_rotations_total").add(
            1, _labels(type_name, outcome)
        )
    )


def observe_open_duration(type_name: str, seconds: float, outcome: str = "ok") -> None:
    """`waddles_platform_secrets_open_duration_seconds{type,outcome}` histogram."""
    _safe(
        lambda: _histogram("waddles_platform_secrets_open_duration_seconds").record(
            seconds, _labels(type_name, outcome)
        )
    )


@contextmanager
def timed_open(type_name: str) -> Iterator[None]:
    """Time one open into the histogram; the outcome is `error` when the body raises."""
    started = time.perf_counter()
    try:
        yield
    except BaseException:
        observe_open_duration(type_name, time.perf_counter() - started, "error")
        raise
    observe_open_duration(type_name, time.perf_counter() - started)


@contextmanager
def span(name: str, **attributes: str | int) -> Iterator[Any]:
    """A `platform_secrets.<name>` span. Attributes are ints/words only; errors record the class.

    `record_exception` is deliberately NOT used: SQLAlchemy and driver messages can embed bound
    parameters (ciphertext), so only the exception class name is attached.
    """
    tracer = trace.get_tracer(_SCOPE)
    # The SDK would otherwise record the exception (message + traceback) and put str(exc) in the
    # status description: both can echo SQL parameters, so both are switched off.
    with tracer.start_as_current_span(
        f"platform_secrets.{name}", record_exception=False, set_status_on_exception=False
    ) as active:
        for key, value in attributes.items():
            _safe(lambda k=key, v=value: active.set_attribute(k, v))
        try:
            yield active
        except BaseException as exc:
            _safe(lambda e=exc: active.set_attribute("error.type", type(e).__name__))
            _safe(lambda: active.set_status(trace.Status(trace.StatusCode.ERROR)))
            raise


def register_cache_gauges(snapshot: Callable[[], Iterable[tuple[str, int, float]]]) -> None:
    """Register `_active_versions` and `_age_seconds` observable gauges over a cache snapshot.

    `snapshot()` yields `(type_name, active_versions, active_age_seconds)` per registry type.
    Registered once per process; later calls are ignored.
    """
    if _STATE.get("gauges"):
        return
    _STATE["gauges"] = True

    def _versions(_: CallbackOptions) -> Iterable[Observation]:
        return [Observation(v, {"type": t}) for t, v, _a in snapshot()]

    def _age(_: CallbackOptions) -> Iterable[Observation]:
        return [Observation(a, {"type": t}) for t, _v, a in snapshot()]

    _safe(
        lambda: _meter().create_observable_gauge(
            "waddles_platform_secrets_active_versions", callbacks=[_versions], unit="1"
        )
    )
    _safe(
        lambda: _meter().create_observable_gauge(
            "waddles_platform_secrets_age_seconds", callbacks=[_age], unit="s"
        )
    )


def reset_for_tests() -> None:
    """Drop cached instruments so a test can install a fresh meter/tracer provider."""
    _STATE.clear()
