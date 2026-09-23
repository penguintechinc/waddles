"""Tests for `waddle_sdk.clock`."""

from __future__ import annotations

import sys
import types

import pytest

import waddle_sdk.clock as clock


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch):
    clock_mod = types.SimpleNamespace(
        now_millis=lambda: 1_757_800_000_000,
        now_rfc3339=lambda: "2026-09-14T00:00:00.000Z",
        monotonic_nanos=lambda: 123456789,
    )
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(clock=clock_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    return clock_mod


def test_now_millis(fake_clock) -> None:
    """now_millis() returns the WIT clock's value as an int."""
    assert clock.now_millis() == 1_757_800_000_000


def test_now_rfc3339(fake_clock) -> None:
    """now_rfc3339() returns the WIT clock's value as a str."""
    assert clock.now_rfc3339() == "2026-09-14T00:00:00.000Z"


def test_monotonic_nanos(fake_clock) -> None:
    """monotonic_nanos() returns the WIT clock's value as an int."""
    assert clock.monotonic_nanos() == 123456789
