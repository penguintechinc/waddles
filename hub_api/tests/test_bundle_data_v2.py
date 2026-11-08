"""Coverage for `services/bundle_data_v2.py` (db v2: several tables per bundle).

Tier 1: pure validation / identity / registry-row tests. Tier 2: golden DDL
text. Tier 3: a real Postgres applies the generated DDL via `provision_schema`
and proves multi-table provisioning, unique + prefix indexes, per-community RLS
across all tables, additive migration, and teardown. Tiers 2/3 use the
`bundle_data_pg_conn` fixture and are skipped (not failed) without Docker.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from services.bundle_data_ddl import MigrationError, render_ddl_sql
from services.bundle_data_schema import TableDeclarationError
from services.bundle_data_v2 import (
    MAX_TABLES,
    derive_v2_table_identity,
    generate_v2_table_ddl,
    migrate_schema,
    provision_schema,
    registry_row,
    teardown_schema,
    validate_schema_v2,
)

_RAW: dict[str, Any] = {
    "tables": [
        {
            "name": "events",
            "columns": [
                {"name": "title", "type": "text(200)", "nullable": False},
                {"name": "start_at", "type": "timestamptz", "nullable": False},
                {"name": "kind", "type": "int4", "nullable": True},
            ],
            "indexes": [
                {"columns": ["start_at"]},
                {"columns": [{"column": "title", "prefix": True}]},
            ],
        },
        {
            "name": "subs",
            "columns": [{"name": "slug", "type": "text(64)", "nullable": False}],
            "indexes": [{"columns": ["slug"], "unique": True}],
        },
    ]
}
_APP = "waddles.cal.test"


def _schema() -> Any:
    return validate_schema_v2(_RAW, provider="thirdparty", declared_names=["events", "subs"])


class TestValidation:
    """Declaration rules: v1 column rules inherited, v2 table/index rules added."""

    def test_valid_schema_parses_two_tables(self) -> None:
        s = _schema()
        assert [t.name for t in s.tables] == ["events", "subs"]
        assert s.table("subs").indexes[0].unique is True
        assert s.table("events").indexes[1].columns[0].prefix_ops is True
        assert s.table("nope") is None

    @pytest.mark.parametrize("bad", ["Events", "a__b", "", "1x", "x-y", "a" * 33])
    def test_bad_logical_names_rejected(self, bad: str) -> None:
        raw = {"tables": [{"name": bad, "columns": _RAW["tables"][1]["columns"]}]}
        with pytest.raises(TableDeclarationError) as e:
            validate_schema_v2(raw, provider="thirdparty")
        assert e.value.reason == "invalid_table_name"

    def test_duplicate_and_too_many_and_empty(self) -> None:
        t = _RAW["tables"][1]
        with pytest.raises(TableDeclarationError) as e:
            validate_schema_v2({"tables": [t, t]}, provider="thirdparty")
        assert e.value.reason == "duplicate_table"
        many = [{**t, "name": f"t{i}"} for i in range(MAX_TABLES + 1)]
        with pytest.raises(TableDeclarationError) as e:
            validate_schema_v2({"tables": many}, provider="thirdparty")
        assert e.value.reason == "too_many_tables"
        with pytest.raises(TableDeclarationError) as e:
            validate_schema_v2({"tables": []}, provider="thirdparty")
        assert e.value.reason == "no_tables"

    def test_v1_column_rules_still_apply(self) -> None:
        bad = {"tables": [{"name": "t", "columns": [{"name": "email", "type": "text(10)"}]}]}
        with pytest.raises(TableDeclarationError) as e:
            validate_schema_v2(bad, provider="thirdparty")
        assert e.value.reason == "column_name_suggests_pii"
        reserved = {"tables": [{"name": "t", "columns": [{"name": "tenant_id", "type": "int4"}]}]}
        with pytest.raises(TableDeclarationError) as e:
            validate_schema_v2(reserved, provider="thirdparty")
        assert e.value.reason == "reserved_column_name"

    def test_index_rules(self) -> None:
        cols = [{"name": "slug", "type": "text(8)"}, {"name": "n", "type": "int4"}]

        def raw(idx: Any) -> dict[str, Any]:
            return {"tables": [{"name": "t", "columns": cols, "indexes": [idx]}]}

        for idx, reason in [
            ({"columns": ["nope"]}, "invalid_index"),
            ({"columns": []}, "invalid_index"),
            ({"columns": ["slug", "n", "slug", "n", "slug"]}, "invalid_index"),
            ({"columns": [{"column": "n", "prefix": True}]}, "prefix_index_not_text"),
        ]:
            with pytest.raises(TableDeclarationError) as e:
                validate_schema_v2(raw(idx), provider="thirdparty")
            assert e.value.reason == reason, idx

    def test_data_tables_must_match_schema(self) -> None:
        with pytest.raises(TableDeclarationError) as e:
            validate_schema_v2(_RAW, provider="thirdparty", declared_names=["events"])
        assert e.value.reason == "table_names_mismatch"


class TestIdentityAndRegistry:
    """Physical names are derived server-side; the registry row is what the data plane reads."""

    def test_identity_is_app_double_underscore_logical(self) -> None:
        ident = derive_v2_table_identity(_APP, "events", provider="thirdparty")
        assert (ident.schema, ident.table) == ("app_community", "waddles_cal_test__events")
        core = derive_v2_table_identity(_APP, "events", provider="builtin")
        assert core.schema == "app_core"

    def test_long_names_are_truncated_with_a_distinguishing_hash(self) -> None:
        app = "waddles." + "x" * 80
        a = derive_v2_table_identity(app, "events", provider="thirdparty")
        b = derive_v2_table_identity(app, "events2", provider="thirdparty")
        assert len(a.table) <= 63 and len(b.table) <= 63 and a.table != b.table

    def test_guest_cannot_smuggle_a_separator(self) -> None:
        with pytest.raises(TableDeclarationError):
            derive_v2_table_identity(_APP, "a__b", provider="thirdparty")

    def test_registry_row_shape(self) -> None:
        row = registry_row(_APP, "thirdparty", _schema().table("events"))
        assert row["app_id"] == _APP and row["schema_kind"] == "community"
        assert row["physical_table"] == "waddles_cal_test__events"
        cols = json.loads(row["columns"])
        assert {"name": "kind", "nullable": True, "type": "int4"} in cols
        idx = json.loads(row["indexes"])
        assert idx[1]["columns"][0]["prefix_ops"] is True and idx[0]["unique"] is False


@pytest.fixture
def cur(bundle_data_pg_conn: Any) -> Any:
    """A cursor on the fresh database with the `bundle_data` platform tables present."""
    with bundle_data_pg_conn.cursor() as c:
        c.execute("CREATE SCHEMA bundle_data")
        c.execute(
            "CREATE TABLE bundle_data.table_registry (app_id text NOT NULL,"
            " logical_name text NOT NULL, schema_kind text NOT NULL,"
            " physical_table text NOT NULL, columns jsonb NOT NULL,"
            " indexes jsonb NOT NULL DEFAULT '[]'::jsonb,"
            " created_at timestamptz NOT NULL DEFAULT now(),"
            " updated_at timestamptz NOT NULL DEFAULT now(),"
            " PRIMARY KEY (app_id, logical_name))"
        )
        c.execute(
            "CREATE TABLE bundle_data.usage (app_id text NOT NULL, tenant_id integer NOT NULL,"
            " community_id integer NOT NULL, row_count bigint NOT NULL DEFAULT 0,"
            " byte_count bigint NOT NULL DEFAULT 0, PRIMARY KEY (app_id, tenant_id, community_id))"
        )
        yield c


class TestGoldenDdl:
    """Exact DDL text -- the Rust integration test restates this shape."""

    def test_indexes_are_scope_prefixed_and_pattern_ops_applied(self, cur: Any) -> None:
        ident = derive_v2_table_identity(_APP, "events", provider="thirdparty")
        stmts = render_ddl_sql(generate_v2_table_ddl(ident, _schema().table("events")), cur)
        assert stmts[0].startswith('CREATE TABLE "app_community"."waddles_cal_test__events"')
        joined = "\n".join(stmts)
        assert (
            'ON "app_community"."waddles_cal_test__events" '
            '("tenant_id", "community_id", "start_at")' in joined
        )
        assert '("tenant_id", "community_id", "title" text_pattern_ops)' in joined
        assert "FORCE ROW LEVEL SECURITY" in joined and "CREATE POLICY" in joined
        uniq = render_ddl_sql(
            generate_v2_table_ddl(
                derive_v2_table_identity(_APP, "subs", provider="thirdparty"),
                _schema().table("subs"),
            ),
            cur,
        )
        assert any(s.startswith("CREATE UNIQUE INDEX") for s in uniq)


class TestRealPostgres:
    """Applies the generated DDL for real and checks behaviour, not text."""

    def test_provision_creates_every_table_registry_rows_and_scoped_rls(self, cur: Any) -> None:
        provision_schema(cur, _APP, "thirdparty", _schema())
        cur.execute(
            "SELECT logical_name, physical_table FROM bundle_data.table_registry ORDER BY 1"
        )
        assert cur.fetchall() == [
            ("events", "waddles_cal_test__events"),
            ("subs", "waddles_cal_test__subs"),
        ]
        # unique index is per (tenant, community): same slug in another community is fine
        ins = (
            "INSERT INTO app_community.waddles_cal_test__subs (tenant_id, community_id, slug)"
            " VALUES (%s, %s, %s)"
        )
        cur.execute(ins, (1, 7, "daily"))
        cur.execute(ins, (1, 8, "daily"))
        with pytest.raises(Exception, match="duplicate key"):
            cur.execute(ins, (1, 7, "daily"))

    def test_rls_isolates_communities_on_every_table(
        self, bundle_data_pg_conn: Any, cur: Any
    ) -> None:
        provision_schema(cur, _APP, "thirdparty", _schema())
        cur.execute(
            "INSERT INTO app_community.waddles_cal_test__events"
            " (tenant_id, community_id, title, start_at)"
            " VALUES (1, 7, 'a', now()), (1, 8, 'b', now())"
        )
        cur.execute("CREATE ROLE v2_probe NOLOGIN IN ROLE waddles_bundle_runtime")
        cur.execute("SET ROLE v2_probe")
        cur.execute("SELECT set_config('waddles.tenant_id', '1', false)")
        cur.execute("SELECT set_config('waddles.community_id', '7', false)")
        cur.execute("SELECT title FROM app_community.waddles_cal_test__events")
        assert cur.fetchall() == [("a",)]
        # unscoped session sees nothing (fails closed)
        cur.execute("SELECT set_config('waddles.community_id', '', false)")
        cur.execute("SELECT count(*) FROM app_community.waddles_cal_test__events")
        assert cur.fetchone() == (0,)
        cur.execute("RESET ROLE")

    def test_additive_migration_adds_table_column_and_index_but_refuses_destruction(
        self, cur: Any
    ) -> None:
        old = _schema()
        provision_schema(cur, _APP, "thirdparty", old)
        raw = json.loads(json.dumps(_RAW))
        raw["tables"][0]["columns"].append({"name": "room", "type": "text(20)", "nullable": True})
        raw["tables"][0]["indexes"].append({"columns": ["kind"]})
        raw["tables"].append(
            {"name": "notes", "columns": [{"name": "body", "type": "text(50)", "nullable": True}]}
        )
        new = validate_schema_v2(raw, provider="thirdparty")
        migrate_schema(cur, _APP, "thirdparty", old, new)
        cur.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = 'waddles_cal_test__events' AND column_name = 'room'"
        )
        assert cur.fetchone() == ("room",)
        cur.execute("SELECT count(*) FROM bundle_data.table_registry WHERE app_id = %s", (_APP,))
        assert cur.fetchone() == (3,)
        # dropping a table or a column is destructive -> refused
        with pytest.raises(MigrationError) as e:
            migrate_schema(cur, _APP, "thirdparty", new, old)
        assert e.value.reason == "table_dropped"
        shrunk = json.loads(json.dumps(raw))
        shrunk["tables"][0]["columns"].pop(0)
        shrunk["tables"][0]["indexes"] = []
        with pytest.raises(MigrationError):
            migrate_schema(
                cur, _APP, "thirdparty", new, validate_schema_v2(shrunk, provider="thirdparty")
            )

    def test_teardown_drops_tables_registry_and_usage(self, cur: Any) -> None:
        s = _schema()
        provision_schema(cur, _APP, "thirdparty", s)
        cur.execute("INSERT INTO bundle_data.usage VALUES (%s, 1, 7, 3, 100)", (_APP,))
        teardown_schema(cur, _APP, "thirdparty", s)
        cur.execute("SELECT to_regclass('app_community.waddles_cal_test__events')")
        assert cur.fetchone() == (None,)
        cur.execute("SELECT count(*) FROM bundle_data.table_registry")
        assert cur.fetchone() == (0,)
        cur.execute("SELECT count(*) FROM bundle_data.usage")
        assert cur.fetchone() == (0,)
