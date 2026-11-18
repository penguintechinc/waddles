"""Erasure writes `(user_uuid, ERASED)` into `data_use_exclusions` (fail-closed suppression list).

Regression: the table was RETAIN-only, so nothing populated it on erasure and a late-arriving
analytics event could re-create layer A rows for an erased subject. Real pydal + sqlite.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from flask_core.database import AsyncDAL

from services import erasure_executor as executor
from services import erasure_registry as registry
from services.erasure_executor import ErasureSubject

U1 = "11111111-1111-1111-1111-111111111111"
U2 = "22222222-2222-2222-2222-222222222222"
BYSTANDER = "33333333-3333-3333-3333-333333333333"


@pytest.fixture
def dal(tmp_path: Path) -> Any:
    """A sqlite DAL holding only the suppression table."""
    adal = AsyncDAL(f"sqlite://{tmp_path / 'x.db'}", pool_size=1)
    adal.dal.executesql(
        "CREATE TABLE data_use_exclusions (user_uuid TEXT NOT NULL, reason_code INTEGER NOT NULL, "
        "since TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (user_uuid, reason_code))"
    )
    adal.dal.commit()
    return adal.dal


def _rows(dal: Any) -> list[tuple[str, int]]:
    rows = dal.executesql("SELECT user_uuid, reason_code FROM data_use_exclusions")
    return sorted((str(u), int(r)) for u, r in rows)


def test_registry_constants_match_the_0129_enum() -> None:
    assert registry.EXCLUSION_REASON_ERASED == 4
    assert registry.EXCLUSIONS_TABLE == "data_use_exclusions"


def test_exclusion_row_exists_for_every_token_after_erasure(dal: Any) -> None:
    executor.run_registry_erasure(dal, ErasureSubject(user_id=5, user_uuids=(U1, U2)))
    dal.commit()
    assert _rows(dal) == [(U1, 4), (U2, 4)]


def test_erasing_twice_is_idempotent_and_leaves_other_rows(dal: Any) -> None:
    dal.executesql(
        f"INSERT INTO data_use_exclusions (user_uuid, reason_code) VALUES ('{BYSTANDER}', 1)"  # noqa: S608
    )
    subject = ErasureSubject(user_id=5, user_uuids=(U1,))
    executor.run_registry_erasure(dal, subject)
    executor.run_registry_erasure(dal, subject)
    dal.commit()
    assert _rows(dal) == [(U1, 4), (BYSTANDER, 1)]


def test_only_the_uuid_token_is_stored_no_pii(dal: Any) -> None:
    subject = ErasureSubject(
        user_id=5, identities=(("discord", "handle-sentinel"),), user_uuids=(U1,)
    )
    executor.run_registry_erasure(dal, subject)
    dal.commit()
    dump = repr(dal.executesql("SELECT * FROM data_use_exclusions"))
    assert "handle-sentinel" not in dump and "discord" not in dump
    cols = [r[1] for r in dal.executesql("PRAGMA table_info(data_use_exclusions)")]
    assert cols == ["user_uuid", "reason_code", "since"]


def test_malformed_tokens_are_dropped_and_no_tokens_inserts_nothing(dal: Any) -> None:
    bad = ("'; DROP TABLE data_use_exclusions; --", "bad")
    executor.run_registry_erasure(dal, ErasureSubject(user_id=5, user_uuids=bad))
    executor.run_registry_erasure(dal, ErasureSubject(user_id=6))
    dal.commit()
    assert _rows(dal) == []


def test_absent_table_is_skipped_not_raised(tmp_path: Path) -> None:
    adal = AsyncDAL(f"sqlite://{tmp_path / 'y.db'}", pool_size=1)
    catalog = executor.table_catalog(adal.dal)
    subject = ErasureSubject(user_id=1, user_uuids=(U1,))
    assert executor.record_erasure_exclusions(adal.dal, subject, catalog) == 0
