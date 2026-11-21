"""pydal custom types for native Postgres ``TEXT[]`` / ``INTEGER[]`` columns.

pydal's built-in ``list:string`` / ``list:integer`` model a list as a bar-delimited string
(``|a|b|``). The hub-api schema is created by Alembic with real Postgres array columns, so with
those built-ins every SELECT of a non-NULL array column raised
``TypeError: expected string or bytes-like object, got 'list'`` (psycopg2 already hands back a
``list``) and every INSERT wrote a bar-string Postgres rejects as a malformed array literal.

``PG_TEXT_ARRAY`` / ``PG_INT_ARRAY`` encode to a Postgres array literal (``{"a","b"}``) and
decode either a driver ``list`` (Postgres) or that literal back from text (SQLite test DBs), so
the same table definition works on every ``DB_TYPE``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from pydal.helpers.classes import SQLCustomType


def _quote(item: Any) -> str:
    """One array element as a double-quoted, escaped Postgres literal member."""
    text = str(item)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _encode(value: Iterable[Any] | None) -> str | None:
    """Python sequence -> Postgres array literal (``None`` stays NULL)."""
    if value is None:
        return None
    if isinstance(value, str):
        raise TypeError("array column expects a sequence, not a bare string")
    return "{" + ",".join(_quote(v) for v in value) + "}"


def _split_literal(text: str) -> list[str]:
    """Parse the members of a one-dimensional Postgres array literal."""
    body = text.strip()
    if not (body.startswith("{") and body.endswith("}")):
        raise ValueError("not a Postgres array literal")
    body = body[1:-1]
    items: list[str] = []
    cur: list[str] = []
    in_quotes = False
    i = 0
    had_any = False
    while i < len(body):
        ch = body[i]
        if in_quotes:
            if ch == "\\" and i + 1 < len(body):
                i += 1
                cur.append(body[i])
            elif ch == '"':
                in_quotes = False
            else:
                cur.append(ch)
        elif ch == '"':
            in_quotes = True
            had_any = True
        elif ch == ",":
            items.append("".join(cur))
            cur = []
            had_any = False
        else:
            cur.append(ch)
            had_any = True
        i += 1
    if cur or had_any or items:
        items.append("".join(cur))
    return items


def _decoder(cast: Callable[[str], Any]) -> Callable[[Any], list[Any] | None]:
    """Build a decoder accepting a driver ``list`` or an array-literal string."""

    def decode(value: Any) -> list[Any] | None:
        if value is None:
            return None
        if isinstance(value, list | tuple):
            return [cast(v) if isinstance(v, str) else v for v in value]
        return [cast(v) for v in _split_literal(str(value))]

    return decode


PG_TEXT_ARRAY = SQLCustomType(type="string", native="text", encoder=_encode, decoder=_decoder(str))
PG_INT_ARRAY = SQLCustomType(type="string", native="text", encoder=_encode, decoder=_decoder(int))
