#!/usr/bin/env python3
"""Static check that every Rust bundle stage in bundles/Dockerfile.core-bundles
COPYs every file outside the crate dir that its release build reads.

Regression gate for alpha deploy run 43812541222: `bundles/rust/rooms/src/lib.rs`
pulls `#[path = "../../webrtc/src/grants.rs"] mod grants;` but the `bundle-rooms`
stage only copied `bundles/rust/rooms`, so `cargo component build` failed with
"couldn't read src/../../webrtc/src/grants.rs" inside the seeder image build.

For each `bundles/rust/<crate>/Cargo.toml` with a `bundle-<crate>` stage, collect:
  * Cargo `path = "..."` references (any table except dev-dependencies), transitively;
  * `#[path = "..."]`, `include!`, `include_str!`, `include_bytes!` in `src/**/*.rs`,
    `build.rs` and any file reached via `#[path]`, skipping `#[cfg(test)]` items
    (release builds never compile them, so their fixtures need not be in the image).
Every resolved path must sit under a COPY source whose destination is the same
path under `/repo` (base stage + the bundle stage itself). Zero crates examined
is a failure (critical-rules.md Verification Integrity).
"""

from __future__ import annotations

import argparse
import posixpath
import re
import sys
from pathlib import Path

import tomllib

FROM_RE = re.compile(r"^\s*FROM\s+(\S+)(?:\s+AS\s+(\S+))?\s*$", re.IGNORECASE)
COPY_RE = re.compile(r"^\s*COPY\s+(.*)$", re.IGNORECASE)
WORKDIR_RE = re.compile(r"^\s*WORKDIR\s+(\S+)\s*$", re.IGNORECASE)
CFG_TEST_RE = re.compile(r"#\[cfg\((?:all\()?test\b")
PATH_ATTR_RE = re.compile(r"#\[path\s*=\s*\"([^\"]+)\"\s*\]")
INCLUDE_RE = re.compile(r"\binclude(?:_str|_bytes)?!\s*\(\s*\"([^\"]+)\"", re.DOTALL)
REPO_PREFIX = "/repo"


def parse_stages(text: str) -> tuple[dict[str, str | None], dict[str, set[str]]]:
    """Returns (stage -> parent stage, stage -> repo-relative paths it COPYs into /repo).

    A COPY counts only when its destination is `/repo/<source>` (absolute, or relative
    to the current WORKDIR), i.e. the file lands where a relative reference expects it.
    """
    parents: dict[str, str | None] = {}
    copies: dict[str, set[str]] = {}
    stage: str | None = None
    workdir = "/"
    for line in text.splitlines():
        m = FROM_RE.match(line)
        if m:
            stage = m.group(2) or m.group(1)
            parents[stage] = m.group(1) if m.group(1) in parents else None
            copies[stage] = set()
            workdir = "/"
            continue
        if stage is None:
            continue
        m = WORKDIR_RE.match(line)
        if m:
            workdir = posixpath.normpath(posixpath.join(workdir, m.group(1)))
            continue
        m = COPY_RE.match(line)
        if not m or "--from=" in m.group(1):
            continue
        tokens = [t for t in m.group(1).split() if not t.startswith("--")]
        if len(tokens) < 2:
            continue
        dest = posixpath.normpath(posixpath.join(workdir, tokens[-1]))
        for src in tokens[:-1]:
            src_norm = posixpath.normpath(src)
            expected = posixpath.normpath(posixpath.join(REPO_PREFIX, src_norm))
            single = len(tokens) == 2
            if (single and dest == expected) or (
                not single and dest == posixpath.dirname(expected)
            ):
                copies[stage].add(src_norm)
    return parents, copies


def stage_coverage(
    stage: str, parents: dict[str, str | None], copies: dict[str, set[str]]
) -> set[str]:
    """All repo paths COPYed into `stage` or any stage it is built FROM."""
    covered: set[str] = set()
    cur: str | None = stage
    while cur is not None:
        covered |= copies.get(cur, set())
        cur = parents.get(cur)
    return covered


def is_covered(rel: str, covered: set[str]) -> bool:
    """True if `rel` equals, or sits beneath, some COPYed source path."""
    return any(rel == c or rel.startswith(c + "/") for c in covered)


def mask_test_items(src: str) -> str:
    """Blanks every `#[cfg(test)]` item (attribute through closing brace or `;`)."""
    out = list(src)
    for m in CFG_TEST_RE.finditer(src):
        i = m.start()
        depth_sq = 0
        # Skip this and any following attributes.
        j = i
        while True:
            while j < len(src) and src[j].isspace():
                j += 1
            if not src.startswith("#[", j):
                break
            depth_sq = 0
            while j < len(src):
                if src[j] == "[":
                    depth_sq += 1
                elif src[j] == "]":
                    depth_sq -= 1
                    if depth_sq == 0:
                        j += 1
                        break
                j += 1
        end = _item_end(src, j)
        for k in range(i, end):
            if out[k] != "\n":
                out[k] = " "
    return "".join(out)


