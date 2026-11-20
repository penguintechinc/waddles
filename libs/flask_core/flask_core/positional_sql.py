"""Correct `$N` positional-placeholder conversion for raw SQL.

PostgreSQL-style `$N` markers are rewritten into a DB-API marker (`%s` for psycopg2,
`?` for sqlite3). A naive `str.replace` mishandles repeated, out-of-order and
`$1`/`$10` markers and rewrites text inside literals; since positional binding then
sends the wrong values to the wrong slots, this tokenizer is the single shared
implementation.
"""

from __future__ import annotations

from typing import Any, Sequence


class PositionalSQLError(ValueError):
    """Raised when a `$N` marker has no matching parameter (or an invalid index)."""


def _skip_quoted(sql: str, i: int, quote: str, backslash: bool) -> int:
    """Return the index just past the quoted run starting at `sql[i]` (doubled quote = escape)."""
    n = len(sql)
    i += 1
    while i < n:
        ch = sql[i]
        if backslash and ch == "\\":
            i += 2
            continue
        if ch == quote:
            if i + 1 < n and sql[i + 1] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    return n


def _dollar_tag_end(sql: str, i: int) -> int:
    """If `sql[i:]` opens a dollar-quote tag like `$tag$` / `$$`, return its end index, else -1."""
    j = i + 1
    n = len(sql)
    if j < n and (sql[j].isalpha() or sql[j] == "_"):
        while j < n and (sql[j].isalnum() or sql[j] == "_"):
            j += 1
    return j + 1 if j < n and sql[j] == "$" else -1


def convert_positional(
    sql: str, params: Sequence[Any] | None, marker: str = "%s"
) -> tuple[str, list[Any]]:
    """Rewrite `$N` markers to `marker`, expanding `params` in occurrence order.

    Skips single-quoted strings (incl. `E'..'`), double-quoted identifiers, `--` and
    `/* */` comments and dollar-quoted bodies. `$1 ... $1` yields `[p1, p1]`.
    Raises PositionalSQLError for `$0` or an index beyond `params`.
    """
    values = list(params) if params is not None else []
    out: list[str] = []
    ordered: list[Any] = []
    n = len(sql)
    i = 0
    while i < n:
        ch = sql[i]
        if ch == "'":
            escaped = i > 0 and sql[i - 1] in "eE" and (i < 2 or not (sql[i - 2].isalnum() or sql[i - 2] == "_"))
            end = _skip_quoted(sql, i, "'", escaped)
        elif ch == '"':
            end = _skip_quoted(sql, i, '"', False)
        elif ch == "-" and sql.startswith("--", i):
            nl = sql.find("\n", i)
            end = n if nl == -1 else nl
        elif ch == "/" and sql.startswith("/*", i):
            close = sql.find("*/", i + 2)
            end = n if close == -1 else close + 2
        elif ch == "$":
            j = i + 1
            while j < n and sql[j].isdigit():
                j += 1
            if j > i + 1:
                index = int(sql[i + 1 : j])
                if index < 1 or index > len(values):
                    raise PositionalSQLError(
                        f"placeholder ${index} has no matching parameter "
                        f"({len(values)} supplied)"
                    )
                ordered.append(values[index - 1])
                out.append(marker)
                i = j
                continue
            tag_end = _dollar_tag_end(sql, i)
            if tag_end == -1:
                end = i + 1
            else:
                close = sql.find(sql[i:tag_end], tag_end)
                end = n if close == -1 else close + (tag_end - i)
        else:
            end = i + 1
        out.append(sql[i:end])
        i = end
    return "".join(out), ordered
