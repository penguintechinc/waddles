#!/usr/bin/env python3
"""Group api-sweep.json FAILs by (method class, status, reason) and by route prefix.

Usage: triage.py [api-sweep.json]   (default /tmp/api-sweep-report/api-sweep.json)
"""

from __future__ import annotations

import collections
import json
import sys


def main() -> int:
    """Print top failure groups with an example route each."""
    path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/api-sweep-report/api-sweep.json"
    d = json.load(open(path))
    s = d["summary"]
    print("missing_from_spec:", len(s["routes_missing_from_spec"]), "spec_not_in_code:", len(s["spec_routes_missing_from_code"]))
    print("skipped:", s["skipped_reasons"])
    groups: dict[tuple, list[str]] = collections.defaultdict(list)
    for r in d["results"]:
        if r["class"] == "FAIL":
            st = r.get("status", r.get("probe_status"))
            groups[(r["method"] == "GET", st, (r.get("why") or "")[:48])].append(f"{r['method']} {r['route']}")
    for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:12]:
        print(len(v), k, "e.g.", v[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
