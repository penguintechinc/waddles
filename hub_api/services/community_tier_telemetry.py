"""OTel instruments for community-tier allocation (logs are emitted by the service via `logging`).

Labels are bounded enums only (tier / result / source). NEVER a tenant, community, user id or
slug: no identifier and no PII in any attribute. A dead exporter never breaks an allocation --
the OTel API no-ops without a configured SDK.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.metrics import CallbackOptions, Observation

_SCOPE = "waddles.hub_api.community_tier"
_meter = metrics.get_meter(_SCOPE)
_tracer = trace.get_tracer(_SCOPE)

allocations = _meter.create_counter(
    "waddles_community_tier_allocations_total",
    description="Community tier allocation attempts, by tier, result and source.",
)
allocation_duration = _meter.create_histogram(
    "waddles_community_tier_allocation_duration_seconds",
    unit="s",
    description="End-to-end community tier allocation latency (lock + count + write), by result.",
)

audit_write_failed = _meter.create_counter(
    "waddles_audit_write_failed_total",
    description="Audit writes that failed after the audited change was committed, by area.",
)

_lock = threading.Lock()
#: ``(tenant_id, tier) -> (used, limit)`` held in memory ONLY to compute the per-tier maximum
#: ratio the gauge reports; tenant ids never leave this dict (no tenant label is ever exported).
_state: dict[tuple[int, str], tuple[int, int]] = {}


def set_utilization(tenant_id: int, tier: str, used: int, limit: int | None) -> None:
    """Record one tenant's pool utilization (used/limit); unlimited or unlicensed pools drop out."""
    with _lock:
        if limit is None or limit <= 0:
            _state.pop((tenant_id, tier), None)
        else:
            _state[(tenant_id, tier)] = (used, limit)


def update_used(tenant_id: int, tier: str, used: int) -> None:
    """Refresh only ``used`` of an already-observed pool (e.g. after a release vacated a slot)."""
    with _lock:
        entry = _state.get((tenant_id, tier))
        if entry is not None:
            _state[(tenant_id, tier)] = (used, entry[1])


def _observe_utilization(_options: CallbackOptions) -> Iterator[Observation]:
    with _lock:
        peak: dict[str, float] = {}
        for (_tenant, tier), (used, limit) in _state.items():
            peak[tier] = max(used / limit, peak.get(tier, 0.0))
    for tier, ratio in peak.items():
        yield Observation(ratio, {"tier": tier})


_meter.create_observable_gauge(
    "waddles_team_pool_utilization",
    callbacks=[_observe_utilization],
    description=(
        "Highest team-pool utilization ratio (used/limit; >1 = overage) among the tenants this "
        "replica has observed, per tier. No tenant label."
    ),
)


@contextmanager
def span(name: str, **attributes: str | int | bool) -> Iterator[Any]:
    """Start a span; attributes must be PII-free scalars (enum names/ints/bools only)."""
    with _tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            current.set_attribute(key, value)
        yield current
