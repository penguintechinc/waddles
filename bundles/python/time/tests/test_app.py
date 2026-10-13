"""Host-native tests for the `time` bundle's `transform`/`dispatch` logic.

No WASM -- a fake `wit_world` stands in for the WIT host imports, same pattern as
`bundles/python/boop/tests/test_app.py`.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any

import pytest
from app import dispatch, transform
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _sample_event(text: str, *, channel_id: str | None = "12345") -> PlatformEvent:
    return PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="viewer-1",
        payload={"text": text, "channel_id": channel_id},
        occurred_at="2026-10-08T00:00:00.000Z",
    )


class _Host:
    """Recorded fake-host state shared with the test body."""

    def __init__(self) -> None:
        self.relay_calls: list[tuple[str, str]] = []
        self.log_calls: list[tuple[int, str, str]] = []
        self.flag_enabled = True
        self.now_ms = 1_700_000_000_000  # 2023-11-14T22:13:20Z


@pytest.fixture
def fake_host(monkeypatch: pytest.MonkeyPatch) -> _Host:
    """Fake WIT host: flags on, relay/log/clock recorded."""
    host = _Host()
    wit = types.ModuleType("wit_world")
    wit.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=types.SimpleNamespace(enabled=lambda key, default_value: host.flag_enabled),
        relay=types.SimpleNamespace(push=lambda p, m: host.relay_calls.append((p, m))),
        log=types.SimpleNamespace(
            Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
            write=lambda lvl, msg, fields: host.log_calls.append((lvl, msg, fields)),
        ),
        clock=types.SimpleNamespace(
            now_millis=lambda: host.now_ms,
            now_rfc3339=lambda: "2023-11-14T22:13:20.000Z",
            monotonic_nanos=lambda: 0,
        ),
    )
    monkeypatch.setitem(sys.modules, "wit_world", wit)
    return host


def _envelope(platform: str, text: str, *, channel_id: str | None = "12345") -> StageEnvelope:
    return StageEnvelope(
        tenant="tenant-1",
        community=None,
        app_id="waddles.core.example.time",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="chat.message",
            actor="viewer-1",
            payload={"text": text, "channel_id": channel_id},
            occurred_at="2026-10-08T00:00:00.000Z",
        ),
        ts="2026-10-08T00:00:00.000Z",
    )


def test_time_replies_with_utc_time_from_the_clock(fake_host: _Host) -> None:
    result = _run(transform(_sample_event("!time")))
    assert result is not None
    assert result.payload["text"] == "Current UTC time: 2023-11-14 22:13:20 UTC"


def test_time_follows_the_clock(fake_host: _Host) -> None:
    fake_host.now_ms += 3_600_000
    assert _run(transform(_sample_event("!time"))).payload["text"].endswith("23:13:20 UTC")


def test_clock_is_imported_eagerly_at_module_top() -> None:
    import app
    import waddle_sdk

    assert app.clock is waddle_sdk.clock


@pytest.mark.parametrize("text", ["!timeping", "time", "", "hello world", "x !time"])
def test_non_matching_text_produces_no_reply(text: str, fake_host: _Host) -> None:
    assert _run(transform(_sample_event(text))) is None


def test_non_chat_payload_is_ignored() -> None:
    event = PlatformEvent(
        platform="twitch", event_type="channel.follow", actor=None, payload={}, occurred_at=""
    )
    assert _run(transform(event)) is None


def test_disabled_flag_suppresses_the_reply(fake_host: _Host) -> None:
    fake_host.flag_enabled = False
    assert _run(transform(_sample_event("!time"))) is None


@pytest.mark.parametrize("platform", ["twitch", "discord"])
def test_dispatch_relays_to_origin_platform(platform: str, fake_host: _Host) -> None:
    result = _run(dispatch(_envelope(platform, "reply"), {}, http_client=None))
    assert result.transport == platform
    provider, message_json = fake_host.relay_calls[0]
    assert provider == platform
    assert json.loads(message_json) == {"channel": "12345", "text": "reply"}


def test_dispatch_raises_when_channel_id_missing(fake_host: _Host) -> None:
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(_envelope("twitch", "reply", channel_id=None), {}, http_client=None))
    assert fake_host.relay_calls == []


def test_logs_never_contain_actor_or_typed_argument(fake_host: _Host) -> None:
    _run(transform(_sample_event("!time")))
    _run(dispatch(_envelope("twitch", "reply"), {}, http_client=None))
    assert fake_host.log_calls
    for _lvl, message, fields_json in fake_host.log_calls:
        assert "viewer-1" not in message + fields_json
        assert "secretviewer" not in message + fields_json
        assert "actor" not in json.loads(fields_json)
