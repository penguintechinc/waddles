"""Backfill coverage for the `roulette` bundle: mod gate, cooldown TTLs, PII-free logs, wiring.

`test_app.py` already drives `src/` to ~100%; this module pins the invariants that number cannot:
the `set cooldown` mod gate fails closed across the whole badge matrix with zero kv traffic,
cooldown parsing/boundary/TTL semantics, per-community counter isolation, every kv error log
carrying only op + exception type, a PII-free-log regression across transform and dispatch paths
(typed arguments + the raw actor), the flag's fail-closed default, and the static `_entry_wiring`
re-export.

Reuses `test_app.py`'s fixtures (`fake_host`: shared charset-enforcing `kv` + flags/relay/log).
"""

# F811: pytest fixtures re-exported from `test_app.py` are re-bound by the test parameters.
# ruff: noqa: F811
from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import pytest
from test_app import (  # noqa: F401 - `fake_host` is a fixture re-exported for this module
    _break_kv,
    _expected_pseudonym,
    _FakeHost,
    _KvError,
    _sample_envelope,
    _sample_event,
    _scoped,
    fake_host,
)

import _entry_wiring
import app
from app import (
    _USAGE,
    DEFAULT_COOLDOWN_SECONDS,
    FLAG_KEY,
    MAX_COOLDOWN_SECONDS,
    MIN_COOLDOWN_SECONDS,
    _lastpull_key,
    _outs_key,
    _pulls_key,
    _survives_key,
    dispatch,
    transform,
)

CANARY = "CANARYuser9f3a"
ERROR_LEVEL = 0
CONFIG_KEY = "roulette.config.cooldown"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


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
    fake_host: _FakeHost, is_mod: bool | None, is_broadcaster: bool | None
) -> None:
    result = _go(
        "config_set_cooldown", arg="cooldown 5", is_mod=is_mod, is_broadcaster=is_broadcaster
    )

    assert result.detail == "config_set_cooldown:denied"
    assert _reply(fake_host) == "only moderators/broadcasters can configure !roulette"
    assert fake_host.kv.calls == [] and fake_host.store == {}


@pytest.mark.parametrize("badge", ["is_mod", "is_broadcaster"])
def test_either_true_badge_may_set_the_cooldown(fake_host: _FakeHost, badge: str) -> None:
    _go("config_set_cooldown", arg="cooldown 45", **{badge: True})
    assert _reply(fake_host) == "roulette cooldown set to 45s"
    assert fake_host.store[_scoped(CONFIG_KEY)] == b"45"


@pytest.mark.parametrize("command", ["pull", "list", "usage"])
def test_pull_list_and_usage_need_no_badge(fake_host: _FakeHost, command: str) -> None:
    assert _go(command).detail == command


# -- cooldown parsing, bounds, TTL
@pytest.mark.parametrize(
    ("arg", "stored"),
    [
        ("cooldown 0", b"0"),
        ("cooldown 3600", b"3600"),
        ("COOLDOWN 30", b"30"),
        ("cooldown +7", b"7"),
        ("cooldown 1_0", b"10"),
        ("cooldown  12", b"12"),
    ],
)
def test_valid_cooldown_arguments_are_stored_as_canonical_ints(
    fake_host: _FakeHost, arg: str, stored: bytes
) -> None:
    _go("config_set_cooldown", arg=arg, is_mod=True)
    assert fake_host.store[_scoped(CONFIG_KEY)] == stored


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
    fake_host: _FakeHost, arg: str
) -> None:
    _go("config_set_cooldown", arg=arg, is_mod=True)
    assert [c for c in fake_host.kv.calls if c[0] == "set"] == []
    assert _scoped(CONFIG_KEY) not in fake_host.store
    assert MIN_COOLDOWN_SECONDS == 0 and MAX_COOLDOWN_SECONDS == 3600


def test_set_cooldown_without_an_arg_payload_replies_usage(fake_host: _FakeHost) -> None:
    _go("config_set_cooldown", is_mod=True)
    assert _reply(fake_host) == _USAGE


