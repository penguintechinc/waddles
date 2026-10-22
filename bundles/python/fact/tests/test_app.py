"""Host-native tests for the `fact` bundle (fake `wit_world`: flags/relay/log, no kv)."""

from __future__ import annotations

import asyncio
import json
import random
import sys
import types
from pathlib import Path
from typing import Any, cast

import app
import pytest
from app import build_reply, dispatch, transform
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _Host:
    def __init__(self) -> None:
        self.relay_calls: list[tuple[str, str]] = []
        self.log_calls: list[tuple[int, str, str]] = []
        self.flag = True


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> _Host:
    h = _Host()
    wit = types.ModuleType("wit_world")
    wit.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=types.SimpleNamespace(enabled=lambda key, default_value: h.flag),
        relay=types.SimpleNamespace(push=lambda p, m: h.relay_calls.append((p, m))),
        log=types.SimpleNamespace(
            Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
            write=lambda lvl, msg, f: h.log_calls.append((lvl, msg, f)),
        ),
    )
    monkeypatch.setitem(sys.modules, "wit_world", wit)
    monkeypatch.setattr(app, "_rng", random.Random(1234))
    return h


def _event(
    text: str, platform: str = "twitch", channel_id: str | None = "12345"
) -> PlatformEvent:
    return PlatformEvent(
        platform=platform,
        event_type="chat.message",
        actor="raw-actor-name",
        payload={"text": text, "channel_id": channel_id},
        occurred_at="2026-10-21T00:00:00.000Z",
    )


def _env(event: PlatformEvent) -> StageEnvelope:
    return StageEnvelope(
        tenant="t-1",
        community="comm-1",
        app_id="waddles.core.example.fact",
        stage="action",
        event=event,
        ts="2026-10-21T00:00:00.000Z",
    )


def _text(out: PlatformEvent | None) -> str:
    assert out is not None
    return cast(str, out.payload["text"])


def test_non_matching_returns_none(host: _Host) -> None:
    assert _run(transform(_event("hello world"))) is None
    assert _run(transform(_event("!other"))) is None
    ev = _event("x")
    ev.payload["text"] = 5
    assert _run(transform(ev)) is None


def test_flag_off_returns_none(host: _Host) -> None:
    host.flag = False
    assert _run(transform(_event("!fact"))) is None


@pytest.mark.parametrize("platform", ["twitch", "discord"])
def test_command_case_insensitive_and_platform(host: _Host, platform: str) -> None:
    out = _run(transform(_event("!FACT", platform)))
    assert out is not None and out.platform == platform
    assert out.payload["channel_id"] == "12345"


def test_seeded_determinism(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    app._rng = random.Random(7)
    first = [build_reply("") for _ in range(5)]
    app._rng = random.Random(7)
    assert first == [build_reply("") for _ in range(5)]


def test_bare_replies_from_pool(host: _Host) -> None:
    for _ in range(30):
        assert (
            _text(_run(transform(_event("!fact")))).removeprefix(app._PREFIX)
            in app.FACTS
        )


def test_extra_args_give_usage(host: _Host) -> None:
    for junk in ("hello", "add foo", "{user:tok}"):
        assert _text(_run(transform(_event(f"!fact {junk}")))) == app._USAGE


def test_pool_sanity() -> None:
    assert len(set(app.FACTS)) == len(app.FACTS) >= 15
    assert all(0 < len(s) <= 200 for s in app.FACTS)


def test_dispatch_relays_reply(host: _Host) -> None:
    out = _run(transform(_event("!fact")))
    res = _run(dispatch(_env(cast(PlatformEvent, out)), {}, http_client=None))
    assert res.transport == "twitch" and res.detail == "relayed"
    provider, msg = host.relay_calls[-1]
    assert provider == "twitch"
    body = json.loads(msg)
    assert body["channel"] == "12345" and body["text"] == _text(out)


def test_dispatch_requires_channel(host: _Host) -> None:
    out = _run(transform(_event("!fact", channel_id=None)))
    with pytest.raises(ValueError):
        _run(dispatch(_env(cast(PlatformEvent, out)), {}, http_client=None))


def test_logs_are_pii_free(host: _Host) -> None:
    _run(transform(_event("!fact")))
    joined = " ".join(f"{m} {f}" for _l, m, f in host.log_calls)
    assert host.log_calls and "raw-actor-name" not in joined and "12345" not in joined


def test_no_kv_import() -> None:
    assert "community_kv" not in Path(app.__file__).read_text()
