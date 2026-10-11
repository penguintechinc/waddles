"""SUCCESS-path regression tests: analytics-core `logger.audit(...)` against the REAL AAALogger.

# regression: `AAALogger.audit(action, user, community, result, **extras)`. Every audit call in
# analytics-core passed a message as the first positional AND `action=...` (and no `user` /
# `community`), so each raised `TypeError: audit() got multiple values for argument 'action'`
# *after* the work had succeeded -- `HealthService.calculate_health_score` stored its daily
# snapshot and then 500'd; the `bot_score_calculator` batch job finished every community and
# then crashed instead of returning its summary.
#
# The bug only exists on the happy path, so each test drives the real public method to its
# success return through a real `AAALogger` (the `capture` fixture -- never a no-op logger,
# never a permissive shim) and asserts (a) it does not raise, and (b) the audit record carries
# the actor, the community and the result.  Only the data-gathering seams (DAL queries, feature
# flag) are stubbed; the method under test, its audit call and the logger are all real.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import date
from typing import TYPE_CHECKING, Any

import pytest

from jobs import bot_score_calculator
from services import health_service
from services.analytics_service import AnalyticsService
from services.bad_actor_service import BadActorService
from services.bot_score_service import BotScoreService
from services.funnel_service import FunnelService
from services.health_service import HealthService
from services.retention_service import RetentionService

if TYPE_CHECKING:
    from conftest import Capture

COMMUNITY_ID = 42


def _const(value: Any) -> Callable[..., Any]:
    """Return an async stub that ignores its arguments and yields `value`."""

    async def _stub(*args: Any, **kwargs: Any) -> Any:
        return value

    return _stub


def _audit_records(capture: Capture) -> list[logging.LogRecord]:
    """Return the AUDIT-category records the logger emitted."""
    return [
        r
        for r in capture.collector.records
        if getattr(r, "event_type", None) == "AUDIT"
    ]


def _only_audit(capture: Capture) -> logging.LogRecord:
    """Return the single AUDIT record (fails if the success path logged none or several)."""
    records = _audit_records(capture)
    assert len(records) == 1, f"expected exactly 1 AUDIT record, got {len(records)}"
    return records[0]


def _assert_actor_community_result(
    record: logging.LogRecord, *, action: str, user: str, community: str, result: str = "SUCCESS"
) -> None:
    """Assert the AAA fields the (previously raising) call must now carry."""
    assert record.action == action  # type: ignore[attr-defined]
    assert record.user == user  # type: ignore[attr-defined]
    assert record.community == community  # type: ignore[attr-defined]
    assert record.result == result  # type: ignore[attr-defined]


class StubDal:
    """Sync DAL double: every query is a no-op returning no rows."""

    def executesql(self, *args: Any, **kwargs: Any) -> list[Any]:
        """Return no rows."""
        return []

    def commit(self) -> None:
        """No-op commit."""


class AsyncStubDal:
    """Async DAL double (the `await dal.execute(...)` style `BotScoreService` uses)."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        """Hold the rows every `execute` returns."""
        self.rows = rows or []

    async def execute(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        """Return the configured rows."""
        return self.rows


def _premium(service: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the service's premium gate pass so the audited success branch is reached."""
    monkeypatch.setattr(service, "_get_config", _const({"is_premium": True}))


