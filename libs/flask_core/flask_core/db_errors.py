"""Redaction-safe description of database driver errors.

A DB driver exception's message routinely embeds the *bound values* of the
statement that failed -- psycopg2's ``DETAIL: Key (email)=(a@b.c) already
exists.``, ``invalid input syntax for type uuid: "<value>"``, pydal's inlined
INSERT text, SQLAlchemy's ``[parameters: (...)]`` suffix. Logging ``{e}`` therefore
writes PII, tokens and handles straight into the log stream.

This module is the single place flask_core turns a driver error into log text.
It is allowlist-based and fails closed: only the exception *type*, the
SQLSTATE / driver error code, a fixed human label looked up from that code,
and schema identifiers (constraint / table / column, regex-validated) are ever
emitted. The exception message, ``args``, SQL text and bound parameters are
never read into the output. A frame-only traceback (file, line, function,
source line -- no exception text) is available for DEBUG.
"""

from __future__ import annotations

import logging
import re
import traceback
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

_DRIVER_MODULE_PREFIXES: Final[tuple[str, ...]] = (
    "psycopg2",
    "psycopg",  # psycopg 3
    "asyncpg",
    "pymysql",
    "MySQLdb",
    "mysql",  # mysql.connector
    "sqlite3",
    "sqlalchemy.exc",
    "penguin_dal",
)
_MYSQL_MODULE_PREFIXES: Final[tuple[str, ...]] = ("pymysql", "MySQLdb", "mysql")

_SQLSTATE_RE: Final = re.compile(r"[0-9A-Z]{5}")
_SQLITE_NAME_RE: Final = re.compile(r"SQLITE_[A-Z0-9_]{1,40}")
_IDENTIFIER_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_$]{0,62}")

_MAX_CHAIN: Final = 16

# Fixed labels -- the only "summary text" that ever reaches a log line. Looked
# up by SQLSTATE so no part of the driver message can leak through.
_SQLSTATE_NAMES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "08001": "unable_to_establish_connection",
        "08003": "connection_does_not_exist",
        "08006": "connection_failure",
        "0A000": "feature_not_supported",
        "22001": "string_data_right_truncation",
        "22003": "numeric_value_out_of_range",
        "22007": "invalid_datetime_format",
        "22008": "datetime_field_overflow",
        "22012": "division_by_zero",
        "22023": "invalid_parameter_value",
        "22P02": "invalid_text_representation",
        "23502": "not_null_violation",
        "23503": "foreign_key_violation",
        "23505": "unique_violation",
        "23514": "check_violation",
        "23P01": "exclusion_violation",
        "25P02": "in_failed_sql_transaction",
        "28000": "invalid_authorization_specification",
        "28P01": "invalid_password",
        "3D000": "invalid_catalog_name",
        "40001": "serialization_failure",
        "40P01": "deadlock_detected",
        "42501": "insufficient_privilege",
        "42601": "syntax_error",
        "42703": "undefined_column",
        "42804": "datatype_mismatch",
        "42883": "undefined_function",
        "42P01": "undefined_table",
        "42P07": "duplicate_table",
        "53300": "too_many_connections",
        "55P03": "lock_not_available",
        "57014": "query_canceled",
        "57P01": "admin_shutdown",
    }
)
_SQLSTATE_CLASSES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "08": "connection_exception",
        "0A": "feature_not_supported",
        "22": "data_exception",
        "23": "integrity_constraint_violation",
        "25": "invalid_transaction_state",
        "28": "invalid_authorization",
        "3D": "invalid_catalog_name",
        "40": "transaction_rollback",
        "42": "syntax_error_or_access_rule_violation",
        "53": "insufficient_resources",
        "54": "program_limit_exceeded",
        "55": "object_not_in_prerequisite_state",
        "57": "operator_intervention",
        "58": "system_error",
        "XX": "internal_error",
    }
)


@dataclass(slots=True, frozen=True)
class DbErrorSummary:
    """Value-free summary of a DB failure -- every field is safe to log.

    Built by `summarize_db_error`; `render()` produces the single log string.
    """

    exc_type: str
    driver_type: str | None = None
    sqlstate: str | None = None
    driver_code: str | None = None
    category: str | None = None
    constraint: str | None = None
    table: str | None = None
    column: str | None = None

    def render(self) -> str:
        """Render as ``type=... sqlstate=... category=...`` (no message text)."""
        parts = [f"type={self.exc_type}"]
        if self.driver_type and self.driver_type != self.exc_type:
            parts.append(f"driver_type={self.driver_type}")
        for key, value in (
            ("sqlstate", self.sqlstate),
            ("code", self.driver_code),
            ("category", self.category),
            ("constraint", self.constraint),
            ("table", self.table),
            ("column", self.column),
        ):
            if value:
                parts.append(f"{key}={value}")
        return " ".join(parts)


def _type_name(exc: BaseException) -> str:
    """Return the dotted exception class name (class names never carry data)."""
    cls = type(exc)
    if cls.__module__ == "builtins":
        return cls.__qualname__
    return f"{cls.__module__}.{cls.__qualname__}"


def _module_matches(module: str, prefixes: tuple[str, ...]) -> bool:
    """True if `module` is, or sits under, any of `prefixes`."""
    return any(module == p or module.startswith(f"{p}.") for p in prefixes)


