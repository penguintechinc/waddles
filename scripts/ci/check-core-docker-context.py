#!/usr/bin/env python3
"""Static check that every core Rust service Dockerfile COPYs each out-of-crate path
its release build reads to the container path the crate expects it at.

Regression gate for alpha deploy runs 43859970070 / 43858247593: the svc-streaming
builder ran `COPY platform_keys ./platform_keys` BEFORE any WORKDIR, so the crate
landed at `/platform_keys` while `Cargo.toml`'s `path = "../platform_keys"` resolves
from `/build/svc_streaming` to `/build/platform_keys` -> "failed to read
/build/platform_keys/Cargo.toml". Sibling of check-bundle-docker-context.py (which
covers bundles/Dockerfile.core-bundles); shares its Rust source scanning.

Images come from scripts/ci/image-inputs.yaml (dockerfile + context + build_contexts),
so the check builds each Dockerfile exactly the way the deploy workflow does. For each
image whose Dockerfile directory holds a Cargo.toml:
  * the crate's own container dir is where its Cargo.toml is COPYed;
  * every Cargo `path = "..."` (any table except dev-dependencies and
    [[test]]/[[bench]]/[[example]] targets), transitively through path deps, plus
    `#[path]` / `include*!` refs in each crate's non-test src and build.rs, must be
    COPYed (from the build context or a named --build-context) so it lands at
    `<crate container dir>/<same relative path>`.
Prints crates and refs examined; zero of either is a failure (critical-rules.md
Verification Integrity).
"""

from __future__ import annotations

import argparse
import importlib.util
import posixpath
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import tomllib
import yaml

_SIBLING = Path(__file__).resolve().parent / "check-bundle-docker-context.py"
FROM_RE = re.compile(r"^\s*FROM\s+(\S+)(?:\s+AS\s+(\S+))?\s*$", re.IGNORECASE)
COPY_RE = re.compile(r"^\s*(?:COPY|ADD)\s+(.*)$", re.IGNORECASE)
WORKDIR_RE = re.compile(r"^\s*WORKDIR\s+(\S+)\s*$", re.IGNORECASE)
SKIP_TABLES = {"dev-dependencies", "test", "bench", "example"}


def _load_sibling() -> ModuleType:
    """Loads check-bundle-docker-context.py (hyphenated name) for its Rust scanners."""
    spec = importlib.util.spec_from_file_location("bundle_docker_context", _SIBLING)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {_SIBLING}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_bdc = _load_sibling()


@dataclass(frozen=True, slots=True)
class Copy:
    """One COPY source mapping: host path -> container path, in a given stage."""

    stage: str
    host: Path
    dest: str


def _logical_lines(text: str) -> list[str]:
    """Joins backslash-continued Dockerfile lines and drops comments."""
    out: list[str] = []
    buf = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if not buf and line.lstrip().startswith("#"):
            continue
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        out.append(buf + line)
        buf = ""
    if buf:
        out.append(buf)
    return out


def parse_dockerfile(
    text: str, ctx_root: Path, named: dict[str, Path]
) -> tuple[dict[str, str | None], list[Copy]]:
    """Returns (stage -> parent stage, every COPY mapping from a host build context)."""
    parents: dict[str, str | None] = {}
    copies: list[Copy] = []
    stage: str | None = None
    workdir = "/"
    for line in _logical_lines(text):
        m = FROM_RE.match(line)
        if m:
            stage = m.group(2) or m.group(1)
            parents[stage] = m.group(1) if m.group(1) in parents else None
            workdir = "/"
            continue
        if stage is None:
            continue
        m = WORKDIR_RE.match(line)
        if m:
            workdir = posixpath.normpath(posixpath.join(workdir, m.group(1)))
            continue
        m = COPY_RE.match(line)
        if not m:
            continue
        toks = m.group(1).split()
        flags = [t for t in toks if t.startswith("--")]
        args = [t for t in toks if not t.startswith("--")]
        if len(args) < 2:
            continue
        root = ctx_root
        frm = next((f.split("=", 1)[1] for f in flags if f.startswith("--from=")), None)
        if frm is not None:
            if frm not in named:
                continue  # copy from another stage/image, not from a host context
            root = named[frm]
        raw_dest = args[-1]
        dest = posixpath.normpath(posixpath.join(workdir, raw_dest))
        multi = len(args) > 2
        for src in args[:-1]:
            host = (root / src).resolve()
            if host.is_dir():
                target = dest  # directory source: its CONTENTS land at dest
            elif multi or raw_dest.endswith("/"):
                target = posixpath.join(dest, host.name)
            else:
                target = dest
            copies.append(Copy(stage, host, target))
    return parents, copies


