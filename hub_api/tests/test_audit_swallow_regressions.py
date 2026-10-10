"""Regression: no audit-trail write may be swallowed (GRC audit finding #3's `except: pass`).

Three writers used to drop failures silently: the bundle-lifecycle `audit_log` insert
(`bundle_audit.record`, covered in `test_bundle_audit.py`), the consent-proof trail
(`cookie_consent_service.log_audit_event`) and the failed-deletion record in
`data_privacy_service.request_data_deletion`. Each must now be loud: ERROR log with the exception
type + a traceback (never a driver message, which can embed bound values), a metric, and -- except
where re-raising would mask a more important original error -- an `AuditWriteError`.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from services import cookie_consent_service, data_privacy_service
from services.audit_service import AuditWriteError

SERVICES_DIR = Path(__file__).resolve().parents[1] / "services"
SECRET = "-".join(["bound", "value", "from", "the", "driver"])


class TestConsentProofTrail:
    async def test_a_failed_consent_audit_insert_is_raised_and_logged_not_swallowed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        async_dal = MagicMock()
        async_dal.insert_async = AsyncMock(side_effect=RuntimeError(f"insert failed: {SECRET}"))
        caplog.set_level(logging.ERROR, logger="services.audit_service")
        with pytest.raises(AuditWriteError) as raised:
            await cookie_consent_service.log_audit_event(
                async_dal,
                MagicMock(),
                consent_id="c-1",
                user_id=7,
                action="ACCEPT",
                version="1.0",
            )
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "audit write FAILED" in text and "action=cookie_consent_audit" in text
        assert "type=RuntimeError" in text and "Traceback (most recent call last)" in text
        assert SECRET not in text and SECRET not in str(raised.value)
        assert isinstance(raised.value.__cause__, RuntimeError)

    async def test_a_successful_insert_is_unchanged(self) -> None:
        async_dal = MagicMock()
        async_dal.insert_async = AsyncMock(return_value=1)
        await cookie_consent_service.log_audit_event(
            async_dal, MagicMock(), consent_id="c-1", user_id=7, action="REVOKE", version="1.0"
        )
        assert async_dal.insert_async.await_count == 1


class TestFailedDeletionRecord:
    def _dal(self, *, insert_error: Exception | None) -> MagicMock:
        async_dal = MagicMock()
        async_dal.select_async = AsyncMock(
            return_value=[SimpleNamespace(email="alice@example.com", password_hash=None)]
        )
        async_dal.delete_async = AsyncMock(side_effect=RuntimeError("the ORIGINAL failure"))
        async_dal.insert_async = AsyncMock(side_effect=insert_error)
        return async_dal

    async def test_failure_to_record_the_failure_is_loud_and_does_not_mask_the_original(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        async_dal = self._dal(insert_error=RuntimeError(f"audit insert failed: {SECRET}"))
        caplog.set_level(logging.ERROR, logger="services.audit_service")
        with pytest.raises(RuntimeError, match="the ORIGINAL failure"):
            await data_privacy_service.request_data_deletion(
                async_dal, MagicMock(), user_id=7, password=None
            )
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "action=privacy.deletion_failure_record" in text
        assert "type=RuntimeError" in text and SECRET not in text

    async def test_when_the_failure_record_is_written_nothing_extra_is_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        async_dal = self._dal(insert_error=None)
        caplog.set_level(logging.ERROR, logger="services.audit_service")
        with pytest.raises(RuntimeError, match="the ORIGINAL failure"):
            await data_privacy_service.request_data_deletion(
                async_dal, MagicMock(), user_id=7, password=None
            )
        assert not [r for r in caplog.records if "audit write FAILED" in r.getMessage()]
        assert async_dal.insert_async.await_count == 1  # the "failed" bookkeeping row


class TestNoSilentExceptHandlersInAuditWriters:
    """Static guard over the audit writers themselves (denominator printed on failure)."""

    WRITERS = (
        "bundle_audit.py",
        "audit_service.py",
        "audit_http.py",
        "cookie_consent_service.py",
        "data_privacy_service.py",
    )

    @staticmethod
    def _silent(handler: ast.ExceptHandler) -> bool:
        """True for a handler whose whole body is `pass`/`...`/a constant/`continue`."""
        return all(
            isinstance(stmt, ast.Pass | ast.Continue)
            or (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))
            for stmt in handler.body
        )

    def test_no_handler_in_an_audit_writer_is_silent(self) -> None:
        examined = 0
        offenders: list[str] = []
        for name in self.WRITERS:
            tree = ast.parse((SERVICES_DIR / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler):
                    examined += 1
                    if self._silent(node):
                        offenders.append(f"{name}:{node.lineno}")
        assert examined > 5, f"scanner examined only {examined} handlers -- wrong files?"
        assert not offenders, f"silent except handlers in audit writers: {offenders}"


@pytest.mark.parametrize("module", [cookie_consent_service, data_privacy_service])
def test_modules_use_the_shared_loud_reporter(module: Any) -> None:
    assert module.report_write_failure is not None
