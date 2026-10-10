"""Backfill coverage for the `rps` bundle: mod gate, cooldown TTLs, PII-free logs, wiring.

`test_app.py` already drives `src/` to ~100%; this module pins the invariants that number cannot:
the `set cooldown` mod gate fails closed across the whole badge matrix with zero kv traffic,
cooldown parsing/boundary/TTL semantics (including that a typo costs no cooldown), exactly one
W/L/T counter moving per round, every kv error log carrying only op + exception type, the gh-631
key-charset regression (the SDK's `kv` validates every key even though this suite's own kv fake
is permissive), a PII-free-log regression across transform and dispatch paths, the flag's
fail-closed default, and the static `_entry_wiring` re-export.

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
    _force_bot_choice,
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
    _USAGE,
    DEFAULT_COOLDOWN_SECONDS,
    FLAG_KEY,
    MAX_COOLDOWN_SECONDS,
    MIN_COOLDOWN_SECONDS,
    _lastplay_key,
    _losses_key,
    _ties_key,
    _wins_key,
    dispatch,
    transform,
)

CANARY = "CANARYuser9f3a"
ERROR_LEVEL = 0
CONFIG_KEY = "rps.config.cooldown"


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
        "config_set_cooldown", arg="cooldown 5", is_mod=is_mod, is_broadcaster=is_broadcaster
    )

    assert result.detail == "config_set_cooldown:denied"
    assert _reply(host) == "only moderators/broadcasters can configure !rps"
    assert host.kv_calls == [] and host.store == {}


@pytest.mark.parametrize("badge", ["is_mod", "is_broadcaster"])
def test_either_true_badge_may_set_the_cooldown(host: _FakeHost, badge: str) -> None:
    _go("config_set_cooldown", arg="cooldown 45", **{badge: True})
    assert _reply(host) == "rps cooldown set to 45s"
    assert host.store[_scoped(CONFIG_KEY)] == b"45"


@pytest.mark.parametrize("command", ["play", "list", "usage"])
def test_play_list_and_usage_need_no_badge(host: _FakeHost, command: str) -> None:
    kwargs = {"choice": "rock"} if command == "play" else {}
    assert _go(command, **kwargs).detail.split(":")[0] == command


def test_known_commands_are_exactly_the_documented_four() -> None:
    assert _KNOWN_COMMANDS == {"play", "list", "config_set_cooldown", "usage"}


# -- cooldown parsing, bounds, TTL
@pytest.mark.parametrize(
    ("arg", "stored"),
    [
        ("cooldown 0", b"0"),
        ("cooldown 3600", b"3600"),
        ("COOLDOWN 30", b"30"),
        ("cooldown +7", b"7"),
        ("cooldown 1_0", b"10"),
    ],
)
def test_valid_cooldown_arguments_are_stored_as_canonical_ints(
    host: _FakeHost, arg: str, stored: bytes
) -> None:
    _go("config_set_cooldown", arg=arg, is_mod=True)
    assert host.store[_scoped(CONFIG_KEY)] == stored


@pytest.mark.parametrize(
    "arg",
    [
        "cooldown -1",
        "cooldown 3601",
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
    assert _scoped(CONFIG_KEY) not in host.store
    assert (MIN_COOLDOWN_SECONDS, MAX_COOLDOWN_SECONDS) == (0, 3600)


def test_set_cooldown_without_an_arg_payload_replies_usage(host: _FakeHost) -> None:
    _go("config_set_cooldown", is_mod=True)
    assert _reply(host) == _USAGE


def test_lastplay_ttl_equals_the_configured_cooldown_and_default_otherwise(
    host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_bot_choice(monkeypatch, "rock")
    _go("play", choice="rock")
    ttls = [c[3] for c in host.kv_calls if c[0] == "set" and "lastplay" in c[1]]
    assert ttls == [DEFAULT_COOLDOWN_SECONDS]

    host.kv_calls.clear()
    _go("config_set_cooldown", arg="cooldown 120", is_mod=True)
    _go("play", choice="rock", actor="someone-else")
    ttls = [c[3] for c in host.kv_calls if c[0] == "set" and "lastplay" in c[1]]
    assert ttls == [120]


@pytest.mark.parametrize("choice", ["", "lizard", "rocks", CANARY])
def test_an_invalid_move_costs_no_cooldown_and_touches_no_counter(
    host: _FakeHost, choice: str
) -> None:
    result = _go("play", choice=choice or None)
    assert result.detail == "play:invalid_choice"
    assert _reply(host) == _USAGE
    assert [c for c in host.kv_calls if c[0] in {"set", "increment"}] == []


@pytest.mark.parametrize(
    ("player", "bot", "counter"),
    [("rock", "scissors", _wins_key), ("rock", "paper", _losses_key), ("rock", "rock", _ties_key)],
)
def test_exactly_one_outcome_counter_moves_per_round_and_it_is_durable(
    host: _FakeHost, monkeypatch: pytest.MonkeyPatch, player: str, bot: str, counter: Any
) -> None:
    _force_bot_choice(monkeypatch, bot)
    _go("play", choice=player)

    pseudonym = _expected_pseudonym("viewer-1")
    moved = {
        name: _scoped(fn(pseudonym)) in host.store
        for name, fn in (("w", _wins_key), ("l", _losses_key), ("t", _ties_key))
    }
    assert sum(moved.values()) == 1 and host.store[_scoped(counter(pseudonym))] == b"1"
    incs = [c for c in host.kv_calls if c[0] == "increment"]
    assert [c[3] for c in incs] == [0]


def test_records_are_isolated_per_community(
    host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_bot_choice(monkeypatch, "scissors")
    for community in ("comm-a", "comm-b"):
        _run(
            dispatch(
                _sample_envelope("twitch", "play", community=community, choice="rock"),
                {},
                http_client=None,
            )
        )
    key = _wins_key(_expected_pseudonym("viewer-1"))
    assert host.store[_scoped(key, "comm-a")] == b"1" and host.store[_scoped(key, "comm-b")] == b"1"


def test_every_kv_key_written_passes_the_host_charset_check(
    host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    """regression: gh-631 -- `waddle_sdk.kv.validate_key` rejects `:`; assert none was written."""
    _force_bot_choice(monkeypatch, "rock")
    _go("config_set_cooldown", arg="cooldown 5", is_mod=True)
    _go("play", choice="paper")
    _go("list")
    keys = {c[1] for c in host.kv_calls}
    assert keys
    for key in keys:
        validate_key(key)
    assert not any(":" in key for key in keys)


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
    _force_bot_choice(monkeypatch, "rock")

    with pytest.raises(RuntimeError, match=f"rps kv {kind} failed: _ErrorBackend"):
        _go("play", choice="rock", actor=f"{CANARY}_actor")

    errors = [(m, json.loads(f)) for lvl, m, f in state.log_calls if lvl == ERROR_LEVEL]
    assert errors == [("rps.kv_error", {"op": kind, "error": "_ErrorBackend"})]
    assert "temporarily unavailable" in _reply(state)


# -- flag fail-closed
def test_flag_is_requested_with_a_fail_closed_default(host: _FakeHost) -> None:
    seen: list[tuple[str, bool]] = []
    sys.modules["wit_world"].imports.flags.enabled = (
        lambda key, default_value: seen.append((key, default_value)) or default_value
    )
    assert _run(transform(_sample_event("!rps rock"))) is None
    assert seen == [(FLAG_KEY, False)]
    assert FLAG_KEY == "waddles.command-rps"


def test_missing_wit_world_keeps_the_command_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "wit_world", None)
    assert _run(transform(_sample_event("!rps rock"))) is None


def test_flag_off_does_no_io_and_logs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _FakeHost()
    _install(monkeypatch, state, flag_enabled=False)
    assert _run(transform(_sample_event(f"!rps {CANARY}", is_mod=True))) is None
    assert state.log_calls == [] and state.kv_calls == []


# -- PII-free logs
def test_no_log_line_in_any_flow_contains_typed_text_or_the_raw_actor(
    host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PII-free-log regression: typed moves / arguments / grammar text and the actor never log."""
    actor = f"{CANARY}_actor"
    for text in (
        f"!rps {CANARY}",
        f"!rps list {CANARY}",
        f"!rps set cooldown {CANARY}",
        f"!rps {CANARY} {CANARY}",
        f"!rps enable {CANARY}",
        "!rps rock",
        "!rps list",
    ):
        _run(transform(_sample_event(text, is_mod=True)))

    _force_bot_choice(monkeypatch, "rock")
    _go("play", choice="paper", actor=actor)
    _go("play", choice="paper", actor=actor)  # cooldown path
    _go("play", choice=CANARY, actor=actor)  # invalid-move path
    _go("list", actor=actor)
    _go("config_set_cooldown", arg=f"cooldown {CANARY}", actor=actor, is_mod=True)  # debug path
    _go("config_set_cooldown", arg="cooldown 5", actor=actor)  # denied path
    host.store[_scoped(CONFIG_KEY)] = b"\xff-garbage"  # corrupt-config logging path
    _go("play", choice="paper", actor=f"{actor}2")

    assert host.log_calls, "expected log lines (denominator must be non-zero)"
    for _lvl, message, fields_json in host.log_calls:
        assert CANARY.lower() not in message.lower()
        assert CANARY.lower() not in fields_json.lower()
    for key, value in host.store.items():
        assert CANARY.lower() not in key.lower()
        assert CANARY.lower().encode() not in value.lower()


def test_lastplay_key_is_pseudonymous() -> None:
    key = _lastplay_key(_expected_pseudonym(CANARY))
    assert CANARY not in key and key.startswith("rps.lastplay.")


def test_entry_wiring_exports_the_stage_functions() -> None:
    assert _entry_wiring.bundle_transform is app.transform
    assert _entry_wiring.bundle_dispatch is app.dispatch
    assert _entry_wiring.__all__ == ["bundle_dispatch", "bundle_transform"]
