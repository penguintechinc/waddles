"""Host-native tests for the `ship` bundle (fake `kv`/`flags`/`relay`/`log` host)."""

from __future__ import annotations

import asyncio
import json
import sys
import types
import uuid
from typing import Any

import pytest
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import FakeKvHost, install_fake_kv_host

import app as bundle
from app import dispatch, transform


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _Host:
    def __init__(self, kv: FakeKvHost) -> None:
        self.kv = kv
        self.relay_calls: list[tuple[str, Any]] = []
        self.log_calls: list[tuple[int, str, str]] = []
        self.flag = True


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> _Host:
    h = _Host(install_fake_kv_host(monkeypatch))
    wit = sys.modules["wit_world"]
    wit.imports.flags = types.SimpleNamespace(enabled=lambda key, default_value: h.flag)  # type: ignore[attr-defined]
    wit.imports.relay = types.SimpleNamespace(  # type: ignore[attr-defined]
        push=lambda provider, msg: h.relay_calls.append((provider, msg))
    )
    wit.imports.log = types.SimpleNamespace(  # type: ignore[attr-defined]
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields_json: h.log_calls.append((lvl, msg, fields_json)),
    )
    return h


def _event(
    text: str,
    *,
    is_mod: bool | None = False,
    actor: str = "viewer-1",
    platform: str = "twitch",
    author_id: str | None = None,
) -> PlatformEvent:
    payload: dict[str, Any] = {"text": text, "channel_id": "chan-1"}
    if author_id is not None:
        payload["author_id"] = author_id
    if is_mod is not None:
        payload["is_mod"] = is_mod
    return PlatformEvent(
        platform=platform,
        event_type="chat.message",
        actor=actor,
        payload=payload,
        occurred_at="2026-10-09T00:00:00.000Z",
    )


def _envelope(event: PlatformEvent) -> StageEnvelope:
    return StageEnvelope(
        tenant="tenant-1",
        community="comm-1",
        app_id="waddles.core.example.ship",
        stage="action",
        event=event,
        ts="2026-10-09T00:00:00.000Z",
    )


def _say(text: str, **kw: Any) -> str:
    result = _run(transform(_event(text, **kw)))
    assert result is not None
    return str(result.payload["text"])


def _log_text(host: _Host) -> str:
    return " ".join(f"{m} {f}" for _, m, f in host.log_calls).lower()


def test_non_command_and_prefix_collision_ignored(host: _Host) -> None:
    assert _run(transform(_event("hello"))) is None
    assert _run(transform(_event("!ships"))) is None


def test_missing_text_ignored_and_wiring_exports(host: _Host) -> None:
    notext = PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="a",
        payload={"channel_id": "chan-1"},
        occurred_at="t",
    )
    assert _run(transform(notext)) is None
    import _entry_wiring

    assert _entry_wiring.bundle_transform is transform
    assert _entry_wiring.bundle_dispatch is dispatch


def test_flag_off_returns_none(host: _Host) -> None:
    host.flag = False
    assert _run(transform(_event("!ship"))) is None


def test_dispatch_relays_and_rejects_bad_payload(host: _Host) -> None:
    out = _run(transform(_event("!ship")))
    assert out is not None
    result = _run(dispatch(_envelope(out), {}, http_client=None))
    assert result.detail == "relayed"
    assert host.relay_calls[0][0] == "twitch"
    assert json.loads(host.relay_calls[0][1])["channel"] == "chan-1"
    notext = PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="a",
        payload={"channel_id": "chan-1"},
        occurred_at="t",
    )
    with pytest.raises(ValueError):
        _run(dispatch(_envelope(notext), {}, http_client=None))
    nochan = PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="a",
        payload={"text": "hi"},
        occurred_at="t",
    )
    with pytest.raises(ValueError):
        _run(dispatch(_envelope(nochan), {}, http_client=None))


def test_deterministic_and_order_independent(host: _Host) -> None:
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    first = _say(f"!ship {a} {b}")
    assert _say(f"!ship {a} {b}") == first
    pct = bundle._compute_match(a, b)[0]
    assert f"= {pct}%" in first
    assert bundle._compute_match(a, b) == bundle._compute_match(b, a)
    assert 0 <= pct <= 100


def test_self_ship_is_100(host: _Host) -> None:
    a = str(uuid.uuid4())
    assert "= 100%" in _say(f"!ship {a} {a}")
    assert "= 100%" in _say("!ship @Bob @bob")


def test_usage_and_bad_target_are_explicit(host: _Host) -> None:
    assert _say("!ship") == bundle._USAGE
    assert _say("!ship onlyone") == bundle._USAGE
    assert _say("!ship a b c") == bundle._USAGE
    assert _say("!ship ??? @ok") == bundle._BAD_TARGET_MSG


def test_replies_and_logs_never_echo_handles(host: _Host) -> None:
    reply = _say("!ship @SecretOne @SecretTwo")
    assert "secret" not in reply.lower()
    assert "secret" not in _log_text(host)
    assert reply.count("-") >= 1


def test_percent_distribution_covers_range() -> None:
    values = {bundle._compute_match(str(uuid.uuid4()), str(uuid.uuid4()))[0] for _ in range(400)}
    assert min(values) < 20 and max(values) > 80
    assert all(0 <= v <= 100 for v in values)