def test_lastpull_ttl_equals_the_configured_cooldown_and_default_otherwise(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app._pull_trigger", lambda: False)
    _go("pull")
    default_ttl = [
        args[2]
        for op, args in fake_host.kv.calls
        if op == "set" and args[0].endswith(_lastpull_key(_expected_pseudonym("viewer-1")))
    ]
    assert default_ttl == [DEFAULT_COOLDOWN_SECONDS]

    fake_host.kv.calls.clear()
    _go("config_set_cooldown", arg="cooldown 120", is_mod=True)
    fake_host.advance(DEFAULT_COOLDOWN_SECONDS + 1)
    _go("pull", actor="someone-else")
    ttls = [args[2] for op, args in fake_host.kv.calls if op == "set" and "lastpull" in args[0]]
    assert ttls == [120]


def test_counters_have_no_ttl_and_are_per_community(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app._pull_trigger", lambda: False)
    for community in ("comm-a", "comm-b", "comm-a"):
        _run(
            dispatch(_sample_envelope("twitch", "pull", community=community), {}, http_client=None)
        )
        fake_host.advance(DEFAULT_COOLDOWN_SECONDS + 1)

    pseudonym = _expected_pseudonym("viewer-1")
    assert fake_host.store[_scoped(_pulls_key(pseudonym), "comm-a")] == b"2"
    assert fake_host.store[_scoped(_pulls_key(pseudonym), "comm-b")] == b"1"
    incs = [args for op, args in fake_host.kv.calls if op == "increment"]
    assert incs and all(args[2] == 0 for args in incs)


@pytest.mark.parametrize(
    ("out", "hit_key", "miss_key"),
    [
        (True, _outs_key, _survives_key),
        (False, _survives_key, _outs_key),
    ],
)
def test_exactly_one_outcome_counter_moves_per_pull(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch, out: bool, hit_key: Any, miss_key: Any
) -> None:
    monkeypatch.setattr("app._pull_trigger", lambda: out)
    _go("pull")
    pseudonym = _expected_pseudonym("viewer-1")
    assert fake_host.store[_scoped(hit_key(pseudonym))] == b"1"
    assert _scoped(miss_key(pseudonym)) not in fake_host.store
    assert fake_host.store[_scoped(_pulls_key(pseudonym))] == b"1"


def test_every_kv_key_written_passes_the_host_charset_check(fake_host: _FakeHost) -> None:
    """regression: gh-631 -- the shared fake raises on `:`; assert none ever reached the store."""
    _go("config_set_cooldown", arg="cooldown 5", is_mod=True)
    _go("pull")
    _go("list")
    keys = {args[0] for _op, args in fake_host.kv.calls}
    assert keys and not any(":" in key for key in keys)


# -- backend failures: only op + exception type are logged
@pytest.mark.parametrize("op", ["get", "set", "increment"])
def test_kv_failure_log_carries_only_op_and_exception_type(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch, op: str
) -> None:
    _break_kv(monkeypatch, op, _KvError())

    with pytest.raises(RuntimeError, match=f"roulette kv {op} failed: _ErrorBackend"):
        _go("pull", actor=f"{CANARY}_actor")

    errors = [(m, json.loads(f)) for lvl, m, f in fake_host.log_calls if lvl == ERROR_LEVEL]
    assert errors == [("roulette.kv_error", {"op": op, "error": "_ErrorBackend"})]
    assert "jammed" in _reply(fake_host)


# -- flag fail-closed
def test_flag_is_requested_with_a_fail_closed_default(fake_host: _FakeHost) -> None:
    seen: list[tuple[str, bool]] = []
    sys.modules["wit_world"].imports.flags.enabled = (
        lambda key, default_value: seen.append((key, default_value)) or default_value
    )
    assert _run(transform(_sample_event("!roulette"))) is None
    assert seen == [(FLAG_KEY, False)]
    assert FLAG_KEY == "waddles.command-roulette"


def test_missing_wit_world_keeps_the_command_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "wit_world", None)
    assert _run(transform(_sample_event("!roulette"))) is None


def test_flag_off_does_no_io_and_logs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    from test_app import _attach_non_kv_fakes
    from waddle_sdk.testing import install_fake_kv_host

    kv = install_fake_kv_host(monkeypatch)
    host = _FakeHost(kv)
    _attach_non_kv_fakes(host, flag_enabled=False)
    assert _run(transform(_sample_event(f"!roulette set cooldown {CANARY}", is_mod=True))) is None
    assert host.log_calls == [] and kv.calls == []


# -- PII-free logs
def test_no_log_line_in_any_flow_contains_typed_text_or_the_raw_actor(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PII-free-log regression: typed arguments / grammar text and the raw actor never log."""
    actor = f"{CANARY}_actor"
    for text in (
        f"!roulette {CANARY}",
        f"!roulette list {CANARY}",
        f"!roulette set cooldown {CANARY}",
        f"!roulette set {CANARY} 5",
        f"!roulette enable {CANARY}",
        "!roulette",
        "!roulette list",
    ):
        _run(transform(_sample_event(text, is_mod=True)))

    monkeypatch.setattr("app._pull_trigger", lambda: True)
    _go("pull", actor=actor)
    fake_host.advance(DEFAULT_COOLDOWN_SECONDS + 1)
    monkeypatch.setattr("app._pull_trigger", lambda: False)
    _go("pull", actor=actor)
    _go("list", actor=actor)
    _go("config_set_cooldown", arg=f"cooldown {CANARY}", actor=actor, is_mod=True)  # debug path
    _go("config_set_cooldown", arg="cooldown 5", actor=actor)  # denied path
    fake_host.store[_scoped(CONFIG_KEY)] = b"\xff-garbage"  # corrupt-config logging path
    _go("pull", actor=actor)

    assert fake_host.log_calls, "expected log lines (denominator must be non-zero)"
    for _lvl, message, fields_json in fake_host.log_calls:
        assert CANARY.lower() not in message.lower()
        assert CANARY.lower() not in fields_json.lower()
    for key, value in fake_host.store.items():
        assert CANARY.lower() not in key.lower()
        assert CANARY.lower().encode() not in value.lower()


def test_entry_wiring_exports_the_stage_functions() -> None:
    assert _entry_wiring.bundle_transform is app.transform
    assert _entry_wiring.bundle_dispatch is app.dispatch
    assert _entry_wiring.__all__ == ["bundle_dispatch", "bundle_transform"]
