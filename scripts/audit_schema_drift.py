#!/usr/bin/env python3
"""Audit pydal table definitions in hub_api against a fresh-replayed Postgres.

Statically parses every `define_table(...)` call under `hub_api/` (non-test code), unions the
`Field(...)` names per table, replays the whole migration chain into a throwaway Postgres 17
(`empty_postgres` + `alembic upgrade head`, i.e. what a brand-new deploy gets) and prints every
(table, column) that code defines but the database lacks, plus whole missing tables.

Usage: python3 scripts/audit_schema_drift.py [--dsn DSN]   (no --dsn => starts a docker container)
Exit 1 when any drift is found, 2 when nothing was examined (a zero denominator is a failure).
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "hub_api"
# Fields pydal adds itself / that are not real columns.
_VIRTUAL = {"id"}


def _const(node: ast.AST) -> str | None:
    """Return the string value of a constant node, else None."""
    return (
        node.value
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        else None
    )


def collect_defined() -> dict[str, dict[str, set[str]]]:
    """Map table -> column -> {defining files}, from every literal define_table call."""
    out: dict[str, dict[str, set[str]]] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        if "/tests/" in rel or path.name.startswith("test_") or "/build-tools/" in rel:
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # `dal.define_table(name, ...)` or the `_define_if_absent(dal, name, ...)` wrapper.
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "define_table"
            ):
                head = 0
            elif (
                isinstance(node.func, ast.Name) and node.func.id == "_define_if_absent"
            ):
                head = 1
            else:
                continue
            if len(node.args) <= head:
                continue
            table = _const(node.args[head])
            if table is None:
                continue
            cols = out.setdefault(table, {})
            for arg in node.args[head + 1 :]:
                if (
                    isinstance(arg, ast.Call)
                    and isinstance(arg.func, ast.Attribute)
                    and arg.func.attr == "Field"
                    and arg.args
                ):
                    name = _const(arg.args[0])
                    if name and name not in _VIRTUAL:
                        cols.setdefault(name, set()).add(rel)
    return out


def live_columns(dsn: str) -> dict[str, set[str]]:
    """Map table -> column names for every table in the public schema."""
    import psycopg2

    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public'"
        )
        live: dict[str, set[str]] = {}
        for table, column in cur.fetchall():
            live.setdefault(table, set()).add(column)
    return live


def diff(
    defined: dict[str, dict[str, set[str]]], live: dict[str, set[str]]
) -> tuple[list[str], list[tuple[str, str, list[str]]]]:
    """Return (missing_tables, [(table, column, defining_files)]) for code-vs-DB drift."""
    missing_tables = sorted(t for t in defined if t not in live)
    missing_cols = [
        (t, c, sorted(files))
        for t, cols in sorted(defined.items())
        if t in live
        for c, files in sorted(cols.items())
        if c not in live[t]
    ]
    return missing_tables, missing_cols


def main() -> int:
    """CLI entry point."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn")
    args = ap.parse_args()
    defined = collect_defined()
    if args.dsn:
        live = live_columns(args.dsn)
    else:
        sys.path.insert(0, str(REPO / "alembic" / "tests"))
        from pg_docker import alembic_cli, empty_postgres

        with empty_postgres("schema-drift-audit") as db:
            alembic_cli("upgrade", "head", dsn=db.dsn)
            live = live_columns(db.dsn)
    n_cols = sum(len(c) for c in defined.values())
    print(
        f"examined: {len(defined)} tables / {n_cols} columns defined; {len(live)} live tables"
    )
    if not defined or not live:
        return 2
    missing_tables, missing_cols = diff(defined, live)
    for t in missing_tables:
        print(f"MISSING TABLE  {t}  ({len(defined[t])} cols)")
    for t, c, files in missing_cols:
        print(f"MISSING COLUMN {t}.{c}  [{', '.join(files)}]")
    print(f"drift: {len(missing_tables)} tables, {len(missing_cols)} columns")
    return 1 if (missing_tables or missing_cols) else 0


if __name__ == "__main__":
    sys.exit(main())
