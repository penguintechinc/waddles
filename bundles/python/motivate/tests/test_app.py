"""Host-native tests for the `motivate` bundle (fake `wit_world`: flags/relay/log, no kv)."""

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
        app_id="waddles.core.example.motivate",
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


@pytest.mark.parametrize("platform", ["twitch", "discord"])
def test_command_case_insensitive_and_platform(host: _Host, platform: str) -> None:
    out = _run(transform(_event("!MOTIVATE", platform)))
    assert out is not None and out.platform == platform
    assert out.payload["channel_id"] == "12345"


def test_seeded_determinism(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    app._rng = random.Random(7)
    first = [build_reply("{user:tok-1}") for _ in range(5)]
    app._rng = random.Random(7)
    assert first == [build_reply("{user:tok-1}") for _ in range(5)]


def test_bare_replies_from_pool(host: _Host) -> None:
    for _ in range(30):
        assert (
            _text(_run(transform(_event("!motivate")))).removeprefix(app._PREFIX)
            in app.LINES
        )


def test_target_mention_token(host: _Host) -> None:
    text = _text(_run(transform(_event("!motivate {user:tok-9}"))))
    assert text.startswith(app._PREFIX + "{user:tok-9}, ")
    assert text.split(", ", 1)[1] in app.LINES


@pytest.mark.parametrize(
    "junk",
    [
        "@rawname",
        "rawname",
        "{user:a} {user:b}",
        "{user:a} extra",
        "{user:}",
        "\\{user:a\\}",
        "x" * 400,
    ],
)
def test_bad_target_gives_usage_without_echo(host: _Host, junk: str) -> None:
    text = _text(_run(transform(_event(f"!motivate {junk}"))))
    assert text == app._USAGE and "rawname" not in text


def test_pool_sanity() -> None:
    assert len(set(app.LINES)) == len(app.LINES) >= 15
    assert all(0 < len(s) <= 200 for s in app.LINES)


def test_dispatch_relays_reply(host: _Host) -> None:
    out = _run(transform(_event("!motivate")))
    res = _run(dispatch(_env(cast(PlatformEvent, out)), {}, http_client=None))
    assert res.transport == "twitch" and res.detail == "relayed"
    provider, msg = host.relay_calls[-1]
    assert provider == "twitch"
    body = json.loads(msg)
    assert body["channel"] == "12345" and body["text"] == _text(out)


def test_dispatch_requires_channel(host: _Host) -> None:
    out = _run(transform(_event("!motivate", channel_id=None)))
    with pytest.raises(ValueError):
        _run(dispatch(_env(cast(PlatformEvent, out)), {}, http_client=None))


def test_logs_are_pii_free(host: _Host) -> None:
    _run(transform(_event("!motivate")))
    joined = " ".join(f"{m} {f}" for _l, m, f in host.log_calls)
    assert host.log_calls and "raw-actor-name" not in joined and "12345" not in joined


def test_no_kv_import() -> None:
    assert "community_kv" not in Path(app.__file__).read_text()


# hardening: activation-only
def test_runs_on_activation_alone_when_flag_backend_says_off(host) -> None:
    """Activation-gated only: an OFF `waddles.command-motivate` flag must not drop the command."""
    host.flag = False
    out = _run(transform(_event("!motivate")))
    assert out is not None
