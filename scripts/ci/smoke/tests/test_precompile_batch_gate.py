"""Tests scripts/ci/smoke/precompile-batch-gate.py (deploy smoke step 2b).

Pins the two rules the precompile gate enforces:
- a precompile *batch Job* that reports Failed since the current release trips the gate
  (exit 1), while a stuck/slow/active or succeeded batch Job never does -- the executor
  JIT-falls-back and the next batch catches up;
- the examined denominator is the enqueue-driven activity (the enqueue Job + its batch
  Jobs). Zero examined is a loud FAILURE, never a vacuous pass -- so an empty input fails,
  while a window with the enqueue Job present but zero batch Jobs yet passes (the enqueue
  Job proves the gate actually ran). A Failed Job created before the release boundary
  belongs to a prior release and is ignored.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_GATE = Path(__file__).resolve().parents[1] / "precompile-batch-gate.py"
_SINCE = "2026-11-14T00:00:00Z"
_AFTER = "2026-11-14T10:00:00Z"
_BEFORE = "2026-11-13T00:00:00Z"  # prior release, before the boundary

_BATCH = "executor-precompile"
_ENQUEUE = "executor-precompile-enqueue"


def _job(
    name: str,
    created: str,
    component: str = _BATCH,
    *,
    failed: bool = False,
    succeeded: int = 0,
) -> dict:
    conditions = [{"type": "Failed", "status": "True"}] if failed else []
    return {
        "metadata": {
            "name": name,
            "creationTimestamp": created,
            "labels": {"app.kubernetes.io/component": component},
        },
        "status": {"conditions": conditions, "succeeded": succeeded},
    }


def _enqueue(name: str, created: str, *, succeeded: int = 1, failed: bool = False) -> dict:
    return _job(name, created, _ENQUEUE, failed=failed, succeeded=succeeded)


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
        [
            _enqueue("waddlebot-executor-precompile-enqueue-rev7", _SINCE),
            _job("waddlebot-executor-precompile-29915000", _AFTER, failed=True),
        ],
    )
    assert res.returncode == 1, res.stderr
    assert "failed=1" in res.stdout
    assert "precompile batch Job(s) failed since this release" in res.stderr


def test_stuck_active_batch_job_passes(tmp_path: Path) -> None:
    """A slow/stuck (active, not Failed) batch Job must NOT fail the deploy."""
    res = _run(
        tmp_path,
        [
            _enqueue("waddlebot-executor-precompile-enqueue-rev7", _SINCE),
            _job("waddlebot-executor-precompile-29915000", _AFTER),
        ],
    )
    assert res.returncode == 0, res.stderr
    assert "examined=2" in res.stdout
    assert "active=1" in res.stdout


def test_succeeded_batch_job_passes(tmp_path: Path) -> None:
    res = _run(
        tmp_path,
        [
            _enqueue("waddlebot-executor-precompile-enqueue-rev7", _SINCE),
            _job("waddlebot-executor-precompile-29915000", _AFTER, succeeded=1),
        ],
    )
    assert res.returncode == 0, res.stderr
    assert "succeeded=1" in res.stdout


def test_enqueue_present_zero_batch_passes(tmp_path: Path) -> None:
    """The common post-deploy window: enqueue Job present, no batch Job fired yet -- PASS,
    because the enqueue Job is counted (examined > 0), so the check is not vacuous."""
    res = _run(
        tmp_path,
        [_enqueue("waddlebot-executor-precompile-enqueue-rev7", _SINCE)],
    )
    assert res.returncode == 0, res.stderr
    assert "examined=1 enqueue=1 batch=0" in res.stdout


def test_zero_examined_fails(tmp_path: Path) -> None:
    """No precompile jobs at all since release -- the enqueue driver is absent -- FAIL loud."""
    res = _run(tmp_path, [])
    assert res.returncode == 1, res.stderr
    assert "examined=0" in res.stdout
    assert "zero precompile jobs examined since release" in res.stderr


def test_only_prerelease_jobs_is_zero_examined_fail(tmp_path: Path) -> None:
    """Jobs exist but all predate the boundary (a prior release): examined=0 -> FAIL, and the
    pre-release Failed batch Job is NOT treated as this release's failure."""
    res = _run(
        tmp_path,
        [
            _enqueue("waddlebot-executor-precompile-enqueue-rev6", _BEFORE),
            _job("waddlebot-executor-precompile-29000000", _BEFORE, failed=True),
        ],
    )
    assert res.returncode == 1, res.stderr
    assert "examined=0" in res.stdout
    assert "zero precompile jobs examined since release" in res.stderr
    assert "precompile batch Job(s) failed" not in res.stderr


def test_prerelease_failed_batch_ignored_when_enqueue_present(tmp_path: Path) -> None:
    """A Failed batch Job from a PRIOR release does not trip this deploy; this release's
    enqueue Job supplies a non-zero denominator."""
    res = _run(
        tmp_path,
        [
            _enqueue("waddlebot-executor-precompile-enqueue-rev7", _SINCE),
            _job("waddlebot-executor-precompile-29000000", _BEFORE, failed=True),
        ],
    )
    assert res.returncode == 0, res.stderr
    assert "examined=1 enqueue=1 batch=0" in res.stdout


def test_healthy_full_set_passes(tmp_path: Path) -> None:
    """Enqueue succeeded + one succeeded batch + one active batch, no failures -> PASS."""
    res = _run(
        tmp_path,
        [
            _enqueue("waddlebot-executor-precompile-enqueue-rev7", _SINCE),
            _job("waddlebot-executor-precompile-29915000", _AFTER, succeeded=1),
            _job("waddlebot-executor-precompile-29915060", _AFTER),
        ],
    )
    assert res.returncode == 0, res.stderr
    assert "examined=3 enqueue=1 batch=2 succeeded=1 active=1 failed=0" in res.stdout
