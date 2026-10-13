"""Backfill coverage for the `raffle` bundle: mod-gate matrix, corrupt-store shapes, PII-free logs.

`test_app.py` already drives `src/` to 100% line + branch; this module pins the invariants that
coverage number cannot: the open/close/draw mod gate fails closed across the whole badge matrix
with zero kv traffic, every corrupt-entrants shape raises loudly and is never overwritten, the
winner announcement discloses only the 8-hex pseudonym tag, the entrant-cap boundary, the flag's
fail-closed default, a PII-free-log regression for every command path, and that every kv key
written passes the real host charset (gh-631). (`_entry_wiring` is covered by
`test_entry_wiring.py`.)

Reuses `test_app.py`'s fixtures (`fake_kv` shared charset-enforcing fake + `fake_host` extras).
"""

# F811: pytest fixtures re-exported from `test_app.py` are re-bound by the test parameters.
# ruff: noqa: F811
from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import pytest
from test_app import (  # noqa: F401 - fixtures re-exported for this module
    _expected_pseudonym,
    _FakeExtras,
    _sample_envelope,
    _sample_event,
    _scoped,
    _set_kv_raises,
    fake_host,
    fake_kv,
)
from waddle_sdk.testing import FakeKvHost

import app
from app import (
    _NO_ENTRANTS_MSG,
    _NOT_OPEN_MSG,
    _PERMISSION_DENIED_MSG,
    FLAG_KEY,
    MAX_ENTRANTS,
    dispatch,
    transform,
)

CANARY = "CANARYuser9f3a"
ERROR_LEVEL = 0


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _go(command: str, **kwargs: Any) -> Any:
    return _run(dispatch(_sample_envelope("twitch", command, **kwargs), {}, http_client=None))


def _reply(host: _FakeExtras) -> str:
    return str(json.loads(host.relay_calls[-1][1])["text"])


# -- mod gate fails closed, zero side effects
@pytest.mark.parametrize("command", ["open", "close", "draw"])
@pytest.mark.parametrize(
    ("is_mod", "is_broadcaster"),
    [(None, None), (False, None), (None, False), (False, False)],
    ids=["no-badges", "mod-false", "broadcaster-false", "both-false"],
)
def test_privileged_commands_are_denied_without_a_true_badge_and_do_no_kv_io(
    fake_host: _FakeExtras,
    fake_kv: FakeKvHost,
    command: str,
    is_mod: bool | None,
    is_broadcaster: bool | None,
) -> None:
    result = _go(command, is_mod=is_mod, is_broadcaster=is_broadcaster)

    assert result.detail == f"{command}:denied"
    assert _reply(fake_host) == _PERMISSION_DENIED_MSG
    assert fake_kv.calls == [] and fake_kv.store == {}


@pytest.mark.parametrize("badge", ["is_mod", "is_broadcaster"])
def test_either_true_badge_runs_the_full_open_enter_draw_close_cycle(
    fake_host: _FakeExtras, badge: str
) -> None:
    _go("open", **{badge: True})
    _go("enter", actor="alice")
    _go("draw", **{badge: True})
    assert _reply(fake_host).startswith("\U0001f389 the winner is entrant ")
    assert _go("close", **{badge: True}).detail == "close"


@pytest.mark.parametrize("command", ["enter", "list", "usage"])
def test_enter_list_and_usage_need_no_badge(fake_host: _FakeExtras, command: str) -> None:
    assert _go(command).detail == command


# -- winner announcement discloses only the pseudonym tag
def test_winner_announcement_is_only_the_eight_hex_pseudonym_tag(fake_host: _FakeExtras) -> None:
    _go("open", is_mod=True)
    _go("enter", actor=f"{CANARY}_winner")

    _go("draw", is_mod=True)

    tag = _expected_pseudonym(f"{CANARY}_winner")[:8]
    assert _reply(fake_host) == (
        f"\U0001f389 the winner is entrant {tag}! DM a mod to claim your prize."
    )
    assert len(tag) == 8 and CANARY not in _reply(fake_host)


