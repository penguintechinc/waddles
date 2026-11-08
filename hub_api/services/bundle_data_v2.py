"""`db` v2 declaration, DDL and provisioning: several tables per bundle.

Extends the single-table pipeline (:mod:`bundle_data_schema` ->
:mod:`bundle_data_ddl`) without changing it: a v2 bundle declares

.. code-block:: yaml

    data:
      tables: [events, subs]          # names -- drives the `db` capability (unchanged)
      schema:                         # the typed detail (this module)
        tables:
          - name: events
            columns: [{name: title, type: "text(200)"}, {name: start_at, type: timestamptz}]
            indexes:
              - {columns: [start_at]}
              - {columns: [{column: title, prefix: true}]}
              - {columns: [slug], unique: true}

Every column goes through :func:`bundle_data_schema.validate_table_declaration`
(typed allowlist, PII-name denylist, reserved platform columns, defaults), so v2
inherits every v1 rule. v2 adds: up to :data:`MAX_TABLES` tables, ``unique`` and
``prefix`` (``text_pattern_ops``) indexes, and indexes always prefixed with
``(tenant_id, community_id)`` -- the scope the data plane injects on every query.

Like its v1 siblings this module only builds validated dataclasses and
``psycopg2.sql.Composable`` DDL (identifiers via ``sql.Identifier``, values via
``sql.Literal``) -- no string-formatted SQL. :func:`provision_schema` applies it
(run as ``waddles_bundle_migrator``) and writes ``bundle_data.table_registry``,
the metadata the data plane reads to resolve a guest's *logical* table name to
the physical table. Wiring it into the approval transaction is the same
follow-up as v1's (see ``docs/bundle-storage-v2.md``).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from psycopg2 import sql

from services.bundle_data_ddl import (
    MigrationError,
    _truncate_identifier,
    compute_additive_migration,
    generate_additive_migration_ddl,
    generate_create_table_ddl,
    generate_drop_table_ddl,
    qualified_identifier,
)
from services.bundle_data_schema import (
    APP_COMMUNITY_SCHEMA,
    APP_CORE_SCHEMA,
    MAX_INDEX_WIDTH,
    MAX_INDEXES,
    POSTGRES_IDENTIFIER_MAX_LEN,
    TABLE_NAME_HASH_SUFFIX_LEN,
    ColumnDecl,
    ColumnType,
    IndexColumnRef,
    TableDeclaration,
    TableDeclarationError,
    TableIdentity,
    derive_table_identity,
    validate_table_declaration,
)

MAX_TABLES = 8
_LOGICAL_NAME_RE = re.compile(r"\A[a-z][a-z0-9_]{0,31}\Z")
_TABLE_SEPARATOR = "__"

REASON_NO_TABLES = "no_tables"
REASON_TOO_MANY_TABLES = "too_many_tables"
REASON_INVALID_TABLE_NAME = "invalid_table_name"
REASON_DUPLICATE_TABLE = "duplicate_table"
REASON_TABLE_NAMES_MISMATCH = "table_names_mismatch"
REASON_PREFIX_NOT_TEXT = "prefix_index_not_text"
REASON_INDEX_SHAPE = "invalid_index"


@dataclass(slots=True, frozen=True)
class IndexColumnV2:
    """One column of a v2 index: direction plus the optional ``text_pattern_ops`` flag."""

    name: str
    descending: bool = False
    prefix_ops: bool = False


@dataclass(slots=True, frozen=True)
class IndexDeclV2:
    """A v2 index; ``(tenant_id, community_id)`` is always prefixed at DDL time."""

    columns: tuple[IndexColumnV2, ...]
    unique: bool = False


@dataclass(slots=True, frozen=True)
class TableDeclV2:
    """One validated v2 table: logical name, v1-validated columns, v2 indexes."""

    name: str
    declaration: TableDeclaration  # columns only; indexes live in `indexes`
    indexes: tuple[IndexDeclV2, ...] = ()


@dataclass(slots=True, frozen=True)
class SchemaDeclV2:
    """All of one bundle's v2 tables."""

    tables: tuple[TableDeclV2, ...]

    def table(self, name: str) -> TableDeclV2 | None:
        """Return the table named ``name``, or ``None``."""
        for t in self.tables:
            if t.name == name:
                return t
        return None