async def test_health_score_success_does_not_raise(
    capture: Capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`calculate_health_score` returns its result (previously: TypeError after the snapshot)."""
    monkeypatch.setattr(health_service, "feature_enabled", _const(True))
    service = HealthService(StubDal(), capture.logger)
    _premium(service, monkeypatch)
    for factor in (
        "_calculate_activity_consistency",
        "_calculate_engagement_rate",
        "_calculate_retention",
        "_calculate_message_distribution",
    ):
        monkeypatch.setattr(service, factor, _const(80.0))
    monkeypatch.setattr(service, "_store_health_snapshot", _const(None))

    result = await service.calculate_health_score(COMMUNITY_ID)

    assert result["health_grade"]
    assert result["overall_score"] == 80.0
    assert result["community_id"] == COMMUNITY_ID
    _assert_actor_community_result(
        _only_audit(capture),
        action="calculate_health_score",
        user="system",
        community=str(COMMUNITY_ID),
    )
    assert not capture.error_records()


async def test_bot_score_success_does_not_raise(
    capture: Capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`calculate_score` logs its audit record instead of raising after the upsert."""
    service = BotScoreService(AsyncStubDal(), capture.logger)
    monkeypatch.setattr(service, "_get_community_size_category", _const("small"))
    for component in (
        "_calculate_bad_actor_score",
        "_calculate_reputation_score",
        "_calculate_security_score",
        "_calculate_ai_behavioral_score",
    ):
        monkeypatch.setattr(service, component, _const(90))
    monkeypatch.setattr(service, "_upsert_bot_score", _const(None))

    result = await service.calculate_score(COMMUNITY_ID)

    assert result["overall_score"] == 90
    record = _only_audit(capture)
    _assert_actor_community_result(
        record, action="calculate_score", user="system", community=str(COMMUNITY_ID)
    )
    assert record.additional["score"] == 90  # type: ignore[attr-defined]
    assert not capture.error_records()


async def test_suspected_bots_query_success_does_not_raise(capture: Capture) -> None:
    """`get_suspected_bots` (read path) audits with a system actor and the row count."""
    service = BotScoreService(AsyncStubDal(), capture.logger)

    bots = await service.get_suspected_bots(COMMUNITY_ID)

    assert bots == []
    record = _only_audit(capture)
    _assert_actor_community_result(
        record, action="get_suspected_bots", user="system", community=str(COMMUNITY_ID)
    )
    assert record.additional["count"] == 0  # type: ignore[attr-defined]


async def test_mark_bot_reviewed_audits_the_reviewer_as_actor(capture: Capture) -> None:
    """The moderator who reviewed is the audit actor (not 'system'), as an id, never a name."""
    now = date(2026, 1, 1)
    row = {
        "community_id": COMMUNITY_ID,
        "hub_user_id": 7,
        "platform_user_id": "p-7",
        "platform_username": "someone",
        "confidence_score": 91,
        "is_false_positive": True,
        "reviewed_by": 1234,
        "reviewed_at": now,
        "detected_at": now,
        "updated_at": now,
    }
    service = BotScoreService(AsyncStubDal([row]), capture.logger)

    result = await service.mark_bot_reviewed(
        COMMUNITY_ID, bot_id=9, is_false_positive=True, reviewer_id=1234
    )

    assert result["reviewed_by"] == 1234
    record = _only_audit(capture)
    _assert_actor_community_result(
        record, action="mark_bot_reviewed", user="1234", community=str(COMMUNITY_ID)
    )
    assert record.additional["bot_id"] == 9  # type: ignore[attr-defined]


async def test_bad_actor_detection_success_does_not_raise(
    capture: Capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`detect_bad_actors` returns its summary instead of raising after storing alerts."""
    service = BadActorService(StubDal(), capture.logger)
    _premium(service, monkeypatch)
    for detector in ("_detect_spam_bursts", "_detect_duplicates", "_detect_coordinated_activity"):
        monkeypatch.setattr(service, detector, _const([]))
    monkeypatch.setattr(service, "_deduplicate_and_store_alerts", _const([]))

    result = await service.detect_bad_actors(COMMUNITY_ID)

    assert "error" not in result
    record = _only_audit(capture)
    _assert_actor_community_result(
        record, action="detect_bad_actors", user="system", community=str(COMMUNITY_ID)
    )
    assert record.additional["alerts_count"] == 0  # type: ignore[attr-defined]


async def test_funnel_tracking_success_does_not_raise(
    capture: Capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`track_engagement_funnel` returns after storing the funnel (previously: TypeError)."""
    service = FunnelService(StubDal(), capture.logger)
    _premium(service, monkeypatch)
    monkeypatch.setattr(service, "_calculate_funnel_steps", _const([]))
    monkeypatch.setattr(service, "_store_funnel_data", _const(None))

    result = await service.track_engagement_funnel(COMMUNITY_ID)

    assert result["funnel_name"] == "new_user_activation"
    record = _only_audit(capture)
    _assert_actor_community_result(
        record, action="track_engagement_funnel", user="system", community=str(COMMUNITY_ID)
    )
    assert record.additional["funnel_name"] == "new_user_activation"  # type: ignore[attr-defined]


async def test_retention_success_does_not_raise(
    capture: Capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`calculate_retention` returns after storing the cohorts (previously: TypeError)."""
    service = RetentionService(StubDal(), capture.logger)
    _premium(service, monkeypatch)
    monkeypatch.setattr(service, "_calculate_cohorts", _const([]))
    monkeypatch.setattr(service, "_store_cohort_data", _const(None))

    result = await service.calculate_retention(COMMUNITY_ID, "week")

    assert result["cohort_type"] == "week"
    record = _only_audit(capture)
    _assert_actor_community_result(
        record, action="calculate_retention", user="system", community=str(COMMUNITY_ID)
    )
    assert record.additional["cohort_period"] == "week"  # type: ignore[attr-defined]


class ConfigDal(StubDal):
    """DAL double whose `analytics_config` SELECT reports one stored config row."""

    def executesql(self, sql: str, *args: Any, **kwargs: Any) -> list[Any]:
        """Return a config row for the config SELECT, nothing for writes."""
        if sql.lstrip().upper().startswith("SELECT"):
            return [(False, True, False, False, False, 30)]
        return []


async def test_update_config_success_does_not_raise(capture: Capture) -> None:
    """`update_config` commits the UPDATE and then returns (previously: TypeError post-commit)."""
    service = AnalyticsService(ConfigDal(), capture.logger)

    result = await service.update_config(COMMUNITY_ID, {"basic_stats_enabled": True})

    assert result["community_id"] == COMMUNITY_ID
    _assert_actor_community_result(
        _only_audit(capture), action="update_config", user="system", community=str(COMMUNITY_ID)
    )


async def test_process_events_success_audits_without_a_community(capture: Capture) -> None:
    """Router-sent event batches span communities: community is empty, never 'None'."""
    service = AnalyticsService(StubDal(), capture.logger)

    result = await service.process_events([{"a": 1}, {"b": 2}])

    assert result == {"processed": 2, "status": "success"}
    record = _only_audit(capture)
    _assert_actor_community_result(record, action="process_events", user="system", community="")
    assert record.additional["events_count"] == 2  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("community_id", "expected_community"), [(COMMUNITY_ID, str(COMMUNITY_ID)), (None, "")]
)
async def test_run_aggregation_success_does_not_raise(
    capture: Capture, community_id: int | None, expected_community: str
) -> None:
    """Aggregation (one community or all) audits; 'all communities' is empty, not 'None'."""
    service = AnalyticsService(StubDal(), capture.logger)

    result = await service.run_aggregation(community_id, force=True)

    assert result["status"] == "queued"
    record = _only_audit(capture)
    _assert_actor_community_result(
        record, action="run_aggregation", user="system", community=expected_community
    )
    assert record.additional["force"] is True  # type: ignore[attr-defined]


class JobDal(StubDal):
    """DAL double listing `community_ids` as the active communities."""

    def __init__(self, community_ids: list[int]) -> None:
        """Hold the ids the community listing returns."""
        self.community_ids = community_ids

    def executesql(self, sql: str, *args: Any, **kwargs: Any) -> list[Any]:
        """Return one `(id,)` row per community for the listing query."""
        if "FROM communities" in sql:
            return [(cid,) for cid in self.community_ids]
        return []


@pytest.mark.parametrize("community_ids", [[], [5, 6]])
def test_bot_score_batch_job_returns_its_summary(
    capture: Capture, monkeypatch: pytest.MonkeyPatch, community_ids: list[int]
) -> None:
    """The nightly batch returns its summary instead of raising at the completion audit.

    # regression: the completion `logger.audit("Bot score calculation completed",
    # action="batch_complete", ...)` raised TypeError after every community was scored, so
    # `main()` reported the whole job as failed (exit 1).
    """
    monkeypatch.setattr(bot_score_calculator, "logger", capture.logger)
    monkeypatch.setattr(
        bot_score_calculator, "init_database", lambda _url: JobDal(community_ids)
    )

    async def _score(self: Any, community_id: int) -> dict[str, Any]:
        return {"community_id": community_id}

    monkeypatch.setattr(BotScoreService, "calculate_score", _score)

    summary = asyncio.run(bot_score_calculator.calculate_all_communities())

    assert summary["total"] == len(community_ids)
    assert summary["success"] == len(community_ids)
    assert summary["errors"] == 0
    record = _only_audit(capture)
    _assert_actor_community_result(record, action="batch_complete", user="system", community="")
    assert record.additional["total"] == len(community_ids)  # type: ignore[attr-defined]
    assert not capture.error_records()


def test_bot_score_batch_job_main_exits_zero(
    capture: Capture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`main()` -- the cron entry point -- exits 0 when the batch succeeds."""
    monkeypatch.setattr(bot_score_calculator, "logger", capture.logger)
    monkeypatch.setattr(bot_score_calculator, "init_database", lambda _url: JobDal([]))

    exit_code = bot_score_calculator.main()

    assert exit_code == 0
    assert "Bot score calculation complete" in capsys.readouterr().out
