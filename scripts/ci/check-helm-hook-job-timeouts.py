#!/usr/bin/env python3
"""Static gate for the helm-hook-timeout bug class (#1391).

Helm blocks the whole release on every pre-/post-install/upgrade hook Job until
it completes OR its activeDeadlineSeconds expires, bounded only by `helm --wait
--timeout`. Two ways a hook hangs the deploy:

  1. A hook Job with NO activeDeadlineSeconds (unbounded). If it never completes
     (image pull wedged, dependency missing), Helm waits the ENTIRE --timeout
     and then fails the release -- the Job itself never self-terminates.
  2. A hook Job whose activeDeadlineSeconds is >= the deploy's helm --timeout:
     the Job's own deadline is useless because Helm gives up first, and a hook
     that genuinely needs that long should not be blocking the release at all.

The #1391 fix removed the executor-precompile-backfill post-upgrade HOOK that
compiled ~160 bundles serially (~70s each) inside Helm's wait and timed out
every alpha deploy. Long/serial work must run as a plain Job/CronJob, never a
blocking hook -- asserted here too, by name marker.

Reads a `helm template` multi-document YAML stream on stdin. Asserts, for every
helm.sh/hook-annotated Job:
  - activeDeadlineSeconds is set and > 0 (bounded);
  - activeDeadlineSeconds < --deploy-timeout.
And, for every Job/CronJob whose name marks it as long/serial work
(precompile, backfill): it carries NO helm.sh/hook annotation.

Prints the number of hook Jobs examined; zero is a hard failure, never a silent
pass (critical-rules.md Verification Integrity).
"""
from __future__ import annotations

import argparse
import sys
from typing import Any

import yaml

HOOK_ANNOTATION = "helm.sh/hook"
# Workloads whose name contains one of these must never be a blocking hook: they
# are long/serial by nature and belong in a plain Job/CronJob off the hot path.
LONG_SERIAL_MARKERS = ("precompile", "backfill")


def hook_types(doc: dict[str, Any]) -> set[str]:
    ann = (doc.get("metadata") or {}).get("annotations") or {}
    raw = ann.get(HOOK_ANNOTATION, "")
    return {h.strip() for h in raw.split(",") if h.strip()}


def job_name(doc: dict[str, Any]) -> str:
    return (doc.get("metadata") or {}).get("name", "<unnamed>")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--deploy-timeout",
        type=int,
        required=True,
        help="the deploy's `helm --timeout` in seconds; every hook Job's "
        "activeDeadlineSeconds must be strictly below this.",
    )
    args = ap.parse_args()
    deploy_timeout = args.deploy_timeout
    if deploy_timeout <= 0:
        print("FAIL: --deploy-timeout must be a positive number of seconds", file=sys.stderr)
        return 1

    docs = [d for d in yaml.safe_load_all(sys.stdin.read()) if d]

    examined = 0
    failures: list[str] = []

    # (1)+(2): every hook Job carries a bounded deadline below the deploy timeout.
    for d in docs:
        if d.get("kind") != "Job":
            continue
        if not hook_types(d):
            continue
        examined += 1
        name = job_name(d)
        ads = (d.get("spec") or {}).get("activeDeadlineSeconds")
        if ads is None:
            print(f"FAIL  Job/{name}: helm hook with NO activeDeadlineSeconds (unbounded -- can hang helm --wait)")
            failures.append(f"Job/{name} unbounded hook")
            continue
        if not isinstance(ads, int) or ads <= 0:
            print(f"FAIL  Job/{name}: activeDeadlineSeconds={ads!r} is not a positive integer")
            failures.append(f"Job/{name} non-positive deadline")
            continue
        if ads >= deploy_timeout:
            print(
                f"FAIL  Job/{name}: activeDeadlineSeconds={ads}s >= deploy helm --timeout "
                f"{deploy_timeout}s (helm gives up first; this hook should not block the release)"
            )
            failures.append(f"Job/{name} deadline >= deploy timeout")
            continue
        print(f"OK    Job/{name}: activeDeadlineSeconds={ads}s < {deploy_timeout}s deploy timeout")

    # (3): long/serial work (precompile, backfill) must never be a blocking hook.
    serial_examined = 0
    for d in docs:
        if d.get("kind") not in ("Job", "CronJob"):
            continue
        name = job_name(d)
        if not any(m in name for m in LONG_SERIAL_MARKERS):
            continue
        serial_examined += 1
        hooks = hook_types(d)
        if hooks:
            print(f"FAIL  {d['kind']}/{name}: long/serial workload is a blocking helm hook {sorted(hooks)}")
            failures.append(f"{d['kind']}/{name} long-serial work as a hook")
        else:
            print(f"OK    {d['kind']}/{name}: long/serial workload is a plain (non-hook) workload")

    print(f"\nhook Jobs examined: {examined}; long/serial workloads examined: {serial_examined}")
    if examined == 0:
        print("FAIL: zero hook Jobs examined -- check is pointed at the wrong input", file=sys.stderr)
        return 1

    if failures:
        print(f"\nFAIL: {len(failures)} helm hook timeout violation(s):", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print("PASS: every helm hook Job is bounded below the deploy timeout; no long/serial work blocks the release")
    return 0


if __name__ == "__main__":
    sys.exit(main())
