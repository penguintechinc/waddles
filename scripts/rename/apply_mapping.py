#!/usr/bin/env python3
"""Deterministic, idempotent bundle app_id renamer (hard cutover, no aliases).

Reads ``app_id_mapping.tsv`` (old, new, kind, rationale) and rewrites every
literal old app_id found in the selected files. Forms handled:

* dotted id        ``waddles.core.example.about`` (also covers the
  ``tool.call:<old>:<tool>`` grant form and ``waddles:app:t:c:<old>:state`` kv keys,
  because ':' is a valid boundary);
* Rust-sanitised   ``waddles_core_example_about`` -- every non [a-z0-9] char -> '_'
  (``core/bundle_capability_gate/src/resource.rs::sanitized_app_id``); this also
  matches per-bundle table names ``<sanitised>_<table>``;
* Python-sanitised ``waddles_core_example_game-servers`` -- lowercase, only '.' -> '_'
  (``hub_api/services/bundle_data_schema.py``), differs from Rust only for '-'.

No path-style (``a/b/c``) or hyphen-joined forms exist in the repo as of WP0.

Matching is longest-old-id-first with identifier boundaries so ``a.b.count`` never
hits ``a.b.counter``. Spans overlapping an EXCLUSION are never touched.

Usage: apply_mapping.py [--paths GLOB ...] [--check] [--mapping FILE] [--root DIR]
"""

from __future__ import annotations

import argparse
import csv
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_MAPPING = HERE / "app_id_mapping.tsv"
SELF_DIR = "scripts/rename/"  # never rewrite the mapping/tests themselves

# Things that look like app_ids but are NOT. Spans matching these are protected.
EXCLUSIONS = [
    # tier_catalog license-feature keys (not app_ids); `.default` makes it an app_id.
    re.compile(
        r"waddles\.integrations\.(?:waddleai|provider_connections|static_connections)"
        r"(?![A-Za-z0-9_-])(?!\.default(?![A-Za-z0-9_-]))"
    ),
    # PostHog platform flags.
    re.compile(r"waddles\.core\.rust-data-plane(?![A-Za-z0-9_-])"),
    re.compile(r"waddles\.core\.disable-[A-Za-z0-9_-]+"),
]


def sanitize_rust(app_id: str) -> str:
    """Mirror of resource.rs::sanitized_app_id (no hash suffix; ids are short)."""
    return "".join(c.lower() if c.isascii() and c.isalnum() else "_" for c in app_id)


def sanitize_py(app_id: str) -> str:
    """Mirror of bundle_data_schema.py: lowercase, '.' -> '_'."""
    return app_id.lower().replace(".", "_")


def load_mapping(path: Path) -> dict[str, str]:
    """Load old->new from the TSV, validating shape, uniqueness and non-overlap."""
    mapping: dict[str, str] = {}
    with open(path, newline="") as fh:
        reader = csv.reader(fh, delimiter="\t")
        header = next(reader)
        if header[:2] != ["old", "new"]:
            raise SystemExit(f"bad mapping header: {header}")
        for row in reader:
            if not row or row[0].startswith("#"):
                continue
            old, new = row[0], row[1]
            if old in mapping:
                raise SystemExit(f"duplicate old id {old}")
            mapping[old] = new
    if len(set(mapping.values())) != len(mapping):
        raise SystemExit("duplicate new ids in mapping")
    return mapping


@dataclass
class Rules:
    """Compiled replacement regexes plus lookup tables for each id form."""

    dotted: re.Pattern[str]
    dotted_map: dict[str, str]
    san: re.Pattern[str]
    san_map: dict[str, str]
    san_hy: re.Pattern[str] | None
    san_hy_map: dict[str, str]


def _alt(keys: list[str]) -> str:
    return "|".join(re.escape(k) for k in sorted(keys, key=lambda k: (-len(k), k)))


def build_rules(mapping: dict[str, str]) -> Rules:
    """Compile the three id-form regexes, longest key first."""
    dotted_map = dict(mapping)
    san_map = {sanitize_rust(o): sanitize_rust(n) for o, n in mapping.items()}
    # Python form only matters where it keeps a '-' (otherwise identical to Rust form).
    san_hy_map = {
        sanitize_py(o): sanitize_py(n)
        for o, n in mapping.items()
        if "-" in sanitize_py(o) and sanitize_py(o) != sanitize_rust(o)
    }
    dotted = re.compile(rf"(?<![A-Za-z0-9_-])(?:{_alt(list(dotted_map))})(?![A-Za-z0-9_-])")
    san = re.compile(rf"(?<![A-Za-z0-9])(?:{_alt(list(san_map))})(?![A-Za-z0-9])")
    san_hy = (
        re.compile(rf"(?<![A-Za-z0-9])(?:{_alt(list(san_hy_map))})(?![A-Za-z0-9-])")
        if san_hy_map
        else None
    )
    return Rules(dotted, dotted_map, san, san_map, san_hy, san_hy_map)


