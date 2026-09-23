"""Tests for `waddle_sdk.relay`."""

from __future__ import annotations

import json
import sys
import types

import pytest

import waddle_sdk.relay as relay


def _run(coro):
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("relay coroutine unexpectedly suspended")


@pytest.fixture
def fake_relay(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[str, str]] = []

    def push(provider: str, message_json: str) -> None:
        calls.append((provider, message_json))

    relay_mod = types.SimpleNamespace(push=push)
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(relay=relay_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    return calls


def test_push_serializes_message_to_json(fake_relay) -> None:
    """push() serializes the message dict to canonical JSON before crossing the WIT boundary."""
    _run(relay.push("discord", {"channel_id": "c1", "text": "hi"}))
    provider, message_json = fake_relay[0]
    assert provider == "discord"
    assert json.loads(message_json) == {"channel_id": "c1", "text": "hi"}
