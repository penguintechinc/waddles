"""Host-native tests for the `say` bundle -- fake `wit_world` approach as `fish`'s tests."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

from app import _DENIED_MSG, _UNSAFE_MSG, _USAGE, MAX_SAY_LEN, dispatch, transform

_BUNDLE_DIR = Path(__file__).resolve().parent.parent


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _FakeHost:
    def __init__(self) -> None:
        self.relay_calls: list[tuple[str, str]] = []
        self.log_calls: list[tuple[Any, str, str]] = []


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> _FakeHost:
    h = _FakeHost()
    relay_mod = types.SimpleNamespace(push=lambda p, m: h.relay_calls.append((p, m)))
    log_mod = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields: h.log_calls.append((lvl, msg, fields)),
    )
    world = types.ModuleType("wit_world")
    world.imports = types.SimpleNamespace(relay=relay_mod, log=log_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", world)
    return h


def _event(text: Any, **badges: Any) -> PlatformEvent:
    payload: dict[str, Any] = {"text": text, "channel_id": "chan-1", **badges}
    return PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="viewer-1",
        payload=payload,
        occurred_at="2026-10-21T00:00:00.000Z",
    )


def _run_through(host: _FakeHost, text: str, **badges: Any) -> str | None:
    """Run transform -> dispatch; return the relayed text, or None if transform dropped it."""
    out = _run(transform(_event(text, **badges)))
    if out is None:
        return None
    env = StageEnvelope(
        tenant="t",
        community="c1",
        app_id="waddles.core.example.say",
        stage="action",
        event=out,
        ts="2026-10-21T00:00:00.000Z",
    )
    _run(dispatch(env, {}, http_client=None))
    provider, msg = host.relay_calls[-1]
    assert provider == "twitch"
    return str(json.loads(msg)["text"])


@pytest.mark.parametrize("badges", [{"is_mod": True}, {"is_broadcaster": True}])
def test_mod_or_broadcaster_announces(host: _FakeHost, badges: dict[str, bool]) -> None:
    assert _run_through(host, "!say Hello chat!", **badges) == "Hello chat!"


@pytest.mark.parametrize(
    "badges",
    [
        {},
        {"is_mod": False, "is_broadcaster": False},
        {"is_mod": "true"},
        {"is_mod": 1},
        {"is_broadcaster": "True"},
        {"is_mod": "false"},
    ],
)
def test_non_mod_is_denied_politely_not_silently(host: _FakeHost, badges: dict[str, Any]) -> None:
    assert _run_through(host, "!say sneaky", **badges) == _DENIED_MSG
    assert len(host.relay_calls) == 1  # exactly one reply, and it is the denial


def test_dispatch_rechecks_gate_against_forged_payload(host: _FakeHost) -> None:
    out = _run(transform(_event("!say hi", is_mod=False)))
    assert out is not None
    out.payload["is_mod"] = "yes"  # a non-bool must never pass the strict gate
    env = StageEnvelope(tenant="t", community="c1", app_id="x", stage="action", event=out, ts="t")
    _run(dispatch(env, {}, http_client=None))
    assert json.loads(host.relay_calls[-1][1])["text"] == _DENIED_MSG


@pytest.mark.parametrize("text", ["!hello", "say hi", "!sayy hi", ""])
def test_non_matching_dropped(host: _FakeHost, text: str) -> None:
    assert _run(transform(_event(text, is_mod=True))) is None


def test_non_string_text_dropped(host: _FakeHost) -> None:
    assert _run(transform(_event(None))) is None


def test_case_insensitive_head(host: _FakeHost) -> None:
    assert _run_through(host, "  !SAY  hi  ", is_mod=True) == "hi"


def test_bare_say_is_usage_for_mod(host: _FakeHost) -> None:
    assert _run_through(host, "!say", is_mod=True) == _USAGE


@pytest.mark.parametrize("lead", ["/", ".", "!"])
def test_unsafe_lead_refused(host: _FakeHost, lead: str) -> None:
    assert _run_through(host, f"!say {lead}ban someone", is_mod=True) == _UNSAFE_MSG


def test_too_long_refused(host: _FakeHost) -> None:
    reply = _run_through(host, "!say " + "a" * (MAX_SAY_LEN + 1), is_mod=True)
    assert reply is not None and "characters or fewer" in reply


def test_missing_channel_and_bad_action_raise(host: _FakeHost) -> None:
    ev = PlatformEvent("twitch", "chat.message", "a", {"action": "announce"}, "t")
    env = StageEnvelope(tenant="t", community="c", app_id="x", stage="action", event=ev, ts="t")
    with pytest.raises(ValueError):
        _run(dispatch(env, {}, http_client=None))
    ev2 = PlatformEvent("twitch", "chat.message", "a", {"channel_id": "1", "action": "zz"}, "t")
    env2 = StageEnvelope(tenant="t", community="c", app_id="x", stage="action", event=ev2, ts="t")
    with pytest.raises(ValueError):
        _run(dispatch(env2, {}, http_client=None))


def test_logs_never_contain_announced_text_or_actor(host: _FakeHost) -> None:
    marker = "ZZ-secret-announcement-ZZ"
    _run_through(host, f"!say {marker}", is_mod=True)
    _run_through(host, f"!say {marker}")  # denied path too
    blob = json.dumps(host.log_calls, default=str)
    assert marker not in blob
    assert "viewer-1" not in blob
    assert host.log_calls  # denominator: something was actually logged


@pytest.mark.parametrize("name", ["bundle.yaml", "hub-manifest.yaml", "README.md"])
def test_attribution_present(name: str) -> None:
    body = (_BUNDLE_DIR / name).read_text()
    assert "PenguinTwitchBot by SuperPenguinTV (with permission)" in body.replace("\n  ", " ")
    assert "no code copied" in body.replace("\n  ", " ")


def test_manifest_permissions_least_privilege() -> None:
    for name in ("bundle.yaml", "hub-manifest.yaml"):
        body = (_BUNDLE_DIR / name).read_text()
        assert 'id: "chat.send:discord"' in body
        assert 'id: "chat.send:twitch"' in body
        assert "storage.kv" not in body
