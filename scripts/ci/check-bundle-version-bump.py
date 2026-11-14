#!/usr/bin/env python3
"""Fail when a core bundle's build inputs changed but its catalog version did not.

app_versions is immutable per (app_id, version): the core-bundle-seeder refuses (409
digest_conflict) any rebuild whose digest differs under an already-published version.
componentize-py bakes the whole WIT world + waddle-sdk into every Python component, so a
change under wit/ or sdk/waddle-sdk/src re-digests EVERY Python bundle (dal2alpha deploy
run 43154169543: 133 bundles). This cheap diff-based gate catches that before deploy.

Usage: check-bundle-version-bump.py <base-ref>   (compares <base-ref>...HEAD)
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterable, Mapping

import yaml

CATALOG = "bundles/core-bundles.yaml"
# Inputs shared by every Python bundle build (bundles/Dockerfile.core-bundles COPY list).
PYTHON_SHARED_INPUTS = (
    "wit/",
    "sdk/waddle-sdk/src/",
    "bundles/build_python_bundles.py",
)
# Files that carry the version itself; editing only these never requires a further bump.
VERSION_ONLY_FILES = ("hub-manifest.yaml", "bundle.yaml", "pyproject.toml")
# Files/dirs inside a bundle dir that never reach the built component (the Dockerfile builds
# src/, the manifest and Cargo/pyproject metadata; build_python_bundles.py reads only
# <dir>/src + <dir>/hub-manifest.yaml): docs, license/notice files, and test trees. A docs- or
# test-only edit never changes the artifact digest, so it never requires a version bump.
NON_BUILD_SUFFIXES = (".md", ".rst", ".txt")
NON_BUILD_NAME_PREFIXES = ("LICENSE", "LICENCE", "NOTICE", "COPYING", "AUTHORS")
NON_BUILD_DIRS = ("docs", "tests")


def _is_non_build_file(path: str, bundle_prefix: str) -> bool:
    """True when `path` (inside `bundle_prefix`) is docs/license/test content, not a build input."""
    rel = path[len(bundle_prefix) :]
    parts = rel.split("/")
    name = parts[-1]
    if any(part in NON_BUILD_DIRS for part in parts[:-1]):
        return True
    return name.endswith(NON_BUILD_SUFFIXES) or name.upper().startswith(
        NON_BUILD_NAME_PREFIXES
    )


def _entries(catalog_text: str) -> dict[str, dict]:
    """Index catalog bundles by app_id (empty when the catalog text is empty)."""
    data = yaml.safe_load(catalog_text) or {}
    return {e["app_id"]: e for e in data.get("bundles", [])}


def find_unbumped(
    base_catalog: str, head_catalog: str, changed_paths: Iterable[str]
) -> list[str]:
    """Return 'app_id@version' for bundles whose inputs changed without a version bump.

    Pure function so it is unit-testable without git. New bundles (absent from base) are
    never flagged; only bundles already published under the same version string are.
    """
    base, head = _entries(base_catalog), _entries(head_catalog)
    changed = list(changed_paths)
    shared_changed = any(p.startswith(PYTHON_SHARED_INPUTS) for p in changed)
    out: list[str] = []
    for app_id, entry in head.items():
        old = base.get(app_id)
        if old is None or str(old["version"]) != str(entry["version"]):
            continue
        src = f"bundles/{entry['source_dir'].rstrip('/')}/"
        own_changed = any(
            p.startswith(src)
            and p.rsplit("/", 1)[-1] not in VERSION_ONLY_FILES
            and not _is_non_build_file(p, src)
            for p in changed
        )
        if own_changed or (entry.get("language") == "python" and shared_changed):
            out.append(f"{app_id}@{entry['version']}")
    return sorted(out)


def _git(*args: str) -> str:
    """Run git and return stdout; raises on failure so the gate cannot silently pass."""
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout


def main(argv: list[str], env: Mapping[str, str] | None = None) -> int:
    """CLI entry: compare <base-ref>...HEAD and print the examined denominator."""
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    base_ref = argv[1]
    merge_base = _git("merge-base", base_ref, "HEAD").strip()
    changed = _git("diff", "--name-only", f"{merge_base}...HEAD").split()
    base_cat = _git("show", f"{merge_base}:{CATALOG}")
    with open(CATALOG, encoding="utf-8") as fh:
        head_cat = fh.read()
    examined = len(_entries(head_cat))
    if examined == 0:
        print(f"FAIL: 0 bundles examined in {CATALOG}", file=sys.stderr)
        return 1
    bad = find_unbumped(base_cat, head_cat, changed)
    print(
        f"bundle-version-bump: examined {examined} bundles, {len(changed)} changed paths"
    )
    if bad:
        print(
            f"FAIL: {len(bad)} bundle(s) changed build inputs without a version bump "
            "(seeder would 409 digest_conflict); bump patch in core-bundles.yaml + "
            "hub-manifest.yaml + bundle.yaml:",
            file=sys.stderr,
        )
        for b in bad:
            print(f"  {b}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
