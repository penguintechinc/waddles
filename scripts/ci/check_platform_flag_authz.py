#!/usr/bin/env python3
"""Fail when a bundle authorizes on a per-message platform badge flag.

`is_mod` / `is_broadcaster` (and the IsMod / IsModerator / IsBroadcaster spellings) are
platform display badges stamped on each chat event. They are NOT an authorization source:
bundles must ask the host via `caller_can(scope)` instead. This gate scans every bundle
source file (Python, Rust, C#, JS/TS under `bundles/`, tests and fixtures excluded) and
fails on any code read of such a flag that is neither allowlisted nor marked display-only.

Display-only reads (formatting a badge, never an auth decision) carry a pragma on the same
line or the line above: `# display-only: platform-flag` (Python) or
`// display-only: platform-flag` (Rust/C#/JS).

The allowlist (`scripts/ci/platform-flag-authz-allowlist.txt`) lists one file per line, each
with a `# caller-can PR (feature/sdk-caller-can) batch N` reference. It may only shrink:
an entry with no reference, or whose file no longer has hits, is itself a failure.

Prints `files scanned: N, hits: H, allowlisted files: A`; exits non-zero on any violation
or when zero files were scanned.

Usage: check_platform_flag_authz.py [--root DIR] [--allowlist FILE]
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

FLAG_RE = re.compile(r"(?i)(?<![A-Za-z0-9])(is_?mod(?:erator)?|is_?broadcaster)(?![A-Za-z0-9])")
FLAG_EXACT = re.compile(r"(?i)^(is_?mod(?:erator)?|is_?broadcaster)$")
PRAGMA_RE = re.compile(r"(?:#|//)\s*display-only:\s*platform-flag\b")
REF_RE = re.compile(r"#\s*caller-can PR \(feature/sdk-caller-can\) batch (\d+)\s*$")
EXTS = {".py", ".rs", ".cs", ".js", ".mjs", ".ts"}
SKIP_DIRS = {
    "tests", "test", "fixtures", "fixture", "__tests__", "node_modules", "target", "bin", "obj",
    ".venv", "venv", "__pycache__", "dist", "build",
}
DEFAULT_ALLOWLIST = "scripts/ci/platform-flag-authz-allowlist.txt"


def is_test_path(rel: Path) -> bool:
    """True for test/fixture sources that the scan must skip."""
    if any(part in SKIP_DIRS or part.endswith(".Tests") for part in rel.parts[:-1]):
        return True
    name = rel.name
    return (
        name.startswith("test_") or name.endswith(("_test.py", "_test.rs", "Tests.cs", "Test.cs"))
        or ".test." in name or ".spec." in name or name == "conftest.py"
    )


def iter_sources(root: Path) -> list[Path]:
    """Every bundle source file under `root/bundles`, tests and fixtures excluded."""
    base = root / "bundles"
    if not base.is_dir():
        return []
    out = []
    for p in sorted(base.rglob("*")):
        if p.is_file() and p.suffix in EXTS and not is_test_path(p.relative_to(root)):
            out.append(p)
    return out


def _strip_c_comments(text: str) -> str:
    """Blank out // and /* */ comments (line count preserved)."""
    text = re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)
    return "\n".join(re.sub(r"//.*$", "", ln) for ln in text.split("\n"))


def _python_hit_lines(text: str) -> set[int]:
    """Line numbers of code reads of a flag in Python source (AST; comments/docstrings ignored)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return {
            i for i, ln in enumerate(text.split("\n"), 1)
            if FLAG_RE.search(re.sub(r"#.*$", "", ln))
        }
    lines: set[int] = set()
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.arg):
            name = node.arg
        elif isinstance(node, ast.keyword):
            name = node.arg
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = node.name
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            name = node.value
        if name and FLAG_EXACT.match(name):
            lines.add(getattr(node, "lineno", 0))
    return lines


def _other_hit_lines(text: str, suffix: str) -> set[int]:
    """Line numbers of flag mentions in Rust/C#/JS code (comments stripped; Rust test mods cut)."""
    if suffix == ".rs":
        cut = re.search(r"^\s*#\[cfg\(test\)\]", text, flags=re.M)
        if cut:
            text = text[: cut.start()]
    return {i for i, ln in enumerate(_strip_c_comments(text).split("\n"), 1) if FLAG_RE.search(ln)}


def file_hits(path: Path) -> list[int]:
    """Unsuppressed hit line numbers for one source file."""
    text = path.read_text(encoding="utf-8", errors="replace")
    raw = _python_hit_lines(text) if path.suffix == ".py" else _other_hit_lines(text, path.suffix)
    src = text.split("\n")
    out = []
    for ln in sorted(raw):
        here = src[ln - 1] if 0 < ln <= len(src) else ""
        prev = src[ln - 2] if ln >= 2 else ""
        if PRAGMA_RE.search(here) or PRAGMA_RE.search(prev):
            continue
        out.append(ln)
    return out


def load_allowlist(path: Path) -> tuple[dict[str, int], list[str]]:
    """Parse the allowlist into {file: batch} plus a list of malformed-entry errors."""
    entries: dict[str, int] = {}
    errors: list[str] = []
    if not path.is_file():
        return entries, [f"allowlist missing: {path}"]
    for n, raw in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        target, _, rest = line.partition("#")
        target = target.strip()
        m = REF_RE.search("#" + rest) if rest else None
        if not m:
            errors.append(f"{path.name}:{n}: `{target}` lacks `# caller-can PR (feature/sdk-caller-can) batch N`")
            continue
        entries[target] = int(m.group(1))
    return entries, errors


def run(root: Path, allowlist: Path) -> int:
    """Run the gate; returns the process exit code."""
    sources = iter_sources(root)
    entries, errors = load_allowlist(allowlist)
    hit_files: dict[str, list[int]] = {}
    total_hits = 0
    for p in sources:
        hits = file_hits(p)
        if hits:
            hit_files[p.relative_to(root).as_posix()] = hits
            total_hits += len(hits)
    print(f"files scanned: {len(sources)}, hits: {total_hits}, allowlisted files: {len(entries)}")
    if not sources:
        errors.append("zero bundle source files scanned (wrong --root?)")
    violations = {f: h for f, h in hit_files.items() if f not in entries}
    for f, h in sorted(violations.items()):
        errors.append(
            f"{f}: authorization-style read of a platform badge flag at line(s) {h}; use "
            "caller_can(scope) or mark display-only with `display-only: platform-flag`"
        )
    for f in sorted(entries):
        if f not in hit_files:
            errors.append(f"stale allowlist entry (no hits, remove it): {f}")
    if errors:
        print(f"FAIL: {len(violations)} non-allowlisted file(s), {len(errors)} problem(s)", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1
    print("OK")
    return 0


def main() -> int:
    """CLI entry point."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument("--allowlist", default=None)
    a = ap.parse_args()
    root = Path(a.root)
    return run(root, Path(a.allowlist) if a.allowlist else root / DEFAULT_ALLOWLIST)


if __name__ == "__main__":
    sys.exit(main())
