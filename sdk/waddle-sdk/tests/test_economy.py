"""Tests for `waddle_sdk.economy` (issue #714)."""

from __future__ import annotations

import re
import sys
import types
from pathlib import Path
from uuid import uuid4

import pytest

import waddle_sdk.economy as economy
from waddle_sdk.testing import (
    FakeEconomyHost,
    _Error_Backend,
    _Error_Denied,
    _Error_InsufficientFunds,
    _Error_Invalid,
    _Error_NotAMember,
    _Error_OverCap,
    _Error_Unavailable,
    _FakeWitError,
    install_fake_economy_host,
)

ALICE = str(uuid4())
BOB = str(uuid4())


def _run(coro):
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("economy coroutine unexpectedly suspended")


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> FakeEconomyHost:
    h = install_fake_economy_host(monkeypatch, {ALICE, BOB})
    h.balances[ALICE] = 100
    h.actor = ALICE  # the invocation was triggered by alice
    return h


# --- happy paths -------------------------------------------------------------


def test_balance_is_zero_for_a_member_who_holds_nothing(host: FakeEconomyHost) -> None:
    assert _run(economy.balance(BOB)) == 0
    assert _run(economy.balance(ALICE)) == 100


def test_wager_applies_the_net_delta_and_returns_the_new_balance(host: FakeEconomyHost) -> None:
    assert _run(economy.wager(ALICE, 10, 25)) == 115  # win
    assert _run(economy.wager(ALICE, 40, 0)) == 75  # loss
    assert _run(economy.balance(ALICE)) == 75
    assert host.ledger == [("wager", ALICE, 15), ("wager", ALICE, -40)]


def test_transfer_moves_money_between_members(host: FakeEconomyHost) -> None:
    assert _run(economy.transfer(ALICE, BOB, 30)) is None
    assert _run(economy.balance(ALICE)) == 70
    assert _run(economy.balance(BOB)) == 30
    assert sum(host.balances.values()) == 100


def test_max_bet_is_the_cap_bounded_by_the_balance(host: FakeEconomyHost) -> None:
    host.max_bet_cap = 60
    assert _run(economy.max_bet(ALICE)) == 60
    host.max_bet_cap = 500
    assert _run(economy.max_bet(ALICE)) == 100
    assert _run(economy.max_bet(BOB)) == 0


def test_leaderboard_returns_typed_entries_highest_first(host: FakeEconomyHost) -> None:
    host.balances[BOB] = 300
    rows = _run(economy.leaderboard(5))
    assert rows == [
        economy.LeaderboardEntry(user=BOB, balance=300),
        economy.LeaderboardEntry(user=ALICE, balance=100),
    ]
    assert _run(economy.leaderboard(1)) == rows[:1]


def test_user_is_normalized_to_canonical_lowercase_uuid(host: FakeEconomyHost) -> None:
    assert _run(economy.balance(ALICE.upper())) == 100


# --- fail-loud on every host refusal (never a swallowed default) -------------


def test_gate_denial_raises_denied_error_with_the_gate_code(host: FakeEconomyHost) -> None:
    host.granted = False
    for coro in (
        economy.balance(ALICE),
        economy.wager(ALICE, 1, 0),
        economy.transfer(ALICE, BOB, 1),
        economy.max_bet(ALICE),
        economy.leaderboard(3),
    ):
        with pytest.raises(economy.DeniedError) as ei:
            _run(coro)
        assert ei.value.code == "not_granted"


def test_insufficient_funds_carries_the_balance_and_moves_nothing(host: FakeEconomyHost) -> None:
    with pytest.raises(economy.InsufficientFundsError) as ei:
        _run(economy.wager(ALICE, 101, 0))
    assert ei.value.balance == 100
    host.actor = BOB  # bob's own invocation: he can only spend what HE holds
    with pytest.raises(economy.InsufficientFundsError) as ei2:
        _run(economy.transfer(BOB, ALICE, 1))
    assert ei2.value.balance == 0
    assert host.ledger == []
    assert host.balances == {ALICE: 100}


def test_you_cannot_stake_what_you_do_not_hold_even_with_a_huge_payout(
    host: FakeEconomyHost,
) -> None:
    host.actor = BOB
    with pytest.raises(economy.InsufficientFundsError):
        _run(economy.wager(BOB, 1, 100))


def test_over_cap_carries_the_cap(host: FakeEconomyHost) -> None:
    host.max_bet_cap = 50
    with pytest.raises(economy.OverCapError) as ei:
        _run(economy.wager(ALICE, 51, 0))
    assert ei.value.cap == 50
    with pytest.raises(economy.OverCapError) as ei2:
        _run(economy.wager(ALICE, 10, 1_001))
    assert ei2.value.cap == 1_000


