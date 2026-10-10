"""Backfill coverage for the `rules` bundle: mod gate, corrupt store, bounds, PII-free logs.

Complements `test_app.py` with what it does not pin: the set/clear mod gate fails closed with
zero kv access across the whole badge matrix, a corrupt (non-UTF-8) stored value raises instead of
being papered over, the exact length boundary + whitespace trimming, the `clear`/`remove` alias and
case-insensitivity, durable (`ttl=0`) writes, the flag's fail-closed default, a log-hygiene
regression for rules text and the exception message, and the static `_entry_wiring` re-export.

Reuses `test_app.py`'s `fake_host` fixture (shared charset-enforcing `kv` fake + flags/relay/log).
"""

# F811: pytest fixtures re-exported from `test_app.py` are re-bound by the test parameters.
# ruff: noqa: F811
from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import _entry_wiring
import app
import pytest
from app import (
    _PERMISSION_DENIED_MSG,
    _RULES_KEY,
    _USAGE,
    FLAG_KEY,
    MAX_RULES_LEN,
    dispatch,
    transform,
)
from test_app import (  # noqa: F401 - `fake_host` is a fixture re-exported for this module
    _envelope,
    _event,
    _FakeHost,
    _last_reply_text,
    _scoped,
    fake_host,
)

CANARY = "CANARYuser9f3a"
ERROR_LEVEL = 0


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _break_kv(op: str, message: str) -> None:
    def _boom(*_args: Any) -> Any:
        raise RuntimeError(message)

    setattr(sys.modules["wit_world"].imports.kv, op, _boom)


# -- mod gate fails closed, zero side effects
@pytest.mark.parametrize("command", ["set", "clear"])
@pytest.mark.parametrize(
    ("is_mod", "is_broadcaster"),
    [(None, None), (False, None), (None, False), (False, False)],
    ids=["no-badges", "mod-false", "broadcaster-false", "both-false"],
)
def test_set_and_clear_are_denied_without_a_true_badge_and_touch_no_state(
    fake_host: _FakeHost, command: str, is_mod: bool | None, is_broadcaster: bool | None
) -> None:
    fake_host.kv.store[_scoped(_RULES_KEY)] = b"existing rules"
    fake_host.kv.calls.clear()

    result = _run(
        dispatch(
            _envelope(command, arg="hijack", is_mod=is_mod, is_broadcaster=is_broadcaster),
            {},
            http_client=None,
        )
    )

    assert result.detail == f"{command}:denied"
    assert _last_reply_text(fake_host) == _PERMISSION_DENIED_MSG
    assert fake_host.kv.calls == []
    assert fake_host.kv.store[_scoped(_RULES_KEY)] == b"existing rules"


@pytest.mark.parametrize("badge", ["is_mod", "is_broadcaster"])
def test_either_true_badge_may_set_then_clear(fake_host: _FakeHost, badge: str) -> None:
    kwargs: dict[str, Any] = {badge: True}
    _run(dispatch(_envelope("set", arg="be nice", **kwargs), {}, http_client=None))
    assert fake_host.kv.store[_scoped(_RULES_KEY)] == b"be nice"
    _run(dispatch(_envelope("clear", **kwargs), {}, http_client=None))
    assert _scoped(_RULES_KEY) not in fake_host.kv.store


def test_show_is_open_to_anyone_with_no_badges(fake_host: _FakeHost) -> None:
    fake_host.kv.store[_scoped(_RULES_KEY)] = b"public rules"
    _run(dispatch(_envelope("show"), {}, http_client=None))
    assert _last_reply_text(fake_host) == "public rules"


def test_denied_attempts_are_logged_without_the_attempted_text(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("set", arg=f"{CANARY} text"), {}, http_client=None))
    denied = [
        json.loads(fields)
        for _l, msg, fields in fake_host.log_calls
        if msg == "rules.permission_denied"
    ]
    assert denied == [{"command": "set", "role_signal": "None"}]


# -- corrupt store raises, never papered over
def test_corrupt_non_utf8_rules_value_raises_and_never_replies(fake_host: _FakeHost) -> None:
    fake_host.kv.store[_scoped(_RULES_KEY)] = b"\xff\xfe\xfd"

    with pytest.raises(UnicodeDecodeError):
        _run(dispatch(_envelope("show"), {}, http_client=None))
    assert fake_host.relay_calls == []


@pytest.mark.parametrize(
    ("kv_op", "command", "op"),
    [("get", "show", "show"), ("set", "set", "set"), ("delete", "clear", "clear")],
)
def test_kv_backend_failure_is_loud_and_never_logs_the_user_text(
    fake_host: _FakeHost, kv_op: str, command: str, op: str
) -> None:
    _break_kv(kv_op, "backend down")

    with pytest.raises(RuntimeError, match=f"rules {op} failed"):
        _run(dispatch(_envelope(command, arg=f"{CANARY} rules", is_mod=True), {}, http_client=None))

    assert "temporarily unavailable" in _last_reply_text(fake_host)
    errors = [(msg, json.loads(f)) for lvl, msg, f in fake_host.log_calls if lvl == ERROR_LEVEL]
    assert [msg for msg, _f in errors] == ["rules.kv_error"]
    assert errors[0][1]["op"] == op
    assert CANARY not in json.dumps(errors)


# -- bounds, trimming, aliases, durability
def test_set_length_boundary_is_inclusive_and_text_is_trimmed(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("set", arg="x" * MAX_RULES_LEN, is_mod=True), {}, http_client=None))
    assert _last_reply_text(fake_host) == "Rules have been updated."

    _run(
        dispatch(_envelope("set", arg="x" * (MAX_RULES_LEN + 1), is_mod=True), {}, http_client=None)
    )
    assert _last_reply_text(fake_host) == f"rules text must be {MAX_RULES_LEN} characters or fewer"
    assert fake_host.kv.store[_scoped(_RULES_KEY)] == b"x" * MAX_RULES_LEN

    _run(dispatch(_envelope("set", arg="   padded   ", is_mod=True), {}, http_client=None))
    assert fake_host.kv.store[_scoped(_RULES_KEY)] == b"padded"


