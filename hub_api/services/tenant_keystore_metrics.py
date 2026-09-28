"""Per-service tenant-fanout observability for the tenant-DEK broker (spec Sec5e).

`svc-ingest`/`svc-process` are shared, multi-tenant processes by design
-- a compromised instance can request the `ingest-stream` key for any
tenant it chooses, one at a time (spec Sec5e's accepted trust tradeoff).
This module is the compensating *detective* control: it tracks, per
`service_id`, the distinct set of `tenant_id`s observed and emits a
counter plus a coarse anomaly-threshold alert -- it does not, and cannot,
prevent a compromised instance from touching many tenants.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from services.bundle_telemetry import get_meter

#: `KEYSTORE_TENANT_FANOUT_THRESHOLD` -- env-overridable; svc-ingest/
#: svc-process fanning out across many tenants is expected by design, so
#: this is a coarse anomaly signal, not a hard block (spec Sec5e).
DEFAULT_FANOUT_THRESHOLD = 50


def _fanout_threshold() -> int:
    return int(os.environ.get("KEYSTORE_TENANT_FANOUT_THRESHOLD", str(DEFAULT_FANOUT_THRESHOLD)))


_meter = get_meter()
_distinct_tenants_counter = _meter.create_counter(
    "waddles_hub_keystore_distinct_tenants_total",
    description=(
        "Incremented once per newly-observed (service_id, tenant_id) pair -- "
        "a proxy for tenant-fanout breadth, not raw call volume (spec Sec5e)."
    ),
)
_fanout_anomaly_counter = _meter.create_counter(
    "waddles_hub_keystore_tenant_fanout_anomaly_total",
    description=(
        "Incremented when one service_id's distinct-tenant count exceeds "
        "KEYSTORE_TENANT_FANOUT_THRESHOLD (spec Sec5e) -- coarse anomaly signal."
    ),
)


@dataclass(slots=True)
class ServiceTenantFanoutTracker:
    """Process-local `service_id -> {tenant_id}` tracker, OTel-instrumented.

    Not persisted/shared across replicas -- each hub-api process tracks
    its own view, which is sufficient for a coarse anomaly signal (spec
    Sec5e); a cluster-wide view would need a shared store, out of scope
    for this PR.
    """

    _seen: dict[str, set[int]] = field(default_factory=dict)
    _alerted: set[str] = field(default_factory=set)
    logger: object | None = None

    def record(self, service_id: str, tenant_id: int) -> int:
        """Record one key fetch; return the service's current distinct-tenant count.

        Emits the counter only on first-seen `(service_id, tenant_id)`;
        fires the anomaly alert (metric + log, once per service until it
        drops back under threshold) the first time the threshold is
        crossed.
        """
        tenants = self._seen.setdefault(service_id, set())
        is_new = tenant_id not in tenants
        tenants.add(tenant_id)
        if is_new:
            _distinct_tenants_counter.add(1, {"service_id": service_id})

        count = len(tenants)
        threshold = _fanout_threshold()
        if count > threshold and service_id not in self._alerted:
            self._alerted.add(service_id)
            _fanout_anomaly_counter.add(1, {"service_id": service_id})
            if self.logger is not None:
                self.logger.system(  # type: ignore[attr-defined]
                    "keystore tenant-fanout anomaly threshold exceeded",
                    action="internal.keys.tenant_dek.fanout_anomaly",
                    result="DEGRADED",
                    extra={
                        "service_id": service_id,
                        "distinct_tenants": count,
                        "threshold": threshold,
                    },
                )
        elif count <= threshold:
            self._alerted.discard(service_id)
        return count