def _parse_index(raw: Mapping[str, Any], columns: TableDeclaration) -> IndexDeclV2:
    raw_cols = raw.get("columns")
    if not isinstance(raw_cols, list | tuple) or not raw_cols:
        raise TableDeclarationError(REASON_INDEX_SHAPE, "an index must declare at least one column")
    if len(raw_cols) > MAX_INDEX_WIDTH:
        raise TableDeclarationError(
            REASON_INDEX_SHAPE, f"index has {len(raw_cols)} columns, exceeds {MAX_INDEX_WIDTH}"
        )
    parsed = []
    name: Any
    for entry in raw_cols:
        if isinstance(entry, str):
            name, desc, prefix = entry, False, False
        elif isinstance(entry, Mapping):
            name = entry.get("column")
            desc = str(entry.get("dir", "asc")).lower() == "desc"
            prefix = bool(entry.get("prefix", False))
        else:
            raise TableDeclarationError(REASON_INDEX_SHAPE, f"bad index column entry {entry!r}")
        column = columns.column(name) if isinstance(name, str) else None
        if column is None:
            raise TableDeclarationError(
                REASON_INDEX_SHAPE, f"index references undeclared column {name!r}"
            )
        if prefix and column.type_kind is not ColumnType.TEXT:
            raise TableDeclarationError(
                REASON_PREFIX_NOT_TEXT, f"prefix index on non-text column {name!r}"
            )
        parsed.append(IndexColumnV2(name=name, descending=desc, prefix_ops=prefix))
    return IndexDeclV2(columns=tuple(parsed), unique=bool(raw.get("unique", False)))


def validate_schema_v2(
    raw: Mapping[str, Any],
    *,
    provider: str,
    declared_names: Sequence[str] | None = None,
) -> SchemaDeclV2:
    """Validate a manifest's ``data.schema`` into a :class:`SchemaDeclV2`.

    ``declared_names`` (the manifest's ``data.tables`` list) must equal the set of
    declared schema tables when given -- one source of truth for what the
    approval UI shows and what is provisioned. ``provider`` is accepted for the
    same reason as in :func:`validate_table_declaration` (resolved by the caller
    from the platform registry, never from the manifest).
    """
    raw_tables = raw.get("tables")
    if not isinstance(raw_tables, list | tuple) or not raw_tables:
        raise TableDeclarationError(REASON_NO_TABLES, "data.schema.tables must declare a table")
    if len(raw_tables) > MAX_TABLES:
        raise TableDeclarationError(
            REASON_TOO_MANY_TABLES, f"{len(raw_tables)} tables exceeds the cap of {MAX_TABLES}"
        )
    tables: list[TableDeclV2] = []
    seen: set[str] = set()
    for raw_table in raw_tables:
        name = raw_table.get("name") if isinstance(raw_table, Mapping) else None
        if (
            not isinstance(name, str)
            or not _LOGICAL_NAME_RE.fullmatch(name)
            or _TABLE_SEPARATOR in name
        ):
            raise TableDeclarationError(
                REASON_INVALID_TABLE_NAME,
                f"table name {name!r} must match [a-z][a-z0-9_]{{0,31}} and not contain '__'",
            )
        if name in seen:
            raise TableDeclarationError(REASON_DUPLICATE_TABLE, f"table {name!r} declared twice")
        seen.add(name)
        # Columns reuse the v1 validator wholesale (typed allowlist, PII denylist, ...).
        declaration = validate_table_declaration(
            {"columns": raw_table.get("columns"), "indexes": []}, provider=provider
        )
        raw_indexes = raw_table.get("indexes", ())
        if len(raw_indexes) > MAX_INDEXES:
            raise TableDeclarationError(
                REASON_INDEX_SHAPE, f"{len(raw_indexes)} indexes exceeds the cap of {MAX_INDEXES}"
            )
        indexes = tuple(_parse_index(i, declaration) for i in raw_indexes)
        tables.append(TableDeclV2(name=name, declaration=declaration, indexes=indexes))
    if declared_names is not None and set(declared_names) != seen:
        raise TableDeclarationError(
            REASON_TABLE_NAMES_MISMATCH,
            f"data.tables {sorted(declared_names)} != data.schema.tables {sorted(seen)}",
        )
    return SchemaDeclV2(tables=tuple(tables))


