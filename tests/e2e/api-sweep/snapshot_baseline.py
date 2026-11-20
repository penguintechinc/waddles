#!/usr/bin/env python3
"""Condense the latest sweep reports into small committed baseline files.

Usage: snapshot_baseline.py YYYY-MM-DD   (reads /tmp/{api,bundle}-sweep-report, /tmp/old-id-scan-report)
Writes tests/e2e/api-sweep/baseline/*-<date>.* so a post-cutover run can be diffed against it.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "baseline"


def main() -> int:
    """Write compact baselines; fail if any source report is missing."""
    date = sys.argv[1]
    OUT.mkdir(exist_ok=True)
    a = json.load(open("/tmp/api-sweep-report/api-sweep.json"))
    json.dump({
        "summary": a["summary"],
        "fail": [f"{r['method']} {r['route']} {r.get('status', r.get('probe_status'))}" for r in a["results"] if r["class"] == "FAIL"],
        "old_prefix_routes": [r["route"] for r in a["results"] if "old_prefix" in r],
    }, open(OUT / f"api-sweep-{date}.json", "w"), indent=1)
    b = json.load(open("/tmp/bundle-sweep-report/bundle-sweep.json"))
    json.dump({
        "summary": b["summary"],
        "failed": [f"{x['app_id']}: {x['error']}" for x in b["bundles"] if x["error"]],
        "app_ids": [x["app_id"] for x in b["bundles"]],
    }, open(OUT / f"bundle-sweep-{date}.json", "w"), indent=1)
    shutil.copy("/tmp/old-id-scan-report/old-id-scan.json", OUT / f"old-id-scan-{date}.json")
    shutil.copy("/tmp/old-id-scan-report/db.txt", OUT / f"old-id-scan-db-{date}.txt")
    print(f"baselines written to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
