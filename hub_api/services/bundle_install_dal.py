"""penguin-dal AsyncDB wiring for hub-api's M2b workstreams/usage-metering tables (R52).

Coordinator ruling R52 ("we aren't using pydal anymore... instead we are
running penguin-dal") applies to every table this slice's own migrations
create: `ingest_sources` (migration 0020), `workstreams` and
`workstream_usage_hourly` (migration 0021). hub-api's pre-existing pydal
surface (tenants, communities, community_members, community_roles,
app_catalog, hub_users, audit_log, and every other table
`hub_api/services/schema.py`'s existing `bind_*_tables()` functions
bind) is untouched -- this module never binds, defines, or migrates any
table; `build_install_dal()` only reflects tables Alembic already
created.

Coexistence: this AsyncDB is a SEPARATE SQLAlchemy async engine/connection
pool from the existing pydal AsyncDAL's pool, both pointed at the exact
same `DATABASE_URL` -- same host/port/user/password/scheme, so identical
TLS and auth posture with zero new configuration (`penguin_dal.
backends.ensure_async_uri()` maps hub-api's existing pydal-style
`postgres://` DSN straight to `postgresql+asyncpg://`).

`raw_sql_rows`/`raw_sql_write` are the escape hatch (joins/GROUP BY/
ON CONFLICT, single-statement) `penguin_dal`'s single-table Query/
TableProxy builder cannot express -- same idiom as `flask_core.
bundle_runtime`'s M1.5 helpers, copied here rather than imported (that
module's contextvar facade is scoped to WASM bundle execution, not
hub-api's own persistent, per-process AsyncDB).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from penguin_dal import AsyncDB, Row, Rows
from sqlalchemy import text

#: pydal's own in-memory-sqlite shorthand (no trailing colon) -- the exact
#: literal `tests/test_app_factory.py::_test_config()` and every
#: `libs/flask_core` DB fixture pass as `DATABASE_URL` to boot the full
#: app with no external dependency. `penguin_dal.backends.normalize_uri`
#: only special-cases `"sqlite:memory:"` (trailing colon) and
#: `"sqlite://:memory:"` -- neither matches this codebase's pydal
#: convention, so it falls through unrecognized and
#: `ensure_async_uri()` raises. Translated to bare `sqlite://`
#: (SQLAlchemy's own in-memory form) before ever reaching `AsyncDB`.
_PYDAL_MEMORY_URI = "sqlite:memory"
_SQLALCHEMY_MEMORY_URI = "sqlite://"


async def build_install_dal(database_url: str, pool_size: int) -> AsyncDB:
    """Construct the M2b `AsyncDB` and reflect the live schema.

    Called once, in `app.py::startup()`. `reflect()` discovers every
    table already in the live Postgres database -- migrations 0020-0021's
    new tables (created by Alembic before this ever runs) plus every
    pre-existing table.

    Args:
        database_url: The same DSN `HubAPIConfig.database_url` already
            builds for the existing pydal DAL -- reused verbatim.
        pool_size: Connection pool size for this second, independent pool.

    Returns:
        A fully reflected `AsyncDB`, ready for `TableProxy`/`Query` access
        against any table in the live schema.
    """
    if database_url == _PYDAL_MEMORY_URI:
        database_url = _SQLALCHEMY_MEMORY_URI
    install_dal = AsyncDB(database_url, pool_size=pool_size)
    await install_dal.reflect()
    return install_dal


async def raw_sql_rows(dal: AsyncDB, sql: str, params: Mapping[str, Any] | None = None) -> Rows:
    """Run a read-only raw SQL query.

    For the joins/GROUP BY/RANDOM() cases `penguin_dal`'s single-table
    Query builder cannot express.

    Args:
        dal: The `install_dal` `AsyncDB` (from `build_install_dal()`).
        sql: SQL text with named `:param` markers.
        params: Bind parameter values, or `None` for a parameterless query.

    Returns:
        A `penguin_dal.Rows` of the result set (empty if no rows matched).
    """
    async with dal.engine.connect() as conn:
        result = await conn.execute(text(sql), params or {})
        return Rows([Row(dict(mapping)) for mapping in result.mappings().all()])


async def raw_sql_write(dal: AsyncDB, sql: str, params: Mapping[str, Any] | None = None) -> Rows:
    """Run a single raw SQL write.

    INSERT/UPDATE/DELETE, optionally `RETURNING`, in its own committed
    transaction.

    Args:
        dal: The `install_dal` `AsyncDB` (from `build_install_dal()`).
        sql: SQL text with named `:param` markers.
        params: Bind parameter values, or `None` for a parameterless statement.

    Returns:
        A `penguin_dal.Rows` of any `RETURNING` rows (empty otherwise).
    """
    async with dal.engine.begin() as conn:
        result = await conn.execute(text(sql), params or {})
        try:
            mappings = result.mappings().all()
        except Exception:  # noqa: BLE001 -- driver raises when the statement has no result set (no RETURNING)
            mappings = []
        return Rows([Row(dict(mapping)) for mapping in mappings])
