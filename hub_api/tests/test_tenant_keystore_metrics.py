"""`services/tenant_keystore_metrics.py` -- spec Sec5e tenant-fanout observability."""

from __future__ import annotations

import pytest

from services.tenant_keystore_metrics import ServiceTenantFanoutTracker


@pytest.fixture(autouse=True)
def _low_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEYSTORE_TENANT_FANOUT_THRESHOLD", "3")


class _FakeLogger:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def system(self, message: str, **kwargs: object) -> None:
        self.calls.append({"message": message, **kwargs})


def test_first_call_for_a_tenant_returns_count_one() -> None:
    tracker = ServiceTenantFanoutTracker()
    assert tracker.record("svc-ingest", 1) == 1


def test_repeated_calls_for_the_same_tenant_do_not_grow_the_distinct_count() -> None:
    tracker = ServiceTenantFanoutTracker()
    tracker.record("svc-ingest", 1)
    tracker.record("svc-ingest", 1)
    tracker.record("svc-ingest", 1)
    assert tracker.record("svc-ingest", 1) == 1


def test_distinct_tenants_are_tracked_separately_per_service() -> None:
    tracker = ServiceTenantFanoutTracker()
    tracker.record("svc-ingest", 1)
    tracker.record("svc-process", 1)
    tracker.record("svc-process", 2)
    assert tracker.record("svc-ingest", 1) == 1
    assert tracker.record("svc-process", 3) == 3


def test_anomaly_alert_fires_once_threshold_is_crossed() -> None:
    logger = _FakeLogger()
    tracker = ServiceTenantFanoutTracker(logger=logger)
    for tenant_id in range(1, 5):  # threshold is 3 (fixture)
        tracker.record("svc-ingest", tenant_id)

    assert len(logger.calls) == 1
    assert logger.calls[0]["result"] == "DEGRADED"


def test_anomaly_alert_does_not_refire_every_call_past_threshold() -> None:
    logger = _FakeLogger()
    tracker = ServiceTenantFanoutTracker(logger=logger)
    for tenant_id in range(1, 8):
        tracker.record("svc-ingest", tenant_id)

    assert len(logger.calls) == 1  # fired once at the crossing, not every subsequent call


def test_below_threshold_never_alerts() -> None:
    logger = _FakeLogger()
    tracker = ServiceTenantFanoutTracker(logger=logger)
    tracker.record("svc-ingest", 1)
    tracker.record("svc-ingest", 2)
    assert logger.calls == []
