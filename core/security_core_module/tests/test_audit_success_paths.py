"""SUCCESS-path regression: security-core `logger.audit(...)` calls vs the REAL `AAALogger`.

# regression: `AAALogger.audit(action, user, community, result, **extras)`. Every audit call in
# security-core passed a message as the first positional (usually alongside `action=`) and
# omitted `user` / `community` (sometimes `result`), so each raised `TypeError` on the success
# path. Consequences, all silent behind the services' broad `except Exception`:
#
# * `SpamDetector.check_spam` -- spam WAS detected, the audit raised, the handler's
#   "fail open" branch returned False: detected spam was never blocked;
# * `ContentFilter.add_blocked_words` / `WarningManager.issue_*_warning` -- the row was already
#   committed, then the call raised to the API as a 500;
# * `SecurityService.sync_moderation_action` -- the action was logged + applied, then raised.
#
# Real `AAALogger` (never a no-op logger), fake DAL; only network/Redis seams are stubbed.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from flask_core import setup_aaa_logging

from config import Config
from services.content_filter import ContentFilter
from services.security_service import SecurityService
from services.spam_detector import SpamDetector
from services.warning_manager import WarningManager


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
    aaa = setup_aaa_logging("security-audit-success", "0.0.0", log_dir=str(tmp_path))
    collector = _Collector()
    aaa.logger.addHandler(collector)
    yield aaa, collector
    aaa.logger.removeHandler(collector)


class _Dal:
    """Fake sync DAL: SELECTs return `rows_for(sql)`; writes are recorded no-ops."""

    def __init__(self, count_rows: list[Any] | None = None, servers: list[Any] | None = None):
        self.count_rows = count_rows or []
        self.servers = servers or []
        self.writes: list[str] = []
        self.commits = 0

    def executesql(self, sql: str, params: list[Any] | None = None) -> list[Any]:
        if "COUNT(*)" in sql:
            return self.count_rows
        if "FROM server_status_configs" in sql:
            return self.servers
        if sql.lstrip().upper().startswith("SELECT"):
            return []
        self.writes.append(sql)
        return []

    def commit(self) -> None:
        self.commits += 1


def _fields(record: logging.LogRecord) -> tuple[str, str, str, str]:
    return (record.action, record.user, record.community, record.result)  # type: ignore[attr-defined]


def _single_audit(collector: _Collector) -> logging.LogRecord:
    records = collector.audit_records()
    assert len(records) == 1, f"expected exactly 1 AUDIT record, got {len(records)}"
    return records[0]


async def test_add_blocked_words_returns_and_audits(logger_and_records: Any) -> None:
    """The UPDATE is committed and the call returns (previously: TypeError post-commit)."""
    logger, collector = logger_and_records
    dal = _Dal()

    result = await ContentFilter(dal, logger).add_blocked_words(7, ["foo", "bar"])

    assert dal.commits == 1
    assert result
    record = _single_audit(collector)
    assert _fields(record) == ("add_blocked_words", "system", "7", "SUCCESS")
    assert record.additional["words_count"] == 2
    assert not collector.errors()


async def test_remove_blocked_words_returns_and_audits(logger_and_records: Any) -> None:
    """`remove_blocked_words` commits, returns and audits with a system actor."""
    logger, collector = logger_and_records

    result = await ContentFilter(_Dal(), logger).remove_blocked_words(7, ["foo"])

    assert result
    assert _fields(_single_audit(collector)) == ("remove_blocked_words", "system", "7", "SUCCESS")
    assert not collector.errors()


async def test_update_config_returns_and_audits(logger_and_records: Any) -> None:
    """`update_config` inserts the first config row, then returns the config (no TypeError)."""
    logger, collector = logger_and_records
    dal = _Dal()

    result = await SecurityService(dal, logger).update_config(7, {"filter_action": "timeout"})

    assert result["community_id"] == 7
    assert dal.commits == 1
    assert _fields(_single_audit(collector)) == ("update_security_config", "system", "7", "SUCCESS")
    assert not collector.errors()


async def test_sync_moderation_action_audits_the_moderator(logger_and_records: Any) -> None:
    """The moderator is the actor of the sync audit; the reputation-impact audit is system."""
    logger, collector = logger_and_records

    result = await SecurityService(_Dal(), logger).sync_moderation_action(
        community_id=7,
        platform="twitch",
        platform_user_id="p-5",
        action_type="ban",
        action_reason="spam",
        moderator_id=11,
    )

    assert result["synced"] is True
    by_action = {r.action: r for r in collector.audit_records()}  # type: ignore[attr-defined]
    assert _fields(by_action["sync_moderation_action"]) == (
        "sync_moderation_action",
        "11",
        "7",
        "SUCCESS",
    )
    assert _fields(by_action["apply_reputation_impact"]) == (
        "apply_reputation_impact",
        "system",
        "7",
        "SUCCESS",
    )
    assert not collector.errors()


async def test_sync_moderation_action_without_moderator_is_attributed_to_system(
    logger_and_records: Any,
) -> None:
    """An automated action (no moderator id) is attributed to 'system', never 'None'."""
    logger, collector = logger_and_records

    await SecurityService(_Dal(), logger).sync_moderation_action(
        community_id=7, platform="twitch", platform_user_id="p-5", action_type="warn"
    )

    record = next(r for r in collector.audit_records() if r.action == "sync_moderation_action")  # type: ignore[attr-defined]
    assert record.user == "system"  # type: ignore[attr-defined]


async def test_game_server_sync_audits_each_server(
    logger_and_records: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A game-server ban sync audits `sync_to_game_server` per server (previously: TypeError)."""
    import httpx

    posted: list[str] = []

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def post(self, url: str, json: dict[str, Any]) -> None:
            posted.append(url)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    logger, collector = logger_and_records
    dal = _Dal(servers=[(3, "rcon")])

    await SecurityService(dal, logger).sync_moderation_action(
        community_id=7,
        platform="twitch",
        platform_user_id="p-5",
        action_type="ban",
        action_reason="cheating",
        moderator_id=11,
        sync_to_platforms=["rcon"],
    )

    assert posted and posted[0].endswith("/7/servers/3/ban")
    record = next(r for r in collector.audit_records() if r.action == "sync_to_game_server")  # type: ignore[attr-defined]
    assert _fields(record) == ("sync_to_game_server", "system", "7", "SUCCESS")
    assert record.additional["server_id"] == 3
    assert not collector.errors()


