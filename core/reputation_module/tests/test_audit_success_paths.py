"""SUCCESS-path regression: reputation `logger.audit(...)` calls vs the REAL `AAALogger`.

# regression: `AAALogger.audit(action, user, community, result, **extras)`. These audit calls
# passed a message as the first positional (and sometimes `action=` too) and omitted
# `user` / `community` / `result`, so each raised `TypeError` AFTER its DB write had already
# been committed:
#
# * `PolicyEnforcer.enable_auto_ban` / `disable_auto_ban` -- committed the UPDATE, then the
#   broad `except Exception` swallowed the TypeError and returned False ("it failed");
# * `WeightManager.update_weights` -- same, returned False after the UPDATE;
# * `PolicyEnforcer._execute_auto_ban` -- the member was ALREADY banned on the platform and
#   marked inactive in the DB, yet `check_thresholds` reported `success=False`;
# * `EventProcessor.process_moderation_action` -- raised straight out to the caller.
#
# Like `test_reputation_service_audit.py`, these use the real `AAALogger` (never the
# permissive `NullLogger`) and a fake DAL, so they run -- and fail -- in every environment.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from flask_core import setup_aaa_logging

from services.event_processor import EventProcessor, ProcessResult
from services.policy_enforcer import PolicyAction, PolicyEnforcer
from services.weight_manager import WeightManager


class _Collector(logging.Handler):
    """Keeps every record so the test can read the AAA fields back."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def audit_records(self) -> list[logging.LogRecord]:
        return [r for r in self.records if getattr(r, "event_type", None) == "AUDIT"]

    def errors(self) -> list[logging.LogRecord]:
        return [r for r in self.records if r.levelno >= logging.ERROR]


@pytest.fixture
def logger_and_records(tmp_path: Path) -> Any:
    """A real AAALogger writing to `tmp_path`, plus a collector for its records."""
    aaa = setup_aaa_logging("reputation-audit-success", "0.0.0", log_dir=str(tmp_path))
    collector = _Collector()
    aaa.logger.addHandler(collector)
    yield aaa, collector
    aaa.logger.removeHandler(collector)


class _Dal:
    """Fake sync DAL: config SELECT reports a premium community; writes are no-ops."""

    def __init__(self, *, premium: bool = True) -> None:
        self.premium = premium
        self.writes: list[str] = []
        self.commits = 0

    def executesql(self, sql: str, params: list[Any] | None = None) -> list[Any]:
        if sql.lstrip().upper().startswith("SELECT"):
            return [(self.premium,)]
        self.writes.append(sql)
        return []

    def commit(self) -> None:
        self.commits += 1


def _single_audit(collector: _Collector) -> logging.LogRecord:
    records = collector.audit_records()
    assert len(records) == 1, f"expected exactly 1 AUDIT record, got {len(records)}"
    return records[0]


async def test_enable_auto_ban_succeeds_and_audits_the_admin(logger_and_records: Any) -> None:
    """The admin who toggled auto-ban is the actor; the call no longer fails post-commit."""
    logger, collector = logger_and_records
    dal = _Dal()
    enforcer = PolicyEnforcer(WeightManager(dal, logger), dal, logger)

    ok = await enforcer.enable_auto_ban(community_id=7, admin_id=99, threshold=400)

    assert ok is True, "enable_auto_ban reported failure after its UPDATE was committed"
    assert dal.commits == 1
    record = _single_audit(collector)
    assert (record.action, record.user, record.community, record.result) == (
        "enable_auto_ban",
        "99",
        "7",
        "SUCCESS",
    )
    assert record.additional["threshold"] == 400
    assert not collector.errors()


async def test_disable_auto_ban_succeeds_and_audits_the_admin(logger_and_records: Any) -> None:
    """`disable_auto_ban` returns True (previously False after committing) and audits the admin."""
    logger, collector = logger_and_records
    dal = _Dal()
    enforcer = PolicyEnforcer(WeightManager(dal, logger), dal, logger)

    ok = await enforcer.disable_auto_ban(community_id=7, admin_id=99)

    assert ok is True
    record = _single_audit(collector)
    assert (record.action, record.user, record.community, record.result) == (
        "disable_auto_ban",
        "99",
        "7",
        "SUCCESS",
    )
    assert not collector.errors()


async def test_update_weights_succeeds_and_audits_the_admin(logger_and_records: Any) -> None:
    """`update_weights` returns True (previously False after committing) and audits the admin."""
    logger, collector = logger_and_records
    dal = _Dal(premium=True)
    manager = WeightManager(dal, logger)

    ok = await manager.update_weights(community_id=7, weights={"chat_message": 2}, admin_id=99)

    assert ok is True
    assert dal.writes, "the UPDATE should have been issued"
    record = _single_audit(collector)
    assert (record.action, record.user, record.community, record.result) == (
        "update_weights",
        "99",
        "7",
        "SUCCESS",
    )
    assert record.additional["updated_fields"] == ["chat_message"]
    assert not collector.errors()