def _safe_getattr(obj: object, name: str) -> object:
    """`getattr` that swallows any exception a driver property might raise."""
    try:
        return getattr(obj, name, None)
    except Exception:  # noqa: BLE001 -- a hostile/odd driver attribute must never break logging
        return None


def _iter_chain(exc: BaseException) -> Iterator[BaseException]:
    """Yield `exc` then its causes/contexts/wrapped originals, cycle-safe and bounded."""
    seen: set[int] = set()
    queue: list[BaseException] = [exc]
    while queue and len(seen) < _MAX_CHAIN:
        current = queue.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        for nxt in (current.__cause__, current.__context__, _safe_getattr(current, "orig")):
            if isinstance(nxt, BaseException):
                queue.append(nxt)
        if isinstance(current, BaseExceptionGroup):
            queue.extend(current.exceptions)


def _sqlstate_of(exc: BaseException) -> str | None:
    """Extract a well-formed SQLSTATE from `exc` (psycopg2/psycopg/asyncpg), else None."""
    for holder in (exc, _safe_getattr(exc, "diag")):
        if holder is None:
            continue
        for attr in ("pgcode", "sqlstate"):
            value = _safe_getattr(holder, attr)
            if isinstance(value, str) and _SQLSTATE_RE.fullmatch(value):
                return value
    return None


def _is_driver_exception(exc: BaseException) -> bool:
    """True if `exc` is a DB driver / DB-layer error (by class module or SQLSTATE)."""
    if any(_module_matches(cls.__module__, _DRIVER_MODULE_PREFIXES) for cls in type(exc).__mro__):
        return True
    return _sqlstate_of(exc) is not None


def _driver_code_of(exc: BaseException) -> str | None:
    """Return a non-message driver error code (sqlite error name / MySQL errno), else None."""
    name = _safe_getattr(exc, "sqlite_errorname")
    if isinstance(name, str) and _SQLITE_NAME_RE.fullmatch(name):
        return name
    if _module_matches(type(exc).__module__, _MYSQL_MODULE_PREFIXES):
        args = _safe_getattr(exc, "args")
        if (
            isinstance(args, tuple)
            and args
            and isinstance(args[0], int)
            and not isinstance(args[0], bool)
        ):
            return f"mysql-{args[0]}"
    return None


def _identifier_of(exc: BaseException, name: str) -> str | None:
    """Return schema identifier `name` (constraint/table/column) if it is a plain identifier."""
    for holder in (_safe_getattr(exc, "diag"), exc):
        if holder is None:
            continue
        value = _safe_getattr(holder, name)
        if isinstance(value, str) and _IDENTIFIER_RE.fullmatch(value):
            return value
    return None


def _category_of(sqlstate: str) -> str | None:
    """Map a SQLSTATE to its fixed label (exact code first, then its 2-char class)."""
    return _SQLSTATE_NAMES.get(sqlstate) or _SQLSTATE_CLASSES.get(sqlstate[:2])


def is_db_driver_error(exc: BaseException) -> bool:
    """Return True if `exc`, or anything in its cause/context chain, is a DB driver error."""
    return any(_is_driver_exception(c) for c in _iter_chain(exc))


def summarize_db_error(exc: BaseException) -> DbErrorSummary:
    """Build a value-free `DbErrorSummary` for `exc` (never reads the message or args)."""
    chain = list(_iter_chain(exc))
    driver = next((c for c in chain if _is_driver_exception(c)), None)
    sqlstate = next((s for s in (_sqlstate_of(c) for c in chain) if s), None)
    category = _category_of(sqlstate) if sqlstate else None

    def first_identifier(name: str) -> str | None:
        return next((i for i in (_identifier_of(c, name) for c in chain) if i), None)

    return DbErrorSummary(
        exc_type=_type_name(exc),
        driver_type=_type_name(driver) if driver is not None else None,
        sqlstate=sqlstate,
        driver_code=_driver_code_of(driver) if driver is not None else None,
        category=category,
        constraint=first_identifier("constraint_name"),
        table=first_identifier("table_name"),
        column=first_identifier("column_name"),
    )


def describe_db_error(exc: BaseException) -> str:
    """Return the loggable, value-free one-line description of a DB failure."""
    return summarize_db_error(exc).render()


def format_sanitized_traceback(exc: BaseException) -> str:
    """Render `exc`'s traceback frames (file/line/function/source) without any exception text.

    Gives DEBUG logs the call path of a DB failure while guaranteeing the
    driver message -- and so any bound value in it -- stays out of the output.
    """
    lines: list[str] = []
    for item in _iter_chain(exc):
        lines.append("Traceback (most recent call last):")
        for frame in traceback.extract_tb(item.__traceback__):
            lines.append(f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}')
            if frame.line:
                lines.append(f"    {frame.line}")
        lines.append(describe_db_error(item))
    return "\n".join(lines)


def log_db_error(
    logger: logging.Logger,
    operation: str,
    exc: BaseException,
    *,
    level: int = logging.ERROR,
) -> None:
    """Log a DB failure as `operation: <type/sqlstate/category>`; sanitized traceback at DEBUG.

    The raw driver message is never logged -- see this module's docstring.
    `stacklevel=2` keeps the record attributed to the caller's file/line.
    """
    logger.log(level, "%s: %s", operation, describe_db_error(exc), stacklevel=2)
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "%s: sanitized traceback\n%s", operation, format_sanitized_traceback(exc), stacklevel=2
        )