async def test_automated_warning_audits_without_error(logger_and_records: Any) -> None:
    """`issue_automated_warning` commits the warning and returns (previously: TypeError)."""
    logger, collector = logger_and_records
    dal = _Dal(count_rows=[(1,)])

    result = await WarningManager(dal, logger).issue_automated_warning(
        7, "twitch", "p-5", "spam", "spam detected"
    )

    assert result
    assert _fields(_single_audit(collector)) == ("issue_warning", "system", "7", "SUCCESS")
    assert not collector.errors()


async def test_manual_warning_audits_the_moderator(logger_and_records: Any) -> None:
    """The issuing moderator is the audit actor."""
    logger, collector = logger_and_records

    result = await WarningManager(_Dal(), logger).issue_manual_warning(
        7, "twitch", "p-5", "be nice", issued_by=11
    )

    assert result["warning_issued"] is True
    assert _fields(_single_audit(collector)) == ("issue_manual_warning", "11", "7", "SUCCESS")


async def test_revoke_warning_audits_the_revoker(logger_and_records: Any) -> None:
    """Revoking is attributed to the moderator; a warning has no community scope in the call."""
    logger, collector = logger_and_records

    result = await WarningManager(_Dal(), logger).revoke_warning(5, revoked_by=11)

    assert result == {"revoked": True}
    assert _fields(_single_audit(collector)) == ("revoke_warning", "11", "", "SUCCESS")


@pytest.mark.parametrize(
    ("count", "expected_action"),
    [
        (Config.DEFAULT_WARNING_THRESHOLD_BAN, "auto_escalation_ban"),
        (Config.DEFAULT_WARNING_THRESHOLD_TIMEOUT, "auto_escalation_timeout"),
    ],
)
async def test_auto_escalation_is_audited(
    logger_and_records: Any, count: int, expected_action: str
) -> None:
    """Crossing a warning threshold audits an escalation (previously: TypeError, swallowed)."""
    logger, collector = logger_and_records
    dal = _Dal(count_rows=[(count,)])

    await WarningManager(dal, logger).issue_automated_warning(
        7, "twitch", "p-5", "spam", "spam detected"
    )

    actions = {r.action: r for r in collector.audit_records()}  # type: ignore[attr-defined]
    assert _fields(actions[expected_action]) == (expected_action, "system", "7", "ESCALATED")
    assert not collector.errors()


@pytest.fixture
def spam_detector(
    logger_and_records: Any, monkeypatch: pytest.MonkeyPatch
) -> tuple[SpamDetector, _Collector]:
    """A `SpamDetector` with Redis unavailable (DB fallback), plus the log collector."""
    import redis

    def _no_redis(*args: Any, **kwargs: Any) -> Any:
        raise redis.ConnectionError("redis unavailable in unit test")

    monkeypatch.setattr(redis.Redis, "from_url", _no_redis)
    logger, collector = logger_and_records
    return SpamDetector(_Dal(), logger), collector


async def test_frequency_spam_is_blocked_not_failed_open(
    spam_detector: tuple[SpamDetector, _Collector], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Detected spam returns True (previously: the audit TypeError made the handler fail OPEN).

    # regression: `check_spam` returned False for a user it had just detected spamming, because
    # the mis-signatured `logger.audit(...)` raised inside the `try` and the `except` branch
    # deliberately fails open -- so spam was never blocked.
    """
    detector, collector = spam_detector

    async def _spam(**kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(detector, "_check_message_frequency", _spam)

    blocked = await detector.check_spam(7, "twitch", "p-5", message="buy now")

    assert blocked is True, "spam was detected but check_spam failed open"
    record = _single_audit(collector)
    assert _fields(record) == ("spam_detection", "system", "7", "BLOCKED")
    assert not collector.errors()


async def test_duplicate_spam_is_blocked_not_failed_open(
    spam_detector: tuple[SpamDetector, _Collector], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Duplicate-message spam is blocked and audited too."""
    detector, collector = spam_detector

    async def _no(**kwargs: Any) -> bool:
        return False

    async def _yes(**kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(detector, "_check_message_frequency", _no)
    monkeypatch.setattr(detector, "_check_duplicate_messages", _yes)

    blocked = await detector.check_spam(7, "twitch", "p-5", message="same text")

    assert blocked is True
    record = _single_audit(collector)
    assert _fields(record) == ("spam_detection", "system", "7", "BLOCKED")
    assert record.additional["detail"] == "Spam detected: duplicate"