def test_draw_on_an_empty_raffle_never_picks_and_never_logs_a_win(
    fake_host: _FakeExtras, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(_seq: list[str]) -> str:
        raise AssertionError("no draw may happen with zero entrants")

    monkeypatch.setattr(app, "_pick_winner", _boom)
    _go("draw", is_mod=True)
    assert _reply(fake_host) == _NO_ENTRANTS_MSG
    assert not [m for _l, m, _f in fake_host.log_calls if m == "raffle.drawn"]


def test_reopening_after_a_draw_starts_a_fresh_round(fake_host: _FakeExtras) -> None:
    _go("open", is_mod=True)
    _go("enter", actor="alice")
    _go("draw", is_mod=True)
    _go("open", is_mod=True)
    _go("list")
    assert _reply(fake_host) == "the raffle is open with 0 entrant(s)."


def test_entry_cap_boundary_admits_the_last_slot_then_rejects(
    fake_host: _FakeExtras, fake_kv: FakeKvHost
) -> None:
    _go("open", is_mod=True)
    fake_kv.store[_scoped("raffle.entrants")] = json.dumps(
        [f"{i:064x}" for i in range(MAX_ENTRANTS - 1)]
    ).encode()

    _go("enter", actor="last")
    assert f"({MAX_ENTRANTS} entered)" in _reply(fake_host)
    _go("enter", actor="overflow")
    assert "full" in _reply(fake_host).lower()
    assert len(json.loads(fake_kv.store[_scoped("raffle.entrants")])) == MAX_ENTRANTS


def test_a_never_opened_raffle_rejects_entries_with_the_not_open_message(
    fake_host: _FakeExtras, fake_kv: FakeKvHost
) -> None:
    _go("enter", actor="alice")
    assert _reply(fake_host) == _NOT_OPEN_MSG
    assert _scoped("raffle.entrants") not in fake_kv.store


# -- corrupt store: every shape raises loudly and is never overwritten
@pytest.mark.parametrize(
    "blob",
    [b"\xff\xfe", b"{bad", b"{}", b'"s"', b"7", b"null", b'[1, "a"]', b'["a", null]'],
)
@pytest.mark.parametrize("command", ["list", "draw", "enter"])
def test_corrupt_entrants_blob_raises_replies_generic_error_and_is_never_overwritten(
    fake_host: _FakeExtras, fake_kv: FakeKvHost, blob: bytes, command: str
) -> None:
    fake_kv.store[_scoped("raffle.state")] = b"open"
    fake_kv.store[_scoped("raffle.entrants")] = blob

    with pytest.raises(RuntimeError, match="raffle corrupt state"):
        _go(command, is_mod=True)

    assert fake_kv.store[_scoped("raffle.entrants")] == blob
    assert "corrupted" in _reply(fake_host)
    errors = [m for lvl, m, _f in fake_host.log_calls if lvl == ERROR_LEVEL]
    assert errors == ["raffle.state_corrupt"]


@pytest.mark.parametrize(("op", "command"), [("get", "list"), ("set", "open"), ("get", "enter")])
def test_kv_backend_error_log_carries_only_op_and_exception_type(
    fake_host: _FakeExtras, monkeypatch: pytest.MonkeyPatch, op: str, command: str
) -> None:
    _set_kv_raises(monkeypatch, op=op)

    with pytest.raises(RuntimeError, match=f"raffle kv {op} failed: _ErrorBackend"):
        _go(command, is_mod=True, actor=f"{CANARY}_actor")

    errors = [(m, json.loads(f)) for lvl, m, f in fake_host.log_calls if lvl == ERROR_LEVEL]
    assert errors == [("raffle.kv_error", {"op": op, "error": "_ErrorBackend"})]
    assert "unavailable" in _reply(fake_host)


# -- kv charset (gh-631) + state hygiene
def test_every_kv_key_written_passes_the_host_charset_check(
    fake_host: _FakeExtras, fake_kv: FakeKvHost
) -> None:
    """regression: gh-631 -- the shared fake raises on `:`; assert none ever reached the store."""
    _go("open", is_mod=True)
    _go("enter", actor="alice")
    _go("close", is_mod=True)
    keys = {args[0] for _op, args in fake_kv.calls}
    assert keys and not any(":" in key for key in keys)


def test_state_and_entrants_are_durable_with_no_ttl(
    fake_host: _FakeExtras, fake_kv: FakeKvHost
) -> None:
    _go("open", is_mod=True)
    _go("enter", actor="alice")
    ttls = {args[0]: args[2] for op, args in fake_kv.calls if op == "set"}
    assert ttls[_scoped("raffle.state")] == 0 and ttls[_scoped("raffle.entrants")] == 0


def test_enter_alias_is_case_insensitive_and_shares_the_raffle_with_bare_raffle(
    fake_host: _FakeExtras,
) -> None:
    for text, expected in (("!ENTER", "enter"), ("!Raffle", "enter"), ("  !enter  ", "enter")):
        out = _run(transform(_sample_event(text)))
        assert out is not None and out.payload["command"] == expected


# -- flag fail-closed
def test_flag_is_requested_with_a_fail_closed_default(fake_host: _FakeExtras) -> None:
    seen: list[tuple[str, bool]] = []
    sys.modules["wit_world"].imports.flags.enabled = (
        lambda key, default_value: seen.append((key, default_value)) or default_value
    )
    assert _run(transform(_sample_event("!raffle"))) is None
    assert seen == [(FLAG_KEY, False)]
    assert FLAG_KEY == "waddles.command-raffle"


def test_missing_wit_world_keeps_the_command_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "wit_world", None)
    assert _run(transform(_sample_event("!raffle"))) is None
    assert _run(transform(_sample_event("!enter"))) is None