def test_non_member_raises_not_a_member(host: FakeEconomyHost) -> None:
    stranger = str(uuid4())
    for coro in (
        economy.balance(stranger),
        economy.wager(stranger, 1, 0),
        economy.transfer(ALICE, stranger, 1),
        economy.transfer(stranger, ALICE, 1),
        economy.max_bet(stranger),
    ):
        with pytest.raises(economy.NotAMemberError):
            _run(coro)


@pytest.mark.parametrize(
    ("variant", "exc_type"),
    [
        (_Error_Invalid("bad"), economy.InvalidEconomyArgError),
        (_Error_Unavailable("not wired"), economy.UnavailableError),
        (_Error_Backend("db down"), economy.BackendError),
        (_Error_NotAMember(), economy.NotAMemberError),
        (_Error_InsufficientFunds(3), economy.InsufficientFundsError),
        (_Error_OverCap(9), economy.OverCapError),
        (_Error_Denied("quota_exceeded"), economy.DeniedError),
    ],
)
def test_every_wit_error_variant_maps_to_its_exception_on_every_call(
    monkeypatch: pytest.MonkeyPatch, variant: object, exc_type: type
) -> None:
    def boom(*_a: object) -> int:
        raise _FakeWitError(variant)

    fake = types.ModuleType("wit_world")
    fake.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        economy=types.SimpleNamespace(
            balance=boom, wager=boom, transfer=boom, max_bet=boom, leaderboard=boom
        )
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake)
    for coro in (
        economy.balance(ALICE),
        economy.wager(ALICE, 1, 0),
        economy.transfer(ALICE, BOB, 1),
        economy.max_bet(ALICE),
        economy.leaderboard(3),
    ):
        with pytest.raises(exc_type):
            _run(coro)


def test_an_unclassifiable_host_exception_is_a_backend_error_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_a: object) -> int:
        raise RuntimeError("wasm trap")

    fake = types.ModuleType("wit_world")
    fake.imports = types.SimpleNamespace(economy=types.SimpleNamespace(balance=boom))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake)
    with pytest.raises(economy.BackendError):
        _run(economy.balance(ALICE))


# --- argument validation happens before any host call ------------------------


@pytest.mark.parametrize("bad", ["", "not-a-uuid", "1234", None, 5])
def test_bad_user_is_rejected_before_any_host_call(bad: object) -> None:
    for coro in (
        economy.balance(bad),  # type: ignore[arg-type]
        economy.wager(bad, 1, 0),  # type: ignore[arg-type]
        economy.max_bet(bad),  # type: ignore[arg-type]
        economy.transfer(bad, BOB, 1),  # type: ignore[arg-type]
        economy.transfer(ALICE, bad, 1),  # type: ignore[arg-type]
    ):
        with pytest.raises(economy.InvalidEconomyArgError):
            _run(coro)


@pytest.mark.parametrize("bad", [0, -1, 2**63, 1.5, "3", True, None])
def test_bad_stake_is_rejected_before_any_host_call(bad: object) -> None:
    with pytest.raises(economy.InvalidEconomyArgError):
        _run(economy.wager(ALICE, bad, 0))  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [-1, 2**63, 1.5, "3", True, None])
def test_bad_payout_is_rejected_before_any_host_call(bad: object) -> None:
    with pytest.raises(economy.InvalidEconomyArgError):
        _run(economy.wager(ALICE, 1, bad))  # type: ignore[arg-type]


def test_zero_payout_is_valid_it_is_a_loss() -> None:
    assert economy.validate_amount("payout", 0, minimum=0) == 0


@pytest.mark.parametrize("bad", [0, -1, 2**63, 1.5, "3", True, None])
def test_bad_transfer_amount_is_rejected_before_any_host_call(bad: object) -> None:
    with pytest.raises(economy.InvalidEconomyArgError):
        _run(economy.transfer(ALICE, BOB, bad))  # type: ignore[arg-type]


def test_transfer_to_oneself_is_rejected_before_any_host_call() -> None:
    with pytest.raises(economy.InvalidEconomyArgError):
        _run(economy.transfer(ALICE, ALICE.upper(), 1))


@pytest.mark.parametrize("bad", [0, -1, 101, 1.5, "3", True, None])
def test_bad_leaderboard_limit_is_rejected_before_any_host_call(bad: object) -> None:
    with pytest.raises(economy.InvalidEconomyArgError):
        _run(economy.leaderboard(bad))  # type: ignore[arg-type]


# --- eager import (componentize-py wizening) ---------------------------------


