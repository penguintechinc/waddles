"""Load `config/postgres/rbac-matrix.yaml` and render GRANT/REVOKE SQL from it.

The single generator behind spec D28 ("Grants are generated from this
file; nobody writes a GRANT by hand.") -- imported by an Alembic
migration (which cannot rely on hub-api's own `sys.path`, since
`alembic/` lives at the repo root, a sibling of `hub_api/`, not inside
it) and by hub-api's own test suite, both via `importlib` against this
file's absolute path so neither caller needs a package-install step.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ALL_PRIVILEGES = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"})

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MATRIX_PATH = REPO_ROOT / "config" / "postgres" / "rbac-matrix.yaml"


@dataclass(slots=True, frozen=True)
class GrantSpec:
    """One `(role, table, privileges)` row from the RBAC matrix file."""

    role: str
    table: str
    privileges: frozenset[str]


class MatrixError(ValueError):
    """Raised when the matrix file is malformed or references an unknown role/table."""


def load_matrix(path: str | Path = DEFAULT_MATRIX_PATH) -> list[GrantSpec]:
    """Parse the YAML matrix file into a list of `GrantSpec`, validated against its own role/table lists."""
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    roles = frozenset(raw.get("roles", []))
    tables = frozenset(raw.get("tables", []))
    if not roles:
        raise MatrixError(f"{path}: 'roles' list is empty")
    if not tables:
        raise MatrixError(f"{path}: 'tables' list is empty")

    specs: list[GrantSpec] = []
    for row in raw.get("grants", []):
        role = row["role"]
        table = row["table"]
        privileges = frozenset(row.get("privileges", []))
        if role not in roles:
            raise MatrixError(f"{path}: grant references unknown role {role!r}")
        if table not in tables:
            raise MatrixError(f"{path}: grant references unknown table {table!r}")
        if not privileges <= ALL_PRIVILEGES:
            raise MatrixError(f"{path}: grant for {role}/{table} has unknown privilege(s)")
        specs.append(GrantSpec(role=role, table=table, privileges=privileges))
    return specs


def matrix_roles(path: str | Path = DEFAULT_MATRIX_PATH) -> frozenset[str]:
    """The full `roles` list declared in the matrix file."""
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return frozenset(raw.get("roles", []))


def matrix_tables(path: str | Path = DEFAULT_MATRIX_PATH) -> frozenset[str]:
    """The full `tables` list declared in the matrix file."""
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return frozenset(raw.get("tables", []))


def render_create_roles_sql(roles: list[str]) -> list[str]:
    """Idempotent `CREATE ROLE ... NOLOGIN` for every role, guarded by a `pg_roles` existence check."""
    statements = []
    for role in roles:
        statements.append(
            f"DO $$ BEGIN\n"
            f"  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN\n"
            f"    CREATE ROLE {role} NOLOGIN;\n"
            f"  END IF;\n"
            f"END $$;"
        )
    return statements


def render_revoke_public_sql(tables: list[str]) -> list[str]:
    """`REVOKE ALL ON <table> FROM PUBLIC` for every table -- the default-deny baseline."""
    return [f"REVOKE ALL ON {table} FROM PUBLIC;" for table in tables]


def render_grant_sql(rows: list[GrantSpec], *, tables: frozenset[str] | None = None) -> list[str]:
    """`GRANT <privs> ON <table> TO <role>` for every non-empty-privilege row.

    `tables`, when given, restricts rendering to those tables only --
    used by a migration that owns a subset of the matrix's tables (e.g.
    Task 2's migration only wants `app_versions`/`app_active_versions`/
    `app_versions_audit_log` rows, not Task 3's five tables, even though
    both read the same, by-then-larger matrix file).
    """
    statements = []
    for spec in rows:
        if tables is not None and spec.table not in tables:
            continue
        if not spec.privileges:
            continue
        privileges = ", ".join(sorted(spec.privileges))
        statements.append(f"GRANT {privileges} ON {spec.table} TO {spec.role};")
    return statements
