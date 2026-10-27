"""OpenTelemetry instruments for envelope encryption / external KMS.

Histograms for latency (KMS call duration -- the most useful signal for a
third-party dependency), counters for events. Labels are bounded sets
(operation, provider, outcome, reason) plus ``tenant_id`` only on the
failure counter, where "which tenant revoked their key" is exactly the
alert an operator needs and cardinality is capped by the tenant count
(~300). No key material, ARN, or PII ever becomes a label or attribute.

The OTLP destination is configured by the standard ``OTEL_EXPORTER_*``
environment variables, never here; a dead exporter cannot affect a request.
"""

from __future__ import annotations

from opentelemetry import metrics, trace

_meter = metrics.get_meter("waddles.hub_api.envelope")
tracer = trace.get_tracer("waddles.hub_api.envelope")

_operations = _meter.create_counter(
    "waddles_envelope_operations_total",
    description="Envelope encrypt/decrypt/rotate/rewrap operations, by operation and result.",
)
_kms_duration = _meter.create_histogram(
    "waddles_envelope_kms_call_duration_seconds",
    unit="s",
    description="Latency of calls to an external KMS provider, by provider/operation/outcome.",
)
_kms_failures = _meter.create_counter(
    "waddles_envelope_kms_failures_total",
    description="Key-provider failures, by KEK kind, reason and tenant.",
)
_cache = _meter.create_counter(
    "waddles_envelope_dek_cache_total",
    description="Tenant data-key cache lookups, by result (hit/miss/stale).",
)
_rewrap_rows = _meter.create_counter(
    "waddles_envelope_rewrap_rows_total",
    description="DEK rows processed by a re-wrap, by result (rewrapped/current/failed).",
)


def record_operation(operation: str, result: str) -> None:
    """Count one envelope operation outcome."""
    _operations.add(1, {"operation": operation, "result": result})


def record_kms_call(provider: str, operation: str, outcome: str, seconds: float) -> None:
    """Record one KMS call's latency under its outcome label."""
    _kms_duration.record(
        seconds, {"provider": provider, "operation": operation, "outcome": outcome}
    )


def record_kms_failure(kek_kind: str, reason: str, tenant_id: int) -> None:
    """Count one key-provider failure (reason: access_denied / unavailable / rejected)."""
    _kms_failures.add(1, {"kek_kind": kek_kind, "reason": reason, "tenant_id": str(tenant_id)})


def record_cache(result: str) -> None:
    """Count one data-key cache lookup (hit / miss / stale)."""
    _cache.add(1, {"result": result})


def record_rewrap_row(result: str) -> None:
    """Count one DEK row processed by a re-wrap."""
    _rewrap_rows.add(1, {"result": result})
