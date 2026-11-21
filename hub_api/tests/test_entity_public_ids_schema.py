"""Every code-side definition of an entity table declares the `public_id` the migration adds.

The `*_entity_public_ids` alembic migration owns the column; the pydal binders (`schema.py` and
the service-local `bot_tables.py` / `community_common.py`, several of which define MINIMAL copies of
the same table) and the SQLAlchemy models must declare it too, or a route that selects/inserts
through a copy without the field silently loses the id. Checks every copy individually (not the
union), the SQLAlchemy models, and that no excluded table slipped into the migration.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HUB = REPO / "hub_api"
_MIGRATIONS = sorted((REPO / "alembic" / "versions").glob("*_entity_public_ids.py"))
#: Entities whose routes are deleted / that are not entity ids -- must NOT be in the migration.
_EXCLUDED = {"community_polls", "community_forms", "poll_options", "form_fields", "tier_code"}


def _entity_tables() -> tuple[str, ...]:
    assert len(_MIGRATIONS) == 1
    spec = importlib.util.spec_from_file_location("entity_public_ids_mig", _MIGRATIONS[0])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return tuple(module.ENTITY_TABLES)


TABLES = _entity_tables()


def _field_name(node: ast.AST) -> str | None:
    """`Field("x", ...)` / `dal.Field("x", ...)` -> "x"."""
    if not isinstance(node, ast.Call) or not node.args:
        return None
    func = node.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    first = node.args[0]
    if name == "Field" and isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def _copies() -> dict[str, list[tuple[str, int, set[str]]]]:
    """Table -> [(file, line, declared field names)] for every literal define in hub_api."""
    out: dict[str, list[tuple[str, int, set[str]]]] = {}
    for path in sorted(HUB.rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        if "/tests/" in rel or "/build-tools/" in rel or path.name.startswith("test_"):
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_define = (isinstance(func, ast.Attribute) and func.attr == "define_table") or (
                isinstance(func, ast.Name) and func.id == "_define_if_absent"
            )
            if not is_define:
                continue
            args = node.args[1:] if isinstance(func, ast.Name) else node.args
            if not args or not (
                isinstance(args[0], ast.Constant) and isinstance(args[0].value, str)
            ):
                continue
            fields = {n for a in args[1:] if (n := _field_name(a)) is not None}
            out.setdefault(args[0].value, []).append((rel, node.lineno, fields))
    return out


COPIES = _copies()


def test_migration_table_list_is_sane() -> None:
    assert len(TABLES) == len(set(TABLES)) == 26
    assert not (_EXCLUDED & set(TABLES))
    assert "platform_connections" not in TABLES and "tenants" not in TABLES  # already have it


@pytest.mark.parametrize("table", TABLES)
def test_every_pydal_copy_declares_public_id(table: str) -> None:
    copies = COPIES.get(table)
    assert copies, f"{table}: no pydal define_table found (denominator 0 is a failure)"
    missing = [f"{f}:{ln}" for f, ln, fields in copies if "public_id" not in fields]
    assert not missing, f"{table}: copies without public_id: {missing}"


def test_all_copies_were_examined() -> None:
    examined = sum(len(COPIES[t]) for t in TABLES)
    assert examined >= 30  # 26 tables + the community_roles x3 / support_tickets x1 extra copies


def test_sqlalchemy_models_declare_public_id() -> None:
    models = (REPO / "libs/flask_core/flask_core/models/guild_pairing.py").read_text()
    tree = ast.parse(models)
    wanted = {"guild_tenant_pairings": False, "community_role_sync_bindings": False}
    for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
        tablename = next(
            (
                s.value.value
                for s in cls.body
                if isinstance(s, ast.Assign)
                and any(getattr(t, "id", "") == "__tablename__" for t in s.targets)
                and isinstance(s.value, ast.Constant)
            ),
            None,
        )
        if tablename in wanted:
            wanted[tablename] = any(
                isinstance(s, ast.Assign)
                and any(getattr(t, "id", "") == "public_id" for t in s.targets)
                for s in cls.body
            )
    assert wanted == {"guild_tenant_pairings": True, "community_role_sync_bindings": True}
