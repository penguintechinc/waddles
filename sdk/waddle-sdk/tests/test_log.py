"""Tests for `waddle_sdk.log`."""

from __future__ import annotations

import json
import sys
import types

import pytest
import wit_shapes

import waddle_sdk.log as log
from waddle_sdk._json_guard import NonObjectJsonError


@pytest.fixture
def fake_log(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[wit_shapes.Level, str, str]] = []

    def write(lvl: wit_shapes.Level, message: str, fields_json: str) -> None:
        calls.append((lvl, message, fields_json))

    log_mod = types.SimpleNamespace(write=write, Level=wit_shapes.Level)
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(log=log_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    return calls


def test_debug_writes_debug_level(fake_log) -> None:
    """debug() passes the real generated Level.DEBUG enum member, not a string."""
    log.debug("hello", widget_id="w1")
    lvl, message, fields_json = fake_log[0]
    assert lvl is wit_shapes.Level.DEBUG
    assert message == "hello"
    assert json.loads(fields_json) == {"widget_id": "w1"}


def test_info_warn_error_use_correct_levels(fake_log) -> None:
    """info()/warn()/error() each pass the matching Level enum member."""
    log.info("i")
    log.warn("w")
    log.error("e")
    assert [c[0] for c in fake_log] == [
        wit_shapes.Level.INFO,
        wit_shapes.Level.WARN,
        wit_shapes.Level.ERROR,
    ]


def test_no_fields_serializes_to_empty_object(fake_log) -> None:
    """Calling with no keyword fields serializes fields_json as `{}`."""
    log.info("no fields")
    _, _, fields_json = fake_log[0]
    assert fields_json == "{}"


def test_write_rejects_non_object_fields(fake_log) -> None:
    """`_write` raises NonObjectJsonError if `fields` isn't a dict.

    The public `debug`/`info`/`warn`/`error` API always builds `fields` from
    `**fields: Any`, which is always a dict -- this exercises `_write`'s own
    guard directly, mirroring the Rust SDK's boundary-wide object-shape check
    rather than assuming the public API is the only caller forever.
    """
    with pytest.raises(NonObjectJsonError, match="expected a JSON object"):
        log._write("INFO", "bad fields", ["not", "an", "object"])  # type: ignore[arg-type]
    assert fake_log == []  # never crossed the WIT boundary
