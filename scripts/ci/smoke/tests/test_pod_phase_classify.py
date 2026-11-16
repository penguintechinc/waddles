"""Tests scripts/ci/smoke/pod-phase-classify.py (deploy smoke step 2).

Pins the owner-aware classification that replaced the single-snapshot phase scan:
- a long-running (Deployment) pod is judged by phase;
- a role-sync-style CronJob pod Init:0/1 created this release is NOT a failure (its Job is
  still active) -- the exact flake the old scan produced;
- a Job created since this release whose status is Failed DOES trip the gate;
- an ancient pre-release Failed CronJob run is ignored;
- precompile pods/Jobs are excluded (judged by step 2b);
- an empty long-running denominator fails.

`test_prefix_single_snapshot_regresses_on_rolesync` proves the pre-fix phase-only logic
fails on the very fixture the owner-aware classifier passes.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_CLS = Path(__file__).resolve().parents[1] / "pod-phase-classify.py"
_SINCE = "2026-11-16T15:25:47.248121990Z"  # release deploy time (RFC3339Nano, as helm emits)
_AFTER = "2026-11-16T15:30:00Z"            # created after the release
_BEFORE = "2026-11-10T00:00:00Z"           # ancient, pre-release


def _dep_pod(name: str, phase: str) -> dict:
    """A Deployment (ReplicaSet-owned) pod -- a long-running workload, judged by phase."""
    return {
        "metadata": {"name": name, "ownerReferences": [{"kind": "ReplicaSet", "name": "rs"}]},
        "status": {"phase": phase},
    }


def _job_pod(name: str, job: str, phase: str) -> dict:
    return {
        "metadata": {"name": name, "ownerReferences": [{"kind": "Job", "name": job}],
                     "labels": {"job-name": job}},
        "status": {"phase": phase},
    }


def _job(name: str, created: str, *, failed: bool = False) -> dict:
    conds = [{"type": "Failed", "status": "True"}] if failed else []
    return {"metadata": {"name": name, "creationTimestamp": created}, "status": {"conditions": conds}}


def _run(tmp_path: Path, pods: list[dict], jobs: list[dict]) -> subprocess.CompletedProcess[str]:
    p = tmp_path / "pods.json"
    j = tmp_path / "jobs.json"
    p.write_text(json.dumps({"items": pods}), encoding="utf-8")
    j.write_text(json.dumps({"items": jobs}), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(_CLS), str(p), str(j), _SINCE],
        capture_output=True, text=True, check=False,
    )


def test_transient_rolesync_init_pod_passes(tmp_path: Path) -> None:
    """A role-sync CronJob pod Init:0/1 (Pending) created this release must NOT fail."""
    res = _run(
        tmp_path,
        [_dep_pod("waddlebot-hub-api-v3-a", "Running"),
         _job_pod("waddlebot-role-sync-29914079-x", "waddlebot-role-sync-29914079", "Pending")],
        [_job("waddlebot-role-sync-29914079", _AFTER)],
    )
    assert res.returncode == 0, res.stderr
    assert "pods examined=1 bad=0" in res.stdout


def test_failed_this_release_job_trips(tmp_path: Path) -> None:
    """A Job created since this release whose status is Failed trips the gate."""
    res = _run(
        tmp_path,
        [_dep_pod("waddlebot-hub-api-v3-a", "Running"),
         _job_pod("waddlebot-role-sync-29914079-x", "waddlebot-role-sync-29914079", "Failed")],
        [_job("waddlebot-role-sync-29914079", _AFTER, failed=True)],
    )
    assert res.returncode == 1, res.stderr
    assert "FAILED JOB (since release): waddlebot-role-sync-29914079" in res.stderr
    assert "bad=1" in res.stdout


def test_ancient_prerelease_failed_job_ignored(tmp_path: Path) -> None:
    """A Failed CronJob run from before this release must not trip the deploy smoke."""
    res = _run(
        tmp_path,
        [_dep_pod("waddlebot-hub-api-v3-a", "Running"),
         _job_pod("waddlebot-role-sync-29000000-x", "waddlebot-role-sync-29000000", "Failed")],
        [_job("waddlebot-role-sync-29000000", _BEFORE, failed=True)],
    )
    assert res.returncode == 0, res.stderr
    assert "pods examined=1 bad=0" in res.stdout


def test_broken_deployment_pod_fails(tmp_path: Path) -> None:
    res = _run(
        tmp_path,
        [_dep_pod("waddlebot-hub-api-v3-a", "Running"),
         _dep_pod("waddlebot-hub-webui-b", "CrashLoopBackOff")],
        [],
    )
    assert res.returncode == 1, res.stderr
    assert "pods examined=2 bad=1" in res.stdout


def test_precompile_excluded(tmp_path: Path) -> None:
    """Precompile pods/Jobs are judged by step 2b, never here -- even a stale Pending one."""
    res = _run(
        tmp_path,
        [_dep_pod("waddlebot-hub-api-v3-a", "Running"),
         _job_pod("waddlebot-executor-precompile-29914020-c",
                  "waddlebot-executor-precompile-29914020", "Pending")],
        [_job("waddlebot-executor-precompile-29914020", _AFTER)],
    )
    assert res.returncode == 0, res.stderr
    assert "pods examined=1 bad=0" in res.stdout


def test_zero_longrunning_denominator_fails(tmp_path: Path) -> None:
    res = _run(
        tmp_path,
        [_job_pod("waddlebot-role-sync-29914079-x", "waddlebot-role-sync-29914079", "Pending")],
        [_job("waddlebot-role-sync-29914079", _AFTER)],
    )
    assert res.returncode == 1, res.stderr
    assert "zero long-running workload pods examined" in res.stderr


def test_prefix_single_snapshot_regresses_on_rolesync(tmp_path: Path) -> None:
    """Proof: the pre-fix phase-only scan (every pod judged by phase) fails the exact
    role-sync-Init fixture the owner-aware classifier passes."""
    pods = [_dep_pod("waddlebot-hub-api-v3-a", "Running"),
            _job_pod("waddlebot-role-sync-29914079-x", "waddlebot-role-sync-29914079", "Pending")]
    prefix_bad = sum(1 for p in pods if p["status"]["phase"] not in ("Running", "Succeeded"))
    assert prefix_bad == 1, "pre-fix phase-only logic should count the role-sync Init pod as bad"
    # ...and the fixed classifier passes the same fixture:
    res = _run(tmp_path, pods, [_job("waddlebot-role-sync-29914079", _AFTER)])
    assert res.returncode == 0 and "bad=0" in res.stdout, res.stderr
