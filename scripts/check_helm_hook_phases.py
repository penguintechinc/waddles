#!/usr/bin/env python3
"""regression: fresh install migrate ran pre-install before postgres (alpha 2026-10-01)

Asserts no Job in k8s/helm/waddlebot that depends on the database (reads
DATABASE_URL, or mounts the dedicated db-migrate-secret) carries a
"pre-install" Helm hook phase. In-chart Postgres
(templates/infrastructure/postgres.yaml) is a plain/regular resource -- Helm
only creates it AFTER the entire pre-install hook phase finishes -- so a
DB-dependent pre-install Job always races a Postgres that doesn't exist yet
on a true fresh install ("Database not ready after 60s", confirmed on alpha
2026-10-01). Fresh-install schema creation now lives in hub-api's own startup
path (hub_api/bootstrap.py); any Job still needing the DB must be
pre-upgrade and/or post-install only.

Fails loudly (never masked) if zero Jobs are examined -- a scanner pointed at
a moved/renamed chart path reporting "0 violations" is not a passing gate,
it's a broken one (critical-rules.md Verification Integrity).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CHART_DIR = REPO_ROOT / "k8s" / "helm" / "waddlebot"


def _render() -> list[dict]:
    result = subprocess.run(
        [
            "helm", "template", "waddlebot", str(CHART_DIR),
            "-f", str(CHART_DIR / "values-alpha.yaml"),
            "--kube-version", "1.30.0",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _needs_db(doc: dict) -> bool:
    text = json.dumps(doc)
    return any(marker in text for marker in ("DATABASE_URL", "POSTGRES_PASSWORD", "db-migrate-secret"))


def main() -> int:
    docs = _render()
    jobs = [doc for doc in docs if doc.get("kind") == "Job"]

    if not jobs:
        print("FAIL: examined 0 Jobs in rendered chart output -- scanner is broken or chart path moved", file=sys.stderr)
        return 1

    print(f"Examined {len(jobs)} Job(s) in rendered chart output.")

    violations = []
    for job in jobs:
        name = job.get("metadata", {}).get("name", "<unknown>")
        hook = job.get("metadata", {}).get("annotations", {}).get("helm.sh/hook", "")
        if _needs_db(job) and "pre-install" in hook.split(","):
            violations.append(f"{name} (hook={hook!r})")

    if violations:
        print(f"FAIL: {len(violations)} DB-dependent Job(s) still carry a pre-install hook phase:", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 1

    print("PASS: no DB-dependent Job carries a pre-install hook phase.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
