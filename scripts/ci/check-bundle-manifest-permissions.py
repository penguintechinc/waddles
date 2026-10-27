#!/usr/bin/env python3
"""Fail if any bundle `hub-manifest.yaml` declares `permissions:` in the legacy bare-string form.

Incident: core bundle manifests shipped `permissions: [storage.kv]` -- the old,
never-enforced bare-string shape. `hub_api/services/bundle_manifest_v2.py`
still PARSES it (back-compat) but yields ZERO structured permission
declarations, so every such bundle was seeded with zero grants and its
capability calls were denied at runtime -- nothing failed loudly at install
(PRs #703/#705 converted the core manifests; hub_api's own unit tests pin the
parser behaviour but not the manifests in the tree). This is the repo-wide
regression gate: every manifest under `bundles/` must use the V2 structured
form.

Rules, per `bundles/**/hub-manifest.yaml`:
  * `permissions` must be present and a list (`permissions: []` is the explicit
    "needs nothing" form -- an omitted key silently parses to zero grants too);
  * every entry must be a mapping, never a bare string;
  * every entry needs a non-empty string `id` and a non-empty string
    `justification` of at most 280 chars (`_MAX_JUSTIFICATION_LEN` in
    bundle_manifest_v2.py -- the parser rejects anything else at install time).
Catalog membership of each `id` stays the parser's job (hub_api tests).

Exit code is the gate (critical-rules.md Verification Integrity): non-zero on
any violation, on an unreadable/invalid YAML file, and on ZERO manifests found
(a scanner pointed at the wrong root reports clean). The manifest and
permission-entry counts examined are always printed.

Usage: check-bundle-manifest-permissions.py [--root DIR]
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

MANIFEST_NAME = "hub-manifest.yaml"
MAX_JUSTIFICATION_LEN = 280
SKIP_DIR_NAMES = frozenset({"node_modules", "target", "dist", "build", "__pycache__"})


@dataclass(slots=True)
class Report:
    """Denominator counters plus the human-readable violations for one run."""

    manifests: int = 0
    entries: int = 0
    violations: list[str] = field(default_factory=list)


def discover_manifests(root: Path) -> list[Path]:
    """Return every `hub-manifest.yaml` under `<root>/bundles`, sorted for stable output."""
    bundles = root / "bundles"
    found: list[Path] = []
    if not bundles.is_dir():
        return found
    for dirpath, dirnames, filenames in os.walk(bundles):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIR_NAMES)
        if MANIFEST_NAME in filenames:
            found.append(Path(dirpath) / MANIFEST_NAME)
    return sorted(found)


def check_entry(index: int, entry: Any) -> str | None:
    """Return why one `permissions[index]` entry is not V2-shaped, or None when it is."""
    if isinstance(entry, str):
        return f"permissions[{index}] is the legacy bare string {entry!r} -- use `- id: {entry}` + `justification:`"
    if not isinstance(entry, dict):
        return f"permissions[{index}] must be a mapping with id+justification, got {type(entry).__name__}"
    permission_id = entry.get("id")
    if not isinstance(permission_id, str) or not permission_id.strip():
        return f"permissions[{index}] is missing a non-empty string `id`"
    justification = entry.get("justification")
    if not isinstance(justification, str) or not justification.strip():
        return f"permissions[{index}] ({permission_id}) is missing a non-empty `justification`"
    if len(justification) > MAX_JUSTIFICATION_LEN:
        return f"permissions[{index}] ({permission_id}) justification is {len(justification)} chars (max {MAX_JUSTIFICATION_LEN})"
    return None


def check_manifest(path: Path, rel: str, report: Report) -> None:
    """Load one manifest and append every V2-shape violation to `report` (YAML errors are violations)."""
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        report.violations.append(f"{rel}: cannot parse manifest: {exc}")
        return
    if not isinstance(doc, dict):
        report.violations.append(f"{rel}: manifest is not a YAML mapping")
        return
    if "permissions" not in doc:
        report.violations.append(f"{rel}: no `permissions:` key -- declare `permissions: []` explicitly (an omitted block silently yields zero grants)")
        return
    permissions = doc["permissions"]
    if not isinstance(permissions, list):
        report.violations.append(f"{rel}: `permissions` must be a list, got {type(permissions).__name__}")
        return
    for index, entry in enumerate(permissions):
        report.entries += 1
        problem = check_entry(index, entry)
        if problem:
            report.violations.append(f"{rel}: {problem}")


def main(argv: list[str]) -> int:
    """CLI entry: scan every bundle manifest under `--root`; exit 1 on any violation or zero manifests."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2],
                        help="repo root containing bundles/ (default: this repo)")
    root = parser.parse_args(argv).root.resolve()
    report = Report()
    manifests = discover_manifests(root)
    report.manifests = len(manifests)
    for path in manifests:
        check_manifest(path, path.relative_to(root).as_posix(), report)
    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    for violation in report.violations:
        print(violation, file=sys.stderr)
        if in_actions:
            rel, _, msg = violation.partition(": ")
            print(f"::error file={rel},title=bundle-manifest-permissions::{msg}")
    zero = report.manifests == 0
    if zero:
        print(f"check-bundle-manifest-permissions: FAIL -- zero {MANIFEST_NAME} found under {root / 'bundles'} (scan root moved/missing?)", file=sys.stderr)
    passed = not zero and not report.violations
    print(
        f"check-bundle-manifest-permissions: manifests_examined={report.manifests} "
        f"permission_entries_examined={report.entries} violations={len(report.violations)} "
        f"-> {'PASS' if passed else 'FAIL'}"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
