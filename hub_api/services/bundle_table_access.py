"""Platform-table access allowlist + `table_access` -> permission-id mapping.

Spec `docs/superpowers/specs/2026-11-06-bundle-table-access-grants.md`. A bundle
manifest may request read/write access to a SMALL, explicit set of platform
(non-bundle-owned) table columns. Every `(op, table, column)` triple is one
`dangerous` permission id, `platform.table.<op>:<table>.<column>`, so it rides
the existing approval/consent/re-consent/revocation machinery unchanged.

Leaf module (no service imports) so `bundle_permission_catalog` can use it
without an import cycle. The allowlist is mirrored byte-for-byte in
`core/bundle_capability_gate/src/platform_table.rs`; both are parity-tested
against `core/bundle_capability_gate/tests/fixtures/platform_table_allowlist.json`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: table -> column -> permitted ops. Deliberately narrow; never an auth/credential/
#: PII identity table or PII column (see the spec's "never allowlisted" list).
PLATFORM_TABLE_ALLOWLIST: dict[str, dict[str, frozenset[str]]] = {
    "community_members": {
        "role": frozenset({"read", "write"}),
        "is_active": frozenset({"read"}),
        "joined_at": frozenset({"read"}),
    },
}

TABLE_ACCESS_OPS = ("read", "write")
TABLE_ACCESS_PREFIX = "platform.table."
MAX_REASON_LEN = 280

_ID_RE = re.compile(
    r"platform\.table\.(?P<op>read|write):(?P<table>[a-z][a-z0-9_]{0,62})\.(?P<column>[a-z][a-z0-9_]{0,62})"
)


class TableAccessError(ValueError):
    """A `table_access` entry or id failed validation; `reason` is a stable machine code."""

    def __init__(self, reason: str, detail: str) -> None:
        """Keep the machine `reason` code and human `detail`."""
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(slots=True, frozen=True)
class TableAccessRequest:
    """One validated manifest `table_access` entry (consent-screen shape)."""

    table: str
    columns: tuple[str, ...]
    ops: tuple[str, ...]
    reason: str


def is_allowed(op: str, table: str, column: str) -> bool:
    """`True` iff `(op, table, column)` is on the allowlist."""
    return op in PLATFORM_TABLE_ALLOWLIST.get(table, {}).get(column, frozenset())


def table_access_permission_id(op: str, table: str, column: str) -> str:
    """The canonical permission id for one `(op, table, column)`."""
    return f"{TABLE_ACCESS_PREFIX}{op}:{table}.{column}"


def parse_table_access_id(permission_id: str) -> tuple[str, str, str] | None:
    """`(op, table, column)` for a well-formed, ALLOWLISTED table-access id, else `None`."""
    match = _ID_RE.fullmatch(permission_id)
    if match is None:
        return None
    op, table, column = match.group("op", "table", "column")
    return (op, table, column) if is_allowed(op, table, column) else None


def is_table_access_id(permission_id: str) -> bool:
    """`True` for ANY id in the `platform.table.` family (allowlisted or not).

    Used by the core-seeder carve-out: the whole family is never auto-granted.
    """
    return permission_id.startswith(TABLE_ACCESS_PREFIX)


def parse_table_access(raw_entries: object) -> tuple[TableAccessRequest, ...]:
    """Validate the manifest `table_access:` list; `TableAccessError` on any violation."""
    if raw_entries is None:
        return ()
    if not isinstance(raw_entries, list):
        raise TableAccessError("invalid_table_access", "table_access must be a list")
    out: list[TableAccessRequest] = []
    seen: set[str] = set()
    for entry in raw_entries:
        if not isinstance(entry, dict):
            raise TableAccessError("invalid_table_access", "each entry must be a mapping")
        table = entry.get("table")
        columns = entry.get("columns")
        ops = entry.get("ops")
        reason = entry.get("reason")
        if not isinstance(table, str) or table not in PLATFORM_TABLE_ALLOWLIST:
            raise TableAccessError("invalid_table_access", f"table {table!r} is not allowlisted")
        if table in seen:
            raise TableAccessError("invalid_table_access", f"duplicate entry for {table!r}")
        seen.add(table)
        if (
            not isinstance(columns, list)
            or not columns
            or not all(isinstance(c, str) for c in columns)
        ):
            raise TableAccessError("invalid_table_access", f"{table}: columns must be non-empty")
        if not isinstance(ops, list) or not ops or not set(ops) <= set(TABLE_ACCESS_OPS):
            raise TableAccessError("invalid_table_access", f"{table}: ops must be read/write")
        if not isinstance(reason, str) or not (0 < len(reason.strip()) <= MAX_REASON_LEN):
            raise TableAccessError(
                "missing_justification", f"{table}: reason is mandatory (1-{MAX_REASON_LEN} chars)"
            )
        for column in columns:
            for op in ops:
                if not is_allowed(op, table, column):
                    raise TableAccessError(
                        "invalid_table_access", f"{op} on {table}.{column} is not allowlisted"
                    )
        out.append(
            TableAccessRequest(
                table=table,
                columns=tuple(sorted(set(columns))),
                ops=tuple(sorted(set(ops))),
                reason=reason.strip(),
            )
        )
    return tuple(out)


def table_access_permission_ids(requests: tuple[TableAccessRequest, ...]) -> tuple[str, ...]:
    """Every permission id a validated `table_access` block expands to, sorted, unique."""
    return tuple(
        sorted(
            {
                table_access_permission_id(op, r.table, column)
                for r in requests
                for column in r.columns
                for op in r.ops
            }
        )
    )