def derive_v2_table_identity(app_id: str, logical: str, *, provider: str) -> TableIdentity:
    """Physical ``(schema, table)`` for one logical table: ``<app>__<logical>``.

    Built on :func:`derive_table_identity` (provider-driven schema, strict
    charset). If the result exceeds Postgres' 63 bytes it is truncated with a
    ``sha256(app_id/logical)`` suffix so two truncated names cannot collide.
    """
    base = derive_table_identity(app_id, provider=provider)
    if not _LOGICAL_NAME_RE.fullmatch(logical) or _TABLE_SEPARATOR in logical:
        raise TableDeclarationError("invalid_table_name", f"bad logical name {logical!r}")
    table = f"{base.table}{_TABLE_SEPARATOR}{logical}"
    if len(table) > POSTGRES_IDENTIFIER_MAX_LEN:
        digest = hashlib.sha256(f"{app_id}/{logical}".encode()).hexdigest()
        digest = digest[:TABLE_NAME_HASH_SUFFIX_LEN]
        keep = POSTGRES_IDENTIFIER_MAX_LEN - TABLE_NAME_HASH_SUFFIX_LEN - 1
        table = f"{table[:keep]}_{digest}"
    return TableIdentity(schema=base.schema, table=table)


def _index_name(identity: TableIdentity, index: IndexDeclV2) -> str:
    parts = [identity.table, "ux" if index.unique else "ix"]
    for c in index.columns:
        parts.append(c.name + ("_desc" if c.descending else "") + ("_pfx" if c.prefix_ops else ""))
    return _truncate_identifier("_".join(parts))


def generate_v2_index_ddl(identity: TableIdentity, index: IndexDeclV2) -> sql.Composable:
    """One ``CREATE [UNIQUE] INDEX`` over ``(tenant_id, community_id, <declared columns>)``."""
    exprs: list[sql.Composable] = [sql.Identifier("tenant_id"), sql.Identifier("community_id")]
    for c in index.columns:
        parts: list[sql.Composable] = [sql.Identifier(c.name)]
        if c.prefix_ops:
            parts.append(sql.SQL("text_pattern_ops"))
        if c.descending:
            parts.append(sql.SQL("DESC"))
        exprs.append(sql.SQL(" ").join(parts))
    return sql.SQL("CREATE {}INDEX {} ON {} ({})").format(
        sql.SQL("UNIQUE " if index.unique else ""),
        sql.Identifier(_index_name(identity, index)),
        qualified_identifier(identity),
        sql.SQL(", ").join(exprs),
    )


def generate_v2_table_ddl(
    identity: TableIdentity, table: TableDeclV2
) -> tuple[sql.Composable, ...]:
    """Full DDL for one v2 table: v1's create/RLS/policy/grant template + v2 indexes."""
    base = generate_create_table_ddl(identity, table.declaration)
    return (
        base[0],
        *(generate_v2_index_ddl(identity, i) for i in table.indexes),
        *base[1:],
    )


def _column_json(col: ColumnDecl) -> dict[str, Any]:
    kind = col.type_kind
    out: dict[str, Any] = {"name": col.name, "nullable": col.nullable}
    if kind is ColumnType.USER_REF:
        out.update(type="uuid", user_ref=True)
    elif kind is ColumnType.NUMERIC:
        out.update(
            type="numeric",
            precision=col.parsed_type.numeric_precision,
            scale=col.parsed_type.numeric_scale,
        )
    else:
        out["type"] = kind.value
    return out


