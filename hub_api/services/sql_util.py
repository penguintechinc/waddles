"""Adapter-neutral raw-SQL helpers shared by every hub_api service.

Services write readable `$1..$n` SQL; `execute_positional` rewrites the markers into the live
pydal adapter's native paramstyle (`%s` psycopg2, `?` sqlite3) so the same SQL runs on both.
Never interpolate values into SQL -- always pass them as placeholders.
"""

from __future__ import annotations

import re
from typing import Any

_DOLLAR_PARAM = re.compile(r"\$(\d+)")


def positional_sql(dal: Any, sql: str, placeholders: list[Any]) -> tuple[str, list[Any]]:
    """Rewrite `$1..$n` markers into the live adapter's native positional marker.

    pydal passes SQL to the driver verbatim: psycopg2 (Postgres) wants `%s`, sqlite3 wants `?`,
    and neither understands `$N` -- Postgres raised `there is no parameter $1` (alpha leaderboard
    500). Markers may repeat or appear out of order; the returned list is re-expanded to match,
    one value per occurrence, so callers keep writing readable `$N` SQL.
    """
    engine = str(getattr(getattr(dal, "_adapter", None), "dbengine", "")).lower()
    marker = "?" if engine == "sqlite" else "%s"
    ordered: list[Any] = []

    def _sub(match: re.Match[str]) -> str:
        ordered.append(placeholders[int(match.group(1)) - 1])
        return marker

    return _DOLLAR_PARAM.sub(_sub, sql), ordered


def execute_positional(dal: Any, sql: str, placeholders: list[Any]) -> Any:
    """`dal.executesql` for `$N`-style SQL, adapted to the adapter's paramstyle."""
    native_sql, values = positional_sql(dal, sql, placeholders)
    return dal.executesql(native_sql, placeholders=values)
