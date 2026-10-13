"""Backfill coverage for the `social` bundle: target edges, counter hygiene, PII-free logs, wiring.

`test_app.py` already drives `src/` to ~100%; this module pins the invariants that number cannot:
the target shape boundaries (32/33 chars, `@@x`, charset), that self / unknown-target replies
touch no state, exactly one given + one received counter per resolved interaction (durable, per
community, pseudonymous), that one flag gates all four commands, corrupt-counter loudness, every
kv error log carrying only op + exception type, a PII-free-log regression for typed targets and
the raw actor (the *reply* may echo the target; logs and keys may not), and the static
`_entry_wiring` re-export. `social` has no moderator-gated verb, so there is no mod-gate matrix.

Reuses `test_app.py`'s fake host (`_install`: shared charset-enforcing `kv` + flags/relay/log).
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
    _INTERACTIONS,
    _NO_RECORD_YET,
    _SOCIAL_USAGE,
    FLAG_KEY,
    _normalize_target,
    _pseudonym,
    _wins_style_key,
    dispatch,
    transform,
)

CANARY = "CANARYuser9f3a"
ERROR_LEVEL = 0
NAMES = tuple(_INTERACTIONS)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> _FakeHost:
    """A fresh fake host with the flag ON."""
    return _install(monkeypatch)


def _interact(interaction: str, target: str, **kwargs: Any) -> Any:
    return _run(
        dispatch(
            _sample_envelope(
                "twitch", "interact", interaction=interaction, target=target, **kwargs
            ),
            {},
            http_client=None,
        )
    )


def _reply(host: _FakeHost) -> str:
    return str(json.loads(host.relay_calls[-1][1])["text"])


# -- target shape boundaries
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a", "a"),
        ("@a", "a"),
        ("_u", "_u"),
        ("dot.name-ok", "dot.name-ok"),
        ("A" * 32, "A" * 32),
        ("@" + "A" * 32, "A" * 32),
        ("", None),
        ("@", None),
        ("@@x", None),
        ("-lead", None),
        (".lead", None),
        ("A" * 33, None),
        ("bad name", None),
        ("emoji\U0001f427", None),
    ],
)
def test_normalize_target_shape_boundaries(raw: str, expected: str | None) -> None:
    assert _normalize_target(raw) == expected


@pytest.mark.parametrize("interaction", NAMES)
@pytest.mark.parametrize("bad", ["@@x", "A" * 33, "-x", "x!", "x/y"])
def test_invalid_targets_reply_unknown_and_touch_no_state(
    host: _FakeHost, interaction: str, bad: str
) -> None:
    result = _interact(interaction, bad)

    assert result.detail == f"interact:{interaction}:unknown_target"
    assert f"I don't know who '{bad}' is" in _reply(host)
    assert host.kv.calls == []


@pytest.mark.parametrize("interaction", NAMES)
@pytest.mark.parametrize("target", ["Viewer-1", "viewer-1", "@VIEWER-1"])
def test_self_target_is_case_insensitive_and_touches_no_state(
    host: _FakeHost, interaction: str, target: str
) -> None:
    result = _interact(interaction, target)

    assert result.detail == f"interact:{interaction}:self"
    assert _reply(host) == _INTERACTIONS[interaction].self_message.format(
        giver="viewer-1", emoji=_INTERACTIONS[interaction].emoji
    )
    assert host.kv.calls == []


def test_a_missing_actor_is_never_treated_as_self(host: _FakeHost) -> None:
    result = _interact("hug", "someone", actor=None)
    assert result.detail == "interact:hug:resolved"
    hug = _INTERACTIONS["hug"]
    assert _reply(host) != hug.self_message.format(giver="someone", emoji=hug.emoji)
    assert len(host.store) == 2  # one given + one received counter were written


# -- counters: exactly one given + one received, durable, pseudonymous
@pytest.mark.parametrize("interaction", NAMES)
def test_resolved_interaction_moves_exactly_one_given_and_one_received(
    host: _FakeHost, interaction: str
) -> None:
    result = _interact(interaction, "@Bob")

    assert result.detail == f"interact:{interaction}:resolved"
    giver = _scoped(_wins_style_key(interaction, "given", _pseudonym("viewer-1")))
    received = _scoped(_wins_style_key(interaction, "received", _pseudonym("bob")))
    assert host.store == {giver: b"1", received: b"1"}
    incs = [c for c in host.kv.calls if c[0] == "increment"]
    assert len(incs) == 2 and all(c[1][2] == 0 for c in incs), "counters must be durable (ttl 0)"


@pytest.mark.parametrize("interaction", NAMES)
def test_reply_names_both_participants_and_uses_a_known_template(
    host: _FakeHost, interaction: str
) -> None:
    spec = _INTERACTIONS[interaction]
    _interact(interaction, "Bob")
    assert _reply(host) in {
        t.format(giver="viewer-1", target="Bob", emoji=spec.emoji) for t in spec.templates
    }


def test_interactions_have_independent_counters_and_communities_do_not_leak(
    host: _FakeHost,
) -> None:
    _interact("hug", "bob")
    _interact("hug", "bob")
    _interact("pat", "bob")
    _interact("hug", "bob", community="comm-2")

    key = lambda i, d, p, c="comm-1": _scoped(_wins_style_key(i, d, _pseudonym(p)), c)  # noqa: E731
    assert host.store[key("hug", "given", "viewer-1")] == b"2"
    assert host.store[key("pat", "given", "viewer-1")] == b"1"
    assert host.store[key("hug", "received", "bob")] == b"2"
    assert host.store[key("hug", "given", "viewer-1", "comm-2")] == b"1"


def test_stats_sums_given_and_received_across_all_interactions(host: _FakeHost) -> None:
    _interact("hug", "bob")
    _interact("pat", "bob")
    _run(
        dispatch(
            _sample_envelope(
                "twitch", "interact", actor="bob", interaction="highfive", target="viewer-1"
            ),
            {},
            http_client=None,
        )
    )

    _run(dispatch(_sample_envelope("twitch", "stats"), {}, http_client=None))

    assert _reply(host) == (
        "Your social stats -- hug 1 given / 0 received, highfive 0 given / 1 received, "
        "pat 1 given / 0 received."
    )


def test_stats_with_no_activity_replies_the_no_record_message(host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "stats"), {}, http_client=None))
    assert _reply(host) == _NO_RECORD_YET
    assert {c[0] for c in host.kv.calls} == {"get"}


@pytest.mark.parametrize(
    ("interaction", "context"),
    [("hug", "stats_given_corrupt"), ("pat", "stats_given_corrupt")],
)
def test_corrupt_given_counter_logs_error_and_reads_as_zero(
    host: _FakeHost, interaction: str, context: str
) -> None:
    host.store[_scoped(_wins_style_key(interaction, "given", _pseudonym("viewer-1")))] = b"\xff-x"
    _run(dispatch(_sample_envelope("twitch", "stats"), {}, http_client=None))
    errors = [(m, json.loads(f)) for lvl, m, f in host.log_calls if lvl == ERROR_LEVEL]
    assert errors == [(f"social.{context}", {"community": "comm-1"})]
    assert _reply(host) == _NO_RECORD_YET


def test_corrupt_received_counter_logs_error_and_reads_as_zero(host: _FakeHost) -> None:
    host.store[_scoped(_wins_style_key("hug", "received", _pseudonym("viewer-1")))] = b"nope"
    _run(dispatch(_sample_envelope("twitch", "stats"), {}, http_client=None))
    errors = [(m, json.loads(f)) for lvl, m, f in host.log_calls if lvl == ERROR_LEVEL]
    assert errors == [("social.stats_received_corrupt", {"community": "comm-1"})]


def test_every_kv_key_written_passes_the_host_charset_check(host: _FakeHost) -> None:
    """regression: gh-631 -- the shared fake raises on `:`; assert none ever reached the store."""
    for interaction in NAMES:
        _interact(interaction, "bob")
    keys = {args[0] for _op, args in host.kv.calls}
    assert keys and not any(":" in key for key in keys)


# -- grammar edges
@pytest.mark.parametrize("text", ["!HUG bob", "  !highfive   bob  ", "!Pat @bob"])
def test_commands_are_case_insensitive_and_whitespace_tolerant(host: _FakeHost, text: str) -> None:
    out = _run(transform(_sample_event(text)))
    assert out is not None and out.payload["command"] == "interact"


@pytest.mark.parametrize("text", ["!hugs bob", "!high5 bob", "!socialize", "hug bob", "!patrick"])
def test_lookalike_commands_do_not_match(host: _FakeHost, text: str) -> None:
    assert _run(transform(_sample_event(text))) is None


def test_usage_text_names_the_interaction_or_falls_back_to_social(host: _FakeHost) -> None:
    for name in NAMES:
        _run(dispatch(_sample_envelope("twitch", "usage", interaction=name), {}, http_client=None))
        assert _reply(host) == f"Usage: !{name} <user>"
    _run(dispatch(_sample_envelope("twitch", "usage"), {}, http_client=None))
    assert _reply(host) == _SOCIAL_USAGE
    _run(dispatch(_sample_envelope("twitch", "usage", interaction="bogus"), {}, http_client=None))
    assert _reply(host) == _SOCIAL_USAGE


# -- backend failures: only op + exception type are logged
class _ErrorBackend:
    """Stand-in for the generated WIT `Error_Backend` variant case class."""


class _KvError(Exception):
    """Stand-in for the generated `Err` wrapper -- `.value` holds the error union member."""

    def __init__(self) -> None:
        self.value = _ErrorBackend()


@pytest.mark.parametrize(
    ("kwarg", "op", "command"),
    [("kv_increment_raises", "increment", "interact"), ("kv_get_raises", "get", "stats")],
)
def test_kv_failure_log_carries_only_op_and_exception_type(
    monkeypatch: pytest.MonkeyPatch, kwarg: str, op: str, command: str
) -> None:
    state = _install(monkeypatch, **{kwarg: _KvError()})
    envelope = (
        _sample_envelope(
            "twitch", "interact", interaction="hug", target=CANARY, actor=f"{CANARY}_actor"
        )
        if command == "interact"
        else _sample_envelope("twitch", "stats", actor=f"{CANARY}_actor")
    )

    with pytest.raises(RuntimeError, match=f"social kv {op} failed: _ErrorBackend"):
        _run(dispatch(envelope, {}, http_client=None))

    errors = [(m, json.loads(f)) for lvl, m, f in state.log_calls if lvl == ERROR_LEVEL]
    assert errors == [("social.kv_error", {"op": op, "error": "_ErrorBackend"})]
    assert "temporarily unavailable" in _reply(state)
    assert CANARY not in _reply(state)


# -- flag fail-closed
@pytest.mark.parametrize("text", ["!hug bob", "!highfive bob", "!pat bob", "!social stats"])
def test_flag_is_requested_with_a_fail_closed_default_for_every_command(
    host: _FakeHost, text: str
) -> None:
    seen: list[tuple[str, bool]] = []
    sys.modules["wit_world"].imports.flags.enabled = (
        lambda key, default_value: seen.append((key, default_value)) or default_value
    )
    assert _run(transform(_sample_event(text))) is None
    assert seen == [(FLAG_KEY, False)]
    assert FLAG_KEY == "waddles.command-social"


def test_missing_wit_world_keeps_every_command_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "wit_world", None)
    for text in ("!hug bob", "!social stats"):
        assert _run(transform(_sample_event(text))) is None


def test_flag_off_does_no_io_and_logs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install(monkeypatch, flag_enabled=False)
    assert _run(transform(_sample_event(f"!hug {CANARY}"))) is None
    assert state.log_calls == [] and state.kv.calls == []


# -- PII-free logs
def test_no_log_line_or_kv_key_contains_a_typed_target_or_the_raw_actor(host: _FakeHost) -> None:
    """PII-free-log regression: typed targets / grammar text / the actor never log or persist."""
    actor = f"{CANARY}_actor"
    for text in (
        f"!hug {CANARY}",
        f"!hug @{CANARY}",
        f"!pat {CANARY} {CANARY}",
        f"!highfive {CANARY}!!",
        f"!social {CANARY}",
        "!social stats",
        "!hug",
    ):
        _run(transform(_sample_event(text)))

    for name in NAMES:
        _interact(name, CANARY, actor=actor)  # resolved
        _interact(name, f"{CANARY}!!", actor=actor)  # unknown target
        _interact(name, actor, actor=actor)  # self
    _run(dispatch(_sample_envelope("twitch", "stats", actor=actor), {}, http_client=None))
    host.store[_scoped(_wins_style_key("hug", "given", _pseudonym(actor)))] = b"\xff-garbage"
    _run(dispatch(_sample_envelope("twitch", "stats", actor=actor), {}, http_client=None))

    assert host.log_calls, "expected log lines (denominator must be non-zero)"
    for _lvl, message, fields_json in host.log_calls:
        assert CANARY.lower() not in message.lower()
        assert CANARY.lower() not in fields_json.lower()
    for key in host.store:
        assert CANARY.lower() not in key.lower()


def test_entry_wiring_exports_the_stage_functions() -> None:
    assert _entry_wiring.bundle_transform is app.transform
    assert _entry_wiring.bundle_dispatch is app.dispatch
    assert _entry_wiring.__all__ == ["bundle_dispatch", "bundle_transform"]
