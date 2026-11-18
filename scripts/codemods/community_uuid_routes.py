#!/usr/bin/env python3
"""Rewrite numeric/untyped community path params to the `cuuid` converter.

Client-facing community identifiers are public UUIDs (see
`hub_api/services/community_public_id.py`). This codemod rewrites, in place:

* every route decorator rule in `hub_api/blueprints/**.py` using `<int:community_id>`,
  `<string:community_id>` or `<community_id>` (also module-level path constants and
  `url_prefix`) -> `<cuuid:community_id>`;
* the route-rule keyed audit tables in `hub_api/services/audit_events.py` (their keys are
  matched against `request.url_rule.rule`, so they must track the rules).

It never touches non-community params, internal gRPC code, tests, or routes that already take a
UUID (`<community_public_id>` / `<community_uuid>` / `<cuuid:...>`). `event.py` ProxyRoute
tables are intentionally left as data: `_register()` applies the converter when it builds rules.
Idempotent: a second run changes nothing. Usage:
    python3 scripts/codemods/community_uuid_routes.py [--dry-run] [--root DIR] [--report FILE]
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROUTE_ATTRS = frozenset({"route", "get", "post", "put", "patch", "delete", "websocket"})
_PARAM_RE = re.compile(r"<(?:(?:int|string):)?community_id>")
NEW_PARAM = "<cuuid:community_id>"
BLUEPRINT_DIR = Path("hub_api/blueprints")
AUDIT_FILE = Path("hub_api/services/audit_events.py")
PROXY_FILE = "event.py"


@dataclass
class FileResult:
    """Per-file outcome: rules rewritten and literals deliberately skipped."""

    path: str
    changed: int = 0
    skipped: list[str] = field(default_factory=list)


def _offsets(src: str) -> list[int]:
    """Absolute start offset of each source line."""
    starts = [0]
    for line in src.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    return starts


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """`id()` of every docstring Constant (never rewritten, never reported)."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _span(node: ast.expr, starts: list[int]) -> tuple[int, int]:
    """Absolute `(start, end)` source offsets of `node`."""
    assert node.end_lineno is not None and node.end_col_offset is not None  # noqa: S101
    return (
        starts[node.lineno - 1] + node.col_offset,
        starts[node.end_lineno - 1] + node.end_col_offset,
    )


def _internal_blueprints(tree: ast.AST) -> set[str]:
    """Names bound to a `Blueprint(...)` whose `url_prefix` is under `/internal` (stay numeric)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and getattr(node.value.func, "id", "") == "Blueprint"
            and isinstance(node.targets[0], ast.Name)
        ):
            for kw in node.value.keywords:
                if (
                    kw.arg == "url_prefix"
                    and isinstance(kw.value, ast.Constant)
                    and "/internal" in str(kw.value.value)
                ):
                    names.add(node.targets[0].id)
    return names


def _internal_rule_ids(tree: ast.AST, internal: set[str]) -> set[int]:
    """`id()` of rule constants registered on an internal blueprint."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in internal
            and node.args
        ):
            ids.add(id(node.args[0]))
    return ids


def _decorator_rule_nodes(tree: ast.AST, internal: set[str]) -> list[ast.Constant]:
    """First-arg string constants of route calls; internal-blueprint routes are excluded."""
    found: list[ast.Constant] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in internal
        ):
            continue
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ROUTE_ATTRS
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            found.append(node.args[0])
    return found


def rewrite_file(path: Path, rel: str, *, audit: bool) -> tuple[str, FileResult]:
    """Return `(new_source, result)` for one file."""
    src = path.read_text()
    tree = ast.parse(src)
    starts = _offsets(src)
    result = FileResult(rel)
    doc_ids = _docstring_nodes(tree)
    internal = _internal_blueprints(tree)
    internal_ids = _internal_rule_ids(tree, internal)
    if audit:
        targets = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in doc_ids
        ]
    elif rel.endswith(PROXY_FILE):
        targets = []
    else:
        # Decorator rules plus module-level path constants (`_BASE = "/communities/<...>"`,
        # `Blueprint(url_prefix=...)`) that are later composed into route rules.
        targets = _decorator_rule_nodes(tree, internal) + [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant)
            and isinstance(n.value, str)
            and n.value.startswith("/")
            and id(n) not in doc_ids
            and id(n) not in internal_ids
        ]
    edits: list[tuple[int, int, str]] = []
    handled: set[int] = set()
    for node in targets:
        if id(node) in handled:
            continue
        handled.add(id(node))
        if not isinstance(node.value, str) or not _PARAM_RE.search(node.value):
            continue
        lo, hi = _span(node, starts)
        new_segment, count = _PARAM_RE.subn(NEW_PARAM, src[lo:hi])
        if count:
            edits.append((lo, hi, new_segment))
            result.changed += count if audit else 1
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in handled
            and id(node) not in doc_ids
            and _PARAM_RE.search(node.value)
        ):
            reason = (
                "internal (service-key) blueprint route: stays numeric"
                if id(node) in internal_ids
                else "ProxyRoute table rule (converter applied by the _register builder)"
                if rel.endswith(PROXY_FILE)
                else "not a route-decorator rule (non-decorator literal)"
            )
            result.skipped.append(f"{rel}:{node.lineno}: {node.value[:90]!r}: {reason}")
    for lo, hi, text in sorted(edits, reverse=True):
        src = src[:lo] + text + src[hi:]
    return src, result


def run(root: Path, *, dry_run: bool) -> list[FileResult]:
    """Process every target file under `root`."""
    results: list[FileResult] = []
    for path in sorted((root / BLUEPRINT_DIR).rglob("*.py")):
        rel = str(path.relative_to(root))
        new_src, res = rewrite_file(path, rel, audit=False)
        if res.changed or res.skipped:
            results.append(res)
        if res.changed and not dry_run:
            path.write_text(new_src)
    audit_path = root / AUDIT_FILE
    new_src, res = rewrite_file(audit_path, str(AUDIT_FILE), audit=True)
    results.append(res)
    if res.changed and not dry_run:
        audit_path.write_text(new_src)
    return results


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    ap.add_argument("--root", default=".", help="repo root")
    ap.add_argument("--report", help="write a JSON report to this file")
    args = ap.parse_args(argv)
    results = run(Path(args.root), dry_run=args.dry_run)
    total = sum(r.changed for r in results)
    mode = "DRY-RUN" if args.dry_run else "APPLIED"
    print(f"[{mode}] files changed: {sum(1 for r in results if r.changed)}, rules: {total}")
    for r in results:
        if r.changed:
            print(f"  {r.changed:4d}  {r.path}")
    skipped = [s for r in results for s in r.skipped]
    print(f"skipped literals: {len(skipped)}")
    for s in skipped:
        print(f"  SKIP {s}")
    if args.report:
        Path(args.report).write_text(
            json.dumps(
                {
                    "mode": mode,
                    "total": total,
                    "files": {r.path: r.changed for r in results if r.changed},
                    "skipped": skipped,
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
