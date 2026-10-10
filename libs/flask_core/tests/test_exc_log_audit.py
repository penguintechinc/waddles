"""`flask_core.exc_log_audit` -- the static guard against exception text in log calls.

# regression: SECURITY (PII in logs). `{e}` / `str(e)` / `exc_info=True` /
# `logger.exception()` in a log call write the exception message -- which for a DB
# driver error embeds the BOUND VALUES of the failed statement (message content,
# usernames, platform ids) -- into the log stream. `describe_db_error` is the
# sanctioned replacement; this auditor keeps the unsafe shapes out.

Mutation check built in: every `UNSAFE` snippet is the pre-fix shape of a real leak
and MUST be flagged (so a scanner that silently stopped matching fails here), every
`SAFE` snippet is the post-fix shape and MUST pass (so the guard cannot cry wolf).
"""

from __future__ import annotations

# ruff: noqa: E501  (the UNSAFE/SAFE tables hold one-line source snippets; splitting hides the shape)
import sys
import types
from pathlib import Path

import pytest

_PKG_DIR = Path(__file__).resolve().parent.parent / "flask_core"
if "flask_core" not in sys.modules:
    _stub = types.ModuleType("flask_core")
    _stub.__path__ = [str(_PKG_DIR)]
    sys.modules["flask_core"] = _stub

from flask_core.exc_log_audit import audit_paths, audit_source, main  # noqa: E402

#: (id, source) -- each must yield >=1 finding.
UNSAFE: list[tuple[str, str]] = [
    (
        "fstring-bare-exc",
        'try:\n    f()\nexcept Exception as e:\n    logger.error(f"Failed to store message: {e}")\n',
    ),
    (
        "fstring-str-exc",
        'try:\n    f()\nexcept Exception as e:\n    logger.error(f"failed: {str(e)}")\n',
    ),
    (
        "percent-arg-exc",
        'try:\n    f()\nexcept Exception as exc:\n    logger.warning("Cache set failed: %s", exc)\n',
    ),
    (
        "exc-info-true",
        'try:\n    f()\nexcept Exception as e:\n    logger.error("failed", exc_info=True)\n',
    ),
    (
        "exc-info-outside-handler",
        'def g():\n    logger.error("failed", exc_info=True)\n',
    ),
    (
        "logger-exception",
        'try:\n    f()\nexcept Exception:\n    logger.exception("failed")\n',
    ),
    (
        "self-logger-kwarg",
        'try:\n    f()\nexcept Exception as e:\n    self.logger.error("failed", error=str(e))\n',
    ),
    (
        "unsafe-attribute-args",
        'try:\n    f()\nexcept Exception as e:\n    logger.error("failed %s", e.args)\n',
    ),
    (
        "unsafe-attribute-response-text",
        'try:\n    f()\nexcept Exception as e:\n    logger.error("failed %s", e.response.text)\n',
    ),
    (
        "traceback-format-exc",
        "import traceback\ntry:\n    f()\nexcept Exception:\n    logger.error(traceback.format_exc())\n",
    ),
    (
        "errorhandler-param",
        "@bp.errorhandler(500)\nasync def internal_error(error):\n"
        '    logger.error(f"Internal server error: {str(error)}")\n',
    ),
    (
        "errorhandler-exc-info",
        "@bp.errorhandler(500)\nasync def internal_error(error):\n"
        '    logger.error("Internal server error", exc_info=True)\n',
    ),
    (
        "nested-handler-uses-outer-name",
        "try:\n    f()\nexcept Exception as e:\n    try:\n        g()\n"
        '    except Exception as inner:\n        logger.error("x %s %s", describe_db_error(inner), e)\n',
    ),
]

