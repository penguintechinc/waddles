"""DDL generation for bundle-owned data tables.

Phase 0 of `docs/superpowers/specs/2026-09-28-bundle-db-capability-and-
schemas.md` (Rev 5) §3/§6.2/§7 -- the "validated -> DDL" half of the
pipeline whose "declared -> validated" half lives in
:mod:`bundle_data_schema`. **Not wired into onboarding yet**: nothing here
is called from `bundle_approval_service.py` or any request path -- this is
a pure library, exercised only by its own tests, that a later phase wires
into the approval transaction (`waddles_bundle_migrator`, §2.1).

Every identifier (schema, table, column, index, role names) is emitted via
`psycopg2.sql.Identifier`; every value (literal defaults, GUC values used
in RLS policy comparisons at execution time) via `psycopg2.sql.Literal`.
**Never string-format identifiers or values into SQL text** -- every
function below returns a `psycopg2.sql.Composable`, executed by passing it
directly to `cursor.execute(...)` (psycopg2 applies quoting against the
live connection at execution time; there is no "render to text" step in
production use). `render_ddl_sql` exists only for tests, which need actual
SQL text for golden-snapshot assertions -- it still goes through
`Composable.as_string(cursor)`, the same psycopg2 quoting path, never
manual string interpolation.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from psycopg2 import extensions as pg_extensions
from psycopg2 import sql

from services.bundle_data_schema import (
    POSTGRES_IDENTIFIER_MAX_LEN,
    RESERVED_COLUMN_NAMES,
    TABLE_NAME_HASH_SUFFIX_LEN,
    ColumnDecl,
    ColumnType,
    IndexDecl,
    TableDeclaration,
    TableDeclarationError,
    TableIdentity,
)

# Roles provisioned once at Phase 0 (infra/hub-api, per the spec's §14
# phased plan) -- referenced here only by name, this module never issues
# `CREATE ROLE` itself.
MIGRATOR_ROLE = "waddles_bundle_migrator"
RUNTIME_ROLE = "waddles_bundle_runtime"

# RLS scope GUCs, set per-transaction via `SET LOCAL` from `InvokeScope`
# (§6.2/§7) -- referenced here only in the policy's `USING` clause.
_TENANT_GUC = "waddles.tenant_id"
_COMMUNITY_GUC = "waddles.community_id"

_SQL_TYPE_BY_KIND = {
    ColumnType.UUID: sql.SQL("uuid"),
    ColumnType.INT4: sql.SQL("integer"),
    ColumnType.INT8: sql.SQL("bigint"),
    ColumnType.BOOL: sql.SQL("boolean"),
    ColumnType.TIMESTAMPTZ: sql.SQL("timestamptz"),
    ColumnType.JSONB: sql.SQL("jsonb"),
}


class MigrationError(Exception):
    """Raised when an old->new `TableDeclaration` diff is not additive-safe.

    §11: additive changes (new nullable/defaulted column, new index) auto-
    apply; anything else (type change, column drop, `NOT NULL` with no
    default) requires an explicit destructive-migration path this module
    deliberately does not implement -- callers must reject or route those
    through the (separate, human-ack'd) destructive-migration flow.
    """

    def __init__(self, reason: str, detail: str) -> None:
        """Store the machine-checkable `reason` code alongside the human-readable `detail`."""
        self.reason = reason
        super().__init__(f"{reason}: {detail}")


REASON_COLUMN_DROPPED = "column_dropped"
REASON_COLUMN_TYPE_CHANGED = "column_type_changed"
REASON_COLUMN_NULLABILITY_CHANGED = "column_nullability_changed"
REASON_COLUMN_DEFAULT_CHANGED = "column_default_changed"
REASON_NEW_COLUMN_NOT_NULL_NO_DEFAULT = "new_column_not_null_no_default"


def _truncate_identifier(name: str) -> str:
    """Enforce Postgres's 63-byte `NAMEDATALEN` on a *derived* (not bundle-input) identifier.

    Index and policy names are built by appending suffixes to an already-
    validated table name (itself already <=63 bytes, per `derive_table_
    identity`) -- appending `_idx_...`/`_tenant_isolation` can still push
    the combined name over the limit. Postgres would otherwise silently
    truncate at 63 bytes itself, which risks two distinct derived names
    colliding on their shared prefix; truncating here first, with the same
    hash-suffix strategy `derive_table_identity` uses, keeps derived names
    collision-resistant instead of leaving it to chance.
    """
    if len(name) <= POSTGRES_IDENTIFIER_MAX_LEN:
        return name
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:TABLE_NAME_HASH_SUFFIX_LEN]
    keep = POSTGRES_IDENTIFIER_MAX_LEN - TABLE_NAME_HASH_SUFFIX_LEN - 1
    return f"{name[:keep]}_{digest}"


def qualified_identifier(identity: TableIdentity) -> sql.Composed:
    """`schema.table`, both parts quoted via `sql.Identifier` -- never string-joined."""
    return sql.SQL("{}.{}").format(sql.Identifier(identity.schema), sql.Identifier(identity.table))


def _column_sql_type(column: ColumnDecl) -> sql.Composable:
    kind = column.type_kind
    if kind is ColumnType.USER_REF:
        return sql.SQL("uuid")
    if kind is ColumnType.NUMERIC:
        pt = column.parsed_type
        return sql.SQL("numeric({},{})").format(
            sql.SQL(str(pt.numeric_precision)), sql.SQL(str(pt.numeric_scale))
        )
    if kind is ColumnType.TEXT:
        pt = column.parsed_type
        return sql.SQL("varchar({})").format(sql.SQL(str(pt.max_len)))
    return _SQL_TYPE_BY_KIND[kind]


def _default_literal_sql(column: ColumnDecl) -> sql.Composable | None:
    if column.default is None:
        return None
    return sql.Literal(column.default.value)


def _column_definition_sql(column: ColumnDecl) -> sql.Composable:
    parts: list[sql.Composable] = [sql.Identifier(column.name), _column_sql_type(column)]
    if not column.nullable:
        parts.append(sql.SQL("NOT NULL"))
    default_sql = _default_literal_sql(column)
    if default_sql is not None:
        parts.append(sql.SQL("DEFAULT {}").format(default_sql))
    return sql.SQL(" ").join(parts)


# Platform-owned (mandatory) columns, §3.3 -- fixed template, never
# bundle-influenced. `gen_random_uuid()`/`now()` are safe here (unlike a
# bundle-declared default) because they are authored in this file, not
# accepted from manifest input.
def _platform_columns_sql() -> list[sql.Composable]:
    return [
        sql.SQL("row_id uuid PRIMARY KEY DEFAULT gen_random_uuid()"),
        sql.SQL("tenant_id integer NOT NULL"),
        sql.SQL("community_id integer NOT NULL"),
        sql.SQL("version integer NOT NULL DEFAULT 1"),
        sql.SQL("created_at timestamptz NOT NULL DEFAULT now()"),
        sql.SQL("updated_at timestamptz NOT NULL DEFAULT now()"),
    ]


def generate_create_table_ddl(
    identity: TableIdentity, declaration: TableDeclaration
) -> tuple[sql.Composable, ...]:
    """Build the full fixed-template DDL statement set for one bundle table.

    Order matches the spec's own template (§3.3/§3.4/§7): `CREATE TABLE`
    (platform columns + declared columns), declared indexes (`tenant_id`
    auto-prefixed, §3.5), `ENABLE`+`FORCE ROW LEVEL SECURITY` (§3.4 C1.2 --
    `FORCE` closes the owning-role RLS-bypass gap `ENABLE` alone leaves
    open), the RLS policy itself (`current_setting(..., true)` so an unset
    GUC compares to NULL and fails closed, never matches), and an explicit
    per-table `GRANT` to `waddles_bundle_runtime` (defense in depth on top
    of the schema-level `ALTER DEFAULT PRIVILEGES` a table's creating role
    already receives at Phase 0 -- see the spec's §2 role table).
    """
    for column in declaration.columns:
        if column.name in RESERVED_COLUMN_NAMES:  # pragma: no cover - already rejected upstream
            raise TableDeclarationError("reserved_column_name", column.name)

    column_defs = _platform_columns_sql() + [_column_definition_sql(c) for c in declaration.columns]
    qualified = qualified_identifier(identity)

    create_table = sql.SQL("CREATE TABLE {} (\n    {}\n)").format(
        qualified, sql.SQL(",\n    ").join(column_defs)
    )

    statements: list[sql.Composable] = [create_table]
    statements.extend(_generate_index_ddl(identity, index) for index in declaration.indexes)

    statements.append(sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(qualified))
    statements.append(sql.SQL("ALTER TABLE {} FORCE ROW LEVEL SECURITY").format(qualified))
    statements.append(_generate_rls_policy_ddl(identity))
    statements.append(
        sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO {}").format(
            qualified, sql.Identifier(RUNTIME_ROLE)
        )
    )
    return tuple(statements)


def _index_name(identity: TableIdentity, index: IndexDecl) -> str:
    # Deterministic within one table: every column the bundle declared in
    # this index, in order, direction-suffixed only for descending columns
    # (matches the worked example's `(kind, score desc)` naming intuition).
    # `_truncate_identifier` handles the case where appending this suffix
    # to an already <=63-byte table name pushes the combined name over the
    # limit.
    parts = [identity.table, "idx", "tenant_id"]
    for col in index.columns:
        parts.append(col.name if not col.descending else f"{col.name}_desc")
    return _truncate_identifier("_".join(parts))


def _generate_index_ddl(identity: TableIdentity, index: IndexDecl) -> sql.Composable:
    """One `CREATE INDEX`, `tenant_id` auto-prefixed ahead of the bundle's own columns (§3.5)."""
    qualified = qualified_identifier(identity)
    index_ident = sql.Identifier(_index_name(identity, index))

    column_exprs: list[sql.Composable] = [sql.Identifier("tenant_id")]
    for col in index.columns:
        expr: sql.Composable = sql.Identifier(col.name)
        if col.descending:
            expr = sql.SQL("{} DESC").format(expr)
        column_exprs.append(expr)

    return sql.SQL("CREATE INDEX {} ON {} ({})").format(
        index_ident, qualified, sql.SQL(", ").join(column_exprs)
    )


def _generate_rls_policy_ddl(identity: TableIdentity) -> sql.Composable:
    """One tenant+community RLS policy -- the `SET LOCAL` half of §6.2's defense-in-depth.

    `NULLIF(current_setting(..., true), '')::integer`, not a bare
    `current_setting(..., true)::integer`: a custom (extension-namespaced)
    GUC that has been `SET` at least once in a session and later `RESET`
    (or discarded via `DISCARD ALL` at pool checkin, §7) reverts to an
    **empty string**, not SQL NULL -- confirmed against a real Postgres,
    not assumed. A bare `::integer` cast on `''` raises
    `invalid_text_representation`, not a clean "no match"; wrapping in
    `NULLIF(..., '')` first normalizes both "never set" (already NULL) and
    "set-then-reset/discarded" (empty string) to NULL, so the policy
    actually fails closed (zero rows) in both cases instead of raising in
    one of them.
    """
    qualified = qualified_identifier(identity)
    policy_name = sql.Identifier(_truncate_identifier(f"{identity.table}_tenant_isolation"))
    using_clause = sql.SQL(
        "tenant_id = NULLIF(current_setting({tenant_guc}, true), '')::integer "
        "AND community_id = NULLIF(current_setting({community_guc}, true), '')::integer"
    ).format(tenant_guc=sql.Literal(_TENANT_GUC), community_guc=sql.Literal(_COMMUNITY_GUC))
    return sql.SQL("CREATE POLICY {} ON {} USING ({})").format(policy_name, qualified, using_clause)


def generate_drop_table_ddl(identity: TableIdentity) -> sql.Composable:
    """Teardown DDL (§10) -- the physical drop; crypto-shred/chunked-delete run before this."""
    return sql.SQL("DROP TABLE IF EXISTS {}").format(qualified_identifier(identity))


# ---------------------------------------------------------------------------
# Additive migrations (§11)
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class MigrationPlan:
    """The additive-only DDL needed to move one table from an old to a new declaration."""

    added_columns: tuple[ColumnDecl, ...]
    added_indexes: tuple[IndexDecl, ...]

    @property
    def is_empty(self) -> bool:
        """True when the diff added nothing (both declarations are identical)."""
        return not self.added_columns and not self.added_indexes


def compute_additive_migration(old: TableDeclaration, new: TableDeclaration) -> MigrationPlan:
    """Diff two declarations, asserting the change is additive-only (§11).

    Additive: every column present in `old` is present in `new`,
    unchanged (name, type, nullability, default) -- new columns must be
    `nullable` or carry a `default` (so existing rows stay valid without a
    backfill). Anything else (a dropped column, a changed type/nullability/
    default on an existing column, or a new `NOT NULL` column with no
    default) raises `MigrationError` -- those are destructive changes,
    routed to the separate ack'd/chunked-transform path this module does
    not implement.
    """
    old_by_name = {c.name: c for c in old.columns}
    new_by_name = {c.name: c for c in new.columns}

    for name, old_col in old_by_name.items():
        if name not in new_by_name:
            raise MigrationError(
                REASON_COLUMN_DROPPED, f"column {name!r} is missing from the new declaration"
            )
        new_col = new_by_name[name]
        if new_col.parsed_type != old_col.parsed_type:
            raise MigrationError(REASON_COLUMN_TYPE_CHANGED, f"column {name!r} changed type")
        if new_col.nullable != old_col.nullable:
            raise MigrationError(
                REASON_COLUMN_NULLABILITY_CHANGED, f"column {name!r} changed nullability"
            )
        if new_col.default != old_col.default:
            raise MigrationError(REASON_COLUMN_DEFAULT_CHANGED, f"column {name!r} changed default")

    added_columns = tuple(c for name, c in new_by_name.items() if name not in old_by_name)
    for column in added_columns:
        if not column.nullable and column.default is None:
            raise MigrationError(
                REASON_NEW_COLUMN_NOT_NULL_NO_DEFAULT,
                f"new column {column.name!r} is NOT NULL with no default -- needs a backfill, "
                "not an additive migration",
            )

    old_index_keys = {_index_key(i) for i in old.indexes}
    added_indexes = tuple(i for i in new.indexes if _index_key(i) not in old_index_keys)

    return MigrationPlan(added_columns=added_columns, added_indexes=added_indexes)


def _index_key(index: IndexDecl) -> tuple[tuple[str, bool], ...]:
    return tuple((c.name, c.descending) for c in index.columns)


def generate_additive_migration_ddl(
    identity: TableIdentity, plan: MigrationPlan
) -> tuple[sql.Composable, ...]:
    """`ALTER TABLE ... ADD COLUMN` / `CREATE INDEX` for one `MigrationPlan`, nothing else."""
    qualified = qualified_identifier(identity)
    statements: list[sql.Composable] = []
    for column in plan.added_columns:
        statements.append(
            sql.SQL("ALTER TABLE {} ADD COLUMN {}").format(
                qualified, _column_definition_sql(column)
            )
        )
    statements.extend(_generate_index_ddl(identity, index) for index in plan.added_indexes)
    return tuple(statements)


# ---------------------------------------------------------------------------
# `indexed-column` static enum mapping (§6.2 C1.1)
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class IndexedColumnMapping:
    """One `indexed-column` WIT enum variant -> its pre-quoted column identifier.

    Built entirely from this table's own already-validated column names --
    no bundle-supplied value is ever interpolated into this mapping, at
    generation time (this function only reads `declaration.indexes`, which
    `bundle_data_schema` already validated column-by-column) or at lookup
    time (a consumer matches on `variant`, a fixed Python/Rust identifier,
    never a request-supplied string). This is the "static lookup table
    generated once, at onboarding" the spec's §6.2/C1.1 requires -- the
    Rust query builder (a later phase, not implemented here) embeds it
    verbatim into the app's compiled component bindings.
    """

    variant: str
    column_identifier: sql.Identifier


def generate_indexed_column_mapping(
    declaration: TableDeclaration,
) -> tuple[IndexedColumnMapping, ...]:
    """One mapping entry per distinct column referenced by any declared index.

    Order is deterministic (first appearance across `declaration.indexes`,
    in index-declaration order, then within-index column order) so two
    calls against the same `TableDeclaration` always produce the same
    enum-variant ordering.
    """
    seen: dict[str, IndexedColumnMapping] = {}
    for index in declaration.indexes:
        for col in index.columns:
            if col.name not in seen:
                seen[col.name] = IndexedColumnMapping(
                    variant=col.name, column_identifier=sql.Identifier(col.name)
                )
    return tuple(seen.values())


# ---------------------------------------------------------------------------
# Rendering helper (tests only -- production code executes Composables
# directly against a live cursor, it never renders them to text)
# ---------------------------------------------------------------------------


def render_ddl_sql(
    statements: Iterable[sql.Composable],
    cursor: pg_extensions.cursor | pg_extensions.connection,
) -> Sequence[str]:
    """Render `Composable` statements to SQL text via psycopg2's own quoting.

    `cursor` must be a real psycopg2 cursor/connection -- `Composable.
    as_string` requires one to apply identifier/literal quoting correctly;
    there is no offline renderer, by design (the same reason production
    code never renders to text at all, it executes the `Composable`
    directly). Test-only.
    """
    return [statement.as_string(cursor) for statement in statements]
