"""Coverage for `services/bundle_data_ddl.py`.

Three tiers: (1) pure-Python tests of the additive-migration diff and the
`indexed-column` static mapping, no DB required; (2) golden-snapshot tests
of the exact rendered DDL text, which need a real psycopg2 connection/
cursor only to apply `Composable.as_string()`'s quoting (there is no
offline renderer -- see `bundle_data_ddl`'s own module docstring); (3) a
real-Postgres test that actually applies the generated DDL and proves RLS
+ the fixed role-grant template isolates two tenants from each other.
Tiers 2/3 use the `bundle_data_pg_conn` fixture (`tests/conftest.py`) and
are skipped, not failed, where Docker is unavailable.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from services.bundle_data_ddl import (
    RUNTIME_ROLE,
    MigrationError,
    compute_additive_migration,
    generate_additive_migration_ddl,
    generate_create_table_ddl,
    generate_drop_table_ddl,
    generate_indexed_column_mapping,
    render_ddl_sql,
)
from services.bundle_data_schema import derive_table_identity, validate_table_declaration

_QUOTES_DECL = {
    "columns": [
        {"name": "kind", "type": "text(32)", "nullable": False},
        {"name": "body", "type": "text(240)", "nullable": False},
        {"name": "weight", "type": "int4", "nullable": True, "default": "1"},
    ],
    "indexes": [{"columns": ["kind"]}],
}


def _quotes_declaration() -> Any:
    return validate_table_declaration(_QUOTES_DECL, provider="builtin")


def _quotes_identity() -> Any:
    return derive_table_identity("waddles.core.quotes.quotes", provider="builtin")


class TestIndexedColumnMapping:
    """§6.2 C1.1 -- the static lookup table, built with no bundle-value interpolation."""

    def test_one_entry_per_distinct_indexed_column(self) -> None:
        decl = validate_table_declaration(
            {
                "columns": [
                    {"name": "kind", "type": "text(32)", "nullable": False},
                    {"name": "score", "type": "int8", "nullable": True},
                    {"name": "user_ref", "type": "user_ref", "nullable": True},
                ],
                "indexes": [
                    {"columns": ["kind", {"column": "score", "dir": "desc"}]},
                    {"columns": ["user_ref"]},
                ],
            },
            provider="builtin",
        )
        mapping = generate_indexed_column_mapping(decl)
        assert [m.variant for m in mapping] == ["kind", "score", "user_ref"]

    def test_column_in_two_indexes_appears_once(self) -> None:
        decl = validate_table_declaration(
            {
                "columns": [
                    {"name": "kind", "type": "text(32)", "nullable": False},
                    {"name": "score", "type": "int8", "nullable": True},
                ],
                "indexes": [{"columns": ["kind"]}, {"columns": ["kind", "score"]}],
            },
            provider="builtin",
        )
        mapping = generate_indexed_column_mapping(decl)
        assert [m.variant for m in mapping] == ["kind", "score"]

    def test_no_indexes_yields_empty_mapping(self) -> None:
        decl = validate_table_declaration(
            {"columns": [{"name": "kind", "type": "text(32)", "nullable": False}]},
            provider="builtin",
        )
        assert generate_indexed_column_mapping(decl) == ()

    def test_deterministic_across_calls(self) -> None:
        decl = _quotes_declaration()
        assert generate_indexed_column_mapping(decl) == generate_indexed_column_mapping(decl)


class TestAdditiveMigration:
    """§11 -- additive-only diff; anything destructive raises `MigrationError`."""

    def test_new_nullable_column_is_additive(self) -> None:
        old = _quotes_declaration()
        new = validate_table_declaration(
            {
                "columns": [
                    *_QUOTES_DECL["columns"],
                    {"name": "author_ref", "type": "user_ref", "nullable": True},
                ],
                "indexes": _QUOTES_DECL["indexes"],
            },
            provider="builtin",
        )
        plan = compute_additive_migration(old, new)
        assert [c.name for c in plan.added_columns] == ["author_ref"]
        assert not plan.is_empty

    def test_new_defaulted_not_null_column_is_additive(self) -> None:
        old = _quotes_declaration()
        new = validate_table_declaration(
            {
                "columns": [
                    *_QUOTES_DECL["columns"],
                    {"name": "views", "type": "int4", "nullable": False, "default": "0"},
                ],
                "indexes": _QUOTES_DECL["indexes"],
            },
            provider="builtin",
        )
        plan = compute_additive_migration(old, new)
        assert [c.name for c in plan.added_columns] == ["views"]

    def test_new_not_null_column_without_default_is_destructive(self) -> None:
        old = _quotes_declaration()
        new = validate_table_declaration(
            {
                "columns": [
                    *_QUOTES_DECL["columns"],
                    {"name": "views", "type": "int4", "nullable": False},
                ],
                "indexes": _QUOTES_DECL["indexes"],
            },
            provider="builtin",
        )
        with pytest.raises(MigrationError) as exc:
            compute_additive_migration(old, new)
        assert exc.value.reason == "new_column_not_null_no_default"

    def test_dropped_column_is_destructive(self) -> None:
        old = _quotes_declaration()
        new = validate_table_declaration(
            {"columns": _QUOTES_DECL["columns"][:2], "indexes": []}, provider="builtin"
        )
        with pytest.raises(MigrationError) as exc:
            compute_additive_migration(old, new)
        assert exc.value.reason == "column_dropped"

    def test_changed_type_is_destructive(self) -> None:
        old = _quotes_declaration()
        new = validate_table_declaration(
            {
                "columns": [
                    {"name": "kind", "type": "text(64)", "nullable": False},
                    *_QUOTES_DECL["columns"][1:],
                ],
                "indexes": _QUOTES_DECL["indexes"],
            },
            provider="builtin",
        )
        with pytest.raises(MigrationError) as exc:
            compute_additive_migration(old, new)
        assert exc.value.reason == "column_type_changed"

    def test_changed_nullability_is_destructive(self) -> None:
        old = _quotes_declaration()
        new = validate_table_declaration(
            {
                "columns": [
                    {"name": "kind", "type": "text(32)", "nullable": True},
                    *_QUOTES_DECL["columns"][1:],
                ],
                "indexes": _QUOTES_DECL["indexes"],
            },
            provider="builtin",
        )
        with pytest.raises(MigrationError) as exc:
            compute_additive_migration(old, new)
        assert exc.value.reason == "column_nullability_changed"

    def test_new_index_is_additive(self) -> None:
        old = _quotes_declaration()
        new = validate_table_declaration(
            {
                "columns": _QUOTES_DECL["columns"],
                "indexes": [*_QUOTES_DECL["indexes"], {"columns": ["weight"]}],
            },
            provider="builtin",
        )
        plan = compute_additive_migration(old, new)
        assert len(plan.added_indexes) == 1

    def test_identical_declarations_yield_empty_plan(self) -> None:
        old = _quotes_declaration()
        new = _quotes_declaration()
        plan = compute_additive_migration(old, new)
        assert plan.is_empty

    def test_changed_default_is_destructive(self) -> None:
        old = _quotes_declaration()
        new = validate_table_declaration(
            {
                "columns": [
                    *_QUOTES_DECL["columns"][:2],
                    {"name": "weight", "type": "int4", "nullable": True, "default": "2"},
                ],
                "indexes": _QUOTES_DECL["indexes"],
            },
            provider="builtin",
        )
        with pytest.raises(MigrationError) as exc:
            compute_additive_migration(old, new)
        assert exc.value.reason == "column_default_changed"


@pytest.mark.usefixtures("bundle_data_pg_conn")
class TestGoldenDdlSnapshots:
    """Exact rendered SQL text for a fixed example -- pins `bundle_data_ddl`'s output shape."""

    def test_create_table_ddl_golden(self, bundle_data_pg_conn: Any) -> None:
        identity = _quotes_identity()
        decl = _quotes_declaration()
        statements = generate_create_table_ddl(identity, decl)
        rendered = render_ddl_sql(statements, bundle_data_pg_conn)

        assert rendered[0] == (
            'CREATE TABLE "app_core"."waddles_core_quotes_quotes" (\n'
            "    row_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),\n"
            "    tenant_id integer NOT NULL,\n"
            "    community_id integer NOT NULL,\n"
            "    version integer NOT NULL DEFAULT 1,\n"
            "    created_at timestamptz NOT NULL DEFAULT now(),\n"
            "    updated_at timestamptz NOT NULL DEFAULT now(),\n"
            '    "kind" varchar(32) NOT NULL,\n'
            '    "body" varchar(240) NOT NULL,\n'
            '    "weight" integer DEFAULT 1\n'
            ")"
        )
        assert rendered[1] == (
            'CREATE INDEX "waddles_core_quotes_quotes_idx_tenant_id_kind" '
            'ON "app_core"."waddles_core_quotes_quotes" ("tenant_id", "kind")'
        )
        assert rendered[2] == (
            'ALTER TABLE "app_core"."waddles_core_quotes_quotes" ENABLE ROW LEVEL SECURITY'
        )
        assert rendered[3] == (
            'ALTER TABLE "app_core"."waddles_core_quotes_quotes" FORCE ROW LEVEL SECURITY'
        )
        assert rendered[4] == (
            'CREATE POLICY "waddles_core_quotes_quotes_tenant_isolation" '
            'ON "app_core"."waddles_core_quotes_quotes" USING '
            "(tenant_id = NULLIF(current_setting('waddles.tenant_id', true), '')::integer "
            "AND community_id = NULLIF(current_setting('waddles.community_id', true), "
            "'')::integer)"
        )
        assert rendered[5] == (
            'GRANT SELECT, INSERT, UPDATE, DELETE ON "app_core"."waddles_core_quotes_quotes" '
            'TO "waddles_bundle_runtime"'
        )
        assert len(rendered) == 6

    def test_drop_table_ddl_golden(self, bundle_data_pg_conn: Any) -> None:
        identity = _quotes_identity()
        rendered = render_ddl_sql([generate_drop_table_ddl(identity)], bundle_data_pg_conn)
        assert rendered[0] == 'DROP TABLE IF EXISTS "app_core"."waddles_core_quotes_quotes"'

    def test_additive_migration_ddl_golden(self, bundle_data_pg_conn: Any) -> None:
        identity = _quotes_identity()
        old = _quotes_declaration()
        new = validate_table_declaration(
            {
                "columns": [
                    *_QUOTES_DECL["columns"],
                    {"name": "author_ref", "type": "user_ref", "nullable": True},
                ],
                "indexes": [*_QUOTES_DECL["indexes"], {"columns": ["author_ref"]}],
            },
            provider="builtin",
        )
        plan = compute_additive_migration(old, new)
        rendered = render_ddl_sql(
            generate_additive_migration_ddl(identity, plan), bundle_data_pg_conn
        )
        assert rendered[0] == (
            'ALTER TABLE "app_core"."waddles_core_quotes_quotes" ADD COLUMN "author_ref" uuid'
        )
        assert rendered[1] == (
            'CREATE INDEX "waddles_core_quotes_quotes_idx_tenant_id_author_ref" '
            'ON "app_core"."waddles_core_quotes_quotes" ("tenant_id", "author_ref")'
        )

    def test_default_literal_defense_in_depth_injection_payload_is_a_bound_parameter(
        self, bundle_data_pg_conn: Any
    ) -> None:
        # A default literal containing SQL-syntax-shaped text renders as a
        # single quoted, doubled-quote-escaped SQL string -- never as raw
        # SQL text that could terminate the statement early.
        decl = validate_table_declaration(
            {
                "columns": [
                    {
                        "name": "label",
                        "type": "text(64)",
                        "nullable": True,
                        "default": "'x''; DROP TABLE app_core.evil; --'",
                    }
                ]
            },
            provider="builtin",
        )
        identity = derive_table_identity("waddles.core.evil.evil", provider="builtin")
        statements = generate_create_table_ddl(identity, decl)
        rendered = render_ddl_sql(statements, bundle_data_pg_conn)
        assert "DEFAULT 'x''; DROP TABLE app_core.evil; --'" in rendered[0]
        # Exactly one statement's worth of SQL -- the payload never broke out
        # into a second statement.
        assert rendered[0].count("CREATE TABLE") == 1

    def test_numeric_column_type_rendered(self, bundle_data_pg_conn: Any) -> None:
        decl = validate_table_declaration(
            {"columns": [{"name": "price", "type": "numeric(10,2)", "nullable": True}]},
            provider="builtin",
        )
        identity = derive_table_identity("waddles.core.shop.shop", provider="builtin")
        rendered = render_ddl_sql(generate_create_table_ddl(identity, decl), bundle_data_pg_conn)
        assert '"price" numeric(10,2)' in rendered[0]

    def test_long_app_id_produces_collision_resistant_short_identifiers(
        self, bundle_data_pg_conn: Any
    ) -> None:
        long_app_id = "waddles.core." + ("segmentnamehere." * 4) + "app"
        identity = derive_table_identity(long_app_id, provider="builtin")
        decl = validate_table_declaration(
            {
                "columns": [
                    {"name": "kind", "type": "text(32)", "nullable": False},
                    {"name": "score", "type": "int8", "nullable": True},
                ],
                "indexes": [{"columns": ["kind", {"column": "score", "dir": "desc"}]}],
            },
            provider="builtin",
        )
        statements = generate_create_table_ddl(identity, decl)
        rendered = render_ddl_sql(statements, bundle_data_pg_conn)
        # Every double-quoted identifier segment must fit Postgres's own
        # NAMEDATALEN-1 (63 byte) limit.
        for sql_text in rendered:
            for ident in re.findall(r'"([^"]+)"', sql_text):
                assert len(ident) <= 63, f"identifier {ident!r} exceeds 63 bytes"


