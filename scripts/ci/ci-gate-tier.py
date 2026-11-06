#!/usr/bin/env python3
"""Tier policy for the ci-gate aggregate check.

Why: PRs into release/** only need the fast tier (+ coverage); the full set is
required for PRs into main (i.e. the release->main PR). ci-gate aggregates every
sibling check on the head SHA, so a tier is expressed as (a) the main-only checks
that are EXCLUDED from the examined set on the release tier and (b) the checks
that must be PRESENT (a missing always-run check fails the gate).

  tier_for_base BASE        -> prints "release" | "main" (unknown/empty -> main, fail closed)
  filter --tier T           -> stdin: JSON array of {name,status,conclusion}; stdout: examined set
  missing --tier T          -> stdin: same array; prints missing required names, exit 1 if any
  skips --tier T            -> stdin: examined array; prints counts, exit 1 if a skip is untrusted
                               or nothing actually ran (zero examined/ran = FAIL)

Path gating (release PRs only): a job the shared detector skipped as "not affected"
reports conclusion "skipped". On the release tier that counts as satisfied ONLY while
a successful "Detect changed areas" check is on the same head (the detector attests the
reason; fail-closed -- a missing/failed/cancelled detector makes every skip untrusted).
Required jobs that are missing/failed/cancelled still fail. The main tier is unchanged:
every check is examined and the detector forces every area on (no path gating), so a
skip there is never "not affected".
"""
import argparse
import json
import re
import sys

# Main-only checks: still required on PRs into main; never examined on release PRs.
MAIN_ONLY_PATTERNS = [
    r"^(Build .+ Container / )?(Determine Version and Tags|Detect Changed Modules|Build( .+)? \((linux/(amd64|arm64)|\$\{\{ matrix\.platform \}\})\)( \[PR\])?|Build & Scan \(PR\)|Merge( .+)? Manifest|Trivy Image Scan \(gated\))$",
    r"^Supply[- ]chain",
    r"^Security Scan$",
    r"^Notify",
    r"^CodeQL",
    r"^Analyze \(",
    r"^kind (install/upgrade|upgrade-path)",
    r"^Rust connector-e2e",
    r"^Rust tool-invoke-e2e",
    r"^Rust bundle-executor e2e",
]

# Always-reporting fast-tier checks (a skipped job still reports); missing = FAIL.
DETECTOR_NAME = "Detect changed areas"
FAST_REQUIRED_PRESENT = [
    DETECTOR_NAME,
    "Gate integrity (lint/security gates can fail)",
    "No stubs / no silent fallbacks (check-no-stubs)",
    "Alembic single-head invariant (static, no DB)",
    "OpenAPI spec drift (openapi/v1.yaml matches generated)",
    "Helm chart lint + hook dependency ordering",
    "Security & Code Quality",
]
# Main tier = everything the fast tier requires plus the full-tier extras.
# Full-tier-only always-reporting checks (workflow has no path filter on PRs into main).
MAIN_EXTRA_PRESENT: list = [
    "Rust bundle-executor e2e (flag-on command, main tier)",
]


def tier_for_base(base: str) -> str:
    """Map a PR/merge-group base ref to a tier; anything but release/** is the full tier."""
    base = base.removeprefix("refs/heads/")
    return "release" if base.startswith("release/") else "main"


def is_main_only(name: str) -> bool:
    """True when the check name belongs to the main-only set."""
    return any(re.search(p, name) for p in MAIN_ONLY_PATTERNS)


def filt(tier: str, checks: list) -> list:
    """Return the checks ci-gate must examine for this tier (main: all of them)."""
    if tier == "main":
        return list(checks)
    return [c for c in checks if not is_main_only(c["name"])]


def required_present(tier: str) -> list:
    """Names that must appear among the reported checks for this tier."""
    return FAST_REQUIRED_PRESENT + (MAIN_EXTRA_PRESENT if tier == "main" else [])


def missing(tier: str, checks: list) -> list:
    """Required names absent from the reported checks."""
    have = {c["name"] for c in checks}
    return [n for n in required_present(tier) if n not in have]


def detector_ok(checks: list) -> bool:
    """True when at least one detector check completed successfully and none failed."""
    det = [c for c in checks if c["name"] == DETECTOR_NAME]
    return bool(det) and all(
        c.get("status") == "completed" and c.get("conclusion") == "success" for c in det
    )


def skip_report(tier: str, checks: list) -> dict:
    """Classify examined checks: ran vs skipped, and which skips are untrusted.

    Release tier: a skipped check is "not affected" (satisfied) only if the detector
    succeeded. Main tier: skips are never attributed to the detector (it never skips
    on main), so they are reported as-is and never counted as path-gated.
    """
    done = [c for c in checks if c.get("status") == "completed"]
    skipped = [c["name"] for c in done if c.get("conclusion") == "skipped"]
    ran = [c for c in done if c.get("conclusion") != "skipped"]
    trusted = tier == "release" and detector_ok(checks)
    return {
        "examined": len(checks),
        "ran": len(ran),
        "skipped_not_affected": len(skipped) if trusted else 0,
        "skipped_untrusted": [] if (trusted or tier == "main") else skipped,
    }


def main() -> int:
    """CLI entry point."""
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tier_for_base")
    t.add_argument("base")
    for c in ("filter", "missing", "skips"):
        p = sub.add_parser(c)
        p.add_argument("--tier", required=True, choices=["release", "main"])
    a = ap.parse_args()
    if a.cmd == "tier_for_base":
        print(tier_for_base(a.base))
        return 0
    checks = json.load(sys.stdin)
    if a.cmd == "filter":
        print(json.dumps(filt(a.tier, checks)))
        return 0
    if a.cmd == "skips":
        r = skip_report(a.tier, checks)
        print(json.dumps(r))
        return 1 if (r["ran"] == 0 or r["skipped_untrusted"]) else 0
    m = missing(a.tier, checks)
    for n in m:
        print(n)
    return 1 if m else 0


if __name__ == "__main__":
    sys.exit(main())
