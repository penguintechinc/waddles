"""Debit idempotency + amount bound for `services/token_billing_service.debit_tokens`.

A retried debit with the same key (scoped to community+product) charges once and returns the
ORIGINAL result; a different key charges again; concurrent same-key calls charge once; the
amount is bounded by `TOKEN_DEBIT_MAX_AMOUNT`. The real-Postgres cross-connection race lives in
`test_token_debit_idempotency_pg_integration.py`.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from services import token_billing_service as svc
from services.errors import ApiError
from services.token_billing_service import debit_tokens
from tests.conftest import seed_community, seed_token_balance, seed_token_product


def _setup(db: Any, balance: int = 10) -> tuple[int, int]:
    product_id = seed_token_product(db, key="ai_call")
    community_id = seed_community(db)
    seed_token_balance(db, community_id=community_id, product_id=product_id, balance=balance)
    return community_id, product_id


def _balance(db: Any, community_id: int, product_id: int) -> int:
    row = (
        db.dal(
            (db.dal.community_token_balances.community_id == community_id)
            & (db.dal.community_token_balances.product_id == product_id)
        )
        .select()
        .first()
    )
    return int(row.balance)


async def _debit(db: Any, community_id: int, amount: int = 3, key: str | None = None) -> Any:
    return await debit_tokens(
        db,
        db.dal,
        community_id=community_id,
        product_key="ai_call",
        amount=amount,
        reason="t",
        idempotency_key=key,
    )


async def test_replay_same_key_debits_once_and_returns_original(token_billing_db: Any) -> None:
    cid, pid = _setup(token_billing_db)
    first = await _debit(token_billing_db, cid, key="stream:1:adm-1")
    second = await _debit(token_billing_db, cid, key="stream:1:adm-1")
    assert first.ok and second.ok
    assert second.balance_after == first.balance_after == 7
    assert second.transaction_id == first.transaction_id
    assert _balance(token_billing_db, cid, pid) == 7
    dal = token_billing_db.dal
    assert dal(dal.token_transactions.community_id == cid).count() == 1


async def test_different_key_debits_again(token_billing_db: Any) -> None:
    cid, pid = _setup(token_billing_db)
    a = await _debit(token_billing_db, cid, key="k-1")
    b = await _debit(token_billing_db, cid, key="k-2")
    assert a.ok and b.ok
    assert a.transaction_id != b.transaction_id
    assert _balance(token_billing_db, cid, pid) == 4


async def test_unkeyed_debits_are_not_deduplicated(token_billing_db: Any) -> None:
    cid, pid = _setup(token_billing_db)
    await _debit(token_billing_db, cid)
    await _debit(token_billing_db, cid)
    assert _balance(token_billing_db, cid, pid) == 4


async def test_concurrent_same_key_debits_once(token_billing_db: Any) -> None:
    cid, pid = _setup(token_billing_db)
    results = await asyncio.gather(
        *[_debit(token_billing_db, cid, amount=2, key="same") for _ in range(8)]
    )
    assert all(r.ok for r in results)
    assert len({r.transaction_id for r in results}) == 1
    assert _balance(token_billing_db, cid, pid) == 8


async def test_same_key_other_community_is_independent(token_billing_db: Any) -> None:
    cid, pid = _setup(token_billing_db)
    other = seed_community(token_billing_db, name="other-comm")
    seed_token_balance(token_billing_db, community_id=other, product_id=pid, balance=10)
    for community in (cid, other):
        assert (await _debit(token_billing_db, community, amount=1, key="shared")).ok
    assert _balance(token_billing_db, cid, pid) == 9
    assert _balance(token_billing_db, other, pid) == 9


async def test_amount_above_maximum_rejected(
    token_billing_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid, pid = _setup(token_billing_db, balance=100)
    monkeypatch.setenv("TOKEN_DEBIT_MAX_AMOUNT", "5")
    with pytest.raises(ApiError):
        await _debit(token_billing_db, cid, amount=6)
    assert (await _debit(token_billing_db, cid, amount=5)).ok
    assert _balance(token_billing_db, cid, pid) == 95


def test_maximum_defaults_and_bad_env_fall_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TOKEN_DEBIT_MAX_AMOUNT", raising=False)
    assert svc.max_debit_amount() == svc.DEFAULT_MAX_DEBIT_AMOUNT
    for bad in ("nope", "0", "-3"):
        monkeypatch.setenv("TOKEN_DEBIT_MAX_AMOUNT", bad)
        assert svc.max_debit_amount() == svc.DEFAULT_MAX_DEBIT_AMOUNT


async def test_overlong_key_rejected(token_billing_db: Any) -> None:
    cid, _ = _setup(token_billing_db)
    with pytest.raises(ApiError):
        await _debit(token_billing_db, cid, amount=1, key="x" * 200)
