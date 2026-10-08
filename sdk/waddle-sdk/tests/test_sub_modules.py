"""Tests for `waddle_sdk.sub_modules.SubModuleGate`."""

from __future__ import annotations

import pytest

from waddle_sdk.command import CommandUsageError, ParsedCommand
from waddle_sdk.sub_modules import SubModuleGate
from waddle_sdk.testing import install_fake_kv_host


def _run(coro):
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("sub_modules coroutine unexpectedly suspended")


@pytest.fixture
def fake_kv(monkeypatch: pytest.MonkeyPatch):
    return install_fake_kv_host(monkeypatch).store


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


def test_key_contains_no_colon() -> None:
    """regression: gh-631 -- `_key` originally used `:`, rejected by the real `kv` host."""
    gate = SubModuleGate(command="shoutout")
    assert ":" not in gate._key("auto")


def test_enable_satisfies_host_guest_key_charset(fake_kv) -> None:
    gate = SubModuleGate(command="shoutout")
    _run(gate.enable("community-1", "auto"))  # raises InvalidKvKeyError if the key is bad
