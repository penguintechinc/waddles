"""Regression tests for the `wheel` bundle (fix/bundle-defects-wave).

1. Mod-gate bypass: a string badge (`"false"`) is truthy, so `bool(is_mod)` let a non-moderator
   mutate the wheel. Only a real boolean `True` opens the gate.
2. PII-free error logs: the `wheel.kv_failure` ERROR line used to carry `str(exc)` -- the host's
   free-form error text and, for a corrupt blob, a decode/parse message derived from stored
   (user-typed) option text. It now carries a static `op` name and the exception CLASS name only.

Reuses `test_app.py`'s `fake_host` fixture (shared charset-enforcing `kv` fake + flags/relay/log).
"""

# F811: pytest fixtures re-exported from `test_app.py` are re-bound by the test parameters.
# ruff: noqa: F811
from __future__ import annotations

import json
import sys
from typing import Any

import pytest
from test_app import (  # noqa: F401 - `fake_host` is a fixture re-exported for this module
    _event,
    _reply_text,
    _seed_options,
    _stored_options,
    _transform_in,
    fake_host,
)
from waddle_sdk.community_kv import _scoped_key

from app import _KV_ERROR_MSG, _PERMISSION_DENIED_MSG, OPTIONS_KEY, _is_privileged

SECRET = "zzSECRETtypedtextzz"


# mod-gate bypass


@pytest.mark.parametrize("badge", ["false", "False", "0", "no", "", "true", "1", 0, 1])
def test_a_non_boolean_badge_never_opens_the_gate(fake_host: Any, badge: Any) -> None:
    assert _is_privileged(_event("!wheel", is_mod=badge, is_broadcaster=badge)) is False


@pytest.mark.parametrize(
    ("is_mod", "is_broadcaster"),
    [
        ("false", False),
        (False, "false"),
        ("0", False),
        (False, "no"),
        ("false", "false"),
    ],
)
def test_a_string_badge_beside_a_real_false_is_still_a_denial(
    fake_host: Any, is_mod: Any, is_broadcaster: Any
) -> None:
    """The exact bypass: one real bool passes the isinstance guard, then `bool("false")` is True."""
    assert (
        _is_privileged(_event("!wheel", is_mod=is_mod, is_broadcaster=is_broadcaster))
        is False
    )


@pytest.mark.parametrize("verb", ["add x", "remove x", "reset"])
def test_string_false_mod_cannot_mutate_the_wheel_end_to_end(
    fake_host: Any, verb: str
) -> None:
    _seed_options(fake_host, "comm-1", ["tacos", "x"])
    before = list(_stored_options(fake_host, "comm-1"))
    kv_calls_before = len(fake_host.kv.calls)

    result = _transform_in(
        "comm-1", f"!wheel {verb}", is_mod="false", is_broadcaster="false"
    )

    assert _reply_text(result) == _PERMISSION_DENIED_MSG
    assert _stored_options(fake_host, "comm-1") == before
    assert len(fake_host.kv.calls) == kv_calls_before, (
        "a denied command must not touch kv"
    )


def test_real_mod_and_real_broadcaster_still_pass(fake_host: Any) -> None:
    assert "Added" in _reply_text(_transform_in("comm-1", "!wheel add a", is_mod=True))
    assert "Added" in _reply_text(
        _transform_in("comm-1", "!wheel add b", is_mod=False, is_broadcaster=True)
    )
    assert _stored_options(fake_host, "comm-1") == ["a", "b"]


def test_reads_stay_open_to_a_string_badge_caller(fake_host: Any) -> None:
    _seed_options(fake_host, "comm-1", ["tacos"])
    result = _transform_in(
        "comm-1", "!wheel list", is_mod="false", is_broadcaster="false"
    )
    assert _reply_text(result) == "Wheel options: tacos"


# PII-free error logs


def _error_lines(host: Any) -> list[tuple[str, dict[str, Any]]]:
    return [(m, json.loads(f)) for lvl, m, f in host.log_calls if lvl == 0]


def test_backend_error_text_is_never_logged(fake_host: Any) -> None:
    def _boom(*_a: Any) -> Any:
        raise RuntimeError(f"backend said: {SECRET}")

    sys.modules["wit_world"].imports.kv.get = _boom
    result = _transform_in("comm-1", "!wheel list")
    assert _reply_text(result) == _KV_ERROR_MSG
    errors = _error_lines(fake_host)
    assert errors == [("wheel.kv_failure", {"op": "kv_get", "error": "RuntimeError"})]
    assert SECRET not in json.dumps(fake_host.log_calls)


@pytest.mark.parametrize(
    ("blob", "op", "error"),
    [
        (f"not json {SECRET}".encode(), "options_decode", "JSONDecodeError"),
        (b"\xff\xfe" + SECRET.encode(), "options_decode", "UnicodeDecodeError"),
        (json.dumps({"k": SECRET}).encode(), "options_shape", "NotAStringList"),
        (json.dumps([SECRET, 3]).encode(), "options_shape", "NotAStringList"),
    ],
)
def test_corrupt_blob_content_is_never_logged(
    fake_host: Any, blob: bytes, op: str, error: str
) -> None:
    fake_host.kv.store[_scoped_key("comm-1", OPTIONS_KEY)] = blob
    _transform_in("comm-1", "!wheel list")
    assert _error_lines(fake_host) == [("wheel.kv_failure", {"op": op, "error": error})]
    assert SECRET not in json.dumps(fake_host.log_calls)


def test_every_log_line_over_a_full_session_is_allow_listed_and_secret_free(
    fake_host: Any,
) -> None:
    allowed = {
        "wheel.transform matched": {"command"},
        "wheel.option_added": {"option_count"},
        "wheel.option_removed": {"option_count"},
        "wheel.spin": {"option_count"},
        "wheel.reset": set(),
        "wheel.permission_denied": {"action"},
        "wheel.role_info_unavailable": {"platform"},
        "wheel.kv_failure": {"op", "error"},
    }
    session = [
        (f"!wheel add {SECRET}", True),
        (f"!wheel add {SECRET}-2", True),
        ("!wheel", False),
        ("!wheel list", False),
        (f"!wheel remove {SECRET}", True),
        (f"!wheel add {SECRET}", False),  # denied
        (f"!wheel {SECRET}", False),  # unknown sub-command
        ("!wheel reset", True),
    ]
    for text, mod in session:
        _transform_in("comm-1", text, is_mod=mod, actor=SECRET)
    assert len(fake_host.log_calls) >= 10, "denominator: the session must actually log"
    for _lvl, message, fields_json in fake_host.log_calls:
        assert message in allowed, f"unexpected log message {message!r}"
        assert set(json.loads(fields_json)) <= allowed[message]
        assert SECRET.lower() not in (message + fields_json).lower()
