"""Runs the Art. 17 erasure registry (`erasure_registry.py`) inside the erasure transaction.

Synchronous on purpose, like `data_privacy_service._sync_erase()` which calls it: every statement
must land on the one pool-thread connection so the whole erasure commits or rolls back together.
It issues DELETE / UPDATE only -- never a commit; `_sync_erase()` owns the transaction.

Uses the pydal query builder throughout (never raw SQL with driver placeholders): the unit suites
run against sqlite and production against Postgres. The only raw statements are the two
parameterless catalog reads in `table_catalog()`.

**Tables are bound lazily as aliases** (`erasure__<table>` with `rname=<table>`) holding just the
columns the registry touches, so the registry can cover ~100 tables without a pydal binder for
each, and never collides with a table another module already defined with a fuller column set.
A table (or column) absent from the live database is skipped and counted -- a deployment that
never ran a migration has no rows to erase there -- but never silently: it is logged and reported
in `RegistryResult`.
"""

from __future__ import annotations

import logging
import threading
import uuid as uuidlib
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any

from pydal import Field

from services import erasure_registry as registry
from services.erasure_registry import Action, Key, Match, Rule

logger = logging.getLogger(__name__)

_ALIAS_LOCK = threading.Lock()
_ALIAS_PREFIX = "erasure__"


@dataclass(slots=True, frozen=True)
class ErasureSubject:
    """Everything the registry needs to recognise the subject's rows, captured BEFORE any delete."""

    user_id: int
    identities: tuple[tuple[str, str], ...] = ()
    member_ids: tuple[int, ...] = ()
    user_uuids: tuple[str, ...] = ()


@dataclass(slots=True)
class RegistryResult:
    """What one registry pass did, including the denominators a bare 'clean' would hide."""

    counts: dict[str, int] = field(default_factory=dict)
    tables_examined: int = 0
    tables_absent: tuple[str, ...] = ()
    rules_skipped: tuple[str, ...] = ()


def scrubbed_key(table: str) -> str:
    """The `deletion_scope` key under which a SCRUB rule's row count is reported."""
    return f"{table}__scrubbed"


def table_catalog(dal: Any) -> dict[str, frozenset[str]]:
    """Map every base table in the live database to its column names (lowercase).

    One catalog read per erasure. Postgres reads `information_schema` for the current schema;
    sqlite (the unit-test dialect) walks `sqlite_master` + `PRAGMA table_info`.
    """
    catalog: dict[str, set[str]] = {}
    if getattr(dal, "_dbname", "") == "sqlite":
        for (name,) in dal.executesql("SELECT name FROM sqlite_master WHERE type = 'table'"):
            safe = str(name).replace('"', "")
            for row in dal.executesql(f'PRAGMA table_info("{safe}")'):
                catalog.setdefault(str(name).lower(), set()).add(str(row[1]).lower())
        return {k: frozenset(v) for k, v in catalog.items()}
    rows = dal.executesql(
        "SELECT c.table_name, c.column_name FROM information_schema.columns c "
        "JOIN information_schema.tables t "
        "ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
        "WHERE c.table_schema = current_schema() AND t.table_type = 'BASE TABLE'"
    )
    for table, column in rows:
        catalog.setdefault(str(table).lower(), set()).add(str(column).lower())
    return {k: frozenset(v) for k, v in catalog.items()}


def _field_type(key: Key | None) -> str:
    return "integer" if key in (Key.HUB_USER, Key.MEMBER) else "string"


def _column_types() -> dict[str, dict[str, str]]:
    """`table -> {column -> pydal type}` for every column the registry reads or writes."""
    types: dict[str, dict[str, str]] = {}

    def note(table: str, column: str, key: Key | None) -> None:
        if column == "id":
            return
        types.setdefault(table, {}).setdefault(column, _field_type(key))

    for rule in registry.executable_rules():
        for match in rule.matches:
            note(rule.table, match.column, match.key)
            if match.platform_column:
                note(rule.table, match.platform_column, None)
        for column in (*rule.set_null, *rule.set_placeholder):
            note(rule.table, column, None)
        for det in rule.detach:
            note(det.table, det.column, Key.HUB_USER)
    for source in registry.OBJECT_SOURCES:
        note(source.table, source.url_column, None)
        note(source.table, source.match.column, source.match.key)
    return types


_COLUMN_TYPES = _column_types()


def _alias(dal: Any, table: str) -> Any:
    """The lazily-defined pydal alias of `table` carrying only the registry's columns."""
    name = _ALIAS_PREFIX + table
    if name not in dal.tables:
        with _ALIAS_LOCK:
            if name not in dal.tables:
                fields = [Field(col, ftype) for col, ftype in _COLUMN_TYPES.get(table, {}).items()]
                rname = dal._adapter.dialect.quote(table)
                dal.define_table(name, *fields, rname=rname, migrate=False)
    return dal[name]