def test_module_eagerly_attempts_the_wit_binding_import_at_load() -> None:
    """The module-level `from wit_world.imports import economy` is what forces wizening.

    Host-side there is no `wit_world`, so the guarded import leaves the
    sentinel `None` -- but the attempt itself must exist at module top level
    (not inside a function), proven by reading the source.
    """
    source = Path(economy.__file__).read_text()
    needle = "    from wit_world.imports import economy"
    top_level_imports = [line for line in source.splitlines() if line.startswith(needle)]
    assert top_level_imports, "eager top-level import of wit_world.imports.economy is missing"
    assert hasattr(economy, "_economy_binding")


def test_component_entry_eager_import_list_includes_economy() -> None:
    entry = Path(economy.__file__).with_name("_component_entry.py").read_text()
    assert '"economy"' in entry


def test_verify_script_pins_every_economy_binding_shape() -> None:
    script = (Path(economy.__file__).parents[2] / "scripts" / "verify_wit_bindings.sh").read_text()
    for needle in (
        "def balance(user: str) -> int:",
        "def wager(user: str, stake: int, payout: int) -> int:",
        "def transfer(from_user: str, to_user: str, amount: int) -> None:",
        "def max_bet(user: str) -> int:",
        "def leaderboard(limit: int) -> List[Entry]:",
        "class Error_InsufficientFunds:",
        "class Error_OverCap:",
    ):
        assert needle in script, needle


# --- cross-language pin to the real host crate -------------------------------

_LIB_RS = Path(__file__).resolve().parents[3] / "core" / "bundle_host_economy" / "src" / "lib.rs"


def test_limits_match_host_lib_rs() -> None:
    """Pins the leaderboard/payout constants to `core/bundle_host_economy/src/lib.rs`."""
    rust = _LIB_RS.read_text()
    for name, mirrored in (
        ("MAX_LEADERBOARD_LIMIT: u32", economy.MAX_LEADERBOARD_LIMIT),
        ("MAX_PAYOUT_MULTIPLE: i64", economy.MAX_PAYOUT_MULTIPLE),
    ):
        m = re.search(rf"pub const {name} = (\d+);", rust)
        assert m, f"{name} not found in lib.rs"
        assert int(m.group(1)) == mirrored, name
    assert economy.MAX_AMOUNT == 2**63 - 1


def test_wire_ops_match_the_executor_and_stage() -> None:
    """The five op names the SDK's WIT import produces are the ones the stage dispatches on."""
    root = Path(__file__).resolve().parents[3]
    executor = (
        root / "core" / "bundle_executor" / "src" / "host" / "stage_next_economy.rs"
    ).read_text()
    stage = (root / "core" / "svc_process" / "src" / "capabilities.rs").read_text()
    for op in ("balance", "wager", "transfer", "max_bet", "leaderboard"):
        assert f'"economy.{op}"' in executor, op
        assert f'"economy.{op}"' in stage, op


# --- #751 money-safety: only the invocation's actor's money moves -------------


def test_a_wager_on_another_members_account_is_refused_actor_mismatch(
    host: FakeEconomyHost,
) -> None:
    host.balances[BOB] = 300
    with pytest.raises(economy.DeniedError) as ei:
        _run(economy.wager(BOB, 50, 0))  # alice triggered this; bob is the victim
    assert ei.value.code == "actor_mismatch"
    assert host.balances[BOB] == 300
    assert host.ledger == []


def test_a_transfer_from_another_members_account_is_refused_actor_mismatch(
    host: FakeEconomyHost,
) -> None:
    host.balances[BOB] = 300
    with pytest.raises(economy.DeniedError) as ei:
        _run(economy.transfer(BOB, ALICE, 100))  # steal INTO the actor
    assert ei.value.code == "actor_mismatch"
    assert host.balances == {ALICE: 100, BOB: 300}
    assert host.ledger == []


def test_the_actor_can_pay_anyone_and_reads_are_not_bound(host: FakeEconomyHost) -> None:
    host.balances[BOB] = 300
    _run(economy.transfer(ALICE, BOB, 30))
    assert _run(economy.balance(BOB)) == 330  # a read of another member is fine
    assert _run(economy.max_bet(BOB)) == 330


def test_an_unlinked_actor_cannot_move_money_at_all(host: FakeEconomyHost) -> None:
    host.actor = None
    for call in (
        lambda: economy.wager(ALICE, 1, 0),
        lambda: economy.transfer(ALICE, BOB, 1),
    ):
        with pytest.raises(economy.DeniedError) as ei:
            _run(call())
        assert ei.value.code == "not_linked"
    assert host.ledger == []