def test_flag_off_does_no_io_and_logs_nothing(fake_host: _FakeExtras, fake_kv: FakeKvHost) -> None:
    fake_host.flag_enabled = False
    assert _run(transform(_sample_event(f"!raffle {CANARY}", is_mod=True))) is None
    assert fake_host.log_calls == [] and fake_kv.calls == []


# -- PII-free logs
def test_no_log_line_in_any_flow_contains_typed_text_or_the_raw_actor(
    fake_host: _FakeExtras, fake_kv: FakeKvHost
) -> None:
    """PII-free-log regression: typed grammar text and the raw actor never reach the sink."""
    actor = f"{CANARY}_actor"
    for text in (
        f"!raffle {CANARY}",
        f"!raffle open {CANARY}",
        f"!raffle list {CANARY}",
        f"!enter {CANARY}",
        f"!raffle set {CANARY}",
        "!raffle",
        "!enter",
    ):
        _run(transform(_sample_event(text, is_mod=True)))
    _go("open", is_mod=True, actor=actor)
    _go("enter", actor=actor)
    _go("enter", actor=actor)  # duplicate-entry path
    _go("list", actor=actor)
    _go("draw", is_mod=True, actor=actor)
    _go("close", is_mod=True, actor=actor)
    _go("draw", actor=actor)  # denied path
    fake_kv.store[_scoped("raffle.entrants")] = b"garbage"
    with pytest.raises(RuntimeError):
        _go("list", actor=actor)  # corrupt-state error path

    assert fake_host.log_calls, "expected log lines (denominator must be non-zero)"
    for _lvl, message, fields_json in fake_host.log_calls:
        assert CANARY.lower() not in message.lower()
        assert CANARY.lower() not in fields_json.lower()
    for key, value in fake_kv.store.items():
        assert CANARY.lower() not in key.lower()
        assert CANARY.lower().encode() not in value.lower()
