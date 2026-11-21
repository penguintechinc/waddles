"""Test helper: an entity row's public UUID (the only id its URLs accept after #1512)."""

from __future__ import annotations

from typing import Any


def pid(dal: Any, table: str, row_id: int) -> str:
    """The `public_id` UUID of `table` row `row_id` (pydal DAL; asserts the row has one)."""
    row = dal(dal[table].id == row_id).select().first()
    assert row is not None and row.public_id, f"{table}#{row_id} has no public_id"
    return str(row.public_id)
