#!/usr/bin/env python3
"""Hard gate: every live Python `builtin_handlers/*` module has a Rust-path / bundle mapping.

Why (gh #840 PR4): the Python ingest/process/action handlers in
`core/svc_{ingest,process,action}/builtin_handlers/` are being replaced by the
Rust data plane (`svc_ingest/src/normalize.rs`, `svc_action/src/senders.rs`) and
by installable connector/feature bundles (`bundles/`). Removing the Python
service must never silently drop a platform or feature. This gate makes the
migration state machine-checked against `.ci/builtin-handler-map.yaml`:

  * every handler module on disk MUST have an entry (a new, unmapped handler FAILS);
  * a `covered` entry's targets MUST exist and their `contains` evidence regex MUST
    match -- the mapping is verified against the real Rust source / bundle manifest,
    never trusted; `parity: verified` additionally needs golden-vector corpus
    files (tests/data_plane_parity) that actually exist;
  * a `pending` entry is an acknowledged gap (needs `gap` + `tracking`) -- allowed
    while the Python handler still exists, FATAL the moment the handler is
    removed from disk (a platform dropped before its replacement landed);
  * an entry whose handler is gone must be deleted (stale entry);
  * every `builtin_handlers.<module>:<fn>` entrypoint referenced from service
    source must resolve to a real handler module + function (no dangling stage
    entrypoint in the app catalog).

`--strict` additionally fails on ANY `pending` entry -- this is the mode the
"remove Python svc-ingest/process/action" PR turns on.

Exit code is the gate (critical-rules.md Verification Integrity): non-zero on
any violation and on ZERO handlers examined. Denominators and the full
covered/pending inventory are always printed.

Usage: check-builtin-handler-inventory.py [--root DIR] [--map FILE] [--strict]
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

STAGES = ("ingest", "process", "action")
SERVICE_ROOT = "core"
MAP_DEFAULT = ".ci/builtin-handler-map.yaml"
TARGET_KINDS = frozenset({"rust-path", "bundle"})
STATUSES = frozenset({"covered", "pending"})
PARITY = frozenset({"verified", "unverified"})
#: Only a fully-quoted literal counts as a declared entrypoint -- prose that merely mentions a
#: path (and may line-wrap mid-identifier in a docstring) must not trip the gate.
ENTRYPOINT_RE = re.compile(r"""["']builtin_handlers\.([A-Za-z0-9_]+):([A-Za-z0-9_]+)["']""")
SKIP_DIRS = frozenset({"tests", "target", "node_modules", "__pycache__", ".venv", "venv"})


@dataclass(slots=True)
class Entry:
    """One parsed mapping entry."""

    handler: str
    status: str
    parity: str | None
    targets: list[dict[str, Any]]
    corpus: list[str]
    gap: str
    tracking: str


@dataclass(slots=True)
class Report:
    """Counters + violations for one run."""

    handlers: list[str] = field(default_factory=list)
    covered: list[Entry] = field(default_factory=list)
    pending: list[Entry] = field(default_factory=list)
    entrypoints: int = 0
    violations: list[str] = field(default_factory=list)


def discover_handlers(root: Path) -> list[str]:
    """Return repo-relative paths of every handler module (never `__init__.py`)."""
    found: list[str] = []
    for stage in STAGES:
        directory = root / SERVICE_ROOT / f"svc_{stage}" / "builtin_handlers"
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.py")):
            if path.name != "__init__.py":
                found.append(path.relative_to(root).as_posix())
    return found


def load_map(path: Path, report: Report) -> list[Entry]:
    """Parse the mapping file; structural problems become violations."""
    if not path.is_file():
        report.violations.append(f"mapping file missing: {path}")
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = data.get("handlers") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        report.violations.append(f"{path}: top-level `handlers:` list is required")
        return []
    entries: list[Entry] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or not isinstance(item.get("handler"), str):
            report.violations.append(f"{path}: entry #{index} needs a string `handler`")
            continue
        entries.append(
            Entry(
                handler=item["handler"],
                status=str(item.get("status", "")),
                parity=item.get("parity"),
                targets=list(item.get("targets") or []),
                corpus=list(item.get("corpus") or []),
                gap=str(item.get("gap") or ""),
                tracking=str(item.get("tracking") or ""),
            )
        )
    return entries


def check_target(root: Path, entry: Entry, target: dict[str, Any], report: Report) -> None:
    """A covered entry's target must exist and its evidence regex must match."""
    kind, rel = target.get("kind"), target.get("path")
    where = f"{entry.handler}: target {rel!r}"
    if kind not in TARGET_KINDS:
        report.violations.append(
            f"{where}: kind must be one of {sorted(TARGET_KINDS)}, got {kind!r}"
        )
        return
    if not isinstance(rel, str) or not (root / rel).is_file():
        report.violations.append(f"{where}: file does not exist")
        return
    evidence = target.get("contains")
    if not isinstance(evidence, str) or not evidence:
        report.violations.append(
            f"{where}: `contains` evidence regex is required (mapping must be verifiable)"
        )
        return
    text = (root / rel).read_text(encoding="utf-8", errors="replace")
    if re.search(evidence, text) is None:
        report.violations.append(
            f"{where}: evidence {evidence!r} not found -- mapping is stale or wrong"
        )
    if kind == "bundle" and not rel.startswith("bundles/"):
        report.violations.append(f"{where}: a bundle target must live under bundles/")
    if kind == "rust-path" and not rel.endswith(".rs"):
        report.violations.append(f"{where}: a rust-path target must be a .rs file")


def check_entry(root: Path, entry: Entry, on_disk: set[str], report: Report) -> None:
    """Validate one mapping entry against the tree."""
    exists = entry.handler in on_disk
    if entry.status not in STATUSES:
        report.violations.append(
            f"{entry.handler}: status must be one of {sorted(STATUSES)}, got {entry.status!r}"
        )
        return
    if not exists:
        if entry.status == "pending":
            report.violations.append(
                f"{entry.handler}: Python handler REMOVED while still `pending` -- "
                "platform/feature dropped before its Rust/bundle replacement landed"
            )
        else:
            report.violations.append(
                f"{entry.handler}: stale entry -- handler no longer exists, delete it from the map"
            )
        return

    if entry.status == "pending":
        if not entry.gap or not entry.tracking:
            report.violations.append(
                f"{entry.handler}: pending entries need both `gap` and `tracking`"
            )
        report.pending.append(entry)
        return

    if not entry.targets:
        report.violations.append(f"{entry.handler}: covered entries need at least one target")
    for target in entry.targets:
        check_target(root, entry, target, report)
    if entry.parity not in PARITY:
        report.violations.append(
            f"{entry.handler}: covered entries need parity: verified|unverified"
        )
    if entry.parity == "verified":
        if not any(t.get("kind") == "rust-path" for t in entry.targets):
            report.violations.append(
                f"{entry.handler}: parity `verified` requires a rust-path target"
            )
        matched = [
            p
            for pattern in entry.corpus
            for p in (root / "tests/data_plane_parity/corpus").glob(pattern)
        ]
        if not entry.corpus or not matched:
            report.violations.append(
                f"{entry.handler}: parity `verified` requires `corpus` globs matching real "
                "tests/data_plane_parity/corpus vectors"
            )
    report.covered.append(entry)


def check_entrypoints(root: Path, handlers: list[str], report: Report) -> None:
    """Every `builtin_handlers.<mod>:<fn>` referenced from service source must resolve."""
    modules = {Path(h).stem: root / h for h in handlers}
    seen: set[tuple[str, str]] = set()
    for path in sorted((root / SERVICE_ROOT).rglob("*.py")):
        parts = set(path.relative_to(root).parts)
        if parts & SKIP_DIRS or path.name.startswith("test_"):
            continue
        for module, function in ENTRYPOINT_RE.findall(
            path.read_text(encoding="utf-8", errors="replace")
        ):
            seen.add((module, function))
    report.entrypoints = len(seen)
    for module, function in sorted(seen):
        target = modules.get(module)
        if target is None:
            report.violations.append(
                f"dangling stage entrypoint builtin_handlers.{module}:{function} -- no such handler module"
            )
        elif (
            re.search(
                rf"^(?:async\s+)?def\s+{re.escape(function)}\s*\(",
                target.read_text(encoding="utf-8"),
                re.M,
            )
            is None
        ):
            report.violations.append(
                f"dangling stage entrypoint builtin_handlers.{module}:{function} -- function not defined"
            )


def run(root: Path, map_path: Path, strict: bool) -> Report:
    """Run every check and return the report."""
    report = Report()
    handlers = discover_handlers(root)
    report.handlers = handlers
    entries = load_map(map_path, report)

    seen: set[str] = set()
    for entry in entries:
        if entry.handler in seen:
            report.violations.append(f"{entry.handler}: duplicate map entry")
        seen.add(entry.handler)

    on_disk = set(handlers)
    for entry in entries:
        check_entry(root, entry, on_disk, report)
    for handler in sorted(on_disk - seen):
        report.violations.append(
            f"{handler}: live Python handler has NO Rust-path/bundle mapping in {MAP_DEFAULT} "
            "(add a `covered` entry with evidence, or an explicit `pending` entry with a tracking issue)"
        )
    check_entrypoints(root, handlers, report)

    if not handlers:
        report.violations.append(
            "zero handler modules discovered -- scanner pointed at the wrong root"
        )
    if strict:
        for entry in report.pending:
            report.violations.append(f"{entry.handler}: --strict forbids pending ({entry.gap})")
    return report


def print_report(report: Report, strict: bool) -> None:
    """Print denominators, the covered/pending inventory, then violations."""
    verified = sum(1 for e in report.covered if e.parity == "verified")
    print(
        f"[handler-inventory] handlers={len(report.handlers)} "
        f"covered={len(report.covered)} (parity verified={verified}, unverified={len(report.covered) - verified}) "
        f"pending={len(report.pending)} stage-entrypoints-checked={report.entrypoints} strict={strict}"
    )
    for stage in STAGES:
        prefix = f"{SERVICE_ROOT}/svc_{stage}/builtin_handlers/"
        stage_handlers = [h for h in report.handlers if h.startswith(prefix)]
        covered = [e for e in report.covered if e.handler.startswith(prefix)]
        pending = [e for e in report.pending if e.handler.startswith(prefix)]
        print(
            f"[handler-inventory] {stage}: {len(stage_handlers)} handlers, {len(covered)} covered, {len(pending)} pending"
        )
        for e in covered:
            kinds = "+".join(sorted({str(t.get("kind")) for t in e.targets}))
            print(
                f"[handler-inventory]   covered  {Path(e.handler).stem:<34} {kinds:<10} parity={e.parity}"
            )
        for e in pending:
            print(
                f"[handler-inventory]   PENDING  {Path(e.handler).stem:<34} {e.tracking}  {e.gap}"
            )
    for violation in report.violations:
        print(f"[handler-inventory] FAIL: {violation}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--map", type=Path, default=None)
    parser.add_argument(
        "--strict", action="store_true", help="fail on any pending (Python-removal mode)"
    )
    args = parser.parse_args(argv)
    map_path = args.map or args.root / MAP_DEFAULT
    report = run(args.root, map_path, args.strict)
    print_report(report, args.strict)
    return 1 if report.violations else 0


if __name__ == "__main__":
    sys.exit(main())
