"""`cli.role_sync_remediate` -- argument wiring and fail-loud on missing DATABASE_URL."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from cli import role_sync_remediate as cli
from services.role_sync_remediation import RemediationReport


def test_missing_database_url_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert cli.main([]) == 1


@pytest.mark.parametrize(("argv", "apply"), [([], False), (["--apply"], True)])
def test_runs_remediation_and_prints_denominators(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    apply: bool,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    seen: dict[str, Any] = {}

    @contextmanager
    def _dal(url: str) -> Any:
        yield SimpleNamespace(dal="dal")

    def _remediate(dal: Any, *, apply: bool, baseline_role: str) -> RemediationReport:
        seen.update(dal=dal, apply=apply, baseline=baseline_role)
        return RemediationReport(purged_bindings_examined=2, candidates=1, newly_queued=1)

    monkeypatch.setattr(cli, "open_worker_dal", _dal)
    monkeypatch.setattr(cli, "remediate_purged_bindings", _remediate)
    assert cli.main(argv) == 0
    assert seen == {"dal": "dal", "apply": apply, "baseline": "member"}
    assert "purged_bindings_examined=2 candidates=1" in capsys.readouterr().out
