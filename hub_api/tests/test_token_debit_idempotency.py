"""Debit idempotency + amount bound for `services/token_billing_service.debit_tokens`.

A retried debit with the same key (scoped to community+product) charges once and returns the
ORIGINAL result; a different key charges again; concurrent same-key calls charge once; the
amount is bounded by `TOKEN_DEBIT_MAX_AMOUNT`. The real-Postgres cross-connection race lives in
`test_token_debit_idempotency_pg_integration.py`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
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


async def _debit(
    db: Any,
    community_id: int,
    amount: int = 3,
    key: str | None = None,
    actor: int | None = None,
) -> Any:
    return await debit_tokens(
        db,
        db.dal,
        community_id=community_id,
        product_key="ai_call",
        amount=amount,
        reason="t",
        idempotency_key=key,
        actor_user_id=actor,
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


async def test_same_key_different_actor_is_rejected_not_replayed(token_billing_db: Any) -> None:
    cid, pid = _setup(token_billing_db)
    first = await _debit(token_billing_db, cid, key="adm", actor=1)
    other = await _debit(token_billing_db, cid, key="adm", actor=2)
    assert first.ok
    assert not other.ok
    assert other.blocked_reason == svc.REASON_IDEMPOTENCY_CONFLICT
    assert _balance(token_billing_db, cid, pid) == 7


async def test_same_key_different_amount_is_rejected(token_billing_db: Any) -> None:
    cid, pid = _setup(token_billing_db)
    assert (await _debit(token_billing_db, cid, amount=3, key="adm", actor=1)).ok
    changed = await _debit(token_billing_db, cid, amount=5, key="adm", actor=1)
    assert changed.blocked_reason == svc.REASON_IDEMPOTENCY_CONFLICT
    assert _balance(token_billing_db, cid, pid) == 7


async def test_retry_within_ttl_replays_exactly_once(token_billing_db: Any) -> None:
    cid, pid = _setup(token_billing_db)
    first = await _debit(token_billing_db, cid, key="adm", actor=1)
    retry = await _debit(token_billing_db, cid, key="adm", actor=1)
    assert retry.ok and retry.transaction_id == first.transaction_id
    assert _balance(token_billing_db, cid, pid) == 7


async def test_reuse_after_ttl_is_rejected_never_a_free_replay(
    token_billing_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid, pid = _setup(token_billing_db)
    dal = token_billing_db.dal
    first = await _debit(token_billing_db, cid, key="adm", actor=1)
    assert first.ok
    # Age the recorded debit past the default 10-minute window.
    old = datetime.now(UTC) - timedelta(seconds=svc.DEFAULT_IDEMPOTENCY_TTL_SECONDS + 5)
    dal(dal.token_transactions.id == first.transaction_id).update(created_at=old)
    dal.commit()
    late = await _debit(token_billing_db, cid, key="adm", actor=1)
    assert not late.ok
    assert late.blocked_reason == svc.REASON_IDEMPOTENCY_CONFLICT
    assert _balance(token_billing_db, cid, pid) == 7

    # The window is configurable: a longer TTL makes the same row replayable again.
    monkeypatch.setenv("TOKEN_DEBIT_IDEMPOTENCY_TTL_SECONDS", "3600")
    assert (await _debit(token_billing_db, cid, key="adm", actor=1)).ok
    assert _balance(token_billing_db, cid, pid) == 7


async def test_new_admission_after_a_started_one_debits_again(token_billing_db: Any) -> None:
    """svc-streaming ends a run's key once it has started; the next start uses a new key."""
    cid, pid = _setup(token_billing_db)
    assert (await _debit(token_billing_db, cid, key="run-1", actor=1)).ok
    assert (await _debit(token_billing_db, cid, key="run-2", actor=1)).ok
    assert _balance(token_billing_db, cid, pid) == 4


def test_ttl_defaults_and_bad_env_fall_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TOKEN_DEBIT_IDEMPOTENCY_TTL_SECONDS", raising=False)
    assert svc.idempotency_ttl_seconds() == svc.DEFAULT_IDEMPOTENCY_TTL_SECONDS == 600
    for bad in ("x", "0", "-1"):
        monkeypatch.setenv("TOKEN_DEBIT_IDEMPOTENCY_TTL_SECONDS", bad)
        assert svc.idempotency_ttl_seconds() == svc.DEFAULT_IDEMPOTENCY_TTL_SECONDS