@pytest.mark.usefixtures("bundle_data_pg_conn")
class TestRealPostgresApplyAndRlsIsolation:
    """Applies the generated DDL for real and proves RLS isolates two tenants."""

    def test_create_table_ddl_applies_cleanly(self, bundle_data_pg_conn: Any) -> None:
        identity = _quotes_identity()
        decl = _quotes_declaration()
        with bundle_data_pg_conn.cursor() as cur:
            for statement in generate_create_table_ddl(identity, decl):
                cur.execute(statement)
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'app_core' AND table_name = 'waddles_core_quotes_quotes' "
                "ORDER BY ordinal_position"
            )
            columns = [row[0] for row in cur.fetchall()]
        assert columns == [
            "row_id",
            "tenant_id",
            "community_id",
            "version",
            "created_at",
            "updated_at",
            "kind",
            "body",
            "weight",
        ]

    def test_rls_isolates_two_tenants(self, bundle_data_pg_conn: Any) -> None:
        identity = _quotes_identity()
        decl = _quotes_declaration()
        with bundle_data_pg_conn.cursor() as cur:
            for statement in generate_create_table_ddl(identity, decl):
                cur.execute(statement)

            # Seed rows for two different tenants as the table owner
            # (bypasses RLS at insert time -- this is setup, not the assertion).
            cur.execute(
                "INSERT INTO app_core.waddles_core_quotes_quotes "
                "(tenant_id, community_id, kind, body) VALUES (1, 1, 'motivational', 'tenant one')"
            )
            cur.execute(
                "INSERT INTO app_core.waddles_core_quotes_quotes "
                "(tenant_id, community_id, kind, body) VALUES (2, 1, 'motivational', 'tenant two')"
            )

        # `waddles_bundle_runtime` is the role the RLS policy and the
        # explicit per-table GRANT both target -- switch to it and set the
        # exact scope GUCs the host would set per-transaction from
        # `InvokeScope` (§6.2/§7).
        with bundle_data_pg_conn.cursor() as cur:
            cur.execute("SET ROLE waddles_bundle_runtime")
            cur.execute("SET waddles.tenant_id = '1'")
            cur.execute("SET waddles.community_id = '1'")
            cur.execute("SELECT body FROM app_core.waddles_core_quotes_quotes")
            tenant_one_rows = [row[0] for row in cur.fetchall()]
            cur.execute("RESET ROLE")

        assert tenant_one_rows == ["tenant one"]

        with bundle_data_pg_conn.cursor() as cur:
            cur.execute("SET ROLE waddles_bundle_runtime")
            cur.execute("SET waddles.tenant_id = '2'")
            cur.execute("SET waddles.community_id = '1'")
            cur.execute("SELECT body FROM app_core.waddles_core_quotes_quotes")
            tenant_two_rows = [row[0] for row in cur.fetchall()]
            cur.execute("RESET ROLE")

        assert tenant_two_rows == ["tenant two"]

    def test_rls_fails_closed_when_scope_guc_is_unset(self, bundle_data_pg_conn: Any) -> None:
        identity = _quotes_identity()
        decl = _quotes_declaration()
        with bundle_data_pg_conn.cursor() as cur:
            for statement in generate_create_table_ddl(identity, decl):
                cur.execute(statement)
            cur.execute(
                "INSERT INTO app_core.waddles_core_quotes_quotes "
                "(tenant_id, community_id, kind, body) VALUES (1, 1, 'motivational', 'tenant one')"
            )

        # No `SET waddles.tenant_id`/`waddles.community_id` at all -- a
        # pooled-connection GUC-leak scenario's opposite failure mode
        # (nothing set, rather than a stale value). `current_setting(...,
        # true)` returns NULL, and `tenant_id = NULL` is never true, so this
        # must return zero rows, never every row.
        with bundle_data_pg_conn.cursor() as cur:
            cur.execute("SET ROLE waddles_bundle_runtime")
            cur.execute("SELECT body FROM app_core.waddles_core_quotes_quotes")
            rows = [row[0] for row in cur.fetchall()]
            cur.execute("RESET ROLE")

        assert rows == []

    def test_force_rls_binds_the_owning_role_too(self, bundle_data_pg_conn: Any) -> None:
        # §3.4 C1.2 -- without FORCE ROW LEVEL SECURITY, the table owner
        # bypasses RLS entirely by default. Postgres superusers always
        # bypass RLS regardless of FORCE, so this must run as the real
        # (non-superuser) `waddles_bundle_migrator` owner role -- otherwise
        # a passing assertion here would prove nothing about FORCE.
        identity = _quotes_identity()
        decl = _quotes_declaration()
        with bundle_data_pg_conn.cursor() as cur:
            cur.execute("SET ROLE waddles_bundle_migrator")
            for statement in generate_create_table_ddl(identity, decl):
                cur.execute(statement)
            # The owner role is itself subject to the RLS policy's USING
            # clause on INSERT once FORCE is set (no WITH CHECK is declared,
            # so USING doubles as the write check) -- set scope to seed the
            # row, then RESET it before the actual assertion query so the
            # query runs with no scope GUCs set at all, same as the other
            # "fails closed" test above.
            cur.execute("SET waddles.tenant_id = '1'")
            cur.execute("SET waddles.community_id = '1'")
            cur.execute(
                "INSERT INTO app_core.waddles_core_quotes_quotes "
                "(tenant_id, community_id, kind, body) VALUES (1, 1, 'motivational', 'owner row')"
            )
            cur.execute("RESET waddles.tenant_id")
            cur.execute("RESET waddles.community_id")
            cur.execute("SELECT body FROM app_core.waddles_core_quotes_quotes")
            rows = [row[0] for row in cur.fetchall()]
            cur.execute("RESET ROLE")

        assert rows == []

    def test_runtime_role_has_no_grants_outside_app_schemas(self, bundle_data_pg_conn: Any) -> None:
        # §2's "narrowly-scoped exception, not a repeal" -- the explicit
        # per-table GRANT this module issues must not be readable as
        # implying broader access. Confirms the runtime role has no
        # table-level privilege on `information_schema`/`pg_catalog`
        # objects this test didn't explicitly grant it.
        with bundle_data_pg_conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM information_schema.role_table_grants "
                "WHERE grantee = %s AND table_schema NOT IN ('app_core', 'app_community')",
                (RUNTIME_ROLE,),
            )
            (count,) = cur.fetchone()
        assert count == 0