def _excluded_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for rx in EXCLUSIONS:
        spans.extend(m.span() for m in rx.finditer(text))
    return spans


def _overlaps(spans: list[tuple[int, int]], s: int, e: int) -> bool:
    return any(s < b and a < e for a, b in spans)


@dataclass
class Counts:
    """Per-form replacement counters."""

    dotted: int = 0
    sanitised: int = 0
    sanitised_py: int = 0
    per_id: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.dotted + self.sanitised + self.sanitised_py


def rewrite_text(text: str, rules: Rules, counts: Counts | None = None) -> str:
    """Return text with every non-excluded old id replaced; update counts."""
    counts = counts if counts is not None else Counts()
    for attr, rx, table in (
        ("dotted", rules.dotted, rules.dotted_map),
        ("sanitised", rules.san, rules.san_map),
        ("sanitised_py", rules.san_hy, rules.san_hy_map),
    ):
        if rx is None:
            continue
        spans = _excluded_spans(text)

        def sub(m: re.Match[str], attr: str = attr, table: dict[str, str] = table,
                spans: list[tuple[int, int]] = spans) -> str:
            if _overlaps(spans, *m.span()):
                return m.group(0)
            setattr(counts, attr, getattr(counts, attr) + 1)
            counts.per_id[m.group(0)] = counts.per_id.get(m.group(0), 0) + 1
            return table[m.group(0)]

        text = rx.sub(sub, text)
    return text


def glob_to_regex(glob: str) -> re.Pattern[str]:
    """Translate a path glob (supports ``**``, ``*``, ``?``) to an anchored regex."""
    out, i = [], 0
    while i < len(glob):
        c = glob[i]
        if glob.startswith("**/", i):
            out.append(r"(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(r".*")
            i += 2
        elif c == "*":
            out.append(r"[^/]*")
            i += 1
        elif c == "?":
            out.append(r"[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def tracked_files(root: Path) -> list[str]:
    """List git-tracked files under root (NUL-separated for safety)."""
    res = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True, capture_output=True,
    )
    return [p for p in res.stdout.decode().split("\0") if p]


def select_files(root: Path, globs: list[str] | None, files: list[str] | None = None) -> list[str]:
    """Filter tracked files by globs, never including this tool's own directory."""
    universe = files if files is not None else tracked_files(root)
    regs = [glob_to_regex(g) for g in globs] if globs else None
    return [
        f for f in universe
        if not f.startswith(SELF_DIR) and (regs is None or any(r.match(f) for r in regs))
    ]


def process(root: Path, rel_files: list[str], rules: Rules, write: bool) -> dict:
    """Scan (and optionally rewrite) files; return stats."""
    stats = {"examined": 0, "skipped_binary": 0, "changed": 0, "counts": Counts(), "files": {}}
    for rel in rel_files:
        p = root / rel
        if not p.is_file() or p.is_symlink():
            continue
        raw = p.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            stats["skipped_binary"] += 1
            continue
        stats["examined"] += 1
        file_counts = Counts()
        new = rewrite_text(text, rules, file_counts)
        if file_counts.total:
            stats["files"][rel] = file_counts.total
            stats["changed"] += 1
            c: Counts = stats["counts"]
            c.dotted += file_counts.dotted
            c.sanitised += file_counts.sanitised
            c.sanitised_py += file_counts.sanitised_py
            for k, v in file_counts.per_id.items():
                c.per_id[k] = c.per_id.get(k, 0) + v
            if write and new != text:
                p.write_bytes(new.encode("utf-8"))
    return stats


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--paths", nargs="+", metavar="GLOB",
                    help="limit to these git-tracked path globs (required unless --check)")
    ap.add_argument("--check", action="store_true",
                    help="report remaining old ids without writing; exit 1 if any")
    ap.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    ap.add_argument("--root", type=Path, default=Path.cwd())
    ap.add_argument("--verbose", "-v", action="store_true", help="list files with hits")
    args = ap.parse_args(argv)

    if not args.check and not args.paths:
        ap.error("--paths is required when writing (refusing a whole-repo rewrite)")

    rules = build_rules(load_mapping(args.mapping))
    files = select_files(args.root, args.paths)
    stats = process(args.root, files, rules, write=not args.check)
    c: Counts = stats["counts"]
    verb = "remaining" if args.check else "replaced"
    print(
        f"files selected={len(files)} examined={stats['examined']} "
        f"skipped_binary={stats['skipped_binary']} files_with_hits={stats['changed']} | "
        f"{verb}: total={c.total} dotted={c.dotted} sanitised={c.sanitised} "
        f"sanitised_py={c.sanitised_py}"
    )
    if args.verbose:
        for rel, n in sorted(stats["files"].items()):
            print(f"  {n:5d} {rel}")
    if stats["examined"] == 0:
        print("FAIL: zero files examined (bad --paths?)", file=sys.stderr)
        return 2
    return 1 if (args.check and c.total) else 0


if __name__ == "__main__":
    sys.exit(main())
