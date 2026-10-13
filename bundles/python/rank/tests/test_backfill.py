"""Backfill coverage for the `rank` bundle: mod-gate matrix, PII-free logs, flag, wiring.

`test_app.py` already drives `src/app.py` to ~100%; this module pins the invariants that coverage
cannot: the add/sub mod gate fails closed across the whole badge matrix with zero kv/db traffic,
amount parsing edges, a PII-free-log regression covering transform *and* dispatch paths for typed
targets/amounts, the flag's fail-closed default, each backend error log carrying only op +
exception type, kv charset (gh-631), and the static `_entry_wiring` re-export.

Reuses `test_app.py`'s fake host (`_install`): shared charset-enforcing `kv` + structured `db`.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import pytest
from test_app import (
    _FakeHost,
    _install,
    _sample_envelope,
    _sample_event,
    _scoped,
)

import _entry_wiring
import app
from app import (
    _KNOWN_COMMANDS,
    _PERMISSION_DENIED_MSG,
    FLAG_KEY,
    _index_key,
    _pseudonym,
    _resolve_adjust,
    dispatch,
    transform,
)

CANARY = "CANARYuser9f3a"
ERROR_LEVEL = 0


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> _FakeHost:
    """A fresh fake host (flags ON, shared charset-enforcing kv, structured db fake)."""
    return _install(monkeypatch)


def _reply(host: _FakeHost) -> str:
    return str(json.loads(host.relay_calls[-1][1])["text"])


def _adjust(verb: str, target: str, amount: int, **kwargs: Any) -> Any:
    return _run(
        dispatch(
            _sample_envelope("twitch", verb, target=target, amount=amount, **kwargs),
            {},
            http_client=None,
        )
    )


# -- mod gate fails closed, zero side effects
@pytest.mark.parametrize("verb", ["add", "sub"])
@pytest.mark.parametrize(
    ("is_mod", "is_broadcaster"),
    [(None, None), (False, None), (None, False), (False, False)],
    ids=["no-badges", "mod-false", "broadcaster-false", "both-false"],
)
def test_adjust_is_denied_without_a_true_badge_and_does_no_kv_or_db_io(
    host: _FakeHost, verb: str, is_mod: bool | None, is_broadcaster: bool | None
) -> None:
    result = _adjust(verb, "alice", 50, is_mod=is_mod, is_broadcaster=is_broadcaster)

    assert result.detail == f"{verb}:denied"
    assert _reply(host) == _PERMISSION_DENIED_MSG
    assert host.kv.calls == [] and host.db.calls == []


@pytest.mark.parametrize("badge", ["is_mod", "is_broadcaster"])
def test_either_true_badge_may_add_then_sub(host: _FakeHost, badge: str) -> None:
    kwargs: dict[str, Any] = {badge: True}
    assert _adjust("add", "alice", 150, **kwargs).detail == "add"
    assert "level 2 (150 XP)" in _reply(host)
    assert _adjust("sub", "alice", 60, **kwargs).detail == "sub"
    assert "level 1 (90 XP)" in _reply(host)


@pytest.mark.parametrize("command", ["rank_self", "rank_other", "leaderboard", "usage"])
def test_reads_and_usage_need_no_badge(host: _FakeHost, command: str) -> None:
    result = _run(
        dispatch(
            _sample_envelope(
                "twitch", command, target="alice" if command == "rank_other" else None
            ),
            {},
            http_client=None,
        )
    )
    assert result.detail == command


def test_privileged_commands_are_exactly_add_and_sub() -> None:
    assert _KNOWN_COMMANDS - {"add", "sub"} == {"rank_self", "rank_other", "leaderboard", "usage"}


# -- amount parsing edges
@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ("5 bob", ("add", "bob", 5)),
        ("+5 bob", ("add", "bob", 5)),
        ("007 bob", ("add", "bob", 7)),
        ("1_000 bob", ("add", "bob", 1000)),
        ("0 bob", ("usage", None, None)),
        ("-5 bob", ("usage", None, None)),
        ("5.5 bob", ("usage", None, None)),
        ("five bob", ("usage", None, None)),
        ("5", ("usage", None, None)),
        ("5 bob extra", ("usage", None, None)),
        ("", ("usage", None, None)),
        (None, ("usage", None, None)),
        ("9" * 5000 + " bob", ("usage", None, None)),  # int() digit-limit ValueError -> usage
    ],
)
def test_adjust_amount_parsing_edges(
    args: str | None, expected: tuple[str, str | None, int | None], host: _FakeHost
) -> None:
    assert _resolve_adjust("add", args) == expected


def test_a_huge_but_parseable_amount_adds_exactly_without_float_rounding(host: _FakeHost) -> None:
    _adjust("add", "alice", 10**18, is_mod=True)
    [row] = host.db.rows.values()
    assert row["xp"] == 10**18 and isinstance(row["xp"], int)


def test_sub_never_drives_xp_negative_even_for_enormous_amounts(host: _FakeHost) -> None:
    _adjust("add", "alice", 10, is_mod=True)
    _adjust("sub", "alice", 10**18, is_mod=True)
    [row] = host.db.rows.values()
    assert row["xp"] == 0


# -- state hygiene
def test_row_stores_only_hash_and_xp_and_index_is_community_scoped(host: _FakeHost) -> None:
    _adjust("add", f"@{CANARY}", 5, is_mod=True)

    [row] = host.db.rows.values()
    assert set(row) == {"actor_hash", "xp"}
    assert row["actor_hash"] == _pseudonym(CANARY.lower())
    assert CANARY.lower() not in json.dumps(row).lower()
    assert _scoped(_index_key(_pseudonym(CANARY.lower()))) in host.kv.store
    for key in host.kv.store:
        assert CANARY.lower() not in key.lower()


def test_every_kv_key_written_passes_the_host_charset_check(host: _FakeHost) -> None:
    """regression: gh-631 -- the shared fake raises on `:`; assert none ever reached the store."""
    _adjust("add", "alice", 5, is_mod=True)
    _adjust("add", "alice", 5, is_mod=True)
    keys = {args[0] for _op, args in host.kv.calls}
    assert keys and not any(":" in key for key in keys)


def test_communities_have_independent_index_entries(host: _FakeHost) -> None:
    for community in ("comm-a", "comm-b"):
        _run(
            dispatch(
                _sample_envelope(
                    "twitch", "add", community=community, target="alice", amount=5, is_mod=True
                ),
                {},
                http_client=None,
            )
        )
    key = _index_key(_pseudonym("alice"))
    assert _scoped(key, "comm-a") in host.kv.store and _scoped(key, "comm-b") in host.kv.store
    assert host.kv.store[_scoped(key, "comm-a")] != host.kv.store[_scoped(key, "comm-b")]


# -- backend failures: only op + exception type are logged
@pytest.mark.parametrize(
    ("kv_op", "command", "kwargs"),
    [
        ("get", "rank_self", {}),
        ("set", "add", {"target": "alice", "amount": 5, "is_mod": True}),
    ],
)
def test_kv_failure_log_carries_only_op_and_exception_type(
    host: _FakeHost,
    monkeypatch: pytest.MonkeyPatch,
    kv_op: str,
    command: str,
    kwargs: dict[str, Any],
) -> None:
    def _boom(*_a: Any) -> Any:
        raise RuntimeError(f"backend echoed {CANARY}")

    import wit_world  # noqa: PLC0415 - installed into sys.modules by `_install`

    monkeypatch.setattr(wit_world.imports.kv, kv_op, _boom)

    with pytest.raises(RuntimeError, match=f"rank kv_{kv_op} failed: RuntimeError"):
        _run(dispatch(_sample_envelope("twitch", command, **kwargs), {}, http_client=None))

    errors = [(m, json.loads(f)) for lvl, m, f in host.log_calls if lvl == ERROR_LEVEL]
    assert errors == [("rank.backend_error", {"op": f"kv_{kv_op}", "error": "RuntimeError"})]
    assert CANARY not in _reply(host)


# -- flag fail-closed
def test_flag_is_requested_with_a_fail_closed_default(host: _FakeHost) -> None:
    seen: list[tuple[str, bool]] = []
    sys.modules["wit_world"].imports.flags.enabled = (
        lambda key, default_value: seen.append((key, default_value)) or default_value
    )
    assert _run(transform(_sample_event("!rank"))) is None
    assert seen == [(FLAG_KEY, False)]
    assert FLAG_KEY == "waddles.command-rank"


def test_missing_wit_world_keeps_the_command_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "wit_world", None)
    assert _run(transform(_sample_event("!rank"))) is None


def test_flag_off_does_no_io_and_logs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    off = _install(monkeypatch, flag_enabled=False)
    assert _run(transform(_sample_event(f"!rank add 5 {CANARY}", is_mod=True))) is None
    assert off.log_calls == [] and off.kv.calls == [] and off.db.calls == []


# -- PII-free logs
def test_no_log_line_in_any_flow_contains_a_typed_target_or_the_raw_actor(host: _FakeHost) -> None:
    """PII-free-log regression: typed targets / amounts / grammar text and the actor never log."""
    actor = f"{CANARY}_actor"
    for text in (
        f"!rank {CANARY}",
        f"!rank @{CANARY}",
        f"!rank add 5 {CANARY}",
        f"!rank add notanumber {CANARY}",
        f"!rank sub 5 {CANARY}",
        f"!rank add {CANARY}",
        f"!rank {CANARY} {CANARY}",
        "!rank top",
        "!rank",
    ):
        _run(transform(_sample_event(text, is_mod=True)))

    _adjust("add", CANARY, 200, actor=actor, is_mod=True)
    _adjust("sub", CANARY, 50, actor=actor, is_mod=True)
    _adjust("add", CANARY, 5, actor=actor)  # denied path
    for command, target in (("rank_self", None), ("rank_other", CANARY), ("leaderboard", None)):
        _run(
            dispatch(
                _sample_envelope("twitch", command, actor=actor, target=target),
                {},
                http_client=None,
            )
        )
    host.db.rows.clear()  # stale-index error path must be PII-free too
    host.db.versions.clear()
    with pytest.raises(RuntimeError):
        _run(
            dispatch(
                _sample_envelope("twitch", "rank_other", actor=actor, target=CANARY),
                {},
                http_client=None,
            )
        )

    assert host.log_calls, "expected log lines (denominator must be non-zero)"
    for _lvl, message, fields_json in host.log_calls:
        assert CANARY.lower() not in message.lower()
        assert CANARY.lower() not in fields_json.lower()


def test_entry_wiring_exports_the_stage_functions() -> None:
    assert _entry_wiring.bundle_transform is app.transform
    assert _entry_wiring.bundle_dispatch is app.dispatch
    assert _entry_wiring.__all__ == ["bundle_dispatch", "bundle_transform"]