class _Weights:
    """Weights double: auto-ban on, threshold 300."""

    auto_ban_enabled = True
    auto_ban_threshold = 300


class _WeightManager:
    """WeightManager double that serves `_Weights` (the real manager needs a live config row)."""

    async def get_weights(self, community_id: int) -> _Weights:
        return _Weights()

    def invalidate_cache(self, community_id: int) -> None:
        return None


async def test_auto_ban_reports_success_when_the_ban_went_through(
    logger_and_records: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ban was applied (DB + platform) -- the result must say so, not `success=False`."""
    logger, collector = logger_and_records
    dal = _Dal()
    enforcer = PolicyEnforcer(_WeightManager(), dal, logger)
    sent: list[str] = []

    async def _send_platform_ban(**kwargs: Any) -> None:
        sent.append(kwargs["platform_user_id"])

    monkeypatch.setattr(enforcer, "_send_platform_ban", _send_platform_ban)

    result = await enforcer.check_thresholds(
        community_id=7, user_id=5, platform="twitch", platform_user_id="p-5", current_score=250
    )

    assert sent == ["p-5"], "the platform ban should have been requested"
    assert result.action_taken is PolicyAction.AUTO_BAN
    assert result.success is True, f"auto-ban applied but reported failure: {result.error}"
    record = _single_audit(collector)
    assert (record.action, record.user, record.community, record.result) == (
        "auto_ban",
        "system",
        "7",
        "SUCCESS",
    )
    assert not collector.errors()


async def test_low_score_warning_reports_success(logger_and_records: Any) -> None:
    """A score in the warning band (above the ban threshold) records the warning and audits it."""
    logger, collector = logger_and_records
    dal = _Dal()
    enforcer = PolicyEnforcer(_WeightManager(), dal, logger)

    class _NoRecentWarning(_Dal):
        def executesql(self, sql: str, params: list[Any] | None = None) -> list[Any]:
            return []

    enforcer.dal = _NoRecentWarning()

    result = await enforcer.check_thresholds(
        community_id=7, user_id=5, platform="twitch", platform_user_id="p-5", current_score=400
    )

    assert result.action_taken is PolicyAction.NOTIFY_MODS
    assert result.success is True, f"warning recorded but reported failure: {result.error}"
    record = _single_audit(collector)
    assert (record.action, record.user, record.community, record.result) == (
        "low_score_warning",
        "system",
        "7",
        "WARNED",
    )
    assert not collector.errors()


async def test_moderation_action_processing_audits_the_moderator(
    logger_and_records: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`process_moderation_action` returns its result and audits the moderator as the actor."""
    logger, collector = logger_and_records
    processor = EventProcessor(object(), object(), object(), logger)

    async def _process_event(event_data: dict[str, Any]) -> ProcessResult:
        return ProcessResult(
            success=True,
            community_id=event_data["community_id"],
            user_id=event_data["user_id"],
            event_type=event_data["event_type"],
            score_change=-25.0,
        )

    monkeypatch.setattr(processor, "process_event", _process_event)

    result = await processor.process_moderation_action(
        community_id=7,
        moderator_id=11,
        target_user_id=5,
        action="timeout",
        platform="twitch",
        platform_user_id="p-5",
    )

    assert result.success is True
    record = _single_audit(collector)
    assert (record.action, record.user, record.community, record.result) == (
        "process_moderation_action",
        "11",
        "7",
        "SUCCESS",
    )
    assert record.additional["score_change"] == -25.0


async def test_failed_moderation_action_is_audited_as_failed(
    logger_and_records: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A moderation action that did not apply audits `result=FAILED` with a zero score change."""
    logger, collector = logger_and_records
    processor = EventProcessor(object(), object(), object(), logger)

    async def _process_event(event_data: dict[str, Any]) -> ProcessResult:
        return ProcessResult(
            success=False,
            community_id=7,
            user_id=5,
            event_type="ban",
            error="not found",
        )

    monkeypatch.setattr(processor, "process_event", _process_event)

    await processor.process_moderation_action(
        community_id=7,
        moderator_id=11,
        target_user_id=5,
        action="ban",
        platform="twitch",
        platform_user_id="p-5",
    )

    record = _single_audit(collector)
    assert record.result == "FAILED"
    assert record.additional["score_change"] == 0
