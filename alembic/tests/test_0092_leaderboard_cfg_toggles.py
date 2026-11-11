"""0092 adds the three leaderboard toggle columns the hub-api DAL selects (idempotent SQL)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

_PATH = (
    Path(__file__).resolve().parents[1]
    / "versions"
    / "0092_leaderboard_config_toggles.py"
)
_spec = importlib.util.spec_from_file_location("mig0092", _PATH)
assert _spec is not None and _spec.loader is not None
_mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mig)

_COLS = ("watch_time_enabled", "messages_enabled", "public_leaderboard")


class _Recorder:
    """Stand-in for `alembic.op` capturing executed SQL."""

    def __init__(self) -> None:
        self.sql: list[str] = []

    def execute(self, statement: Any) -> None:
        self.sql.append(str(statement))


def test_upgrade_adds_each_column_idempotently(monkeypatch: Any) -> None:
    rec = _Recorder()
    monkeypatch.setattr(_mig, "op", rec)
    _mig.upgrade()
    assert len(rec.sql) == len(_COLS)  # zero statements would prove nothing
    for col, sql in zip(_COLS, rec.sql, strict=True):
        assert "ADD COLUMN IF NOT EXISTS " + col in sql
        assert "DEFAULT TRUE" in sql
        assert "community_leaderboard_config" in sql


def test_downgrade_drops_each_column(monkeypatch: Any) -> None:
    rec = _Recorder()
    monkeypatch.setattr(_mig, "op", rec)
    _mig.downgrade()
    assert len(rec.sql) == len(_COLS)
    assert all("DROP COLUMN IF EXISTS" in s for s in rec.sql)


def test_chains_off_the_prior_head_and_id_fits() -> None:
    assert _mig.down_revision == "0091_marketplace_vendor_view"
    assert len(_mig.revision) <= 32
