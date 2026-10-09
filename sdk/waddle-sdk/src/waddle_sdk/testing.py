"""Shared, charset-enforcing test double for the `kv` host capability (gh-631).

**Why this module exists.** Every bundle test suite needs a stand-in for
``wit_world.imports.kv`` (no real componentize-py/wasmtime build runs in
unit tests). Before this module, each bundle hand-rolled its own in-memory
fake (`count`, `lurk`, `sdk/waddle-sdk/tests/test_community_kv.py`,
`test_sub_modules.py` all did), and every one of them accepted *any* string
as a key. The real host capability
(`core/bundle_host_kv/src/scope.rs::is_allowed_key_byte`) rejects any key
containing a byte outside ASCII alnum + ``_``/``-``/``.`` -- most
importantly ``:``, its own reserved namespace separator. `count`'s
`"count:registry"` and `lurk`'s `"lurk:state:{community}:{pseudonym}"` both
passed every test against a permissive fake and then failed on the real
host in production with `kv.error::backend` (gh-631).

Use :func:`install_fake_kv_host` (or the exported :class:`FakeKvHost`
directly) instead of hand-rolling a fake: it enforces the exact same
charset as ``waddle_sdk.kv.validate_key`` -- which is itself mirrored from
``scope.rs`` and pinned to it by
``sdk/waddle-sdk/tests/test_kv.py::test_allowed_key_charset_matches_host_scope_rs``
-- so a colon (or any other host-rejected byte) in a bundle's key fails the
test suite immediately instead of hiding a production-only bug.

Usage (pytest)::

    from waddle_sdk.testing import install_fake_kv_host

    @pytest.fixture
    def fake_kv(monkeypatch):
        return install_fake_kv_host(monkeypatch)

    def test_something(fake_kv):
        ...
        assert fake_kv.store["my.key"] == b"value"
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from typing import Any

from waddle_sdk.kv import InvalidKvKeyError, validate_key

__all__ = [
    "FakeEconomyHost",
    "FakeKvHost",
    "FakeReputationHost",
    "InvalidKvKeyError",
    "install_fake_economy_host",
    "install_fake_kv_host",
    "install_fake_reputation_host",
]


@dataclass(slots=True)
class FakeKvHost:
    """In-memory stand-in for `wit_world.imports.kv`, validating every key like the real host.

    `store`/`calls` are public so a test can seed state or assert on exact
    host calls, matching the shape every hand-rolled fake already used
    (`.store[key]`, `.calls == [("get", (key,)), ...]`) -- migrating an
    existing bundle test suite onto this fake is a near drop-in swap.
    """

    store: dict[str, bytes] = field(default_factory=dict)
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)

    def get(self, key: str) -> bytes | None:
        """Return the stored value for `key`, or `None` if unset. Validates `key` first."""
        validate_key(key)
        self.calls.append(("get", (key,)))
        return self.store.get(key)

    def set(self, key: str, value: bytes, ttl_seconds: int) -> None:
        """Store `value` under `key` (TTL recorded but not enforced). Validates `key` first."""
        validate_key(key)
        self.calls.append(("set", (key, bytes(value), ttl_seconds)))
        self.store[key] = bytes(value)

    def delete(self, key: str) -> None:
        """Delete `key` if present. Validates `key` first."""
        validate_key(key)
        self.calls.append(("delete", (key,)))
        self.store.pop(key, None)

    def increment(self, key: str, delta: int, ttl_seconds: int) -> int:
        """Add `delta` to the value under `key`, return the new total. Validates `key` first."""
        validate_key(key)
        self.calls.append(("increment", (key, delta, ttl_seconds)))
        current = int(self.store.get(key, b"0"))
        new_value = current + delta
        self.store[key] = str(new_value).encode()
        return new_value


def install_fake_kv_host(monkeypatch: Any) -> FakeKvHost:
    """Install a fresh `FakeKvHost` as `wit_world.imports.kv` and return it.

    `monkeypatch` takes `pytest.MonkeyPatch` (left untyped so importing this
    module never requires pytest to be installed outside test environments).
    """
    host = FakeKvHost()
    kv_mod = types.SimpleNamespace(
        get=host.get, set=host.set, delete=host.delete, increment=host.increment
    )
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(kv=kv_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    return host


@dataclass(frozen=True)
class _FakeWitError(Exception):
    """Stand-in for componentize-py's generated ``Err`` wrapper (``.value`` = the error variant)."""

    value: Any