def _uuid_tokens(values: Collection[str]) -> list[str]:
    """The well-formed uuids among `values`, canonicalised.

    A uuid-typed column rejects any other literal, and that error would abort the whole erasure
    transaction -- so a malformed token is dropped (and logged) instead of queried.
    """
    tokens: list[str] = []
    for value in values:
        try:
            token = str(uuidlib.UUID(str(value)))
        except ValueError:
            logger.warning("dsar.erasure_uuid_token_skipped")
            continue
        if token not in tokens:
            tokens.append(token)
    return tokens


def _match_query(dal: Any, alias: Any, match: Match, subject: ErasureSubject) -> Any | None:
    """The pydal query selecting rows where `match` holds for `subject`; None if it cannot."""
    column = alias[match.column]
    if match.key is Key.HUB_USER:
        return column == subject.user_id
    if match.key is Key.USER_UUID:
        tokens = _uuid_tokens(subject.user_uuids)
        return column.belongs(tokens) if tokens else None
    if match.key is Key.MEMBER:
        return column.belongs(list(subject.member_ids)) if subject.member_ids else None
    query: Any | None = None
    platform_column = alias[match.platform_column]
    for platform_name, platform_user_id in subject.identities:
        pair = (platform_column == platform_name) & (column == platform_user_id)
        query = pair if query is None else (query | pair)
    return query


def _any_match(dal: Any, alias: Any, matches: Collection[Match], subject: ErasureSubject) -> Any:
    query: Any | None = None
    for match in matches:
        one = _match_query(dal, alias, match, subject)
        if one is not None:
            query = one if query is None else (query | one)
    return query


def _detach(dal: Any, rule: Rule, query: Any, alias: Any) -> None:
    """NULL FK-`NO ACTION` self/child references to rows the rule is about to delete."""
    for det in rule.detach:
        ids = [row.id for row in dal(query).select(alias.id)]
        if not ids:
            continue
        target = _alias(dal, det.table)
        dal(target[det.column].belongs(ids)).update(**{det.column: None})


def run_registry_erasure(dal: Any, subject: ErasureSubject) -> RegistryResult:
    """Execute every DELETE/SCRUB rule for `subject`; no commit (the caller owns the transaction).

    Counts are keyed by table for deletes and by `<table>__scrubbed` for scrubs, and only non-zero
    counts are reported (a ~100-table ledger of zeros would bury the signal).
    """
    catalog = table_catalog(dal)
    result = RegistryResult()
    examined: set[str] = set()
    absent: set[str] = set()
    skipped: list[str] = []
    for rule in registry.executable_rules():
        present = catalog.get(rule.table)
        if present is None:
            absent.add(rule.table)
            continue
        examined.add(rule.table)
        needed = set(registry.columns_in_rule(rule))
        if rule.detach:
            needed.add("id")
        missing = sorted(needed - present)
        if missing:
            skipped.append(f"{rule.table}({','.join(missing)})")
            logger.warning(
                "dsar.erasure_rule_skipped",
                extra={"table": rule.table, "missing_columns": missing, "action": rule.action},
            )
            continue
        alias = _alias(dal, rule.table)
        query = _any_match(dal, alias, rule.matches, subject)
        if query is None:
            continue
        if rule.action is Action.DELETE:
            _detach(dal, rule, query, alias)
            deleted = int(dal(query).delete() or 0)
            if deleted:
                result.counts[rule.table] = result.counts.get(rule.table, 0) + deleted
        else:
            values: dict[str, Any] = {col: None for col in rule.set_null}
            values.update({col: registry.ERASED_PLACEHOLDER for col in rule.set_placeholder})
            changed = int(dal(query).update(**values) or 0)
            if changed:
                key = scrubbed_key(rule.table)
                result.counts[key] = result.counts.get(key, 0) + changed
    result.tables_examined = len(examined)
    result.tables_absent = tuple(sorted(absent))
    result.rules_skipped = tuple(skipped)
    logger.debug(
        "dsar.erasure_registry_done",
        extra={
            "tables_examined": result.tables_examined,
            "tables_absent": len(result.tables_absent),
            "rules_skipped": len(result.rules_skipped),
            "rows_touched": sum(result.counts.values()),
        },
    )
    return result


def collect_object_urls(dal: Any, subject: ErasureSubject) -> tuple[str, ...]:
    """Every uploaded-object URL held by the subject's rows (read before those rows are erased)."""
    catalog = table_catalog(dal)
    urls: set[str] = set()
    for source in registry.OBJECT_SOURCES:
        present = catalog.get(source.table)
        needed = {source.url_column, source.match.column}
        if present is None or not needed <= (present | {"id"}):
            continue
        alias = _alias(dal, source.table)
        query = _match_query(dal, alias, source.match, subject)
        if query is None:
            continue
        for row in dal(query).select(alias[source.url_column]):
            value = row[source.url_column]
            if value:
                urls.add(str(value))
    return tuple(sorted(urls))
