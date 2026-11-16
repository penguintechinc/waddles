#!/usr/bin/env python3
"""Inventory hub-api route handlers and the DB access shape of each.

Walks `hub_api/blueprints/**` with the AST, emits one JSON row per route
handler (file, function, path, methods, whether it touches a DAL, which
write-looking calls it makes, and its non-route decorators). It is the
mechanical first pass behind `docs/hub-api-db-routing.md`; a human then
classifies. Usage: `python3 scripts/hubapi_db_inventory.py OUT.json`.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

WRITE_CALL = re.compile(
    r"(insert|update|delete|create|set_|add_|remove|revoke|grant|upsert|record|save|write|"
    r"mark|approve|reject|install|activate|enable|disable|rotate|register|submit|join|leave|"
    r"link|unlink|accept|cancel|publish|sync|bind|reset|commit|store|issue|consume|redeem|"
    r"purge|anonymi|erase|ban_|vote|enqueue|dequeue|skip|reorder)",
    re.IGNORECASE,
)
READ_PREFIX = (
    "get",
    "list",
    "find",
    "fetch",
    "read",
    "is_",
    "has",
    "load",
    "resolve",
    "_get",
    "_list",
)
DAL_USE = re.compile(r"_dal\(|async_dal|config\[\"dal\"\]|\bdal\b")


def _route_info(dec: ast.expr) -> tuple[str, list[str]] | None:
    """Return `(path, methods)` when `dec` is a `*.route(...)` decorator, else None."""
    if not (
        isinstance(dec, ast.Call)
        and isinstance(dec.func, ast.Attribute)
        and dec.func.attr == "route"
    ):
        return None
    path = "?"
    if dec.args and isinstance(dec.args[0], ast.Constant):
        path = str(dec.args[0].value)
    methods = ["GET"]
    for kw in dec.keywords:
        if kw.arg == "methods":
            try:
                methods = list(ast.literal_eval(kw.value))
            except (ValueError, SyntaxError):
                methods = ["?"]
    return path, methods


def inventory(root: Path) -> list[dict[str, object]]:
    """Collect one row per route handler under `root/blueprints`."""
    rows: list[dict[str, object]] = []
    for path in sorted((root / "blueprints").rglob("*.py")):
        src = path.read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            for dec in node.decorator_list:
                info = _route_info(dec)
                if info is None:
                    continue
                seg = ast.get_source_segment(src, node) or ""
                calls = {
                    (
                        c.func.attr
                        if isinstance(c.func, ast.Attribute)
                        else getattr(c.func, "id", "")
                    )
                    for c in ast.walk(node)
                    if isinstance(c, ast.Call)
                }
                writes = sorted(
                    c
                    for c in calls
                    if c and WRITE_CALL.search(c) and not c.startswith(READ_PREFIX)
                )
                decos = [
                    ast.unparse(d)[:40]
                    for d in node.decorator_list
                    if _route_info(d) is None
                ]
                rows.append(
                    {
                        "file": str(path.relative_to(root)),
                        "fn": node.name,
                        "path": info[0],
                        "methods": info[1],
                        "uses_dal": bool(DAL_USE.search(seg)),
                        "writes": writes,
                        "decos": decos,
                        "line": node.lineno,
                    }
                )
    return rows


def main() -> int:
    """Write the inventory JSON to argv[1] and print a one-line denominator."""
    if len(sys.argv) != 2:
        print("usage: hubapi_db_inventory.py OUT.json", file=sys.stderr)
        return 2
    rows = inventory(Path(__file__).resolve().parent.parent / "hub_api")
    if not rows:
        print("FAIL: zero route handlers examined", file=sys.stderr)
        return 1
    Path(sys.argv[1]).write_text(json.dumps(rows, indent=1))
    print(f"routes={len(rows)} touching_dal={sum(1 for r in rows if r['uses_dal'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
