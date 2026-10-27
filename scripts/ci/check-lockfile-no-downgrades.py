#!/usr/bin/env python3
"""Fails a PR that silently changes an EXISTING lockfile package's version.

Regression gate for gh-425: a PR touched an unrelated `Cargo.lock` and, as a
side effect of `cargo build` re-resolving the graph, quietly downgraded a
transitive dependency -- nobody reviewing the diff noticed a single version
bump buried in a multi-thousand-line lockfile, and the downgrade reintroduced
an already-fixed bug. Additions (brand-new packages) are always fine; a
VERSION CHANGE on a package that already existed at the PR's merge-base is
flagged unless explicitly acknowledged.

Diffs each changed `Cargo.lock` / `package-lock.json` / hash-pinned
`requirements.txt` between the merge-base and the current tree. A flagged
change is allowed only if the PR body or a commit message in this branch's
history contains a matching line:

    lockfile-bump: <package> <old-version>-><new-version> <reason>

Exit code is the gate: this script always reports how many lockfiles exist
in the repo (proving the scanner is pointed at real files) separately from
how many changed in this diff -- a PR touching zero lockfiles is a
legitimate pass, not a masked zero-denominator failure (critical-rules.md
Verification Integrity).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import tomllib

LOCKFILE_PATTERNS = ("Cargo.lock", "package-lock.json", "requirements.txt")
BUMP_RE = re.compile(
    r"lockfile-bump:\s*(\S+)\s+(\S+)\s*->\s*(\S+)", re.IGNORECASE
)
REQUIREMENTS_PIN_RE = re.compile(r"^([A-Za-z0-9_.\-]+)==([A-Za-z0-9_.\-]+)\s*\\?\s*$")


def run_git(args: list[str], cwd: Path) -> str:
    """Runs a fixed-argv git command (no shell) in `cwd` and returns stdout."""
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


def is_relevant_lockfile(path: str) -> bool:
    """True for Cargo.lock, package-lock.json, or any requirements.txt."""
    return Path(path).name in LOCKFILE_PATTERNS


def parse_cargo_lock(text: str) -> dict[str, str]:
    """Returns `{package_name: version}` from a Cargo.lock's `[[package]]` entries.

    A name can appear more than once (multiple semver-major versions
    resolved simultaneously) -- keyed as `name@major.minor` is overkill for
    this gate's purpose, so instead every (name, version) pair is tracked
    and a "change" is only flagged when a name's FULL set of resolved
    versions shrinks by losing an old one and gaining a different new one at
    the same cardinality position; see `diff_versions`.
    """
    data = tomllib.loads(text)
    versions: dict[str, list[str]] = {}
    for pkg in data.get("package", []):
        name = pkg.get("name")
        version = pkg.get("version")
        if name and version:
            versions.setdefault(name, []).append(version)
    return {name: sorted(vs) for name, vs in versions.items()}


def parse_package_lock(text: str) -> dict[str, list[str]]:
    """Returns `{package_name: [versions]}` from a `package-lock.json` v2/v3 file.

    Keyed by the `packages` map's basename (`node_modules/foo` -> `foo`) so
    the same package resolved at multiple nested paths (npm dedup) is
    compared as one logical package, mirroring `parse_cargo_lock`.
    """
    data = json.loads(text)
    versions: dict[str, list[str]] = {}
    for key, meta in data.get("packages", {}).items():
        if not key or not isinstance(meta, dict):
            continue
        version = meta.get("version")
        if not version:
            continue
        name = key.split("node_modules/")[-1]
        versions.setdefault(name, []).append(version)
    return {name: sorted(vs) for name, vs in versions.items()}


def parse_requirements(text: str) -> dict[str, list[str]]:
    """Returns `{package_name: [version]}` from a hash-pinned `requirements.txt`.

    Only top-level `name==version \\` pin lines are read -- `--hash=...`
    continuation lines and `# via ...` comment lines are ignored.
    """
    versions: dict[str, list[str]] = {}
    for line in text.splitlines():
        match = REQUIREMENTS_PIN_RE.match(line.strip())
        if match:
            versions.setdefault(match.group(1).lower(), []).append(match.group(2))
    return versions


def parse_lockfile(name: str, text: str) -> dict[str, list[str]]:
    """Dispatches to the right parser based on the lockfile's basename."""
    if name == "Cargo.lock":
        return {k: v for k, v in parse_cargo_lock(text).items()}
    if name == "package-lock.json":
        return parse_package_lock(text)
    return parse_requirements(text)


