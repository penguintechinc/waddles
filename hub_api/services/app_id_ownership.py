"""Atomic app_id ownership claim and storage-key collision refusal (`app_id_owners`).

`app_id_owners(app_id PK, owner_id, storage_key UNIQUE, created_at)` (alembic 0136) is the single
arbiter of two invariants, both enforced by the database rather than by a read-then-write:

* **First claim wins.** A claim is one `INSERT ... ON CONFLICT DO NOTHING RETURNING`; of two
  concurrent claims on a fresh app_id exactly one row is inserted. A later claim by a different
  owner is refused (unless the caller is a platform admin, `enforce_owner=False`, which may still
  upload to any non-colliding id but never changes the recorded owner).
* **Distinct app_ids never share a storage key.** `storage_key` is UNIQUE, so a registration whose
  `bundle_storage_key(app_id)` already belongs to a DIFFERENT app_id is refused, never merged.
"""

from __future__ import annotations

import logging
from typing import Any

from services.bundle_app_id import bundle_storage_key
from services.bundle_install_dal import raw_sql_rows, raw_sql_write
from services.errors import conflict, forbidden

logger = logging.getLogger(__name__)

_CLAIM_SQL = (
    "INSERT INTO app_id_owners (app_id, owner_id, storage_key) "
    "VALUES (:app_id, :owner_id, :storage_key) "
    "ON CONFLICT DO NOTHING RETURNING app_id"
)
_LOOKUP_SQL = (
    "SELECT app_id, owner_id, storage_key FROM app_id_owners "
    "WHERE app_id = :app_id OR storage_key = :storage_key"
)


async def claim_app_id(
    dal: Any,
    *,
    app_id: str,
    owner_id: int | None,
    enforce_owner: bool = True,
) -> None:
    """Atomically claim `app_id` for `owner_id`; refuse a foreign owner or a key collision.

    Idempotent for the existing owner. Raises 403 when `app_id` is already owned by someone else
    and `enforce_owner` is true, and 409 when a different app_id already holds the same
    `bundle_storage_key`. `owner_id` is NULL for the system actor (seeded bundles).
    """
    key = bundle_storage_key(app_id)
    params = {"app_id": app_id, "owner_id": owner_id, "storage_key": key}
    claimed = await raw_sql_write(dal, _CLAIM_SQL, params)
    if len(claimed) > 0:
        return

    rows = await raw_sql_rows(dal, _LOOKUP_SQL, {"app_id": app_id, "storage_key": key})
    own = next((r for r in rows if r["app_id"] == app_id), None)
    if own is None:
        logger.warning(
            "bundle registration refused: storage key collides with another app_id",
            extra={"event_type": "AUTHZ", "action": "claim_app_id", "result": "COLLISION"},
        )
        raise conflict(
            "this app_id maps to a storage namespace already in use by a different app_id"
        )
    if enforce_owner and own["owner_id"] != owner_id:
        logger.warning(
            "bundle registration refused: app_id already owned by another publisher",
            extra={"event_type": "AUTHZ", "action": "claim_app_id", "result": "FORBIDDEN"},
        )
        raise forbidden("this app_id is already registered by another publisher")


async def owner_of(dal: Any, app_id: str) -> tuple[bool, int | None]:
    """`(has_owner_row, owner_id)` for `app_id`; `(False, None)` when never claimed."""
    rows = await raw_sql_rows(
        dal, "SELECT owner_id FROM app_id_owners WHERE app_id = :app_id", {"app_id": app_id}
    )
    for row in rows:
        return True, row["owner_id"]
    return False, None


__all__ = ["claim_app_id", "owner_of"]