def test_set_with_non_string_arg_is_treated_as_missing(fake_host: _FakeHost) -> None:
    env = _envelope("set", is_mod=True)
    env.event.payload["arg"] = 12345
    _run(dispatch(env, {}, http_client=None))
    assert _last_reply_text(fake_host) == "Usage: !rules set <text>"
    assert fake_host.kv.calls == []


def test_rules_text_is_stored_durably_with_no_ttl(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("set", arg="forever", is_mod=True), {}, http_client=None))
    [(_op, (_key, value, ttl))] = [c for c in fake_host.kv.calls if c[0] == "set"]
    assert (value, ttl) == (b"forever", 0)


def test_unicode_rules_round_trip_through_utf8(fake_host: _FakeHost) -> None:
    text = "Be kind \U0001f427 — no spam, über-polite"
    _run(dispatch(_envelope("set", arg=text, is_mod=True), {}, http_client=None))
    _run(dispatch(_envelope("show"), {}, http_client=None))
    assert _last_reply_text(fake_host) == text


@pytest.mark.parametrize("text", ["!rules clear", "!rules CLEAR", "!rules Remove", "!rules remove"])
def test_clear_and_remove_are_case_insensitive_aliases(text: str, fake_host: _FakeHost) -> None:
    out = _run(transform(_event(text, is_mod=True)))
    assert out is not None and out.payload["command"] == "clear"


@pytest.mark.parametrize(
    "text",
    ["!rules clear now", "!rules remove all", "!rules enable", "!rules list", "!rules bogus x"],
)
def test_extra_or_unsupported_grammar_degrades_to_the_usage_reply(
    text: str, fake_host: _FakeHost
) -> None:
    out = _run(transform(_event(text, is_mod=True)))
    assert out is not None
    assert out.payload["command"] == "usage"
    assert "arg" not in out.payload
    _run(dispatch(_envelope("usage"), {}, http_client=None))
    assert _last_reply_text(fake_host) == _USAGE


def test_set_without_text_reaches_dispatch_and_is_answered_with_set_usage(
    fake_host: _FakeHost,
) -> None:
    out = _run(transform(_event("!rules set", is_mod=True)))
    assert out is not None and out.payload["command"] == "set" and "arg" not in out.payload
    _run(dispatch(_envelope("set", is_mod=True), {}, http_client=None))
    assert _last_reply_text(fake_host) == "Usage: !rules set <text>"
    assert fake_host.kv.calls == []


def test_tenant_wide_sentinel_scopes_under_the_literal_zero_segment(fake_host: _FakeHost) -> None:
    _run(
        dispatch(_envelope("set", arg="tenant", is_mod=True, community=None), {}, http_client=None)
    )
    assert f"c.0.{_RULES_KEY}" in fake_host.kv.store


def test_every_kv_key_written_passes_the_host_charset_check(fake_host: _FakeHost) -> None:
    """regression: gh-631 -- the shared fake raises on `:`; assert none ever reached the store."""
    _run(dispatch(_envelope("set", arg="r", is_mod=True), {}, http_client=None))
    _run(dispatch(_envelope("clear", is_mod=True), {}, http_client=None))
    keys = {key for _op, (key, *_rest) in fake_host.kv.calls}
    assert keys and not any(":" in key for key in keys)


# -- flag fail-closed
def test_flag_is_requested_with_a_fail_closed_default(fake_host: _FakeHost) -> None:
    seen: list[tuple[str, bool]] = []
    sys.modules["wit_world"].imports.flags.enabled = (
        lambda key, default_value: seen.append((key, default_value)) or default_value
    )
    assert _run(transform(_event("!rules"))) is None
    assert seen == [(FLAG_KEY, False)]
    assert FLAG_KEY == "waddles.command-rules"


def test_missing_wit_world_keeps_the_command_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "wit_world", None)
    assert _run(transform(_event("!rules"))) is None


def test_flag_off_does_no_io_and_logs_nothing(fake_host: _FakeHost) -> None:
    fake_host.flag_state["enabled"] = False
    assert _run(transform(_event(f"!rules set {CANARY}", is_mod=True))) is None
    assert fake_host.log_calls == [] and fake_host.kv.calls == []


# -- PII-free logs
def test_no_log_line_in_any_flow_contains_rules_text_or_user_input(fake_host: _FakeHost) -> None:
    """PII-free-log regression: the rules body and any typed grammar text never reach the sink."""
    for text in (
        f"!rules set {CANARY} rule one",
        f"!rules {CANARY}",  # invalid verb holding the canary -> debug grammar log path
        f"!rules clear {CANARY}",
        "!rules",
    ):
        out = _run(transform(_event(text, is_mod=True)))
        assert out is not None
        _run(
            dispatch(
                _envelope(str(out.payload["command"]), arg=out.payload.get("arg"), is_mod=True),
                {},
                http_client=None,
            )
        )
    assert fake_host.log_calls, "expected log lines (denominator must be non-zero)"
    for _lvl, message, fields_json in fake_host.log_calls:
        assert CANARY.lower() not in message.lower()
        assert CANARY.lower() not in fields_json.lower()


def test_entry_wiring_exports_the_stage_functions() -> None:
    assert _entry_wiring.bundle_transform is app.transform
    assert _entry_wiring.bundle_dispatch is app.dispatch
    assert _entry_wiring.__all__ == ["bundle_dispatch", "bundle_transform"]
