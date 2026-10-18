"""0062_encrypt_vendor_payment: plaintext rows -> envelope, idempotent, fail-closed, reversible."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

_PATH = (
    Path(__file__).resolve().parents[1] / "versions" / "0062_encrypt_vendor_payment.py"
)
_spec = importlib.util.spec_from_file_location("rev0062", _PATH)
assert _spec and _spec.loader
rev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rev)


@pytest.fixture
def table(monkeypatch: Any) -> dict[int, Any]:
    """In-memory stand-in for vendor_submissions (id -> payment_details)."""
    data: dict[int, Any] = {
        1: {"paypal_email": "a@example.com", "bank_account": "111"},
        2: {},
        3: None,
    }
    monkeypatch.setattr(rev.op, "get_bind", lambda: object())
    monkeypatch.setattr(rev, "_table_exists", lambda c: True)
    monkeypatch.setattr(
        rev, "_rows", lambda c: [(i, v) for i, v in data.items() if v is not None]
    )
    monkeypatch.setattr(
        rev, "_update", lambda c, i, v: data.__setitem__(i, json.loads(json.dumps(v)))
    )
    return data


def test_upgrade_encrypts_then_downgrade_restores(
    table: dict[int, Any], monkeypatch: Any
) -> None:
    monkeypatch.setenv("MARKETPLACE_ENCRYPTION_KEY", "e" * 64)
    original = dict(table[1])
    rev.upgrade()
    assert rev._is_envelope(table[1])
    assert "a@example.com" not in json.dumps(table[1])
    assert table[2] == {} and table[3] is None
    snapshot = json.dumps(table[1])
    rev.upgrade()  # idempotent: no re-encryption
    assert json.dumps(table[1]) == snapshot
    rev.downgrade()
    assert table[1] == original


def test_upgrade_without_key_raises_and_leaves_data(
    table: dict[int, Any], monkeypatch: Any
) -> None:
    monkeypatch.delenv("MARKETPLACE_ENCRYPTION_KEY", raising=False)
    with pytest.raises(RuntimeError):
        rev.upgrade()
    assert table[1]["paypal_email"] == "a@example.com"


def test_revision_chain() -> None:
    assert rev.down_revision == "0061_port_legacy_sql_103_codec"
    assert len(rev.revision) <= 32