def _item_end(src: str, j: int) -> int:
    """Index just past the item starting at `j` (brace-balanced, strings/comments skipped)."""
    depth = 0
    n = len(src)
    while j < n:
        c = src[j]
        if src.startswith("//", j):
            j = src.find("\n", j)
            j = n if j == -1 else j
            continue
        if src.startswith("/*", j):
            k = src.find("*/", j + 2)
            j = n if k == -1 else k + 2
            continue
        raw = re.match(r"r(#*)\"", src[j : j + 10])
        if raw and (j == 0 or not (src[j - 1].isalnum() or src[j - 1] == "_")):
            close = '"' + raw.group(1)
            k = src.find(close, j + raw.end())
            j = n if k == -1 else k + len(close)
            continue
        if c == '"':
            j += 1
            while j < n and src[j] != '"':
                j += 2 if src[j] == "\\" else 1
            j += 1
            continue
        if c == "'" and j + 2 < n and src[j + 2] == "'":
            j += 3
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return j + 1
        elif c == ";" and depth == 0:
            return j + 1
        j += 1
    return n


def cargo_path_refs(cargo_toml: Path) -> list[Path]:
    """Every `path = "..."` value in a Cargo.toml outside dev-dependencies tables."""
    found: list[Path] = []

    def walk(node: object, key: str) -> None:
        if key == "dev-dependencies":
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


def crate_refs(crate_dir: Path) -> set[Path]:
    """All paths a release build of `crate_dir` reads outside its own source scan roots."""
    refs: set[Path] = set()
    seen_tomls: set[Path] = set()
    toml_queue = [crate_dir / "Cargo.toml"]
    while toml_queue:
        toml = toml_queue.pop().resolve()
        if toml in seen_tomls or not toml.is_file():
            continue
        seen_tomls.add(toml)
        for p in cargo_path_refs(toml):
            refs.add(p)
            if (p / "Cargo.toml").is_file():
                toml_queue.append(p / "Cargo.toml")

    rs_queue = sorted((crate_dir / "src").rglob("*.rs"))
    if (crate_dir / "build.rs").is_file():
        rs_queue.append(crate_dir / "build.rs")
    seen_rs: set[Path] = set()
    while rs_queue:
        rs = rs_queue.pop().resolve()
        if rs in seen_rs or not rs.is_file():
            continue
        seen_rs.add(rs)
        text = mask_test_items(rs.read_text())
        for m in PATH_ATTR_RE.finditer(text):
            target = (rs.parent / m.group(1)).resolve()
            refs.add(target)
            if target.suffix == ".rs":
                rs_queue.append(target)
        for m in INCLUDE_RE.finditer(text):
            refs.add((rs.parent / m.group(1)).resolve())
    return refs


def main() -> int:
    """CLI entry point; prints per-crate result and the denominator, exit 1 on any gap."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo-root", default=".", type=Path)
    ap.add_argument("--dockerfile", default="bundles/Dockerfile.core-bundles")
    args = ap.parse_args()
    root = args.repo_root.resolve()
    parents, copies = parse_stages((root / args.dockerfile).read_text())

    examined = refs_total = skipped = 0
    failures: list[str] = []
    for toml in sorted((root / "bundles" / "rust").glob("*/Cargo.toml")):
        crate = toml.parent.name
        stage = f"bundle-{crate}"
        if stage not in copies:
            print(f"  SKIP {crate}: no {stage} stage in {args.dockerfile}")
            skipped += 1
            continue
        examined += 1
        covered = stage_coverage(stage, parents, copies)
        for ref in sorted(crate_refs(toml.parent)):
            refs_total += 1
            try:
                rel = ref.relative_to(root).as_posix()
            except ValueError:
                failures.append(f"{crate}: {ref} is outside the repo build context")
                continue
            if not is_covered(rel, covered):
                failures.append(f"{crate}: {rel} not COPYed into stage {stage}")

    for f in failures:
        print(f"  FAIL {f}")
    print(
        f"bundle-docker-context check: crates_examined={examined} skipped={skipped} "
        f"refs_examined={refs_total} failing={len(failures)}"
    )
    if examined == 0 or refs_total == 0:
        print("FAIL: zero crates/refs examined", file=sys.stderr)
        return 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
