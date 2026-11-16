#!/usr/bin/env python3
"""Classify namespace pods for the dal2-alpha deploy smoke step (step 2).

A single `kubectl get pods` snapshot is the wrong gate for Job/CronJob-owned pods: a
role-sync / event-sync CronJob fires every minute and its pod is legitimately Pending /
Init for a few seconds, so an instantaneous phase check fails the deploy at random. This
classifier judges each pod by what owns it:

- Long-running workloads (Deployment/StatefulSet/DaemonSet/bare pods -- anything NOT owned
  by a Job) are judged by phase, exactly as before: Running or Succeeded is good, anything
  else is bad. These are the denominator ("pods examined=N"); zero of them is a failure.
- Job/CronJob-owned pods are judged by their JOB's status, never the instantaneous pod
  phase. A Job created at/after this release's deploy time whose status reports Failed trips
  the gate; a Job pod that is merely Pending/Init/active does not. A Job created BEFORE the
  release boundary is ignored entirely, so an ancient Failed CronJob run (days old) never
  trips this deploy's smoke.
- AOT-precompile pods/Jobs are excluded here -- they are judged by the explicit precompile
  gate (precompile-batch-gate.py, step 2b), which remains their single authority.

Prints "pods examined=N bad=M". Exits non-zero on any bad long-running pod, any Failed
this-release Job, or an empty long-running denominator.

Usage: pod-phase-classify.py <pods.json> <jobs.json> <since-rfc3339>
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone

_GOOD_PHASES = {"Running", "Succeeded"}
_PRECOMPILE = "executor-precompile"  # judged by step 2b, not here


def _parse(ts: str) -> datetime:
    """Parse an RFC3339 timestamp (incl. helm's 9-digit RFC3339Nano and a 'Z' suffix)."""
    s = ts.strip().replace("Z", "+00:00")
    # datetime.fromisoformat accepts at most microseconds; trim any longer fraction.
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _job_name(pod: dict) -> str | None:
    """Return the owning Job's name if this pod is Job/CronJob-owned, else None."""
    for owner in pod["metadata"].get("ownerReferences") or []:
        if owner.get("kind") == "Job":
            return owner.get("name")
    labels = pod["metadata"].get("labels") or {}
    return labels.get("job-name") or labels.get("batch.kubernetes.io/job-name")


def _job_failed(job: dict) -> bool:
    return any(
        c.get("type") == "Failed" and c.get("status") == "True"
        for c in job.get("status", {}).get("conditions", []) or []
    )


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print("usage: pod-phase-classify.py <pods.json> <jobs.json> <since>", file=sys.stderr)
        return 2
    pods = json.load(open(argv[1], encoding="utf-8"))["items"]
    jobs = {j["metadata"]["name"]: j for j in json.load(open(argv[2], encoding="utf-8"))["items"]}
    since = _parse(argv[3])

    examined = 0
    bad = 0
    failed_jobs: set[str] = set()

    for pod in pods:
        name = pod["metadata"]["name"]
        if _PRECOMPILE in name:  # step 2b authority
            continue
        job_name = _job_name(pod)
        if job_name:
            # Job/CronJob-owned: judge the JOB, never the instantaneous pod phase.
            if _PRECOMPILE in job_name:
                continue
            job = jobs.get(job_name)
            if job is None:
                continue  # Job already GC'd; nothing to judge
            if _parse(job["metadata"]["creationTimestamp"]) < since:
                continue  # pre-release run (e.g. ancient Failed CronJob) -- never trips
            if _job_failed(job):
                failed_jobs.add(job_name)
            continue
        # Long-running workload pod: judge by phase.
        examined += 1
        phase = pod["status"].get("phase", "")
        if phase not in _GOOD_PHASES:
            print(f"NOT READY: {name} phase={phase}", file=sys.stderr)
            bad += 1

    for jn in sorted(failed_jobs):
        print(f"FAILED JOB (since release): {jn}", file=sys.stderr)
        bad += 1

    print(f"pods examined={examined} bad={bad}")
    if examined == 0:
        print("::error::zero long-running workload pods examined", file=sys.stderr)
        return 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
