"""Read an entity row's `public_id` for a response DTO, failing loud when it is missing."""

from __future__ import annotations

from typing import Any


def row_public_id(row: Any) -> str:
    """The row's `public_id` as UUID text; raises (PII-free) instead of falling back to `row.id`.

    Response DTOs carry the public UUID only. A row without one is a data defect (the column is
    NOT NULL after alembic `0138_entity_public_ids`), never a reason to leak the numeric PK.
    """
    value = getattr(row, "public_id", None)
    if not value:
        raise RuntimeError(f"{getattr(row, '_tablename', 'row')} row is missing public_id")
    return str(value)
