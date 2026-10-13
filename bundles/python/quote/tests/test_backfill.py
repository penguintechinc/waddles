"""Backfill coverage for the `quote` bundle: backend fail-loud, corrupt stores, mod gate, PII logs.

Complements `test_app.py` with what it does not pin: every `kv`/`db` backend-failure path
(`kv_get`/`kv_set`/`kv_delete`/`kv_increment`/`db_get`/`db_delete`) is loud (ERROR log with
exception *type* only + generic chat reply + `RuntimeError`), corrupt kv/db state raises instead of
being papered over, the add/remove mod gate fails closed with zero side effects, no log line ever
carries a user-typed value, and the static `_entry_wiring` re-export.

Reuses `test_app.py`'s fake host (`_install`) so both modules exercise the same shared
charset-enforcing `kv` fake + structured `db` fake.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import pytest
import wit_fake_db
from test_app import _FakeHost, _install, _sample_envelope, _sample_event, _scoped

import _entry_wiring
import app
from app import (
    _KNOWN_COMMANDS,
    _PERMISSION_DENIED_ADD,
    _PERMISSION_DENIED_REMOVE,
    _UNAVAILABLE_MSG,
    FLAG_KEY,
    MAX_QUOTE_LEN,
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


def _seed(host: _FakeHost, text: str = "hello world") -> None:
    """Add one quote as a moderator through the real `dispatch` path."""
    _run(dispatch(_sample_envelope("twitch", "add", arg=text, is_mod=True), {}, http_client=None))
    host.relay_calls.clear()
    host.log_calls.clear()


def _break_kv(monkeypatch: pytest.MonkeyPatch, op: str) -> None:
    """Make the `kv` host import's `op` raise, leaving the other kv ops intact."""
    import wit_world  # noqa: PLC0415 - installed into sys.modules by `_install`

    def _boom(*_args: Any) -> Any:
        raise RuntimeError(f"kv {op} down")

    monkeypatch.setattr(wit_world.imports.kv, op, _boom)


def _error_logs(host: _FakeHost) -> list[dict[str, Any]]:
    return [
        {"msg": msg, **json.loads(fields)}
        for lvl, msg, fields in host.log_calls
        if lvl == ERROR_LEVEL
    ]


# -- backend failures are fail-loud (ERROR log w/ type only, generic reply, RuntimeError)
@pytest.mark.parametrize(
    ("kv_op", "command", "arg", "seed", "expected_op"),
    [
        ("get", "get", "1", True, "kv_get"),
        ("get", "remove", "1", True, "kv_get"),
        ("set", "add", "new quote", False, "kv_set"),
        ("increment", "add", "new quote", False, "kv_increment"),
        ("delete", "remove", "1", True, "kv_delete"),
    ],
)
def test_kv_backend_failure_is_loud_and_leaks_nothing(
    host: _FakeHost,
    monkeypatch: pytest.MonkeyPatch,
    kv_op: str,
    command: str,
    arg: str,
    seed: bool,
    expected_op: str,
) -> None:
    if seed:
        _seed(host)
    _break_kv(monkeypatch, kv_op)

    with pytest.raises(RuntimeError, match=f"quote {expected_op} failed: RuntimeError"):
        _run(
            dispatch(
                _sample_envelope("twitch", command, arg=arg, is_mod=True), {}, http_client=None
            )
        )

    assert _reply(host) == _UNAVAILABLE_MSG
    assert _error_logs(host) == [
        {"msg": "quote.backend_error", "op": expected_op, "error": "RuntimeError"}
    ]


