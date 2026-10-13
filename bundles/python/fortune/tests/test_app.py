"""Host-native tests for the `fortune` bundle -- fake-`wit_world` approach mirrors `dice`'s tests."""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest
from app import FORTUNES, dispatch, transform
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope


def _run(coro):
    return asyncio.run(coro)


def _event(text: str, *, channel_id: str | None = "12345") -> PlatformEvent:
    return PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="viewer-1",
        payload={"text": text, "channel_id": channel_id},
        occurred_at="2026-10-03T00:00:00.000Z",
    )


def _envelope(
    platform: str, text: str, *, channel_id: str | None = "12345"
) -> StageEnvelope:
    return StageEnvelope(
        tenant="tenant-1",
        community=None,
        app_id="waddles.core.example.fortune",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="chat.message",
            actor="viewer-1",
            payload={"text": text, "channel_id": channel_id},
            occurred_at="2026-10-03T00:00:00.000Z",
        ),
        ts="2026-10-03T00:00:00.000Z",
    )


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch):
    """Fake WIT host: flag on, recording relay + log."""
    relays: list = []
    logs: list = []
    world = types.ModuleType("wit_world")
    world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=types.SimpleNamespace(enabled=lambda key, default_value: True),
        relay=types.SimpleNamespace(push=lambda p, m: relays.append((p, m))),
        log=types.SimpleNamespace(
            Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
            write=lambda lvl, msg, f: logs.append((lvl, msg, f)),
        ),
    )
    monkeypatch.setitem(sys.modules, "wit_world", world)
    return relays, logs


def test_fortune_list_is_about_thirty_unique_lines() -> None:
    assert len(FORTUNES) >= 30
    assert len(set(FORTUNES)) == len(FORTUNES)


def test_fortune_replies_with_a_known_line(host) -> None:
    result = _run(transform(_event("!fortune")))
    assert result is not None
    assert any(f in result.payload["text"] for f in FORTUNES)
    assert result.payload["channel_id"] == "12345"


@pytest.mark.parametrize(
    "text", ["!fortunes", "fortune", "!fortunecookie", "hello", ""]
)
def test_non_matching_text_produces_no_reply(text: str, host) -> None:
    assert _run(transform(_event(text))) is None


def test_non_chat_payload_is_ignored() -> None:
    event = PlatformEvent(
        platform="twitch",
        event_type="channel.follow",
        actor=None,
        payload={},
        occurred_at="",
    )
    assert _run(transform(event)) is None


def test_disabled_flag_suppresses_the_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    world = types.ModuleType("wit_world")
    world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=types.SimpleNamespace(enabled=lambda key, default_value: False)
    )
    monkeypatch.setitem(sys.modules, "wit_world", world)
    assert _run(transform(_event("!fortune"))) is None


@pytest.mark.parametrize("platform", ["twitch", "discord"])
def test_dispatch_relays_to_origin_platform(platform: str, host) -> None:
    relays, _ = host
    result = _run(dispatch(_envelope(platform, "hi"), {}, http_client=None))
    assert result.transport == platform
    assert relays[0][0] == platform
    assert json.loads(relays[0][1]) == {"channel": "12345", "text": "hi"}


def test_dispatch_raises_without_channel_id(host) -> None:
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(_envelope("twitch", "hi", channel_id=None), {}, http_client=None))


def test_logs_never_contain_actor_or_user_text(host) -> None:
    _, logs = host
    _run(transform(_event("!fortune secret-words")))
    _run(dispatch(_envelope("twitch", "x"), {}, http_client=None))
    assert logs
    for _lvl, msg, fields in logs:
        assert "viewer-1" not in msg + fields
        assert "secret-words" not in msg + fields
