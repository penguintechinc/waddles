"""Host-native tests for the `pyping` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- `transform()` is plain async Python, testable
directly; `dispatch()` is tested against a fake `wit_world.imports.relay`
module (same pattern as `sdk/waddle-sdk/tests/test_relay.py`), isolating
"does this bundle build the right relay call" from "does the WIT import
actually work", which is `waddle_sdk`'s own, already-covered concern.
Mirrors `bundles/rust/ping/src/lib.rs`'s `#[cfg(test)] mod tests` coverage.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

from app import PING_COMMAND, PONG_REPLY, dispatch, transform
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope


def _run(coro):
    return asyncio.run(coro)


def _sample_event(text: str, *, channel_id: str | None = "12345") -> PlatformEvent:
    return PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="viewer-1",
        payload={"text": text, "channel_id": channel_id},
        occurred_at="2026-09-23T00:00:00.000Z",
    )


def test_pyping_produces_a_pong_reply_on_the_same_platform_and_channel() -> None:
    event = _sample_event(PING_COMMAND)
    result = _run(transform(event))

    assert result is not None
    assert result.platform == event.platform
    assert result.event_type == event.event_type
    assert result.payload["text"] == PONG_REPLY
    assert result.payload["channel_id"] == "12345"


def test_pyping_with_surrounding_whitespace_still_matches() -> None:
    result = _run(transform(_sample_event(f"  {PING_COMMAND}  ")))
    assert result is not None


@pytest.mark.parametrize("text", ["!pypingpong", "pyping", "!pyping extra", "hello", ""])
def test_non_matching_text_produces_no_reply(text: str) -> None:
    assert _run(transform(_sample_event(text))) is None


def test_non_chat_payload_is_ignored_rather_than_erroring() -> None:
    event = PlatformEvent(
        platform="twitch",
        event_type="channel.follow",
        actor=None,
        payload={},
        occurred_at="2026-09-23T00:00:00.000Z",
    )
    assert _run(transform(event)) is None


def _sample_envelope(platform: str, *, channel_id: str | None = "12345") -> StageEnvelope:
    return StageEnvelope(
        tenant="tenant-1",
        community=None,
        app_id="waddles.core.example.pyping",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="chat.message",
            actor="viewer-1",
            payload={"text": PONG_REPLY, "channel_id": channel_id},
            occurred_at="2026-09-23T00:00:00.000Z",
        ),
        ts="2026-09-23T00:00:00.000Z",
    )


@pytest.fixture
def fake_relay(monkeypatch: pytest.MonkeyPatch):
    """Stand in for the WIT `relay` host import (action-stage only)."""
    calls: list[tuple[str, str]] = []

    def push(provider: str, message_json: str) -> None:
        calls.append((provider, message_json))

    relay_mod = types.SimpleNamespace(push=push)
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(relay=relay_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    return calls


@pytest.mark.parametrize("platform", ["twitch", "discord"])
def test_dispatch_relays_to_the_events_own_origin_platform_not_a_hardcoded_one(
    platform: str, fake_relay
) -> None:
    """Regression guard mirroring `bundles/rust/ping`'s own regression test: the relay
    provider must always track `envelope.event.platform`, never a fixed constant.
    """
    envelope = _sample_envelope(platform)
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.transport == platform
    provider, message_json = fake_relay[0]
    assert provider == platform
    assert json.loads(message_json) == {"channel": "12345", "text": PONG_REPLY}


def test_dispatch_raises_when_channel_id_is_missing(fake_relay) -> None:
    envelope = _sample_envelope("twitch", channel_id=None)

    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(envelope, {}, http_client=None))
    assert fake_relay == []  # never crossed the WIT boundary