#: (id, source) -- each must yield zero findings.
SAFE: list[tuple[str, str]] = [
    (
        "describe-db-error",
        'try:\n    f()\nexcept Exception as e:\n    logger.error(f"failed: {describe_db_error(e)}")\n',
    ),
    (
        "describe-db-error-percent",
        'try:\n    f()\nexcept Exception as e:\n    logger.warning("failed: %s", describe_db_error(e))\n',
    ),
    (
        "type-name-only",
        'try:\n    f()\nexcept Exception as e:\n    logger.error("failed: %s", type(e).__name__)\n',
    ),
    (
        "value-free-attributes",
        "try:\n    f()\nexcept Exception as e:\n"
        '    logger.error("rpc %s http %s", e.code(), e.response.status_code)\n',
    ),
    (
        "authored-message-attribute",
        'try:\n    f()\nexcept CommunityAccessError as e:\n    logger.warning(f"denied: {e.message}")\n',
    ),
    (
        "exc-info-explicitly-off",
        'try:\n    f()\nexcept Exception as e:\n    logger.error("failed", exc_info=False)\n',
    ),
    (
        "no-exception-involved",
        'logger.info(f"started {name}")\n',
    ),
    (
        "non-logger-receiver-with-exception",
        'try:\n    f()\nexcept Exception as e:\n    result.add_error(f"bad: {e}")\n',
    ),
    (
        "exception-used-outside-log-call",
        'try:\n    f()\nexcept Exception as e:\n    last_error = e\n    logger.error("failed")\n',
    ),
    (
        "errorhandler-error-id-only",
        "@bp.errorhandler(500)\nasync def internal_error(error):\n"
        '    logger.error("Internal server error error_id=%s", error_id)\n',
    ),
]


class TestAuditSource:
    """`audit_source` flags the unsafe shapes and passes the sanctioned ones."""

    @pytest.mark.parametrize(("name", "source"), UNSAFE, ids=[n for n, _ in UNSAFE])
    def test_unsafe_shape_is_flagged(self, name: str, source: str) -> None:
        """Each pre-fix leak shape produces at least one finding."""
        examined, findings = audit_source(source, f"{name}.py")
        assert examined >= 1
        assert findings, f"{name}: unsafe shape was NOT flagged"

    @pytest.mark.parametrize(("name", "source"), SAFE, ids=[n for n, _ in SAFE])
    def test_safe_shape_passes(self, name: str, source: str) -> None:
        """Each sanctioned shape produces no findings."""
        _, findings = audit_source(source, f"{name}.py")
        assert not findings, [f.render() for f in findings]

    def test_finding_carries_path_and_line(self) -> None:
        """A finding points at the offending line, for `path:line` triage."""
        _, findings = audit_source(UNSAFE[0][1], "svc.py")
        assert findings[0].render().startswith("svc.py:4:")


class TestAuditPaths:
    """`audit_paths` walks files, skips tests, and reports a denominator."""

    def test_reports_denominator_and_skips_test_files(self, tmp_path: Path) -> None:
        """Test files/dirs are excluded; files and log calls examined are counted."""
        (tmp_path / "svc.py").write_text(UNSAFE[0][1])
        (tmp_path / "test_svc.py").write_text(UNSAFE[0][1])
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "helper.py").write_text(UNSAFE[0][1])
        report = audit_paths([tmp_path])
        assert report.files_examined == 1
        assert report.log_calls_examined == 1
        assert len(report.findings) == 1

    def test_cli_fails_on_findings(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The CLI exits 1 and prints the finding when something unsafe is found."""
        (tmp_path / "svc.py").write_text(UNSAFE[0][1])
        assert main([str(tmp_path)]) == 1
        assert "svc.py:4:" in capsys.readouterr().out

    def test_cli_passes_when_clean(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The CLI exits 0 on a clean tree and prints the denominator."""
        (tmp_path / "svc.py").write_text(SAFE[0][1])
        assert main([str(tmp_path)]) == 0
        assert "files=1 log_calls=1 findings=0" in capsys.readouterr().out

    def test_cli_fails_when_nothing_examined(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Zero files / zero log calls examined is a FAILURE, never a clean pass."""
        assert main([str(tmp_path)]) == 1
        (tmp_path / "svc.py").write_text("x = 1\n")
        assert main([str(tmp_path)]) == 1
        assert "nothing examined" in capsys.readouterr().err

    def test_cli_without_arguments_is_a_usage_error(self) -> None:
        """No paths given -> exit code 2 (usage), not a vacuous pass."""
        assert main([]) == 2
