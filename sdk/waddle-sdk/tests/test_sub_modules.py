"""Tests for `waddle_sdk.sub_modules.SubModuleGate`."""

from __future__ import annotations

import sys
import types

import pytest

from waddle_sdk.command import CommandUsageError, ParsedCommand
from waddle_sdk.sub_modules import SubModuleGate


def _run(coro):
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("sub_modules coroutine unexpectedly suspended")


@pytest.fixture
def fake_kv(monkeypatch: pytest.MonkeyPatch):
    store: dict[str, bytes] = {}

    def get(key: str):
        return store.get(key)

    def set_(key: str, value: bytes, ttl_seconds: int) -> None:
        store[key] = bytes(value)

    def delete(key: str) -> None:
        store.pop(key, None)

    def increment(key: str, delta: int, ttl_seconds: int) -> int:
        current = int(store.get(key, b"0"))
        new_value = current + delta
        store[key] = str(new_value).encode()
        return new_value

    kv_mod = types.SimpleNamespace(get=get, set=set_, delete=delete, increment=increment)
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(kv=kv_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    return store


def test_submodule_is_disabled_by_default(fake_kv) -> None:
    """A never-toggled sub-module reports disabled -- default OFF."""
    gate = SubModuleGate(command="shoutout")
    assert _run(gate.is_enabled("community-1", "auto")) is False


def test_enable_then_is_enabled_true(fake_kv) -> None:
    """enable() flips is_enabled() to True for that community only."""
    gate = SubModuleGate(command="shoutout")
    _run(gate.enable("community-1", "auto"))
    assert _run(gate.is_enabled("community-1", "auto")) is True


def test_enable_is_scoped_per_community(fake_kv) -> None:
    """Enabling a sub-module in one community never enables it in another."""
    gate = SubModuleGate(command="shoutout")
    _run(gate.enable("community-1", "auto"))
    assert _run(gate.is_enabled("community-2", "auto")) is False


def test_disable_resets_to_default_off(fake_kv) -> None:
    """disable() after enable() returns the sub-module to its default-OFF state."""
    gate = SubModuleGate(command="shoutout")
    _run(gate.enable("community-1", "auto"))
    _run(gate.disable("community-1", "auto"))
    assert _run(gate.is_enabled("community-1", "auto")) is False


def test_two_commands_same_submodule_name_dont_collide(fake_kv) -> None:
    """`command` namespaces the stored key -- two commands' same-named sub-module don't collide."""
    shoutout_gate = SubModuleGate(command="shoutout")
    command_gate = SubModuleGate(command="command")
    _run(shoutout_gate.enable("community-1", "ai"))
    assert _run(command_gate.is_enabled("community-1", "ai")) is False


def test_apply_toggle_enable(fake_kv) -> None:
    """apply_toggle() with an enable ParsedCommand enables and returns True."""
    gate = SubModuleGate(command="lurk")
    parsed = ParsedCommand(command="lurk", sub_module="ai", option="enable", args=None)
    assert _run(gate.apply_toggle("community-1", parsed)) is True
    assert _run(gate.is_enabled("community-1", "ai")) is True


def test_apply_toggle_disable(fake_kv) -> None:
    """apply_toggle() with a disable ParsedCommand disables and returns False."""
    gate = SubModuleGate(command="lurk")
    _run(gate.enable("community-1", "ai"))
    parsed = ParsedCommand(command="lurk", sub_module="ai", option="disable", args=None)
    assert _run(gate.apply_toggle("community-1", parsed)) is False
    assert _run(gate.is_enabled("community-1", "ai")) is False


def test_apply_toggle_non_toggle_option_returns_none(fake_kv) -> None:
    """apply_toggle() on a non-enable/disable ParsedCommand is a no-op returning None."""
    gate = SubModuleGate(command="lurk")
    parsed = ParsedCommand(command="lurk", sub_module=None, option="set", args="5")
    assert _run(gate.apply_toggle("community-1", parsed)) is None


def test_apply_toggle_missing_submodule_raises(fake_kv) -> None:
    """A toggle ParsedCommand with no sub_module (shouldn't happen via parse_command) fails loud."""
    gate = SubModuleGate(command="lurk")
    parsed = ParsedCommand(command="lurk", sub_module=None, option="enable", args=None)
    with pytest.raises(CommandUsageError, match="no sub-module to toggle"):
        _run(gate.apply_toggle("community-1", parsed))