def _variant(name: str, with_payload: bool) -> type:
    """Build a class named exactly like a generated ``reputation.Error_*`` variant case."""
    if with_payload:

        @dataclass
        class _Case:
            value: str

    else:

        @dataclass
        class _Case:  # type: ignore[no-redef]
            pass

    _Case.__name__ = name
    _Case.__qualname__ = name
    return _Case


_Error_Denied = _variant("Error_Denied", True)
_Error_NotAMember = _variant("Error_NotAMember", False)
_Error_DailyCapExceeded = _variant("Error_DailyCapExceeded", False)
_Error_Invalid = _variant("Error_Invalid", True)
_Error_Unavailable = _variant("Error_Unavailable", True)
_Error_Backend = _variant("Error_Backend", True)


@dataclass(slots=True)
class FakeReputationHost:
    """In-memory stand-in for `wit_world.imports.reputation`, enforcing the real host's rules.

    Mirrors what the real stage does (gate + store): the target must be in
    `members`; `granted` False denies every call `not_granted`; one adjust may
    not exceed `per_call_abs_max`; the rolling total per user may not exceed
    `daily_abs_cap` (the catalog's `reputation.community.write` ceilings are 5
    and 5 -- the defaults here). Every failure raises the same-named
    `Error_*` variant the generated binding would, wrapped in an `Err`-shaped
    exception, so a bundle's error handling is exercised for real.
    """

    members: set[str] = field(default_factory=set)
    balances: dict[str, int] = field(default_factory=dict)
    ledger: list[tuple[str, int, str]] = field(default_factory=list)
    granted: bool = True
    per_call_abs_max: int = 5
    daily_abs_cap: int = 5
    _used: dict[str, int] = field(default_factory=dict)

    def _check(self, user: str) -> None:
        if not self.granted:
            raise _FakeWitError(_Error_Denied("not_granted"))
        if user not in self.members:
            raise _FakeWitError(_Error_NotAMember())

    def get(self, user: str) -> int:
        """Return `user`'s balance (0 for a member with no adjustments)."""
        self._check(user)
        return self.balances.get(user, 0)

    def adjust(self, user: str, delta: int, reason: str) -> int:
        """Apply `delta` and return the new balance, enforcing bounds and the daily cap."""
        self._check(user)
        if abs(delta) > self.per_call_abs_max:
            raise _FakeWitError(_Error_Denied("delta_out_of_bounds"))
        if self._used.get(user, 0) + abs(delta) > self.daily_abs_cap:
            raise _FakeWitError(_Error_DailyCapExceeded())
        self._used[user] = self._used.get(user, 0) + abs(delta)
        self.balances[user] = self.balances.get(user, 0) + delta
        self.ledger.append((user, delta, reason))
        return self.balances[user]