def stage_chain(stage: str, parents: dict[str, str | None]) -> set[str]:
    """`stage` plus every stage it is built FROM."""
    out: set[str] = set()
    cur: str | None = stage
    while cur is not None and cur not in out:
        out.add(cur)
        cur = parents.get(cur)
    return out


def container_path(host: Path, copies: list[Copy], stages: set[str]) -> str | None:
    """Container path `host` is COPYed to within `stages` (last COPY wins), else None."""
    found: str | None = None
    for c in copies:
        if c.stage not in stages:
            continue
        if host == c.host:
            found = c.dest
        elif c.host in host.parents:
            found = posixpath.join(c.dest, host.relative_to(c.host).as_posix())
    return found


def cargo_paths(cargo_toml: Path) -> list[Path]:
    """Every `path = "..."` in a Cargo.toml outside dev-deps and test/bench/example targets."""
    found: list[Path] = []

    def walk(node: object, key: str) -> None:
        if key in SKIP_TABLES:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "path" and isinstance(v, str):
                    found.append((cargo_toml.parent / v).resolve())
                else:
                    walk(v, k)
        elif isinstance(node, list):
            for v in node:
                walk(v, key)

    walk(tomllib.loads(cargo_toml.read_text()), "")
    return found


MOD_DECL_RE = re.compile(
    r"((?:#\[[^\]]*\]\s*)*)(?:pub(?:\([^)]*\))?\s+)?mod\s+([A-Za-z_][A-Za-z0-9_]*)\s*;"
)


def _module_roots(crate_dir: Path) -> list[Path]:
    """Crate-root source files a release build compiles: lib/main/bin targets and build.rs."""
    roots = [
        crate_dir / "src" / "lib.rs",
        crate_dir / "src" / "main.rs",
        crate_dir / "build.rs",
    ]
    roots += sorted((crate_dir / "src" / "bin").glob("*.rs"))
    manifest = tomllib.loads((crate_dir / "Cargo.toml").read_text())
    targets = [manifest.get("lib", {})] + list(manifest.get("bin", []))
    roots += [
        (crate_dir / t["path"]) for t in targets if isinstance(t, dict) and "path" in t
    ]
    return [r.resolve() for r in roots if r.is_file()]


def source_refs(crate_dir: Path) -> set[Path]:
    """`#[path]` / `include*!` targets reached from a crate's non-test module tree.

    Walks `mod x;` declarations from the crate roots, skipping `#[cfg(test)]` items, so
    test-only modules (and the fixtures they `include_str!`) are not required in the image.
    """
    refs: set[Path] = set()
    queue: list[tuple[Path, Path]] = [(r, r.parent) for r in _module_roots(crate_dir)]
    seen: set[Path] = set()
    while queue:
        rs, mod_dir = queue.pop()
        rs = rs.resolve()
        if rs in seen or not rs.is_file():
            continue
        seen.add(rs)
        text = _bdc.mask_test_items(rs.read_text())
        for m in _bdc.INCLUDE_RE.finditer(text):
            refs.add((rs.parent / m.group(1)).resolve())
        for m in MOD_DECL_RE.finditer(text):
            attrs, name = m.group(1), m.group(2)
            pm = _bdc.PATH_ATTR_RE.search(attrs or "")
            if pm:
                target = (rs.parent / pm.group(1)).resolve()
                refs.add(target)
                queue.append((target, target.parent))
                continue
            for cand in (mod_dir / f"{name}.rs", mod_dir / name / "mod.rs"):
                if cand.is_file():
                    sub_dir = (
                        cand.parent if cand.name == "mod.rs" else cand.parent / name
                    )
                    queue.append((cand, sub_dir))
                    break
    return refs


