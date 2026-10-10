"""Backfill coverage for the `wheel` bundle: PII-free logs, mod-gate matrix, corrupt-store, bounds.

Complements `test_app.py` with what it does not pin: a PII-free-log regression covering every
user-typed value (option text, unknown sub-command token, actor) -- including the unknown-token
leak this suite found and that `transform()` now guards (the matched-command log field is a
static allowlist, never the typed token) -- the full mod-gate badge matrix with zero kv access on
denial, every corrupt-store shape failing loud, per-community isolation, bounds, the flag's
fail-closed default, and the static `_entry_wiring` re-export.

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
    _EMPTY_WHEEL_MSG,
    _KV_ERROR_MSG,
    _PERMISSION_DENIED_MSG,
    _USAGE,
    FLAG_KEY,
    MAX_OPTION_LEN,
    MAX_OPTIONS,
    OPTIONS_KEY,
    transform,
)
from test_app import (  # noqa: F401 - `fake_host` is a fixture re-exported for this module
    _event,
    _no_role_event,
    _reply_text,
    _seed_options,
    _stored_options,
    _transform_in,
    fake_host,
)
from waddle_sdk.flask_core.bundle_runtime import bundle_context

CANARY = "CANARYuser9f3a"
ERROR_LEVEL = 0


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# -- PII-free logs
@pytest.mark.parametrize(
    "text",
    [
        f"!wheel {CANARY}",  # unknown sub-command token (was logged verbatim)
        f"!wheel @{CANARY}",
        f"!wheel {CANARY} extra words",
        f"!wheel add {CANARY} option",
        f"!wheel remove {CANARY}",
        f"!wheel add {'x' * (MAX_OPTION_LEN + 1)}{CANARY}",
        f"!wheel ADD {CANARY}",
        "!wheel",
        "!wheel spin",
        "!wheel list",
        "!wheel reset",
    ],
)
def test_no_log_line_contains_any_user_typed_value(fake_host: Any, text: str) -> None:
    """PII-free-log regression: typed option text / sub-command tokens never reach the sink."""
    _seed_options(fake_host, "comm-1", ["tacos", f"{CANARY}-seed"])
    fake_host.log_calls.clear()

    _transform_in("comm-1", text, is_mod=True, actor=f"{CANARY}_actor")

    assert fake_host.log_calls, "expected log lines (denominator must be non-zero)"
    for _lvl, message, fields_json in fake_host.log_calls:
        # lower-cased compare: `transform` lower-cases the typed token before it ever logs it
        assert CANARY.lower() not in message.lower()
        assert CANARY.lower() not in fields_json.lower()


def test_matched_command_log_field_is_a_static_name(fake_host: Any) -> None:
    for text, expected in [
        ("!wheel", "spin"),
        ("!wheel spin", "spin"),
        ("!wheel ADD x", "add"),
        ("!wheel remove x", "remove"),
        ("!wheel list", "list"),
        ("!wheel reset", "reset"),
        (f"!wheel {CANARY}", "unknown"),
    ]:
        fake_host.log_calls.clear()
        _transform_in("comm-1", text, is_mod=True)
        matched = [
            json.loads(fields)
            for _l, msg, fields in fake_host.log_calls
            if msg == "wheel.transform matched"
        ]
        assert matched == [{"command": expected}], text


def test_corrupt_store_error_log_never_echoes_stored_content(fake_host: Any) -> None:
    from waddle_sdk.community_kv import _scoped_key

    fake_host.kv.store[_scoped_key("comm-1", OPTIONS_KEY)] = f'["{CANARY}", 3]'.encode()
    _transform_in("comm-1", "!wheel list")
    assert any(lvl == ERROR_LEVEL for lvl, _m, _f in fake_host.log_calls)
    for _lvl, message, fields_json in fake_host.log_calls:
        assert CANARY.lower() not in (message + fields_json).lower()


# -- mod gate fails closed with zero kv access
@pytest.mark.parametrize("verb", ["add tacos", "remove tacos", "reset"])
@pytest.mark.parametrize(
    ("is_mod", "is_broadcaster"),
    [(False, False), (False, None), (None, False)],
    ids=["both-false", "mod-false", "broadcaster-false"],
)
def test_privileged_verbs_are_denied_without_a_true_badge_and_never_mutate(
    fake_host: Any, verb: str, is_mod: bool | None, is_broadcaster: bool | None
) -> None:
    _seed_options(fake_host, "comm-1", ["tacos"])
    fake_host.kv.calls.clear()

    result = _transform_in("comm-1", f"!wheel {verb}", is_mod=is_mod, is_broadcaster=is_broadcaster)

    assert _reply_text(result) == _PERMISSION_DENIED_MSG
    assert [c for c in fake_host.kv.calls if c[0] != "get"] == []
    assert _stored_options(fake_host, "comm-1") == ["tacos"]


@pytest.mark.parametrize("verb", ["add tacos", "remove tacos", "reset"])
def test_missing_badge_fields_entirely_are_denied_and_logged_at_debug(
    fake_host: Any, verb: str
) -> None:
    _seed_options(fake_host, "comm-1", ["tacos"])
    with bundle_context(tenant="t", community="comm-1", app_id="waddles.core.example.wheel"):
        result = _run(transform(_no_role_event(f"!wheel {verb}")))
    assert _reply_text(result) == _PERMISSION_DENIED_MSG
    assert _stored_options(fake_host, "comm-1") == ["tacos"]
    assert any(msg == "wheel.role_info_unavailable" for _l, msg, _f in fake_host.log_calls)


@pytest.mark.parametrize("badge", ["is_mod", "is_broadcaster"])
def test_either_true_badge_grants_the_privileged_verbs(fake_host: Any, badge: str) -> None:
    kwargs: dict[str, Any] = {"is_mod": False, "is_broadcaster": False, badge: True}
    _transform_in("comm-1", "!wheel add tacos", **kwargs)
    assert _stored_options(fake_host, "comm-1") == ["tacos"]
    _transform_in("comm-1", "!wheel reset", **kwargs)
    assert _stored_options(fake_host, "comm-1") == []


@pytest.mark.parametrize("verb", ["list", "spin", ""])
def test_reads_and_spins_are_open_to_anyone(fake_host: Any, verb: str) -> None:
    _seed_options(fake_host, "comm-1", ["tacos"])
    result = _transform_in("comm-1", f"!wheel {verb}".strip(), is_mod=False, is_broadcaster=False)
    assert "tacos" in _reply_text(result)


# -- corrupt store: every shape fails loud (generic reply + ERROR), never reset
@pytest.mark.parametrize(
    "blob",
    [b"\xff\xfe", b"not json", b"{}", b'"str"', b"42", b"null", b'[1, "a"]', b'["a", null]'],
)
@pytest.mark.parametrize("verb", ["list", "spin", "add x", "remove x"])
def test_corrupt_options_blob_replies_generic_error_logs_error_and_is_never_overwritten(
    fake_host: Any, blob: bytes, verb: str
) -> None:
    from waddle_sdk.community_kv import _scoped_key

    key = _scoped_key("comm-1", OPTIONS_KEY)
    fake_host.kv.store[key] = blob

    result = _transform_in("comm-1", f"!wheel {verb}", is_mod=True)

    assert _reply_text(result) == _KV_ERROR_MSG
    assert fake_host.kv.store[key] == blob, "corrupt state must not be silently reset"
    errors = [(m, json.loads(f)) for lvl, m, f in fake_host.log_calls if lvl == ERROR_LEVEL]
    assert [m for m, _f in errors] == ["wheel.kv_failure"]
    assert "corrupt wheel options" in errors[0][1]["error"]


@pytest.mark.parametrize(
    ("kv_op", "verb"), [("get", "list"), ("set", "add x"), ("set", "reset"), ("get", "spin")]
)
def test_kv_backend_failure_replies_generic_error_and_logs_the_failing_call(
    fake_host: Any, kv_op: str, verb: str
) -> None:
    def _boom(*_a: Any) -> Any:
        raise RuntimeError("backend down")

    setattr(sys.modules["wit_world"].imports.kv, kv_op, _boom)

    result = _transform_in("comm-1", f"!wheel {verb}", is_mod=True)

    assert _reply_text(result) == _KV_ERROR_MSG
    [(_m, fields)] = [(m, json.loads(f)) for lvl, m, f in fake_host.log_calls if lvl == ERROR_LEVEL]
    assert f"kv.{kv_op}(" in fields["error"] and "backend down" in fields["error"]


# -- behavior details
def test_communities_have_isolated_wheels(fake_host: Any) -> None:
    _transform_in("comm-a", "!wheel add alpha", is_mod=True)
    _transform_in("comm-b", "!wheel add beta", is_mod=True)
    assert _stored_options(fake_host, "comm-a") == ["alpha"]
    assert _stored_options(fake_host, "comm-b") == ["beta"]
    assert _reply_text(_transform_in("comm-a", "!wheel list")) == "Wheel options: alpha"


def test_wheel_is_capped_and_duplicates_are_rejected_before_the_cap(fake_host: Any) -> None:
    _seed_options(fake_host, "comm-1", [f"opt{i}" for i in range(MAX_OPTIONS)])
    assert "(the max)" in _reply_text(_transform_in("comm-1", "!wheel add one-more", is_mod=True))
    assert _reply_text(_transform_in("comm-1", "!wheel add OPT3", is_mod=True)) == (
        "'OPT3' is already on the wheel."
    )
    assert len(_stored_options(fake_host, "comm-1")) == MAX_OPTIONS


def test_add_trims_and_remove_reports_the_stored_spelling(fake_host: Any) -> None:
    _transform_in("comm-1", "!wheel add    Tacos   ", is_mod=True)
    assert _stored_options(fake_host, "comm-1") == ["Tacos"]
    assert _reply_text(_transform_in("comm-1", "!wheel remove tacos", is_mod=True)) == (
        "Removed 'Tacos' from the wheel."
    )


def test_reset_on_an_empty_wheel_is_a_clean_no_op_success(fake_host: Any) -> None:
    assert _reply_text(_transform_in("comm-1", "!wheel reset", is_mod=True)) == (
        "The wheel has been cleared."
    )
    assert _stored_options(fake_host, "comm-1") == []


def test_spin_on_an_emptied_wheel_is_rejected_not_crashed(fake_host: Any) -> None:
    _transform_in("comm-1", "!wheel add x", is_mod=True)
    _transform_in("comm-1", "!wheel reset", is_mod=True)
    assert _reply_text(_transform_in("comm-1", "!wheel spin")) == _EMPTY_WHEEL_MSG


def test_spin_draws_via_random_choice_over_the_stored_options(
    fake_host: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_options(fake_host, "comm-1", ["a", "b", "c"])
    seen: list[list[str]] = []
    monkeypatch.setattr(app.random, "choice", lambda seq: (seen.append(list(seq)), seq[1])[1])
    assert "lands on: b!" in _reply_text(_transform_in("comm-1", "!wheel spin"))
    assert seen == [["a", "b", "c"]]


def test_unknown_subcommand_replies_with_usage_and_touches_no_state(fake_host: Any) -> None:
    fake_host.kv.calls.clear()
    reply = _reply_text(_transform_in("comm-1", "!wheel frobnicate", is_mod=True))
    assert reply == f"Unknown !wheel subcommand 'frobnicate'. {_USAGE}"
    assert fake_host.kv.calls == []


def test_no_community_context_never_touches_kv(fake_host: Any) -> None:
    result = _transform_in(None, "!wheel add x", is_mod=True)
    assert "requires a community context" in _reply_text(result)
    assert fake_host.kv.calls == []


def test_every_kv_key_written_passes_the_host_charset_check(fake_host: Any) -> None:
    """regression: gh-631 -- the shared fake raises on `:`; assert none ever reached the store."""
    _transform_in("comm-1", "!wheel add x", is_mod=True)
    _transform_in("comm-1", "!wheel reset", is_mod=True)
    keys = {args[0] for _op, args in fake_host.kv.calls}
    assert keys and not any(":" in key for key in keys)


# -- flag fail-closed
def test_flag_is_requested_with_a_fail_closed_default(fake_host: Any) -> None:
    seen: list[tuple[str, bool]] = []
    fake_host.wit_world.imports.flags.enabled = (
        lambda key, default_value: seen.append((key, default_value)) or default_value
    )
    assert _transform_in("comm-1", "!wheel") is None
    assert seen == [(FLAG_KEY, False)]
    assert FLAG_KEY == "waddles.command-wheel"


def test_missing_wit_world_keeps_the_command_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "wit_world", None)
    assert _run(transform(_event("!wheel"))) is None


def test_flag_off_does_no_io_and_logs_nothing(fake_host: Any) -> None:
    fake_host.flag_state["enabled"] = False
    assert _transform_in("comm-1", f"!wheel add {CANARY}", is_mod=True) is None
    assert fake_host.log_calls == [] and fake_host.kv.calls == []


def test_entry_wiring_exports_the_stage_functions() -> None:
    assert _entry_wiring.bundle_transform is app.transform
    assert _entry_wiring.bundle_dispatch is app.dispatch
    assert _entry_wiring.__all__ == ["bundle_dispatch", "bundle_transform"]
