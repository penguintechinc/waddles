"""Golden vectors, planted-collision and ownership-claim tests for the app_id cutover.

The vectors in `testdata/bundle_app_id/vectors.json` are ALSO read by the Rust test
`core/bundle_capability_gate/src/resource.rs` (`golden_vectors_*`), so both languages are proven
to accept/reject the same ids, agree on core-namespace detection, and derive the SAME storage key.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from services import app_id_ownership
from services.app_id_ownership import claim_app_id
from services.bundle_app_id import (
    bundle_storage_key,
    is_core_namespace,
    is_valid_app_id,
)
from services.bundle_data_schema import derive_table_identity
from services.errors import ApiError

_VECTORS = json.loads(
    (
        Path(__file__).resolve().parents[2] / "testdata" / "bundle_app_id" / "vectors.json"
    ).read_text()
)


@pytest.mark.parametrize("app_id", _VECTORS["valid"])
def test_golden_valid_ids_are_accepted(app_id: str) -> None:
    assert is_valid_app_id(app_id)


@pytest.mark.parametrize("app_id", _VECTORS["invalid"])
def test_golden_invalid_ids_are_rejected(app_id: str) -> None:
    assert not is_valid_app_id(app_id)


@pytest.mark.parametrize("app_id", _VECTORS["core"]["yes"])
def test_golden_core_namespace_yes(app_id: str) -> None:
    assert is_core_namespace(app_id)


@pytest.mark.parametrize("app_id", _VECTORS["core"]["no"])
def test_golden_core_namespace_no(app_id: str) -> None:
    assert not is_core_namespace(app_id)


@pytest.mark.parametrize("case", _VECTORS["storage_keys"], ids=lambda c: c["app_id"][:40])
def test_golden_storage_keys(case: dict[str, str]) -> None:
    assert bundle_storage_key(case["app_id"]) == case["expected"]


def test_vectors_have_a_nonzero_denominator() -> None:
    assert len(_VECTORS["valid"]) > 5
    assert len(_VECTORS["invalid"]) > 10
    assert len(_VECTORS["storage_keys"]) > 5


def test_planted_collision_ids_get_distinct_keys_everywhere() -> None:
    first, second = "acme.bot.kv-store", "acme.bot_kv.store"
    assert bundle_storage_key(first) != bundle_storage_key(second)
    for provider in ("builtin", "thirdparty"):
        assert (
            derive_table_identity(first, provider=provider).table
            != derive_table_identity(second, provider=provider).table
        )


def test_storage_key_shape_is_postgres_and_kv_safe() -> None:
    for app_id in [c["app_id"] for c in _VECTORS["storage_keys"]]:
        key = bundle_storage_key(app_id)
        assert len(key.encode()) <= 63
        assert key[0].isalpha() and key == key.lower()
        assert ":" not in key and "__" not in key and "." not in key


class _Dal:
    """Just enough of `AsyncDB` for `raw_sql_write` / `raw_sql_rows`: an `engine` on SQLite."""

    def __init__(self, url: str) -> None:
        self.engine = create_async_engine(url)


@pytest.fixture
async def dal(tmp_path: Path) -> AsyncGenerator[_Dal]:
    instance = _Dal(f"sqlite+aiosqlite:///{tmp_path / 'owners.db'}")
    async with instance.engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE app_id_owners (app_id VARCHAR(127) PRIMARY KEY, owner_id INTEGER, "
            "storage_key VARCHAR(63) NOT NULL UNIQUE, "
            "created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
    yield instance
    await instance.engine.dispose()


async def test_first_claim_wins_and_a_second_owner_is_refused(dal: _Dal) -> None:
    await claim_app_id(dal, app_id="acme.bot.alpha", owner_id=1)
    await claim_app_id(dal, app_id="acme.bot.alpha", owner_id=1)  # idempotent for the owner
    with pytest.raises(ApiError) as exc:
        await claim_app_id(dal, app_id="acme.bot.alpha", owner_id=2)
    assert exc.value.status_code == 403


async def test_admin_claim_does_not_enforce_owner(dal: _Dal) -> None:
    await claim_app_id(dal, app_id="acme.bot.alpha", owner_id=1)
    await claim_app_id(dal, app_id="acme.bot.alpha", owner_id=99, enforce_owner=False)
    assert await app_id_ownership.owner_of(dal, "acme.bot.alpha") == (True, 1)


async def test_concurrent_claims_exactly_one_wins(dal: _Dal) -> None:
    results = await asyncio.gather(
        *(claim_app_id(dal, app_id="acme.bot.race", owner_id=n) for n in range(1, 9)),
        return_exceptions=True,
    )
    winners = [r for r in results if r is None]
    refused = [r for r in results if isinstance(r, ApiError) and r.status_code == 403]
    assert len(winners) == 1, results
    assert len(refused) == 7, results


async def test_second_id_with_a_colliding_storage_key_is_refused(
    dal: _Dal, monkeypatch: pytest.MonkeyPatch
) -> None:
    await claim_app_id(dal, app_id="acme.bot.kv-store", owner_id=1)
    # The two planted ids derive distinct keys, so both register fine...
    await claim_app_id(dal, app_id="acme.bot_kv.store", owner_id=2)
    # ...but if a (hypothetical) hash collision made another id derive an existing key, the
    # UNIQUE storage_key makes the database refuse it instead of merging the namespaces.
    existing_key = bundle_storage_key("acme.bot.kv-store")
    monkeypatch.setattr(app_id_ownership, "bundle_storage_key", lambda *_args: existing_key)
    with pytest.raises(ApiError) as exc:
        await claim_app_id(dal, app_id="evil.bot.kv-store", owner_id=3)
    assert exc.value.status_code == 409
    assert await app_id_ownership.owner_of(dal, "evil.bot.kv-store") == (False, None)
