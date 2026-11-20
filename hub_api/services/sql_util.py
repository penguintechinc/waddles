"""Adapter-neutral raw-SQL helpers shared by every hub_api service.

Services write readable `$1..$n` SQL; `execute_positional` rewrites the markers into the live
pydal adapter's native paramstyle (`%s` psycopg2, `?` sqlite3) so the same SQL runs on both.
The rewrite is delegated to `flask_core.positional_sql.convert_positional` (the single shared
tokenizer). Never interpolate values into SQL -- always pass them as placeholders.
"""

from __future__ import annotations

from typing import Any

from flask_core.positional_sql import PositionalSQLError, convert_positional

__all__ = ["PositionalSQLError", "execute_positional", "positional_sql"]


def positional_sql(dal: Any, sql: str, placeholders: list[Any]) -> tuple[str, list[Any]]:
    """Rewrite `$1..$n` markers into the live adapter's native positional marker.

    pydal passes SQL to the driver verbatim: psycopg2 (Postgres) wants `%s`, sqlite3 wants `?`,
    and neither understands `$N`. Delegates to `convert_positional`, so markers inside literals,
    comments and dollar-quoted bodies are left alone, repeated/out-of-order markers are expanded
    one value per occurrence, and `$0` or an out-of-range index raises `PositionalSQLError`.
    """
    engine = str(getattr(getattr(dal, "_adapter", None), "dbengine", "")).lower()
    marker = "?" if engine == "sqlite" else "%s"
    return convert_positional(sql, placeholders, marker)


def execute_positional(dal: Any, sql: str, placeholders: list[Any]) -> Any:
    """`dal.executesql` for `$N`-style SQL, adapted to the adapter's paramstyle."""
    native_sql, values = positional_sql(dal, sql, placeholders)
    return dal.executesql(native_sql, placeholders=values)
