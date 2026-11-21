"""Test helpers for hub-user PUBLIC-UUID URLs (`<uuuid:user_id>`).

Client-facing user/member routes take `hub_users.uuid`, never the numeric id. Most existing tests
seed users by numeric id, so the autouse `deterministic_user_uuids` fixture (tests/conftest.py)
swaps the two DB lookups in `services.user_public_id` for a pure, reversible mapping:

    from tests.user_ids import uid

    url = f"/api/v1/platform/users/{uid(7)}/role"      # the UUID for numeric user 7
    assert body["user"]["userId"] == uid(7)

Tests that must prove the REAL `hub_users.uuid` resolution carry
`@pytest.mark.real_user_ids` and seed actual rows (see tests/test_user_public_id.py).
"""

from __future__ import annotations

import uuid
from typing import Any

_MARK = 0x5D1C1A2E7A53  # marker in bits 48-95; the low 48 bits carry the numeric id
_LOW_MASK = (1 << 48) - 1


def uid(user_id: int | str) -> str:
    """Deterministic public UUID for numeric user `user_id` (what the fake lookup resolves)."""
    return str(uuid.UUID(int=(_MARK << 48) | (int(user_id) & _LOW_MASK)))


def uid_to_int(public_id: str) -> int:
    """Inverse of `uid`; any UUID outside the `uid` range is unknown (-1)."""
    value = uuid.UUID(public_id).int
    if value >> 48 != _MARK:
        return -1
    return value & _LOW_MASK


def _dal(db: Any) -> Any:
    """The raw pydal DAL for either the async wrapper fixture or the DAL itself."""
    return getattr(db, "dal", db)


def row_uuid(db: Any, user_id: int) -> str:
    """The REAL `hub_users.uuid` stored for numeric `user_id` (response DTOs carry this)."""
    dal = _dal(db)
    row = dal(dal.hub_users.id == user_id).select(dal.hub_users.uuid).first()
    assert row is not None, f"no hub_users row {user_id}"
    return str(row.uuid)


def row_id(db: Any, public_id: str) -> int:
    """Numeric `hub_users.id` of the row whose `uuid` is `public_id`."""
    dal = _dal(db)
    row = dal(dal.hub_users.uuid == public_id).select(dal.hub_users.id).first()
    assert row is not None, f"no hub_users row with uuid {public_id}"
    return int(row.id)
