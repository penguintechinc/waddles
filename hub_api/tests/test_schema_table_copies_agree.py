"""regression: shared-DAL tables defined in several binders must carry the same columns.

Whichever binder runs first wins on a shared DAL, so a minimal copy silently drops columns:
`GET /admin/<c>/commands` 500'd (`commands.platforms`) and `GET /admin/<c>/interaction/roles`
500'd (`community_roles.description`) on alpha.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SCHEMA = Path(__file__).resolve().parents[1] / "services" / "schema.py"


def _copies(table: str) -> list[set[str]]:
    """Field-name sets of every literal `define_table(table, ...)` in schema.py."""
    out: list[set[str]] = []
    for node in ast.walk(ast.parse(SCHEMA.read_text())):
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", "") == "define_table"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == table
        ):
            out.append(
                {
                    a.args[0].value
                    for a in node.args[1:]
                    if isinstance(a, ast.Call)
                    and getattr(a.func, "id", "") == "Field"
                    and a.args
                    and isinstance(a.args[0], ast.Constant)
                }
            )
    return out


@pytest.mark.parametrize("table", ["commands", "community_roles"])
def test_every_copy_has_the_full_column_set(table: str) -> None:
    copies = _copies(table)
    assert len(copies) >= 2, "expected multiple binders for this table"
    union = set().union(*copies)
    for copy in copies:
        assert copy == union, f"{table}: a binder copy is missing {sorted(union - copy)}"
