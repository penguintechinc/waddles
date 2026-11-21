"""Gate tests for scripts/check_helm_hook_phases.py (DB-dependent Jobs never pre-install).

Proves the gate can fail: a DB-dependent Job planted into a copy of the chart as a pre-install
hook is reported, and the real chart passes.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_CHART_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _CHART_DIR.parents[2]
_CHECKER = _REPO_ROOT / "scripts" / "check_helm_hook_phases.py"

_PLANTED_JOB = """\
apiVersion: batch/v1
kind: Job
metadata:
  name: planted-{name}
  annotations:
    "helm.sh/hook": {hook}
spec:
  template:
    spec:
      restartPolicy: Never
      containers:
      - name: c
        image: busybox
        env:
        - name: DATABASE_URL
          value: postgresql://example
"""


@pytest.fixture(scope="module", autouse=True)
def _helm() -> None:
    if not shutil.which("helm"):
        pytest.fail("helm CLI not on PATH -- this gate test must never silently skip")


def _run(chart: Path) -> subprocess.CompletedProcess[str]:
    """Run the checker against `chart` and return the completed process."""
    return subprocess.run(  # fixed argv, test-only
        [sys.executable, str(_CHECKER), "--chart-dir", str(chart)],
        capture_output=True,
        text=True,
        check=False,
    )


def _planted_chart(tmp_path: Path, name: str, hook: str) -> Path:
    """Copy the chart and add one DB-dependent Job with the given hook phases."""
    chart = tmp_path / "waddlebot"
    shutil.copytree(
        _CHART_DIR, chart, ignore=shutil.ignore_patterns("tests", "__pycache__")
    )
    (chart / "templates" / "zz-planted-job.yaml").write_text(
        _PLANTED_JOB.format(name=name, hook=hook)
    )
    return chart


def test_real_chart_passes() -> None:
    res = _run(_CHART_DIR)
    assert res.returncode == 0, res.stderr
    assert "Examined " in res.stdout and "PASS" in res.stdout


@pytest.mark.parametrize("hook", ["pre-install", "pre-install,pre-upgrade"])
def test_planted_db_dependent_pre_install_hook_fails(tmp_path: Path, hook: str) -> None:
    res = _run(_planted_chart(tmp_path, "pre", hook))
    assert res.returncode == 1, res.stdout + res.stderr
    assert "planted-pre" in res.stderr and "carries pre-install" in res.stderr


def test_planted_db_dependent_post_install_hook_passes(tmp_path: Path) -> None:
    res = _run(_planted_chart(tmp_path, "post", "post-install,post-upgrade"))
    assert res.returncode == 0, res.stdout + res.stderr
