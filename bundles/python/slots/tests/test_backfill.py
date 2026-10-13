"""Backfill coverage for the `slots` bundle: mod gate, payout table, corrupt state, PII-free logs.

`test_app.py` already drives `src/` to ~100%; this module pins the invariants that number cannot:
the `set cooldown` mod gate fails closed across the whole badge matrix with zero kv traffic,
cooldown bounds (this bundle's floor is 5 s, not 0), the reel table's structural invariants (rarer
symbol pays more, every symbol wins/pays exactly its own value), the best-payout record only moves
on a strictly better spin and heals from corruption loudly, every kv error log carrying only op +
exception type, the gh-631 key-charset regression (the SDK's `kv` validates every key even though
this suite's own kv fake is permissive), a PII-free-log regression, the flag's fail-closed
default, and the static `_entry_wiring` re-export.

Reuses `test_app.py`'s hand-rolled fake host (`_FakeHost`/`_install`).
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import pytest
from test_app import (
    _expected_pseudonym,
    _FakeHost,
    _install,
    _sample_envelope,
    _sample_event,
    _scoped,
)
from waddle_sdk.kv import validate_key

import _entry_wiring
import app
from app import (
    _KNOWN_COMMANDS,
    _REEL_TABLE,
    _USAGE,
    DEFAULT_COOLDOWN_SECONDS,
    FLAG_KEY,
    MAX_COOLDOWN_SECONDS,
    MIN_COOLDOWN_SECONDS,
    _bestpayout_key,
    _evaluate_spin,
    _lastspin_key,
    _spins_key,
    _wins_key,
    dispatch,
    transform,
)

CANARY = "CANARYuser9f3a"
ERROR_LEVEL = 0
CONFIG_KEY = "slots.config.cooldown"
SYMBOLS = [entry[0] for entry in _REEL_TABLE]


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> _FakeHost:
    """A fresh fake host with the flag ON."""
    state = _FakeHost()
    _install(monkeypatch, state)
    return state


def _go(command: str, **kwargs: Any) -> Any:
    return _run(dispatch(_sample_envelope("twitch", command, **kwargs), {}, http_client=None))


def _reply(host: _FakeHost) -> str:
    return str(json.loads(host.relay_calls[-1][1])["text"])


def _force_reels(monkeypatch: pytest.MonkeyPatch, *reels: Any) -> None:
    monkeypatch.setattr("app._roll_reels", lambda: tuple(reels))


# -- mod gate fails closed, zero side effects
@pytest.mark.parametrize(
    ("is_mod", "is_broadcaster"),
    [(None, None), (False, None), (None, False), (False, False)],
    ids=["no-badges", "mod-false", "broadcaster-false", "both-false"],
)
def test_set_cooldown_is_denied_without_a_true_badge_and_does_no_kv_io(
    host: _FakeHost, is_mod: bool | None, is_broadcaster: bool | None
) -> None:
    result = _go(
        "config_set_cooldown", arg="cooldown 30", is_mod=is_mod, is_broadcaster=is_broadcaster
    )

    assert result.detail == "config_set_cooldown:denied"
    assert _reply(host) == "only moderators/broadcasters can configure !slots"
    assert host.kv_calls == [] and host.store == {}


@pytest.mark.parametrize("badge", ["is_mod", "is_broadcaster"])
def test_either_true_badge_may_set_the_cooldown(host: _FakeHost, badge: str) -> None:
    _go("config_set_cooldown", arg="cooldown 45", **{badge: True})
    assert _reply(host) == "slots cooldown set to 45s"
    assert host.store[_scoped(CONFIG_KEY)] == b"45"


@pytest.mark.parametrize("command", ["spin", "list", "usage"])
def test_spin_list_and_usage_need_no_badge(host: _FakeHost, command: str) -> None:
    assert _go(command).detail == command


def test_known_commands_are_exactly_the_documented_four() -> None:
    assert _KNOWN_COMMANDS == {"spin", "list", "config_set_cooldown", "usage"}


# -- cooldown parsing, bounds, TTL
@pytest.mark.parametrize("seconds", [MIN_COOLDOWN_SECONDS, MAX_COOLDOWN_SECONDS, 60])
def test_cooldown_bounds_are_inclusive(host: _FakeHost, seconds: int) -> None:
    _go("config_set_cooldown", arg=f"cooldown {seconds}", is_mod=True)
    assert host.store[_scoped(CONFIG_KEY)] == str(seconds).encode()


@pytest.mark.parametrize(
    "arg",
    [
        "cooldown 0",
        "cooldown 4",
        "cooldown 3601",
        "cooldown -5",
        "cooldown 99999999999999999999",
        "cooldown abc",
        "cooldown 1.5",
        "cooldown",
        "cooldown 5 6",
        "frobnicate 5",
        "5",
        "",
        "set cooldown 5",
    ],
)
def test_invalid_cooldown_arguments_are_rejected_and_write_nothing(
    host: _FakeHost, arg: str
) -> None:
    _go("config_set_cooldown", arg=arg, is_mod=True)
    assert [c for c in host.kv_calls if c[0] == "set"] == []
    assert (MIN_COOLDOWN_SECONDS, MAX_COOLDOWN_SECONDS) == (5, 3600)


def test_set_cooldown_without_an_arg_payload_replies_usage(host: _FakeHost) -> None:
    _go("config_set_cooldown", is_mod=True)
    assert _reply(host) == _USAGE


def test_lastspin_ttl_equals_the_configured_cooldown_and_default_otherwise(
    host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _go("spin")
    ttls = [c[3] for c in host.kv_calls if c[0] == "set" and "lastspin" in c[1]]
    assert ttls == [DEFAULT_COOLDOWN_SECONDS]

    host.kv_calls.clear()
    _go("config_set_cooldown", arg="cooldown 120", is_mod=True)
    _go("spin", actor="someone-else")
    ttls = [c[3] for c in host.kv_calls if c[0] == "set" and "lastspin" in c[1]]
    assert ttls == [120]


# -- reel table invariants + payout math
def test_reel_table_is_well_formed_and_rarer_symbols_pay_more() -> None:
    names = [s.name for s in SYMBOLS]
    assert len(names) == len(set(names)) == 6
    weights = [entry[1] for entry in _REEL_TABLE]
    assert all(w > 0 for w in weights)
    ranked = sorted(_REEL_TABLE, key=lambda entry: -entry[1])  # most common first
    payouts = [entry[0].payout for entry in ranked]
    assert payouts == sorted(payouts), "a rarer symbol must never pay less than a commoner one"
    assert all(s.payout > 0 and s.emoji and s.flavor for s in SYMBOLS)


@pytest.mark.parametrize("symbol", SYMBOLS, ids=lambda s: s.name)
def test_triple_match_wins_exactly_the_symbols_payout(
    host: _FakeHost, monkeypatch: pytest.MonkeyPatch, symbol: Any
) -> None:
    _force_reels(monkeypatch, symbol, symbol, symbol)
    assert _evaluate_spin((symbol, symbol, symbol)) == (True, symbol.payout)

    _go("spin")

    reply = _reply(host)
    assert f"You win {symbol.payout} points! (spin #1)" in reply
    assert reply.count(symbol.emoji) == 3
    pseudonym = _expected_pseudonym("viewer-1")
    assert host.store[_scoped(_wins_key(pseudonym))] == b"1"
    assert json.loads(host.store[_scoped(_bestpayout_key(pseudonym))]) == {
        "symbol": symbol.name,
        "payout": symbol.payout,
    }


def test_two_of_a_kind_and_all_distinct_are_losses(
    host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b, c = SYMBOLS[:3]
    assert _evaluate_spin((a, a, b)) == (False, 0)
    assert _evaluate_spin((a, b, c)) == (False, 0)
    _force_reels(monkeypatch, a, a, b)
    _go("spin")
    assert "You win" not in _reply(host)
    assert _scoped(_wins_key(_expected_pseudonym("viewer-1"))) not in host.store


def test_best_payout_only_moves_on_a_strictly_better_spin(
    host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    cherry, lemon = SYMBOLS[0], SYMBOLS[1]
    key = _scoped(_bestpayout_key(_expected_pseudonym("viewer-1")))
    sequence = ((cherry, "Cherry"), (lemon, "Lemon"), (cherry, "Lemon"), (lemon, "Lemon"))
    for symbol, expected in sequence:
        _force_reels(monkeypatch, symbol, symbol, symbol)
        _go("spin")
        host.advance(DEFAULT_COOLDOWN_SECONDS + 1)
        assert json.loads(host.store[key])["symbol"] == expected
    best_writes = [c for c in host.kv_calls if c[0] == "set" and "bestpayout" in c[1]]
    assert len(best_writes) == 2, "only the first win and the strictly better one write"


@pytest.mark.parametrize(
    "blob", [b"\xff\xfe", b"not json", b"{}", b'{"payout": "x"}', b"[1]", b"null", b'"s"']
)
def test_corrupt_best_payout_logs_error_and_is_healed_by_the_next_win(
    host: _FakeHost, monkeypatch: pytest.MonkeyPatch, blob: bytes
) -> None:
    pseudonym = _expected_pseudonym("viewer-1")
    key = _scoped(_bestpayout_key(pseudonym))
    host.store[key] = blob
    cherry = SYMBOLS[0]
    _force_reels(monkeypatch, cherry, cherry, cherry)

    _go("spin")

    errors = [(m, json.loads(f)) for lvl, m, f in host.log_calls if lvl == ERROR_LEVEL]
    assert errors == [("slots.bestpayout_corrupt", {"community": "comm-1"})]
    assert json.loads(host.store[key]) == {"symbol": cherry.name, "payout": cherry.payout}


def test_list_with_a_corrupt_best_payout_logs_error_and_shows_none_yet(host: _FakeHost) -> None:
    pseudonym = _expected_pseudonym("viewer-1")
    host.store[_scoped(_spins_key(pseudonym))] = b"3"
    host.store[_scoped(_bestpayout_key(pseudonym))] = b"garbage"

    _go("list")

    assert _reply(host) == "Spins: 3. Wins: 0. Best payout: none yet."
    assert [m for lvl, m, _f in host.log_calls if lvl == ERROR_LEVEL] == [
        "slots.bestpayout_corrupt"
    ]


def test_every_kv_key_written_passes_the_host_charset_check(
    host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    """regression: gh-631 -- `waddle_sdk.kv.validate_key` rejects `:`; assert none was written."""
    cherry = SYMBOLS[0]
    _force_reels(monkeypatch, cherry, cherry, cherry)
    _go("config_set_cooldown", arg="cooldown 5", is_mod=True)
    _go("spin")
    _go("list")
    keys = {c[1] for c in host.kv_calls}
    assert keys
    for key in keys:
        validate_key(key)
    assert not any(":" in key for key in keys)
    assert _lastspin_key("p") == "slots.lastspin.p"


def test_records_are_isolated_per_community(
    host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    for community in ("comm-a", "comm-b"):
        _run(
            dispatch(_sample_envelope("twitch", "spin", community=community), {}, http_client=None)
        )
    key = _spins_key(_expected_pseudonym("viewer-1"))
    assert host.store[_scoped(key, "comm-a")] == b"1" and host.store[_scoped(key, "comm-b")] == b"1"


# -- backend failures: only op + exception type are logged
class _ErrorBackend:
    """Stand-in for the generated WIT `Error_Backend` variant case class."""


class _KvError(Exception):
    """Stand-in for the generated `Err` wrapper -- `.value` holds the error union member."""

    def __init__(self) -> None:
        self.value = _ErrorBackend()


@pytest.mark.parametrize("kind", ["get", "set", "increment"])
def test_kv_failure_log_carries_only_op_and_exception_type(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    state = _FakeHost()
    _install(monkeypatch, state, **{f"kv_{kind}_raises": _KvError()})

    with pytest.raises(RuntimeError, match=f"slots kv {kind} failed: _ErrorBackend"):
        _go("spin", actor=f"{CANARY}_actor")

    errors = [(m, json.loads(f)) for lvl, m, f in state.log_calls if lvl == ERROR_LEVEL]
    assert errors == [("slots.kv_error", {"op": kind, "error": "_ErrorBackend"})]


# -- flag fail-closed
def test_flag_is_requested_with_a_fail_closed_default(host: _FakeHost) -> None:
    seen: list[tuple[str, bool]] = []
    sys.modules["wit_world"].imports.flags.enabled = (
        lambda key, default_value: seen.append((key, default_value)) or default_value
    )
    assert _run(transform(_sample_event("!slots"))) is None
    assert seen == [(FLAG_KEY, False)]
    assert FLAG_KEY == "waddles.command-slots"


def test_missing_wit_world_keeps_the_command_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "wit_world", None)
    assert _run(transform(_sample_event("!slots"))) is None


def test_flag_off_does_no_io_and_logs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _FakeHost()
    _install(monkeypatch, state, flag_enabled=False)
    assert _run(transform(_sample_event(f"!slots {CANARY}", is_mod=True))) is None
    assert state.log_calls == [] and state.kv_calls == []


# -- PII-free logs
def test_no_log_line_in_any_flow_contains_typed_text_or_the_raw_actor(
    host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PII-free-log regression: typed arguments / grammar text and the raw actor never log."""
    actor = f"{CANARY}_actor"
    for text in (
        f"!slots {CANARY}",
        f"!slots list {CANARY}",
        f"!slots set cooldown {CANARY}",
        f"!slots enable {CANARY}",
        "!slots",
        "!slots list",
    ):
        _run(transform(_sample_event(text, is_mod=True)))

    cherry = SYMBOLS[0]
    _force_reels(monkeypatch, cherry, cherry, cherry)
    _go("spin", actor=actor)
    _go("spin", actor=actor)  # cooldown path
    _go("list", actor=actor)
    _go("config_set_cooldown", arg=f"cooldown {CANARY}", actor=actor, is_mod=True)  # debug path
    _go("config_set_cooldown", arg="cooldown 5", actor=actor)  # denied path
    host.store[_scoped(CONFIG_KEY)] = b"\xff-garbage"  # corrupt-config logging path
    _go("spin", actor=f"{actor}2")

    assert host.log_calls, "expected log lines (denominator must be non-zero)"
    for _lvl, message, fields_json in host.log_calls:
        assert CANARY.lower() not in message.lower()
        assert CANARY.lower() not in fields_json.lower()
    for key, value in host.store.items():
        assert CANARY.lower() not in key.lower()
        assert CANARY.lower().encode() not in value.lower()


def test_entry_wiring_exports_the_stage_functions() -> None:
    assert _entry_wiring.bundle_transform is app.transform
    assert _entry_wiring.bundle_dispatch is app.dispatch
    assert _entry_wiring.__all__ == ["bundle_dispatch", "bundle_transform"]