def install_fake_reputation_host(
    monkeypatch: Any, members: set[str] | None = None
) -> FakeReputationHost:
    """Install a fresh `FakeReputationHost` as `wit_world.imports.reputation` and return it."""
    host = FakeReputationHost(members=set(members or ()))
    rep_mod = types.SimpleNamespace(get=host.get, adjust=host.adjust)
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(reputation=rep_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    return host


_Error_InsufficientFunds = _variant("Error_InsufficientFunds", True)
_Error_OverCap = _variant("Error_OverCap", True)


@dataclass(frozen=True)
class _FakeEntry:
    """Stand-in for the generated ``economy.Entry`` record."""

    user: str
    balance: int


@dataclass(slots=True)
class FakeEconomyHost:
    """In-memory stand-in for `wit_world.imports.economy`, enforcing the real host's rules.

    Mirrors what the real stage does (gate + store): every named user must be
    in `members`; `granted` False denies every call `not_granted`; a wager's
    stake must be `1..=max_bet` (else `Error_OverCap(max_bet)`), held by the
    user (else `Error_InsufficientFunds(balance)`), and its payout at most
    `stake * 100` (else `Error_OverCap(stake * 100)`); a transfer needs two
    distinct members and a held, in-cap amount. Money moves atomically (the
    check and the write are one step, so the balance can never go negative)
    and every movement appends a `ledger` row. Every refusal raises the
    same-named `Error_*` variant the generated binding would, wrapped in an
    `Err`-shaped exception, so a bundle's error handling is exercised for real.
    """

    members: set[str] = field(default_factory=set)
    balances: dict[str, int] = field(default_factory=dict)
    ledger: list[tuple[str, str, int]] = field(default_factory=list)
    granted: bool = True
    max_bet_cap: int = 1_000
    max_amount_cap: int = 1_000
    payout_multiple: int = 100

    def _check(self, *users: str) -> None:
        if not self.granted:
            raise _FakeWitError(_Error_Denied("not_granted"))
        for user in users:
            if user not in self.members:
                raise _FakeWitError(_Error_NotAMember())

    def balance(self, user: str) -> int:
        """Return `user`'s balance (0 for a member who holds nothing)."""
        self._check(user)
        return self.balances.get(user, 0)

    def max_bet(self, user: str) -> int:
        """Return `min(max_bet_cap, balance)`."""
        self._check(user)
        return min(self.max_bet_cap, self.balances.get(user, 0))

    def wager(self, user: str, stake: int, payout: int) -> int:
        """Atomically debit `stake`, credit `payout`; enforce cap, funds and payout multiple."""
        self._check(user)
        if stake < 1:
            raise _FakeWitError(_Error_Invalid("stake must be >= 1"))
        if stake > self.max_bet_cap:
            raise _FakeWitError(_Error_OverCap(self.max_bet_cap))
        if payout > stake * self.payout_multiple:
            raise _FakeWitError(_Error_OverCap(stake * self.payout_multiple))
        held = self.balances.get(user, 0)
        if held < stake:
            raise _FakeWitError(_Error_InsufficientFunds(held))
        self.balances[user] = held - stake + payout
        self.ledger.append(("wager", user, payout - stake))
        return self.balances[user]

    def transfer(self, from_user: str, to_user: str, amount: int) -> None:
        """Atomically move `amount` between two distinct members."""
        self._check(from_user, to_user)
        if from_user == to_user or amount < 1:
            raise _FakeWitError(_Error_Invalid("bad transfer"))
        if amount > self.max_amount_cap:
            raise _FakeWitError(_Error_OverCap(self.max_amount_cap))
        held = self.balances.get(from_user, 0)
        if held < amount:
            raise _FakeWitError(_Error_InsufficientFunds(held))
        self.balances[from_user] = held - amount
        self.balances[to_user] = self.balances.get(to_user, 0) + amount
        self.ledger.append(("transfer_out", from_user, -amount))
        self.ledger.append(("transfer_in", to_user, amount))

    def leaderboard(self, limit: int) -> list[_FakeEntry]:
        """Return the top `limit` members by balance, highest first (ties by user id)."""
        self._check()
        if not 1 <= limit <= 100:
            raise _FakeWitError(_Error_Invalid("limit must be 1..=100"))
        ranked = sorted(
            ((u, b) for u, b in self.balances.items() if u in self.members),
            key=lambda ub: (-ub[1], ub[0]),
        )
        return [_FakeEntry(user=u, balance=b) for u, b in ranked[:limit]]


def install_fake_economy_host(monkeypatch: Any, members: set[str] | None = None) -> FakeEconomyHost:
    """Install a fresh `FakeEconomyHost` as `wit_world.imports.economy` and return it."""
    host = FakeEconomyHost(members=set(members or ()))
    eco_mod = types.SimpleNamespace(
        balance=host.balance,
        wager=host.wager,
        transfer=host.transfer,
        max_bet=host.max_bet,
        leaderboard=host.leaderboard,
    )
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(economy=eco_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    return host
