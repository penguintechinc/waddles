#!/usr/bin/env python3
"""AOT-precompile batch-Job gate for the dal2-alpha deploy smoke step (step 2b).

The precompile CronJob never gates `helm upgrade` (it is not a Helm hook): a stuck or
slow compile is legitimate because the executor JIT-falls-back from the signed source and
the next batch catches up. So the generic pod-phase scan excludes precompile pods by name,
and the only hard failure this gate enforces is a precompile *batch Job* that reports
Failed since the current release went out.

Reads the `kubectl get jobs -l app.kubernetes.io/component=executor-precompile -o json`
document and the current enqueue Job's creationTimestamp (the "since" boundary). A batch
Job created at/after that boundary whose status carries a `Failed`/`True` condition trips
the gate (exit 1). Prints how many Jobs it examined; a slow/active Job is never a failure.

Usage: precompile-batch-gate.py <jobs.json> <since-rfc3339>
"""

from __future__ import annotations

import json
import sys


def main(argv: list[str]) -> int:
    """Exit non-zero iff a precompile batch Job created since <since> has Failed."""
    if len(argv) != 3:
        print("usage: precompile-batch-gate.py <jobs.json> <since>", file=sys.stderr)
        return 2
    with open(argv[1], encoding="utf-8") as fh:
        jobs = json.load(fh)["items"]
    since = argv[2]
    mine = [j for j in jobs if j["metadata"]["creationTimestamp"] >= since]
    failed = [
        j["metadata"]["name"]
        for j in mine
        if any(
            c.get("type") == "Failed" and c.get("status") == "True"
            for c in j.get("status", {}).get("conditions", [])
        )
    ]
    done = sum(1 for j in mine if (j.get("status", {}).get("succeeded") or 0) >= 1)
    active = len(mine) - done - len(failed)
    print(
        f"precompile batch jobs examined={len(mine)} succeeded={done} "
        f"active={active} failed={len(failed)}"
    )
    if failed:
        print(
            f"::error::precompile batch Job(s) failed since this release: {failed}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
