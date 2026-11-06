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
FAST_REQUIRED_PRESENT = [
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


def main() -> int:
    """CLI entry point."""
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tier_for_base")
    t.add_argument("base")
    for c in ("filter", "missing"):
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
    m = missing(a.tier, checks)
    for n in m:
        print(n)
    return 1 if m else 0


if __name__ == "__main__":
    sys.exit(main())