def diff_versions(
    old: dict[str, list[str]], new: dict[str, list[str]]
) -> list[tuple[str, str, str]]:
    """Returns `(package, old_version, new_version)` for every version CHANGE.

    A package present in both with an identical version set is unchanged.
    A brand-new package (not in `old`) is always fine (addition). A package
    whose version set changed is flagged once per differing version pair,
    zipped positionally after sorting -- sufficient for this gate's purpose
    (single-version-per-package is by far the common case; multi-version
    entries still get *a* flagged pair, never silently pass).
    """
    changes: list[tuple[str, str, str]] = []
    for name, new_versions in new.items():
        old_versions = old.get(name)
        if old_versions is None:
            continue  # brand-new package -- always fine
        if sorted(old_versions) == sorted(new_versions):
            continue
        for old_v, new_v in zip(sorted(old_versions), sorted(new_versions)):
            if old_v != new_v:
                changes.append((name, old_v, new_v))
        # Length mismatch beyond the zipped pairs (version added/removed at
        # a cardinality the zip didn't cover) -- flag the extras too.
        if len(new_versions) > len(old_versions):
            for extra in sorted(new_versions)[len(old_versions):]:
                changes.append((name, "(none)", extra))
    return changes


def collect_allowlist(pr_body: str, commit_messages: str) -> set[tuple[str, str, str]]:
    """Parses every `lockfile-bump: <pkg> <old>-><new> <reason>` line found."""
    allowed: set[tuple[str, str, str]] = set()
    for text in (pr_body, commit_messages):
        for match in BUMP_RE.finditer(text):
            allowed.add((match.group(1), match.group(2), match.group(3)))
    return allowed


def main() -> int:
    """CLI entry point. Prints all changes found; exit 1 on any unacknowledged one."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--merge-base", required=True, help="merge-base commit/ref")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--repo-root", default=".", type=Path)
    parser.add_argument("--pr-body-file", type=Path, default=None)
    args = parser.parse_args()

    repo_root: Path = args.repo_root.resolve()
    all_lockfiles = sorted(
        p for pattern in LOCKFILE_PATTERNS for p in repo_root.rglob(pattern)
        if "node_modules" not in p.parts and ".venv" not in p.parts
    )
    print(f"{len(all_lockfiles)} lockfile(s) present in the repo tree.")
    if not all_lockfiles:
        print(
            "::error::0 lockfiles found anywhere in the repo -- scanner is "
            "pointed at the wrong root.",
            file=sys.stderr,
        )
        return 1

    changed_paths = [
        line for line in run_git(
            ["diff", "--name-only", args.merge_base, args.head], repo_root
        ).splitlines()
        if is_relevant_lockfile(line)
    ]
    print(f"{len(changed_paths)} lockfile(s) changed in this diff: {changed_paths}")

    pr_body = args.pr_body_file.read_text() if args.pr_body_file and args.pr_body_file.exists() else ""
    commit_messages = run_git(["log", f"{args.merge_base}..{args.head}", "--format=%B"], repo_root)
    allowlist = collect_allowlist(pr_body, commit_messages)

    all_changes: list[tuple[str, str, str, str]] = []
    flagged: list[tuple[str, str, str, str]] = []
    for path in changed_paths:
        try:
            old_text = run_git(["show", f"{args.merge_base}:{path}"], repo_root)
        except subprocess.CalledProcessError:
            continue  # file didn't exist at merge-base -- brand-new lockfile, fine
        new_text = (repo_root / path).read_text()
        name = Path(path).name
        old_versions = parse_lockfile(name, old_text)
        new_versions = parse_lockfile(name, new_text)
        for pkg, old_v, new_v in diff_versions(old_versions, new_versions):
            all_changes.append((path, pkg, old_v, new_v))
            if (pkg, old_v, new_v) not in allowlist:
                flagged.append((path, pkg, old_v, new_v))

    if all_changes:
        print("Version changes found:")
        for path, pkg, old_v, new_v in all_changes:
            status = "FLAGGED" if (path, pkg, old_v, new_v) in [
                (p, pk, o, n) for p, pk, o, n in flagged
            ] else "allowed (lockfile-bump acknowledged)"
            print(f"  {status:35s} {path}: {pkg} {old_v} -> {new_v}")
    else:
        print("No package version changes in the diff (additions only, or no diff).")

    if flagged:
        for path, pkg, old_v, new_v in flagged:
            print(
                f"::error::{path}: '{pkg}' changed {old_v} -> {new_v} with no matching "
                f"'lockfile-bump: {pkg} {old_v}->{new_v} <reason>' line in the PR body "
                "or a commit message.",
                file=sys.stderr,
            )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
