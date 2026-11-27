#!/usr/bin/env python3
"""Fails when a Cargo.toml package version drifts from its own Cargo.lock entry.

Regression for the alpha deploy "Build seeder" failure after 9e2f8eba3: the
reputation-accrual bundle's Cargo.toml went to 1.0.3 while its Cargo.lock still
said 1.0.2, so every `cargo ... --locked` build died with "cannot update the lock
file". Pure parse (stdlib tomllib, no cargo, no network): for every in-scope
Cargo.lock, the sibling Cargo.toml and every local `path =` dependency it pulls
in must appear in the lockfile as a source-less `[[package]]` with the same
name and version -- exactly the entries `cargo --locked` would rewrite.

Prints lockfiles examined / local packages compared / mismatches; zero
lockfiles examined is a FAILURE (critical-rules.md Verification Integrity).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import tomllib

# Lockfiles the deploy and CI build with --locked: every Rust bundle, every core
# crate (incl. its test fixtures), and the Rust SDK.
SCAN_GLOBS = ("bundles/rust/*/Cargo.lock", "core/**/Cargo.lock", "sdk/**/Cargo.lock")
SKIP_PARTS = {"target", "node_modules", ".worktrees"}
DEP_TABLES = ("dependencies", "build-dependencies")
DEFAULT_VERSION = "0.0.0"  # cargo's implicit version when [package].version is omitted


def find_lockfiles(root: Path) -> list[Path]:
    """Returns every in-scope Cargo.lock under `root`, sorted, build dirs excluded."""
    found: set[Path] = set()
    for pattern in SCAN_GLOBS:
        for path in root.glob(pattern):
            if not SKIP_PARTS.intersection(path.relative_to(root).parts):
                found.add(path)
    return sorted(found)


def _path_deps(manifest: dict[str, Any], is_root: bool) -> list[str]:
    """Returns the `path = ...` values of a manifest's dependency tables (incl. target.*).

    For the root crate every table (dev too) and every optional dep is resolved into
    the lock; for a dependency crate cargo locks only non-dev deps of ACTIVATED
    features, so optional deps there are skipped (a false "missing" otherwise).
    """
    tables = list(DEP_TABLES) + (["dev-dependencies"] if is_root else [])
    scopes: list[dict[str, Any]] = [manifest]
    scopes.extend(t for t in manifest.get("target", {}).values() if isinstance(t, dict))
    paths: list[str] = []
    for scope in scopes:
        for table in tables:
            for spec in scope.get(table, {}).values():
                if isinstance(spec, dict) and "path" in spec:
                    if spec.get("optional") and not is_root:
                        continue
                    paths.append(str(spec["path"]))
    return paths


def local_packages(
    manifest_path: Path,
) -> tuple[list[tuple[str, str, Path]], list[str]]:
    """Walks the root manifest and its transitive path deps.

    Returns ([(name, version, manifest_path)], [errors]). Dev-dependencies are
    followed only for the root crate, matching what cargo resolves into the lock.
    """
    out: list[tuple[str, str, Path]] = []
    errors: list[str] = []
    seen: set[Path] = set()
    queue: list[tuple[Path, bool]] = [(manifest_path.resolve(), True)]
    while queue:
        path, is_root = queue.pop(0)
        if path in seen:
            continue
        seen.add(path)
        if not path.is_file():
            errors.append(f"{path}: path dependency manifest not found")
            continue
        try:
            data = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as exc:
            errors.append(f"{path}: unparseable Cargo.toml ({exc})")
            continue
        pkg = data.get("package")
        if isinstance(pkg, dict) and "name" in pkg:
            version = pkg.get("version", DEFAULT_VERSION)
            if not isinstance(version, str):
                errors.append(
                    f"{path}: non-literal [package].version {version!r} is unsupported"
                )
            else:
                out.append((str(pkg["name"]), version, path))
        elif is_root:
            errors.append(
                f"{path}: root manifest has no [package] (workspaces unsupported)"
            )
        for rel in _path_deps(data, is_root=is_root):
            queue.append(((path.parent / rel / "Cargo.toml").resolve(), False))
    return out, errors


def check_lockfile(lock_path: Path) -> tuple[int, list[str]] | None:
    """Returns (packages compared, mismatch messages) for one Cargo.lock.

    None means a manifest-less lockfile (a test fixture whose Cargo.toml is
    generated at test time) -- counted and reported as skipped by the caller.
    """
    manifest = lock_path.parent / "Cargo.toml"
    if not manifest.is_file():
        return None
    try:
        lock = tomllib.loads(lock_path.read_text())
    except tomllib.TOMLDecodeError as exc:
        return 0, [f"{lock_path}: unparseable Cargo.lock ({exc})"]
    local_in_lock: dict[str, set[str]] = {}
    for entry in lock.get("package", []):
        if "source" not in entry and "name" in entry:
            local_in_lock.setdefault(entry["name"], set()).add(entry.get("version", ""))
    pkgs, errors = local_packages(manifest)
    mismatches = [f"{lock_path}: {e}" for e in errors]
    for name, version, src in pkgs:
        locked = local_in_lock.get(name)
        if not locked:
            mismatches.append(f"{lock_path}: '{name}' ({src}) has no lockfile entry")
        elif version not in locked:
            mismatches.append(
                f"{lock_path}: '{name}' Cargo.toml={version} ({src}) "
                f"but Cargo.lock={','.join(sorted(locked))}"
            )
    return len(pkgs), mismatches


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Exit 1 on any mismatch or when zero lockfiles are examined."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    root = args.repo_root.resolve()

    lockfiles = find_lockfiles(root)
    compared = 0
    skipped: list[Path] = []
    mismatches: list[str] = []
    for lock in lockfiles:
        result = check_lockfile(lock)
        if result is None:
            skipped.append(lock)
            continue
        compared += result[0]
        mismatches.extend(result[1])
    examined = len(lockfiles) - len(skipped)

    for msg in mismatches:
        print(
            f"::error::{msg} -- `cargo ... --locked` will refuse to build",
            file=sys.stderr,
        )
    for lock in skipped:
        print(f"skipped (no sibling Cargo.toml): {lock.relative_to(root)}")
    print(
        f"check-bundle-lockfiles: {examined} lockfile(s) examined, "
        f"{compared} local package(s) compared, {len(mismatches)} mismatched, "
        f"{len(skipped)} skipped"
    )
    if examined == 0:
        print(
            "::error::0 Cargo.lock files examined -- scanner pointed at the wrong root",
            file=sys.stderr,
        )
        return 1
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
