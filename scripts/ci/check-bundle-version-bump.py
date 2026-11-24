#!/usr/bin/env python3
"""Fail when a core bundle's build inputs changed but its catalog version did not.

app_versions is immutable per (app_id, version): the core-bundle-seeder refuses (409
digest_conflict) any rebuild whose digest differs under an already-published version.
componentize-py bakes the whole WIT world + waddle-sdk into every Python component, so a
change under wit/ or sdk/waddle-sdk/src re-digests EVERY Python bundle (dal2alpha deploy
run 43154169543: 133 bundles). A Rust bundle likewise re-digests when any out-of-crate input
its release build reads changes: Cargo path deps (sdk/waddle-sdk-rs, wit/<world>, transitively),
`#[path]` and `include_*!` targets (bundles/contracts/...). dal2alpha deploy run 43849873861:
a sdk/waddle-sdk-rs/src/tool.rs change (#1372) re-digested 8 Rust service bundles under
unchanged versions. Those refs come from check-bundle-docker-context.py's crate_refs().
This cheap diff-based gate catches both before deploy.

Usage: check-bundle-version-bump.py <base-ref>   (compares <base-ref>...HEAD)
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

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


def _is_shared_non_build(path: str) -> bool:
    """True when a shared-input path is docs/license/test content (never compiled in)."""
    parts = path.split("/")
    name = parts[-1]
    if any(part in NON_BUILD_DIRS for part in parts[:-1]):
        return True
    return name.endswith(NON_BUILD_SUFFIXES) or name.upper().startswith(
        NON_BUILD_NAME_PREFIXES
    )


def _ref_hit(path: str, ref: str) -> bool:
    """True when changed `path` affects build ref `ref` (an exact file, or a dir's build file).

    A file the release build names explicitly (`include_str!`, `#[path]`) is always an
    input, even under a tests/ dir; inside a referenced directory only docs/license/test
    content is ignored.
    """
    ref = ref.rstrip("/")
    if path == ref:
        return True
    return path.startswith(ref + "/") and not _is_shared_non_build(path)


def _entries(catalog_text: str) -> dict[str, dict]:
    """Index catalog bundles by app_id (empty when the catalog text is empty)."""
    data = yaml.safe_load(catalog_text) or {}
    return {e["app_id"]: e for e in data.get("bundles", [])}


def find_unbumped(
    base_catalog: str,
    head_catalog: str,
    changed_paths: Iterable[str],
    shared_refs: Mapping[str, Iterable[str]] | None = None,
) -> list[str]:
    """Return 'app_id@version' for bundles whose inputs changed without a version bump.

    Pure function so it is unit-testable without git. New bundles (absent from base) are
    never flagged; only bundles already published under the same version string are.
    `shared_refs` maps app_id -> repo-relative out-of-crate build inputs (files or dirs)
    of a compiled bundle; a build-relevant change under any of them demands the bump.
    """
    refs_by_app = shared_refs or {}
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
        refs = list(refs_by_app.get(app_id, ()))
        ref_changed = any(_ref_hit(p, r) for p in changed for r in refs)
        if (
            own_changed
            or ref_changed
            or (entry.get("language") == "python" and shared_changed)
        ):
            out.append(f"{app_id}@{entry['version']}")
    return sorted(out)


def _git(*args: str) -> str:
    """Run git and return stdout; raises on failure so the gate cannot silently pass."""
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout


def _load_crate_refs():
    """Import crate_refs() from check-bundle-docker-context.py (one resolver, no drift)."""
    path = Path(__file__).resolve().parent / "check-bundle-docker-context.py"
    spec = importlib.util.spec_from_file_location("check_bundle_docker_context", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.crate_refs


def rust_shared_refs(head_catalog: str, root: Path) -> dict[str, list[str]]:
    """Map each Rust catalog bundle to its repo-relative out-of-crate build inputs."""
    crate_refs = _load_crate_refs()
    root = root.resolve()
    out: dict[str, list[str]] = {}
    for app_id, entry in _entries(head_catalog).items():
        if entry.get("language") != "rust":
            continue
        crate_dir = root / "bundles" / entry["source_dir"].rstrip("/")
        if not (crate_dir / "Cargo.toml").is_file():
            raise FileNotFoundError(f"{app_id}: no Cargo.toml in {crate_dir}")
        refs: list[str] = []
        for ref in crate_refs(crate_dir):
            try:
                refs.append(ref.relative_to(root).as_posix())
            except ValueError as exc:
                raise ValueError(f"{app_id}: {ref} is outside the repo") from exc
        out[app_id] = sorted(refs)
    return out


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
    shared = rust_shared_refs(head_cat, Path("."))
    if not shared:
        print("FAIL: 0 Rust bundles resolved for shared-input refs", file=sys.stderr)
        return 1
    bad = find_unbumped(base_cat, head_cat, changed, shared)
    n_refs = sum(len(v) for v in shared.values())
    print(
        f"bundle-version-bump: examined {examined} bundles ({len(shared)} rust, "
        f"{n_refs} out-of-crate refs), {len(changed)} changed paths"
    )
    if bad:
        print(
            f"FAIL: {len(bad)} bundle(s) changed build inputs (own source or out-of-crate "
            "refs) without a version bump (seeder would 409 digest_conflict); bump patch "
            "in core-bundles.yaml + hub-manifest.yaml + bundle.yaml:",
            file=sys.stderr,
        )
        for b in bad:
            print(f"  {b}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
