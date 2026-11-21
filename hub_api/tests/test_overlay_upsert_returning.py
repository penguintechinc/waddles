"""regression: browser-sources/overlay 500 -- pydal's `RETURNING "id"` preceded `ON CONFLICT`."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services import overlay_service


class _Table:
    def _insert(self, **_: Any) -> str:
        return 'INSERT INTO "overlay_view_credentials"("community_id") VALUES (1)RETURNING "id";'


class _Dal:
    def __init__(self) -> None:
        self.overlay_view_credentials = _Table()
        self.sql: list[str] = []

    def executesql(self, sql: str) -> None:
        self.sql.append(sql)


@pytest.mark.parametrize("overwrite", [True, False])
def test_upsert_has_no_returning_before_on_conflict(overwrite: bool) -> None:
    dal = _Dal()
    overlay_service._upsert_credentials(
        dal,
        community_id=1,
        key_hash="h",
        previous_key_hash=None,
        is_active=True,
        rotated_at=None,
        now=datetime.now(UTC),
        overwrite=overwrite,
    )
    (sql,) = dal.sql
    assert "RETURNING" not in sql
    assert ') ON CONFLICT ("community_id")' in sql or ")ON CONFLICT" in sql or " ON CONFLICT" in sql
    assert sql.startswith('INSERT INTO "overlay_view_credentials"')
