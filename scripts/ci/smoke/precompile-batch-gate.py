#!/usr/bin/env python3
"""AOT-precompile Job gate for the dal2-alpha deploy smoke step (step 2b).

Precompile is not a Helm hook, so it never blocks `helm upgrade` itself: a stuck or slow
compile is legitimate because the executor JIT-falls-back from the signed source and the
next batch catches up. The generic pod-phase scan (step 2) therefore excludes precompile
pods by name, and this post-upgrade gate enforces exactly two things:

- A precompile *batch Job* created at/after this release's deploy whose status carries a
  `Failed`/`True` condition trips the gate (exit 1) -- a real, this-release failure signal.
  A slow/active or succeeded batch Job never trips it.
- The denominator can never be vacuously zero. The examined set is the enqueue-driven
  activity for this release: the enqueue Job (which this deploy waits on and which drives
  every batch) plus any batch Jobs it has spawned. If *nothing* was examined -- not even the
  enqueue Job -- the precompile machinery for this release is absent and the gate FAILS
  loudly rather than passing on an empty input (verification-integrity: zero examined is a
  failure, never a silent pass). A window with zero batch Jobs yet is fine *because* the
  enqueue Job is still counted, so the gate proves it actually ran.

Reads the `kubectl get jobs` document selecting BOTH components
(`app.kubernetes.io/component in (executor-precompile, executor-precompile-enqueue)`) and
the current enqueue Job's creationTimestamp (the "since" boundary). Jobs created before the
boundary belong to a prior release and are ignored. Counts are always printed.

Usage: precompile-batch-gate.py <jobs.json> <since-rfc3339>
"""

from __future__ import annotations

import json
import sys

_ENQUEUE = "executor-precompile-enqueue"  # the Job that drives this release's precompile


def _component(job: dict) -> str:
    return (job["metadata"].get("labels") or {}).get("app.kubernetes.io/component", "")


def _is_enqueue(job: dict) -> bool:
    return _component(job) == _ENQUEUE


def _failed(job: dict) -> bool:
    return any(
        c.get("type") == "Failed" and c.get("status") == "True"
        for c in job.get("status", {}).get("conditions", []) or []
    )


def main(argv: list[str]) -> int:
    """Exit non-zero iff a since-release precompile batch Job Failed, or nothing was examined."""
    if len(argv) != 3:
        print("usage: precompile-batch-gate.py <jobs.json> <since>", file=sys.stderr)
        return 2
    with open(argv[1], encoding="utf-8") as fh:
        jobs = json.load(fh)["items"]
    since = argv[2]

    mine = [j for j in jobs if j["metadata"]["creationTimestamp"] >= since]
    enqueue = [j for j in mine if _is_enqueue(j)]
    batch = [j for j in mine if not _is_enqueue(j)]
    examined = len(mine)  # enqueue-driven activity: enqueue Job + its batch Jobs
    failed = [j["metadata"]["name"] for j in batch if _failed(j)]
    done = sum(1 for j in batch if (j.get("status", {}).get("succeeded") or 0) >= 1)
    active = len(batch) - done - len(failed)
    print(
        f"precompile jobs examined={examined} enqueue={len(enqueue)} batch={len(batch)} "
        f"succeeded={done} active={active} failed={len(failed)}"
    )

    if examined == 0:
        print(
            "::error::zero precompile jobs examined since release -- the enqueue Job that "
            "drives precompile is absent; refusing to pass vacuously",
            file=sys.stderr,
        )
        return 1
    if failed:
        print(
            f"::error::precompile batch Job(s) failed since this release: {failed}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
