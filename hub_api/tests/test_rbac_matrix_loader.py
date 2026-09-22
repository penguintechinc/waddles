"""Unit tests for the RBAC matrix loader/generator -- no DB required."""

from __future__ import annotations

from services.rbac_matrix import (
    ALL_PRIVILEGES,
    DEFAULT_MATRIX_PATH,
    load_matrix,
    matrix_roles,
    matrix_tables,
    render_create_roles_sql,
    render_grant_sql,
    render_revoke_public_sql,
)


def test_matrix_has_at_least_8_roles_and_8_tables() -> None:
    roles = matrix_roles()
    tables = matrix_tables()
    assert len(roles) >= 8, f"expected >= 8 roles, found {len(roles)}: {sorted(roles)}"
    assert len(tables) >= 8, f"expected >= 8 tables, found {len(tables)}: {sorted(tables)}"


def test_app_versions_has_exactly_two_writers() -> None:
    rows = load_matrix()
    writers = {
        r.role
        for r in rows
        if r.table == "app_versions" and r.privileges and r.role != "migration_runner"
    }
    assert writers == {"waddles_publisher", "hub_api"}, writers


def test_every_role_has_an_explicit_row_for_every_table() -> None:
    rows = load_matrix()
    seen = {(r.role, r.table) for r in rows}
    roles = matrix_roles()
    tables = matrix_tables()
    missing = [(role, table) for role in roles for table in tables if (role, table) not in seen]
    # Every (role, table) pair this milestone's tables/roles define must be
    # explicit -- a missing pair would make the live-grants equality test
    # (Task 5) silently treat "never checked" as "no privileges", which is
    # not the same claim.
    assert not missing, f"matrix is missing explicit rows for: {missing}"


def test_render_grant_sql_only_emits_non_empty_privilege_rows() -> None:
    rows = load_matrix()
    statements = render_grant_sql(rows, tables=frozenset({"app_versions"}))
    assert any("waddles_publisher" in s for s in statements)
    assert any("hub_api" in s for s in statements)
    assert not any("svc_ingest" in s for s in statements)


def test_render_revoke_public_sql_covers_every_table() -> None:
    tables = sorted(matrix_tables())
    statements = render_revoke_public_sql(tables)
    assert len(statements) == len(tables)
    assert all("REVOKE ALL ON" in s and "FROM PUBLIC" in s for s in statements)


def test_render_create_roles_sql_is_idempotent_guarded() -> None:
    statements = render_create_roles_sql(["hub_api", "waddles_publisher"])
    assert len(statements) == 2
    assert all("IF NOT EXISTS" in s for s in statements)


def test_privileges_are_bounded_to_the_known_set() -> None:
    rows = load_matrix()
    for row in rows:
        assert row.privileges <= ALL_PRIVILEGES


def test_default_matrix_path_exists() -> None:
    assert DEFAULT_MATRIX_PATH.exists()