def test_kv_increment_failure_never_touches_the_db(
    host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seq counter is taken first, so a counter outage inserts no orphan row."""
    _break_kv(monkeypatch, "increment")
    with pytest.raises(RuntimeError, match="kv_increment"):
        _run(
            dispatch(_sample_envelope("twitch", "add", arg="x", is_mod=True), {}, http_client=None)
        )
    assert host.db.calls == []


@pytest.mark.parametrize(
    ("db_op", "command", "error"),
    [
        ("get", "get", RuntimeError("db get down")),
        ("get", "remove", RuntimeError("db get down")),
        ("delete", "remove", wit_fake_db.WitDbError(wit_fake_db.Error_Conflict("stale version"))),
    ],
)
def test_db_backend_failure_is_loud_and_names_only_the_exception_type(
    host: _FakeHost, db_op: str, command: str, error: Exception
) -> None:
    _seed(host)
    host.db.raise_on[db_op] = error

    with pytest.raises(RuntimeError, match=f"quote db_{db_op} failed"):
        _run(
            dispatch(
                _sample_envelope("twitch", command, arg="1", is_mod=True), {}, http_client=None
            )
        )

    assert _reply(host) == _UNAVAILABLE_MSG
    [log_line] = _error_logs(host)
    assert log_line["op"] == f"db_{db_op}"
    assert log_line["error"] in {"DbError", "ConflictError"}
    assert "stale version" not in json.dumps(log_line)  # exception *message* never logged


def test_delete_conflict_keeps_the_kv_index_so_the_quote_stays_addressable(
    host: _FakeHost,
) -> None:
    """A lost optimistic-concurrency race must not drop the index of a still-present row."""
    _seed(host)
    host.db.raise_on["delete"] = wit_fake_db.WitDbError(wit_fake_db.Error_Conflict("v"))
    with pytest.raises(RuntimeError):
        _run(
            dispatch(
                _sample_envelope("twitch", "remove", arg="1", is_mod=True), {}, http_client=None
            )
        )

    assert _scoped("quote.rowid.1") in host.kv.store
    assert len(host.db.rows) == 1


def test_remove_with_stale_index_is_loud_not_a_silent_not_found(host: _FakeHost) -> None:
    """Index points at a row that no longer exists -> `index_stale`, never `Quote #1 not found.`"""
    _seed(host)
    host.db.rows.clear()
    host.db.versions.clear()

    with pytest.raises(RuntimeError, match="quote index_stale failed"):
        _run(
            dispatch(
                _sample_envelope("twitch", "remove", arg="1", is_mod=True), {}, http_client=None
            )
        )

    assert _reply(host) == _UNAVAILABLE_MSG
    assert host.db.calls[-1][0] == "get"  # never reached delete


def test_handle_get_without_a_target_is_a_programming_error() -> None:
    """`transform` only ever emits `get` with a digit arg; a missing one is a loud ValueError."""
    with pytest.raises(ValueError, match="no target sequence number"):
        _run(app._handle_get("comm-1", None, provider="twitch", channel_id="c"))


# -- corrupt stores raise instead of being papered over
def test_corrupt_non_utf8_kv_index_raises_and_never_replies(host: _FakeHost) -> None:
    _seed(host)
    host.kv.store[_scoped("quote.rowid.1")] = b"\xff\xfe\xfd"

    with pytest.raises(UnicodeDecodeError):
        _run(dispatch(_sample_envelope("twitch", "get", arg="1"), {}, http_client=None))
    assert host.relay_calls == []


@pytest.mark.parametrize("command", ["get", "random", "list"])
def test_db_row_missing_the_seq_column_raises_loudly(host: _FakeHost, command: str) -> None:
    _seed(host)
    [row_id] = host.db.rows
    host.db.rows[row_id] = {"quote_text": "orphaned column set"}

    with pytest.raises(KeyError, match="seq"):
        _run(
            dispatch(
                _sample_envelope("twitch", command, arg="1" if command == "get" else None),
                {},
                http_client=None,
            )
        )
    assert host.relay_calls == []


def test_db_row_with_a_non_numeric_seq_raises_loudly(host: _FakeHost) -> None:
    _seed(host)
    [row_id] = host.db.rows
    host.db.rows[row_id]["seq"] = "not-a-number"

    with pytest.raises(ValueError):
        _run(dispatch(_sample_envelope("twitch", "random"), {}, http_client=None))
    assert host.relay_calls == []


# -- mod gate fails closed with zero side effects
@pytest.mark.parametrize("command", ["add", "remove"])
@pytest.mark.parametrize(
    ("is_mod", "is_broadcaster"),
    [(None, None), (False, None), (None, False), (False, False)],
    ids=["no-badges", "mod-false", "broadcaster-false", "both-false"],
)
def test_privileged_commands_are_denied_without_a_true_badge(
    host: _FakeHost, command: str, is_mod: bool | None, is_broadcaster: bool | None
) -> None:
    _seed(host)
    host.kv.calls.clear()
    host.db.calls.clear()

    result = _run(
        dispatch(
            _sample_envelope(
                "twitch",
                command,
                arg="1" if command == "remove" else "sneaky",
                is_mod=is_mod,
                is_broadcaster=is_broadcaster,
            ),
            {},
            http_client=None,
        )
    )

    expected = _PERMISSION_DENIED_ADD if command == "add" else _PERMISSION_DENIED_REMOVE
    assert _reply(host) == expected
    assert result.detail == command
    assert host.kv.calls == [] and host.db.calls == []  # no read, no write
    assert len(host.db.rows) == 1


@pytest.mark.parametrize("badge", ["is_mod", "is_broadcaster"])
def test_either_true_badge_grants_the_privileged_commands(host: _FakeHost, badge: str) -> None:
    kwargs: dict[str, Any] = {badge: True}
    _run(dispatch(_sample_envelope("twitch", "add", arg="allowed", **kwargs), {}, http_client=None))
    assert _reply(host) == "Saved as quote #1."
    _run(dispatch(_sample_envelope("twitch", "remove", arg="1", **kwargs), {}, http_client=None))
    assert _reply(host) == "Removed quote #1."


@pytest.mark.parametrize("command", ["random", "list"])
def test_reads_are_open_to_anyone_with_no_badges(host: _FakeHost, command: str) -> None:
    _seed(host, "anyone may read")
    _run(dispatch(_sample_envelope("twitch", command), {}, http_client=None))
    assert "#1" in _reply(host)


def test_add_length_boundary_is_inclusive(host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope("twitch", "add", arg="x" * MAX_QUOTE_LEN, is_mod=True),
            {},
            http_client=None,
        )
    )
    assert _reply(host) == "Saved as quote #1."
    _run(
        dispatch(
            _sample_envelope("twitch", "add", arg="x" * (MAX_QUOTE_LEN + 1), is_mod=True),
            {},
            http_client=None,
        )
    )
    assert "500 characters or fewer" in _reply(host)
    assert len(host.db.rows) == 1


def test_add_strips_surrounding_whitespace_before_storing(host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope("twitch", "add", arg="   padded   ", is_mod=True), {}, http_client=None
        )
    )
    [row] = host.db.rows.values()
    assert row["quote_text"] == "padded"


def test_ids_are_scoped_per_community_and_never_collide(host: _FakeHost) -> None:
    for community in ("comm-a", "comm-b"):
        _run(
            dispatch(
                _sample_envelope("twitch", "add", community=community, arg=community, is_mod=True),
                {},
                http_client=None,
            )
        )
    assert _scoped("quote.rowid.1", "comm-a") in host.kv.store
    assert _scoped("quote.rowid.1", "comm-b") in host.kv.store
    assert _scoped("quote.seq.counter", "comm-a") != _scoped("quote.seq.counter", "comm-b")


def test_every_kv_key_written_passes_the_host_charset_check(host: _FakeHost) -> None:
    """regression: gh-631 -- `:` in a kv key is rejected by the host (`FakeKvHost` enforces)."""
    _seed(host)
    _run(dispatch(_sample_envelope("twitch", "remove", arg="1", is_mod=True), {}, http_client=None))
    keys = {key for _op, (key, *_rest) in host.kv.calls}
    assert keys and not any(":" in key for key in keys)


# -- flag fail-closed
def test_flag_is_requested_with_a_fail_closed_default(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, bool]] = []
    _install(monkeypatch)
    import wit_world  # noqa: PLC0415 - installed into sys.modules by `_install`

    wit_world.imports.flags.enabled = (
        lambda key, default_value: seen.append((key, default_value)) or default_value
    )
    assert _run(transform(_sample_event("!quote random"))) is None
    assert seen == [(FLAG_KEY, False)]
    assert FLAG_KEY == "waddles.command-quote"


def test_missing_wit_world_keeps_the_command_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "wit_world", None)
    assert _run(transform(_sample_event("!quote random"))) is None


def test_flag_off_logs_nothing_and_does_no_io(monkeypatch: pytest.MonkeyPatch) -> None:
    off = _install(monkeypatch, flag_enabled=False)
    assert _run(transform(_sample_event(f"!quote add {CANARY}", is_mod=True))) is None
    assert off.log_calls == [] and off.kv.calls == [] and off.db.calls == []


def test_known_commands_cover_every_command_transform_can_emit(host: _FakeHost) -> None:
    texts = {
        "add": "!quote add x",
        "get": "!quote 3",
        "random": "!quote random",
        "list": "!quote list",
        "remove": "!quote remove 1",
        "usage": "!quote",
        "unknown": "!quote set foo",
    }
    for expected, text in texts.items():
        out = _run(transform(_sample_event(text)))
        assert out is not None and out.payload["command"] == expected
        assert expected in _KNOWN_COMMANDS


# -- PII-free logs
def test_no_log_line_in_any_flow_contains_user_typed_text(host: _FakeHost) -> None:
    """PII-free-log regression: quote text, ids, targets and the actor never reach the sink."""
    flows = [
        ("add", {"arg": f"{CANARY} quote body"}),
        ("add", {"arg": ""}),
        ("get", {"arg": "1"}),
        ("get", {"arg": "99"}),
        ("random", {}),
        ("list", {}),
        ("remove", {"arg": "99"}),
        ("remove", {"arg": f"{CANARY}"}),
        ("remove", {"arg": "1"}),
        ("usage", {"arg": f"{CANARY} bad grammar"}),
        ("unknown", {"raw_option": CANARY}),
    ]
    for command, kwargs in flows:
        _run(
            dispatch(
                _sample_envelope("twitch", command, actor=f"{CANARY}_actor", is_mod=True, **kwargs),
                {},
                http_client=None,
            )
        )
    # transform-side: raw text (incl. a malformed grammar) never logged either
    for text in (f"!quote add {CANARY}", f"!quote {CANARY}", f"!quote remove {CANARY}"):
        _run(transform(_sample_event(text, is_mod=True)))

    assert host.log_calls, "expected log lines (denominator must be non-zero)"
    for _level, message, fields_json in host.log_calls:
        assert CANARY.lower() not in message.lower()
        assert CANARY.lower() not in fields_json.lower()


def test_entry_wiring_exports_the_stage_functions() -> None:
    assert _entry_wiring.bundle_transform is app.transform
    assert _entry_wiring.bundle_dispatch is app.dispatch
    assert _entry_wiring.__all__ == ["bundle_dispatch", "bundle_transform"]
