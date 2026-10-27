#!/usr/bin/env python3
"""regression: alpha-deploy.sh's helm install must never pass --wait/--atomic (2026-10-01)

USER DECISION (fix/chart-fresh-install-hooks): db-migrate now runs
post-install (schema always comes from it, fresh install included -- see
k8s/helm/waddlebot/templates/migrations-job.yaml). Every other workload in
the chart (hub-api included) only becomes Ready after that hook runs. If
`helm upgrade --install` in scripts/alpha-deploy.sh ever gained `--wait` (or
`--atomic`, which implies it), Helm would block on every just-created
Deployment reaching Ready BEFORE running any post-install hook -- a deadlock:
Deployments wait on hub-api, hub-api waits on db-migrate, db-migrate waits on
Helm to stop waiting on Deployments.

Asserts the HELM_ARGS array literal in scripts/alpha-deploy.sh (the args
passed to the actual `helm upgrade --install` invocation) never contains
`--wait` or `--atomic`.

Fails loudly (never masked) if the HELM_ARGS block can't be found at all -- a
scanner silently matching nothing is not a passing gate, it's a broken one
(critical-rules.md Verification Integrity).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "alpha-deploy.sh"
FORBIDDEN = ("--wait", "--atomic")


def main() -> int:
    text = SCRIPT.read_text()
    match = re.search(r"HELM_ARGS=\(\n(.*?)\n\)", text, re.DOTALL)
    if not match:
        print(
            f"FAIL: could not locate a HELM_ARGS=(...) block in {SCRIPT} -- "
            "scanner is broken or the script was restructured",
            file=sys.stderr,
        )
        return 1

    block = match.group(1)
    lines = [ln for ln in block.splitlines() if ln.strip()]
    print(f"Examined {len(lines)} HELM_ARGS line(s) in {SCRIPT.relative_to(REPO_ROOT)}.")

    violations = [flag for flag in FORBIDDEN if flag in block]
    if violations:
        print(
            f"FAIL: HELM_ARGS contains forbidden flag(s) {violations} -- "
            "this deadlocks a fresh install (see this script's module docstring)",
            file=sys.stderr,
        )
        return 1

    print("PASS: HELM_ARGS carries neither --wait nor --atomic.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