def crate_graph(crate_dir: Path) -> tuple[list[Path], set[Path]]:
    """(crates reached via path deps incl. root, every path ref they make)."""
    crates: list[Path] = []
    refs: set[Path] = set()
    queue = [crate_dir.resolve()]
    while queue:
        d = queue.pop()
        if d in crates or not (d / "Cargo.toml").is_file():
            continue
        crates.append(d)
        refs.add(d / "Cargo.toml")
        for p in cargo_paths(d / "Cargo.toml"):
            refs.add(p)
            if (p / "Cargo.toml").is_file():
                queue.append(p)
        refs |= source_refs(d)
    return crates, refs


def check_image(
    name: str, spec: dict[str, object], root: Path, text: str | None = None
) -> tuple[int, int, list[str]] | None:
    """Checks one image; None when its Dockerfile does not build a Cargo crate.

    `text` replaces the Dockerfile's contents (negative tests) while keeping its location.
    """
    dockerfile = root / str(spec["dockerfile"])
    crate_dir = dockerfile.parent.resolve()
    if not (crate_dir / "Cargo.toml").is_file():
        return None
    ctx_root = (root / str(spec.get("context", "."))).resolve()
    named: dict[str, Path] = {}
    for bc in spec.get("build_contexts", []) or []:  # type: ignore[union-attr]
        k, v = str(bc).split("=", 1)
        named[k] = (root / v).resolve()
    parents, copies = parse_dockerfile(
        dockerfile.read_text() if text is None else text, ctx_root, named
    )

    root_toml = crate_dir / "Cargo.toml"
    owner = [c for c in copies if container_path(root_toml, [c], {c.stage}) is not None]
    if not owner:
        return 1, 0, [f"{name}: {root_toml.relative_to(root)} is never COPYed"]
    stages = stage_chain(owner[-1].stage, parents)
    crate_ctr = posixpath.dirname(container_path(root_toml, copies, stages) or "")

    crates, refs = crate_graph(crate_dir)
    failures: list[str] = []
    for ref in sorted(refs):
        rel_repo = (
            ref.relative_to(root).as_posix() if ref.is_relative_to(root) else str(ref)
        )
        expected = posixpath.normpath(
            posixpath.join(
                crate_ctr, posixpath.relpath(ref.as_posix(), crate_dir.as_posix())
            )
        )
        actual = container_path(ref, copies, stages)
        if actual is None:
            in_ctx = ref.is_relative_to(ctx_root) or any(
                ref.is_relative_to(r) for r in named.values()
            )
            why = "not COPYed" if in_ctx else "outside the build context"
            failures.append(f"{name}: {rel_repo} {why} (needed at {expected})")
        elif posixpath.normpath(actual) != expected:
            failures.append(
                f"{name}: {rel_repo} COPYed to {actual}, crate expects {expected}"
            )
    return len(crates), len(refs), failures


def main() -> int:
    """CLI entry point; prints per-image results and the denominator, exit 1 on any gap."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo-root", default=".", type=Path)
    ap.add_argument("--inputs", default="scripts/ci/image-inputs.yaml")
    ap.add_argument(
        "--dockerfile-override",
        action="append",
        default=[],
        metavar="IMAGE=PATH",
        help="check PATH in place of IMAGE's Dockerfile (negative tests)",
    )
    args = ap.parse_args()
    root = args.repo_root.resolve()
    images = yaml.safe_load((root / args.inputs).read_text())["images"]
    overrides: dict[str, str] = {}
    for ov in args.dockerfile_override:
        img, path = ov.split("=", 1)
        if img not in images:
            print(
                f"FAIL: unknown image {img!r} in --dockerfile-override", file=sys.stderr
            )
            return 1
        overrides[img] = Path(path).read_text()

    images_n = crates_n = refs_n = 0
    failures: list[str] = []
    for name in sorted(images):
        res = check_image(name, dict(images[name]), root, overrides.get(name))
        if res is None:
            continue
        images_n += 1
        c, r, f = res
        crates_n += c
        refs_n += r
        print(
            f"  {'FAIL' if f else 'ok  '} {name}: crates={c} refs={r} failing={len(f)}"
        )
        failures += f

    for f in failures:
        print(f"  FAIL {f}")
    print(
        f"core-docker-context check: images_examined={images_n} crates_examined={crates_n} "
        f"refs_examined={refs_n} failing={len(failures)}"
    )
    if images_n == 0 or crates_n == 0 or refs_n == 0:
        print("FAIL: zero images/crates/refs examined", file=sys.stderr)
        return 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
