"""Tests scripts/ci/smoke/precompile-batch-gate.py (deploy smoke step 2b).

Pins the one hard rule the precompile gate enforces: a precompile *batch Job* that reports
Failed since the current release trips the gate (exit 1), while a stuck/slow/active batch
Job never does -- the executor JIT-falls-back and the next batch catches up. Each case
asserts the examined count is printed; a Failed job created before the release boundary is
ignored (it belongs to a prior release).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_GATE = Path(__file__).resolve().parents[1] / "precompile-batch-gate.py"
_SINCE = "2026-11-14T00:00:00Z"


def _job(name: str, created: str, *, failed: bool = False, succeeded: int = 0) -> dict:
    conditions = [{"type": "Failed", "status": "True"}] if failed else []
    return {
        "metadata": {"name": name, "creationTimestamp": created},
        "status": {"conditions": conditions, "succeeded": succeeded},
    }


def _run(tmp_path: Path, jobs: list[dict]) -> subprocess.CompletedProcess[str]:
    doc = tmp_path / "jobs.json"
    doc.write_text(json.dumps({"items": jobs}), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(_GATE), str(doc), _SINCE],
        capture_output=True,
        text=True,
        check=False,
    )


def test_failed_batch_job_trips_the_gate(tmp_path: Path) -> None:
    """A precompile batch Job Failed since the release must fail the deploy."""
    res = _run(
        tmp_path,
        [_job("waddlebot-executor-precompile-29915000", "2026-11-14T10:00:00Z", failed=True)],
    )
    assert res.returncode == 1, res.stderr
    assert "failed=1" in res.stdout
    assert "precompile batch Job(s) failed" in res.stderr


def test_stuck_active_batch_job_passes(tmp_path: Path) -> None:
    """A slow/stuck (active, not Failed) batch Job must NOT fail the deploy."""
    res = _run(
        tmp_path,
        [_job("waddlebot-executor-precompile-29915000", "2026-11-14T10:00:00Z")],
    )
    assert res.returncode == 0, res.stderr
    assert "examined=1" in res.stdout
    assert "active=1" in res.stdout


def test_succeeded_batch_job_passes(tmp_path: Path) -> None:
    res = _run(
        tmp_path,
        [_job("waddlebot-executor-precompile-29915000", "2026-11-14T10:00:00Z", succeeded=1)],
    )
    assert res.returncode == 0, res.stderr
    assert "succeeded=1" in res.stdout


def test_failed_job_before_release_is_ignored(tmp_path: Path) -> None:
    """A Failed Job from a PRIOR release (created before the boundary) is not this deploy's."""
    res = _run(
        tmp_path,
        [_job("waddlebot-executor-precompile-29000000", "2026-11-13T00:00:00Z", failed=True)],
    )
    assert res.returncode == 0, res.stderr
    assert "examined=0" in res.stdout