def registry_row(app_id: str, provider: str, table: TableDeclV2) -> dict[str, Any]:
    """The ``bundle_data.table_registry`` row the data plane resolves a logical table from."""
    identity = derive_v2_table_identity(app_id, table.name, provider=provider)
    return {
        "app_id": app_id,
        "logical_name": table.name,
        "schema_kind": "core" if identity.schema == APP_CORE_SCHEMA else "community",
        "physical_table": identity.table,
        "columns": json.dumps([_column_json(c) for c in table.declaration.columns]),
        "indexes": json.dumps(
            [
                {
                    "unique": i.unique,
                    "columns": [
                        {"name": c.name, "descending": c.descending, "prefix_ops": c.prefix_ops}
                        for c in i.columns
                    ],
                }
                for i in table.indexes
            ]
        ),
    }


_UPSERT_REGISTRY = sql.SQL(
    "INSERT INTO bundle_data.table_registry "
    "(app_id, logical_name, schema_kind, physical_table, columns, indexes) "
    "VALUES (%(app_id)s, %(logical_name)s, %(schema_kind)s, %(physical_table)s, "
    "%(columns)s::jsonb, %(indexes)s::jsonb) "
    "ON CONFLICT (app_id, logical_name) DO UPDATE SET "
    "columns = EXCLUDED.columns, indexes = EXCLUDED.indexes, updated_at = now()"
)


def provision_schema(cursor: Any, app_id: str, provider: str, schema: SchemaDeclV2) -> None:
    """Create every table (+ RLS, grants, indexes) and register it, in the caller's transaction.

    Run as ``waddles_bundle_migrator``. Atomic with the caller's transaction:
    a failure part-way leaves neither tables nor registry rows.
    """
    for table in schema.tables:
        identity = derive_v2_table_identity(app_id, table.name, provider=provider)
        for stmt in generate_v2_table_ddl(identity, table):
            cursor.execute(stmt)
        cursor.execute(_UPSERT_REGISTRY, registry_row(app_id, provider, table))


def migrate_schema(
    cursor: Any, app_id: str, provider: str, old: SchemaDeclV2, new: SchemaDeclV2
) -> None:
    """Additive upgrade: new tables, new nullable/defaulted columns, new indexes.

    Anything destructive (dropped table or column, type change) raises
    :class:`MigrationError`, exactly like the v1 additive-only rule.
    """
    for old_table in old.tables:
        if new.table(old_table.name) is None:
            raise MigrationError("table_dropped", f"table {old_table.name!r} was removed")
    for table in new.tables:
        identity = derive_v2_table_identity(app_id, table.name, provider=provider)
        previous = old.table(table.name)
        if previous is None:
            for stmt in generate_v2_table_ddl(identity, table):
                cursor.execute(stmt)
        else:
            plan = compute_additive_migration(previous.declaration, table.declaration)
            for stmt in generate_additive_migration_ddl(identity, plan):
                cursor.execute(stmt)
            old_keys = {_index_key(i) for i in previous.indexes}
            for index in table.indexes:
                if _index_key(index) not in old_keys:
                    cursor.execute(generate_v2_index_ddl(identity, index))
        cursor.execute(_UPSERT_REGISTRY, registry_row(app_id, provider, table))


def _index_key(index: IndexDeclV2) -> tuple[Any, ...]:
    return (index.unique, tuple((c.name, c.descending, c.prefix_ops) for c in index.columns))


def teardown_schema(cursor: Any, app_id: str, provider: str, schema: SchemaDeclV2) -> None:
    """Drop every table and its registry/usage rows (uninstall; run as the migrator)."""
    for table in schema.tables:
        identity = derive_v2_table_identity(app_id, table.name, provider=provider)
        cursor.execute(generate_drop_table_ddl(identity))
    cursor.execute("DELETE FROM bundle_data.table_registry WHERE app_id = %s", (app_id,))
    cursor.execute("DELETE FROM bundle_data.usage WHERE app_id = %s", (app_id,))


__all__ = [
    "APP_COMMUNITY_SCHEMA",
    "MAX_TABLES",
    "IndexColumnRef",
    "IndexColumnV2",
    "IndexDeclV2",
    "SchemaDeclV2",
    "TableDeclV2",
    "derive_v2_table_identity",
    "generate_v2_index_ddl",
    "generate_v2_table_ddl",
    "migrate_schema",
    "provision_schema",
    "registry_row",
    "teardown_schema",
    "validate_schema_v2",
]
