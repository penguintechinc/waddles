"""Static guard: no raw `executesql` call may carry PostgreSQL-style `$N` placeholders.

pydal hands SQL to the driver verbatim; psycopg2 wants `%s`, so `$1` raises
`UndefinedParameter: there is no parameter $1` on real Postgres while SQLite tests accept it.
SQL written with `$N` must go through `services.sql_util.execute_positional` instead.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_ROOTS = [_REPO / "hub_api", _REPO / "libs" / "flask_core" / "flask_core"]
_SKIP_PARTS = {"tests", ".venv", "venv", "node_modules", "__pycache__", "build-tools"}
_DOLLAR = re.compile(r"\$\d")


def _source_files() -> list[Path]:
    """Every non-test service/library Python file under the scanned roots."""
    files = []
    for root in _ROOTS:
        for path in root.rglob("*.py"):
            if not _SKIP_PARTS.intersection(path.relative_to(root).parts):
                files.append(path)
    return sorted(files)


def _strings(node: ast.AST) -> list[str]:
    """All string constants under `node` (f-string literal parts included)."""
    return [
        n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def _assigned_strings(tree: ast.Module, name: str) -> list[str]:
    """Every string constant assigned or appended to `name` anywhere in the module."""
    out: list[str] = []
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AugAssign | ast.AnnAssign):
            targets = [node.target]
        value = getattr(node, "value", None)
        if value is not None and any(isinstance(t, ast.Name) and t.id == name for t in targets):
            out.extend(_strings(value))
    return out


def _executesql_calls(tree: ast.Module) -> list[ast.Call]:
    """Every `<x>.executesql(...)` call that has a SQL argument."""
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "executesql"
        and n.args
    ]


def _offenders(path: Path) -> list[str]:
    """`file:line` of each executesql call whose SQL contains a `$N` marker."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bad = []
    for call in _executesql_calls(tree):
        sql = call.args[0]
        parts = _strings(sql)
        if isinstance(sql, ast.Name):
            parts += _assigned_strings(tree, sql.id)
        if any(_DOLLAR.search(p) for p in parts):
            bad.append(f"{path}:{call.lineno}")
    return bad


def test_scan_covers_the_known_call_sites() -> None:
    """A scanner pointed at the wrong root reports clean; prove it examined real sites."""
    files = _source_files()
    sites = sum(len(_executesql_calls(ast.parse(p.read_text(encoding="utf-8")))) for p in files)
    assert len(files) > 100
    assert sites >= 30


def test_no_executesql_call_uses_dollar_placeholders() -> None:
    """The regression: any `$N` SQL reaching `executesql` directly fails here."""
    offenders = [o for p in _source_files() for o in _offenders(p)]
    assert not offenders, (
        "executesql with `$N` placeholders breaks on Postgres; use "
        f"services.sql_util.execute_positional: {offenders}"
    )


@pytest.mark.parametrize(
    "src",
    [
        'dal.executesql("SELECT $1", placeholders=[1])',
        'SQL = "x = $2"\ndal.executesql(SQL, placeholders=[1])',
    ],
)
def test_scanner_detects_the_regression(tmp_path: Path, src: str) -> None:
    """The guard itself must be able to fail."""
    f = tmp_path / "bad.py"
    f.write_text(src, encoding="utf-8")
    assert _offenders(f)
