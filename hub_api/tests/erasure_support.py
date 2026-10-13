"""Shared fixtures for the Art. 17 registry tests: a full-registry sqlite schema and test doubles.

`full_registry_ddl()` renders a `CREATE TABLE` for every table the registry executes against (and
every object-source table), with exactly the columns the registry reads/writes, generated FROM the
registry -- so a rule added tomorrow is exercised by the behaviour suite with no test edit.
`FakeExternal` / `FakeRedis` / `FakeS3` stand in for the three network stores; the real-Valkey
test lives in `test_erasure_external.py` and is skipped when no server is configured.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from services import erasure_executor


def _ddl_type(pydal_type: str) -> str:
    return "INTEGER" if pydal_type == "integer" else "TEXT"


def full_registry_ddl() -> dict[str, list[tuple[str, str]]]:
    """`table -> [(column, sqlite type)]` for every table the executor or object sources touch."""
    out: dict[str, list[tuple[str, str]]] = {}
    for table, columns in erasure_executor._COLUMN_TYPES.items():
        out[table] = [(col, _ddl_type(ftype)) for col, ftype in sorted(columns.items())]
    return out


def create_missing_registry_tables(dal: Any) -> None:
    """Create each registry table that is absent, and ADD any column a present one lacks."""
    catalog = erasure_executor.table_catalog(dal)
    for table, columns in full_registry_ddl().items():
        present = catalog.get(table)
        if present is None:
            body = ", ".join(f'"{c}" {t}' for c, t in columns)
            dal.executesql(
                f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY AUTOINCREMENT'
                + (f", {body}" if body else "")
                + ")"
            )
            continue
        for column, ctype in columns:
            if column not in present:
                dal.executesql(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {ctype}')
    dal.commit()


@dataclass(slots=True)
class FakeExternal:
    """Records what erasure asked the object store / KV to delete; can be told to fail."""

    fail_with: Exception | None = None
    calls: list[tuple[tuple[str, ...], tuple[str, ...]]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def erase(self, *, object_urls: Sequence[str], user_uuids: Sequence[str]) -> dict[str, int]:
        """Record the call; raise if configured to."""
        self.calls.append((tuple(object_urls), tuple(user_uuids)))
        if self.fail_with is not None:
            raise self.fail_with
        return dict(self.counts)


class FakeRedis:
    """The slice of redis-py `erase_kv_keys` uses, backed by a dict with real glob matching."""

    def __init__(self, data: dict[str, int | str] | None = None) -> None:
        """Start with `data` (key -> value)."""
        self.data: dict[str, int | str] = dict(data or {})
        self.unlinked: list[str] = []

    def scan_iter(self, match: str, count: int = 500) -> Iterable[bytes]:
        """Yield matching keys as bytes (what redis-py returns without `decode_responses`)."""
        return [k.encode() for k in sorted(self.data) if fnmatch.fnmatchcase(k, match)]

    def unlink(self, key: str) -> int:
        """Delete one key."""
        self.unlinked.append(key)
        return 1 if self.data.pop(key, None) is not None else 0

    def exists(self, key: str) -> int:
        """Does the key exist?"""
        return int(key in self.data)

    def decrby(self, key: str, amount: int) -> int:
        """Lower an integer key."""
        self.data[key] = int(self.data[key]) - amount
        return int(self.data[key])

    def delete(self, key: str) -> int:
        """Delete one key."""
        return 1 if self.data.pop(key, None) is not None else 0

    def close(self) -> None:
        """No connection to close."""


class FakeS3:
    """The slice of boto3's S3 client `delete_owned_objects_strict` uses."""

    def __init__(self, fail_with: Exception | None = None) -> None:
        """Optionally raise `fail_with` from every delete."""
        self.deleted: list[tuple[str, str]] = []
        self.fail_with = fail_with

    def delete_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803 - boto3 API
        """Record the delete (or raise)."""
        if self.fail_with is not None:
            raise self.fail_with
        self.deleted.append((Bucket, Key))
        return {}
