"""Temporary local `raw_sql_rows`/`raw_sql_write` shim (M1.5, D21a).

`penguin_dal`'s single-table `TableProxy`/`AsyncQuerySet` builder cannot
express the joins, `GROUP BY`, `ORDER BY RANDOM()`, and `ON CONFLICT ...`
upsert statements several svc-process bundles need. Both helpers below wrap
`penguin_dal.AsyncDB`'s own public `.engine` property (a real SQLAlchemy
async engine) with `sqlalchemy.text()`, returning `penguin_dal.Row`/`Rows`
so callers get the exact same `row["x"]`/`row.x`/`rows.first()` ergonomics
as the query builder -- this is still "the `penguin-dal` public API"
(D21a), just the `.engine` escape hatch instead of the builder.

This module is a stopgap, scoped to `core/svc_process/bundles/` only,
because the milestone's own cross-service cutover (retyping the shared
runtime accessors' bound object type, and rewiring both bundle-hosting
services' startup to construct a `penguin_dal.AsyncDB` there instead) is a
single, all-bundles-migrated-first gate -- out of scope for a per-service
bundle migration. Once that cutover lands, this module's two functions
should move to the shared runtime module (same names, same signatures)
and every bundle importing from here should switch its import instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from penguin_dal import AsyncDB, Row, Rows
from sqlalchemy import text


async def raw_sql_rows(dal: AsyncDB, sql: str, params: Mapping[str, Any] | None = None) -> Rows:
    """Run a read-only raw SQL query against the bound `penguin_dal.AsyncDB`.

    Args:
        dal: The bound `penguin_dal.AsyncDB` (from `get_bundle_dal()`).
        sql: SQL text with named `:param` placeholders.
        params: Bind parameter values, or `None` for a parameterless query.

    Returns:
        A `penguin_dal.Rows` of the result set (empty if no rows matched).
    """
    async with dal.engine.connect() as conn:
        result = await conn.execute(text(sql), params or {})
        return Rows([Row(dict(mapping)) for mapping in result.mappings().all()])


async def raw_sql_write(dal: AsyncDB, sql: str, params: Mapping[str, Any] | None = None) -> Rows:
    """Run a raw SQL write (INSERT/UPDATE/DELETE, optionally `RETURNING`).

    Runs in a committed transaction against the bound `penguin_dal.AsyncDB`.

    Commits on success (via `AsyncDB.engine.begin()`'s own transaction
    scope) and rolls back on any exception raised inside the block. A
    statement with no `RETURNING` clause returns an empty `Rows`, never
    raises for that reason alone.

    Args:
        dal: The bound `penguin_dal.AsyncDB` (from `get_bundle_dal()`).
        sql: SQL text with named `:param` placeholders.
        params: Bind parameter values, or `None` for a parameterless statement.

    Returns:
        A `penguin_dal.Rows` of any `RETURNING` rows (empty otherwise).
    """
    async with dal.engine.begin() as conn:
        result = await conn.execute(text(sql), params or {})
        if result.returns_rows:
            return Rows([Row(dict(mapping)) for mapping in result.mappings().all()])
        return Rows([])
