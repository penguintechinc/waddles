"""Backfill coverage for the `shoutout` bundle: mod gate, PII-free logs, no-fake-AI, kv charset.

Complements `test_app.py` with what it does not pin: the full set/list/enable/disable/shoutout mod
gate (every command is admin/mod only except `usage`) fails closed with zero kv access, a
PII-free-log + PII-free-state regression for the shoutout target / auto-list entries / template
text, the `ai` sub-module is *not* silently faked (identical reply with it on or off -- gh-626),
every kv key written passes the real host charset (gh-631: the stale `submodule:...` separator
these tests used to assert is now `submodule.<cmd>.<sub>`), sub-module gate kv failures propagate
loudly, bounds, the flag's fail-closed default, and the static `_entry_wiring` re-export.

Reuses `test_app.py`'s fake host (`_install`), which routes kv through the real `waddle_sdk.kv`
(so `validate_key` runs on every key even though this suite's fake store itself is permissive).
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
    _expected_pseudonym,
    _FakeHost,
    _install,
    _last_reply,
    _sample_envelope,
    _sample_event,
    _scoped,
    fake_host,
)
from waddle_sdk.kv import validate_key

import _entry_wiring
import app
from app import (
    _AUTO_LIST_KEY,
    _KNOWN_COMMANDS,
    _PERMISSION_DENIED_MSG,
    _TEMPLATE_KEY,
    _USAGE,
    DEFAULT_TEMPLATE,
    FLAG_KEY,
    MAX_AUTO_LIST_SIZE,
    MAX_TEMPLATE_LEN,
    dispatch,
    transform,
)

CANARY = "CANARYuser9f3a"
ERROR_LEVEL = 0

#: Every command except `usage` needs a moderator/broadcaster.
PRIVILEGED = sorted(_KNOWN_COMMANDS - {"usage"})


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _go(command: str, **kwargs: Any) -> Any:
    return _run(dispatch(_sample_envelope("twitch", command, **kwargs), {}, http_client=None))


def _arg_for(command: str) -> str | None:
    return {
        "shoutout": "penguin",
        "config_set_template": "hi $(username)",
        "auto_add": "penguin",
        "auto_remove": "penguin",
    }.get(command)


# -- mod gate fails closed with zero kv access
@pytest.mark.parametrize("command", PRIVILEGED)
@pytest.mark.parametrize(
    ("is_mod", "is_broadcaster"),
    [(None, None), (False, None), (None, False), (False, False)],
    ids=["no-badges", "mod-false", "broadcaster-false", "both-false"],
)
def test_every_privileged_command_is_denied_without_a_true_badge_and_does_no_io(
    fake_host: _FakeHost, command: str, is_mod: bool | None, is_broadcaster: bool | None
) -> None:
    result = _go(command, arg=_arg_for(command), is_mod=is_mod, is_broadcaster=is_broadcaster)

    assert result.detail == f"{command}:denied"
    assert _last_reply(fake_host) == _PERMISSION_DENIED_MSG
    assert fake_host.kv_calls == []
    assert fake_host.store == {}


@pytest.mark.parametrize("badge", ["is_mod", "is_broadcaster"])
def test_either_true_badge_runs_the_command(fake_host: _FakeHost, badge: str) -> None:
    result = _go("shoutout", arg="penguin", **{badge: True})
    assert result.detail == "shoutout"
    assert "penguin" in _last_reply(fake_host)


def test_usage_is_the_only_command_open_to_everyone(fake_host: _FakeHost) -> None:
    assert _go("usage").detail == "usage"
    assert _last_reply(fake_host) == _USAGE
    assert fake_host.kv_calls == []


def test_privileged_set_matches_known_commands_exactly() -> None:
    assert set(PRIVILEGED) | {"usage"} == set(_KNOWN_COMMANDS)
    assert len(PRIVILEGED) == 9


# -- the `ai` sub-module is not silently faked (gh-626)
def test_ai_enabled_reply_is_identical_to_ai_disabled_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # regression: gh-626 -- AI shoutout generation is pending; enabling `ai` must not alter output.
    host = _FakeHost()
    _install(monkeypatch, host, tier="enterprise")
    _go("config_set_template", arg="hello $(username)!", is_mod=True)
    _go("shoutout", arg="penguin", is_mod=True)
    before = _last_reply(host)

    _go("config_enable_ai", is_mod=True)
    _go("shoutout", arg="penguin", is_mod=True)

    assert (before, _last_reply(host)) == ("hello penguin!", "hello penguin!")


def test_ai_pending_is_logged_at_debug_with_no_user_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _FakeHost()
    _install(monkeypatch, host, tier="professional")
    _go("config_enable_ai", is_mod=True)
    host.log_calls.clear()

    _go("shoutout", arg=f"{CANARY}", is_mod=True)

    pending = [
        (lvl, json.loads(f))
        for lvl, m, f in host.log_calls
        if m == "shoutout.ai_requested_but_pending"
    ]
    assert pending == [(3, {"community": "comm-1"})]


@pytest.mark.parametrize("tier", ["free", "", "FREE", "nonsense"])
def test_enable_ai_is_denied_for_free_or_unrecognized_tiers_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tier: str
) -> None:
    host = _FakeHost()
    _install(monkeypatch, host, tier=tier)
    result = _go("config_enable_ai", is_mod=True)
    assert result.detail == "config_enable_ai"
    assert "requires a professional license" in _last_reply(host)
    assert host.store == {}


# -- kv charset (gh-631) + state hygiene
def test_every_kv_key_written_passes_the_real_host_charset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # regression: gh-631 -- `submodule:shoutout:*` was rejected by the real kv host; the gate now
    # uses `.` and this suite asserts every key it ever touches satisfies `validate_key`.
    host = _FakeHost()
    _install(monkeypatch, host, tier="enterprise")
    for command in ("config_enable_auto", "config_enable_ai", "config_set_template"):
        _go(command, arg=_arg_for(command), is_mod=True)
    _go("auto_add", arg="penguin", is_mod=True)
    _go("config_disable_auto", is_mod=True)
    _go("config_disable_ai", is_mod=True)

    keys = {call[1] for call in host.kv_calls}
    assert keys, "expected kv traffic (denominator must be non-zero)"
    for key in keys:
        validate_key(key)
        assert ":" not in key
    assert _scoped("submodule.shoutout.auto") in keys
    assert _scoped("submodule.shoutout.ai") in keys


def test_target_usernames_are_stored_only_as_pseudonyms_never_raw(fake_host: _FakeHost) -> None:
    _go("config_enable_auto", is_mod=True)
    _go("auto_add", arg=f"@{CANARY}", is_mod=True)

    assert _scoped(_AUTO_LIST_KEY) in fake_host.store
    for key, value in fake_host.store.items():
        assert CANARY.lower() not in key.lower()
        assert CANARY.lower().encode() not in value.lower()
    assert json.loads(fake_host.store[_scoped(_AUTO_LIST_KEY)]) == [_expected_pseudonym(CANARY)]


def test_auto_list_is_persisted_sorted_deduplicated_and_durable(fake_host: _FakeHost) -> None:
    _go("config_enable_auto", is_mod=True)
    for name in ("zeta", "Alpha", "alpha", "mid"):
        _go("auto_add", arg=name, is_mod=True)

    stored = json.loads(fake_host.store[_scoped(_AUTO_LIST_KEY)])
    assert stored == sorted({_expected_pseudonym(n) for n in ("zeta", "alpha", "mid")})
    [(_op, _key, _value, ttl)] = [
        c for c in fake_host.kv_calls if c[0] == "set" and c[1] == _scoped(_AUTO_LIST_KEY)
    ][-1:]
    assert ttl == 0


def test_auto_list_boundary_is_inclusive_of_the_cap(fake_host: _FakeHost) -> None:
    _go("config_enable_auto", is_mod=True)
    fake_host.store[_scoped(_AUTO_LIST_KEY)] = json.dumps(
        [f"{i:064x}" for i in range(MAX_AUTO_LIST_SIZE - 1)]
    ).encode()

    _go("auto_add", arg="last-one", is_mod=True)
    assert "added last-one" in _last_reply(fake_host)
    _go("auto_add", arg="one-too-many", is_mod=True)
    assert _last_reply(fake_host) == f"the auto-shoutout list is full ({MAX_AUTO_LIST_SIZE} max)"
    assert len(json.loads(fake_host.store[_scoped(_AUTO_LIST_KEY)])) == MAX_AUTO_LIST_SIZE


def test_template_length_boundary_is_inclusive_and_text_trimmed(fake_host: _FakeHost) -> None:
    _go("config_set_template", arg="x" * MAX_TEMPLATE_LEN, is_mod=True)
    assert _last_reply(fake_host) == "shoutout template updated"
    _go("config_set_template", arg="x" * (MAX_TEMPLATE_LEN + 1), is_mod=True)
    assert "characters or fewer" in _last_reply(fake_host)
    _go("config_set_template", arg="  $(username) rocks  ", is_mod=True)
    assert fake_host.store[_scoped(_TEMPLATE_KEY)] == b"$(username) rocks"


def test_template_substitutes_every_placeholder_occurrence(fake_host: _FakeHost) -> None:
    _go("config_set_template", arg="$(username)! yes, $(username)!", is_mod=True)
    _go("shoutout", arg="penguin", is_mod=True)
    assert _last_reply(fake_host) == "penguin! yes, penguin!"


def test_leading_at_signs_are_stripped_from_targets(fake_host: _FakeHost) -> None:
    _go("shoutout", arg="@@penguin", is_mod=True)
    assert _last_reply(fake_host) == DEFAULT_TEMPLATE.replace("$(username)", "penguin")


# -- failures propagate loudly
@pytest.mark.parametrize("command", ["auto_add", "auto_remove", "auto_list"])
def test_sub_module_gate_read_failure_propagates_raw_instead_of_defaulting_off(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """`SubModuleGate` calls skip `_fail_kv`: a backend error surfaces, never reads as 'off'."""
    host = _FakeHost()
    _install(monkeypatch, host, kv_get_raises=RuntimeError("kv down"))

    with pytest.raises(RuntimeError, match="kv down"):
        _go(command, arg=_arg_for(command), is_mod=True)
    assert host.relay_calls == []


def test_sub_module_gate_write_failure_propagates_and_never_reports_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _FakeHost()
    _install(monkeypatch, host, kv_set_raises=RuntimeError("kv down"))

    with pytest.raises(RuntimeError, match="kv down"):
        _go("config_enable_auto", is_mod=True)
    assert host.relay_calls == [] and host.store == {}


def test_kv_backend_error_log_carries_only_op_and_exception_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _FakeHost()
    _install(monkeypatch, host, kv_get_raises=RuntimeError(f"backend echoed {CANARY}"))

    with pytest.raises(RuntimeError, match="shoutout kv get failed: RuntimeError"):
        _go("shoutout", arg="penguin", is_mod=True)

    errors = [(m, json.loads(f)) for lvl, m, f in host.log_calls if lvl == ERROR_LEVEL]
    assert errors == [("shoutout.kv_error", {"op": "get", "error": "RuntimeError"})]
    assert "temporarily unavailable" in _last_reply(host)
    assert CANARY not in _last_reply(host)


@pytest.mark.parametrize(
    "blob",
    [b"\xff\xfe", b"not json", b"{}", b'"s"', b"7", b"[1, 2]", b'["ok", 3]'],
)
@pytest.mark.parametrize("command", ["auto_add", "auto_remove", "auto_list"])
def test_corrupt_auto_list_fails_loud_and_is_never_reset(
    fake_host: _FakeHost, blob: bytes, command: str
) -> None:
    _go("config_enable_auto", is_mod=True)
    fake_host.store[_scoped(_AUTO_LIST_KEY)] = blob

    with pytest.raises(RuntimeError, match="shoutout corrupt state"):
        _go(command, arg="penguin", is_mod=True)

    assert fake_host.store[_scoped(_AUTO_LIST_KEY)] == blob
    assert "storage is corrupted" in _last_reply(fake_host)
    errors = [(m, json.loads(f)) for lvl, m, f in fake_host.log_calls if lvl == ERROR_LEVEL]
    assert [m for m, _f in errors] == ["shoutout.state_corrupt"]


def test_corrupt_template_logs_at_error_and_falls_back_to_the_default(
    fake_host: _FakeHost,
) -> None:
    fake_host.store[_scoped(_TEMPLATE_KEY)] = b"\xff\xfe"
    _go("shoutout", arg="penguin", is_mod=True)
    assert _last_reply(fake_host) == DEFAULT_TEMPLATE.replace("$(username)", "penguin")
    errors = [(m, json.loads(f)) for lvl, m, f in fake_host.log_calls if lvl == ERROR_LEVEL]
    assert errors == [("shoutout.template_corrupt", {"community": "comm-1"})]


# -- flag fail-closed
def test_flag_is_requested_with_a_fail_closed_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _FakeHost()
    _install(monkeypatch, host)
    seen: list[tuple[str, bool]] = []
    sys.modules["wit_world"].imports.flags.enabled = (
        lambda key, default_value: seen.append((key, default_value)) or default_value
    )
    assert _run(transform(_sample_event("!so penguin"))) is None
    assert seen == [(FLAG_KEY, False)]
    assert FLAG_KEY == "waddles.command-shoutout"


def test_missing_wit_world_keeps_the_command_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "wit_world", None)
    assert _run(transform(_sample_event("!so penguin"))) is None


def test_flag_off_does_no_io_and_logs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHost()
    _install(monkeypatch, host, flag_enabled=False)
    assert _run(transform(_sample_event(f"!so {CANARY}", is_mod=True))) is None
    assert host.log_calls == [] and host.kv_calls == []


# -- PII-free logs
def test_no_log_line_in_any_flow_contains_a_target_template_or_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PII-free-log regression: typed targets, template text and the raw actor never log."""
    host = _FakeHost()
    _install(monkeypatch, host, tier="enterprise")
    actor = f"{CANARY}_actor"

    for text in (
        f"!so {CANARY}",
        f"!so @{CANARY}",
        f"!so {CANARY} extra",
        f"!so set {CANARY} $(username)",
        f"!so auto add {CANARY}",
        f"!so auto {CANARY}",
        f"!so enable {CANARY}",
        f"!so {CANARY.upper()}",
    ):
        _run(transform(_sample_event(text, is_mod=True)))

    for command in PRIVILEGED:
        arg = (
            f"{CANARY}" if command in {"shoutout", "auto_add", "auto_remove"} else _arg_for(command)
        )
        if command == "config_set_template":
            arg = f"{CANARY} $(username)"
        _go(command, arg=arg, actor=actor, is_mod=True)
    _go("shoutout", arg=CANARY, actor=actor)  # denied path
    _go("shoutout", arg=f"{CANARY}!!bad", actor=actor, is_mod=True)  # invalid-target path

    assert host.log_calls, "expected log lines (denominator must be non-zero)"
    for _lvl, message, fields_json in host.log_calls:
        assert CANARY.lower() not in message.lower()
        assert CANARY.lower() not in fields_json.lower()


def test_entry_wiring_exports_the_stage_functions() -> None:
    assert _entry_wiring.bundle_transform is app.transform
    assert _entry_wiring.bundle_dispatch is app.dispatch
    assert _entry_wiring.__all__ == ["bundle_dispatch", "bundle_transform"]
